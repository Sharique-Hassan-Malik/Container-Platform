"""
Client API for publishing tasks to the broker and retrieving results.

The client maintains a persistent TCP connection to the broker.
Every publish() call sends a PUBLISH message and receives a CONFIRM.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid

from taskqueue.config import BrokerConfig, Message, MessageType, Task, TaskState, TaskResult
from taskqueue.broker.protocol import (
    ConnectionClosed, FramingError, read_message, write_message,
)


class AsyncResult:
    """
    Handle to a submitted task.  Blocking .get() waits for the result.
    """

    def __init__(self, task_id: str, client: "TaskClient"):
        self.task_id = task_id
        self._client  = client
        self._result: TaskResult | None = None
        self._event   = threading.Event()

    def get(self, timeout: float = 30.0) -> TaskResult | None:
        """Block until the result arrives or timeout expires."""
        self._event.wait(timeout=timeout)
        return self._result

    def _set_result(self, result: TaskResult):
        self._result = result
        self._event.set()

    def __repr__(self) -> str:
        state = self._result.state.value if self._result else "PENDING"
        return f"AsyncResult(task_id={self.task_id!r}, state={state})"


class TaskClient:
    """
    Persistent connection to the broker for publishing tasks.

    Thread-safe: multiple threads can call publish() concurrently.
    """

    def __init__(self, config: BrokerConfig | None = None):
        self._config = config or BrokerConfig()
        self._sock: socket.socket | None = None
        self._lock   = threading.Lock()
        self._pending: dict[str, AsyncResult] = {}
        self._connected = False
        self._connect()

    # ── Public API ────────────────────────────────────────────────────────

    def publish(
        self,
        name: str,
        args: list | None = None,
        kwargs: dict | None = None,
        queue: str = "default",
        priority: int = 5,
        max_retries: int = 3,
        retry_delay: float = 5.0,
        timeout: float | None = None,
        eta: float | None = None,
    ) -> AsyncResult:
        """
        Publish a task and return an AsyncResult handle.

        The task will be executed by the next available worker subscribed
        to the specified queue.
        """
        task_id = str(uuid.uuid4())
        payload = {
            "task_id":     task_id,
            "name":        name,
            "args":        args or [],
            "kwargs":      kwargs or {},
            "queue":       queue,
            "priority":    priority,
            "max_retries": max_retries,
            "retry_delay": retry_delay,
            "timeout":     timeout,
            "eta":         eta,
        }
        ar = AsyncResult(task_id, self)
        with self._lock:
            self._pending[task_id] = ar
            write_message(self._sock, Message(MessageType.PUBLISH, payload))
        return ar

    def revoke(self, task_id: str):
        """Request cancellation of a queued or running task."""
        with self._lock:
            write_message(self._sock, Message(MessageType.REVOKE, {"task_id": task_id}))

    def stats(self) -> dict:
        """Request queue statistics from the broker."""
        with self._lock:
            write_message(self._sock, Message(MessageType.STATS))
            response = read_message(self._sock)
        return response.payload

    def close(self):
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    # ── Internal ──────────────────────────────────────────────────────────

    def _connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((self._config.host, self._config.port))
        self._sock.settimeout(30.0)
        self._connected = True
        threading.Thread(target=self._read_loop, daemon=True, name="client-reader").start()

    def _read_loop(self):
        """Background thread reads broker responses and resolves AsyncResults."""
        while self._connected:
            try:
                msg = read_message(self._sock)
            except (ConnectionClosed, FramingError, OSError):
                break

            if msg.type == MessageType.RESULT:
                task_id = msg.payload.get("task_id")
                with self._lock:
                    ar = self._pending.pop(task_id, None)
                if ar:
                    result = TaskResult(
                        task_id=task_id,
                        state=TaskState(msg.payload.get("state", "FAILURE")),
                        result=msg.payload.get("result"),
                        error=msg.payload.get("error", ""),
                        traceback=msg.payload.get("traceback", ""),
                        started_at=msg.payload.get("started_at", 0.0),
                        ended_at=msg.payload.get("ended_at", 0.0),
                        worker_id=msg.payload.get("worker_id", ""),
                    )
                    ar._set_result(result)

            elif msg.type == MessageType.PING:
                try:
                    write_message(self._sock, Message(MessageType.PONG))
                except OSError:
                    break
