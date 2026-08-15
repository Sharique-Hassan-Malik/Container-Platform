"""
Durable state that must survive crashes.

Raft requires that current_term and voted_for are written to stable storage
before responding to any RPC, so they are flushed synchronously on every write.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Optional


class PersistentState:
    def __init__(self, data_dir: Optional[str] = None, node_id: str = "") -> None:
        self._lock       = Lock()
        self._term       = 0
        self._voted_for: Optional[str] = None
        self._path = Path(data_dir) / f"{node_id}.state" if data_dir else None
        if self._path and self._path.exists():
            self._load()

    # ── Term ──────────────────────────────────────────────────────────────────

    @property
    def current_term(self) -> int:
        with self._lock:
            return self._term

    def set_term(self, term: int) -> None:
        with self._lock:
            self._term = term
            self._voted_for = None
            self._save()

    def increment_term(self) -> int:
        with self._lock:
            self._term += 1
            self._voted_for = None
            self._save()
            return self._term

    # ── Voted-for ─────────────────────────────────────────────────────────────

    @property
    def voted_for(self) -> Optional[str]:
        with self._lock:
            return self._voted_for

    def set_voted_for(self, candidate: Optional[str]) -> None:
        with self._lock:
            self._voted_for = candidate
            self._save()

    # ── Private ───────────────────────────────────────────────────────────────

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        data = {"term": self._term, "voted_for": self._voted_for}
        with open(tmp, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(self._path)

    def _load(self) -> None:
        with open(self._path) as fh:
            data = json.load(fh)
        self._term      = data.get("term", 0)
        self._voted_for = data.get("voted_for")
