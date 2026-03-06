# llm-relay

**Latency-aware model cascade for agentic LLM workflows.**
Automatically switches to a faster model when your preferred model is too slow — before you timeout.

[![PyPI version](https://img.shields.io/pypi/v/llm-relay.svg)](https://pypi.org/project/llm-relay/)
[![Python](https://img.shields.io/pypi/pyversions/llm-relay.svg)](https://pypi.org/project/llm-relay/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## The problem

Claude Opus is the best model for complex coding tasks. But under load, it can take 12–15 seconds to return its first word. For a developer mid-task in an AI coding agent, this feels identical to a crash.

Current solutions: wait it out, or manually switch models.

**llm-relay fixes this automatically.**

---

## What it does

llm-relay is a transparent proxy implementing the **LLM Request Cascade Pattern** — a new latency-aware routing strategy for agentic AI workflows.

It maintains an ordered cascade of models with per-model **time-to-first-token (TTFT) budgets**. When a model exceeds its budget, llm-relay aborts the request early and retries on the next model — delivering a response within your latency budget instead of timing out.

```
Default cascade:
  1. claude-opus-4-6    → 8s TTFT budget  (best quality)
  2. claude-sonnet-4-6  → 5s TTFT budget  (faster)
  3. claude-haiku-4-5   → 3s TTFT budget  (fastest)
  4. qwen2.5:14b        → no limit        (local Ollama, always works)
```

---

## Quick start

```bash
pip install llm-relay

export ANTHROPIC_API_KEY=sk-ant-...
llm-relay start

# Point Claude Code at the proxy
export ANTHROPIC_BASE_URL=http://127.0.0.1:8743
```

That's it. llm-relay handles the rest.

---

## How it works

```
Request → Try opus (8s budget)
              │
         First token in 2s? → stream full response ✓
         First token in 10s? → abort, try sonnet
                                   │
                              First token in 1.5s? → stream ✓
```

### Proactive routing (the smart part)

llm-relay tracks a rolling window of observed TTFT values per model and computes **p95 latency** continuously. If a model's p95 already exceeds its budget, it is **skipped without waiting**.

```
Normal day:    opus (p95=2s)  → serves most requests
Peak load:     opus (p95=11s) → skipped, sonnet serves instead
Recovery:      opus (p95=3s)  → resumes serving
```

You don't wait for opus to timeout again — the proxy learned it's slow and routes around it proactively.

---

## Inspect the cascade

```bash
curl http://127.0.0.1:8743/_llm_relay/status
```

```json
{
  "cascade": [
    {
      "model": "claude-opus-4-6",
      "ttft_budget": 8.0,
      "latency": { "samples": 20, "p95": 11.2, "mean": 9.1, "min": 1.2, "max": 14.3 }
    },
    {
      "model": "claude-sonnet-4-6",
      "ttft_budget": 5.0,
      "latency": { "samples": 14, "p95": 2.1, "mean": 1.7, "min": 0.8, "max": 3.2 }
    }
  ]
}
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | required | Your Anthropic API key |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Upstream endpoint |
| `OLLAMA_URL` | `http://localhost:11434` | Local Ollama instance |
| `CASCADE_JSON` | see defaults | Custom cascade as JSON array |
| `PROACTIVE_SKIP` | `true` | Skip models whose p95 > budget |
| `TRACKER_WINDOW` | `20` | Rolling window size for p95 |
| `PROXY_PORT` | `8743` | Port the proxy listens on |

### Custom cascade

```bash
export CASCADE_JSON='[
  {"provider": "anthropic", "model": "claude-sonnet-4-6", "ttft_budget": 4.0},
  {"provider": "anthropic", "model": "claude-haiku-4-5",  "ttft_budget": 2.0},
  {"provider": "ollama",    "model": "qwen2.5:14b",        "ttft_budget": null}
]'
```

---

## The pattern

This project introduces the **LLM Request Cascade Pattern** — a new reliability primitive that applies latency-aware routing to the specific semantics of LLM APIs: streaming token output, time-to-first-token as a health signal, and heterogeneous model quality tiers.

Read the full pattern documentation: [`docs/the-cascade-pattern.md`](docs/the-cascade-pattern.md)

---

## The Agentic Reliability Stack

Use llm-relay with [llm-circuit](https://github.com/phanisaimunipalli/llm-circuit) for complete coverage:

```
Claude Code
    │
llm-relay    ← handles slowness (latency cascade)
    │
llm-circuit  ← handles outages (circuit breaker)
    │
 ┌──┴──┐
API  Ollama
```

- llm-relay catches slow responses and routes to faster models
- llm-circuit catches full outages and routes to local Ollama
- Together: your AI coding agent keeps working through anything

---

## Comparison

| Tool | What it does | Latency-aware? | Proactive routing? |
|---|---|---|---|
| LiteLLM | Multi-provider routing | No | No |
| llm-circuit | Outage circuit breaker | No | No |
| **llm-relay** | TTFT-budget cascade | **Yes** | **Yes** |

---

## Development

```bash
git clone https://github.com/phanisaimunipalli/llm-relay
cd llm-relay
pip install -e ".[dev]"
pytest tests/ -v
```

---

## License

MIT — see [LICENSE](LICENSE).

---

## Citation

```
Munipalli, Phani Sai Ram. "llm-relay: LLM Request Cascade Pattern for Agentic Workflows."
GitHub, 2026. https://github.com/phanisaimunipalli/llm-relay
```
