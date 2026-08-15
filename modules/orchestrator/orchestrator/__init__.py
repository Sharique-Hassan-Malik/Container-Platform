"""mini-orchestrator -- a Kubernetes-shaped control plane, from first principles.

    from orchestrator import ControlPlane, MemoryStore, deployment_spec, pod_spec

    plane = ControlPlane(MemoryStore()).with_nodes(3).start()
    plane.apply_deployment("serve", deployment_spec(
        replicas=4,
        template={"labels": {"app": "serve"}, "spec": pod_spec("serve:v1")},
        selector={"app": "serve"},
        max_surge=1, max_unavailable=0,
    ))
    plane.wait_available("serve", 4)

Declarative state, reconciliation loops, a scheduler, rolling updates and
rollback -- with cluster state in `raft-kv` instead of etcd, and pods executed
by `container-runtime`. Both are sibling modules in this repository and both
are optional: without them the control plane runs on an in-memory store and a
simulated runtime, which is what the tests use.
"""

from ._siblings import add_siblings, sibling_path

# Optional backends ship as sibling modules in this repository; make them
# importable before anything tries to load one.
add_siblings()

from .cluster import ControlPlane
from .controller import Controller, EdgeTriggeredController, WorkQueue
from .node_agent import NodeAgent, NodeMonitor
from .objects import (
    AVAILABLE,
    DEPLOYMENT,
    FAILED,
    NODE,
    POD,
    READY,
    REPLICASET,
    RUNNING,
    ConflictError,
    NotFoundError,
    Object,
    deployment_spec,
    new_object,
    node_spec,
    pod_spec,
)
from .runtime import ContainerRuntime, Handle, ProcessRuntime, SimulatedRuntime
from .scheduler import Scheduler
from .store import ADDED, DELETED, MODIFIED, ClusterStore, Event, MemoryStore, ObjectStateMachine, RaftStore
from .workloads import DeploymentController, ReplicaSetController, revisions, rollback

__version__ = "1.0.0"

__all__ = [
    "ControlPlane",
    "Controller", "EdgeTriggeredController", "WorkQueue",
    "NodeAgent", "NodeMonitor",
    "AVAILABLE", "DEPLOYMENT", "FAILED", "NODE", "POD", "READY", "REPLICASET", "RUNNING",
    "ConflictError", "NotFoundError", "Object",
    "deployment_spec", "new_object", "node_spec", "pod_spec",
    "ContainerRuntime", "Handle", "ProcessRuntime", "SimulatedRuntime",
    "Scheduler",
    "ADDED", "DELETED", "MODIFIED", "ClusterStore", "Event", "MemoryStore",
    "ObjectStateMachine", "RaftStore",
    "DeploymentController", "ReplicaSetController", "revisions", "rollback",
]
