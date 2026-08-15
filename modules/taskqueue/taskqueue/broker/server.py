"""
TCP broker server.

The broker is the central hub of the task queue system.  It:

  1. Accepts TCP connections from both workers and clients.
  2. Maintains named queues and dispatches tasks to subscribed workers.
  3. Tracks unacknowledged deliveries and requeues them on timeout.
  4. Exposes a STATS message for the dashboard.

Each connected peer runs in its own thread.  The broker is single-process
but the connection handlers are fully concurrent through threading.

Connection lifecycle:
  Worker:  SUBSCRIBE → receives DELIVER messages → sends ACK/NACK/RESULT
  Client:  PUBLISH → receives CONFIRM → optionally waits for RESULT
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

from taskqueue.config import BrokerConfig, Message, MessageType, Task, TaskState
from taskqueue.broker.protocol import (
    ConnectionClosed, FramingError, read_message, write_message,
)
from taskqueue.broker.queue import TaskQueue

logger = logging.getLogger("taskqueue.broker")


@dataclass
class WorkerConnection:
    sock:      socket.socket
    addr:      tuple
    worker_id: str
    queues:    list[str]
    busy:      bool  = False
    last_ping: float = field(default_factory=time.time)


@dataclass
class PendingDelivery:
    task:       Task
    worker_id:  str
    delivered_at: float


class BrokerServer:
    """
    Multi-threaded TCP broker server.

    Thread model:
        - One acceptor thread (main loop)
        - One thread per connection
        - One reaper thread (unacknowledged task timeout)
        - One dispatcher thread (routes tasks to idle workers)
    """

    def __init__(self, config: BrokerConfig):
        self.config   = config
        self._queue   = TaskQueue(persist_path=config.persist_path)
        self._lock    = threading.Lock()

        # Registered workers: worker_id → WorkerConnection
        self._workers: dict[str, WorkerConnection] = {}

        # Unacknowledged deliveries: task_id → PendingDelivery
        self._pending: dict[str, PendingDelivery] = {}

        # Listeners waiting for a specific result: task_id → list[socket]
        self._result_listeners: dict[str, list[socket.socket]] = defaultdict(list)

        self._running = False
        self._server_sock: socket.socket | None = None

    # ── Start / stop ──────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self.config.host, self.config.port))
        self._server_sock.listen(self.config.max_connections)
        self._server_sock.settimeout(1.0)

        threading.Thread(target=self._reaper_loop, daemon=True, name="broker-reaper").start()
        threading.Thread(target=self._dispatch_loop, daemon=True, name="broker-dispatch").start()

        logger.info("Broker listening on %s:%d", self.config.host, self.config.port)

        try:
            while self._running:
                try:
                    conn, addr = self._server_sock.accept()
                    threading.Thread(
                        target=self._handle_connection,
                        args=(conn, addr),
                        daemon=True,
                        name=f"broker-conn-{addr[1]}",
                    ).start()
                except socket.timeout:
                    pass
        finally:
            self._server_sock.close()

    def stop(self):
        self._running = False
        if self._server_sock:
            self._server_sock.close()

    # ── Connection handler ────────────────────────────────────────────────

    def _handle_connection(self, sock: socket.socket, addr: tuple):
        sock.settimeout(self.config.ack_timeout)
        worker_conn: WorkerConnection | None = None

        try:
            while self._running:
                try:
                    msg = read_message(sock)
                except socket.timeout:
                    # Send PING to check liveness
                    try:
                        write_message(sock, Message(MessageType.PING))
                    except OSError:
                        break
                    continue
                except (ConnectionClosed, FramingError) as exc:
                    logger.debug("Connection %s closed: %s", addr, exc)
                    break

                if msg.type == MessageType.SUBSCRIBE:
                    worker_conn = self._handle_subscribe(sock, addr, msg)

                elif msg.type == MessageType.PUBLISH:
                    self._handle_publish(sock, msg)

                elif msg.type == MessageType.ACK:
                    self._handle_ack(msg)

                elif msg.type == MessageType.NACK:
                    self._handle_nack(msg)

                elif msg.type == MessageType.RESULT:
                    self._handle_result(sock, msg)
                    if worker_conn:
                        with self._lock:
                            worker_conn.busy = False

                elif msg.type == MessageType.REVOKE:
                    self._handle_revoke(msg)

                elif msg.type == MessageType.STATS:
                    self._handle_stats(sock)

                elif msg.type == MessageType.PING:
                    write_message(sock, Message(MessageType.PONG))

                elif msg.type == MessageType.PONG:
                    if worker_conn:
                        with self._lock:
                            worker_conn.last_ping = time.time()

        except OSError:
            pass
        finally:
            if worker_conn:
                with self._lock:
                    self._workers.pop(worker_conn.worker_id, None)
            try:
                sock.close()
            except OSError:
                pass

    # ── Message handlers ──────────────────────────────────────────────────

    def _handle_subscribe(
        self, sock: socket.socket, addr: tuple, msg: Message
    ) -> WorkerConnection:
        worker_id = msg.payload.get("worker_id", f"worker-{addr[1]}")
        queues    = msg.payload.get("queues", ["default"])
        wc = WorkerConnection(sock=sock, addr=addr, worker_id=worker_id, queues=queues)
        with self._lock:
            self._workers[worker_id] = wc
        logger.info("Worker %s subscribed to queues %s", worker_id, queues)
        return wc

    def _handle_publish(self, sock: socket.socket, msg: Message):
        p    = msg.payload
        task = Task(
            task_id=p["task_id"], name=p["name"],
            args=p.get("args", []), kwargs=p.get("kwargs", {}),
            queue=p.get("queue", "default"),
            priority=p.get("priority", 5),
            retries=p.get("retries", 0),
            max_retries=p.get("max_retries", 3),
            retry_delay=p.get("retry_delay", 5.0),
            timeout=p.get("timeout"),
            eta=p.get("eta"),
            state=TaskState.QUEUED,
        )
        self._queue.enqueue(task)
        try:
            write_message(sock, Message(MessageType.CONFIRM, {"task_id": task.task_id}))
        except OSError:
            pass

    def _handle_ack(self, msg: Message):
        task_id = msg.payload.get("task_id")
        with self._lock:
            self._pending.pop(task_id, None)

    def _handle_nack(self, msg: Message):
        task_id  = msg.payload.get("task_id")
        requeue  = msg.payload.get("requeue", True)
        with self._lock:
            pending = self._pending.pop(task_id, None)
        if pending and requeue:
            task = pending.task
            task.retries += 1
            if task.retries <= task.max_retries:
                self._queue.requeue(task, delay=task.retry_delay)
            else:
                logger.warning("Task %s exceeded max retries", task_id)

    def _handle_result(self, sock: socket.socket, msg: Message):
        task_id = msg.payload.get("task_id")
        with self._lock:
            self._pending.pop(task_id, None)
            listeners = self._result_listeners.pop(task_id, [])
        for ls in listeners:
            try:
                write_message(ls, Message(MessageType.RESULT, msg.payload))
            except OSError:
                pass

    def _handle_revoke(self, msg: Message):
        # Best-effort: mark pending delivery as revoked if possible
        task_id = msg.payload.get("task_id")
        with self._lock:
            pending = self._pending.get(task_id)
            if pending:
                pending.task.state = TaskState.REVOKED

    def _handle_stats(self, sock: socket.socket):
        sizes = self._queue.all_sizes()
        with self._lock:
            n_workers = len(self._workers)
            n_pending = len(self._pending)
        try:
            write_message(sock, Message(MessageType.STATS, {
                "queues":       sizes,
                "workers":      n_workers,
                "pending_acks": n_pending,
            }))
        except OSError:
            pass

    # ── Background loops ──────────────────────────────────────────────────

    def _dispatch_loop(self):
        """Continuously try to match queued tasks to idle workers."""
        while self._running:
            dispatched = False
            with self._lock:
                workers = list(self._workers.values())

            for wc in workers:
                if wc.busy:
                    continue
                for q in wc.queues:
                    task = self._queue.dequeue(q)
                    if task is None:
                        continue
                    task.state = TaskState.RUNNING
                    payload = {
                        "task_id": task.task_id, "name": task.name,
                        "args": task.args, "kwargs": task.kwargs,
                        "queue": task.queue, "retries": task.retries,
                        "max_retries": task.max_retries,
                        "retry_delay": task.retry_delay,
                        "timeout": task.timeout,
                    }
                    try:
                        write_message(wc.sock, Message(MessageType.DELIVER, payload))
                        with self._lock:
                            wc.busy = True
                            self._pending[task.task_id] = PendingDelivery(
                                task=task, worker_id=wc.worker_id,
                                delivered_at=time.time(),
                            )
                        dispatched = True
                        break
                    except OSError:
                        # Worker disconnected — requeue the task
                        self._queue.requeue(task)
                        with self._lock:
                            self._workers.pop(wc.worker_id, None)
                        break

            if not dispatched:
                time.sleep(0.05)

    def _reaper_loop(self):
        """Requeue tasks that have not been acknowledged within ack_timeout."""
        while self._running:
            now     = time.time()
            timeout = self.config.ack_timeout
            to_requeue: list[PendingDelivery] = []

            with self._lock:
                for task_id, pending in list(self._pending.items()):
                    if now - pending.delivered_at > timeout:
                        to_requeue.append(pending)
                        del self._pending[task_id]

            for pending in to_requeue:
                task = pending.task
                task.retries += 1
                if task.retries <= task.max_retries:
                    logger.warning(
                        "Task %s timed out, requeuing (attempt %d/%d)",
                        task.task_id, task.retries, task.max_retries,
                    )
                    self._queue.requeue(task)
                else:
                    logger.error("Task %s exceeded max retries after timeout", task.task_id)

            time.sleep(1.0)
