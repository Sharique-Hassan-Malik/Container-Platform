"""Raw Linux syscalls, via ctypes.

A container is not a kernel object. There is no `create_container()`. It is a
process that has had six unrelated syscalls applied to it, and this module is
the complete list of them:

    unshare(2)      detach namespaces from the parent
    mount(2)        assemble a root filesystem, and make it private
    pivot_root(2)   swap the process's idea of "/"
    sethostname(2)  the only thing the UTS namespace holds
    setns(2)        join an existing namespace (how `exec` into a container works)
    umount2(2)      detach the old root once nothing needs it

Python exposes `os.setns` (3.12+) and nothing else here, so everything is bound
through libc. Every wrapper checks the return value and raises `OSError` with
the real `errno`, because these calls fail for reasons that are precise and
worth reading: `EPERM` writing a uid_map means something different from `EPERM`
on mount, and debugging without the distinction is guesswork.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import sys

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)

# ---------------------------------------------------------------------------
# clone / unshare flags  (linux/sched.h)
# ---------------------------------------------------------------------------

CLONE_NEWNS = 0x00020000      # mount namespace -- the original one, hence no 'MNT'
CLONE_NEWCGROUP = 0x02000000
CLONE_NEWUTS = 0x04000000
CLONE_NEWIPC = 0x08000000
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000

NAMESPACES = {
    "mnt": CLONE_NEWNS,
    "cgroup": CLONE_NEWCGROUP,
    "uts": CLONE_NEWUTS,
    "ipc": CLONE_NEWIPC,
    "user": CLONE_NEWUSER,
    "pid": CLONE_NEWPID,
    "net": CLONE_NEWNET,
}

# ---------------------------------------------------------------------------
# mount flags  (sys/mount.h)
# ---------------------------------------------------------------------------

MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REMOUNT = 32
MS_BIND = 4096
MS_MOVE = 8192
MS_REC = 16384
MS_PRIVATE = 1 << 18
MS_SLAVE = 1 << 19
MS_SHARED = 1 << 20
MS_RELATIME = 1 << 21

MNT_DETACH = 2

# Sensible defaults for the pseudo-filesystems every container needs. `nosuid`
# and `nodev` are not decoration: without them a writable /dev or /tmp is a
# privilege-escalation surface even inside a user namespace.
PSEUDO_FLAGS = MS_NOSUID | MS_NODEV | MS_NOEXEC


def _check(result: int, call: str, *context) -> int:
    if result != 0:
        code = ctypes.get_errno()
        detail = " ".join(str(c) for c in context if c is not None)
        raise OSError(code, f"{call}({detail}) failed: {os.strerror(code)}")
    return result


# ---------------------------------------------------------------------------
# syscalls
# ---------------------------------------------------------------------------


def unshare(flags: int) -> None:
    """Detach the calling process from the namespaces named in `flags`.

    Note the asymmetry that shapes every container runtime: after
    `unshare(CLONE_NEWPID)` the caller is *not* in the new PID namespace -- its
    next child is. That is why creating a container needs a fork after the
    unshare, and why the process that becomes PID 1 is a grandchild of the
    runtime rather than a child.
    """
    _check(_libc.unshare(ctypes.c_int(flags)), "unshare", hex(flags))


def mount(source: str, target: str, fstype: str | None, flags: int = 0, data: str | None = None) -> None:
    _check(
        _libc.mount(
            source.encode() if source else None,
            target.encode(),
            fstype.encode() if fstype else None,
            ctypes.c_ulong(flags),
            data.encode() if data else None,
        ),
        "mount",
        source,
        target,
        fstype,
        data,
    )


def umount(target: str, flags: int = 0) -> None:
    _check(_libc.umount2(target.encode(), ctypes.c_int(flags)), "umount2", target)


def pivot_root(new_root: str, put_old: str) -> None:
    """Swap "/" for `new_root`, leaving the old root visible at `put_old`.

    `pivot_root` is preferred over `chroot` because it changes the mount tree
    rather than one process's idea of a path. A chrooted process that still
    holds a descriptor to a directory outside the jail can walk back out;
    after a pivot the old root is a mount that can be detached outright, and
    then it is genuinely gone.

    Both arguments must be directories, `new_root` must be a mount point, and
    `put_old` must be underneath it. All three conditions produce `EINVAL`.
    """
    # glibc has no pivot_root wrapper -- it must go through syscall(2).
    SYS_pivot_root = 155  # x86_64
    _check(
        _libc.syscall(ctypes.c_long(SYS_pivot_root), new_root.encode(), put_old.encode()),
        "pivot_root",
        new_root,
        put_old,
    )


def sethostname(name: str) -> None:
    encoded = name.encode()
    _check(_libc.sethostname(encoded, ctypes.c_size_t(len(encoded))), "sethostname", name)


def setns(fd: int, nstype: int = 0) -> None:
    _check(_libc.setns(ctypes.c_int(fd), ctypes.c_int(nstype)), "setns", fd, hex(nstype))


def make_root_private() -> None:
    """Stop mount events propagating back to the host.

    systemd mounts `/` shared, so a fresh mount namespace still forwards every
    mount and unmount to its parent. Without this the container's `/proc` shows
    up on the host and, worse, the container's unmounts take the host's mounts
    with them. This one line is the difference between an isolated mount tree
    and a very confusing afternoon.
    """
    mount("none", "/", None, MS_REC | MS_PRIVATE)


# ---------------------------------------------------------------------------
# capability and namespace introspection
# ---------------------------------------------------------------------------


def namespace_ids(pid: int | str = "self") -> dict[str, str]:
    """Read /proc/<pid>/ns/* -- the identity of each namespace as `type:[inode]`.

    Two processes share a namespace exactly when these strings match, which
    makes this the only honest way to test isolation. `bench/isolation.py`
    compares them rather than trusting that `unshare` returned zero.
    """
    out: dict[str, str] = {}
    base = f"/proc/{pid}/ns"
    for name in ("user", "mnt", "pid", "net", "uts", "ipc", "cgroup"):
        try:
            out[name] = os.readlink(os.path.join(base, name))
        except OSError:
            continue
    return out


def effective_capabilities(pid: int | str = "self") -> int:
    with open(f"/proc/{pid}/status") as handle:
        for line in handle:
            if line.startswith("CapEff:"):
                return int(line.split()[1], 16)
    return 0


CAP_SYS_ADMIN = 21
CAP_NET_ADMIN = 12
CAP_SETUID = 7


def has_capability(bit: int, pid: int | str = "self") -> bool:
    return bool(effective_capabilities(pid) & (1 << bit))


# ---------------------------------------------------------------------------
# support detection
# ---------------------------------------------------------------------------


def userns_available() -> tuple[bool, str]:
    """Can this machine create an unprivileged user namespace?

    Three separate mechanisms say no, with three different symptoms, and a
    runtime that reports "permission denied" for all of them is unusable:

      * `user.max_user_namespaces = 0`   -- the namespace is never created
      * Debian's `kernel.unprivileged_userns_clone = 0` -- same
      * Ubuntu's AppArmor restriction    -- unshare succeeds, then the uid_map
                                            write fails with EPERM
    """
    try:
        with open("/proc/sys/user/max_user_namespaces") as handle:
            if int(handle.read().strip()) == 0:
                return False, "user.max_user_namespaces is 0"
    except OSError:
        pass
    for path, message in (
        ("/proc/sys/kernel/unprivileged_userns_clone", "kernel.unprivileged_userns_clone is 0"),
        ("/proc/sys/kernel/apparmor_restrict_unprivileged_userns", "AppArmor restricts unprivileged user namespaces"),
    ):
        try:
            with open(path) as handle:
                value = int(handle.read().strip())
        except (OSError, ValueError):
            continue
        if path.endswith("userns_clone") and value == 0:
            return False, message
        if path.endswith("unprivileged_userns") and value == 1:
            return False, message
    return True, "available"


def fork_thread_hazard() -> tuple[bool, str]:
    """Whether something in this process will put threads in its forked children.

    `unshare(CLONE_NEWUSER)` needs the caller to be the only thread in its
    thread group. A forked child normally is — POSIX keeps only the calling
    thread — so forking then unsharing is the standard way to build a
    container. It stops being true when a library has registered a
    `pthread_atfork` handler that *starts* threads in the child.

    gRPC does, whenever a server or channel is live:

        baseline                  child_threads=1  unshare=0
        after grpc server start   child_threads=6  unshare=-1 errno=22

    Detected rather than probed, because probing costs a fork on every
    container start and the condition is exactly knowable: gRPC only installs
    those handlers when its fork support is enabled.
    """
    if "grpc" in sys.modules and os.environ.get("GRPC_ENABLE_FORK_SUPPORT") != "0":
        return True, (
            "gRPC is loaded in this process with fork support enabled, so its "
            "pthread_atfork handler starts threads in every forked child, and "
            "unshare(CLONE_NEWUSER) then fails with EINVAL. Set "
            "GRPC_ENABLE_FORK_SUPPORT=0 before importing grpc — the container "
            "child never uses gRPC, so it loses nothing."
        )
    return False, ""


def overlayfs_in_userns() -> bool:
    """Unprivileged overlayfs needs Linux 5.11+. Older kernels return EPERM."""
    try:
        with open("/proc/version") as handle:
            release = handle.read().split()[2]
        major, minor = (int(part) for part in release.split(".")[:2])
    except (OSError, ValueError, IndexError):
        return False
    return (major, minor) >= (5, 11)


class NotSupported(RuntimeError):
    """Raised when the host cannot provide an isolation primitive.

    Distinct from OSError on purpose: the caller can degrade (skip a test, drop
    a namespace) rather than treating a policy decision as a bug.
    """
