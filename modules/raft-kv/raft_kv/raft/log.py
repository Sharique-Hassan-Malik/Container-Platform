"""
Raft replicated log.

Each entry stores (term, index, command). Indices are 1-based; index 0 is a
sentinel that represents "nothing committed yet".

The log is kept in memory. Persistence is provided by LogStore which writes
each entry to a JSON-lines file on append and rewrites the file on snapshot.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from threading import Lock
from typing import Optional


@dataclass
class LogEntry:
    term:    int
    index:   int
    command: str   # "SET key value" | "DEL key" | "NOP"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LogEntry":
        return cls(**d)


class RaftLog:
    """
    In-memory replicated log with optional file-backed durability.

    All public methods are thread-safe.
    """

    def __init__(self, data_dir: Optional[str] = None, node_id: str = "") -> None:
        self._lock = Lock()
        self._entries: list[LogEntry] = []
        self._snapshot_last_index = 0
        self._snapshot_last_term  = 0
        self._path = Path(data_dir) / f"{node_id}.log" if data_dir else None
        if self._path and self._path.exists():
            self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def append(self, entry: LogEntry) -> None:
        with self._lock:
            self._entries.append(entry)
            self._persist_entry(entry)

    def append_all(self, entries: list[LogEntry]) -> None:
        with self._lock:
            self._entries.extend(entries)
            for e in entries:
                self._persist_entry(e)

    def get(self, index: int) -> Optional[LogEntry]:
        """Return entry at *index* (1-based). Returns None if out of range."""
        with self._lock:
            return self._get(index)

    def last_index(self) -> int:
        with self._lock:
            if not self._entries:
                return self._snapshot_last_index
            return self._entries[-1].index

    def last_term(self) -> int:
        with self._lock:
            if not self._entries:
                return self._snapshot_last_term
            return self._entries[-1].term

    def term_at(self, index: int) -> int:
        """Return the term of the entry at *index*, or 0 if before log start."""
        with self._lock:
            if index == 0:
                return 0
            if index <= self._snapshot_last_index:
                return self._snapshot_last_term
            entry = self._get(index)
            return entry.term if entry else 0

    def entries_from(self, start: int) -> list[LogEntry]:
        """Return all entries with index >= *start*."""
        with self._lock:
            offset = self._snapshot_last_index
            idx = start - offset - 1
            if idx < 0:
                idx = 0
            return list(self._entries[idx:])

    def truncate_from(self, index: int) -> None:
        """Remove all entries with index >= *index* (conflict resolution)."""
        with self._lock:
            offset = self._snapshot_last_index
            keep = index - offset - 1
            if keep < 0:
                keep = 0
            self._entries = self._entries[:keep]
            self._rewrite()

    def snapshot(self, last_index: int, last_term: int) -> None:
        """Discard all entries up to and including *last_index*."""
        with self._lock:
            offset = self._snapshot_last_index
            cut = last_index - offset
            self._entries = self._entries[cut:]
            self._snapshot_last_index = last_index
            self._snapshot_last_term  = last_term
            self._rewrite()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # ── Private ───────────────────────────────────────────────────────────────

    def _get(self, index: int) -> Optional[LogEntry]:
        offset = self._snapshot_last_index
        pos = index - offset - 1
        if pos < 0 or pos >= len(self._entries):
            return None
        return self._entries[pos]

    def _persist_entry(self, entry: LogEntry) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a") as fh:
            fh.write(json.dumps(entry.to_dict()) + "\n")

    def _rewrite(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as fh:
            meta = {"snapshot_last_index": self._snapshot_last_index,
                    "snapshot_last_term":  self._snapshot_last_term}
            fh.write(json.dumps(meta) + "\n")
            for e in self._entries:
                fh.write(json.dumps(e.to_dict()) + "\n")

    def _load(self) -> None:
        with open(self._path) as fh:
            lines = [l.strip() for l in fh if l.strip()]
        if not lines:
            return
        first = json.loads(lines[0])
        if "snapshot_last_index" in first:
            self._snapshot_last_index = first["snapshot_last_index"]
            self._snapshot_last_term  = first["snapshot_last_term"]
            entries_raw = lines[1:]
        else:
            entries_raw = lines
        self._entries = [LogEntry.from_dict(json.loads(l)) for l in entries_raw]
