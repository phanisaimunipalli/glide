"""
Configuration for glide.

The cascade is an ordered list of models. glide tries them in order,
moving to the next if the current exceeds its TTFT or TTT budget.

Two latency budgets per model:
  ttft_budget — time-to-first-token: abort if no bytes arrive within N seconds.
                Catches cold starts, connection issues, overloaded endpoints.
  ttt_budget  — time-to-think: abort if thinking/reasoning exceeds N seconds
                before the first real text token appears.
                Only fires for models with extended thinking (opus, o1, etc.).
                For regular models, this signal is never triggered.

Supported providers:
  anthropic  — Anthropic Messages API (claude-*)
  openai     — OpenAI Chat Completions API (gpt-*, o1-*, etc.)
  google     — Google Gemini API (gemini-*)
  ollama     — Local Ollama instance (any model)

Default cascade:
  claude-opus-4-6   (ttft=4s, ttt=15s) → best quality, try first
  claude-sonnet-4-6 (ttft=5s, ttt=20s) → faster, good quality
  claude-haiku-4-5  (ttft=3s, ttt=None) → fastest Anthropic, no thinking
  qwen2.5:14b       (no timeout)        → local Ollama, always available

Override via CASCADE_JSON env var to add OpenAI/Gemini/custom models.
Example:
  CASCADE_JSON='[
    {"provider":"anthropic","model":"claude-opus-4-6","ttft_budget":4.0,"ttt_budget":15.0},
    {"provider":"openai","model":"gpt-4o","ttft_budget":5.0,"ttt_budget":null},
    {"provider":"google","model":"gemini-2.0-flash","ttft_budget":3.0,"ttt_budget":null},
    {"provider":"ollama","model":"qwen2.5:14b","ttft_budget":null,"ttt_budget":null}
  ]'
"""

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

from pydantic_settings import BaseSettings


@dataclass
class ModelConfig:
    provider: str                        # "anthropic", "openai", "google", or "ollama"
    model: str                           # model identifier
    ttft_budget: Optional[float] = None  # seconds to first byte; None = no timeout
    ttt_budget: Optional[float] = None   # seconds until first text token (post-thinking); None = no timeout


DEFAULT_CASCADE = [
    ModelConfig(provider="anthropic", model="claude-opus-4-6",   ttft_budget=4.0,  ttt_budget=10.0),
    ModelConfig(provider="anthropic", model="claude-sonnet-4-6", ttft_budget=5.0,  ttt_budget=10.0),
    ModelConfig(provider="anthropic", model="claude-haiku-4-5",  ttft_budget=3.0,  ttt_budget=None),
    ModelConfig(provider="ollama",    model="qwen2.5:14b",        ttft_budget=None, ttt_budget=None),
]


class Settings(BaseSettings):
    # Anthropic
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"

    # OpenAI
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com"

    # Google Gemini
    google_api_key: str = ""

    # Ollama
    ollama_url: str = "http://localhost:11434"

    # Cascade (override via CASCADE_JSON env var for custom chains)
    cascade_json: str = ""

    # Proxy
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8743
    log_level: str = "INFO"

    # Latency tracker
    tracker_window: int = 20   # rolling window size for p95 calculation
    proactive_skip: bool = True  # skip models whose p95 TTFT > budget

    # Persistence
    db_path: str = "~/.glide/latency.db"  # SQLite path; set to "" to disable

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def get_cascade(self) -> List[ModelConfig]:
        if self.cascade_json:
            raw = json.loads(self.cascade_json)
            return [ModelConfig(**m) for m in raw]
        return DEFAULT_CASCADE


settings = Settings()
