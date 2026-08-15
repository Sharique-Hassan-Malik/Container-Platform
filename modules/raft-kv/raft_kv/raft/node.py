"""
Raft consensus node.

Implements:
  - Leader election with randomised timeouts (§5.2)
  - Log replication with consistency check (§5.3)
  - Commit and apply via commit index advancement (§5.3)
  - Term enforcement: any RPC with higher term converts node to follower (§5.1)

References:
    Ongaro, D. and Ousterhout, J. (2014). In Search of an Understandable
    Consensus Algorithm. USENIX ATC.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from enum import Enum, auto
from typing import Callable, Optional

from .log   import RaftLog, LogEntry
from .state import PersistentState

log = logging.getLogger(__name__)

ELECTION_TIMEOUT_MIN = 0.15
ELECTION_TIMEOUT_MAX = 0.30
HEARTBEAT_INTERVAL   = 0.04


class Role(Enum):
    FOLLOWER  = auto()
    CANDIDATE = auto()
    LEADER    = auto()


class RaftNode:
    """
    Single Raft consensus participant.

    Parameters
    ----------
    node_id:  Unique string identifier for this node.
    peers:    Dict {peer_id: peer_addr} for all other nodes.
    apply_fn: Callback invoked with each committed LogEntry in order.
    send_rv:  Callable(peer_id, term, candidate_id, last_log_index, last_log_term)
              → (term, vote_granted)
    send_ae:  Callable(peer_id, term, leader_id, prev_log_index, prev_log_term,
                       entries, leader_commit)
              → (term, success, match_index)
    data_dir: Optional path for persistent state and log.
    """

    def __init__(
        self,
        node_id:  str,
        peers:    dict[str, str],
        apply_fn: Callable[[LogEntry], None],
        send_rv:  Callable,
        send_ae:  Callable,
        data_dir: Optional[str] = None,
    ) -> None:
        self.node_id  = node_id
        self.peers    = peers
        self.apply_fn = apply_fn
        self._send_rv = send_rv
        self._send_ae = send_ae

        self._ps  = PersistentState(data_dir, node_id)
        self._log = RaftLog(data_dir, node_id)

        self._lock           = threading.Lock()
        self._role:          Role          = Role.FOLLOWER
        self._leader_id:     Optional[str] = None
        self._commit_index:  int           = 0
        self._last_applied:  int           = 0

        self._next_index:  dict[str, int] = {}
        self._match_index: dict[str, int] = {}

        self._last_contact   = time.monotonic()
        self._election_timeout = self._new_timeout()

        self._stop_event = threading.Event()

        # Single background thread drives election timer, heartbeats and apply.
        self._ticker = threading.Thread(target=self._loop, daemon=True)
        self._ticker.start()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def role(self) -> Role:
        with self._lock:
            return self._role

    @property
    def leader_id(self) -> Optional[str]:
        with self._lock:
            return self._leader_id

    @property
    def current_term(self) -> int:
        return self._ps.current_term

    @property
    def commit_index(self) -> int:
        with self._lock:
            return self._commit_index

    def is_leader(self) -> bool:
        return self.role == Role.LEADER

    def propose(self, command: str) -> tuple[bool, int]:
        with self._lock:
            if self._role != Role.LEADER:
                return False, 0
            index = self._log.last_index() + 1
            entry = LogEntry(term=self._ps.current_term, index=index, command=command)
            self._log.append(entry)
            self._match_index[self.node_id] = index
            return True, index

    def wait_for_commit(self, index: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._commit_index >= index:
                    return True
            time.sleep(0.01)
        return False

    def stop(self) -> None:
        self._stop_event.set()

    # ── RPC handlers ──────────────────────────────────────────────────────────

    def handle_request_vote(
        self,
        term: int,
        candidate_id: str,
        last_log_index: int,
        last_log_term: int,
    ) -> tuple[int, bool]:
        with self._lock:
            self._check_term(term)
            my_term = self._ps.current_term

            if term < my_term:
                return my_term, False

            vf = self._ps.voted_for
            if vf is not None and vf != candidate_id:
                return my_term, False

            log_ok = (
                last_log_term > self._log.last_term()
                or (last_log_term == self._log.last_term()
                    and last_log_index >= self._log.last_index())
            )
            if not log_ok:
                return my_term, False

            self._ps.set_voted_for(candidate_id)
            self._reset_contact()
            return my_term, True

    def handle_append_entries(
        self,
        term: int,
        leader_id: str,
        prev_log_index: int,
        prev_log_term: int,
        entries: list[LogEntry],
        leader_commit: int,
    ) -> tuple[int, bool, int]:
        with self._lock:
            self._check_term(term)
            my_term = self._ps.current_term

            if term < my_term:
                return my_term, False, 0

            self._leader_id = leader_id
            self._reset_contact()
            if self._role == Role.CANDIDATE:
                self._role = Role.FOLLOWER

            # Consistency check
            if prev_log_index > 0:
                if self._log.last_index() < prev_log_index:
                    return my_term, False, self._log.last_index()
                if self._log.term_at(prev_log_index) != prev_log_term:
                    self._log.truncate_from(prev_log_index)
                    return my_term, False, prev_log_index - 1

            if entries:
                first_new = prev_log_index + 1
                for i, entry in enumerate(entries):
                    stored = self._log.get(first_new + i)
                    if stored is None:
                        self._log.append_all(entries[i:])
                        break
                    if stored.term != entry.term:
                        self._log.truncate_from(first_new + i)
                        self._log.append_all(entries[i:])
                        break

            if leader_commit > self._commit_index:
                self._commit_index = min(leader_commit, self._log.last_index())

            return my_term, True, self._log.last_index()

    # ── Background loop ───────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._tick()
            self._apply_committed()
            time.sleep(0.01)

    def _tick(self) -> None:
        with self._lock:
            role = self._role
            elapsed = time.monotonic() - self._last_contact
            timeout = self._election_timeout

        if role == Role.LEADER:
            elapsed_hb = time.monotonic() - self._last_contact
            if elapsed_hb >= HEARTBEAT_INTERVAL:
                self._replicate()
        else:
            if elapsed >= timeout:
                self._start_election()

    # ── Election ──────────────────────────────────────────────────────────────

    def _start_election(self) -> None:
        with self._lock:
            self._role = Role.CANDIDATE
            new_term   = self._ps.increment_term()
            self._ps.set_voted_for(self.node_id)
            self._leader_id = None
            self._reset_contact()
            last_log_index = self._log.last_index()
            last_log_term  = self._log.last_term()
            peers = dict(self.peers)

        votes  = 1
        quorum = (len(peers) + 2) // 2

        for peer_id in peers:
            try:
                r_term, granted = self._send_rv(
                    peer_id, new_term, self.node_id, last_log_index, last_log_term
                )
            except Exception:
                continue
            with self._lock:
                if self._ps.current_term != new_term or self._role != Role.CANDIDATE:
                    return
                if r_term > new_term:
                    self._step_down(r_term)
                    return
            if granted:
                votes += 1
                if votes >= quorum:
                    self._become_leader(new_term)
                    return

    def _become_leader(self, term: int) -> None:
        with self._lock:
            if self._ps.current_term != term or self._role != Role.CANDIDATE:
                return
            self._role      = Role.LEADER
            self._leader_id = self.node_id
            next_idx = self._log.last_index() + 1
            for pid in self.peers:
                self._next_index[pid]  = next_idx
                self._match_index[pid] = 0
            self._match_index[self.node_id] = self._log.last_index()
            self._reset_contact()
            log.info("%s became leader term=%d", self.node_id, term)
        # Immediately replicate to assert leadership
        self._replicate()

    def _step_down(self, term: int) -> None:
        """Must be called with self._lock held."""
        self._ps.set_term(term)
        self._role = Role.FOLLOWER
        self._reset_contact()

    def _check_term(self, term: int) -> None:
        """Step down if we see a higher term. Must be called with lock held."""
        if term > self._ps.current_term:
            self._step_down(term)

    def _reset_contact(self) -> None:
        """Restart election timer. Must be called with lock held (or just after)."""
        self._last_contact      = time.monotonic()
        self._election_timeout  = self._new_timeout()

    @staticmethod
    def _new_timeout() -> float:
        return random.uniform(ELECTION_TIMEOUT_MIN, ELECTION_TIMEOUT_MAX)

    # ── Replication ───────────────────────────────────────────────────────────

    def _replicate(self) -> None:
        """Send AppendEntries to all peers. Called without holding _lock."""
        with self._lock:
            if self._role != Role.LEADER:
                return
            term     = self._ps.current_term
            peers    = dict(self.peers)
            next_idx = dict(self._next_index)
            commit   = self._commit_index
            self._last_contact = time.monotonic()

        for peer_id in peers:
            ni        = next_idx.get(peer_id, 1)
            prev_idx  = ni - 1
            prev_term = self._log.term_at(prev_idx)
            entries   = self._log.entries_from(ni)
            try:
                r_term, success, match_idx = self._send_ae(
                    peer_id, term, self.node_id,
                    prev_idx, prev_term, entries, commit,
                )
            except Exception:
                continue

            with self._lock:
                if r_term > self._ps.current_term:
                    self._step_down(r_term)
                    return
                if self._role != Role.LEADER:
                    return
                if success:
                    self._match_index[peer_id] = match_idx
                    self._next_index[peer_id]  = match_idx + 1
                    self._match_index[self.node_id] = self._log.last_index()
                    self._advance_commit(term)
                else:
                    self._next_index[peer_id] = max(1, match_idx + 1)

    def _advance_commit(self, term: int) -> None:
        """Advance commit_index to the highest index on a quorum. Lock held."""
        n      = self._log.last_index()
        quorum = (len(self.peers) + 2) // 2
        while n > self._commit_index:
            if self._log.term_at(n) == term:
                replicated = sum(
                    1 for mid in self._match_index.values() if mid >= n
                )
                if replicated >= quorum:
                    self._commit_index = n
                    break
            n -= 1

    # ── Apply ─────────────────────────────────────────────────────────────────

    def _apply_committed(self) -> None:
        with self._lock:
            ci = self._commit_index
            la = self._last_applied
        for idx in range(la + 1, ci + 1):
            entry = self._log.get(idx)
            if entry:
                try:
                    self.apply_fn(entry)
                except Exception as exc:
                    log.error("apply error index=%d: %s", idx, exc)
        with self._lock:
            if self._commit_index > self._last_applied:
                self._last_applied = self._commit_index
