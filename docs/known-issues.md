# Known issues

What is not fixed, and what was. Everything here is reachable from the test
suite; nothing is a rumour.

`pytest` runs 307 tests and skips none of them.

---

## Fixed: gRPC and user namespaces in one process

**Reproduce (before the fix):** run any module that starts a gRPC server, then
start a container in the same process.

This is the bug the repository exists to have found, because it only appears
when the modules are composed — which is exactly what `ctl up --store raft
--runtime container` does.

`unshare(CLONE_NEWUSER)` returns `EINVAL` unless the calling process is the
only thread in its thread group. A container runtime therefore forks first,
because a forked child is single-threaded — POSIX keeps only the calling
thread. That is airtight until a library registers a `pthread_atfork` handler
that *starts threads in the child*. gRPC does:

```
baseline                  child_threads=1  unshare=0
after import grpc         child_threads=1  unshare=0
after grpc server start   child_threads=6  unshare=-1 errno=22
```

Six threads in a forked child, before a line of Python runs in it. Every
container start then failed with `Invalid argument` raised four frames deep in
a namespace helper, naming nothing connected to gRPC.

Three changes, in decreasing order of how much they matter:

1. **`ctl/__init__.py` sets `GRPC_ENABLE_FORK_SUPPORT=0`** before anything
   imports grpc. That is the documented switch for exactly this: gRPC installs
   the fork handlers so a forked child can keep using gRPC, and our forked
   child unshares and execs a container — it never speaks gRPC, so it loses
   nothing. The platform is the right place for it because the platform is what
   puts the two in one process.
2. **Layer unpacking and cleanup re-exec a helper** (`minicon/nshelper.py`)
   rather than relying on fork alone. An `exec`'d process has exactly one
   thread and none of the parent's fork handlers, whatever the parent loaded —
   which is also why runc has `nsenter`. The cost is that the work crosses an
   exec, so those operations are described as JSON rather than passed as
   closures.
3. **`minicon.linux.fork_thread_hazard()`** detects the condition and the
   container child reports it up the pipe, so if the setting is ever lost the
   error names gRPC instead of saying `Invalid argument`.

Pinned by `tests/test_integration.py::test_a_forked_child_is_single_threaded_while_grpc_serves`,
which starts a real gRPC server and measures the child.

## Fixed: the capability gate that hid it

Forty-three namespace tests skipped themselves whenever the suite ran alongside
gRPC, with this reason:

> user namespaces need a single-threaded process; the kernel reports 10 threads
> in this one. Run this module's tests on their own.

The statement is true about the syscall and false about these tests: nothing
here unshares in the test process — the runtime and the helpers all fork first.
So the gate was measuring the wrong process, and it was hiding the real bug
above, which is the worst thing a skip can do.

The gate is gone. `single_threaded()` survives as a diagnostic, and
`test_units.py::test_unshare_is_only_ever_called_after_a_fork` walks the AST of
`minicon/` and `tests/` to assert that every `unshare` call really is reachable
only from a forked child — because the next person to add one in the wrong
place would not see a failure, they would see the tests quietly start skipping
again.

## Fixed: concurrent unpacks of one layer destroyed each other

Four replicas of one image start at once, all find the same layer missing, and
all four unpack it into the same directory — each beginning by deleting what
the others are using. The container that loses gets:

```
minicon: exec /bin/busybox: No such file or directory
```

Intermittently, on a rollout, which is the worst way to find a bug. It was
invisible with `--runtime simulated` because nothing unpacks.

The unpack helper now takes an exclusive `flock` per layer digest and re-checks
readiness while holding it, so the check and the unpack are one step: whoever
gets there first does the work and the rest wait and find it ready. Pinned by
`test_concurrent_unpacks_of_one_layer_do_not_destroy_each_other`, which fails
without the lock.

## Fixed: `ctl run --store` was rejected

`ctl run` delegates to `minicon`'s own CLI with the subcommand already in
place, so `--store` arrived *after* `run` while `minicon` only accepted it
before. `minicon` now accepts both, using `argparse.SUPPRESS` on the
subcommand copy so that a value given before the subcommand is not silently
overwritten by the subparser's default.

## Fixed: a missing image store said `FileNotFoundError`

`ctl up --runtime container --image-store ./oci` failed if `./oci` was not an
OCI layout — correctly, since there is nothing to run — but it surfaced three
frames inside the runtime, naming a path and no way to fix it.

Validated in `build_runtime` now, which is where the caller passes the path:

```
ctl: runtime backend 'container' needs an OCI image layout at nope, and
nope/index.json is not there. Build one:
    ctl image --store nope build ./context -t serve:v1
or point --image-store at a layout you already have.
```

Deliberately *not* in `Backend.check()`. The runtime is perfectly usable on
this host; you have simply not built an image. Reporting that as "container
backend unavailable" in `ctl status` would be a lie about the host.

## Fixed: `run_in_userns` under the fork hazard

The general entry point forks and unshares in the child, so it inherited the
gRPC hazard above. It now detects the condition before forking and falls back
to `run_helper`, pickling the callable into a freshly exec'd process, which is
single-threaded by construction.

That fallback needs the callable to be picklable and its module importable, so
a lambda cannot survive it — and the error says exactly that, naming
`run_helper` as the alternative, rather than surfacing an EINVAL. `run_helper`
also passes the parent's import roots through `PYTHONPATH`, because a pickled
callable is useless in a child that cannot import the module defining it.

Both paths are tested against a live gRPC server.

---

## Not fixed

### The task queue dashboard is not wired into `ctl up`

`ctl queue dashboard` runs it standalone. Attaching it to a running control
plane would mean the plane exporting queue metrics, which it does not.
