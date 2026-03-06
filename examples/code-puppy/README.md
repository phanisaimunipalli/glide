# Using glide with code_puppy

code_puppy uses a `custom_anthropic` model type to support custom base URLs.
Standard `anthropic` model entries in code_puppy ignore `ANTHROPIC_BASE_URL`,
so you must use the `custom_anthropic` type with an explicit `base_url`.

## Setup

```bash
pip install glide
glide start
# Proxy on http://127.0.0.1:8743
```

## code_puppy model config

Add this entry to your code_puppy model config (typically `~/.config/code_puppy/models.json`
or wherever your config lives):

```json
{
  "model_type": "custom_anthropic",
  "model_name": "claude-opus-4-6",
  "base_url": "http://127.0.0.1:8743",
  "api_key": "YOUR_API_KEY_OR_LEAVE_EMPTY_FOR_MAX_PLAN"
}
```

For Max plan / OAuth users, omit `api_key` entirely or set it to an empty string —
glide passes your session auth through automatically.

## How it works

```
code_puppy → glide (http://127.0.0.1:8743) → claude-opus-4-6
                  │ (if opus TTFT > 4s)
                  └──────────────────────── → claude-sonnet-4-6
                  │ (if sonnet TTFT > 5s)
                  └──────────────────────── → claude-haiku-4-5
                  │ (if haiku TTFT > 3s)
                  └──────────────────────── → qwen2.5:14b (Ollama)
```

## Multi-provider cascade for code_puppy

If you have OpenAI or Gemini keys, you can add them as fallback tiers:

```bash
export OPENAI_API_KEY=sk-...
export GOOGLE_API_KEY=AIza...

export CASCADE_JSON='[
  {"provider": "anthropic", "model": "claude-opus-4-6",    "ttft_budget": 4.0},
  {"provider": "openai",    "model": "gpt-4o",             "ttft_budget": 5.0},
  {"provider": "google",    "model": "gemini-2.0-flash",   "ttft_budget": 3.0},
  {"provider": "ollama",    "model": "qwen2.5:14b",         "ttft_budget": null}
]'
glide start
```

code_puppy's model config stays the same — glide handles all the routing.
