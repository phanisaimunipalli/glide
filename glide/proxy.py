"""
FastAPI proxy server for glide.

Intercepts /v1/messages and runs it through the model cascade.
All other paths pass through to Anthropic unchanged.
"""

import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from .cascade import AllModelsFailedError, cascade_stream
from .config import settings
from .tracker import registry

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


app = FastAPI(title="glide", version="0.1.0", lifespan=lifespan)


@app.get("/_glide/status")
async def status():
    """Inspect cascade configuration and per-model latency stats."""
    cascade = settings.get_cascade()
    return {
        "cascade": [
            {
                "provider": m.provider,
                "model": m.model,
                "ttft_budget": m.ttft_budget,
                "latency": registry.get(m.model).stats(),
            }
            for m in cascade
        ]
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    body_bytes = await request.body()

    if path == "v1/messages" and request.method == "POST":
        body = json.loads(body_bytes) if body_bytes else {}
        cascade = settings.get_cascade()

        try:
            return StreamingResponse(
                cascade_stream(body, cascade),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-Glide": "true",
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


async def passthrough(request: Request, path: str, body_bytes: bytes):
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }
    # Pass through original auth (x-api-key or Authorization).
    # Supports API key users and Max plan users (OAuth session auth).
    # Only inject ANTHROPIC_API_KEY as fallback if no auth is present.
    has_auth = "x-api-key" in headers or "authorization" in headers
    if not has_auth and settings.anthropic_api_key:
        headers["x-api-key"] = settings.anthropic_api_key
    url = f"{settings.anthropic_base_url}/{path}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.request(request.method, url, content=body_bytes, headers=headers)
    return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
