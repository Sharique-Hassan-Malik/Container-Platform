#!/usr/bin/env python3
"""Level-triggered versus edge-triggered reconciliation, under a lossy watch.

    python3 bench/triggering.py --drop 0.2

Both controllers are given the same job -- keep N pods alive -- and the same
event stream, through a watch that drops a configurable fraction of events. This
is not a contrived fault: a watch is a network connection, and every real one
drops, duplicates and reorders.

    edge-triggered   reads the event: "a pod was deleted" -> create one
    level-triggered  reads the key, then re-reads the world: count, then act

The result is not that one is faster. It is that one of them is still correct.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Controller, EdgeTriggeredController, MemoryStore, new_object  # noqa: E402
from orchestrator.store import ADDED, DELETED, Event  # noqa: E402


class LossyStore(MemoryStore):
    """A store whose watch drops, duplicates and reorders, like a real one."""

    def __init__(self, drop: float = 0.2, duplicate: float = 0.1, seed: int = 0, **kwargs):
        super().__init__(**kwargs)
        self.drop = drop
        self.duplicate = duplicate
        self.random = random.Random(seed)
        self.delivered = 0
        self.dropped = 0
        self.duplicated = 0

    def _emit(self, event: Event) -> None:
        self._history.append(event)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]
        if self.random.random() < self.drop:
            self.dropped += 1
            return
        copies = 2 if self.random.random() < self.duplicate else 1
        self.duplicated += copies - 1
        for _ in range(copies):
            self.delivered += 1
            for handler in list(self._watchers):
                try:
                    handler(event)
                except Exception:  # noqa: BLE001
                    continue


DESIRED_KEY = "ReplicaSet/default/target"


class LevelReconciler(Controller):
    """Reads current state and closes the gap. Never reads the event."""

    name = "level"
    resync_period = 0.05

    def interesting(self, event: Event) -> list[str]:
        return [DESIRED_KEY]

    def reconcile(self, key: str) -> float | None:
        rs = self.store.get(DESIRED_KEY)
        desired = int(rs.spec["replicas"])
        pods = [p for p in self.store.list("Pod") if p.meta.owner == DESIRED_KEY]
        for _ in range(desired - len(pods)):
            self.store.create(new_object("Pod", f"p-{time.time_ns()}", {}, owner=DESIRED_KEY))
        for pod in pods[desired:]:
            try:
                self.store.delete(pod.key)
            except Exception:  # noqa: BLE001
                pass
        return None


class EdgeReconciler(EdgeTriggeredController):
    """Reacts to the event itself. Correct only if the stream is perfect."""

    name = "edge"

    def handle(self, event: Event) -> None:
        if event.object.kind != "Pod" or event.object.meta.owner != DESIRED_KEY:
            return
        if event.type == DELETED:
            # "One was removed, so create one." Sound reasoning about an event
            # that may never arrive, or may arrive twice.
            self.store.create(new_object("Pod", f"p-{time.time_ns()}", {}, owner=DESIRED_KEY))


def run(kind: str, *, replicas: int, churn: int, drop: float, duplicate: float, seed: int) -> dict:
    store = LossyStore(drop=drop, duplicate=duplicate, seed=seed)
    rs = new_object("ReplicaSet", "target", {"replicas": replicas})
    store.create(rs)
    for _ in range(replicas):
        store.create(new_object("Pod", f"p-{time.time_ns()}", {}, owner=DESIRED_KEY))

    controller = (LevelReconciler if kind == "level" else EdgeReconciler)(store)
    controller.start()
    time.sleep(0.1)

    chaos = random.Random(seed + 1)
    for _ in range(churn):
        pods = [p for p in store.list("Pod") if p.meta.owner == DESIRED_KEY]
        if pods:
            try:
                store.delete(chaos.choice(pods).key)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.004)

    # Give both a generous chance to settle. The level-triggered one resyncs;
    # the edge-triggered one has nothing left to react to.
    time.sleep(1.0)
    final = len([p for p in store.list("Pod") if p.meta.owner == DESIRED_KEY])
    controller.stop()
    return {
        "kind": kind,
        "final": final,
        "desired": replicas,
        "error": final - replicas,
        "delivered": store.delivered,
        "dropped": store.dropped,
        "duplicated": store.duplicated,
        "reconciles": controller.reconciles,
    }


def table(rows, headers) -> str:
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    lines = ["  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replicas", type=int, default=10)
    parser.add_argument("--churn", type=int, default=40, help="pods deleted out from under the controller")
    parser.add_argument("--duplicate", type=float, default=0.1)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--drop", type=float, default=None, help="single drop rate; default sweeps")
    args = parser.parse_args()

    rates = [args.drop] if args.drop is not None else [0.0, 0.05, 0.1, 0.2, 0.4]
    rows = []
    for drop in rates:
        for kind in ("level", "edge"):
            results = [
                run(kind, replicas=args.replicas, churn=args.churn, drop=drop,
                    duplicate=args.duplicate, seed=trial)
                for trial in range(args.trials)
            ]
            errors = [r["error"] for r in results]
            converged = sum(1 for e in errors if e == 0)
            rows.append([
                f"{drop:.0%}", kind,
                f"{sum(r['final'] for r in results) / len(results):.1f}",
                f"{min(errors):+d} .. {max(errors):+d}",
                f"{converged}/{args.trials}",
                f"{sum(r['dropped'] for r in results) // len(results)}",
            ])

    print(
        f"desired={args.replicas} replicas, {args.churn} deletions injected, "
        f"{args.duplicate:.0%} duplicate rate, {args.trials} trials each\n"
    )
    print(table(rows, ["drop rate", "controller", "final pods", "error range", "converged", "events dropped"]))
    print("""
Both controllers see the same stream. The difference is what they read from it.

The edge-triggered controller acts on the event: "a pod was deleted, create
one". Every dropped DELETE is a replica it never replaces, and every duplicated
DELETE is a replica too many. The errors accumulate and nothing removes them --
there is no further event coming, so the controller is finished, permanently
wrong, and reporting no error.

The level-triggered controller ignores the payload entirely: it re-reads the
count and creates the difference. A dropped event costs latency until the next
resync. A duplicate costs one wasted pass that finds nothing to do. Reordering
is meaningless because the order was never consulted.

This is why every controller in this project takes a *key* and re-reads state,
why the store is allowed to drop an event when a watcher misbehaves, and why
`Controller.reconcile` is handed a string rather than an Event -- a reconcile
that cannot see the event cannot come to depend on it.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
