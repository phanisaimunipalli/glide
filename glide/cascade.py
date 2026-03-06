"""
Model cascade orchestrator.

Tries each model in the configured cascade in order. For each model:
  1. Check if p95 TTFT or p95 TTT already exceeds budget (proactive skip)
  2. Send the request and measure time-to-first-token (TTFT)
  3. If TTFT exceeds budget → record, skip to next model
  4. If model has extended thinking, parse SSE events and measure TTT
  5. If TTT exceeds ttt_budget → record, abort stream, skip to next model
  6. If request succeeds → record TTFT + TTT, stream response

TTFT = time to first byte. Catches slow starts and connection issues.
TTT  = time from request start until the first *text* content block begins,
       i.e. after any thinking/reasoning tokens complete. Only fires for
       models with extended thinking. For regular models TTT is never tracked.

The cascade ends when:
  - A model responds within budget (success), or
  - All models are exhausted (raises AllModelsFailedError)

Supported providers: anthropic, openai, google, ollama
All providers yield Anthropic SSE bytes internally.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator, List, Optional

import httpx

from .config import ModelConfig, settings
from .tracker import registry
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


async def cascade_stream(
    body: dict,
    cascade: List[ModelConfig],
    request_headers: dict = None,
) -> AsyncIterator[bytes]:
    """
    Try each model in cascade order, yielding a streaming response
    from the first model that responds within its TTFT budget.

    body: Anthropic Messages API format (already normalized by proxy).
    request_headers: forwarded from the original client request so auth
    (API key, OAuth/Pro/Max bearer token) is preserved across every attempt.
    """
    original_model = body.get("model", "unknown")

    for model_cfg in cascade:
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
