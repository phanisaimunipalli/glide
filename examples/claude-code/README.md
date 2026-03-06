# Using glide with Claude Code

## Setup

```bash
pip install glide

export ANTHROPIC_API_KEY=sk-ant-...
glide start
# Proxy listening on http://127.0.0.1:8743
```

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8743
claude  # Claude Code now uses the cascade
```

## What happens on a slow response

Without glide (opus takes 12s → you wait 12s or timeout):
```
Claude Code → Anthropic (opus) → 12s wait → response
```

With glide (opus budget is 8s → switches to sonnet):
```
Claude Code → glide → opus (8s budget exceeded) → sonnet (2s TTFT) → response
Total wait: ~10s instead of 12s, and you never timeout
```

After a few slow opus responses, proactive routing kicks in:
```
Claude Code → glide → skip opus (p95=11s > 8s budget) → sonnet (2s) → response
Total wait: ~2s
```

## Check cascade status and latency stats

```bash
curl http://127.0.0.1:8743/_glide/status | python -m json.tool
```

```json
{
  "cascade": [
    {
      "provider": "anthropic",
      "model": "claude-opus-4-6",
      "ttft_budget": 8.0,
      "latency": {"samples": 12, "p95": 11.2, "mean": 9.1}
    },
    {
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "ttft_budget": 5.0,
      "latency": {"samples": 8, "p95": 2.3, "mean": 1.8}
    }
  ]
}
```

## Custom cascade

Override via environment variable:

```bash
export CASCADE_JSON='[
  {"provider": "anthropic", "model": "claude-sonnet-4-6", "ttft_budget": 5.0},
  {"provider": "ollama",    "model": "qwen2.5:14b",        "ttft_budget": null}
]'
glide start
```

## Use with llm-circuit (recommended)

Run both for full coverage — cascade handles slowness, circuit breaker handles outages:

```
ANTHROPIC_BASE_URL=http://127.0.0.1:8743  # glide
glide routes through llm-circuit which routes to Anthropic or Ollama
```
