"""
Model cascade orchestrator.

Tries each model in the configured cascade in order. For each model:
  1. Check if p95 TTFT already exceeds budget (proactive skip)
  2. Send the request and measure time-to-first-token
  3. If TTFT exceeds budget → record, skip to next model
  4. If request succeeds → record TTFT, stream response

The cascade ends when:
  - A model responds within budget (success), or
  - All models are exhausted (raises AllModelsFailedError)
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
from .translator import anthropic_to_ollama, stream_ollama_as_anthropic

logger = logging.getLogger("glide.cascade")


class TTFTTimeoutError(Exception):
    pass


class AllModelsFailedError(Exception):
    pass


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


async def cascade_stream(body: dict, cascade: List[ModelConfig]) -> AsyncIterator[bytes]:
    """
    Try each model in cascade order, yielding a streaming response
    from the first model that responds within its TTFT budget.
    """
    original_model = body.get("model", "unknown")

    for model_cfg in cascade:
        tracker = registry.get(model_cfg.model)

        # Proactive skip: if p95 already exceeds budget, don't bother
        if tracker.should_skip(model_cfg.ttft_budget):
            logger.info(
                f"[cascade] Skipping {model_cfg.model} "
                f"(p95={tracker.p95():.2f}s > budget={model_cfg.ttft_budget}s)"
            )
            continue

        logger.info(
            f"[cascade] Trying {model_cfg.provider}/{model_cfg.model} "
            f"(budget={model_cfg.ttft_budget}s)"
        )

        try:
            async for chunk in _try_model_stream(model_cfg, body, original_model):
                yield chunk
            return  # success — stop cascade

        except TTFTTimeoutError:
            logger.warning(
                f"[cascade] {model_cfg.model} exceeded TTFT budget — trying next"
            )
            tracker.record(model_cfg.ttft_budget or 999.0)
            continue

        except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
            logger.warning(f"[cascade] {model_cfg.model} unreachable: {e} — trying next")
            continue

    raise AllModelsFailedError("All models in cascade failed or timed out")


async def _try_model_stream(
    model_cfg: ModelConfig,
    body: dict,
    original_model: str,
) -> AsyncIterator[bytes]:
    """
    Attempt a single model. Yields response bytes.
    Raises TTFTTimeoutError if first token exceeds budget.
    """
    if model_cfg.provider == "anthropic":
        async for chunk in _stream_anthropic(model_cfg, body):
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

    # Preserve original auth from the incoming request.
    # Supports API key users AND Max plan users (OAuth session auth).
    # Only inject ANTHROPIC_API_KEY as fallback if no auth header is present.
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

            # Race first chunk against TTFT budget
            first_chunk, ttft = await _first_token_timeout(byte_iter, model_cfg.ttft_budget)
            tracker.record(ttft)
            logger.info(
                f"[cascade] {model_cfg.model} TTFT={ttft:.2f}s "
                f"(budget={model_cfg.ttft_budget}s) — streaming"
            )

            yield first_chunk
            async for chunk in byte_iter:
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
