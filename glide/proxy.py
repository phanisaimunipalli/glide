"""
FastAPI proxy server for glide.

Intercepts /v1/messages (Anthropic) and /v1/chat/completions (OpenAI) and
runs them through the model cascade. All other paths pass through to the
upstream (Anthropic by default).

Input formats accepted:
  POST /v1/messages           — Anthropic Messages API
  POST /v1/chat/completions   — OpenAI Chat Completions API

All cascade providers yield Anthropic SSE internally. If the client sent
an OpenAI-format request, the response is converted back to OpenAI SSE.
"""

import json
import logging
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from .cascade import AllModelsFailedError, cascade_stream
from .config import settings
from .tracker import registry
from .translator import normalize_to_anthropic, anthropic_sse_to_openai_sse

logger = logging.getLogger("glide.proxy")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cascade = settings.get_cascade()
    logger.info("=" * 60)
    logger.info("  glide proxy started")
    logger.info(f"  Listening : http://{settings.proxy_host}:{settings.proxy_port}")
    logger.info(f"  Cascade   :")
    for i, m in enumerate(cascade):
        budget = f"{m.ttft_budget}s" if m.ttft_budget else "no limit"
        logger.info(f"    {i+1}. {m.provider}/{m.model} (TTFT budget: {budget})")
    logger.info("=" * 60)
    yield


app = FastAPI(title="glide", version="0.2.0", lifespan=lifespan)


@app.get("/_glide/status")
async def status():
    """Inspect cascade configuration and per-model latency stats."""
    cascade = settings.get_cascade()
    env_key_set = bool(settings.anthropic_api_key)
    return {
        "auth": {
            "env_api_key_set": env_key_set,
            "note": (
                "API key mode (env)" if env_key_set
                else "Passthrough mode — Pro/Max/OAuth auth forwarded from client"
            ),
        },
        "cascade": [
            {
                "provider": m.provider,
                "model": m.model,
                "ttft_budget": m.ttft_budget,
                "ttt_budget": m.ttt_budget,
                "latency": registry.get(m.model).stats(),
            }
            for m in cascade
        ],
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    body_bytes = await request.body()

    is_messages = path == "v1/messages" and request.method == "POST"
    is_chat = path == "v1/chat/completions" and request.method == "POST"

    if (is_messages or is_chat) and body_bytes:
        raw_body = json.loads(body_bytes)
        cascade = settings.get_cascade()

        request_headers = _extract_headers(request)
        auth_mode = _detect_auth_mode(request_headers)
        logger.info(f"[proxy] path={path} auth={auth_mode}")

        # Normalize to Anthropic format internally
        body = normalize_to_anthropic(raw_body) if is_chat else raw_body

        try:
            if is_chat:
                # Client expects OpenAI SSE back
                chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
                model = body.get("model", "unknown")
                return StreamingResponse(
                    _wrap_as_openai_sse(
                        cascade_stream(body, cascade, request_headers),
                        chat_id,
                        model,
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "X-Glide": "true",
                    },
                )
            else:
                # Client expects Anthropic SSE back
                return StreamingResponse(
                    cascade_stream(body, cascade, request_headers),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "X-Glide": "true",
                        "X-Glide-Auth": auth_mode,
                    },
                )
        except AllModelsFailedError:
            logger.error("[proxy] All cascade models failed")
            return Response(
                content=json.dumps({
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": "All models in cascade failed or timed out",
                    },
                }),
                status_code=503,
                media_type="application/json",
            )

    return await passthrough(request, path, body_bytes)


async def _wrap_as_openai_sse(anthropic_stream, chat_id: str, model: str):
    """Translate Anthropic SSE stream → OpenAI SSE stream."""
    async for chunk in anthropic_stream:
        converted = anthropic_sse_to_openai_sse(chunk, chat_id, model)
        if converted:
            yield converted


def _extract_headers(request: Request) -> dict:
    """Strip hop-by-hop headers, keep everything else including auth."""
    return {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }


def _detect_auth_mode(headers: dict) -> str:
    """Detect which auth mode the client is using."""
    if "x-api-key" in headers:
        return "api-key"
    if "authorization" in headers:
        auth = headers["authorization"].lower()
        if auth.startswith("bearer"):
            return "oauth-bearer"
        return "authorization"
    if settings.anthropic_api_key:
        return "env-api-key"
    return "none"


async def passthrough(request: Request, path: str, body_bytes: bytes):
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }
    has_auth = "x-api-key" in headers or "authorization" in headers
    if not has_auth and settings.anthropic_api_key:
        headers["x-api-key"] = settings.anthropic_api_key
    url = f"{settings.anthropic_base_url}/{path}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.request(request.method, url, content=body_bytes, headers=headers)
    return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
