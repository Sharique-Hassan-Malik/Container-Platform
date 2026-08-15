"""
In-memory priority queue with ETA (earliest-time-to-arrive) support and
optional SQLite persistence for crash recovery.

Tasks are stored as a heap keyed by (priority, eta, enqueue_time) so that:
  - Lower priority number = higher urgency (1 runs before 10)
  - Within same priority, tasks with the nearest ETA run first
  - Within same priority and ETA, FIFO ordering is preserved

SQLite persistence writes every enqueue and dequeue to disk so the broker
can recover its queue state after a restart without losing tasks.
"""

from __future__ import annotations

import heapq
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from taskqueue.config import Task, TaskState


@dataclass
class _QueueEntry:
    sort_key: tuple   # (priority, eta, enqueue_time)
    task: Task = None  # type: ignore[assignment]

    def __eq__(self, other):
        return self.sort_key == other.sort_key

    def __lt__(self, other):
        return self.sort_key < other.sort_key

    def __le__(self, other):
        return self.sort_key <= other.sort_key

    def __gt__(self, other):
        return self.sort_key > other.sort_key

    def __ge__(self, other):
        return self.sort_key >= other.sort_key


class TaskQueue:
    """
    Thread-safe priority queue with per-queue isolation.

    Each named queue (e.g. "default", "high") maintains its own heap.
    """

    def __init__(self, persist_path: str | None = None):
        self._lock   = threading.Lock()
        self._heaps: dict[str, list[_QueueEntry]] = {}
        self._counts: dict[str, int] = {}
        self._db: sqlite3.Connection | None = None

        if persist_path:
            self._init_db(persist_path)
            self._load_from_db()

    # ── Public API ────────────────────────────────────────────────────────

    def enqueue(self, task: Task):
        eta = task.eta or task.created_at
        entry = _QueueEntry(
            sort_key=(task.priority, eta, task.created_at),
            task=task,
        )
        with self._lock:
            q = self._heaps.setdefault(task.queue, [])
            heapq.heappush(q, entry)
            self._counts[task.queue] = self._counts.get(task.queue, 0) + 1
        if self._db:
            self._persist_enqueue(task)

    def dequeue(self, queue: str = "default") -> Task | None:
        """
        Pop the highest-priority task whose ETA has passed.
        Returns None if the queue is empty or all tasks are in the future.
        """
        now = time.time()
        with self._lock:
            heap = self._heaps.get(queue)
            if not heap:
                return None
            # Peek without popping to check ETA
            top = heap[0]
            if top.sort_key[1] > now:   # ETA in the future
                return None
            entry = heapq.heappop(heap)
            self._counts[queue] = max(0, self._counts.get(queue, 1) - 1)
        if self._db:
            self._persist_dequeue(entry.task.task_id)
        return entry.task

    def requeue(self, task: Task, delay: float = 0.0):
        """Re-enqueue a task with an optional ETA delay (for retries)."""
        task.eta = time.time() + delay
        task.state = TaskState.RETRY
        self.enqueue(task)

    def size(self, queue: str = "default") -> int:
        with self._lock:
            return self._counts.get(queue, 0)

    def queue_names(self) -> list[str]:
        with self._lock:
            return list(self._heaps.keys())

    def all_sizes(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    # ── Persistence ───────────────────────────────────────────────────────

    def _init_db(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS queued_tasks (
                task_id    TEXT PRIMARY KEY,
                queue      TEXT NOT NULL,
                priority   INTEGER NOT NULL,
                eta        REAL NOT NULL,
                created_at REAL NOT NULL,
                data       TEXT NOT NULL
            )
        """)
        self._db.commit()

    def _persist_enqueue(self, task: Task):
        data = json.dumps({
            "task_id": task.task_id, "name": task.name,
            "args": task.args, "kwargs": task.kwargs,
            "queue": task.queue, "priority": task.priority,
            "retries": task.retries, "max_retries": task.max_retries,
            "retry_delay": task.retry_delay, "timeout": task.timeout,
            "eta": task.eta, "created_at": task.created_at,
        })
        self._db.execute(
            "INSERT OR REPLACE INTO queued_tasks VALUES (?,?,?,?,?,?)",
            (task.task_id, task.queue, task.priority,
             task.eta or task.created_at, task.created_at, data)
        )
        self._db.commit()

    def _persist_dequeue(self, task_id: str):
        self._db.execute("DELETE FROM queued_tasks WHERE task_id=?", (task_id,))
        self._db.commit()

    def _load_from_db(self):
        rows = self._db.execute(
            "SELECT data FROM queued_tasks ORDER BY priority, eta, created_at"
        ).fetchall()
        for (data_str,) in rows:
            d = json.loads(data_str)
            task = Task(
                task_id=d["task_id"], name=d["name"],
                args=d["args"], kwargs=d["kwargs"],
                queue=d["queue"], priority=d["priority"],
                retries=d["retries"], max_retries=d["max_retries"],
                retry_delay=d["retry_delay"], timeout=d["timeout"],
                eta=d["eta"], created_at=d["created_at"],
                state=TaskState.QUEUED,
            )
            self.enqueue(task)
