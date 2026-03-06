"""
Request/response translation between Anthropic and Ollama API formats.
(Adapted and extended from llm-circuit for multi-model cascade context.)
"""

import json
import uuid
import logging
from typing import AsyncIterator

import httpx

logger = logging.getLogger("glide.translator")


def anthropic_to_ollama(body: dict, ollama_model: str) -> dict:
    """Translate Anthropic /v1/messages body → Ollama /api/chat body."""
    messages = list(body.get("messages", []))

    if system := body.get("system"):
        messages = [{"role": "system", "content": system}] + messages

    return {
        "model": ollama_model,
        "messages": messages,
        "stream": True,
        "options": {
            "num_predict": body.get("max_tokens", 4096),
            "temperature": body.get("temperature", 1.0),
        },
    }


async def stream_ollama_as_anthropic(
    ollama_url: str,
    ollama_body: dict,
    original_model: str,
    msg_id: str,
) -> AsyncIterator[bytes]:
    """Stream Ollama NDJSON response translated to Anthropic SSE format."""

    def sse(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

    yield sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [], "model": original_model,
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    yield sse("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    })
    yield sse("ping", {"type": "ping"})

    output_tokens = 0
    input_tokens = 0

    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream("POST", f"{ollama_url}/api/chat", json=ollama_body) as resp:
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not chunk.get("done"):
                    text = chunk.get("message", {}).get("content", "")
                    if text:
                        output_tokens += 1
                        yield sse("content_block_delta", {
                            "type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": text},
                        })
                else:
                    input_tokens = chunk.get("prompt_eval_count", 0)
                    output_tokens = chunk.get("eval_count", output_tokens)

    yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })
    yield sse("message_stop", {"type": "message_stop"})
