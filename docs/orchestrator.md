# Architecture

## Components

```
                        ┌───────────────────────────────┐
   kubectl-shaped  ───▶ │  ClusterStore                 │
   writes to spec       │  CAS on resourceVersion       │
                        │  global revision + watches    │
                        │                               │
                        │  MemoryStore  |  RaftStore    │
                        └───────┬───────────────┬───────┘
                                │ watch         │ apply()
                    ┌───────────┴──────┐   ┌────┴──────────────┐
                    │                  │   │ ObjectStateMachine│
                    ▼                  ▼   │  (replicated by   │
        ┌──────────────────┐  ┌────────────┤   raft-kv)        │
        │ Scheduler        │  │ Deployment └───────────────────┘
        │  Pod -> nodeName │  │  Controller
        └──────────────────┘  │   └─▶ ReplicaSet per revision
                              │
        ┌──────────────────┐  │  ┌──────────────────────┐
        │ ReplicaSet       │◀─┘  │ NodeMonitor          │
        │  Controller      │     │  heartbeat -> evict  │
        │   count -> Pods  │     └──────────────────────┘
        └──────────────────┘
                    │
                    ▼
        ┌──────────────────────────────────┐
        │ NodeAgent (one per node)         │
        │  start/stop, probe, backoff      │
        │  Simulated | Process | Container │
        └──────────────────────────────────┘
```

Every arrow is a *watch plus a read*, never a message. No component tells
another to do anything; each observes state and acts. That is what makes the
whole thing restartable at any point — a controller that comes back with no
memory lists the world and continues.

## Level-triggered reconciliation

The rule, stated once:

> A reconcile receives a **key**. It re-reads current state, compares desired
> with actual, and takes one step. It never reads the event.

`Controller.reconcile(key: str)` takes a string, not an `Event`, so the rule is
enforced by the type rather than by discipline. `interesting(event) -> list[str]`
is the only place an event is touched, and all it may do is map it to keys.

The consequences are the whole benefit:

| event stream defect | edge-triggered | level-triggered |
|---|---|---|
| dropped | permanent divergence | latency until next resync |
| duplicated | spurious action | one pass that finds nothing to do |
| reordered | wrong action | meaningless — order never consulted |
| controller restarted | lost all pending work | lists the world, continues |

`bench/triggering.py` measures this. The resync loop is the safety net that
makes a dropped event survivable, and it is also why `MemoryStore._emit` is
allowed to swallow an exception from a broken watcher: a lost notification
costs latency, never correctness.

## Why `spec` and `status` are separate

`spec` is written by users, `status` by controllers, and neither writes the
other's half. A controller that writes `spec` has started arguing with the user
and the system stops converging.

Two metadata fields do the rest of the work:

**`resourceVersion`** changes on every write and gates every update. Two
controllers routinely act on the same object; without the check, the second
silently overwrites the first. `update_with_retry` re-reads and recomputes on
`ConflictError` — retrying is the concurrency model, not a workaround, because
the conflict means the mutation must be recomputed against new state rather than
replayed against old.

**`generation`** increments only when `spec` changes, and controllers record
what they acted on in `status.observedGeneration`. Without it, "the rollout is
complete" and "the controller has not looked at the new spec yet" are
indistinguishable — which is a bug this project shipped and then fixed:
`wait_complete` returned true for the *previous* rollout.

## Deployment → ReplicaSet → Pod

The indirection exists for exactly one reason: a **ReplicaSet owns an immutable
template**, so it cannot roll anything out, and a **Deployment owns a sequence of
ReplicaSets**, so rolling out is moving two numbers.

```
Deployment "serve"  replicas=4  maxSurge=1  maxUnavailable=0
  ├── ReplicaSet serve-52189e (revision 1, template hash 52189e)  replicas=0
  └── ReplicaSet serve-aea176 (revision 2, template hash aea176)  replicas=4
```

ReplicaSets are keyed by a **hash of the template**, which makes rollback nearly
free: editing the Deployment back to a previous spec re-selects the existing
ReplicaSet rather than creating a duplicate. The rolled-back pods are then
identical to the ones that worked, not a fresh interpretation of an old config.
`rollback()` is not a special code path — it rewrites `spec.template` and lets
the ordinary rollout logic run.

The exact template is stored in an annotation, because the copy in
`rs.spec.template` has selector and hash labels merged in and therefore hashes
differently.

### The two asynchrony bugs

Both were real, both shipped, both are the same mistake on opposite sides:

**Surge.** Scale-up headroom must be computed against replicas *requested*
across all ReplicaSets, not pods *observed*. Pod creation is asynchronous, so
counting observations lets a second pass grant the same headroom before the
first materialised. `maxSurge=1` then produces unbounded pods.

**Unavailability.** Symmetrically, availability must be clamped *per
ReplicaSet*: a pod that is still `Ready` but exceeds its own ReplicaSet's
requested count has already been asked to go. Counting it as headroom authorises
a second removal for the same slot, and two of those breach `maxUnavailable=0`.

The general rule both express: **compare requests with requests, never a request
with an observation that has not caught up to it.**

## Crash handling

`CrashLoopBackOff` is deliberately **not** a terminal phase. If a crashed pod
were terminal, its ReplicaSet would see the count drop and create a replacement,
which would also crash — 122 pods per node, measured. Restarting in place with
exponential backoff keeps the pod object stable, so the count stays right and a
broken rollout simply *stalls*, which is what a stuck deploy should do.

Node failure needs no special recovery path at all. `NodeMonitor` infers failure
from a stale heartbeat (a crashed node cannot report its own death), marks the
node not-ready and deletes its pods. That turns "a node died" into "the
ReplicaSet is short a replica", which every other controller already knows how
to handle.

## Scheduling

Filter then score, and the separation matters more than either algorithm. A node
that fails a predicate is *removed*, not penalised — scoring an infeasible node
produces a placement that fails at startup, costing a scheduling round trip and
a container start.

Allocation counts **assigned** pods, not running ones. A pod bound but not yet
started still owns its resources; ignoring it lets a second pod be placed into
the same space during the startup window, and both then fail.

The scheduler writes `spec.nodeName` and nothing else. It does not start the
pod, wait for it, or care whether it succeeds. That is why a scheduler outage
stops new placements without touching anything already running.

Anti-affinity applies only in spread mode — under bin-packing it would fight the
objective it exists to serve, which is a bug this project had until the
bin-packing test failed.

## The work queue

Three sets, not one list, because "queued", "in flight" and "changed again while
in flight" are genuinely different states. Collapsing them loses a change that
arrives mid-reconcile: the pass already running read state from before it.

A burst of fifty events for one object costs two reconciles — one in flight, one
requeued — not fifty. Failures back off exponentially so a permanently broken
object cannot hot-loop.

## Storage backends

| | `MemoryStore` | `RaftStore` |
|---|---|---|
| CAS | under a lock | inside `apply`, after Raft ordering |
| revision | a counter | the state machine's counter, identical on every replica |
| reads | local | local, possibly one round trip stale |
| watches | direct callbacks | state-machine listeners |

Local reads on a follower can lag the leader. That is safe here *because*
controllers are level-triggered: acting on state one revision stale produces a
redundant action and then convergence, never divergence. The same property that
makes dropped events survivable makes stale reads survivable.

## Layout

| path | role |
|---|---|
| `orchestrator/objects.py` | object model, phases, spec helpers |
| `orchestrator/store.py` | CAS, revisions, watches; Memory and Raft backends |
| `orchestrator/controller.py` | work queue, level-triggered base, edge foil |
| `orchestrator/scheduler.py` | filter/score placement |
| `orchestrator/workloads.py` | ReplicaSet and Deployment controllers, rollback |
| `orchestrator/node_agent.py` | pod lifecycle, probes, backoff; node monitor |
| `orchestrator/runtime.py` | Simulated / Process / Container execution |
| `orchestrator/cluster.py` | wiring, and the test/bench observation helpers |
| `bench/triggering.py` | level vs edge under a lossy watch |
| `bench/rollout.py` | strategy sweep, readiness vs liveness |

## What this is not

- **No API server.** The store is the API. Adding HTTP would exercise
  serialisation, not orchestration.
- **No Services, DNS, or load balancing.** Readiness is tracked because rollouts
  depend on it, but nothing routes traffic.
- **No autoscaling.** That is #21, deliberately: scaling on queue depth rather
  than CPU is a separate idea and deserves its own project.
- **No leader election among controllers.** All controllers run in one process
  against one store. In a real deployment each would need a lease; the
  reconciliation logic would not change, which is rather the point.
