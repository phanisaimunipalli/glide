# Using glide with Claude Code

## Setup

```bash
pip install glide
```

**Max / Pro plan (no API key needed):**
```bash
glide start
export ANTHROPIC_BASE_URL=http://127.0.0.1:8743
claude
```

**API key:**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
glide start
export ANTHROPIC_BASE_URL=http://127.0.0.1:8743
claude
```

**Persist across sessions** — add to `~/.zshrc`:
```bash
echo 'export ANTHROPIC_BASE_URL=http://127.0.0.1:8743' >> ~/.zshrc
```

**Check live cascade status:**
```bash
glide status
```

## What happens on a slow response

Without glide (opus takes 12s → you wait 12s or timeout):
```
Claude Code → Anthropic (opus) → 12s wait → response
```

With glide (opus TTFT budget=4s, TTT budget=10s):
```
Claude Code → glide → opus TTFT ok, but thinking > 10s → cascade to sonnet
Claude Code → glide → sonnet (0.3s TTFT, no thinking) → response in ~10.3s
```

After a few slow opus responses, proactive routing kicks in:
```
Claude Code → glide → skip opus (p95_ttt=11s > 10s budget) → sonnet directly → response in ~0.3s
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
      "ttft_budget": 4.0,
      "ttt_budget": 10.0,
      "latency": {
        "ttft": {"samples": 12, "p95": 1.2, "mean": 0.9},
        "ttt":  {"samples": 8,  "p95": 11.2, "mean": 9.1}
      }
    },
    {
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "ttft_budget": 5.0,
      "ttt_budget": 10.0,
      "latency": {
        "ttft": {"samples": 8, "p95": 0.4, "mean": 0.3},
        "ttt":  {"samples": 0, "p95": null, "mean": null}
      }
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
