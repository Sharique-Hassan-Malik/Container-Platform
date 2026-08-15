"""
Key-value state machine.

Applied sequentially by the Raft node via apply_fn. All reads and writes are
protected by a single lock so the state machine is linearisable after Raft
delivers commands in commit order.
"""

from __future__ import annotations

import threading
from typing import Optional


class KVStore:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()

    def apply(self, command: str) -> None:
        """Parse and execute a command string: 'SET k v' or 'DEL k'."""
        parts = command.split(" ", 2)
        if not parts:
            return
        op = parts[0].upper()
        if op == "SET" and len(parts) >= 3:
            with self._lock:
                self._data[parts[1]] = parts[2]
        elif op == "DEL" and len(parts) >= 2:
            with self._lock:
                self._data.pop(parts[1], None)
        elif op == "NOP":
            pass

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._data.get(key)

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._data)

    def restore(self, snapshot: dict[str, str]) -> None:
        with self._lock:
            self._data = dict(snapshot)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
