"""The reconciliation loop, and the one design decision the whole system rests on.

A controller can be built two ways.

**Edge-triggered.** An event arrives saying "pod X was deleted", and the
controller reacts to *that event*: creates one replacement. It is the obvious
design, it is what everyone writes first, and it is wrong. Its correctness
depends on receiving every event, exactly once, in order -- from a network. Drop
one and the system is permanently one replica short, with nothing to notice
because no further events are coming.

**Level-triggered.** An event arrives and the controller ignores its contents
entirely; it takes the *key*, re-reads current state, compares desired with
actual, and takes one step. Now an event is only a hint that something might
have changed. Dropping one costs latency until the next resync. Duplicating one
costs nothing, because the second pass finds nothing to do. Reordering is
meaningless, because the payload was never read.

`bench/triggering.py` runs both under a lossy watch and measures the difference.
The level-triggered reconciler converges; the edge-triggered one does not, and
never recovers on its own.

The work queue exists to make that safe under concurrency: a key already queued
is not queued again, and a key being processed defers a duplicate until the
current pass finishes -- so a burst of fifty events for one object costs two
reconciles, not fifty.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from .store import ClusterStore, Event

log = logging.getLogger("orchestrator.controller")


@dataclass
class WorkQueue:
    """Deduplicating queue with delayed retry.

    Three sets rather than one list, because "queued", "in flight" and "queued
    again while in flight" are genuinely different states. Collapsing them lets
    a change that arrives mid-reconcile be lost -- the reconcile that is already
    running read state from before it.
    """

    _pending: list[str] = field(default_factory=list)
    _queued: set[str] = field(default_factory=set)
    _processing: set[str] = field(default_factory=set)
    _dirty: set[str] = field(default_factory=set)
    _delayed: dict[str, float] = field(default_factory=dict)
    _failures: dict[str, int] = field(default_factory=dict)
    _condition: threading.Condition = field(default_factory=threading.Condition)
    _closed: bool = False

    added: int = 0
    deduplicated: int = 0
    processed: int = 0
    retries: int = 0

    def add(self, key: str) -> None:
        with self._condition:
            self.added += 1
            if key in self._processing:
                # Arrived mid-reconcile: remember that the object changed again,
                # and requeue when the current pass finishes.
                self._dirty.add(key)
                return
            if key in self._queued:
                self.deduplicated += 1
                return
            self._queued.add(key)
            self._pending.append(key)
            self._condition.notify()

    def add_after(self, key: str, delay: float) -> None:
        with self._condition:
            due = time.monotonic() + delay
            self._delayed[key] = min(self._delayed.get(key, due), due)
            self._condition.notify()

    def get(self, timeout: float = 0.25) -> str | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                now = time.monotonic()
                for key, due in list(self._delayed.items()):
                    if due <= now:
                        del self._delayed[key]
                        if key not in self._queued and key not in self._processing:
                            self._queued.add(key)
                            self._pending.append(key)
                if self._pending:
                    key = self._pending.pop(0)
                    self._queued.discard(key)
                    self._processing.add(key)
                    return key
                if self._closed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(min(remaining, 0.05))

    def done(self, key: str, *, requeue_after: float | None = None) -> None:
        with self._condition:
            self._processing.discard(key)
            self.processed += 1
            if key in self._dirty:
                self._dirty.discard(key)
                if key not in self._queued:
                    self._queued.add(key)
                    self._pending.append(key)
                    self._condition.notify()
            elif requeue_after is not None:
                due = time.monotonic() + requeue_after
                self._delayed[key] = min(self._delayed.get(key, due), due)
                self._condition.notify()

    def fail(self, key: str, base_delay: float = 0.05, max_delay: float = 2.0) -> float:
        """Exponential backoff, so a permanently broken object cannot hot-loop."""
        with self._condition:
            self._processing.discard(key)
            self.retries += 1
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            delay = min(base_delay * (2 ** (count - 1)), max_delay)
            self._delayed[key] = time.monotonic() + delay
            self._condition.notify()
            return delay

    def succeed(self, key: str) -> None:
        with self._condition:
            self._failures.pop(key, None)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def __len__(self) -> int:
        with self._condition:
            return len(self._pending) + len(self._delayed)


class Controller:
    """Base class: watch for hints, reconcile from current state.

    Subclasses implement `reconcile(key)` and `interesting(event)`. Nothing
    else. In particular a subclass never receives the event object, which is
    deliberate -- it is the only way to guarantee the reconcile cannot depend on
    it.
    """

    name = "controller"
    resync_period = 1.0

    def __init__(self, store: ClusterStore, *, resync_period: float | None = None):
        self.store = store
        self.resync_period = resync_period if resync_period is not None else self.resync_period
        self.queue = WorkQueue()
        self._cancel_watch = None
        self._thread: threading.Thread | None = None
        self._resync_thread: threading.Thread | None = None
        self._running = threading.Event()
        self.reconciles = 0
        self.errors = 0

    # -- to implement --------------------------------------------------------

    def interesting(self, event: Event) -> list[str]:
        """Map an event to the keys that may now need work.

        Usually the object itself, but a Pod event maps to its owning
        ReplicaSet: the thing that needs to reconsider is whoever is responsible
        for the desired state, not the object that changed.
        """
        return [event.key]

    def reconcile(self, key: str) -> float | None:
        """Take one step toward the desired state. Return a requeue delay, or None.

        Must be idempotent and must read its own inputs. Being called twice for
        one change is normal; being called for an object that no longer exists
        is normal; being called with no event at all (resync) is normal.
        """
        raise NotImplementedError

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "Controller":
        self._running.set()
        self._cancel_watch = self.store.watch(self._on_event)
        for obj in self.store.list():
            for key in self.interesting(Event("ADDED", obj, obj.meta.resource_version)):
                self.queue.add(key)
        self._thread = threading.Thread(target=self._worker, name=f"{self.name}-worker", daemon=True)
        self._thread.start()
        if self.resync_period:
            self._resync_thread = threading.Thread(target=self._resync, name=f"{self.name}-resync", daemon=True)
            self._resync_thread.start()
        return self

    def stop(self) -> None:
        self._running.clear()
        if self._cancel_watch:
            self._cancel_watch()
        self.queue.close()
        for thread in (self._thread, self._resync_thread):
            if thread:
                thread.join(timeout=2.0)

    def __enter__(self) -> "Controller":
        return self.start()

    def __exit__(self, *_) -> None:
        self.stop()

    # -- internals -----------------------------------------------------------

    def _on_event(self, event: Event) -> None:
        for key in self.interesting(event):
            self.queue.add(key)

    def _worker(self) -> None:
        while self._running.is_set():
            key = self.queue.get(timeout=0.1)
            if key is None:
                continue
            try:
                requeue = self.reconcile(key)
                self.reconciles += 1
                self.queue.succeed(key)
                self.queue.done(key, requeue_after=requeue)
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                log.debug("%s: reconcile %s failed: %s", self.name, key, exc)
                self.queue.fail(key)

    def _resync(self) -> None:
        """Periodically re-enqueue everything.

        The safety net that makes dropped events survivable. A watch that misses
        a notification costs at most one resync period of latency, rather than
        permanent divergence -- and this loop is why the store is allowed to
        drop an event when a watcher misbehaves.
        """
        while self._running.is_set():
            time.sleep(self.resync_period)
            if not self._running.is_set():
                return
            try:
                for obj in self.store.list():
                    for key in self.interesting(Event("SYNC", obj, obj.meta.resource_version)):
                        self.queue.add(key)
            except Exception:  # noqa: BLE001
                continue

    def reconcile_once(self, key: str) -> float | None:
        """Run one reconcile synchronously. Tests use this instead of sleeping."""
        result = self.reconcile(key)
        self.reconciles += 1
        return result

    def drain(self, timeout: float = 5.0, quiet_for: float = 0.05) -> bool:
        """Wait until the queue has been empty and idle for `quiet_for`.

        Better than a fixed sleep in tests: it fails loudly on a controller that
        never settles, instead of passing whenever the sleep happened to be long
        enough.
        """
        deadline = time.monotonic() + timeout
        quiet_since = None
        while time.monotonic() < deadline:
            if len(self.queue) == 0 and not self.queue._processing:
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= quiet_for:
                    return True
            else:
                quiet_since = None
            time.sleep(0.005)
        return False


class EdgeTriggeredController(Controller):
    """The design this project exists to argue against. Used only by the benchmark.

    Reacts to the event payload rather than re-reading state. Correct exactly
    when the event stream is perfect, which is never.
    """

    name = "edge-controller"
    resync_period = 0.0        # no safety net, by construction

    def handle(self, event: Event) -> None:
        raise NotImplementedError

    def start(self) -> "EdgeTriggeredController":
        self._running.set()
        self._cancel_watch = self.store.watch(self._dispatch)
        return self

    def _dispatch(self, event: Event) -> None:
        try:
            self.handle(event)
            self.reconciles += 1
        except Exception:  # noqa: BLE001
            self.errors += 1

    def stop(self) -> None:
        self._running.clear()
        if self._cancel_watch:
            self._cancel_watch()
