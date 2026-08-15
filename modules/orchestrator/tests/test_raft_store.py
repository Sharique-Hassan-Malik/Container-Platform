"""Cluster state on `raft-kv` instead of etcd.

The sibling project supplies consensus; this supplies the state machine. The
compare-and-swap runs *inside* `apply`, after Raft has ordered the command, so
every replica reaches the same verdict. Checking the version before proposing
would be a read followed by a write with a window in between, and two clients
could pass the check and both commit.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from orchestrator import ConflictError, NotFoundError, ObjectStateMachine, RaftStore, new_object
from orchestrator.objects import AlreadyExistsError

RAFT_KV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "raft-kv",
)

try:
    # Installed distribution first; fall back to a colocated checkout.
    #   pip install "raft-kv @ git+https://github.com/Sharique-Hassan-Malik/\
    #   container-platform.git#subdirectory=modules/raft-kv"
    if importlib.util.find_spec("raft_kv") is None:
        sys.path.insert(0, RAFT_KV)
    import raft_kv as _raft_kv  # noqa: E402

    from raft_kv.raft.node import RaftNode  # noqa: E402
    from raft_kv.rpc.client import make_send_ae, make_send_rv  # noqa: E402
    from raft_kv.rpc.server import build_server  # noqa: E402

    RAFT_AVAILABLE = True
except Exception:  # noqa: BLE001
    RAFT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not RAFT_AVAILABLE,
    reason="raft-kv (and grpcio) not importable - it lives at "
           f"{RAFT_KV}; install grpcio to enable these tests",
)


def obj(name="a", spec=None):
    return new_object("Pod", name, spec or {"image": "x"})


# ---------------------------------------------------------------------------
# the state machine, without any networking
# ---------------------------------------------------------------------------


def test_state_machine_applies_create_update_delete():
    sm = ObjectStateMachine()
    pod = obj()
    sm.apply(f"CREATE r1 {pod.serialise()}")
    ok, payload = sm.result_for("r1")
    assert ok
    stored = type(pod).deserialise(payload)
    assert stored.meta.resource_version == 1

    stored.spec["image"] = "y"
    sm.apply(f"UPDATE r2 {stored.serialise()}")
    ok, payload = sm.result_for("r2")
    assert ok and type(pod).deserialise(payload).spec["image"] == "y"

    sm.apply(f"DELETE r3 {pod.key}")
    assert sm.result_for("r3")[0] is True
    assert sm.get(pod.key) is None


def test_state_machine_rejects_a_stale_update():
    sm = ObjectStateMachine()
    pod = obj()
    sm.apply(f"CREATE r1 {pod.serialise()}")
    stored = type(pod).deserialise(sm.result_for("r1")[1])

    winner = stored.copy()
    winner.spec["image"] = "winner"
    sm.apply(f"UPDATE r2 {winner.serialise()}")
    assert sm.result_for("r2")[0] is True

    loser = stored.copy()          # still holds the old resourceVersion
    loser.spec["image"] = "loser"
    sm.apply(f"UPDATE r3 {loser.serialise()}")
    ok, message = sm.result_for("r3")
    assert ok is False and "ConflictError" in message
    assert type(pod).deserialise(sm.get(pod.key)).spec["image"] == "winner"


def test_state_machine_is_deterministic_across_replicas():
    """Two replicas fed the same command log must reach identical state.

    This is the property Raft exists to provide and the one a state machine can
    silently break -- by depending on wall-clock time, iteration order, or
    anything else not in the log.
    """
    commands = []
    pod = obj()
    commands.append(f"CREATE c1 {pod.serialise()}")

    first = ObjectStateMachine()
    first.apply(commands[0])
    stored = type(pod).deserialise(first.result_for("c1")[1])
    stored.spec["image"] = "y"
    commands.append(f"UPDATE c2 {stored.serialise()}")
    commands.append(f"CREATE c3 {obj('b').serialise()}")
    for command in commands[1:]:
        first.apply(command)

    second = ObjectStateMachine()
    for command in commands:
        second.apply(command)

    assert first.snapshot() == second.snapshot()


def test_unparseable_commands_are_ignored_not_fatal():
    sm = ObjectStateMachine()
    sm.apply("garbage")
    sm.apply("CREATE only-two-fields")
    assert sm.snapshot() == {}


# ---------------------------------------------------------------------------
# a real three-node cluster
# ---------------------------------------------------------------------------


@pytest.fixture
def raft_cluster():
    """Three in-process nodes, each with its own ObjectStateMachine."""
    import time

    base = 21000 + (os.getpid() % 500) * 5
    members = {f"n{i}": f"127.0.0.1:{base + i}" for i in range(3)}
    machines: dict[str, ObjectStateMachine] = {}
    started = []

    for node_id, address in members.items():
        peers = {pid: paddr for pid, paddr in members.items() if pid != node_id}
        sm = ObjectStateMachine()
        machines[node_id] = sm
        node = RaftNode(
            node_id=node_id,
            peers=peers,
            apply_fn=lambda entry, s=sm: s.apply(entry.command),
            send_rv=make_send_rv(peers),
            send_ae=make_send_ae(peers),
            data_dir=None,
        )
        server = build_server(node, sm, peers, address)
        server.start()
        started.append(_raft_kv.ClusterMember(node_id=node_id, node=node, store=sm, server=server))

    leader = _raft_kv.wait_for_leader(started, timeout=10.0)
    if leader is None:
        for member in started:
            member.node.stop()
            member.server.stop(grace=0)
        pytest.skip("no leader elected within 10s")

    yield started, machines, leader
    for member in started:
        member.node.stop()
        member.server.stop(grace=0)


def test_writes_replicate_to_every_node(raft_cluster):
    import time

    started, machines, leader = raft_cluster
    store = RaftStore(leader, machines[leader.node_id])
    created = store.create(obj("replicated"))
    assert created.meta.resource_version >= 1

    deadline = time.time() + 5
    while time.time() < deadline:
        if all(sm.get(created.key) is not None for sm in machines.values()):
            break
        time.sleep(0.05)
    # Every replica applied the same command and holds the same object.
    assert all(sm.get(created.key) is not None for sm in machines.values())
    assert len({sm.get(created.key) for sm in machines.values()}) == 1


def test_conflicting_update_is_rejected_by_consensus(raft_cluster):
    started, machines, leader = raft_cluster
    store = RaftStore(leader, machines[leader.node_id])
    created = store.create(obj("cas"))

    winner = created.copy()
    winner.spec["image"] = "winner"
    store.update(winner)

    loser = created.copy()
    loser.spec["image"] = "loser"
    with pytest.raises(ConflictError):
        store.update(loser)
    assert store.get(created.key).spec["image"] == "winner"


def test_duplicate_create_and_missing_delete_raise(raft_cluster):
    started, machines, leader = raft_cluster
    store = RaftStore(leader, machines[leader.node_id])
    store.create(obj("dup"))
    with pytest.raises(AlreadyExistsError):
        store.create(obj("dup"))
    with pytest.raises(NotFoundError):
        store.delete("Pod/default/ghost")


def test_a_follower_refuses_writes(raft_cluster):
    started, machines, leader = raft_cluster
    follower = next(m for m in started if m.node_id != leader.node_id)
    store = RaftStore(follower, machines[follower.node_id])
    with pytest.raises(ConflictError, match="not the leader"):
        store.create(obj("from-follower"))


def test_update_with_retry_works_over_raft(raft_cluster):
    started, machines, leader = raft_cluster
    store = RaftStore(leader, machines[leader.node_id])
    created = store.create(new_object("Pod", "counter", {"n": 0}))
    for _ in range(5):
        store.update_with_retry(created.key, lambda c: (c.spec.__setitem__("n", c.spec["n"] + 1), True)[1])
    assert store.get(created.key).spec["n"] == 5
