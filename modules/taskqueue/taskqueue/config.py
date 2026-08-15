from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Wire protocol constants
# ---------------------------------------------------------------------------

PROTOCOL_VERSION  = 1
HEADER_SIZE       = 8        # 4-byte magic + 4-byte payload length
MAGIC             = b"TSKQ"
MAX_MESSAGE_BYTES = 10 * 1024 * 1024   # 10 MB


# ---------------------------------------------------------------------------
# Task and message enums
# ---------------------------------------------------------------------------

class TaskState(Enum):
    PENDING   = "PENDING"
    QUEUED    = "QUEUED"
    RUNNING   = "RUNNING"
    SUCCESS   = "SUCCESS"
    FAILURE   = "FAILURE"
    RETRY     = "RETRY"
    REVOKED   = "REVOKED"


class MessageType(Enum):
    # Client → Broker
    PUBLISH   = "PUBLISH"     # enqueue a task
    ACK       = "ACK"         # worker acknowledges receipt
    NACK      = "NACK"        # worker rejects (requeue or fail)
    RESULT    = "RESULT"      # worker sends result
    SUBSCRIBE = "SUBSCRIBE"   # worker registers for a queue
    REVOKE    = "REVOKE"      # client cancels a task

    # Broker → Client/Worker
    DELIVER   = "DELIVER"     # broker pushes a task to a worker
    CONFIRM   = "CONFIRM"     # broker confirms publish
    ERROR     = "ERROR"       # broker signals an error
    STATS     = "STATS"       # broker responds with queue statistics

    # Keepalive
    PING      = "PING"
    PONG      = "PONG"


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Task:
    task_id:     str
    name:        str
    args:        list
    kwargs:      dict
    queue:       str          = "default"
    priority:    int          = 5         # 1 (high) – 10 (low)
    retries:     int          = 0
    max_retries: int          = 3
    retry_delay: float        = 5.0       # seconds
    timeout:     float | None = None
    eta:         float | None = None      # earliest time to run (unix timestamp)
    created_at:  float        = field(default_factory=time.time)
    state:       TaskState    = TaskState.QUEUED

    @classmethod
    def create(
        cls,
        name: str,
        args: list | None = None,
        kwargs: dict | None = None,
        **options,
    ) -> "Task":
        return cls(
            task_id=str(uuid.uuid4()),
            name=name,
            args=args or [],
            kwargs=kwargs or {},
            **options,
        )


@dataclass
class TaskResult:
    task_id:    str
    state:      TaskState
    result:     Any           = None
    error:      str           = ""
    traceback:  str           = ""
    started_at: float         = 0.0
    ended_at:   float         = 0.0
    worker_id:  str           = ""

    @property
    def duration(self) -> float:
        if self.started_at and self.ended_at:
            return self.ended_at - self.started_at
        return 0.0


@dataclass
class Message:
    type:    MessageType
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type.value, "payload": self.payload}

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        return cls(
            type=MessageType(d["type"]),
            payload=d.get("payload", {}),
        )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class BrokerConfig:
    host:              str   = "127.0.0.1"
    port:              int   = 6380
    max_connections:   int   = 100
    ack_timeout:       float = 30.0    # seconds before unacked task is requeued
    max_queue_size:    int   = 10_000
    persist_path:      str   = "broker.db"   # SQLite path for queue persistence


@dataclass
class WorkerConfig:
    broker_host:     str   = "127.0.0.1"
    broker_port:     int   = 6380
    queues:          list  = field(default_factory=lambda: ["default"])
    concurrency:     int   = 4         # number of worker processes
    prefetch:        int   = 1         # tasks fetched ahead per worker
    heartbeat:       float = 10.0      # seconds between pings
    reconnect_delay: float = 5.0
    worker_id:       str   = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class BackendConfig:
    persist_path:    str   = "results.db"    # SQLite path
    result_ttl:      float = 3600.0          # seconds to keep results


@dataclass
class DashboardConfig:
    host:  str = "127.0.0.1"
    port:  int = 8888
