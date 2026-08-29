# Mini Orchestrator

> Part of the [Container Platform](../../README.md). Runs standalone from this
> folder, and composes with the other four modules through the `ctl` CLI.

A Kubernetes-shaped control plane written from first principles: declarative
objects, level-triggered reconciliation, a scheduler, rolling updates with
rollback, node failure detection — and cluster state in **`raft-kv`** instead of
etcd.

---

## Background

Orchestration is usually learned as YAML fluency. That teaches the interface and
almost none of the design, and the design is where all the interesting failures
live. Three questions are worth more than the whole API surface:

- **Why is a controller a loop instead of an event handler?** Everyone writes
  the event handler first. It is shorter, it is obvious, and it is wrong in a
  way that does not show up until production.
- **Why does a Deployment create ReplicaSets instead of pods?** The indirection
  looks like ceremony until you try to implement rollback without it.
- **Why does `readiness` exist when `liveness` already does?** For a model
  server the gap between them is however long the weights take to load, which is
  exactly long enough to turn a routine deploy into an outage.

This project answers all three by building the thing and measuring it.

---

## The headline result

Two controllers, same job — keep 10 pods alive — same event stream, through a
watch that drops and duplicates events the way every real one does. One reads
the event ("a pod was deleted, create one"); the other reads only the *key*,
re-reads current state, and closes the gap.

| drop rate | controller | final pods | error range | converged | events dropped |
|---|---|---:|---:|---:|---:|
| 0% | **level** | 10.0 | +0 .. +0 | **5/5** | 0 |
| 0% | edge | 13.2 | +1 .. +5 | 0/5 | 0 |
| 5% | **level** | 10.0 | +0 .. +0 | **5/5** | 6 |
| 5% | edge | 9.6 | −3 .. +3 | 1/5 | 6 |
| 10% | **level** | 10.0 | +0 .. +0 | **5/5** | 10 |
| 10% | edge | 8.0 | −5 .. −1 | 0/5 | 10 |
| 20% | **level** | 10.0 | +0 .. +0 | **5/5** | 16 |
| 20% | edge | 7.0 | −7 .. +1 | 0/5 | 15 |
| 40% | **level** | 10.0 | +0 .. +0 | **5/5** | 35 |
| 40% | edge | **0.0** | −10 .. −10 | 0/5 | 25 |

`python3 bench/triggering.py`

The level-triggered reconciler converges to exactly 10 in every trial at every
loss rate, including 40%. The edge-triggered one never converges — and at 40%
loss it ends with **zero pods**, a total outage, while reporting no error at
all, because there are no further events to react to.

It fails at 0% loss too, for the mirror-image reason: duplicated events make it
create replicas nobody asked for.

That is the entire argument for the design. A dropped event costs a
level-triggered controller some latency until the next resync. It costs an
edge-triggered controller correctness, permanently. Which is why
`Controller.reconcile()` here is handed a **string key**, never an `Event` — a
reconcile that cannot see the event cannot come to depend on it.

---

## Rolling updates: what each knob actually buys

6 replicas on 4 nodes, 150 ms to load weights per replica. Availability is
sampled from the store's watch, which fires synchronously on every write — a
polling sampler aliases under GIL contention and misses exactly the dips it
exists to catch.

| strategy | duration | min available | peak pods* | capacity lost |
|---|---:|---:|---:|---:|
| surge=1, unavailable=0 | 1.12 s | **6/6** | 7 | 0% |
| surge=2, unavailable=0 | 0.64 s | **6/6** | 10 | 0% |
| surge=0, unavailable=1 | 1.08 s | 5/6 | 7 | 17% |
| surge=0, unavailable=3 | 0.44 s | 3/6 | 9 | 50% |
| surge=3, unavailable=3 | **0.24 s** | 3/6 | 9 | 50% |

`python3 bench/rollout.py`

\* observed, not requested. `maxSurge` bounds the replicas the controller *asks*
for; a pod already asked to go still exists until its deletion is processed, so
the observed count transiently exceeds the bound.

**maxSurge buys speed with capacity. maxUnavailable buys speed with
availability.** A cluster with no headroom can only choose the second — which is
how "we cannot afford another node" quietly becomes "deploys are a partial
outage". The safe setting is 4.7× slower than the fast one and never drops a
single replica.

## Readiness is not liveness

The same rollouts, scored by "running" instead of "ready":

| strategy | min ready (truth) | worst overstatement by "running" |
|---|---:|---:|
| surge=1, unavailable=0 | 6/6 | +1 |
| surge=2, unavailable=0 | 6/6 | +2 |
| surge=0, unavailable=3 | 3/6 | +3 |
| surge=3, unavailable=3 | 3/6 | **+6** |

A pod is Running the moment its process starts and Ready only once it will serve
a request. Scoring by Running overstates availability by up to **a full
deployment's worth of replicas**.

A controller that advances on Running retires the last working replica while
every new one is still loading. Every pod reports healthy, the dashboard shows
full capacity, and the service returns errors for the length of a model load.
This is the single most expensive mistake available in this design space, and it
is one boolean.

## Cluster state on Raft, not etcd

`RaftStore` replaces etcd with the sibling `raft-kv` project. The interesting
part is where the compare-and-swap runs:

```python
def apply(self, command: str) -> None:      # ObjectStateMachine
    verb, request_id, payload = command.split(" ", 2)
    ...
    if obj.meta.resource_version != current.meta.resource_version:
        return False, "ConflictError: ..."
```

Inside `apply`, **after** Raft has ordered the command. Every replica evaluates
the same check against the same prior state and reaches the same verdict.
Checking the version before proposing would be a read on the leader followed by
a write, with a window in between — two clients could both pass the check and
both commit, which is precisely the lost update the version exists to prevent.

`ObjectStateMachine` is duck-compatible with `raft_kv.store.KVStore` (`apply` +
`get`), so it drops into that project's gRPC server unchanged.

```
test_state_machine_is_deterministic_across_replicas
test_conflicting_update_is_rejected_by_consensus
test_a_follower_refuses_writes
test_writes_replicate_to_every_node
```

## Usage

```python
from orchestrator import ControlPlane, MemoryStore, deployment_spec, pod_spec
from orchestrator.workloads import rollback

plane = ControlPlane(MemoryStore()).with_nodes(3).start()

plane.apply_deployment("serve", deployment_spec(
    replicas=4,
    template={"labels": {"app": "serve"}, "spec": pod_spec("serve:v1", readiness_delay_s=2.0)},
    selector={"app": "serve"},
    max_surge=1, max_unavailable=0,          # never drop a replica
))
plane.wait_available("serve", 4)

plane.apply_deployment("serve", ...)          # roll to v2
plane.wait_complete("serve")

rollback(plane.store, "Deployment/default/serve")   # back to the previous revision
```

Pods can be executed three ways behind one interface: `SimulatedRuntime`
(deterministic, used by tests), `ProcessRuntime` (a subprocess each), and
`ContainerRuntime` (a real container each, via `mini-container-runtime` #19).
The control plane's correctness has nothing to do with which — requiring
unprivileged user namespaces in order to test a scheduler would be the wrong
dependency, so the container runtime is imported lazily.

## What is implemented

| | |
|---|---|
| declarative objects | `metadata` / `spec` / `status`, `resourceVersion`, `generation` |
| optimistic concurrency | compare-and-swap with `ConflictError` and a retry loop |
| watches | resumable from a revision; refuses to serve a gap it cannot fill |
| work queue | dedup, in-flight tracking, exponential backoff |
| scheduler | filter/score split, resource fit, node selectors, cordon, anti-affinity |
| ReplicaSet | count reconciliation, least-useful-first deletion |
| Deployment | ReplicaSet per revision, maxSurge/maxUnavailable, minReadySeconds |
| rollback | re-selects the existing revision, never re-derives it |
| node agent | starts/stops pods, probes, crash-loop backoff, heartbeat |
| node monitor | heartbeat timeout → eviction → ordinary rescheduling |

## Running it

```bash
python3 -m pytest tests/ -q          # 48 tests
python3 bench/triggering.py          # level vs edge under a lossy watch
python3 bench/rollout.py             # strategy sweep, readiness vs liveness
```

The Raft tests skip unless the sibling `raft-kv` project and `grpcio` are
importable; everything else is standard library only.

## Optional: replicated state via Raft-KV

`RaftStore` puts cluster state behind Raft consensus using **Raft-KV**, from the
[`networking-distributed-systems`](https://github.com/Sharique-Hassan-Malik/networking-distributed-systems)
repository — a cross-repository dependency declared in
[`requirements-optional.txt`](./requirements-optional.txt):

```bash
pip install -r requirements-optional.txt
```

Resolution is two-tier: an installed distribution first, then a `Raft-KV/`
directory beside this project. Without it the RaftStore tests skip and the
in-memory store is used.

---

## License

MIT
