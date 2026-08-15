"""
raft_kv — Raft-based distributed key-value store.

Entry point for bootstrapping a local cluster (used in scripts and tests):

    from raft_kv import bootstrap_cluster, teardown_cluster
    nodes = bootstrap_cluster({"n1": "127.0.0.1:15001",
                               "n2": "127.0.0.1:15002",
                               "n3": "127.0.0.1:15003"})
    # ... interact with nodes ...
    teardown_cluster(nodes)
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import grpc

from .raft.node  import RaftNode
from .store.kv   import KVStore
from .rpc.client import make_send_rv, make_send_ae
from .rpc.server import build_server


@dataclass
class ClusterMember:
    node_id:  str
    node:     RaftNode
    store:    KVStore
    server:   object   # grpc.Server


def bootstrap_cluster(
    members: dict[str, str],
    data_dir: str | None = None,
) -> list[ClusterMember]:
    """
    Start a local in-process cluster.

    Parameters
    ----------
    members:  {node_id: "host:port"} for every node in the cluster.
    data_dir: Optional directory for durable state (disabled if None).

    Returns a list of ClusterMember objects, one per node.
    """
    result = []
    for node_id, addr in members.items():
        peers = {pid: paddr for pid, paddr in members.items() if pid != node_id}
        store = KVStore()
        node  = RaftNode(
            node_id  = node_id,
            peers    = peers,
            apply_fn = lambda entry, s=store: s.apply(entry.command),
            send_rv  = make_send_rv(peers),
            send_ae  = make_send_ae(peers),
            data_dir = data_dir,
        )
        server = build_server(node, store, peers, addr)
        server.start()
        result.append(ClusterMember(node_id=node_id, node=node, store=store, server=server))
    return result


def wait_for_leader(
    members: list[ClusterMember],
    timeout: float = 5.0,
) -> ClusterMember | None:
    """Block until one member reports it is the leader."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for m in members:
            if m.node.is_leader():
                return m
        time.sleep(0.05)
    return None


def teardown_cluster(members: list[ClusterMember]) -> None:
    for m in members:
        m.node.stop()
        m.server.stop(grace=0)
