# The LLM Request Cascade Pattern

*A latency-aware routing primitive for agentic AI development workflows.*

---

## Background

The circuit breaker pattern (see [llm-circuit](https://github.com/phanisaimunipalli/llm-circuit)) addresses binary failure: a provider is either up or down. But in practice, the most common degradation mode for LLM APIs is not failure — it is **slowness**.

During peak load, a frontier model like Claude Opus may take 12–15 seconds to return its first token. For a developer mid-task in an AI coding agent, this is indistinguishable from a timeout. The agent feels broken. Work stops.

This document defines the **LLM Request Cascade Pattern**: a latency-aware routing strategy that automatically moves requests to faster models when the preferred model is too slow — before the user experiences a timeout.

---

## The Core Insight: Latency Signals for Streaming LLMs

Existing LLM proxy tools measure health in binary terms (up/down) or after full response completion (total latency). Neither is useful for streaming agentic workflows.

The LLM Request Cascade Pattern defines two actionable latency signals:

### TTFT — Time to First Token

The elapsed time from request dispatch to receipt of the first response byte.

- TTFT is what the user *feels* at the start — it's when the agent appears to begin responding
- TTFT spikes under server load before connection errors appear
- TTFT can be measured mid-stream and acted on *before the full response completes*

If TTFT exceeds a threshold, **abort the request early and retry on the next model**.

### TTT — Time to Think

For models with extended reasoning (e.g. Claude Opus with extended thinking, OpenAI o1/o3), the response stream contains a *thinking block* before any text is emitted. During this phase, the user sees no output despite the connection being active.

TTT measures the elapsed time from request start until the first *text content block* begins — i.e. after all reasoning tokens have streamed.

- TTT catches runaway reasoning loops that TTFT misses
- A model may have fast TTFT (thinking tokens start immediately) but still spend 30+ seconds in the reasoning phase
- TTT budget enforcement aborts the stream mid-think and cascades to the next model if the budget is exceeded

For models without extended thinking, TTT is never measured — the signal does not fire.

---

## The Pattern

### Cascade Configuration

An ordered list of models with per-model TTFT and TTT budgets:

```
1. claude-opus-4-6    TTFT: 4s  TTT: 10s  (best quality, try first)
2. claude-sonnet-4-6  TTFT: 5s  TTT: 10s  (faster, good quality)
3. claude-haiku-4-5   TTFT: 3s  TTT: —    (fastest Anthropic, no thinking)
4. qwen2.5:14b        no limit  TTT: —    (local Ollama, always available)
```

Providers are not limited to a single vendor. A cascade may mix Anthropic, OpenAI, Google Gemini, and local Ollama models. glide normalizes request formats internally.

### Request Flow

```
Request arrives
      │
      ▼
Try Model 1 (opus)
      │
  ┌───┴──────────────────────────────────────┐
  │ Phase 1 — TTFT check                     │
  │   Race first byte against 4s budget      │
  │   Timeout? → abort, try Model 2          │
  │                                          │
  │ Phase 2 — TTT check (if thinking)        │
  │   Parse SSE events for thinking block    │
  │   Thinking exceeds 10s? → abort, next    │
  │   Text block starts in time? → stream ✓  │
  └───┬──────────────────────────────────────┘
      │ (either budget exceeded)
      ▼
Try Model 2 (sonnet)
      │
      ... (TTFT: 5s, TTT: 10s)
      │
      ▼
Try Model 3 (haiku)
      │
      ... (TTFT: 3s only — no thinking)
      │
      ▼
Try Model 4 (local Ollama)
      │
  No budget → always completes
      │
      ▼
Response delivered ✓
```

### Proactive Routing

A naive cascade waits for each model to timeout before trying the next. This compounds latency: if opus takes 8s to timeout and sonnet takes 5s, the user waits 13s before reaching haiku.

The LLM Request Cascade Pattern introduces **proactive routing**: a rolling window of observed TTFT values is maintained per model. The p95 TTFT is computed continuously. If a model's p95 already exceeds its budget, it is **skipped without waiting** — the request goes directly to the next model.

This means the cascade learns from history. After a period of opus slowness, the proxy stops wasting time attempting it and routes directly to sonnet, delivering responses within the sonnet budget rather than the opus budget plus wait time.

---

## Relation to Prior Art

### Circuit Breaker (llm-circuit)
The circuit breaker detects binary failure (up/down) and routes to a fallback. It does not measure latency or operate within-provider. The cascade complements the circuit breaker: use both together for full reliability coverage.

### Retry Logic
Standard retry logic re-attempts the same endpoint. The cascade moves to a *different model* on slow response — a different action with different tradeoffs (speed vs. quality).

### Load Balancing
Load balancers distribute requests across identical instances. The cascade routes across *heterogeneous models* with explicit quality/speed tradeoffs, using observed latency to make routing decisions.

### Tail Latency Hedging
Google's "The Tail at Scale" (Dean & Barroso, 2013) describes request hedging: sending the same request to two replicas and using whichever responds first. The cascade applies an analogous idea across model tiers rather than identical replicas — a generalization appropriate for the heterogeneous LLM ecosystem.

---

## Implementation Considerations

### Aborting a streaming request mid-TTFT wait
The challenge is that HTTP streaming requests are long-lived. To implement TTFT timeout, the proxy must:
1. Begin the streaming request
2. Race `asyncio.wait_for` against receipt of the first byte
3. On timeout: cancel the connection, do not yield any bytes to the client
4. Retry on next model

This requires careful resource cleanup to avoid connection leaks.

### Aborting a streaming request mid-think (TTT)
TTT enforcement is more complex than TTFT because the stream is already active when the budget fires. Implementation:
1. After TTFT passes, maintain a byte buffer and parse complete SSE events (delimited by `\n\n`)
2. Track state: `no_thinking` → `in_thinking` → `text_started`
3. On `content_block_start` with `type=thinking` → enter `in_thinking`, start TTT clock
4. On `content_block_start` with `type=text` → record TTT; if TTT > budget → raise and cascade
5. On each chunk while `in_thinking`: check elapsed time; if > budget → raise and cascade

The yield-then-check pattern ensures all chunks are forwarded to the client immediately while monitoring runs inline.

### Quality degradation signaling
When the cascade falls back to a lower-tier model, the response header `X-Glide: true` is always present. The `message_start` event in the SSE stream contains the actual model name that served the response.

### Cascade depth
A deeper cascade (more models) provides more resilience but increases worst-case latency. Practical deployments should cap the cascade at 3–4 tiers, with local Ollama as the terminal fallback.

---

## The Agentic Reliability Stack

Together, the LLM Circuit Breaker Pattern and the LLM Request Cascade Pattern form an **Agentic Reliability Stack**:

```
┌─────────────────────────────────────────┐
│          AI Coding Agent                │
│     (Claude Code, Cursor, etc.)         │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│           glide                     │
│   LLM Request Cascade Pattern           │
│   (latency-aware model routing)         │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│           llm-circuit                   │
│   LLM Circuit Breaker Pattern           │
│   (outage detection & failover)         │
└─────────────────┬───────────────────────┘
                  │
        ┌─────────┴──────────┐
        │                    │
   Anthropic API         Local Ollama
   (primary)             (fallback)
```

This two-layer architecture addresses the full reliability spectrum: from transient slowness (cascade) to full outages (circuit breaker), with local models as the terminal fallback in both cases.
