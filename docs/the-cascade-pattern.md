# The LLM Request Cascade Pattern

*A latency-aware routing primitive for agentic AI development workflows.*

---

## Background

The circuit breaker pattern (see [llm-circuit](https://github.com/phanisaimunipalli/llm-circuit)) addresses binary failure: a provider is either up or down. But in practice, the most common degradation mode for LLM APIs is not failure — it is **slowness**.

During peak load, a frontier model like Claude Opus may take 12–15 seconds to return its first token. For a developer mid-task in an AI coding agent, this is indistinguishable from a timeout. The agent feels broken. Work stops.

This document defines the **LLM Request Cascade Pattern**: a latency-aware routing strategy that automatically moves requests to faster models when the preferred model is too slow — before the user experiences a timeout.

---

## The Core Insight: Time-to-First-Token as a Health Signal

Existing LLM proxy tools measure health in binary terms (up/down) or after full response completion (total latency). Neither is useful for streaming agentic workflows.

The correct metric is **time-to-first-token (TTFT)**: the elapsed time from request dispatch to receipt of the first response byte. For streaming LLMs:

- TTFT is what the user *feels* — it's when the agent appears to "start thinking"
- TTFT spikes under server load before connection errors appear
- TTFT can be measured mid-stream and acted on *before the full response completes*

This creates an opportunity: if TTFT exceeds a threshold, **abort the request early and retry on the next model**, rather than waiting for a full timeout.

---

## The Pattern

### Cascade Configuration

An ordered list of models with per-model TTFT budgets:

```
1. claude-opus-4-6    TTFT budget: 8s   (best quality, try first)
2. claude-sonnet-4-6  TTFT budget: 5s   (faster, good quality)
3. claude-haiku-4-5   TTFT budget: 3s   (fastest Anthropic model)
4. qwen2.5:14b        TTFT budget: none  (local Ollama, always available)
```

### Request Flow

```
Request arrives
      │
      ▼
Try Model 1 (opus)
      │
  ┌───┴──────────────────────────────┐
  │ Start TTFT timer                 │
  │ Wait for first token...          │
  │                                  │
  │  First token < 8s?               │
  │     YES → stream full response ✓ │
  │     NO  → abort, record TTFT     │
  └───┬──────────────────────────────┘
      │ (timeout)
      ▼
Try Model 2 (sonnet)
      │
      ... (same logic, 5s budget)
      │
      ▼
Try Model 3 (haiku)
      │
      ... (same logic, 3s budget)
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

### Quality degradation signaling
When the cascade falls back to a lower-tier model, the client should know. llm-relay includes the `X-LLM-Relay-Model` response header indicating which model actually served the request.

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
│           llm-relay                     │
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
