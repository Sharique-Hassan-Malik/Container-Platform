#!/usr/bin/env python3
"""What a rolling update costs, and what readiness has to do with it.

    python3 bench/rollout.py

Two experiments.

**Strategy sweep.** Availability is sampled from the store's watch, which fires
synchronously on every write, so the minimum reported is a state the cluster
genuinely passed through -- a polling sampler aliases and misses exactly the
dips it exists to catch.

**Readiness vs liveness.** The same rollout scored two ways: counting pods that
are *running*, and counting pods that are *ready*. For a model server the gap
between those is however long weights take to load, and a rollout that advances
on the first number retires working replicas while the new ones are still
loading.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import ControlPlane, MemoryStore, SimulatedRuntime, deployment_spec, pod_spec  # noqa: E402
from orchestrator.objects import READY, RUNNING, TERMINAL  # noqa: E402
from orchestrator.workloads import is_available  # noqa: E402


def table(rows, headers) -> str:
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    lines = ["  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join(lines)


def spec(image, replicas, surge, unavailable, load_time):
    return deployment_spec(
        replicas=replicas,
        template={"labels": {"app": "serve"}, "spec": pod_spec(image, readiness_delay_s=load_time)},
        selector={"app": "serve"},
        max_surge=surge,
        max_unavailable=unavailable,
    )


def run(replicas: int, surge: int, unavailable: int, load_time: float, nodes: int):
    import time

    plane = ControlPlane(MemoryStore(), runtime=SimulatedRuntime()).with_nodes(nodes)
    plane.start()
    try:
        plane.apply_deployment("serve", spec("serve:v1", replicas, surge, unavailable, load_time))
        if not plane.wait_available("serve", replicas, timeout=30):
            raise RuntimeError("initial deployment never became available")

        ready_samples, running_samples, pod_counts, overstatement = [], [], [], []

        def on_event(event):
            if event.object.kind != "Pod":
                return
            pods = plane.store.list("Pod", {"app": "serve"})
            ready_samples.append(sum(1 for p in pods if is_available(p)))
            running_samples.append(sum(
                1 for p in pods if p.status.get("phase") in (RUNNING, READY)
            ))
            pod_counts.append(sum(1 for p in pods if p.status.get("phase") not in TERMINAL))
            overstatement.append(running_samples[-1] - ready_samples[-1])

        cancel = plane.store.watch(on_event)
        started = time.perf_counter()
        plane.apply_deployment("serve", spec("serve:v2", replicas, surge, unavailable, load_time))
        completed = plane.wait_complete("serve", timeout=60)
        elapsed = time.perf_counter() - started
        cancel()

        return {
            "completed": completed,
            "seconds": elapsed,
            "min_ready": min(ready_samples) if ready_samples else replicas,
            "min_running": min(running_samples) if running_samples else replicas,
            "max_pods": max(pod_counts) if pod_counts else replicas,
            "max_overstatement": max(overstatement) if overstatement else 0,
            "samples": len(ready_samples),
        }
    finally:
        plane.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replicas", type=int, default=6)
    parser.add_argument("--nodes", type=int, default=4)
    parser.add_argument("--load-time", type=float, default=0.15,
                        help="seconds a replica takes to become ready (weights loading)")
    args = parser.parse_args()

    print(f"{args.replicas} replicas on {args.nodes} nodes, "
          f"{args.load_time * 1000:.0f} ms to load weights per replica\n")

    strategies = [
        ("surge=1, unavailable=0", 1, 0),
        ("surge=2, unavailable=0", 2, 0),
        ("surge=0, unavailable=1", 0, 1),
        ("surge=0, unavailable=3", 0, 3),
        ("surge=3, unavailable=3", 3, 3),
    ]
    rows, results = [], {}
    for label, surge, unavailable in strategies:
        result = run(args.replicas, surge, unavailable, args.load_time, args.nodes)
        results[label] = result
        rows.append([
            label,
            f"{result['seconds']:.2f}",
            f"{result['min_ready']}/{args.replicas}",
            f"{result['max_pods']}",
            f"{(1 - result['min_ready'] / args.replicas) * 100:.0f}%",
        ])
    print(table(rows, ["strategy", "duration (s)", "min available", "peak pods*", "capacity lost"]))
    print("\n* observed, not requested. maxSurge bounds the replicas the controller\n"
          "  asks for; a pod already asked to go still exists until its deletion is\n"
          "  processed, so the observed count transiently exceeds the bound.")

    safe = results["surge=1, unavailable=0"]
    fast = results["surge=3, unavailable=3"]
    lossy = results["surge=0, unavailable=3"]

    print(f"""
The tradeoff is bounded on both sides and neither end is free.

  surge=1, unavailable=0   never drops below {safe['min_ready']}/{args.replicas} available, needs {safe['max_pods']} pods'
                           worth of capacity, takes {safe['seconds']:.2f} s.
  surge=3, unavailable=3   finishes in {fast['seconds']:.2f} s ({safe['seconds'] / max(fast['seconds'], 1e-9):.1f}x faster) and drops to
                           {fast['min_ready']}/{args.replicas} available on the way.
  surge=0, unavailable=3   needs no spare capacity at all -- peak {lossy['max_pods']} pods -- and pays
                           for it by serving on {lossy['min_ready']}/{args.replicas} replicas mid-rollout.

maxSurge buys speed with capacity. maxUnavailable buys speed with availability.
A cluster with no headroom can only choose the second, which is why "we cannot
afford another node" quietly becomes "deploys are a partial outage".""")

    # -- readiness vs liveness ----------------------------------------------
    print("\n\nReadiness versus liveness, same rollouts:\n")
    print(table(
        [[label,
          f"{results[label]['min_ready']}/{args.replicas}",
          f"+{results[label]['max_overstatement']}"]
         for label, _, _ in strategies],
        ["strategy", "min ready (truth)", "worst overstatement by 'running'"],
    ))
    gaps = [results[l]['max_overstatement'] for l, _, _ in strategies]
    print(f"""
A pod is Running the moment its process starts and Ready only once it will
serve a request -- here {args.load_time * 1000:.0f} ms later, which for a real model server is
however long the weights take to load. Scoring these rollouts by "running"
overstates availability by up to {max(gaps)} replicas.

A controller that advances on Running retires the last working replica while
every new one is still loading. Every pod reports healthy, the dashboard shows
full capacity, and the service returns errors for the length of a model load.
That is why `is_available()` checks Ready, and why `minReadySeconds` requires it
to have held for a while before counting.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
