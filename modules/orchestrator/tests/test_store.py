"""The store: optimistic concurrency, watches, and the work queue."""

from __future__ import annotations

import threading

import pytest

from orchestrator import ConflictError, MemoryStore, NotFoundError, WorkQueue, new_object
from orchestrator.objects import AlreadyExistsError
from orchestrator.store import ADDED, DELETED, MODIFIED


@pytest.fixture
def store():
    return MemoryStore()


def obj(name="a", spec=None, **kwargs):
    return new_object("Pod", name, spec or {"image": "x"}, **kwargs)


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def test_create_assigns_a_resource_version(store):
    created = store.create(obj())
    assert created.meta.resource_version == 1
    assert store.get(created.key).spec == {"image": "x"}


def test_duplicate_create_is_rejected(store):
    store.create(obj())
    with pytest.raises(AlreadyExistsError):
        store.create(obj())


def test_update_requires_the_current_resource_version(store):
    created = store.create(obj())
    stale = created.copy()
    fresh = created.copy()
    fresh.spec["image"] = "y"
    store.update(fresh)

    stale.spec["image"] = "z"
    with pytest.raises(ConflictError, match="has been modified"):
        store.update(stale)
    # The winner's write survives; the loser's is not silently applied.
    assert store.get(created.key).spec["image"] == "y"


def test_generation_tracks_spec_changes_only(store):
    created = store.create(obj())
    assert created.meta.generation == 1

    status_only = created.copy()
    status_only.status = {"phase": "Running"}
    after_status = store.update(status_only)
    assert after_status.meta.generation == 1        # status writes must not bump it

    spec_change = after_status.copy()
    spec_change.spec["image"] = "y"
    assert store.update(spec_change).meta.generation == 2


def test_update_of_a_missing_object_raises(store):
    with pytest.raises(NotFoundError):
        store.update(obj())


def test_delete_removes_and_raises_on_repeat(store):
    created = store.create(obj())
    store.delete(created.key)
    with pytest.raises(NotFoundError):
        store.get(created.key)
    with pytest.raises(NotFoundError):
        store.delete(created.key)


def test_stored_objects_are_copies(store):
    created = store.create(obj())
    created.spec["image"] = "mutated"
    assert store.get(created.key).spec["image"] == "x"


# ---------------------------------------------------------------------------
# retry loop
# ---------------------------------------------------------------------------


def test_update_with_retry_survives_a_concurrent_writer(store):
    created = store.create(obj())
    interference = {"count": 0}

    def mutate(candidate):
        # Simulate another controller winning the race on the first attempt.
        if interference["count"] == 0:
            interference["count"] += 1
            other = store.get(created.key)
            other.status = {"touched": True}
            store.update(other)
        candidate.spec["image"] = "final"
        return True

    result = store.update_with_retry(created.key, mutate)
    assert result.spec["image"] == "final"
    assert result.status == {"touched": True}      # the other write was not lost


def test_update_with_retry_can_abandon(store):
    created = store.create(obj())
    result = store.update_with_retry(created.key, lambda candidate: False)
    assert result.meta.resource_version == created.meta.resource_version


def test_update_with_retry_on_missing_object_returns_none(store):
    assert store.update_with_retry("Pod/default/ghost", lambda c: True) is None


def test_concurrent_increments_do_not_lose_updates(store):
    created = store.create(new_object("Pod", "counter", {"n": 0}))

    def bump():
        for _ in range(25):
            store.update_with_retry(created.key, lambda c: (c.spec.__setitem__("n", c.spec["n"] + 1), True)[1],
                                    attempts=200)

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.get(created.key).spec["n"] == 100


# ---------------------------------------------------------------------------
# reads and watches
# ---------------------------------------------------------------------------


def test_list_filters_by_kind_and_labels(store):
    store.create(new_object("Pod", "a", {}, labels={"app": "x"}))
    store.create(new_object("Pod", "b", {}, labels={"app": "y"}))
    store.create(new_object("Node", "n", {}))
    assert [o.meta.name for o in store.list("Pod")] == ["a", "b"]
    assert [o.meta.name for o in store.list("Pod", {"app": "x"})] == ["a"]
    assert len(store.list()) == 3


def test_watch_receives_every_event_type(store):
    seen = []
    cancel = store.watch(seen.append)
    created = store.create(obj())
    updated = created.copy()
    updated.spec["image"] = "y"
    store.update(updated)
    store.delete(created.key)
    cancel()
    store.create(obj("later"))
    assert [event.type for event in seen] == [ADDED, MODIFIED, DELETED]


def test_a_broken_watcher_does_not_break_the_writer(store):
    def explode(_event):
        raise RuntimeError("watcher is broken")

    store.watch(explode)
    # The write must still succeed: a dropped notification costs latency, which
    # the resync loop absorbs, and never correctness.
    assert store.create(obj()).meta.resource_version == 1


def test_events_since_replays_from_a_revision(store):
    first = store.create(obj("a"))
    store.create(obj("b"))
    events = store.events_since(first.meta.resource_version)
    assert [event.object.meta.name for event in events] == ["b"]


def test_events_since_refuses_a_gap_it_cannot_fill(store):
    small = MemoryStore(history=3)
    for i in range(6):
        small.create(obj(f"p{i}"))
    with pytest.raises(ConflictError, match="must resync"):
        small.events_since(1)


# ---------------------------------------------------------------------------
# work queue
# ---------------------------------------------------------------------------


def test_queue_deduplicates_a_burst():
    queue = WorkQueue()
    for _ in range(50):
        queue.add("Pod/default/a")
    assert queue.get(timeout=0.1) == "Pod/default/a"
    assert queue.get(timeout=0.05) is None
    assert queue.deduplicated == 49


def test_a_change_during_processing_requeues_once():
    queue = WorkQueue()
    queue.add("k")
    key = queue.get(timeout=0.1)
    for _ in range(5):
        queue.add(key)          # arrived while the reconcile is running
    queue.done(key)
    assert queue.get(timeout=0.1) == "k"
    assert queue.get(timeout=0.05) is None


def test_failures_back_off_exponentially():
    queue = WorkQueue()
    delays = []
    for _ in range(4):
        queue.add("k")
        key = queue.get(timeout=0.2)
        delays.append(queue.fail(key))
    assert delays == sorted(delays)
    assert delays[-1] > delays[0]


def test_success_clears_the_backoff():
    queue = WorkQueue()
    queue.add("k")
    queue.fail(queue.get(timeout=0.2))
    queue.succeed("k")
    queue.add("k")
    assert queue.fail(queue.get(timeout=0.2)) == pytest.approx(0.05)
