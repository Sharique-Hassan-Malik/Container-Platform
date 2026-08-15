"""
SQLite result backend.

Stores TaskResult records keyed by task_id.  A background thread
expires records older than result_ttl.

The backend is designed to be queried by both workers (to store results)
and clients (to retrieve them), so all methods are thread-safe.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from taskqueue.config import BackendConfig, TaskResult, TaskState


class ResultBackend:

    def __init__(self, config: BackendConfig):
        self._config = config
        self._db     = sqlite3.connect(config.persist_path, check_same_thread=False)
        self._lock   = threading.Lock()
        self._init_db()
        threading.Thread(target=self._expiry_loop, daemon=True, name="backend-expiry").start()

    def store(self, result: TaskResult):
        data = json.dumps({
            "task_id":   result.task_id,
            "state":     result.state.value,
            "result":    result.result,
            "error":     result.error,
            "traceback": result.traceback,
            "started_at": result.started_at,
            "ended_at":   result.ended_at,
            "worker_id":  result.worker_id,
        })
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO results VALUES (?, ?, ?)",
                (result.task_id, data, time.time()),
            )
            self._db.commit()

    def get(self, task_id: str) -> TaskResult | None:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM results WHERE task_id=?", (task_id,)
            ).fetchone()
        if not row:
            return None
        d = json.loads(row[0])
        return TaskResult(
            task_id=d["task_id"],
            state=TaskState(d["state"]),
            result=d["result"],
            error=d.get("error", ""),
            traceback=d.get("traceback", ""),
            started_at=d.get("started_at", 0.0),
            ended_at=d.get("ended_at", 0.0),
            worker_id=d.get("worker_id", ""),
        )

    def wait(self, task_id: str, timeout: float = 30.0, poll: float = 0.1) -> TaskResult | None:
        """Poll until the result is available or timeout expires."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self.get(task_id)
            if result and result.state in (TaskState.SUCCESS, TaskState.FAILURE):
                return result
            time.sleep(poll)
        return None

    def all_recent(self, limit: int = 100) -> list[TaskResult]:
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM results ORDER BY stored_at DESC LIMIT ?", (limit,)
            ).fetchall()
        results = []
        for (data_str,) in rows:
            d = json.loads(data_str)
            results.append(TaskResult(
                task_id=d["task_id"],
                state=TaskState(d["state"]),
                result=d.get("result"),
                error=d.get("error", ""),
                traceback=d.get("traceback", ""),
                started_at=d.get("started_at", 0.0),
                ended_at=d.get("ended_at", 0.0),
                worker_id=d.get("worker_id", ""),
            ))
        return results

    # ── Internal ──────────────────────────────────────────────────────────

    def _init_db(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS results (
                task_id   TEXT PRIMARY KEY,
                data      TEXT NOT NULL,
                stored_at REAL NOT NULL
            )
        """)
        self._db.commit()

    def _expiry_loop(self):
        while True:
            time.sleep(60)
            cutoff = time.time() - self._config.result_ttl
            with self._lock:
                self._db.execute("DELETE FROM results WHERE stored_at < ?", (cutoff,))
                self._db.commit()
