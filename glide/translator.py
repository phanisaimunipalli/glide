"""
Request/response translation between all supported provider formats.

Internal canonical format: Anthropic Messages API body.
All provider streams yield Anthropic SSE bytes internally.
The proxy converts to the client's expected format at the edge.

Supported:
  Input  → normalize_to_anthropic():  Anthropic, OpenAI Chat Completions
  Output → anthropic_to_openai_sse(): convert Anthropic SSE → OpenAI SSE
  Providers: anthropic, openai, google, ollama
"""

import json
import logging
import uuid
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger("glide.translator")


# ---------------------------------------------------------------------------
# Input normalization
# ---------------------------------------------------------------------------

def normalize_to_anthropic(body: dict) -> dict:
    """
    Normalize an OpenAI Chat Completions body to Anthropic Messages format.
    Anthropic bodies pass through unchanged.
    """
    # If it already has Anthropic shape (has "messages" with role/content and
    # no "choices" key), assume it's already Anthropic format.
    if "messages" not in body:
        return body

    # Detect OpenAI shape: messages array may contain system role entries
    messages = body.get("messages", [])
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    anthropic_body = {
        "model": body.get("model", "claude-sonnet-4-6"),
        "messages": non_system,
        "max_tokens": body.get("max_tokens") or body.get("max_completion_tokens") or 4096,
        "stream": True,
    }
    if system_parts:
        anthropic_body["system"] = "\n\n".join(system_parts)
    if "temperature" in body:
        anthropic_body["temperature"] = body["temperature"]
    if "top_p" in body:
        anthropic_body["top_p"] = body["top_p"]
    return anthropic_body


# ---------------------------------------------------------------------------
# Output conversion: Anthropic SSE → OpenAI SSE
# Used at the proxy edge when the client sent an OpenAI-format request.
# ---------------------------------------------------------------------------

def anthropic_sse_to_openai_sse(chunk: bytes, chat_id: str, model: str) -> Optional[bytes]:
    """
    Convert one Anthropic SSE chunk (bytes) to an OpenAI SSE chunk (bytes).
    Returns None for event types that have no OpenAI equivalent.
    """
    text = chunk.decode("utf-8", errors="replace")
    lines = text.strip().splitlines()

    event_type = None
    data_str = None
    for line in lines:
        if line.startswith("event: "):
            event_type = line[7:].strip()
        elif line.startswith("data: "):
            data_str = line[6:].strip()

    if not event_type or not data_str:
        return None

    try:
        data = json.loads(data_str)
    except json.JSONDecodeError:
        return None

    base = {"id": chat_id, "object": "chat.completion.chunk", "created": 0, "model": model}

    if event_type == "message_start":
        payload = {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
        return f"data: {json.dumps(payload)}\n\n".encode()

    if event_type == "content_block_delta":
        text_delta = data.get("delta", {}).get("text", "")
        if not text_delta:
            return None
        payload = {**base, "choices": [{"index": 0, "delta": {"content": text_delta}, "finish_reason": None}]}
        return f"data: {json.dumps(payload)}\n\n".encode()

    if event_type == "message_delta":
        payload = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        return f"data: {json.dumps(payload)}\n\n".encode()

    if event_type == "message_stop":
        return b"data: [DONE]\n\n"

    return None


# ---------------------------------------------------------------------------
# Anthropic → Ollama
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Anthropic → OpenAI
# ---------------------------------------------------------------------------

def anthropic_to_openai(body: dict, openai_model: str) -> dict:
    """Translate Anthropic /v1/messages body → OpenAI /v1/chat/completions body."""
    messages = list(body.get("messages", []))
    if system := body.get("system"):
        messages = [{"role": "system", "content": system}] + messages

    openai_body = {
        "model": openai_model,
        "messages": messages,
        "stream": True,
        "max_tokens": body.get("max_tokens", 4096),
    }
    if "temperature" in body:
        openai_body["temperature"] = body["temperature"]
    return openai_body


# ---------------------------------------------------------------------------
# Anthropic → Gemini
# ---------------------------------------------------------------------------

def anthropic_to_gemini(body: dict) -> dict:
    """Translate Anthropic /v1/messages body → Gemini generateContent body."""
    contents = []
    for msg in body.get("messages", []):
        role = "user" if msg["role"] == "user" else "model"
        content = msg["content"]
        if isinstance(content, str):
            parts = [{"text": content}]
        elif isinstance(content, list):
            parts = []
            for block in content:
                if block.get("type") == "text":
                    parts.append({"text": block["text"]})
        else:
            parts = [{"text": str(content)}]
        contents.append({"role": role, "parts": parts})

    gemini_body: dict = {"contents": contents}

    if system := body.get("system"):
        gemini_body["systemInstruction"] = {"parts": [{"text": system}]}

    max_tokens = body.get("max_tokens")
    temp = body.get("temperature")
    gen_config = {}
    if max_tokens:
        gen_config["maxOutputTokens"] = max_tokens
    if temp is not None:
        gen_config["temperature"] = temp
    if gen_config:
        gemini_body["generationConfig"] = gen_config

    return gemini_body


# ---------------------------------------------------------------------------
# Stream: Ollama NDJSON → Anthropic SSE
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Stream: OpenAI SSE → Anthropic SSE
# ---------------------------------------------------------------------------

async def stream_openai_as_anthropic(
    openai_url: str,
    openai_body: dict,
    original_model: str,
    msg_id: str,
    headers: dict,
) -> AsyncIterator[bytes]:
    """Stream OpenAI SSE response translated to Anthropic SSE format."""

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

    output_tokens = 0

    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream(
            "POST", f"{openai_url}/v1/chat/completions",
            json=openai_body, headers=headers,
        ) as resp:
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or line == "data: [DONE]":
                    continue
                if line.startswith("data: "):
                    line = line[6:]
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                for choice in chunk.get("choices", []):
                    text = choice.get("delta", {}).get("content") or ""
                    if text:
                        output_tokens += 1
                        yield sse("content_block_delta", {
                            "type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": text},
                        })

    yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })
    yield sse("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# Stream: Gemini NDJSON → Anthropic SSE
# ---------------------------------------------------------------------------

async def stream_gemini_as_anthropic(
    gemini_body: dict,
    model: str,
    original_model: str,
    msg_id: str,
    api_key: str,
) -> AsyncIterator[bytes]:
    """Stream Gemini streamGenerateContent response translated to Anthropic SSE format."""

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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent"
    output_tokens = 0

    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream(
            "POST", url,
            json=gemini_body,
            params={"key": api_key, "alt": "sse"},
            headers={"Content-Type": "application/json"},
        ) as resp:
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data: "):
                    continue
                try:
                    chunk = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                for candidate in chunk.get("candidates", []):
                    for part in candidate.get("content", {}).get("parts", []):
                        text = part.get("text", "")
                        if text:
                            output_tokens += 1
                            yield sse("content_block_delta", {
                                "type": "content_block_delta", "index": 0,
                                "delta": {"type": "text_delta", "text": text},
                            })

    yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })
    yield sse("message_stop", {"type": "message_stop"})
