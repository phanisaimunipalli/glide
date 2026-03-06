# Contributing to llm-relay

```bash
git clone https://github.com/phanisaimunipalli/llm-relay
cd llm-relay
pip install -e ".[dev]"
pytest tests/ -v
```

## Areas most needed
- Request hedging (send to 2 models simultaneously, use first response)
- OpenAI / Gemini provider support
- Persistent latency stats (SQLite) across restarts
- Grafana-compatible metrics endpoint
- Integration tests with real Ollama
