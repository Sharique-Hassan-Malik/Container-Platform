"""Bringing up a control plane on real backends.

The orchestrator does not know how to start a Raft cluster and should not — its
job ends at the `ClusterStore` interface. Standing up three consensus nodes,
waiting for an election and handing the leader's state machine to a `RaftStore`
is composition work, so it lives here, in the platform, where the modules meet.

Everything is a context manager because every real backend owns something that
has to be released: gRPC servers, container workspaces, node agents.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from typing import Iterator

from . import backends
from .paths import add_modules

add_modules()


@dataclass
class Cluster:
    """A running control plane and the backends it was built on."""

    plane: object
    store_backend: backends.Backend
    runtime_backend: backends.Backend
    members: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def description(self) -> str:
        return (f"store={self.store_backend.name} "
                f"runtime={self.runtime_backend.name}")


@contextlib.contextmanager
def raft_cluster(size: int = 3, timeout: float = 10.0) -> Iterator[list]:
    """`size` in-process Raft nodes, each with its own object state machine.

    Ports are derived from the pid so two runs on one machine do not collide.
    """
    import raft_kv
    from orchestrator import ObjectStateMachine
    from raft_kv.raft.node import RaftNode
    from raft_kv.rpc.client import make_send_ae, make_send_rv
    from raft_kv.rpc.server import build_server

    base = 21000 + (os.getpid() % 500) * 5
    addresses = {f"n{i}": f"127.0.0.1:{base + i}" for i in range(size)}
    members = []

    try:
        for node_id, address in addresses.items():
            peers = {pid: addr for pid, addr in addresses.items() if pid != node_id}
            machine = ObjectStateMachine()
            node = RaftNode(
                node_id=node_id,
                peers=peers,
                apply_fn=lambda entry, m=machine: m.apply(entry.command),
                send_rv=make_send_rv(peers),
                send_ae=make_send_ae(peers),
                data_dir=None,
            )
            server = build_server(node, machine, peers, address)
            server.start()
            members.append(
                raft_kv.ClusterMember(node_id=node_id, node=node, store=machine, server=server)
            )

        leader = raft_kv.wait_for_leader(members, timeout=timeout)
        if leader is None:
            raise RuntimeError(f"no Raft leader elected within {timeout:g}s")
        yield members
    finally:
        for member in members:
            with contextlib.suppress(Exception):
                member.node.stop()
            with contextlib.suppress(Exception):
                member.server.stop(grace=0)


@contextlib.contextmanager
def start(
    *,
    store: str = "auto",
    runtime: str = "auto",
    nodes: int = 3,
    raft_size: int = 3,
    image_store: str = "./images",
    workspace: str = "./.state/containers",
) -> Iterator[Cluster]:
    """Start a control plane, falling back only when told to and saying so.

    `auto` picks the most capable backend this host supports. Naming a backend
    explicitly is a hard requirement: if it cannot run here you get an error,
    not a quiet downgrade to the simulator.
    """
    from orchestrator import ControlPlane

    notes: list[str] = []

    store_backend = backends.best("store") if store == "auto" else backends.get("store", store)
    runtime_backend = (
        backends.best("runtime") if runtime == "auto" else backends.get("runtime", runtime)
    )
    for chosen, requested in ((store_backend, store), (runtime_backend, runtime)):
        if requested == "auto":
            usable, reason = chosen.check()
            if not usable:
                notes.append(f"{chosen.seam}: fell back to {chosen.name} — {reason}")

    with contextlib.ExitStack() as stack:
        if store_backend.name == "raft":
            members = stack.enter_context(raft_cluster(size=raft_size))
            # raft_cluster does not return until an election has settled, so
            # the leader is known; writes go to it, reads to its local machine.
            import raft_kv

            leader = raft_kv.wait_for_leader(members, timeout=10.0)
            cluster_store = backends.build_store(
                "raft", member=leader, state_machine=leader.store
            )
        else:
            members = []
            cluster_store = backends.build_store("memory")

        pod_runtime = backends.build_runtime(
            runtime_backend.name, image_store=image_store, workspace=workspace
        )

        plane = ControlPlane(cluster_store, runtime=pod_runtime).with_nodes(nodes)
        plane.start()
        try:
            yield Cluster(
                plane=plane,
                store_backend=store_backend,
                runtime_backend=runtime_backend,
                members=members,
                notes=notes,
            )
        finally:
            with contextlib.suppress(Exception):
                plane.stop()
