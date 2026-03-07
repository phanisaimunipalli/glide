"""
Model cascade + hedge orchestrator.

Two routing strategies, used together:

HEDGE (top N models, simultaneous):
  Broadcasts the same request to the top `hedge_top` cascade models at once.
  Whichever returns its first byte first wins — all others are cancelled.
  This is the LLM application of Google's "Tail at Scale" request hedging.
  Eliminates p95 tail latency: if one model is slow, the other wins.

CASCADE (remaining models, sequential):
  If all hedged models fail, falls through to the remaining cascade models
  tried one at a time with TTFT + TTT budget enforcement.

TTFT = time to first byte. Catches slow starts and connection issues.
TTT  = time from request start until the first *text* content block begins,
       after any thinking/reasoning tokens. Only enforced in sequential cascade.

The cascade ends when:
  - A hedged or sequential model responds (success), or
  - All models are exhausted (raises AllModelsFailedError)

Supported providers: anthropic, openai, google, ollama
All providers yield Anthropic SSE bytes internally.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator, Dict, List, Optional

import httpx

from .config import ModelConfig, settings
from .tracker import registry
from .metrics import metrics
from .translator import (
    anthropic_to_ollama,
    anthropic_to_openai,
    anthropic_to_gemini,
    stream_ollama_as_anthropic,
    stream_openai_as_anthropic,
    stream_gemini_as_anthropic,
)

logger = logging.getLogger("glide.cascade")


class TTFTTimeoutError(Exception):
    pass


class TTTTimeoutError(Exception):
    pass


class AllModelsFailedError(Exception):
    pass


def _parse_sse_buffer(buf: bytes) -> tuple:
    """
    Parse complete SSE events from a byte buffer.
    Returns (events, remaining_buf) where events is a list of
    {'event': str, 'data': dict} and remaining_buf is any trailing
    incomplete event data.
    """
    events = []
    text = buf.decode("utf-8", errors="replace")
    parts = text.split("\n\n")
    for block in parts[:-1]:
        block = block.strip()
        if not block:
            continue
        event_type = None
        event_data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                try:
                    event_data = json.loads(line[6:])
                except (json.JSONDecodeError, ValueError):
                    pass
        if event_type:
            events.append({"event": event_type, "data": event_data or {}})
    remaining = parts[-1].encode("utf-8") if parts[-1] else b""
    return events, remaining


async def _first_token_timeout(
    aiter,
    budget: Optional[float],
) -> tuple:
    """
    Race: get first chunk from aiter within budget seconds.
    Returns (first_chunk, elapsed_ttft).
    Raises TTFTTimeoutError if budget is exceeded.
    """
    start = time.monotonic()
    if budget is None:
        first_chunk = await aiter.__anext__()
        return first_chunk, time.monotonic() - start
    try:
        first_chunk = await asyncio.wait_for(aiter.__anext__(), timeout=budget)
        return first_chunk, time.monotonic() - start
    except asyncio.TimeoutError:
        raise TTFTTimeoutError(f"TTFT exceeded {budget}s budget")


async def hedge_stream(
    body: dict,
    models: List[ModelConfig],
    original_model: str,
    request_headers: dict = None,
) -> AsyncIterator[bytes]:
    """
    Broadcast request to all models simultaneously.
    Stream from whichever produces its first byte fastest.
    Cancel all losers immediately upon winner selection.

    Uses no per-model TTFT/TTT budgets — the race itself is the timeout
    (bounded by the maximum ttft_budget across hedged models).
    TTT is not enforced for the winner; once a model wins the race it
    streams its full response uninterrupted.
    """
    n = len(models)
    request_start = time.monotonic()

    # Pre-create one unbounded queue per model for chunk streaming
    chunk_queues: Dict[str, asyncio.Queue] = {m.model: asyncio.Queue() for m in models}

    # race_q receives (model_cfg, ttft) on first byte, or None on failure
    race_q: asyncio.Queue = asyncio.Queue()
    winner_event = asyncio.Event()

    async def run_model(model_cfg: ModelConfig):
        q = chunk_queues[model_cfg.model]
        announced = False
        # Strip budgets — hedge race is the only timeout
        unbudgeted = ModelConfig(
            provider=model_cfg.provider,
            model=model_cfg.model,
            ttft_budget=None,
            ttt_budget=None,
        )
        try:
            async for chunk in _try_model_stream(unbudgeted, body, original_model, request_headers):
                await q.put(chunk)
                if not announced:
                    announced = True
                    if not winner_event.is_set():
                        winner_event.set()
                        ttft = time.monotonic() - request_start
                        await race_q.put((model_cfg, ttft))
                    else:
                        # Another model already won — stop streaming to free resources
                        return
        except asyncio.CancelledError:
            pass  # cancelled as a loser — expected
        except Exception:
            if not announced:
                await race_q.put(None)  # signal this model failed the race
        finally:
            await q.put(None)  # sentinel: stream complete

    tasks = {m.model: asyncio.create_task(run_model(m)) for m in models}

    # Race: collect results until we have a winner or all have failed
    max_budget = max((m.ttft_budget or 30.0) for m in models)
    winner_cfg: Optional[ModelConfig] = None
    winner_ttft: Optional[float] = None
    fails = 0

    while winner_cfg is None:
        try:
            result = await asyncio.wait_for(race_q.get(), timeout=max_budget)
        except asyncio.TimeoutError:
            logger.warning(f"[hedge] All {n} models timed out ({max_budget}s)")
            break

        if result is None:
            fails += 1
            if fails >= n:
                logger.warning(f"[hedge] All {n} models failed")
                break
        else:
            winner_cfg, winner_ttft = result

    # Cancel all losers (and all tasks if no winner)
    for model_name, task in tasks.items():
        if winner_cfg is None or model_name != winner_cfg.model:
            task.cancel()

    if winner_cfg is None:
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        raise AllModelsFailedError("All hedged models failed")

    losers = [m.model for m in models if m.model != winner_cfg.model]
    registry.get(winner_cfg.model).record_ttft(winner_ttft)
    metrics.record_hedge_winner(winner_cfg.model)
    logger.info(
        f"[hedge] Winner: {winner_cfg.provider}/{winner_cfg.model} "
        f"TTFT={winner_ttft:.3f}s — cancelled: {', '.join(losers)}"
    )

    # Stream winner's buffered + remaining chunks
    winner_q = chunk_queues[winner_cfg.model]
    while True:
        item = await winner_q.get()
        if item is None:
            break
        yield item

    await asyncio.gather(*tasks.values(), return_exceptions=True)


def _hedge_decision(hedge_models: List[ModelConfig]) -> str:
    """
    Decide whether to hedge, go solo, or skip to sequential.

    Returns one of:
      "hedge" — broadcast top N simultaneously (model 1 is risky or cold)
      "solo"  — model 1 is healthy; send it alone, no extra cost
      "skip"  — all hedge models are risky; skip straight to sequential cascade

    Healthy = p95_ttft < budget * 0.8  (20% margin before the deadline)
    Cold    = fewer than 5 samples (we don't know yet → hedge conservatively)
    Risky   = p95_ttft >= budget * 0.8
    """
    first = hedge_models[0]
    t1 = registry.get(first.model)
    p95_1 = t1.p95()
    budget_1 = first.ttft_budget or 30.0

    # Cold start — hedge conservatively until we have data
    if p95_1 is None:
        logger.debug(f"[hedge-decision] {first.model} cold → hedge")
        return "hedge"

    healthy_threshold = budget_1 * 0.8

    # Model 1 comfortably within budget — no hedge needed
    if p95_1 < healthy_threshold:
        logger.info(
            f"[hedge-decision] {first.model} healthy "
            f"(p95={p95_1:.2f}s < {healthy_threshold:.2f}s threshold) → solo"
        )
        return "solo"

    # Model 1 is risky — check if model 2 is also risky
    if len(hedge_models) > 1:
        second = hedge_models[1]
        t2 = registry.get(second.model)
        p95_2 = t2.p95()
        budget_2 = second.ttft_budget or 30.0

        if p95_2 is not None and p95_2 >= budget_2 * 0.8:
            logger.info(
                f"[hedge-decision] both risky "
                f"({first.model} p95={p95_1:.2f}s, {second.model} p95={p95_2:.2f}s) → skip to sequential"
            )
            return "skip"

    logger.info(
        f"[hedge-decision] {first.model} risky (p95={p95_1:.2f}s ≥ {healthy_threshold:.2f}s) → hedge"
    )
    return "hedge"


async def cascade_stream(
    body: dict,
    cascade: List[ModelConfig],
    request_headers: dict = None,
) -> AsyncIterator[bytes]:
    """
    Route each request through hedge → sequential cascade.

    body: Anthropic Messages API format (already normalized by proxy).
    request_headers: forwarded from the original client request so auth
    (API key, OAuth/Pro/Max bearer token) is preserved across every attempt.
    """
    original_model = body.get("model", "unknown")
    metrics.record_request()

    # Phase 1: Smart hedge decision
    hedge_n = settings.hedge_top
    if hedge_n >= 2 and len(cascade) >= 2:
        hedge_models = cascade[:hedge_n]
        decision = _hedge_decision(hedge_models)
        metrics.record_hedge_decision(decision)

        if decision == "hedge":
            remaining = cascade[hedge_n:]
            logger.info(
                "[cascade] Hedging: "
                + " vs ".join(f"{m.provider}/{m.model}" for m in hedge_models)
            )
            try:
                async for chunk in hedge_stream(body, hedge_models, original_model, request_headers):
                    yield chunk
                return  # hedge succeeded
            except AllModelsFailedError:
                metrics.record_cascade_fallback()
                logger.warning("[cascade] Hedge failed — falling through to sequential")

        elif decision == "solo":
            remaining = cascade

        else:  # "skip"
            remaining = cascade

    else:
        remaining = cascade

    # Phase 2: Sequential cascade with TTFT + TTT budgets
    for model_cfg in remaining:
        tracker = registry.get(model_cfg.model)

        # Proactive skip: if p95 TTFT or p95 TTT already exceeds budget
        if tracker.should_skip(model_cfg.ttft_budget):
            logger.info(
                f"[cascade] Skipping {model_cfg.model} "
                f"(p95_ttft={tracker.p95():.2f}s > budget={model_cfg.ttft_budget}s)"
            )
            continue
        if tracker.should_skip_ttt(model_cfg.ttt_budget):
            logger.info(
                f"[cascade] Skipping {model_cfg.model} "
                f"(p95_ttt={tracker.ttt_p95():.2f}s > ttt_budget={model_cfg.ttt_budget}s)"
            )
            continue

        logger.info(
            f"[cascade] Trying {model_cfg.provider}/{model_cfg.model} "
            f"(budget={model_cfg.ttft_budget}s)"
        )

        try:
            async for chunk in _try_model_stream(model_cfg, body, original_model, request_headers):
                yield chunk
            return  # success — stop cascade

        except TTFTTimeoutError:
            logger.warning(
                f"[cascade] {model_cfg.model} exceeded TTFT budget — trying next"
            )
            tracker.record_ttft(model_cfg.ttft_budget or 999.0)
            continue

        except TTTTimeoutError:
            logger.warning(
                f"[cascade] {model_cfg.model} exceeded TTT budget (thinking too long) — trying next"
            )
            tracker.record_ttt(model_cfg.ttt_budget or 999.0)
            continue

        except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
            logger.warning(f"[cascade] {model_cfg.model} unreachable: {e} — trying next")
            continue

    raise AllModelsFailedError("All models in cascade failed or timed out")


async def _try_model_stream(
    model_cfg: ModelConfig,
    body: dict,
    original_model: str,
    request_headers: dict = None,
) -> AsyncIterator[bytes]:
    """
    Attempt a single model. Yields Anthropic SSE response bytes.
    Raises TTFTTimeoutError if first token exceeds budget.
    """
    if model_cfg.provider == "anthropic":
        async for chunk in _stream_anthropic(model_cfg, body, request_headers):
            yield chunk
    elif model_cfg.provider == "openai":
        async for chunk in _stream_openai(model_cfg, body, original_model, request_headers):
            yield chunk
    elif model_cfg.provider == "google":
        async for chunk in _stream_google(model_cfg, body, original_model):
            yield chunk
    elif model_cfg.provider == "ollama":
        async for chunk in _stream_ollama(model_cfg, body, original_model):
            yield chunk


async def _stream_anthropic(
    model_cfg: ModelConfig,
    body: dict,
    request_headers: dict = None,
) -> AsyncIterator[bytes]:
    patched_body = {**body, "model": model_cfg.model, "stream": True}

    headers = {
        k: v for k, v in (request_headers or {}).items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }
    headers.update({
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "accept": "text/event-stream",
    })
    has_auth = "x-api-key" in headers or "authorization" in headers
    if not has_auth and settings.anthropic_api_key:
        headers["x-api-key"] = settings.anthropic_api_key

    tracker = registry.get(model_cfg.model)
    request_start = time.monotonic()

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            f"{settings.anthropic_base_url}/v1/messages",
            json=patched_body,
            headers=headers,
        ) as resp:
            if resp.status_code >= 500:
                raise httpx.RemoteProtocolError(
                    f"HTTP {resp.status_code}", request=resp.request
                )

            byte_iter = resp.aiter_bytes()

            # Phase 1: TTFT — race first byte against budget
            first_chunk, ttft = await _first_token_timeout(byte_iter, model_cfg.ttft_budget)
            tracker.record_ttft(ttft)
            logger.info(
                f"[cascade] {model_cfg.model} TTFT={ttft:.2f}s "
                f"(budget={model_cfg.ttft_budget}s) — streaming"
            )

            yield first_chunk

            # Phase 2: TTT — parse SSE events to detect thinking→text transition.
            # We maintain a byte buffer, consume complete events, and track state:
            #   no_thinking  → model doesn't use thinking; TTT never fires
            #   in_thinking  → thinking block active; enforce ttt_budget
            #   text_started → text block started; record TTT, stop monitoring
            _buf = b""
            _in_thinking = False
            _ttt_done = False

            async for chunk in byte_iter:
                yield chunk

                if _ttt_done or model_cfg.ttt_budget is None:
                    continue

                _buf += chunk
                _buf_events, _buf = _parse_sse_buffer(_buf)

                for evt in _buf_events:
                    if evt["event"] != "content_block_start":
                        continue
                    cb_type = evt["data"].get("content_block", {}).get("type", "")
                    if cb_type == "thinking":
                        _in_thinking = True
                    elif cb_type == "text":
                        if _in_thinking:
                            # Thinking phase is over; measure total thinking time
                            ttt = time.monotonic() - request_start
                            tracker.record_ttt(ttt)
                            logger.info(
                                f"[cascade] {model_cfg.model} TTT={ttt:.2f}s "
                                f"(budget={model_cfg.ttt_budget}s)"
                            )
                            if ttt > model_cfg.ttt_budget:
                                raise TTTTimeoutError(
                                    f"{model_cfg.model} TTT {ttt:.2f}s "
                                    f"exceeded {model_cfg.ttt_budget}s budget"
                                )
                        _ttt_done = True

                # Still thinking — check elapsed against budget
                if _in_thinking and not _ttt_done:
                    elapsed = time.monotonic() - request_start
                    if elapsed > model_cfg.ttt_budget:
                        raise TTTTimeoutError(
                            f"{model_cfg.model} still thinking at {elapsed:.2f}s, "
                            f"budget={model_cfg.ttt_budget}s"
                        )


async def _stream_openai(
    model_cfg: ModelConfig,
    body: dict,
    original_model: str,
    request_headers: dict = None,
) -> AsyncIterator[bytes]:
    openai_body = anthropic_to_openai(body, model_cfg.model)
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    tracker = registry.get(model_cfg.model)

    # Build auth headers for OpenAI
    openai_headers = {
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    # Use OPENAI_API_KEY from settings; allow override via request header
    forwarded = {k.lower(): v for k, v in (request_headers or {}).items()}
    if "authorization" in forwarded and "openai" in forwarded.get("authorization", "").lower():
        openai_headers["authorization"] = forwarded["authorization"]
    elif settings.openai_api_key:
        openai_headers["authorization"] = f"Bearer {settings.openai_api_key}"

    gen = stream_openai_as_anthropic(
        settings.openai_base_url, openai_body, original_model, msg_id, openai_headers
    )

    first_chunk, ttft = await _first_token_timeout(gen, model_cfg.ttft_budget)
    tracker.record(ttft)
    logger.info(
        f"[cascade] {model_cfg.model} TTFT={ttft:.2f}s — streaming from OpenAI"
    )

    yield first_chunk
    async for chunk in gen:
        yield chunk


async def _stream_google(
    model_cfg: ModelConfig,
    body: dict,
    original_model: str,
) -> AsyncIterator[bytes]:
    gemini_body = anthropic_to_gemini(body)
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    tracker = registry.get(model_cfg.model)

    if not settings.google_api_key:
        raise httpx.ConnectError("GOOGLE_API_KEY not set", request=None)

    gen = stream_gemini_as_anthropic(
        gemini_body, model_cfg.model, original_model, msg_id, settings.google_api_key
    )

    first_chunk, ttft = await _first_token_timeout(gen, model_cfg.ttft_budget)
    tracker.record(ttft)
    logger.info(
        f"[cascade] {model_cfg.model} TTFT={ttft:.2f}s — streaming from Google Gemini"
    )

    yield first_chunk
    async for chunk in gen:
        yield chunk


async def _stream_ollama(
    model_cfg: ModelConfig,
    body: dict,
    original_model: str,
) -> AsyncIterator[bytes]:
    ollama_body = anthropic_to_ollama(body, model_cfg.model)
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    tracker = registry.get(model_cfg.model)

    gen = stream_ollama_as_anthropic(
        settings.ollama_url, ollama_body, original_model, msg_id
    )

    # Race first chunk against TTFT budget
    first_chunk, ttft = await _first_token_timeout(gen, model_cfg.ttft_budget)
    tracker.record(ttft)
    logger.info(
        f"[cascade] {model_cfg.model} TTFT={ttft:.2f}s — streaming from Ollama"
    )

    yield first_chunk
    async for chunk in gen:
        yield chunk
