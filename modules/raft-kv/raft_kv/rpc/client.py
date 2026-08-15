"""
gRPC client helpers used by RaftNode to contact peers.

send_request_vote and send_append_entries are passed as callables into
RaftNode so the node itself has no import dependency on grpc.
"""

from __future__ import annotations

import grpc
import logging
from typing import Optional

from . import raft_pb2      as pb
from . import raft_pb2_grpc as rpc
from ..raft.log import LogEntry

log = logging.getLogger(__name__)

_TIMEOUT = 0.1   # per-RPC deadline in seconds


def _channel(addr: str) -> grpc.Channel:
    return grpc.insecure_channel(addr)


def make_send_rv(peer_addrs: dict[str, str]):
    """
    Return a send_request_vote callable that contacts peers over gRPC.

    peer_addrs: {peer_id: "host:port"}
    """
    def send_rv(
        peer_id: str,
        term: int,
        candidate_id: str,
        last_log_index: int,
        last_log_term: int,
    ) -> tuple[int, bool]:
        addr = peer_addrs[peer_id]
        with grpc.insecure_channel(addr) as ch:
            stub = rpc.RaftServiceStub(ch)
            resp = stub.RequestVote(
                pb.RequestVoteRequest(
                    term           = term,
                    candidate_id   = candidate_id,
                    last_log_index = last_log_index,
                    last_log_term  = last_log_term,
                ),
                timeout = _TIMEOUT,
            )
        return resp.term, resp.vote_granted
    return send_rv


def make_send_ae(peer_addrs: dict[str, str]):
    """
    Return a send_append_entries callable that contacts peers over gRPC.
    """
    def send_ae(
        peer_id: str,
        term: int,
        leader_id: str,
        prev_log_index: int,
        prev_log_term: int,
        entries: list[LogEntry],
        leader_commit: int,
    ) -> tuple[int, bool, int]:
        addr = peer_addrs[peer_id]
        proto_entries = [
            pb.LogEntry(term=e.term, index=e.index, command=e.command)
            for e in entries
        ]
        with grpc.insecure_channel(addr) as ch:
            stub = rpc.RaftServiceStub(ch)
            resp = stub.AppendEntries(
                pb.AppendEntriesRequest(
                    term           = term,
                    leader_id      = leader_id,
                    prev_log_index = prev_log_index,
                    prev_log_term  = prev_log_term,
                    entries        = proto_entries,
                    leader_commit  = leader_commit,
                ),
                timeout = _TIMEOUT,
            )
        return resp.term, resp.success, resp.match_index
    return send_ae


class KVClient:
    """
    Thin client for the KVService.

    Automatically follows leader hints so callers do not need to track the
    current leader themselves.
    """

    def __init__(self, addresses: list[str]) -> None:
        self._addrs   = list(addresses)
        self._current = 0

    def _stub(self) -> tuple[grpc.Channel, rpc.KVServiceStub]:
        ch   = grpc.insecure_channel(self._addrs[self._current])
        stub = rpc.KVServiceStub(ch)
        return ch, stub

    def get(self, key: str, timeout: float = 2.0) -> Optional[str]:
        for _ in range(len(self._addrs)):
            try:
                with grpc.insecure_channel(self._addrs[self._current]) as ch:
                    resp = rpc.KVServiceStub(ch).Get(
                        pb.GetRequest(key=key), timeout=timeout
                    )
                return resp.value if resp.found else None
            except grpc.RpcError:
                self._rotate()
        return None

    def set(self, key: str, value: str, timeout: float = 5.0) -> bool:
        for _ in range(len(self._addrs)):
            try:
                with grpc.insecure_channel(self._addrs[self._current]) as ch:
                    resp = rpc.KVServiceStub(ch).Set(
                        pb.SetRequest(key=key, value=value), timeout=timeout
                    )
                if resp.ok:
                    return True
                if resp.leader_hint:
                    self._follow_hint(resp.leader_hint)
                else:
                    self._rotate()
            except grpc.RpcError:
                self._rotate()
        return False

    def delete(self, key: str, timeout: float = 5.0) -> bool:
        for _ in range(len(self._addrs)):
            try:
                with grpc.insecure_channel(self._addrs[self._current]) as ch:
                    resp = rpc.KVServiceStub(ch).Del(
                        pb.DelRequest(key=key), timeout=timeout
                    )
                if resp.ok:
                    return True
                if resp.leader_hint:
                    self._follow_hint(resp.leader_hint)
                else:
                    self._rotate()
            except grpc.RpcError:
                self._rotate()
        return False

    def _rotate(self) -> None:
        self._current = (self._current + 1) % len(self._addrs)

    def _follow_hint(self, addr: str) -> None:
        if addr in self._addrs:
            self._current = self._addrs.index(addr)
