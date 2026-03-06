# Contributing to glide

```bash
git clone https://github.com/phanisaimunipalli/glide
cd glide
pip install -e ".[dev]"
pytest tests/ -v
```

## Areas most needed
- Request hedging (send to 2 models simultaneously, use first response)
- Persistent latency stats (SQLite) across restarts
- Grafana / Prometheus metrics endpoint
- Integration tests with real Ollama, OpenAI, and Gemini endpoints
- TTT detection for OpenAI o1/o3 reasoning tokens (different SSE format)
- Context preservation across cascade hops (carry conversation state)
