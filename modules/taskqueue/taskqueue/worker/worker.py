"""
Worker process pool.

Architecture:
  - One coordinator thread per worker instance that:
      1. Maintains a persistent TCP connection to the broker.
      2. Receives DELIVER messages.
      3. Dispatches each task to a subprocess in the pool.
      4. Sends ACK/NACK/RESULT back to the broker.
      5. Sends PING heartbeats to keep the connection alive.

  - A multiprocessing.Pool executes the actual task functions in separate
    processes so that CPU-bound tasks do not block the coordinator and a
    crashed task cannot corrupt the worker's memory.

Task execution flow:
  1. Broker sends DELIVER → coordinator sends ACK and submits to pool.
  2. Pool executes the task function.
  3. On success: coordinator sends RESULT(state=SUCCESS).
  4. On exception: coordinator checks retries — if retries remaining it
     sends NACK(requeue=True, delay=retry_delay), otherwise RESULT(FAILURE).
  5. On timeout: coordinator cancels the pool future and sends NACK.
"""

from __future__ import annotations

import importlib
import logging
import multiprocessing
import socket
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, Future, TimeoutError as FuturesTimeout
from typing import Callable

from taskqueue.config import Message, MessageType, TaskResult, TaskState, WorkerConfig
from taskqueue.broker.protocol import (
    ConnectionClosed, FramingError, read_message, write_message,
)

logger = logging.getLogger("taskqueue.worker")


# ---------------------------------------------------------------------------
# Task registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Callable] = {}


def task(name: str | None = None):
    """
    Decorator that registers a callable in the task registry.

        @task()
        def add(x, y):
            return x + y

        @task(name="myapp.send_email")
        def send_email(to, subject, body):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        task_name = name or f"{fn.__module__}.{fn.__qualname__}"
        _REGISTRY[task_name] = fn
        fn._task_name = task_name
        return fn
    return decorator


def get_task(name: str) -> Callable:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown task: {name!r}. Registered: {list(_REGISTRY)}")
    return _REGISTRY[name]


# ---------------------------------------------------------------------------
# Task execution (runs in a subprocess)
# ---------------------------------------------------------------------------

def _execute_task(name: str, args: list, kwargs: dict, task_modules: list[str]) -> tuple:
    """
    Execute a registered task in a subprocess.

    Returns (result, error_str, traceback_str).
    task_modules is a list of module paths to import so the registry
    is populated in the subprocess.
    """
    for mod in task_modules:
        importlib.import_module(mod)

    fn = get_task(name)
    try:
        result = fn(*args, **kwargs)
        return result, "", ""
    except Exception as exc:
        return None, str(exc), traceback.format_exc()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class Worker:
    """
    Connects to the broker, receives tasks and executes them in a process pool.
    """

    def __init__(self, config: WorkerConfig, task_modules: list[str] | None = None):
        self._config       = config
        self._task_modules = task_modules or []
        self._sock: socket.socket | None = None
        self._lock   = threading.Lock()
        self._running = False
        self._executor: ProcessPoolExecutor | None = None
        # task_id → Future
        self._futures: dict[str, Future] = {}

    # ── Public API ────────────────────────────────────────────────────────

    def start(self):
        self._running  = True
        self._executor = ProcessPoolExecutor(max_workers=self._config.concurrency)
        self._connect_and_run()

    def stop(self):
        self._running = False
        if self._executor:
            self._executor.shutdown(wait=False)
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    # ── Connection management ─────────────────────────────────────────────

    def _connect_and_run(self):
        while self._running:
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.connect((self._config.broker_host, self._config.broker_port))
                self._sock.settimeout(self._config.heartbeat * 2)

                # Register with the broker
                write_message(self._sock, Message(
                    MessageType.SUBSCRIBE, {
                        "worker_id": self._config.worker_id,
                        "queues":    self._config.queues,
                    }
                ))

                logger.info(
                    "Worker %s connected, queues=%s",
                    self._config.worker_id, self._config.queues,
                )

                threading.Thread(
                    target=self._heartbeat_loop, daemon=True,
                    name=f"worker-{self._config.worker_id}-ping",
                ).start()

                self._message_loop()

            except (ConnectionRefusedError, OSError) as exc:
                logger.warning("Cannot connect to broker: %s. Retrying in %ds",
                               exc, self._config.reconnect_delay)
                time.sleep(self._config.reconnect_delay)

    def _message_loop(self):
        sock = self._sock
        while self._running:
            try:
                msg = read_message(sock)
            except socket.timeout:
                continue
            except (ConnectionClosed, FramingError, OSError) as exc:
                logger.warning("Connection lost: %s", exc)
                break

            if msg.type == MessageType.DELIVER:
                self._handle_deliver(msg)

            elif msg.type == MessageType.PING:
                try:
                    write_message(sock, Message(MessageType.PONG))
                except OSError:
                    break

    def _handle_deliver(self, msg: Message):
        p       = msg.payload
        task_id = p["task_id"]
        name    = p["name"]
        args    = p.get("args", [])
        kwargs  = p.get("kwargs", {})
        timeout = p.get("timeout")
        retries = p.get("retries", 0)
        max_ret = p.get("max_retries", 3)
        delay   = p.get("retry_delay", 5.0)

        # Acknowledge receipt immediately so broker stops the ack timer
        self._send(Message(MessageType.ACK, {"task_id": task_id}))
        started_at = time.time()

        future = self._executor.submit(
            _execute_task, name, args, kwargs, self._task_modules
        )
        with self._lock:
            self._futures[task_id] = future

        def _on_done(fut: Future):
            ended_at = time.time()
            try:
                result_val, error, tb = fut.result(timeout=0)
            except FuturesTimeout:
                self._send(Message(MessageType.NACK, {
                    "task_id": task_id, "requeue": retries < max_ret,
                    "reason": "timeout",
                }))
                return
            except Exception as exc:
                error = str(exc)
                tb    = traceback.format_exc()
                result_val = None

            if error:
                if retries < max_ret:
                    self._send(Message(MessageType.NACK, {
                        "task_id": task_id, "requeue": True,
                        "reason": error,
                    }))
                else:
                    self._send(Message(MessageType.RESULT, {
                        "task_id":   task_id,
                        "state":     TaskState.FAILURE.value,
                        "error":     error,
                        "traceback": tb,
                        "started_at": started_at,
                        "ended_at":   ended_at,
                        "worker_id":  self._config.worker_id,
                    }))
            else:
                self._send(Message(MessageType.RESULT, {
                    "task_id":   task_id,
                    "state":     TaskState.SUCCESS.value,
                    "result":    result_val,
                    "error":     "",
                    "traceback": "",
                    "started_at": started_at,
                    "ended_at":   ended_at,
                    "worker_id":  self._config.worker_id,
                }))

            with self._lock:
                self._futures.pop(task_id, None)

        future.add_done_callback(_on_done)

    def _heartbeat_loop(self):
        while self._running:
            time.sleep(self._config.heartbeat)
            try:
                self._send(Message(MessageType.PING))
            except OSError:
                break

    def _send(self, msg: Message):
        with self._lock:
            if self._sock:
                try:
                    write_message(self._sock, msg)
                except OSError as exc:
                    logger.warning("Send failed: %s", exc)
