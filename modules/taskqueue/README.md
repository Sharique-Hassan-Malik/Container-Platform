# Distributed Task Queue

> Part of the [Container Platform](../../README.md). Runs standalone from this
> folder, and composes with the other four modules through the `ctl` CLI.

A Celery-like distributed task queue built entirely from scratch — custom TCP
broker protocol, worker process pool, SQLite result backend and a web dashboard.

No Celery, no Redis, no RabbitMQ, no external libraries.

---

## Features

- Custom length-prefixed TCP protocol (`TSKQ` magic, JSON payload)
- Multi-threaded broker with priority dispatch, ACK timeout reaper and result relay
- Priority queue with ETA scheduling (1 = urgent, 10 = background)
- SQLite persistence — broker recovers queue state after restart
- Worker process pool via `ProcessPoolExecutor` — CPU-safe, crash-isolated
- `@task()` decorator for task registration
- Automatic retry with configurable delay and max attempts
- `AsyncResult` handle with blocking `.get(timeout)`
- SQLite result backend with TTL-based expiry
- Web dashboard at `localhost:8888` — live queue stats, recent results
- 40+ offline pytest tests — no network, no subprocess required

---

## Requirements

Python 3.11+ — no runtime dependencies.

```bash
pip install pytest   # for running tests only
```

---

## Quick Start

Start the broker in one terminal:

```bash
python tq.py broker
```

Start a worker in a second terminal:

```bash
python tq.py worker -I scripts.tasks
```

Submit a task and wait for the result:

```bash
python tq.py submit scripts.tasks.add 1 2 --wait
```

Start the dashboard:

```bash
python tq.py dashboard
# Open http://127.0.0.1:8888/
```

---

## Defining Tasks

```python
# myapp/tasks.py
from worker.worker import task

@task()
def add(x, y):
    return x + y

@task(name="myapp.send_email")
def send_email(to, subject, body):
    ...
```

Start a worker that imports your task module:

```bash
python tq.py worker -I myapp.tasks --queues default email
```

---

## Publishing Tasks Programmatically

```python
from config import BrokerConfig
from client.client import TaskClient

client = TaskClient(BrokerConfig(host="127.0.0.1", port=6380))

ar = client.publish(
    "scripts.tasks.add",
    args=[10, 20],
    queue="default",
    priority=3,
    max_retries=5,
)

result = ar.get(timeout=30)
print(result.result)   # 30
print(result.state)    # TaskState.SUCCESS
print(result.duration) # seconds
```

---

## CLI Reference

```
python tq.py broker
    --host 127.0.0.1  --port 6380
    --ack-timeout 30  --persist broker.db

python tq.py worker
    --broker-host 127.0.0.1  --broker-port 6380
    --queues default high    --concurrency 4
    -I myapp.tasks           # repeatable

python tq.py submit <task_name> [args...]
    --kwargs '{"key":"val"}' --queue default
    --priority 5             --wait --timeout 30

python tq.py stats
python tq.py dashboard  --port 8888
```

---

## Running Tests

```bash
python -m pytest tests/ -v
```

All tests run offline — no broker process, no worker, no network.

---

## Architecture Summary

```
Client ──PUBLISH──► Broker ──DELIVER──► Worker Pool
       ◄─CONFIRM──         ◄──RESULT──  (subprocess)

Broker internals:
  TaskQueue   priority heap + ETA + SQLite persist
  Dispatcher  thread: dequeue → DELIVER idle workers
  Reaper      thread: requeue timed-out deliveries

Worker internals:
  Coordinator  TCP connection, receives DELIVER
  Pool         ProcessPoolExecutor runs task functions
  Registry     @task() decorator populates task lookup
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for full protocol documentation,
retry logic, queue ordering and the dashboard design.

---

## Project Structure

```
taskqueue/
├── tq.py
├── config.py
├── broker/
│   ├── protocol.py
│   ├── queue.py
│   └── server.py
├── worker/
│   └── worker.py
├── client/
│   └── client.py
├── backend/
│   └── sqlite_backend.py
├── dashboard/
│   └── server.py
├── tests/
│   └── test_taskqueue.py
└── scripts/
    └── tasks.py
```
