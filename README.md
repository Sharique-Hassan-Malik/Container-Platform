# Container Platform

A container runtime, an OCI image builder, a Raft-replicated store, a task
queue, and the control plane that runs on top of them. Five modules that
compose into something Kubernetes-shaped, built from first principles — no
Docker, no runc, no etcd, no privileged helper.

```
ctl status                                   # what this host can actually run
ctl image build ./ctx --tag serve:v1         # OCI layout, reproducible layers
ctl run serve:v1 -- /bin/echo hello          # namespaces, cgroups, overlayfs
ctl up --store raft --runtime container --replicas 4 --rollout serve:v2
```

```
$ ctl up --store raft --runtime simulated --replicas 4 --rollout serve:v2
  control plane up on store=raft runtime=simulated (3 nodes, 0.7s)
  serve: 4 replicas of serve:v1 available
      serve-b6d54efee2-037738      Ready      node-0
      serve-b6d54efee2-090255      Ready      node-1
      serve-b6d54efee2-160871      Ready      node-2
      serve-b6d54efee2-196414      Ready      node-0

  rolling serve to serve:v2 (surge 1, unavailable 0)…
  rollout complete in 0.1s — never below 4 available

  cluster state replicated across 3 Raft members
```

## The five modules

| Module | What it is |
|---|---|
| [`orchestrator`](modules/orchestrator) | The control plane: declarative objects, a store with compare-and-swap and resumable watches, level-triggered controllers, a scheduler, rolling updates and rollback. |
| [`container-runtime`](modules/container-runtime) | Containers from unprivileged user namespaces, cgroups v2 and overlayfs. Runs as an ordinary user. |
| [`image-toolkit`](modules/image-toolkit) | Builds OCI images: content-addressed layers, deterministic digests, and layer ordering tuned for model cold-start. |
| [`raft-kv`](modules/raft-kv) | Raft consensus with a replicated key-value state machine, over gRPC. |
| [`taskqueue`](modules/taskqueue) | A broker, worker pool and result backend over a binary protocol, with a dashboard. |

## The two seams

The control plane has exactly two places where a real implementation and a
simulated one are interchangeable, and both are the point of the repository:

**Where cluster state lives.** `memory` is a single-process object store;
`raft` replicates every write through consensus and serves reads from the local
applied state machine. This is the etcd-shaped hole, filled by the `raft-kv`
module in this repo.

**How a pod is executed.** `simulated` runs pods in-process with exact timing,
which is what makes rollout tests deterministic. `process` forks a subprocess.
`container` runs a real container through `container-runtime`.

```
$ ctl status

  backends

    store    memory      ready
                           Single-process object store. Correct, not replicated.
  * store    raft        ready
                           Cluster state replicated through Raft — the etcd-shaped seam.

    runtime  simulated   ready
                           In-process pods with exact timing. What the tests use.
    runtime  process     ready
                           One real subprocess per pod. No isolation.
  * runtime  container   ready
                           One real container per pod: namespaces, cgroups, overlayfs.
```

An unavailable backend is **named and explained**, never silently swapped. A
rollout benchmark that ran against the simulator because overlayfs was missing
is a number about nothing, and the only way to know is for the tool to say
which backend it used. `--store raft` fails if Raft cannot run here; only
`auto` falls back, and it says so.

## Using one module on its own

Each module folder is a self-contained source root with its own CLI, tests and
README:

```bash
cd modules/container-runtime && python -m minicon run serve:v1
cd modules/image-toolkit     && python -m imagekit build ./ctx --tag serve:v1
cd modules/raft-kv           && python scripts/run_node.py --id n0
cd modules/taskqueue         && python tq.py broker
cd modules/orchestrator      && python -m pytest tests/
```

The orchestrator finds its optional backends by looking for sibling module
folders, so the Raft store and the container runtime work when it is run from
its own directory exactly as they do through `ctl` — "works standalone" and
"works integrated" cannot drift apart.

`ctl image`, `ctl run` and `ctl queue` delegate to those same CLIs rather than
reimplementing them, arguments and `--help` included.

## Install

```bash
pip install -e .            # the control plane, runtime and image toolkit
pip install -e ".[raft]"    # adds grpcio, enabling the Raft store
```

The container runtime needs unprivileged user namespaces and overlayfs. Check
with `ctl status`; on a host without them, `--runtime auto` drops to `process`
and tells you why.

## Tests

```bash
pytest                            # everything, 280+ tests
pytest modules/orchestrator       # one module
```

## Licence

MIT — see [LICENSE](LICENSE).
