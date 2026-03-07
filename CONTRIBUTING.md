# Contributing to glide

```bash
git clone https://github.com/phanisaimunipalli/glide
cd glide
pip install -e ".[dev]"
pytest tests/ -v   # 22 tests
```

## Areas most needed

- **Integration tests** — live tests with real Ollama, OpenAI, and Gemini endpoints
- **TTT for OpenAI o1/o3** — detect reasoning tokens in OpenAI SSE format (different event shape than Anthropic)
- **Context preservation** — carry conversation state across cascade hops (e.g. inject prior tokens as context on retry)
- **Grafana dashboard** — pre-built dashboard JSON for the `/metrics` Prometheus endpoint
- **Hedge for TTT** — extend the hedge trigger to also consider TTT p95, not just TTFT
