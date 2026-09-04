# Architecture

Five modules, two seams, one CLI. Each module's internals are documented in
[`docs/`](docs); this is about how they compose.

```
                            ctl/cli.py
                                │
              ┌─────────────────┼──────────────────┐
              │                 │                  │
        delegation          ctl/plane.py      ctl/backends.py
   image / run / queue    brings up a plane   what this host can run
              │                 │                  │
              ▼                 ▼                  │
   ┌──────────────────┐   orchestrator ◄───────────┘
   │ image-toolkit    │   (the control plane)
   │ container-runtime│         │
   │ taskqueue        │    ┌────┴────┐
   └──────────────────┘    │         │
                       store seam  runtime seam
                           │         │
                        raft-kv   container-runtime
```

## The two seams

A control plane's correctness has nothing to do with where its state is stored
or how a pod is executed, so both are interfaces with more than one
implementation:

| Seam | Interface | Implementations |
|---|---|---|
| cluster state | `ClusterStore` | `MemoryStore`, `RaftStore` (via `raft-kv`) |
| pod execution | `PodRuntime` | `SimulatedRuntime`, `ProcessRuntime`, `ContainerRuntime` (via `container-runtime`) |

`ctl/backends.py` is the registry over both. Its job is not to pick — it is to
**know why a backend cannot run here and say so**. gRPC missing, user
namespaces disabled, overlayfs unusable inside a userns: each is a specific
answer, not a generic "unavailable".

That matters because the failure mode is silent. A rollout benchmark that ran
against `SimulatedRuntime` because overlayfs was missing produces a plausible
number that means nothing. So `--store raft` is a *requirement* and raises if
unmet; only `--store auto` falls back, and it prints what it fell back to and
why.

## Where composition lives

`ctl/plane.py` stands up three Raft nodes, waits for an election, and hands the
leader's applied state machine to a `RaftStore`. That logic does not belong in
the orchestrator — its job ends at the `ClusterStore` interface — and it does
not belong in `raft-kv`, which knows nothing about cluster objects. It belongs
in the platform, which is the only place that knows both.

The natural place for it to end up is inside the orchestrator's *test suite* —
the test being the only thing that ever wires the two together — which leaves
the CLI reimplementing it. The test and the CLI use the same function.

## Reads, writes and staleness

`RaftStore` proposes writes through the leader and serves reads from the local
applied state machine, which may lag by a round trip. That is safe here for a
specific reason: the controllers are **level-triggered**. A controller acting
on state one revision stale takes a redundant action and then converges. An
edge-triggered controller that missed an event would diverge forever — which is
why the store also offers resumable watches, and tells a watcher to resync
rather than handing it a silent gap.

## Standalone and integrated

Each module folder is its own source root, so `modules/raft-kv` holds the
`raft_kv` package and can be run from that directory alone.

The orchestrator's optional backends are siblings in the same repository, and
it finds them through `orchestrator/_siblings.py`, which adds the sibling
folders to `sys.path`. That is 20 lines and it depends on nothing in `ctl`, so
running the orchestrator from its own directory gets the same Raft store and
container runtime that `ctl` gets. "Works standalone" and "works integrated"
cannot drift apart, because they are the same import path.

Nothing is required. A missing sibling means the import fails and the caller
falls back to the in-memory store and the simulated runtime — which is exactly
what the tests use.

## Delegation, not reimplementation

`ctl image`, `ctl run` and `ctl queue` dispatch to `imagekit.cli`,
`minicon.cli` and `taskqueue.cli` before argparse sees the arguments, so the
module's own parser handles them — `ctl image --help` prints imagekit's help.
A wrapper that re-declared those flags would be a second copy to keep in sync,
and it would drift.

The one change this required was splitting `taskqueue`'s CLI out of its `tq.py`
entry script into `taskqueue/cli.py`, so there is a `main(argv)` to call. The
entry script is now four lines over that function.

## gRPC and namespaces, in one process

The one genuinely hard interaction between two modules, and the reason this is
a platform rather than five directories.

`unshare(CLONE_NEWUSER)` returns EINVAL unless the caller is the only thread in
its thread group, so a container runtime forks first — a forked child keeps
only the calling thread. gRPC breaks that: it registers a `pthread_atfork`
handler that recreates its polling threads *in the child*, so with a server
running, every fork produces a child with six threads and every container start
fails with an `Invalid argument` that names nothing involved.

`ctl up --store raft --runtime container` runs both in one process, so this is
the platform's problem to solve, and `ctl/__init__.py` solves it where a
process-global setting belongs:

```python
# Set before anything imports grpc, and this package is the first thing the
# CLI touches.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
```

The forked child unshares and execs a container; it never speaks gRPC, so
disabling fork support costs nothing.

Two supporting pieces, because a platform should not depend on an environment
variable for correctness:

- `minicon/nshelper.py` re-execs a clean process for layer unpacking and
  cleanup. An `exec`'d process has one thread and none of the parent's fork
  handlers, whatever the parent loaded — the same reason runc has `nsenter`.
  The price is that those operations cross an exec, so they are JSON documents
  rather than closures.
- `minicon.linux.fork_thread_hazard()` names the cause, and the container child
  reports it back up its pipe, so the failure mode is a sentence about gRPC
  rather than an errno.

## Known trade-offs

- **The Raft cluster is in-process.** `ctl up --store raft` starts three nodes
  as gRPC servers inside one Python process. That exercises real consensus —
  election, replication, leader rejection of follower writes — but it is not a
  distributed deployment, and it does not test network partitions.
- **`SimulatedRuntime` is the default for tests on purpose.** Real subprocess
  startup varies by tens of milliseconds, which is enough to make a rollout
  benchmark unreproducible and a rollout test flaky. Every behaviour the
  control plane cares about — start latency, readiness delay, startup failure,
  crashing later — is expressible in the simulator exactly.
- **The container runtime needs an unprivileged user namespace and a working
  overlayfs.** Both are common on modern Linux and absent in many containers
  and on macOS. `ctl status` is the check.
