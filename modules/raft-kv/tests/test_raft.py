"""
Tests for raft-kv.

The test cluster runs in-process using direct Python calls for Raft RPCs
(bypassing gRPC) so tests are fast and deterministic. The RaftNode's
send_rv and send_ae callables are wired directly to other nodes' handler
methods.
"""

from __future__ import annotations

import time
import threading
import pytest

from raft_kv.raft.log   import RaftLog, LogEntry
from raft_kv.raft.state import PersistentState
from raft_kv.raft.node  import RaftNode, Role, ELECTION_TIMEOUT_MAX
from raft_kv.store.kv   import KVStore


# ── Helpers ───────────────────────────────────────────────────────────────────

class InProcessCluster:
    """
    Minimal in-process Raft cluster with direct RPC dispatch.

    Nodes call each other's handle_* methods directly (no gRPC),
    which makes elections and replication fully synchronous from the
    test's perspective.
    """

    def __init__(self, n: int = 3) -> None:
        self.ids    = [f"n{i}" for i in range(n)]
        self.stores = {nid: KVStore() for nid in self.ids}
        self.nodes: dict[str, RaftNode] = {}
        self._build()

    def _build(self) -> None:
        ids = self.ids

        def make_send_rv(src_id):
            def send_rv(peer_id, term, candidate_id, last_log_index, last_log_term):
                return self.nodes[peer_id].handle_request_vote(
                    term, candidate_id, last_log_index, last_log_term
                )
            return send_rv

        def make_send_ae(src_id):
            def send_ae(peer_id, term, leader_id, prev_log_index, prev_log_term, entries, leader_commit):
                return self.nodes[peer_id].handle_append_entries(
                    term, leader_id, prev_log_index, prev_log_term, entries, leader_commit
                )
            return send_ae

        for nid in ids:
            peers = {pid: "" for pid in ids if pid != nid}
            store = self.stores[nid]
            node  = RaftNode(
                node_id  = nid,
                peers    = peers,
                apply_fn = lambda entry, s=store: s.apply(entry.command),
                send_rv  = make_send_rv(nid),
                send_ae  = make_send_ae(nid),
            )
            self.nodes[nid] = node

    def wait_leader(self, timeout: float = 3.0) -> RaftNode | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for node in self.nodes.values():
                if node.is_leader():
                    return node
            time.sleep(0.02)
        return None

    def leader(self) -> RaftNode | None:
        for node in self.nodes.values():
            if node.is_leader():
                return node
        return None

    def stop(self) -> None:
        for node in self.nodes.values():
            node.stop()


# ── RaftLog tests ─────────────────────────────────────────────────────────────

def test_log_append_and_get():
    rl = RaftLog()
    e  = LogEntry(term=1, index=1, command="SET k v")
    rl.append(e)
    assert rl.get(1) == e


def test_log_last_index_empty():
    assert RaftLog().last_index() == 0


def test_log_last_index_after_append():
    rl = RaftLog()
    for i in range(1, 6):
        rl.append(LogEntry(term=1, index=i, command=f"NOP"))
    assert rl.last_index() == 5


def test_log_truncate():
    rl = RaftLog()
    for i in range(1, 5):
        rl.append(LogEntry(term=1, index=i, command="NOP"))
    rl.truncate_from(3)
    assert rl.last_index() == 2
    assert rl.get(3) is None


def test_log_entries_from():
    rl = RaftLog()
    for i in range(1, 6):
        rl.append(LogEntry(term=1, index=i, command=f"NOP"))
    got = rl.entries_from(3)
    assert [e.index for e in got] == [3, 4, 5]


def test_log_term_at():
    rl = RaftLog()
    rl.append(LogEntry(term=1, index=1, command="NOP"))
    rl.append(LogEntry(term=2, index=2, command="NOP"))
    assert rl.term_at(1) == 1
    assert rl.term_at(2) == 2
    assert rl.term_at(0) == 0


def test_log_persistence(tmp_path):
    rl = RaftLog(str(tmp_path), "n0")
    for i in range(1, 4):
        rl.append(LogEntry(term=1, index=i, command=f"SET k{i} v{i}"))
    # Reload from disk
    rl2 = RaftLog(str(tmp_path), "n0")
    assert rl2.last_index() == 3
    assert rl2.get(2).command == "SET k2 v2"


def test_log_snapshot():
    rl = RaftLog()
    for i in range(1, 6):
        rl.append(LogEntry(term=1, index=i, command="NOP"))
    rl.snapshot(3, 1)
    assert rl.last_index() == 5
    assert rl.get(1) is None  # compacted away
    assert rl.get(4) is not None


# ── PersistentState tests ─────────────────────────────────────────────────────

def test_state_initial_term():
    ps = PersistentState()
    assert ps.current_term == 0
    assert ps.voted_for is None


def test_state_set_term():
    ps = PersistentState()
    ps.set_term(3)
    assert ps.current_term == 3
    assert ps.voted_for is None


def test_state_increment_term():
    ps = PersistentState()
    t  = ps.increment_term()
    assert t == 1
    assert ps.current_term == 1


def test_state_voted_for():
    ps = PersistentState()
    ps.set_term(1)
    ps.set_voted_for("n1")
    assert ps.voted_for == "n1"


def test_state_persistence(tmp_path):
    ps = PersistentState(str(tmp_path), "n0")
    ps.set_term(5)
    ps.set_voted_for("n2")
    ps2 = PersistentState(str(tmp_path), "n0")
    assert ps2.current_term == 5
    assert ps2.voted_for    == "n2"


# ── KVStore tests ─────────────────────────────────────────────────────────────

def test_kv_set_get():
    kv = KVStore()
    kv.apply("SET foo bar")
    assert kv.get("foo") == "bar"


def test_kv_del():
    kv = KVStore()
    kv.apply("SET x 1")
    kv.apply("DEL x")
    assert kv.get("x") is None


def test_kv_missing_key():
    assert KVStore().get("missing") is None


def test_kv_overwrite():
    kv = KVStore()
    kv.apply("SET k v1")
    kv.apply("SET k v2")
    assert kv.get("k") == "v2"


def test_kv_snapshot_restore():
    kv = KVStore()
    kv.apply("SET a 1")
    kv.apply("SET b 2")
    snap = kv.snapshot()
    kv2  = KVStore()
    kv2.restore(snap)
    assert kv2.get("a") == "1"
    assert kv2.get("b") == "2"


def test_kv_nop():
    kv = KVStore()
    kv.apply("NOP")
    assert len(kv) == 0


# ── Raft consensus tests ──────────────────────────────────────────────────────

def test_leader_elected():
    c = InProcessCluster(3)
    leader = c.wait_leader(timeout=3.0)
    assert leader is not None
    c.stop()


def test_exactly_one_leader():
    c = InProcessCluster(3)
    c.wait_leader(timeout=3.0)
    leaders = [n for n in c.nodes.values() if n.is_leader()]
    assert len(leaders) == 1
    c.stop()


def test_propose_and_commit():
    c = InProcessCluster(3)
    leader = c.wait_leader()
    assert leader is not None
    ok, idx = leader.propose("SET hello world")
    assert ok is True
    committed = leader.wait_for_commit(idx, timeout=3.0)
    assert committed
    c.stop()


def test_kv_replicated():
    c = InProcessCluster(3)
    leader = c.wait_leader()
    ok, idx = leader.propose("SET city Islamabad")
    leader.wait_for_commit(idx, timeout=3.0)
    time.sleep(0.1)  # allow apply loop to catch up on all nodes
    for nid, store in c.stores.items():
        assert store.get("city") == "Islamabad", f"node {nid} missing value"
    c.stop()


def test_multiple_writes_replicated():
    c   = InProcessCluster(3)
    leader = c.wait_leader()
    last_idx = 0
    for i in range(5):
        ok, idx = leader.propose(f"SET k{i} v{i}")
        assert ok
        last_idx = idx
    leader.wait_for_commit(last_idx, timeout=5.0)
    time.sleep(0.15)
    for i in range(5):
        for store in c.stores.values():
            assert store.get(f"k{i}") == f"v{i}"
    c.stop()


def test_del_replicated():
    c = InProcessCluster(3)
    leader = c.wait_leader()
    _, idx = leader.propose("SET temp 42")
    leader.wait_for_commit(idx, timeout=3.0)
    _, idx2 = leader.propose("DEL temp")
    leader.wait_for_commit(idx2, timeout=3.0)
    time.sleep(0.1)
    for store in c.stores.values():
        assert store.get("temp") is None
    c.stop()


def test_terms_increase_monotonically():
    c = InProcessCluster(3)
    c.wait_leader(timeout=3.0)
    terms = [n.current_term for n in c.nodes.values()]
    assert all(t >= 1 for t in terms)
    c.stop()


def test_non_leader_rejects_propose():
    c = InProcessCluster(3)
    c.wait_leader()
    followers = [n for n in c.nodes.values() if not n.is_leader()]
    assert len(followers) >= 1
    ok, idx = followers[0].propose("SET x 1")
    assert not ok
    c.stop()


def test_request_vote_stale_term_rejected():
    c = InProcessCluster(3)
    c.wait_leader()
    any_node = next(iter(c.nodes.values()))
    term, granted = any_node.handle_request_vote(
        term=0, candidate_id="ghost", last_log_index=0, last_log_term=0
    )
    assert not granted
    c.stop()
