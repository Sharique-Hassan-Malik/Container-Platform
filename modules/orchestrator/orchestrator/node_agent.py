"""The node agent: the only component that touches a workload.

Its contract is narrow and that is the point. It reads pods bound to its node,
starts what should be running, stops what should not, probes what is running,
and writes `status`. It never writes `spec`, never decides placement, and never
knows a Deployment exists.

Everything it does is derived from current state, not from events, so a restart
loses nothing: it lists its pods, sees what it has running, and closes the gap.
That is the same level-triggered discipline as every other controller here,
applied to processes instead of objects.

The node heartbeat is separate and deliberately dumb. `status.ready` plus a
timestamp; a stale timestamp is what makes a node failure detectable at all,
since a crashed node does not send an "I have crashed" event.
"""

from __future__ import annotations

import threading
import time

from .controller import Controller
from .objects import CRASHLOOP, FAILED, NODE, POD, READY, RUNNING, SCHEDULED, TERMINAL, Object
from .runtime import Handle, PodRuntime
from .store import Event, NotFoundError


class NodeAgent(Controller):
    name = "node-agent"
    resync_period = 0.2

    def __init__(self, store, node_name: str, runtime: PodRuntime, **kwargs):
        super().__init__(store, **kwargs)
        self.node_name = node_name
        self.runtime = runtime
        self.handles: dict[str, Handle] = {}
        self.name = f"node-agent[{node_name}]"
        self._heartbeat_thread: threading.Thread | None = None
        self._probe_thread: threading.Thread | None = None
        self.probe_interval = 0.02

    def interesting(self, event: Event) -> list[str]:
        if event.object.kind == POD and event.object.spec.get("nodeName") == self.node_name:
            return [event.key]
        return []

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "NodeAgent":
        super().start()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._heartbeat_thread.start()
        self._probe_thread = threading.Thread(target=self._probe_loop, daemon=True)
        self._probe_thread.start()
        return self

    def stop(self) -> None:
        super().stop()
        for thread in (self._heartbeat_thread, self._probe_thread):
            if thread:
                thread.join(timeout=2.0)
        for handle in list(self.handles.values()):
            try:
                self.runtime.stop(handle)
            except Exception:  # noqa: BLE001
                continue
        self.handles.clear()

    # -- reconcile -----------------------------------------------------------

    def reconcile(self, key: str) -> float | None:
        try:
            pod = self.store.get(key)
        except NotFoundError:
            # Deleted from the store: stop it here too. This is the only place
            # a running process is reaped after a pod disappears.
            handle = self.handles.pop(key, None)
            if handle:
                self.runtime.stop(handle)
            return None

        if pod.spec.get("nodeName") != self.node_name:
            handle = self.handles.pop(key, None)
            if handle:
                self.runtime.stop(handle)
            return None

        phase = pod.status.get("phase")
        if phase in TERMINAL:
            handle = self.handles.pop(key, None)
            if handle:
                self.runtime.stop(handle)
            return None

        handle = self.handles.get(key)
        if handle is None:
            backoff = self._backoff_remaining(pod)
            if backoff > 0:
                return backoff
            handle = self.runtime.start(pod)
            self.handles[key] = handle
            if handle.failed:
                self.runtime.stop(handle)
                self.handles.pop(key, None)
                return self._crashed(key, "container failed to start")
            self._write_phase(key, RUNNING)
            return 0.02

        if not self.runtime.alive(handle):
            self.handles.pop(key, None)
            self.runtime.stop(handle)
            return self._crashed(key, "process exited")

        if self.runtime.ready(handle):
            if phase != READY:
                self._write_phase(key, READY)
            return None
        return 0.02

    # -- background loops ----------------------------------------------------

    def _probe_loop(self) -> None:
        """Poll liveness and readiness for everything this node runs.

        Separate from reconcile because a probe result is a change in the world
        that generates no store event -- nothing would wake the reconciler up.
        """
        while self._running.is_set():
            time.sleep(self.probe_interval)
            for key in list(self.handles):
                self.queue.add(key)

    def _heartbeat(self, period: float = 0.1) -> None:
        while self._running.is_set():
            try:
                def mutate(node: Object) -> bool:
                    node.status["ready"] = True
                    node.status["heartbeat"] = time.time()
                    node.status["pods"] = len(self.handles)
                    return True

                self.store.update_with_retry(f"{NODE}/default/{self.node_name}", mutate)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(period)

    def _backoff_remaining(self, pod: Object) -> float:
        due = float(pod.status.get("nextRestartAt", 0.0))
        return max(0.0, due - time.time())

    def _crashed(self, key: str, reason: str) -> float:
        """Record a crash and schedule a restart, backing off exponentially.

        Capped, because an unbounded backoff makes a service that recovers on
        its own look permanently dead. Restarting *in place* is what keeps the
        ReplicaSet from spawning an endless stream of replacements.
        """
        delay = [0.05]

        def mutate(pod: Object) -> bool:
            restarts = int(pod.status.get("restartCount", 0)) + 1
            delay[0] = min(0.05 * (2 ** (restarts - 1)), 2.0)
            pod.status["phase"] = CRASHLOOP
            pod.status["reason"] = reason
            pod.status["restartCount"] = restarts
            pod.status["nextRestartAt"] = time.time() + delay[0]
            pod.status.pop("readySince", None)
            pod.status["node"] = self.node_name
            return True

        try:
            self.store.update_with_retry(key, mutate)
        except NotFoundError:
            return None
        return delay[0]

    def _write_phase(self, key: str, phase: str, reason: str = "") -> None:
        def mutate(pod: Object) -> bool:
            if pod.status.get("phase") == phase and not reason:
                return False
            pod.status["phase"] = phase
            if phase == READY and not pod.status.get("readySince"):
                pod.status["readySince"] = time.time()
            if phase != READY:
                pod.status.pop("readySince", None)
            if reason:
                pod.status["reason"] = reason
            pod.status["node"] = self.node_name
            return True

        try:
            self.store.update_with_retry(key, mutate)
        except NotFoundError:
            pass


class NodeMonitor(Controller):
    """Marks nodes whose heartbeat has stopped, and evicts their pods.

    A crashed node cannot report its own failure, so failure is inferred from
    silence. The eviction that follows is what turns "a node died" into "the
    ReplicaSet is short a replica", which every other controller already knows
    how to fix -- no special recovery path exists or is needed.
    """

    name = "node-monitor"
    resync_period = 0.1

    def __init__(self, store, *, timeout: float = 1.0, **kwargs):
        super().__init__(store, **kwargs)
        self.timeout = timeout
        self.evicted = 0
        self.marked_down: set[str] = set()

    def interesting(self, event: Event) -> list[str]:
        return [event.key] if event.object.kind == NODE else []

    def reconcile(self, key: str) -> float | None:
        try:
            node = self.store.get(key)
        except NotFoundError:
            return None
        if node.kind != NODE:
            return None

        heartbeat = node.status.get("heartbeat", 0.0)
        if not heartbeat:
            return self.timeout / 2
        if time.time() - heartbeat <= self.timeout:
            self.marked_down.discard(node.meta.name)
            return self.timeout / 2

        if node.status.get("ready", True):
            def mark(candidate: Object) -> bool:
                candidate.status["ready"] = False
                candidate.status["reason"] = "heartbeat timeout"
                return True

            self.store.update_with_retry(key, mark)
        self.marked_down.add(node.meta.name)

        for pod in self.store.list(POD):
            if pod.spec.get("nodeName") != node.meta.name:
                continue
            if pod.status.get("phase") in TERMINAL:
                continue
            try:
                self.store.delete(pod.key)
                self.evicted += 1
            except NotFoundError:
                continue
        return self.timeout / 2
