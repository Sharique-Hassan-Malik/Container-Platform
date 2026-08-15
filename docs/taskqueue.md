# Architecture — Distributed Task Queue

## Overview

A Celery-like distributed task queue built from scratch.  Tasks are published
over a custom TCP protocol, dispatched to worker process pools, and results
persisted in SQLite.  A web dashboard shows live queue status.

No Celery, no Redis, no RabbitMQ, no external libraries.

---

## System Components

```
 ┌────────────┐   PUBLISH     ┌──────────────────┐   DELIVER  ┌────────────────┐
 │   Client   │ ────────────► │   Broker Server  │ ─────────► │  Worker Pool   │
 │ TaskClient │ ◄──── CONFIRM │   (TCP, port     │ ◄── RESULT │  (processes)   │
 └────────────┘               │    6380)         │            └────────────────┘
                              │                  │
 ┌────────────┐   STATS ─────►│  TaskQueue       │
 │  Dashboard │              │  (priority heap) │
 │  (HTTP     │              │                  │
 │   :8888)   │◄─────────────│  SQLite persist  │
 └────────────┘              └──────────────────┘
                                       │
                              ┌────────▼───────┐
                              │  ResultBackend │
                              │  (SQLite)      │
                              └────────────────┘
```

---

## Wire Protocol

Every message over TCP is framed as:

```
[0:4]   magic b"TSKQ"         (4 bytes)
[4:8]   payload length        (uint32, big-endian)
[8:]    JSON payload           (UTF-8)
```

Length-prefix framing means the receiver knows exactly how many bytes to read
before parsing.  The JSON payload always contains a `"type"` field (one of the
`MessageType` enum values) and a `"payload"` dict.

Message types by role:

| Direction | Message | Meaning |
|-----------|---------|---------|
| Client → Broker | PUBLISH | Enqueue a task |
| Client → Broker | REVOKE | Cancel a task |
| Client → Broker | STATS | Request queue statistics |
| Broker → Client | CONFIRM | Acknowledge publish |
| Broker → Client | RESULT | Relay worker result to waiting client |
| Worker → Broker | SUBSCRIBE | Register for named queues |
| Worker → Broker | ACK | Confirm task receipt |
| Worker → Broker | NACK | Reject task (with optional requeue) |
| Worker → Broker | RESULT | Report task outcome |
| Broker → Worker | DELIVER | Send a task to execute |
| Both | PING / PONG | Keepalive |
| Broker → Any | ERROR | Signal a protocol error |

---

## Broker Server

Multi-threaded, one thread per connection.  Three background threads run
continuously:

**Dispatcher** — every 50 ms, scans idle workers and dequeues the highest-
priority task from each worker's subscribed queues.  Sends DELIVER and records
the delivery in a `pending` dict.

**Reaper** — every second, scans the `pending` dict for deliveries older than
`ack_timeout` seconds.  Requeues timed-out tasks with `retries += 1`.

**Per-connection handler** — reads messages in a loop:
- SUBSCRIBE: registers the worker, adds it to the workers dict.
- PUBLISH: deserialises the task, enqueues it.
- ACK: removes the delivery from pending.
- NACK: either requeues (if retries remaining) or marks as permanently failed.
- RESULT: removes from pending, notifies any clients waiting on that task_id.

---

## Task Priority Queue

Priority heap keyed by `(priority, eta, created_at)`:

- **priority**: 1 (urgent) to 10 (background)
- **eta**: earliest time to run (Unix timestamp); tasks whose ETA is in the
  future are not dequeued even if they are at the head of the heap
- **created_at**: FIFO tiebreak within same priority and ETA

SQLite persistence writes every enqueue and dequeue.  On startup, the broker
reloads all unprocessed tasks from `broker.db`.

---

## Worker Process Pool

The coordinator thread connects to the broker and receives DELIVER messages.
Each task is submitted to a `ProcessPoolExecutor`:

```
DELIVER → ACK (immediate) → ProcessPoolExecutor.submit()
                                │
              ┌─────────────────┴──────────────────┐
              │ task function runs in subprocess    │
              └─────────────────┬──────────────────┘
              success           │            failure
              RESULT(SUCCESS) ◄─┤─► retries < max?
                                │       yes: NACK(requeue=True)
                                │       no:  RESULT(FAILURE)
```

The subprocess model ensures:
- CPU-bound tasks do not block the coordinator's I/O loop.
- A crashed task cannot corrupt the coordinator's memory.
- `ProcessPoolExecutor` manages process lifecycle.

Tasks are registered with the `@task()` decorator:

```python
@task()
def add(x, y):
    return x + y

@task(name="myapp.send_email")
def send_email(to, subject, body):
    ...
```

The registry is a module-level dict.  Workers import task modules at startup
so the registry is populated in each subprocess.

---

## Retry Logic

Tasks carry `retries`, `max_retries` and `retry_delay` fields:

1. Worker sends NACK with `requeue=True` on transient failure.
2. Broker increments `task.retries` and calls `queue.requeue(task, delay=retry_delay)`.
3. `requeue` sets `task.eta = now + delay`, so the task sits in the future
   portion of the heap until the delay expires.
4. When `retries > max_retries` the broker drops the task and logs an error.

Timeouts (ack_timeout) go through the same path via the reaper thread.

---

## Result Backend

`ResultBackend` wraps a SQLite database with:
- `store(result)` — upsert by task_id
- `get(task_id)` — single-result lookup
- `wait(task_id, timeout)` — blocking poll for success/failure
- `all_recent(limit)` — ordered by `stored_at` DESC for the dashboard
- Background TTL expiry (default 1 hour)

---

## Dashboard

A single-file stdlib `HTTPServer` serving:
- `GET /` — HTML page with polling JS (2 s interval)
- `GET /api/stats` — JSON from broker STATS message
- `GET /api/results` — JSON from result backend

No WebSockets, no external JS.  The HTML page polls `/api/stats` and
`/api/results` with `setInterval` and updates the DOM directly.

---

## Files

```
taskqueue/
├── tq.py                       — CLI: broker, worker, submit, stats, dashboard
├── config.py                   — Task, TaskResult, Message, all config dataclasses
├── broker/
│   ├── protocol.py             — frame encode/decode, TCP read/write helpers
│   ├── queue.py                — priority heap + ETA + SQLite persistence
│   └── server.py               — multi-threaded TCP broker
├── worker/
│   └── worker.py               — @task registry, ProcessPoolExecutor pool, coordinator
├── client/
│   └── client.py               — TaskClient, AsyncResult
├── backend/
│   └── sqlite_backend.py       — ResultBackend with TTL expiry
├── dashboard/
│   └── server.py               — DashboardServer, inline HTML
├── tests/
│   └── test_taskqueue.py       — 40+ offline tests
└── scripts/
    └── tasks.py                — example task definitions
```
