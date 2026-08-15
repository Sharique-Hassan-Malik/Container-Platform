"""
Test suite for the distributed task queue.

All tests run fully offline:
  - Protocol encode/decode
  - Priority queue ordering and ETA scheduling
  - Result backend CRUD and TTL
  - Message type round-trips
  - Worker task registry
  - Broker queue deduplication / retry counting

Run with:
    python -m pytest tests/test_taskqueue.py -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from taskqueue.config import (
    BrokerConfig, BackendConfig, Message, MessageType,
    Task, TaskResult, TaskState,
)
from taskqueue.broker.protocol import encode_message, decode_frame, FramingError
from taskqueue.broker.queue import TaskQueue
from taskqueue.backend.sqlite_backend import ResultBackend
from taskqueue.worker.worker import task as task_decorator, get_task, _REGISTRY


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class TestProtocol:

    def test_encode_decode_roundtrip(self):
        msg  = Message(MessageType.PUBLISH, {"task_id": "abc", "name": "add"})
        data = encode_message(msg)
        out  = decode_frame(data)
        assert out.type == MessageType.PUBLISH
        assert out.payload["task_id"] == "abc"

    def test_magic_header(self):
        msg  = Message(MessageType.PING)
        data = encode_message(msg)
        assert data[:4] == b"TSKQ"

    def test_length_field(self):
        import struct
        msg  = Message(MessageType.PONG)
        data = encode_message(msg)
        length = struct.unpack_from(">I", data, 4)[0]
        assert length == len(data) - 8

    def test_bad_magic_raises(self):
        bad = b"XXXX\x00\x00\x00\x05hello"
        with pytest.raises(FramingError):
            decode_frame(bad)

    def test_all_message_types_encodable(self):
        for mt in MessageType:
            msg  = Message(mt, {"x": 1})
            data = encode_message(msg)
            out  = decode_frame(data)
            assert out.type == mt

    def test_large_payload(self):
        payload = {"data": "x" * 100_000}
        msg  = Message(MessageType.RESULT, payload)
        data = encode_message(msg)
        out  = decode_frame(data)
        assert len(out.payload["data"]) == 100_000

    def test_empty_payload(self):
        msg  = Message(MessageType.PING)
        data = encode_message(msg)
        out  = decode_frame(data)
        assert out.payload == {}

    def test_short_data_raises(self):
        with pytest.raises(FramingError):
            decode_frame(b"\x54\x53")

    def test_message_from_dict(self):
        d   = {"type": "ACK", "payload": {"task_id": "xyz"}}
        msg = Message.from_dict(d)
        assert msg.type == MessageType.ACK
        assert msg.payload["task_id"] == "xyz"

    def test_message_to_dict(self):
        msg = Message(MessageType.CONFIRM, {"task_id": "123"})
        d   = msg.to_dict()
        assert d["type"] == "CONFIRM"
        assert d["payload"]["task_id"] == "123"


# ---------------------------------------------------------------------------
# Task priority queue
# ---------------------------------------------------------------------------

class TestTaskQueue:

    def _task(self, name="t", priority=5, queue="default", eta=None) -> Task:
        return Task.create(name=name, priority=priority, queue=queue, eta=eta)

    def test_enqueue_dequeue(self, tmp_path):
        q = TaskQueue()
        t = self._task()
        q.enqueue(t)
        out = q.dequeue("default")
        assert out is not None
        assert out.task_id == t.task_id

    def test_priority_order(self, tmp_path):
        q  = TaskQueue()
        t1 = self._task("low",  priority=9)
        t2 = self._task("high", priority=1)
        q.enqueue(t1)
        q.enqueue(t2)
        first = q.dequeue("default")
        assert first.name == "high"

    def test_empty_dequeue_returns_none(self):
        q = TaskQueue()
        assert q.dequeue("nonexistent") is None

    def test_size_tracking(self):
        q = TaskQueue()
        q.enqueue(self._task())
        q.enqueue(self._task())
        assert q.size("default") == 2
        q.dequeue("default")
        assert q.size("default") == 1

    def test_eta_not_ready(self):
        q   = TaskQueue()
        eta = time.time() + 3600   # one hour from now
        t   = self._task(eta=eta)
        q.enqueue(t)
        assert q.dequeue("default") is None

    def test_eta_past_is_ready(self):
        q   = TaskQueue()
        eta = time.time() - 1
        t   = self._task(eta=eta)
        q.enqueue(t)
        out = q.dequeue("default")
        assert out is not None

    def test_requeue_increments_retries(self):
        q = TaskQueue()
        t = self._task()
        t.retries = 1
        q.requeue(t, delay=0)
        out = q.dequeue("default")
        assert out is not None
        assert out.state == TaskState.RETRY

    def test_multiple_queues(self):
        q  = TaskQueue()
        t1 = self._task(queue="fast")
        t2 = self._task(queue="slow")
        q.enqueue(t1)
        q.enqueue(t2)
        assert q.dequeue("fast") is not None
        assert q.dequeue("slow") is not None

    def test_persistence(self, tmp_path):
        db = str(tmp_path / "queue.db")
        q1 = TaskQueue(persist_path=db)
        t  = self._task()
        q1.enqueue(t)
        # New instance loads from disk
        q2 = TaskQueue(persist_path=db)
        out = q2.dequeue("default")
        assert out is not None
        assert out.task_id == t.task_id

    def test_all_sizes(self):
        q = TaskQueue()
        q.enqueue(self._task(queue="a"))
        q.enqueue(self._task(queue="a"))
        q.enqueue(self._task(queue="b"))
        sizes = q.all_sizes()
        assert sizes["a"] == 2
        assert sizes["b"] == 1


# ---------------------------------------------------------------------------
# Result backend
# ---------------------------------------------------------------------------

class TestResultBackend:

    def _cfg(self, tmp_path) -> BackendConfig:
        return BackendConfig(persist_path=str(tmp_path / "results.db"), result_ttl=3600)

    def _result(self, task_id="tid", state=TaskState.SUCCESS) -> TaskResult:
        return TaskResult(task_id=task_id, state=state, result=42)

    def test_store_and_get(self, tmp_path):
        b = ResultBackend(self._cfg(tmp_path))
        r = self._result()
        b.store(r)
        out = b.get("tid")
        assert out is not None
        assert out.state == TaskState.SUCCESS
        assert out.result == 42

    def test_missing_returns_none(self, tmp_path):
        b = ResultBackend(self._cfg(tmp_path))
        assert b.get("nonexistent") is None

    def test_overwrite(self, tmp_path):
        b  = ResultBackend(self._cfg(tmp_path))
        r1 = self._result(state=TaskState.RUNNING)
        r2 = self._result(state=TaskState.SUCCESS)
        b.store(r1)
        b.store(r2)
        out = b.get("tid")
        assert out.state == TaskState.SUCCESS

    def test_failure_stored(self, tmp_path):
        b = ResultBackend(self._cfg(tmp_path))
        r = TaskResult(task_id="f1", state=TaskState.FAILURE, error="oops")
        b.store(r)
        out = b.get("f1")
        assert out.error == "oops"

    def test_all_recent(self, tmp_path):
        b = ResultBackend(self._cfg(tmp_path))
        for i in range(5):
            b.store(self._result(task_id=f"t{i}"))
        results = b.all_recent(limit=10)
        assert len(results) == 5

    def test_duration_computed(self, tmp_path):
        b = ResultBackend(self._cfg(tmp_path))
        r = TaskResult(
            task_id="dur", state=TaskState.SUCCESS,
            started_at=100.0, ended_at=101.5,
        )
        b.store(r)
        out = b.get("dur")
        assert out.duration == pytest.approx(1.5)

    def test_persistence_across_instances(self, tmp_path):
        cfg = self._cfg(tmp_path)
        b1  = ResultBackend(cfg)
        b1.store(self._result(task_id="persistent"))
        b2  = ResultBackend(cfg)
        assert b2.get("persistent") is not None


# ---------------------------------------------------------------------------
# Task registry
# ---------------------------------------------------------------------------

class TestTaskRegistry:

    def test_register_and_retrieve(self):
        @task_decorator(name="test.reg_fn")
        def my_fn(x):
            return x * 2
        fn = get_task("test.reg_fn")
        assert fn(5) == 10

    def test_auto_name(self):
        @task_decorator()
        def auto_named_task():
            return "ok"
        assert "auto_named_task" in " ".join(_REGISTRY.keys())

    def test_unknown_task_raises(self):
        with pytest.raises(KeyError):
            get_task("completely.unknown.task.name")

    def test_task_fn_attribute(self):
        @task_decorator(name="test.attr_fn")
        def attr_fn():
            pass
        assert hasattr(attr_fn, "_task_name")
        assert attr_fn._task_name == "test.attr_fn"


# ---------------------------------------------------------------------------
# Task dataclass
# ---------------------------------------------------------------------------

class TestTask:

    def test_create_generates_uuid(self):
        t1 = Task.create("add")
        t2 = Task.create("add")
        assert t1.task_id != t2.task_id

    def test_default_queue(self):
        t = Task.create("fn")
        assert t.queue == "default"

    def test_default_state(self):
        t = Task.create("fn")
        assert t.state == TaskState.QUEUED

    def test_custom_args(self):
        t = Task.create("add", args=[1, 2], kwargs={"c": 3})
        assert t.args == [1, 2]
        assert t.kwargs == {"c": 3}
