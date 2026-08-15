"""The object model: what a declarative control plane actually stores.

Every object has the same three-part shape, and the split is the entire reason
declarative systems work:

    metadata   identity, and the bookkeeping that makes concurrency safe
    spec       what the user wants        -- written by users, never by controllers
    status     what is currently true     -- written by controllers, never by users

A controller's whole job is to notice `spec != status` and take one step toward
closing the gap. Nothing else. If a controller ever writes to `spec` it has
started arguing with the user, and the system stops converging.

Two metadata fields carry more weight than they look:

``resourceVersion`` changes on every write. A client that read version 7,
computed an update, and tries to write it back is rejected if the object is now
at version 8. Without that check two controllers acting on the same object
silently overwrite each other -- the classic lost update, and the reason
`ConflictError` exists here rather than a lock.

``generation`` increments only when `spec` changes. Controllers record the
generation they last acted on in `status.observedGeneration`, which is how
anyone can tell "the rollout is complete" from "the controller has not looked at
the new spec yet". They are indistinguishable otherwise, and the difference is
the whole of a rollout's progress reporting.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class ConflictError(Exception):
    """A write lost an optimistic-concurrency race. Re-read and retry."""


class NotFoundError(KeyError):
    pass


class AlreadyExistsError(Exception):
    pass


@dataclass
class Meta:
    name: str
    namespace: str = "default"
    uid: str = ""
    resource_version: int = 0
    generation: int = 1
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    owner: str = ""              # "kind/name" of the controller that created this
    created_at: float = 0.0
    deleted_at: float = 0.0

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "uid": self.uid,
            "resourceVersion": self.resource_version,
            "generation": self.generation,
            "labels": dict(self.labels),
            "annotations": dict(self.annotations),
            "owner": self.owner,
            "createdAt": self.created_at,
            "deletedAt": self.deleted_at,
        }

    @classmethod
    def from_json(cls, obj: dict) -> "Meta":
        return cls(
            name=obj["name"],
            namespace=obj.get("namespace", "default"),
            uid=obj.get("uid", ""),
            resource_version=int(obj.get("resourceVersion", 0)),
            generation=int(obj.get("generation", 1)),
            labels=dict(obj.get("labels", {})),
            annotations=dict(obj.get("annotations", {})),
            owner=obj.get("owner", ""),
            created_at=obj.get("createdAt", 0.0),
            deleted_at=obj.get("deletedAt", 0.0),
        )


@dataclass
class Object:
    kind: str
    meta: Meta
    spec: dict[str, Any] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind}/{self.meta.namespace}/{self.meta.name}"

    def copy(self) -> "Object":
        return Object(self.kind, Meta(**vars(self.meta)), copy.deepcopy(self.spec), copy.deepcopy(self.status))

    def to_json(self) -> dict:
        return {"kind": self.kind, "metadata": self.meta.to_json(), "spec": self.spec, "status": self.status}

    def serialise(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, obj: dict) -> "Object":
        return cls(
            kind=obj["kind"],
            meta=Meta.from_json(obj["metadata"]),
            spec=obj.get("spec", {}) or {},
            status=obj.get("status", {}) or {},
        )

    @classmethod
    def deserialise(cls, text: str) -> "Object":
        return cls.from_json(json.loads(text))

    def matches(self, selector: dict[str, str]) -> bool:
        """Label selection: every selector key must match. An empty selector matches all.

        Labels are the only way controllers find the objects they own. There are
        no pointers in the object graph -- a ReplicaSet does not hold a list of
        its Pods, it holds a query. That is what makes the system tolerant of a
        controller restarting with no memory of what it created.
        """
        return all(self.meta.labels.get(k) == v for k, v in selector.items())


def new_object(kind: str, name: str, spec: dict, *, namespace: str = "default",
               labels: dict[str, str] | None = None, owner: str = "") -> Object:
    return Object(
        kind=kind,
        meta=Meta(
            name=name,
            namespace=namespace,
            uid=uuid.uuid4().hex[:12],
            labels=dict(labels or {}),
            owner=owner,
            created_at=time.time(),
        ),
        spec=copy.deepcopy(spec),
        status={},
    )


# ---------------------------------------------------------------------------
# Well-known kinds
# ---------------------------------------------------------------------------

NODE = "Node"
POD = "Pod"
REPLICASET = "ReplicaSet"
DEPLOYMENT = "Deployment"

# Pod phases, in the order a healthy pod passes through them.
PENDING = "Pending"        # accepted, not yet placed
SCHEDULED = "Scheduled"    # placed on a node, not yet started
RUNNING = "Running"        # process is up
READY = "Ready"            # readiness probe passing -- only now does it get traffic
CRASHLOOP = "CrashLoopBackOff"   # exited; the node agent will retry, with backoff
SUCCEEDED = "Succeeded"
FAILED = "Failed"

# CrashLoopBackOff is deliberately NOT terminal. If a crashed pod were terminal,
# its ReplicaSet would see the replica count drop and create a replacement --
# which would also crash, forever. Restarting in place with backoff keeps the
# pod object stable, so the count stays right and the rollout simply stalls,
# which is the outcome a stuck deploy should have.
TERMINAL = (SUCCEEDED, FAILED)
# The distinction between RUNNING and READY is the one that matters during a
# rollout: counting running pods as available is how a rolling update takes an
# entire service down while every replica reports healthy.
AVAILABLE = (READY,)


def pod_spec(image: str, *, command: list[str] | None = None, cpu: float = 0.1,
             memory_mb: int = 64, env: dict[str, str] | None = None,
             readiness_delay_s: float = 0.0, fail: bool = False) -> dict:
    return {
        "image": image,
        "command": list(command or []),
        "resources": {"cpu": cpu, "memoryMB": memory_mb},
        "env": dict(env or {}),
        "readinessDelaySeconds": readiness_delay_s,
        "failOnStart": fail,
    }


def node_spec(cpu: float = 4.0, memory_mb: int = 8192, *, unschedulable: bool = False,
              labels: dict[str, str] | None = None) -> dict:
    return {
        "capacity": {"cpu": cpu, "memoryMB": memory_mb},
        "unschedulable": unschedulable,
        "labels": dict(labels or {}),
    }


def deployment_spec(replicas: int, template: dict, *, selector: dict[str, str] | None = None,
                    max_surge: int = 1, max_unavailable: int = 0,
                    min_ready_seconds: float = 0.0, progress_deadline_s: float = 30.0) -> dict:
    return {
        "replicas": replicas,
        "selector": dict(selector or {}),
        "template": copy.deepcopy(template),
        "strategy": {
            "maxSurge": max_surge,
            "maxUnavailable": max_unavailable,
            "minReadySeconds": min_ready_seconds,
            "progressDeadlineSeconds": progress_deadline_s,
        },
    }
