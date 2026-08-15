"""
gRPC server hosting both RaftService (node-to-node) and KVService (client-facing).

The server is intentionally thin: it delegates every call to the RaftNode and
KVStore objects it receives at construction time. Transport and business logic
stay separated.
"""

from __future__ import annotations

import grpc
import logging
from concurrent import futures
from typing import Optional

from ..raft.node  import RaftNode, Role
from ..raft.log   import LogEntry
from ..store.kv   import KVStore
from . import raft_pb2       as pb
from . import raft_pb2_grpc  as rpc

log = logging.getLogger(__name__)


class RaftServicer(rpc.RaftServiceServicer):
    def __init__(self, node: RaftNode) -> None:
        self._node = node

    def RequestVote(self, request, context):
        term, granted = self._node.handle_request_vote(
            term           = request.term,
            candidate_id   = request.candidate_id,
            last_log_index = request.last_log_index,
            last_log_term  = request.last_log_term,
        )
        return pb.RequestVoteResponse(term=term, vote_granted=granted)

    def AppendEntries(self, request, context):
        entries = [
            LogEntry(term=e.term, index=e.index, command=e.command)
            for e in request.entries
        ]
        term, success, match_idx = self._node.handle_append_entries(
            term           = request.term,
            leader_id      = request.leader_id,
            prev_log_index = request.prev_log_index,
            prev_log_term  = request.prev_log_term,
            entries        = entries,
            leader_commit  = request.leader_commit,
        )
        return pb.AppendEntriesResponse(term=term, success=success, match_index=match_idx)


class KVServicer(rpc.KVServiceServicer):
    def __init__(self, node: RaftNode, store: KVStore, peers: dict[str, str]) -> None:
        self._node  = node
        self._store = store
        self._peers = peers   # peer_id → addr (for leader hint)

    def _leader_addr(self) -> str:
        lid = self._node.leader_id
        if lid == self._node.node_id:
            return ""
        return self._peers.get(lid or "", "")

    def Get(self, request, context):
        # Reads are served locally from the committed state machine.
        # For strict linearisability a leader-lease or ReadIndex RPC would be
        # required; this implementation uses committed-read semantics.
        value = self._store.get(request.key)
        return pb.GetResponse(
            value       = value or "",
            found       = value is not None,
            leader_hint = self._leader_addr(),
        )

    def Set(self, request, context):
        if not self._node.is_leader():
            return pb.SetResponse(ok=False, leader_hint=self._leader_addr(),
                                  error="not leader")
        ok, idx = self._node.propose(f"SET {request.key} {request.value}")
        if not ok:
            return pb.SetResponse(ok=False, leader_hint=self._leader_addr(),
                                  error="not leader")
        committed = self._node.wait_for_commit(idx)
        if not committed:
            return pb.SetResponse(ok=False, error="commit timeout")
        return pb.SetResponse(ok=True)

    def Del(self, request, context):
        if not self._node.is_leader():
            return pb.DelResponse(ok=False, leader_hint=self._leader_addr(),
                                  error="not leader")
        ok, idx = self._node.propose(f"DEL {request.key}")
        if not ok:
            return pb.DelResponse(ok=False, leader_hint=self._leader_addr(),
                                  error="not leader")
        committed = self._node.wait_for_commit(idx)
        if not committed:
            return pb.DelResponse(ok=False, error="commit timeout")
        return pb.DelResponse(ok=True)

    def Leader(self, request, context):
        lid = self._node.leader_id or ""
        addr = "" if lid == self._node.node_id else self._peers.get(lid, "")
        return pb.LeaderResponse(leader_id=lid, leader_addr=addr)


def build_server(
    node:    RaftNode,
    store:   KVStore,
    peers:   dict[str, str],
    address: str,
    workers: int = 4,
) -> grpc.Server:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers))
    rpc.add_RaftServiceServicer_to_server(RaftServicer(node),          server)
    rpc.add_KVServiceServicer_to_server(KVServicer(node, store, peers), server)
    server.add_insecure_port(address)
    return server
