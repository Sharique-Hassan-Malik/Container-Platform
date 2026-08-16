"""A clean process that enters a user namespace and does one job.

`fork()` is not enough, and the reason is worth stating precisely because it
cost a long afternoon.

`unshare(CLONE_NEWUSER)` returns EINVAL unless the calling process is the only
thread in its thread group. A forked child is normally exactly that — POSIX
says only the calling thread survives — so forking and then unsharing looks
airtight, and it is, right up until something in the parent has registered a
`pthread_atfork` handler that *starts threads in the child*.

gRPC does. With a server running, every `fork()` from that process produces a
child with six threads before a single line of Python runs in it:

    baseline                  child_threads=1  unshare=0
    after import grpc         child_threads=1  unshare=0
    after grpc server start   child_threads=6  unshare=-1 errno=22

This is not hypothetical for this platform: `ctl up --store raft --runtime
container` runs the Raft client and the container runtime in one process, so
the runtime must work with gRPC loaded and serving.

`exec()` is the only way to be sure. A freshly exec'd process has exactly one
thread and none of the parent's fork handlers, whatever the parent had loaded.
That is also why real runtimes re-exec themselves — runc's `nsenter` exists for
the same reason.

The cost is that the work has to cross an exec, so it is described as data
rather than passed as a closure. Every operation this helper supports is
therefore a small JSON document, read on stdin.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys


def _unpack(spec: dict) -> None:
    """Unpack layer blobs into the layer cache, as root inside the namespace.

    Needs the namespace because a layer can contain device nodes and files
    owned by other uids, and `mknod`/`chown` are refused to an ordinary user.

    Locked per layer, because the cache is shared and the callers are not
    coordinated. Four replicas of one image start at once, all four find the
    same layer missing, and all four unpack it into the same directory — each
    one beginning by deleting what the others are using. The container that
    loses gets `exec: No such file or directory`, intermittently, on a rollout.

    The lock makes the check and the unpack one step: whoever gets there first
    does the work, and the rest wait and then find it ready.
    """
    from .image import ImageStore, LayerCache, unpack_layer

    store = ImageStore(spec["store_root"])
    cache = LayerCache(spec["cache_root"])
    for digest in spec["digests"]:
        target = cache.path_for(digest)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(cache.lock_for(digest), "w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if cache.is_ready(digest):
                continue                      # another process won the race
            shutil.rmtree(target, ignore_errors=True)
            os.makedirs(target, exist_ok=True)
            unpack_layer(store.read_blob(digest), target)
            with open(cache.marker_for(digest), "w") as handle:
                handle.write(digest)


def _remove(spec: dict) -> None:
    """Delete directories whose contents this user cannot otherwise unlink.

    An overlay upper layer holds files created by the container's root, so the
    calling user gets EPERM on them.
    """
    for path in spec["paths"]:
        shutil.rmtree(path, ignore_errors=True)


def _call(spec: dict) -> None:
    """Run a pickled callable inside the namespace.

    The escape hatch for `run_in_userns`, whose argument is a callable and so
    cannot cross an exec on its own. Only reached when forking cannot produce a
    single-threaded child; the fork path stays the default because it needs no
    pickling and no import of the caller's module.

    The payload comes from this same process tree, so unpickling it is not a
    trust boundary — it is the same code, one exec later.
    """
    import base64
    import pickle

    pickle.loads(base64.b64decode(spec["callable"]))()


OPERATIONS = {"unpack": _unpack, "remove": _remove, "call": _call}


def main(argv: list[str] | None = None) -> int:
    """Read one operation from stdin, enter a user namespace, run it.

    The exit status is the whole protocol: zero means the operation completed,
    non-zero means it did not and stderr says why. Deliberately narrow —
    anything richer would have to survive an exec as well as a fork.
    """
    from . import idmap, linux

    try:
        spec = json.loads(sys.stdin.read())
        operation = OPERATIONS[spec["op"]]
    except (ValueError, KeyError) as exc:
        print(f"nshelper: bad request: {exc}", file=sys.stderr)
        return 2

    # Read before unsharing, and prefer what the caller measured. The kernel
    # requires the single map line to name the writer's uid *in the parent
    # namespace*, and after `unshare` this process is unmapped — `os.getuid()`
    # then reports the overflow uid, 65534, and writing `0 65534 1` is refused
    # with EPERM. This is the whole bug, and it is invisible: the number looks
    # like a uid.
    outside_uid = spec.get("uid", os.getuid())
    outside_gid = spec.get("gid", os.getgid())

    try:
        linux.unshare(linux.CLONE_NEWUSER | linux.CLONE_NEWNS)
    except OSError as exc:
        threads = _thread_count()
        extra = (f" — this process has {threads} threads, which should be "
                 "impossible after exec") if threads > 1 else ""
        print(f"nshelper: {exc}{extra}", file=sys.stderr)
        return 1

    try:
        idmap.write_maps(os.getpid(), idmap.root_mapping(outside_uid, outside_gid))
        linux.make_root_private()
        operation(spec)
    except BaseException as exc:  # noqa: BLE001 — the status is the channel out
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def _thread_count() -> int:
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
