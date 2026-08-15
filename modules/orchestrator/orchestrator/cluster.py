"""Wiring: one object that starts every controller and one node agent per node.

A real cluster runs these as separate processes on separate machines. Running
them as threads against one store changes nothing about the logic -- every
controller already assumes it sees stale state, races with its peers, and can
be restarted at any moment, because that is what the level-triggered design
requires whether or not the deployment topology forces it.
"""

from __future__ import annotations

import time

from .controller import Controller
from .node_agent import NodeAgent, NodeMonitor
from .objects import (
    DEPLOYMENT,
    NODE,
    POD,
    READY,
    REPLICASET,
    Object,
    new_object,
    node_spec,
)
from .runtime import PodRuntime, SimulatedRuntime
from .scheduler import Scheduler
from .store import ClusterStore, MemoryStore, NotFoundError
from .workloads import DeploymentController, ReplicaSetController, is_available


class ControlPlane:
    def __init__(self, store: ClusterStore | None = None, *, runtime: PodRuntime | None = None,
                 spread: bool = True, node_timeout: float = 1.0, monitor: bool = True):
        self.store = store or MemoryStore()
        self.runtime = runtime or SimulatedRuntime()
        self.scheduler = Scheduler(self.store, spread=spread)
        self.replicasets = ReplicaSetController(self.store)
        self.deployments = DeploymentController(self.store)
        self.monitor = NodeMonitor(self.store, timeout=node_timeout) if monitor else None
        self.agents: dict[str, NodeAgent] = {}
        self._started = False

    # -- topology ------------------------------------------------------------

    def with_nodes(self, count: int, *, cpu: float = 4.0, memory_mb: int = 8192,
                   labels: dict[str, str] | None = None) -> "ControlPlane":
        for i in range(count):
            self.add_node(f"node-{i}", cpu=cpu, memory_mb=memory_mb, labels=labels)
        return self

    def add_node(self, name: str, *, cpu: float = 4.0, memory_mb: int = 8192,
                 labels: dict[str, str] | None = None) -> Object:
        node = new_object(NODE, name, node_spec(cpu, memory_mb, labels=labels), labels=labels or {})
        node.status = {"ready": True, "heartbeat": time.time()}
        created = self.store.create(node)
        agent = NodeAgent(self.store, name, self.runtime)
        self.agents[name] = agent
        if self._started:
            agent.start()
        return created

    def remove_node(self, name: str, *, graceful: bool = False) -> None:
        """Simulate a node going away.

        `graceful=False` is the interesting case: the agent stops without
        touching the store, so the node's pods stay listed as Running with no
        process behind them -- exactly what a crashed machine looks like. Only
        the missing heartbeat reveals it.
        """
        agent = self.agents.pop(name, None)
        if agent:
            agent.stop()
        if graceful:
            try:
                self.store.delete(f"{NODE}/default/{name}")
            except NotFoundError:
                pass

    # -- lifecycle -----------------------------------------------------------

    def controllers(self) -> list[Controller]:
        base: list[Controller] = [self.scheduler, self.replicasets, self.deployments]
        if self.monitor:
            base.append(self.monitor)
        return base + list(self.agents.values())

    def start(self) -> "ControlPlane":
        for controller in self.controllers():
            controller.start()
        self._started = True
        return self

    def stop(self) -> None:
        for controller in reversed(self.controllers()):
            controller.stop()
        self._started = False

    def __enter__(self) -> "ControlPlane":
        return self.start() if not self._started else self

    def __exit__(self, *_) -> None:
        self.stop()

    # -- operations ----------------------------------------------------------

    def apply_deployment(self, name: str, spec: dict, *, namespace: str = "default") -> Object:
        key = f"{DEPLOYMENT}/{namespace}/{name}"
        try:
            self.store.get(key)
        except NotFoundError:
            return self.store.create(
                new_object(DEPLOYMENT, name, spec, namespace=namespace, labels=spec.get("selector", {}))
            )

        def mutate(deployment: Object) -> bool:
            if deployment.spec == spec:
                return False
            deployment.spec = spec
            return True

        return self.store.update_with_retry(key, mutate)

    # -- observation ---------------------------------------------------------

    def pods(self, selector: dict[str, str] | None = None) -> list[Object]:
        return self.store.list(POD, selector)

    def available(self, selector: dict[str, str], min_ready_seconds: float = 0.0) -> int:
        return sum(1 for pod in self.store.list(POD, selector) if is_available(pod, min_ready_seconds))

    def placement(self, selector: dict[str, str] | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for pod in self.store.list(POD, selector):
            node = pod.spec.get("nodeName")
            if node:
                counts[node] = counts.get(node, 0) + 1
        return counts

    def wait(self, predicate, timeout: float = 10.0, interval: float = 0.01) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def wait_available(self, name: str, count: int, *, namespace: str = "default",
                       timeout: float = 10.0) -> bool:
        key = f"{DEPLOYMENT}/{namespace}/{name}"
        selector = self.store.get(key).spec.get("selector", {})
        return self.wait(lambda: self.available(selector) >= count, timeout=timeout)

    def wait_complete(self, name: str, *, namespace: str = "default", timeout: float = 10.0) -> bool:
        """Wait for a rollout of the *current* spec to finish.

        `status.complete` alone is not enough: immediately after a spec change
        it still describes the previous rollout, and a caller polling it would
        conclude the new one had finished before the controller had even looked.
        Requiring `observedGeneration == generation` is what closes that window.
        """
        key = f"{DEPLOYMENT}/{namespace}/{name}"

        def done() -> bool:
            try:
                deployment = self.store.get(key)
            except NotFoundError:
                return False
            status = deployment.status
            return (
                bool(status.get("complete"))
                and status.get("observedGeneration") == deployment.meta.generation
            )

        return self.wait(done, timeout=timeout)

    def summary(self) -> str:
        lines = []
        for deployment in self.store.list(DEPLOYMENT):
            status = deployment.status
            lines.append(
                f"{deployment.meta.name}: {status.get('availableReplicas', 0)}/"
                f"{deployment.spec.get('replicas', 0)} available, "
                f"revision {status.get('updatedRevision', '?')}, "
                f"{'complete' if status.get('complete') else 'rolling'}"
            )
        for rs in self.store.list(REPLICASET):
            lines.append(f"  rs {rs.meta.name}: spec={rs.spec.get('replicas', 0)} "
                         f"ready={rs.status.get('readyReplicas', 0)}")
        for node, count in sorted(self.placement().items()):
            lines.append(f"  {node}: {count} pods")
        return "\n".join(lines)
