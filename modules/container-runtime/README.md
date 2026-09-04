# Mini Container Runtime

> Part of the [Container Platform](../../README.md). Runs standalone from this
> folder, and composes with the other four modules through the `ctl` CLI.

A container runtime built from the syscalls up: Linux namespaces, cgroup v2
limits, an overlayfs root assembled from OCI layers, `pivot_root`, and a
rtnetlink client written from scratch to wire containers together.

No Docker, no runc, no `ip`, no privileged helper, no setuid binary. Everything
runs as an ordinary user through unprivileged user namespaces.

---

## Background

`image-toolkit` builds OCI images and, at the end of its README,
admits two things: its `RUN` executor is not a sandbox, and its cold-start
runner executes the entrypoint on the host rather than inside the image. Both
admissions point at the same missing piece — nothing there actually *contains*
anything.

This is that piece. It is also the answer to a question `image-toolkit` can
only gesture at from where it sits: **why does the tenth container from an image
cost nothing?** The answer is not caching in any ordinary sense. It is that the image
is unpacked once into read-only directories, and each container adds one empty
directory on top of them.

A container turns out not to be a kernel object at all. There is no
`create_container()`. It is an ordinary process that has had six unrelated
syscalls applied to it — `unshare`, `mount`, `pivot_root`, `sethostname`,
`setns`, `umount2` — plus a directory in `/sys/fs/cgroup`. Implementing it is
mostly a matter of applying them in an order the kernel accepts, and every step
of that order exists because of a specific refusal.

---

## The headline result

An image with 64 MB of incompressible weights, on a 4-core laptop:

| phase | first container | warm |
|---|---:|---:|
| resolve image | 0.1 | 0.5 |
| **unpack layers** | **388.8** | **0.1** |
| create cgroup | 0.7 | 0.8 |
| create namespaces | 13.1 | 13.2 |
| write id maps | 0.2 | 0.5 |
| container setup | 16.9 | 11.5 |
| &nbsp;&nbsp;· mount overlay | 0.6 | 0.7 |
| &nbsp;&nbsp;· mount /proc /dev /sys | 9.5 | 4.7 |
| &nbsp;&nbsp;· pivot_root | 0.4 | 0.6 |
| **total** | **419.7 ms** | **26.6 ms** |

`python3 bench/startup.py --weights-mb 64`

Then twenty containers from that image, all running at once:

| measure | value |
|---|---:|
| unpacked layer cache (shared) | 66.0 MB |
| all 20 container bundles | **36.6 KB** |
| per additional container | **1.8 KB** |
| copy-per-container would be | 1.3 GB |
| median start | 25.3 ms |

Each container adds an empty overlay upper directory, a work directory and a
state file — 1.8 KB. The image is unpacked once and mounted read-only by all
twenty, so twenty containers cost **36,000× less disk** than giving each its own
root filesystem.

That is the whole mechanism behind scale-to-zero. Stopping a replica and
starting a new one costs 25 ms and no I/O proportional to the model, which is
why an autoscaler can afford to do it on every traffic dip. The 389 ms unpack
is paid once per node per image, and nothing else in the table grows with image
size.

---

## What is actually isolated

Every claim is asserted against the kernel, by comparing
`/proc/<pid>/ns/<type>` inodes or by observing something the container genuinely
cannot see — never by checking that `unshare` returned zero.

| namespace | what changes | how the test proves it |
|---|---|---|
| user | uid 0 inside is uid 1000 outside | `id -u` is 0 inside, `os.getuid()` is not |
| pid | container is PID 1 | `/proc` lists ≤ 3 processes, not the host's hundreds |
| mnt | root is the image | `ls /` shows the image, `.oldroot` is gone |
| net | one interface | `ip -o link \| wc -l` is 1 |
| uts | own hostname | `hostname` differs from `uname().nodename` |
| ipc | own SysV/POSIX IPC | namespace inode differs |
| cgroup | own cgroup root | `/proc/self/cgroup` is `0::/`, no `user.slice` prefix |

Resource limits are asserted by making the container exceed them, because a
limit that is set but not enforced looks identical from the outside.

## A cgroup result worth knowing

`memory.max` is a **reclaim trigger first and a killer only as a last resort**.
A container writing 256 MB into a 32 MB cgroup does not die: tmpfs and page
cache are reclaimable, so the kernel pushes them to swap and lets the write
finish. Usage never exceeds the limit and `memory.events.max` counts every
stall — the limit is genuinely enforced — but anyone expecting an OOM kill gets
mysterious slowness instead.

Adding `memory.swap.max = 0` removes the escape hatch and the same workload is
killed. Both cases are pinned by tests:

```
test_memory_limit_throttles_by_reclaim_before_it_kills
test_memory_limit_kills_when_reclaim_has_nowhere_to_go
```

## Networking, and an honest boundary

`minicon/netlink.py` is a rtnetlink client in ~250 lines: message framing,
4-byte-aligned TLV attributes, three-deep nesting for `veth` creation, link
moves by namespace file descriptor, addresses and routes. `ip` is never
invoked.

It genuinely works — `test_tcp_traffic_crosses_the_veth_pair` builds a veth
pair, moves one end into a second network namespace, addresses both, and moves
a byte across a real TCP connection.

And it stops where privilege stops. `CAP_NET_ADMIN` is held only over network
namespaces owned by a user namespace *this process created*, so containers can
be wired to each other and **cannot** be attached to the host's real network.
Rootless Docker and Podman solve that with `slirp4netns` or `pasta` — a
userspace TCP/IP stack on a tun device — which is a second network stack, not a
smaller version of this one. `minicon check` reports the limitation rather than
failing obscurely later:

```
host networking      : no (unprivileged: CAP_NET_ADMIN is held only over network
                       namespaces owned by a user namespace this process created...)
```

## The rootless UID problem

An unprivileged process may write **exactly one line** into `uid_map`, and it
must map its own UID. One line means one UID, so a container can be root, or it
can be uid 10001, but it cannot be root that later drops to uid 10001.

Three strategies, chosen automatically from the image's `USER`:

| strategy | mapping | needs |
|---|---|---|
| `root` | `0 → your uid` | nothing |
| `single` | `10001 → your uid` | nothing |
| `subid` | `0..65535 → /etc/subuid range` | `newuidmap` (package: `uidmap`) |

On a host without `newuidmap`, `USER 10001:10001` selects `single`: the
workload really does run as uid 10001, at the cost of uid 0 not existing inside
the container. Choosing `root` instead would silently run as root — the exact
thing the directive asked to prevent — so the runtime picks the honest option
and reports it. The `subid` path is implemented and its command construction is
tested; the helper is not installed here, and the code says so instead of
pretending.

## Usage

```bash
python3 -m minicon check                                  # what this host supports
python3 -m minicon --store ./images images
python3 -m minicon --store ./images run serve:v1
python3 -m minicon --store ./images run --memory 256M --cpu 0.5 --pids 64 -v serve:v1
python3 -m minicon --store ./images run --init --readonly serve:v1 -- /bin/sh -c 'echo hi'
```

As a library:

```python
from minicon import Container, ContainerConfig, ImageStore, Limits

store = ImageStore("./images")                    # an OCI layout, e.g. from #18
config = ContainerConfig(
    image="serve:v1",
    limits=Limits(memory_bytes=256 << 20, cpu_quota=0.5, pids_max=64),
    use_init=True,
)
with Container(config, store, "./.run").create() as container:
    container.start()
    print(container.phases.table())
    code = container.wait()
    print(container.usage())
```

`minicon check` on this machine:

```
user namespaces       : yes (available)
unprivileged overlayfs: yes (needs Linux 5.11+)
cgroup v2 delegation  : yes (/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service)
  controllers         : cpu, memory, pids
host networking       : no (unprivileged)
newuidmap/newgidmap   : no (install the uidmap package for multi-UID containers)
```

## PID 1

By default the entrypoint *is* PID 1, because that is what the image asked for.
It also hands the application two jobs it was probably not written to do:
reaping orphaned children, and dying on SIGTERM. PID 1 gets no default signal
dispositions, so a process with no SIGTERM handler ignores it entirely and
every shutdown becomes a SIGKILL after the grace period.

`--init` installs a ~40-line supervisor as PID 1 that forwards signals and
reaps orphans. Both behaviours are tested, including the case that matters:
a container whose PID 1 does `trap '' TERM` is still stopped, because `kill()`
falls back to `cgroup.kill` — which enumerates every process regardless of what
PID 1 did or failed to do.

## Running it

```bash
python3 -m pytest tests/ -q        # 78 tests
python3 bench/startup.py --count 20 --weights-mb 64
```

Tests that need a user namespace skip rather than fail where the host forbids
one — a CI box with `kernel.unprivileged_userns_clone=0` is not a broken
runtime. The test image is built from the host's statically linked `busybox`,
so the fixture needs nothing installed and a container test runs a real
container.

Requires Linux 5.11+ (unprivileged overlayfs), cgroup v2 with a delegated
subtree (systemd gives every user one), and Python 3.10+. Standard library
only — `ctypes` for the syscalls, `socket` for netlink.

Note: a restricted sandbox that denies `uid_map` writes cannot run this suite —
user namespaces are the feature under test, so run it on the host.

## Relationship to the other modules

- **`image-toolkit`** produces the OCI layouts this consumes, and `image.py`
  reads them with the standard library rather than importing that package — an
  image format whose only reader is its own writer has not been tested against
  anything. It also closes that module's two stated gaps: `RunExecutor` gets a
  real sandbox, and cold start gets measured inside the container.
- **`orchestrator`** schedules these containers across nodes, with cluster state
  in `raft-kv`.

## License

MIT
