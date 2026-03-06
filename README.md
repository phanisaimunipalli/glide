<div align="center">

# 🪂 glide

### Latency-aware model cascade for agentic LLM workflows.

**Auto-switches to a faster model when yours is slow — before you ever timeout.**

[![PyPI version](https://img.shields.io/pypi/v/glide.svg?style=flat-square)](https://pypi.org/project/glide/)
[![Python](https://img.shields.io/pypi/pyversions/glide.svg?style=flat-square)](https://pypi.org/project/glide/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-13%20passing-brightgreen?style=flat-square)]()

```bash
pip install glide
glide start                                        # Max plan users: no key needed
export ANTHROPIC_API_KEY=sk-ant-...               # API key users: set this first
export ANTHROPIC_BASE_URL=http://127.0.0.1:8743
```

*That's the entire setup.*

</div>

---

## The problem

Claude Opus is the best model for complex coding tasks. But under load, it can take **12–15 seconds** to return its first word. For a developer mid-task in an AI coding agent, this feels identical to a crash.

> *Without glide: wait 15 seconds, or manually switch models.*
> *With glide: response arrives in ~2 seconds, automatically.*

---

## How it works

glide runs as a transparent proxy between your AI agent and the Anthropic API. It maintains a cascade of models, each with a **time-to-first-token (TTFT) budget**. When a model is too slow, glide aborts early and tries the next one — before you experience the timeout.

```
┌─────────────────────────────────────────────────────────┐
│                    🪂 glide proxy                        │
│                                                         │
│  Request → claude-opus    (budget: 8s)                  │
│               │                                         │
│            slow? ──yes──► claude-sonnet (budget: 5s)   │
│                                │                        │
│                             slow? ──yes──► claude-haiku │
│                                                │        │
│                                             slow? ──►  │
│                                          qwen2.5:14b   │
│                                        (local Ollama)  │
└─────────────────────────────────────────────────────────┘
```

### The smart part: proactive routing

glide tracks a **rolling p95 TTFT** per model. If opus is consistently slow, glide skips it without waiting — routing directly to sonnet before the timeout even starts.

```
Normal day    → opus p95=2s   → serves requests in ~2s
Peak load     → opus p95=11s  → skipped, sonnet serves in ~1.5s
Recovery      → opus p95=3s   → resumes automatically
```

No restarts. No config changes. No intervention.

---

## Default cascade

| # | Model | TTFT Budget | Role |
|---|---|---|---|
| 1 | `claude-opus-4-6` | 8s | Best quality, tried first |
| 2 | `claude-sonnet-4-6` | 5s | Fast + high quality |
| 3 | `claude-haiku-4-5` | 3s | Fastest Anthropic model |
| 4 | `qwen2.5:14b` (Ollama) | no limit | Local fallback, always works |

---

## Quick start

**Prerequisites:** Python 3.9+, [Ollama](https://ollama.ai) with a model pulled

```bash
# 1. Install
pip install glide

# 2. Start the proxy
# API key users:
export ANTHROPIC_API_KEY=sk-ant-...
# Max plan / OAuth users: skip the above — glide passes your session auth through
glide start

# Output:
# ============================================================
#   🪂 glide proxy started
#   Listening : http://127.0.0.1:8743
#   Cascade   :
#     1. anthropic/claude-opus-4-6    (TTFT budget: 8s)
#     2. anthropic/claude-sonnet-4-6  (TTFT budget: 5s)
#     3. anthropic/claude-haiku-4-5   (TTFT budget: 3s)
#     4. ollama/qwen2.5:14b           (TTFT budget: no limit)
# ============================================================

# 3. Point your agent at glide
export ANTHROPIC_BASE_URL=http://127.0.0.1:8743
```

Works with **Claude Code**, **Cursor**, or any tool using the Anthropic Messages API **or OpenAI Chat Completions API**.

---

## Multi-provider cascade

glide is **provider-agnostic**. Mix Anthropic, OpenAI, Gemini, and Ollama models in a single cascade — glide normalizes formats internally and routes to wherever response comes first.

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # optional
export OPENAI_API_KEY=sk-...          # optional
export GOOGLE_API_KEY=AIza...         # optional

export CASCADE_JSON='[
  {"provider": "anthropic", "model": "claude-opus-4-6",      "ttft_budget": 4.0},
  {"provider": "openai",    "model": "gpt-4o",               "ttft_budget": 5.0},
  {"provider": "google",    "model": "gemini-2.0-flash",     "ttft_budget": 3.0},
  {"provider": "ollama",    "model": "qwen2.5:14b",           "ttft_budget": null}
]'
glide start
```

**Accepted input formats:**
- `POST /v1/messages` — Anthropic Messages API
- `POST /v1/chat/completions` — OpenAI Chat Completions API

glide detects the input format and returns the matching response format automatically.

---

## Live status

```bash
curl http://127.0.0.1:8743/_glide/status | python -m json.tool
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
    },
    {
      "model": "claude-haiku-4-5",
      "ttft_budget": 3.0,
      "latency": { "samples": 3, "p95": 1.1, "mean": 1.0, "min": 0.9, "max": 1.1 }
    },
    {
      "model": "qwen2.5:14b",
      "ttft_budget": null,
      "latency": { "samples": 0, "p95": null, "mean": null }
    }
  ]
}
```

Responses served through a fallback model include the header `X-Glide-Model: claude-sonnet-4-6`.

---

## Configuration

All config via environment variables or `.env` file:

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | optional | API key users only — Max plan / OAuth users omit this |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Anthropic upstream endpoint |
| `OPENAI_API_KEY` | optional | Required when using `openai` provider in cascade |
| `OPENAI_BASE_URL` | `https://api.openai.com` | OpenAI upstream endpoint |
| `GOOGLE_API_KEY` | optional | Required when using `google` provider in cascade |
| `OLLAMA_URL` | `http://localhost:11434` | Local Ollama instance |
| `CASCADE_JSON` | see defaults | Custom cascade as a JSON array |
| `PROACTIVE_SKIP` | `true` | Skip models whose p95 > budget |
| `TRACKER_WINDOW` | `20` | Rolling window size for p95 |
| `PROXY_PORT` | `8743` | Port glide listens on |

### Custom cascade

```bash
export CASCADE_JSON='[
  {"provider": "anthropic", "model": "claude-sonnet-4-6", "ttft_budget": 4.0},
  {"provider": "anthropic", "model": "claude-haiku-4-5",  "ttft_budget": 2.0},
  {"provider": "ollama",    "model": "qwen2.5:14b",        "ttft_budget": null}
]'
glide start
```

---

## The pattern behind glide

glide introduces the **LLM Request Cascade Pattern** — a new reliability primitive that applies latency-aware routing to the specific semantics of LLM APIs: streaming token output, time-to-first-token as a health signal, and heterogeneous model quality tiers.

Unlike standard retry logic (re-tries the same model) or load balancing (distributes across identical instances), the LLM Request Cascade routes across **heterogeneous model tiers** using observed p95 latency to make real-time routing decisions.

Read the full pattern documentation → [`docs/the-cascade-pattern.md`](docs/the-cascade-pattern.md)

---

## Use with llm-circuit for full resilience

glide handles **slowness**. [llm-circuit](https://github.com/phanisaimunipalli/llm-circuit) handles **outages**. Use both together:

```
Your AI Agent
     │
  🪂 glide          ← slow response? cascade to faster model
     │
  ⚡ llm-circuit    ← full outage? switch to local Ollama
     │
 ┌───┴────┐
API     Ollama
```

Together they form the **Agentic Reliability Stack** — your AI coding agent keeps working through anything.

---

## What makes this different

| | LiteLLM | llm-circuit | 🪂 glide |
|---|---|---|---|
| Outage failover | ✓ (manual) | ✓ (auto) | ✓ (via cascade) |
| Latency-aware routing | ✗ | ✗ | ✓ |
| TTFT budget enforcement | ✗ | ✗ | ✓ |
| Proactive p95 routing | ✗ | ✗ | ✓ |
| Zero config change | ✗ | ✓ | ✓ |

---

## Development

```bash
git clone https://github.com/phanisaimunipalli/glide
cd glide
pip install -e ".[dev]"
pytest tests/ -v   # 13 tests, all passing
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for areas where help is needed.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Citation

If you reference the LLM Request Cascade Pattern in research or writing:

```
Munipalli, Phani Sai Ram. "glide: LLM Request Cascade Pattern for Agentic Workflows."
GitHub, 2026. https://github.com/phanisaimunipalli/glide
```

---

<div align="center">

Built for developers who can't afford to wait.

**[⭐ Star on GitHub](https://github.com/phanisaimunipalli/glide)** · **[📖 Pattern Docs](docs/the-cascade-pattern.md)** · **[🐛 Issues](https://github.com/phanisaimunipalli/glide/issues)**

</div>
