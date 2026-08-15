"""Cluster state: an object store with optimistic concurrency and watches.

This is the etcd-shaped hole in a control plane, and it needs exactly three
properties. Everything else in the system is built on them.

**Compare-and-swap on `resourceVersion`.** Controllers run concurrently and
routinely act on the same object. Without a version check, two controllers that
both read version 7 and both write back produce a silent lost update -- and the
symptom is a rollout that stalls for reasons no log explains. `update()` raises
`ConflictError` instead, and every caller retries by re-reading. That retry loop
is not a workaround; it is the concurrency model.

**A global, monotonic revision.** Every write bumps one counter shared by all
objects, so "everything that changed since revision N" is a single comparison.
Per-object versions cannot answer that question, which is why watches need it.

**Watches that can be resumed.** A watcher passes the revision it last saw and
receives everything after it. A watcher that reconnects and passes nothing is
told to resync from scratch rather than being handed a silent gap -- the gap
being the failure mode that makes an edge-triggered controller diverge forever.

Two backends, one interface: `MemoryStore` for a single-process control plane
and tests, `RaftStore` for a replicated one backed by the `raft-kv` project.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator

from .objects import AlreadyExistsError, ConflictError, NotFoundError, Object


@dataclass
class Event:
    type: str            # "ADDED" | "MODIFIED" | "DELETED"
    object: Object
    revision: int

    @property
    def key(self) -> str:
        return self.object.key


ADDED, MODIFIED, DELETED = "ADDED", "MODIFIED", "DELETED"


class ClusterStore:
    """Interface shared by the in-memory and Raft-backed implementations."""

    def create(self, obj: Object) -> Object: raise NotImplementedError
    def update(self, obj: Object) -> Object: raise NotImplementedError
    def delete(self, key: str) -> None: raise NotImplementedError
    def get(self, key: str) -> Object: raise NotImplementedError
    def list(self, kind: str = "", selector: dict[str, str] | None = None) -> list[Object]: raise NotImplementedError
    def revision(self) -> int: raise NotImplementedError
    def events_since(self, revision: int) -> list[Event]: raise NotImplementedError
    def watch(self, handler: Callable[[Event], None]) -> Callable[[], None]: raise NotImplementedError

    # -- shared helpers ------------------------------------------------------

    def update_with_retry(self, key: str, mutate: Callable[[Object], bool], attempts: int = 8) -> Object | None:
        """Read-modify-write until it sticks.

        `mutate` returns False to abandon the write. Retrying on conflict is the
        only correct response: the conflict means someone else's change landed
        first, so the mutation has to be recomputed against the new state rather
        than replayed against the old one.
        """
        for _ in range(attempts):
            try:
                current = self.get(key)
            except NotFoundError:
                return None
            candidate = current.copy()
            if not mutate(candidate):
                return current
            try:
                return self.update(candidate)
            except ConflictError:
                continue
        raise ConflictError(f"{key}: gave up after {attempts} attempts")


class MemoryStore(ClusterStore):
    def __init__(self, history: int = 4096):
        self._objects: dict[str, Object] = {}
        self._revision = 0
        self._history: list[Event] = []
        self._history_limit = history
        self._watchers: list[Callable[[Event], None]] = []
        self._lock = threading.RLock()

    # -- writes --------------------------------------------------------------

    def create(self, obj: Object) -> Object:
        with self._lock:
            if obj.key in self._objects:
                raise AlreadyExistsError(obj.key)
            stored = obj.copy()
            self._revision += 1
            stored.meta.resource_version = self._revision
            if not stored.meta.created_at:
                stored.meta.created_at = time.time()
            self._objects[stored.key] = stored
            self._emit(Event(ADDED, stored.copy(), self._revision))
            return stored.copy()

    def update(self, obj: Object) -> Object:
        with self._lock:
            current = self._objects.get(obj.key)
            if current is None:
                raise NotFoundError(obj.key)
            if obj.meta.resource_version != current.meta.resource_version:
                raise ConflictError(
                    f"{obj.key}: object has been modified "
                    f"(have {obj.meta.resource_version}, current {current.meta.resource_version})"
                )
            stored = obj.copy()
            # generation tracks spec changes only, so a status write never makes
            # a rollout look like it regressed.
            stored.meta.generation = current.meta.generation + (1 if obj.spec != current.spec else 0)
            self._revision += 1
            stored.meta.resource_version = self._revision
            self._objects[stored.key] = stored
            self._emit(Event(MODIFIED, stored.copy(), self._revision))
            return stored.copy()

    def delete(self, key: str) -> None:
        with self._lock:
            existing = self._objects.pop(key, None)
            if existing is None:
                raise NotFoundError(key)
            self._revision += 1
            existing.meta.deleted_at = time.time()
            self._emit(Event(DELETED, existing.copy(), self._revision))

    # -- reads ---------------------------------------------------------------

    def get(self, key: str) -> Object:
        with self._lock:
            obj = self._objects.get(key)
            if obj is None:
                raise NotFoundError(key)
            return obj.copy()

    def list(self, kind: str = "", selector: dict[str, str] | None = None) -> list[Object]:
        with self._lock:
            out = [
                obj.copy() for obj in self._objects.values()
                if (not kind or obj.kind == kind) and (selector is None or obj.matches(selector))
            ]
        return sorted(out, key=lambda o: o.key)

    def revision(self) -> int:
        with self._lock:
            return self._revision

    def events_since(self, revision: int) -> list[Event]:
        """Replay from a revision, or raise if history no longer reaches back.

        A silent partial answer here is the single most dangerous thing this
        store could do: the watcher would believe it is caught up while missing
        exactly the events it needed.
        """
        with self._lock:
            if not self._history:
                return []
            oldest = self._history[0].revision
            if revision < oldest - 1:
                raise ConflictError(
                    f"revision {revision} is older than the retained history ({oldest}); "
                    "the watcher must resync by listing"
                )
            return [event for event in self._history if event.revision > revision]

    # -- watches -------------------------------------------------------------

    def watch(self, handler: Callable[[Event], None]) -> Callable[[], None]:
        with self._lock:
            self._watchers.append(handler)

        def cancel() -> None:
            with self._lock:
                if handler in self._watchers:
                    self._watchers.remove(handler)

        return cancel

    def _emit(self, event: Event) -> None:
        self._history.append(event)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]
        for handler in list(self._watchers):
            try:
                handler(event)
            except Exception:  # noqa: BLE001
                # A broken watcher must not take down the writer. This is why
                # controllers are level-triggered: a dropped notification costs
                # latency, never correctness.
                continue


# ---------------------------------------------------------------------------
# Raft-backed
# ---------------------------------------------------------------------------


class ObjectStateMachine:
    """The replicated state machine applied by every Raft node.

    Duck-compatible with `raft_kv.store.KVStore` (`apply` + `get`) so it drops
    into that project's gRPC server unchanged, but it understands objects rather
    than strings, and -- critically -- performs the compare-and-swap *inside*
    `apply`.

    That placement is the whole point. Raft delivers commands to every replica
    in the same order, so a CAS evaluated during apply reaches the same verdict
    everywhere. Checking the version before proposing would be a read on the
    leader followed by a write, with a window in between: two clients could both
    pass the check and both commit.
    """

    def __init__(self):
        self._objects: dict[str, Object] = {}
        self._revision = 0
        self._history: list[Event] = []
        self._results: dict[str, tuple[bool, str]] = {}
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Event], None]] = []

    def apply(self, command: str) -> None:
        try:
            verb, request_id, payload = command.split(" ", 2)
        except ValueError:
            return
        with self._lock:
            try:
                if verb == "CREATE":
                    self._results[request_id] = self._do_create(Object.deserialise(payload))
                elif verb == "UPDATE":
                    self._results[request_id] = self._do_update(Object.deserialise(payload))
                elif verb == "DELETE":
                    self._results[request_id] = self._do_delete(payload)
            except Exception as exc:  # noqa: BLE001
                self._results[request_id] = (False, f"{type(exc).__name__}: {exc}")

    def _do_create(self, obj: Object) -> tuple[bool, str]:
        if obj.key in self._objects:
            return False, f"AlreadyExistsError: {obj.key}"
        self._revision += 1
        obj.meta.resource_version = self._revision
        self._objects[obj.key] = obj
        self._record(Event(ADDED, obj.copy(), self._revision))
        return True, obj.serialise()

    def _do_update(self, obj: Object) -> tuple[bool, str]:
        current = self._objects.get(obj.key)
        if current is None:
            return False, f"NotFoundError: {obj.key}"
        if obj.meta.resource_version != current.meta.resource_version:
            return False, (
                f"ConflictError: {obj.key}: object has been modified "
                f"(have {obj.meta.resource_version}, current {current.meta.resource_version})"
            )
        obj.meta.generation = current.meta.generation + (1 if obj.spec != current.spec else 0)
        self._revision += 1
        obj.meta.resource_version = self._revision
        self._objects[obj.key] = obj
        self._record(Event(MODIFIED, obj.copy(), self._revision))
        return True, obj.serialise()

    def _do_delete(self, key: str) -> tuple[bool, str]:
        existing = self._objects.pop(key, None)
        if existing is None:
            return False, f"NotFoundError: {key}"
        self._revision += 1
        self._record(Event(DELETED, existing.copy(), self._revision))
        return True, ""

    def _record(self, event: Event) -> None:
        self._history.append(event)
        if len(self._history) > 4096:
            del self._history[: len(self._history) - 4096]
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001
                continue

    # -- read side (KVStore-compatible) --------------------------------------

    def get(self, key: str) -> str | None:
        with self._lock:
            obj = self._objects.get(key)
            return obj.serialise() if obj else None

    def result_for(self, request_id: str) -> tuple[bool, str] | None:
        with self._lock:
            return self._results.get(request_id)

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {key: obj.serialise() for key, obj in self._objects.items()}

    def restore(self, snapshot: dict[str, str]) -> None:
        with self._lock:
            self._objects = {key: Object.deserialise(text) for key, text in snapshot.items()}


class RaftStore(ClusterStore):
    """Cluster state replicated by `raft-kv` instead of etcd.

    Reads are served locally from this node's applied state machine, which is
    the same trade every real control plane makes: a follower may lag the leader
    by one round trip. It is safe here precisely because controllers are
    level-triggered -- acting on state that is one revision stale produces a
    redundant action, then convergence, never divergence.
    """

    def __init__(self, member, state_machine: ObjectStateMachine, timeout: float = 5.0):
        self.member = member
        self.sm = state_machine
        self.timeout = timeout
        self._counter = 0
        self._lock = threading.Lock()

    def _propose(self, verb: str, payload: str) -> str:
        with self._lock:
            self._counter += 1
            request_id = f"{id(self):x}-{self._counter}"
        proposal = self.member.node.propose(f"{verb} {request_id} {payload}")
        ok, index = proposal if isinstance(proposal, tuple) else (bool(proposal), 0)
        if not ok:
            leader = self.member.node.leader_id   # a property in raft-kv, not a method
            raise ConflictError(f"not the leader (leader is {leader!r}); retry against it")
        if not self.member.node.wait_for_commit(index, timeout=self.timeout):
            raise TimeoutError(f"{verb} did not commit within {self.timeout}s")

        # Commit and apply are separate steps: the commit index advances when a
        # majority has the entry, and the state machine consumes it a moment
        # later on its own thread. The result only exists after apply, so
        # waiting for the commit is necessary but not sufficient.
        deadline = time.monotonic() + self.timeout
        result = self.sm.result_for(request_id)
        while result is None and time.monotonic() < deadline:
            time.sleep(0.002)
            result = self.sm.result_for(request_id)
        if result is None:
            raise TimeoutError(f"{verb} committed at index {index} but was never applied")
        succeeded, detail = result
        if not succeeded:
            kind, _, message = detail.partition(": ")
            raise {"ConflictError": ConflictError, "NotFoundError": NotFoundError,
                   "AlreadyExistsError": AlreadyExistsError}.get(kind, RuntimeError)(message or detail)
        return detail

    def create(self, obj: Object) -> Object:
        return Object.deserialise(self._propose("CREATE", obj.serialise()))

    def update(self, obj: Object) -> Object:
        return Object.deserialise(self._propose("UPDATE", obj.serialise()))

    def delete(self, key: str) -> None:
        self._propose("DELETE", key)

    def get(self, key: str) -> Object:
        text = self.sm.get(key)
        if text is None:
            raise NotFoundError(key)
        return Object.deserialise(text)

    def list(self, kind: str = "", selector: dict[str, str] | None = None) -> list[Object]:
        out = [Object.deserialise(text) for text in self.sm.snapshot().values()]
        out = [o for o in out if (not kind or o.kind == kind) and (selector is None or o.matches(selector))]
        return sorted(out, key=lambda o: o.key)

    def revision(self) -> int:
        return self.sm._revision

    def events_since(self, revision: int) -> list[Event]:
        return [event for event in list(self.sm._history) if event.revision > revision]

    def watch(self, handler: Callable[[Event], None]) -> Callable[[], None]:
        self.sm._listeners.append(handler)

        def cancel() -> None:
            if handler in self.sm._listeners:
                self.sm._listeners.remove(handler)

        return cancel
