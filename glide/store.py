"""
SQLite-backed persistence for per-model latency samples.

Stores TTFT and TTT samples so the p95 rolling window survives glide restarts.
Without this, proactive routing requires a warm-up period after every restart.

Schema:
  latency_samples(model TEXT, signal TEXT, value REAL, recorded_at INTEGER)
  signal is 'ttft' or 'ttt'.

Only the most recent `window_size` rows per (model, signal) are kept.
Older rows are pruned on each insert.
"""

import logging
import os
import sqlite3
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("glide.store")

_CREATE = """
CREATE TABLE IF NOT EXISTS latency_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model       TEXT    NOT NULL,
    signal      TEXT    NOT NULL,
    value       REAL    NOT NULL,
    recorded_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_model_signal
    ON latency_samples (model, signal, recorded_at DESC);
"""


class LatencyStore:
    def __init__(self, db_path: str):
        path = Path(db_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        self._conn: Optional[sqlite3.Connection] = None
        self._init()

    def _init(self):
        try:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.executescript(_CREATE)
            self._conn.commit()
            logger.debug(f"[store] opened {self._path}")
        except Exception as e:
            logger.warning(f"[store] could not open {self._path}: {e} — running without persistence")
            self._conn = None

    def load(self, model: str, signal: str, limit: int) -> List[float]:
        """Return the most recent `limit` values for (model, signal), oldest first."""
        if not self._conn:
            return []
        try:
            rows = self._conn.execute(
                """
                SELECT value FROM latency_samples
                WHERE model = ? AND signal = ?
                ORDER BY recorded_at DESC, id DESC
                LIMIT ?
                """,
                (model, signal, limit),
            ).fetchall()
            values = [r[0] for r in reversed(rows)]
            if values:
                logger.debug(f"[store] loaded {len(values)} {signal} samples for {model}")
            return values
        except Exception as e:
            logger.warning(f"[store] load failed: {e}")
            return []

    def append(self, model: str, signal: str, value: float, limit: int):
        """Insert a new sample and prune rows beyond `limit` for (model, signal)."""
        if not self._conn:
            return
        try:
            self._conn.execute(
                "INSERT INTO latency_samples (model, signal, value) VALUES (?, ?, ?)",
                (model, signal, value),
            )
            # Keep only the most recent `limit` rows
            self._conn.execute(
                """
                DELETE FROM latency_samples
                WHERE model = ? AND signal = ?
                  AND id NOT IN (
                      SELECT id FROM latency_samples
                      WHERE model = ? AND signal = ?
                      ORDER BY recorded_at DESC, id DESC
                      LIMIT ?
                  )
                """,
                (model, signal, model, signal, limit),
            )
            self._conn.commit()
        except Exception as e:
            logger.warning(f"[store] append failed: {e}")

    def total_samples(self) -> int:
        """Total rows in the database (for status display)."""
        if not self._conn:
            return 0
        try:
            return self._conn.execute("SELECT COUNT(*) FROM latency_samples").fetchone()[0]
        except Exception:
            return 0

    @property
    def path(self) -> str:
        return self._path

    @property
    def available(self) -> bool:
        return self._conn is not None


# Singleton — shared across tracker registry
_store: Optional[LatencyStore] = None


def get_store() -> LatencyStore:
    global _store
    if _store is None:
        from .config import settings
        _store = LatencyStore(settings.db_path)
    return _store
