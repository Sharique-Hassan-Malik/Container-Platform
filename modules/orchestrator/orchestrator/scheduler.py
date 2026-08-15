"""Placement: choosing a node for a pod that has none.

Two phases, and keeping them separate matters more than either algorithm.

**Filter** answers "can this pod run here at all?" -- capacity, readiness,
cordon, node selector. A node that fails any predicate is removed, not
penalised. Scoring an infeasible node produces a placement that fails at
startup, which costs a scheduling round trip *and* a container start.

**Score** answers "which of the feasible nodes is best?" Only reachable if the
answer cannot be wrong, so it is free to encode preferences that trade off
against each other.

The scheduler writes `spec.nodeName` and nothing else. It does not start the
pod, does not wait for it, and does not care whether it succeeds -- the node
agent owns that. This split is why a scheduler outage stops new placements
without touching anything already running.
"""

from __future__ import annotations

from dataclasses import dataclass

from .controller import Controller
from .objects import NODE, PENDING, POD, SCHEDULED, Object
from .store import Event, NotFoundError


@dataclass
class Fit:
    node: str
    feasible: bool
    score: float = 0.0
    reason: str = ""


def requested(pod: Object) -> tuple[float, float]:
    resources = pod.spec.get("resources", {})
    return float(resources.get("cpu", 0.0)), float(resources.get("memoryMB", 0))


def allocated(pods: list[Object], node_name: str) -> tuple[float, float]:
    """What is already committed on a node.

    Counts *assigned* pods, not running ones. A pod that has been bound but has
    not started yet still owns its resources -- ignoring it lets the scheduler
    place a second pod into the same space during the startup window, and both
    then fail.
    """
    cpu = memory = 0.0
    for pod in pods:
        if pod.spec.get("nodeName") != node_name:
            continue
        if pod.status.get("phase") in ("Succeeded", "Failed"):
            continue
        pod_cpu, pod_memory = requested(pod)
        cpu += pod_cpu
        memory += pod_memory
    return cpu, memory


class Scheduler(Controller):
    """Binds unscheduled pods to nodes.

    `spread=True` (the default) prefers the emptiest feasible node, which keeps
    a service's replicas apart so one node failure does not take all of them.
    `spread=False` packs onto the fullest node that still fits, which empties
    nodes so they can be scaled down. Neither is correct in general; the
    benchmark measures what each costs.
    """

    name = "scheduler"
    resync_period = 0.5

    def __init__(self, store, *, spread: bool = True, **kwargs):
        super().__init__(store, **kwargs)
        self.spread = spread
        self.bound = 0
        self.unschedulable: dict[str, str] = {}

    def interesting(self, event: Event) -> list[str]:
        if event.object.kind == POD:
            return [event.key]
        if event.object.kind == NODE:
            # A node appearing or freeing capacity can unblock pending pods, so
            # re-examine everything that is still waiting.
            return [pod.key for pod in self.store.list(POD) if not pod.spec.get("nodeName")]
        return []

    def reconcile(self, key: str) -> float | None:
        try:
            pod = self.store.get(key)
        except NotFoundError:
            return None
        if pod.kind != POD or pod.spec.get("nodeName"):
            return None
        if pod.status.get("phase") in ("Succeeded", "Failed"):
            return None

        fits = self.evaluate(pod)
        feasible = [fit for fit in fits if fit.feasible]
        if not feasible:
            reason = "; ".join(sorted({fit.reason for fit in fits})) or "no nodes registered"
            self.unschedulable[key] = reason
            self._set_condition(pod, reason)
            # Retry rather than give up: capacity is a moving target.
            return 0.5

        best = max(feasible, key=lambda fit: fit.score)
        self.unschedulable.pop(key, None)

        def bind(candidate: Object) -> bool:
            if candidate.spec.get("nodeName"):
                return False
            candidate.spec["nodeName"] = best.node
            candidate.status["phase"] = SCHEDULED
            candidate.status["conditions"] = {"scheduled": True}
            return True

        if self.store.update_with_retry(key, bind) is not None:
            self.bound += 1
        return None

    def evaluate(self, pod: Object) -> list[Fit]:
        nodes = self.store.list(NODE)
        pods = self.store.list(POD)
        pod_cpu, pod_memory = requested(pod)
        selector = pod.spec.get("nodeSelector", {}) or {}
        owner = pod.meta.owner

        out: list[Fit] = []
        for node in nodes:
            capacity = node.spec.get("capacity", {})
            node_cpu = float(capacity.get("cpu", 0.0))
            node_memory = float(capacity.get("memoryMB", 0))
            used_cpu, used_memory = allocated(pods, node.meta.name)

            if node.spec.get("unschedulable"):
                out.append(Fit(node.meta.name, False, reason="node is cordoned"))
                continue
            if not node.status.get("ready", True):
                out.append(Fit(node.meta.name, False, reason="node is not ready"))
                continue
            labels = {**node.spec.get("labels", {}), **node.meta.labels}
            if any(labels.get(k) != v for k, v in selector.items()):
                out.append(Fit(node.meta.name, False, reason="node selector does not match"))
                continue
            if used_cpu + pod_cpu > node_cpu:
                out.append(Fit(node.meta.name, False, reason="insufficient cpu"))
                continue
            if used_memory + pod_memory > node_memory:
                out.append(Fit(node.meta.name, False, reason="insufficient memory"))
                continue

            free_cpu = (node_cpu - used_cpu - pod_cpu) / node_cpu if node_cpu else 0.0
            free_memory = (node_memory - used_memory - pod_memory) / node_memory if node_memory else 0.0
            balance = (free_cpu + free_memory) / 2
            score = balance if self.spread else 1.0 - balance

            # Anti-affinity: strongly prefer a node not already running a
            # sibling. Without this, "spread" only balances resources and a
            # three-replica service whose pods are small can still land entirely
            # on one node. It applies only in spread mode -- under bin-packing
            # it would fight the objective it exists to serve.
            if owner and self.spread:
                siblings = sum(
                    1 for other in pods
                    if other.meta.owner == owner and other.spec.get("nodeName") == node.meta.name
                    and other.meta.name != pod.meta.name
                )
                score -= siblings * 1.5
            out.append(Fit(node.meta.name, True, score=score))
        return out

    def _set_condition(self, pod: Object, reason: str) -> None:
        def mark(candidate: Object) -> bool:
            if candidate.status.get("unschedulableReason") == reason:
                return False
            candidate.status["phase"] = PENDING
            candidate.status["unschedulableReason"] = reason
            return True

        try:
            self.store.update_with_retry(pod.key, mark)
        except Exception:  # noqa: BLE001
            pass
