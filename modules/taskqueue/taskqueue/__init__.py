"""taskqueue — a broker, workers and a result backend, over a binary protocol.

    from taskqueue import BrokerServer, TaskClient, Worker, task

    @task
    def resize(path: str) -> str: ...

Runs standalone (`python tq.py broker`), and is also the Job backend for the
control plane in this repository — a Job is a queued task with a pod attached.
"""

from .backend.sqlite_backend import ResultBackend
from .broker.queue import TaskQueue
from .broker.server import BrokerServer
from .client.client import AsyncResult, TaskClient
from .config import (
    BackendConfig,
    BrokerConfig,
    DashboardConfig,
    Message,
    MessageType,
    Task,
    TaskResult,
    TaskState,
    WorkerConfig,
)
from .dashboard.server import DashboardServer
from .worker.worker import Worker, get_task, task

__all__ = [
    "ResultBackend", "TaskQueue", "BrokerServer", "TaskClient", "AsyncResult",
    "DashboardServer", "Worker", "task", "get_task",
    "BrokerConfig", "WorkerConfig", "BackendConfig", "DashboardConfig",
    "Message", "MessageType", "Task", "TaskState", "TaskResult",
]
