"""
Configuration for llm-relay.

The cascade is an ordered list of models. llm-relay tries them in order,
moving to the next if the current exceeds its TTFT budget.

Default cascade:
  claude-opus-4-6   (8s budget)  → best quality, try first
  claude-sonnet-4-6 (5s budget)  → faster, good quality
  claude-haiku-4-5  (3s budget)  → fastest Anthropic model
  qwen2.5:14b       (no timeout) → local Ollama, always available
"""

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

from pydantic_settings import BaseSettings


@dataclass
class ModelConfig:
    provider: str          # "anthropic" or "ollama"
    model: str             # model identifier
    ttft_budget: Optional[float] = None  # seconds; None = no timeout


DEFAULT_CASCADE = [
    ModelConfig(provider="anthropic", model="claude-opus-4-6",   ttft_budget=8.0),
    ModelConfig(provider="anthropic", model="claude-sonnet-4-6", ttft_budget=5.0),
    ModelConfig(provider="anthropic", model="claude-haiku-4-5",  ttft_budget=3.0),
    ModelConfig(provider="ollama",    model="qwen2.5:14b",        ttft_budget=None),
]


class Settings(BaseSettings):
    # Anthropic
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"

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

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def get_cascade(self) -> List[ModelConfig]:
        if self.cascade_json:
            raw = json.loads(self.cascade_json)
            return [ModelConfig(**m) for m in raw]
        return DEFAULT_CASCADE


settings = Settings()
