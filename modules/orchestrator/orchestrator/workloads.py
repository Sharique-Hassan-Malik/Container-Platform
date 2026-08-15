"""ReplicaSet and Deployment: the two controllers that make rollouts work.

They are deliberately separate, and the reason is the whole trick.

A **ReplicaSet** owns one immutable pod template and one number. Its entire job
is "make the count of pods matching my selector equal my replica count". It
cannot roll anything out, because its template never changes.

A **Deployment** owns a sequence of ReplicaSets, one per revision, and rolls
between them by moving two numbers: scale the new one up, scale the old one
down, within the bounds `maxSurge` and `maxUnavailable`. It never touches a pod.

That separation is what makes rollback nearly free. The old ReplicaSet is still
there at zero replicas, still holding the exact template that worked, so
rolling back is scaling it up again -- not re-deriving an old configuration and
hoping it matches.

The other load-bearing detail is **readiness**, not liveness. A rolling update
that counts *running* pods as available will happily retire the last working
replica while every new one is still loading its weights. For a model server,
where readiness lags start by seconds, that turns a routine deploy into an
outage. Availability here means `Ready`, and `minReadySeconds` requires it to
have held.
"""

from __future__ import annotations

import time

from .controller import Controller
from .objects import (
    AVAILABLE,
    DEPLOYMENT,
    POD,
    READY,
    REPLICASET,
    TERMINAL,
    Object,
    new_object,
)
from .store import Event, NotFoundError

import hashlib
import json

REVISION_ANNOTATION = "orchestrator/revision"
TEMPLATE_ANNOTATION = "orchestrator/template"
TEMPLATE_HASH_LABEL = "orchestrator/template-hash"


def template_hash(template: dict) -> str:
    payload = json.dumps(template, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:10]


def is_available(pod: Object, min_ready_seconds: float = 0.0) -> bool:
    """Available means Ready, and Ready for long enough.

    `minReadySeconds` exists because a process can pass one readiness check and
    crash a second later. Without it a rollout advances on the strength of a
    single probe, and a crash-looping new version replaces a working old one at
    full speed.
    """
    if pod.status.get("phase") != READY:
        return False
    if min_ready_seconds <= 0:
        return True
    since = pod.status.get("readySince", 0.0)
    return bool(since) and (time.time() - since) >= min_ready_seconds


class ReplicaSetController(Controller):
    """Keeps the number of pods matching a selector equal to `spec.replicas`."""

    name = "replicaset"
    resync_period = 0.5

    def interesting(self, event: Event) -> list[str]:
        if event.object.kind == REPLICASET:
            return [event.key]
        if event.object.kind == POD and event.object.meta.owner:
            # A pod event is a hint about its owner, not about itself. The pod
            # has no desired state of its own to reconcile.
            return [event.object.meta.owner]
        return []

    def reconcile(self, key: str) -> float | None:
        try:
            rs = self.store.get(key)
        except NotFoundError:
            return None
        if rs.kind != REPLICASET:
            return None

        desired = int(rs.spec.get("replicas", 0))
        pods = [p for p in self.store.list(POD, rs.spec.get("selector", {})) if p.meta.owner == rs.key]
        alive = [p for p in pods if p.status.get("phase") not in TERMINAL]
        ready = [p for p in alive if p.status.get("phase") == READY]

        if len(alive) < desired:
            for _ in range(desired - len(alive)):
                self._create_pod(rs)
        elif len(alive) > desired:
            # Remove the least useful first: not-ready pods before ready ones,
            # newest before oldest. Deleting a ready pod while a Pending one
            # exists would reduce capacity to fix a count.
            surplus = sorted(
                alive,
                key=lambda p: (p.status.get("phase") == READY, p.meta.created_at),
            )[: len(alive) - desired]
            for pod in surplus:
                try:
                    self.store.delete(pod.key)
                except NotFoundError:
                    continue

        def write_status(candidate: Object) -> bool:
            status = {
                "replicas": len(alive),
                "readyReplicas": len(ready),
                "observedGeneration": candidate.meta.generation,
            }
            if candidate.status == status:
                return False
            candidate.status = status
            return True

        self.store.update_with_retry(key, write_status)
        return None

    def _create_pod(self, rs: Object) -> None:
        template = rs.spec.get("template", {})
        labels = {**rs.spec.get("selector", {}), **template.get("labels", {})}
        name = f"{rs.meta.name}-{int(time.time() * 1e6) % 1_000_000:06d}"
        pod = new_object(POD, name, template.get("spec", {}), namespace=rs.meta.namespace,
                         labels=labels, owner=rs.key)
        pod.status = {"phase": "Pending"}
        try:
            self.store.create(pod)
        except Exception:  # noqa: BLE001
            pass


class DeploymentController(Controller):
    """Rolls between ReplicaSets, one revision at a time."""

    name = "deployment"
    resync_period = 0.5

    def interesting(self, event: Event) -> list[str]:
        if event.object.kind == DEPLOYMENT:
            return [event.key]
        if event.object.kind == REPLICASET and event.object.meta.owner:
            return [event.object.meta.owner]
        return []

    def reconcile(self, key: str) -> float | None:
        try:
            deployment = self.store.get(key)
        except NotFoundError:
            return None
        if deployment.kind != DEPLOYMENT:
            return None

        strategy = deployment.spec.get("strategy", {})
        max_surge = int(strategy.get("maxSurge", 1))
        max_unavailable = int(strategy.get("maxUnavailable", 0))
        min_ready = float(strategy.get("minReadySeconds", 0.0))
        desired = int(deployment.spec.get("replicas", 0))
        selector = deployment.spec.get("selector", {})

        replica_sets = sorted(
            [rs for rs in self.store.list(REPLICASET) if rs.meta.owner == key],
            key=lambda rs: int(rs.meta.annotations.get(REVISION_ANNOTATION, 0)),
        )
        target = self._ensure_target(deployment, replica_sets, selector)
        replica_sets = sorted(
            [rs for rs in self.store.list(REPLICASET) if rs.meta.owner == key],
            key=lambda rs: int(rs.meta.annotations.get(REVISION_ANNOTATION, 0)),
        )
        old = [rs for rs in replica_sets if rs.meta.name != target.meta.name]

        pods = self.store.list(POD, selector)
        by_owner: dict[str, list[Object]] = {}
        for pod in pods:
            by_owner.setdefault(pod.meta.owner, []).append(pod)

        def available_for(rs: Object) -> int:
            return sum(1 for p in by_owner.get(rs.key, []) if is_available(p, min_ready))

        def alive_for(rs: Object) -> int:
            return sum(1 for p in by_owner.get(rs.key, []) if p.status.get("phase") not in TERMINAL)

        total_available = sum(available_for(rs) for rs in replica_sets)
        total_alive = sum(alive_for(rs) for rs in replica_sets)
        target_available = available_for(target)

        changed = False

        # 1. Scale up, but never beyond desired + maxSurge.
        #
        # Surge is accounted against replicas *requested* across all ReplicaSets,
        # not against pods observed. Pod creation is asynchronous, so counting
        # observed pods lets this pass run again before the previous scale-up
        # has materialised -- and grant the same headroom twice. Repeatedly.
        # maxSurge=1 then produces an unbounded number of pods, which is exactly
        # what it exists to prevent.
        total_requested = sum(int(rs.spec.get("replicas", 0)) for rs in replica_sets)
        room = (desired + max_surge) - total_requested
        target_spec = int(target.spec.get("replicas", 0))
        if room > 0 and target_spec < desired:
            step = min(room, desired - target_spec)
            changed |= self._scale(target.key, target_spec + step)

        # 2. Scale down old ReplicaSets, but never below desired - maxUnavailable
        #    *available* pods. Counting alive instead of available here is the
        #    bug that takes a service down during a routine deploy.
        floor = max(desired - max_unavailable, 0)
        # Availability is re-read here rather than reused from the snapshot
        # above, because step 1 has just written to the store and a stale count
        # authorises a removal the current state does not support.
        #
        # It is then clamped *per ReplicaSet*: a pod that is still Ready but
        # exceeds its own ReplicaSet's requested count has already been asked to
        # go, and counting it as headroom grants a second removal for the same
        # slot. Two such double-counts in a row is exactly how a rollout with
        # maxUnavailable=0 loses a replica.
        fresh = sorted(
            [rs for rs in self.store.list(REPLICASET) if rs.meta.owner == key],
            key=lambda rs: int(rs.meta.annotations.get(REVISION_ANNOTATION, 0)),
        )
        fresh_pods: dict[str, list[Object]] = {}
        for pod in self.store.list(POD, selector):
            fresh_pods.setdefault(pod.meta.owner, []).append(pod)

        def clamped_available(rs: Object) -> int:
            ready = sum(1 for p in fresh_pods.get(rs.key, []) if is_available(p, min_ready))
            return min(ready, int(rs.spec.get("replicas", 0)))

        surplus = sum(clamped_available(rs) for rs in fresh) - floor
        for rs in reversed([rs for rs in fresh if rs.meta.name != target.meta.name]):
            if surplus <= 0:
                break
            current = int(rs.spec.get("replicas", 0))
            if current == 0:
                continue
            step = min(current, surplus)
            changed |= self._scale(rs.key, current - step)
            surplus -= step

        # 3. Once the new ReplicaSet carries the whole load, retire the rest.
        if target_available >= desired:
            for rs in old:
                if int(rs.spec.get("replicas", 0)) != 0:
                    changed |= self._scale(rs.key, 0)

        # 4. Plain scale-down of a settled deployment. Step 1 only ever scales
        #    the target *up*, so lowering `replicas` on a Deployment that is
        #    already fully rolled out would otherwise change nothing at all.
        if target_spec > desired:
            changed |= self._scale(target.key, desired)

        # "Complete" requires the old pods to be *gone*, not merely scaled to
        # zero. Scaling is a request; the ReplicaSet controller acts on it a
        # moment later, and a caller told the rollout finished while old-version
        # pods are still serving traffic has been told something false.
        remaining_old = sum(
            1 for rs in old for pod in fresh_pods.get(rs.key, [])
            if pod.status.get("phase") not in TERMINAL
        )
        complete = (
            target_available >= desired
            and all(int(rs.spec.get("replicas", 0)) == 0 for rs in old)
            and remaining_old == 0
        )
        # The generation this verdict was computed against. If the spec has
        # moved on by the time the status write lands, the verdict is stale and
        # must not be published -- that is the whole reason observedGeneration
        # exists, and publishing `complete` from a previous generation is what
        # makes a caller believe a rollout finished before it started.
        self._write_status(deployment, target, total_available, total_alive, complete,
                           strategy, deployment.meta.generation)
        # Poll while a rollout is in flight; a readiness change may arrive with
        # no event of its own if a probe result is written by another process.
        return None if complete else 0.1

    # -- helpers -------------------------------------------------------------

    def _ensure_target(self, deployment: Object, replica_sets: list[Object], selector: dict) -> Object:
        """Find or create the ReplicaSet for the current template.

        Keyed by a hash of the template, so editing a Deployment back to a
        previous spec re-selects the *existing* ReplicaSet rather than creating
        a duplicate. That is what makes `rollback` and "edit it back by hand"
        behave identically.
        """
        template = deployment.spec.get("template", {})
        digest = template_hash(template)
        for rs in replica_sets:
            if rs.meta.labels.get(TEMPLATE_HASH_LABEL) == digest:
                return rs

        revision = max(
            [int(rs.meta.annotations.get(REVISION_ANNOTATION, 0)) for rs in replica_sets] or [0]
        ) + 1
        labels = {**selector, TEMPLATE_HASH_LABEL: digest}
        pod_labels = {**template.get("labels", {}), **labels}
        rs = new_object(
            REPLICASET,
            f"{deployment.meta.name}-{digest}",
            {
                "replicas": 0,
                "selector": labels,
                "template": {**template, "labels": pod_labels},
            },
            namespace=deployment.meta.namespace,
            labels=labels,
            owner=deployment.key,
        )
        rs.meta.annotations[REVISION_ANNOTATION] = str(revision)
        # Keep the Deployment's template verbatim. The copy stored in
        # `rs.spec.template` has selector and hash labels merged in, so it
        # hashes differently -- and a rollback built from it would create a new
        # revision instead of re-selecting this one.
        rs.meta.annotations[TEMPLATE_ANNOTATION] = json.dumps(template, sort_keys=True, separators=(",", ":"))
        try:
            return self.store.create(rs)
        except Exception:  # noqa: BLE001
            return self.store.get(rs.key)

    def _scale(self, key: str, replicas: int) -> bool:
        def mutate(rs: Object) -> bool:
            if int(rs.spec.get("replicas", 0)) == replicas:
                return False
            rs.spec["replicas"] = replicas
            return True

        before = self.store.get(key).spec.get("replicas")
        self.store.update_with_retry(key, mutate)
        return before != replicas

    def _write_status(self, deployment: Object, target: Object, available: int,
                      alive: int, complete: bool, strategy: dict, generation: int) -> None:
        deadline = float(strategy.get("progressDeadlineSeconds", 30.0))

        def mutate(candidate: Object) -> bool:
            if candidate.meta.generation != generation:
                # The spec changed while this pass was running. Drop the verdict
                # rather than attribute it to the new generation.
                return False
            previous = candidate.status.get("availableReplicas", -1)
            started = candidate.status.get("rolloutStartedAt") or time.time()
            progressed = candidate.status.get("lastProgressAt") or time.time()
            if available != previous:
                progressed = time.time()
            if complete:
                started, progressed = 0.0, 0.0

            status = {
                "replicas": alive,
                "availableReplicas": available,
                "updatedRevision": target.meta.annotations.get(REVISION_ANNOTATION, "0"),
                "observedGeneration": candidate.meta.generation,
                "complete": complete,
                "rolloutStartedAt": started,
                "lastProgressAt": progressed,
            }
            if not complete and progressed and (time.time() - progressed) > deadline:
                # Stalled, not failed: the deployment is reported as not
                # progressing and left alone. Automatic rollback belongs to a
                # policy layer, not to the controller that owns desired state.
                status["condition"] = "ProgressDeadlineExceeded"
            if candidate.status == status:
                return False
            candidate.status = status
            return True

        self.store.update_with_retry(deployment.key, mutate)


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------


def revisions(store, deployment_key: str) -> list[tuple[int, Object]]:
    """Every revision ever rolled out, oldest first. This is the rollback menu."""
    out = [
        (int(rs.meta.annotations.get(REVISION_ANNOTATION, 0)), rs)
        for rs in store.list(REPLICASET)
        if rs.meta.owner == deployment_key
    ]
    return sorted(out, key=lambda pair: pair[0])


def rollback(store, deployment_key: str, to_revision: int | None = None) -> int:
    """Point a Deployment's template back at a previous revision's ReplicaSet.

    Rollback is not a special code path: it rewrites `spec.template` and lets
    the ordinary rollout logic run. Because ReplicaSets are keyed by template
    hash, the old one is re-selected rather than recreated, so the rolled-back
    pods are identical to the ones that worked -- not a fresh interpretation of
    an old config.
    """
    history = revisions(store, deployment_key)
    if len(history) < 2:
        raise ValueError("nothing to roll back to")
    current_hash = template_hash(store.get(deployment_key).spec.get("template", {}))
    if to_revision is None:
        candidates = [
            (rev, rs) for rev, rs in history
            if rs.meta.labels.get(TEMPLATE_HASH_LABEL) != current_hash
        ]
        if not candidates:
            raise ValueError("nothing to roll back to")
        revision, target = candidates[-1]
    else:
        matches = [(rev, rs) for rev, rs in history if rev == to_revision]
        if not matches:
            raise ValueError(f"no revision {to_revision} (have {[r for r, _ in history]})")
        revision, target = matches[0]

    stored = target.meta.annotations.get(TEMPLATE_ANNOTATION)
    if not stored:
        raise ValueError(f"revision {revision} predates template recording and cannot be restored")
    template = json.loads(stored)

    def mutate(deployment: Object) -> bool:
        deployment.spec["template"] = template
        return True

    store.update_with_retry(deployment_key, mutate)
    return revision
