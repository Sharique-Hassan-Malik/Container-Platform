"""Building the container's root filesystem, then becoming it.

The root is an overlay, not a copy:

    upperdir  ── the container's writes, thrown away on delete
    lowerdir  ── the image's layers, read-only, shared by every container
    workdir   ── overlayfs scratch, must be an empty dir on the upper filesystem

That is why starting the tenth container from an image costs nothing: the layers
are already unpacked and every container adds one empty upper directory. Copying
a 500 MB rootfs per container instead would make scale-to-zero pointless.

The lowerdir ordering is the part everyone gets backwards. OCI layers are listed
bottom-first; overlayfs takes them **top-first**. Reverse the list and the base
image silently wins every conflict with the application layer.

Then `pivot_root` makes it "/" -- not `chroot`, which only changes a path
lookup and can be escaped by a process holding an open descriptor outside it.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from .linux import (
    MNT_DETACH,
    MS_BIND,
    MS_NODEV,
    MS_NOEXEC,
    MS_NOSUID,
    MS_RDONLY,
    MS_REC,
    MS_REMOUNT,
    PSEUDO_FLAGS,
    NotSupported,
    mount,
    pivot_root,
    umount,
)

# Devices every process expects. mknod is not permitted in a user namespace, so
# each one is bind-mounted from the host instead of created -- the same trick
# every rootless runtime uses.
DEFAULT_DEVICES = ("null", "zero", "full", "random", "urandom", "tty")


@dataclass
class Mount:
    source: str
    target: str          # absolute, inside the container
    fstype: str | None
    flags: int = 0
    data: str | None = None
    optional: bool = False

    def apply(self, root: str) -> bool:
        target = os.path.join(root, self.target.lstrip("/"))
        try:
            if self.flags & MS_BIND:
                if not os.path.exists(self.source):
                    if self.optional:
                        return False
                    raise FileNotFoundError(self.source)
                if os.path.isdir(self.source):
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    if not os.path.exists(target):
                        open(target, "w").close()
            else:
                os.makedirs(target, exist_ok=True)
            mount(self.source, target, self.fstype, self.flags, self.data)
            return True
        except OSError:
            if self.optional:
                return False
            raise


@dataclass
class OverlayRoot:
    """Where a container's root filesystem lives on the host."""

    merged: str
    upper: str
    work: str
    lowers: list[str] = field(default_factory=list)

    @classmethod
    def prepare(cls, base: str, lowers: list[str]) -> "OverlayRoot":
        root = cls(
            merged=os.path.join(base, "merged"),
            upper=os.path.join(base, "upper"),
            work=os.path.join(base, "work"),
            lowers=list(lowers),
        )
        for path in (root.merged, root.upper, root.work):
            os.makedirs(path, exist_ok=True)
        return root

    def options(self) -> str:
        if not self.lowers:
            raise ValueError("overlay needs at least one lower directory")
        # Top-first: the last image layer must win.
        lowerdir = ":".join(reversed(self.lowers))
        for path in self.lowers:
            if ":" in path or "," in path:
                raise ValueError(f"overlayfs cannot express a layer path containing ':' or ',': {path}")
        return f"lowerdir={lowerdir},upperdir={self.upper},workdir={self.work}"

    def mount(self) -> None:
        try:
            mount("overlay", self.merged, "overlay", 0, self.options())
        except OSError as exc:
            raise NotSupported(
                f"overlayfs mount failed ({exc.strerror}). Unprivileged overlayfs needs "
                "Linux 5.11+ and a user namespace; on older kernels a runtime must fall "
                "back to copying each layer into a private rootfs."
            ) from exc

    def unmount(self) -> None:
        try:
            umount(self.merged, MNT_DETACH)
        except OSError:
            pass

    def diff(self) -> list[str]:
        """Paths the container wrote -- the upper layer, which is its whole delta."""
        out: list[str] = []
        for dirpath, _, filenames in os.walk(self.upper):
            for name in filenames:
                path = os.path.join(dirpath, name)
                out.append("/" + os.path.relpath(path, self.upper))
        return sorted(out)

    def cleanup(self) -> None:
        """Remove the container's private directories.

        `workdir` is the awkward one. overlayfs creates `workdir/work` itself,
        with kernel credentials: root:root, mode 000. An unprivileged process
        cannot remove it from outside, even though it created the parent -- so
        cleanup re-enters a user namespace where it is root, exactly as
        unpacking does. Leaving it behind would leak a directory per container.
        """
        self.unmount()
        targets = [self.upper, self.work, self.merged]
        for path in targets:
            shutil.rmtree(path, ignore_errors=True)
        remaining = [path for path in targets if os.path.exists(path)]
        if not remaining:
            return
        from .image import run_in_userns

        def remove_as_root() -> None:
            for path in remaining:
                shutil.rmtree(path, ignore_errors=True)

        try:
            run_in_userns(remove_as_root)
        except RuntimeError:
            # A leaked work directory is untidy, not incorrect; refusing to
            # delete the container over it would be worse.
            pass


def default_mounts(*, readonly_root: bool = False, tmpfs_size: str = "64m") -> list[Mount]:
    """The filesystems a container needs before anything else will work.

    `/proc` is the interesting one: mounting it requires being in a *PID*
    namespace, not a mount namespace. Without one the kernel refuses, and
    without `/proc` almost nothing in userspace runs -- which is why PID
    isolation is effectively mandatory rather than optional.
    """
    mounts = [
        Mount("proc", "/proc", "proc", PSEUDO_FLAGS & ~MS_NOEXEC),
        Mount("tmpfs", "/dev", "tmpfs", MS_NOSUID, f"size={tmpfs_size},mode=755"),
        Mount("devpts", "/dev/pts", "devpts", MS_NOSUID | MS_NOEXEC, "newinstance,ptmxmode=0666,mode=620", optional=True),
        Mount("tmpfs", "/dev/shm", "tmpfs", PSEUDO_FLAGS, f"size={tmpfs_size}"),
        Mount("tmpfs", "/tmp", "tmpfs", MS_NOSUID | MS_NODEV, f"size={tmpfs_size}"),
        # sysfs cannot be mounted in a user namespace unless the network
        # namespace is owned by it, which is why it is optional here.
        Mount("sysfs", "/sys", "sysfs", PSEUDO_FLAGS | MS_RDONLY, optional=True),
    ]
    for device in DEFAULT_DEVICES:
        mounts.append(Mount(f"/dev/{device}", f"/dev/{device}", None, MS_BIND, optional=True))
    # `readonly_root` is deliberately not handled here. Sealing the root before
    # pivot_root would make pivot_root fail: it has to mkdir the directory the
    # old root gets parked in, and that mkdir lands on the root being sealed.
    # See `remount_root_readonly`, called after the pivot.
    return mounts


def remount_root_readonly() -> None:
    """Seal "/" after pivoting into it.

    Ordering is forced: pivot_root needs a writable root to create `put_old`,
    so the seal can only happen once the process is already inside. The tmpfs
    mounts for /tmp and /dev/shm are separate mounts and stay writable, which is
    the combination a hardened service actually wants -- immutable code, mutable
    scratch.
    """
    mount("none", "/", None, MS_REMOUNT | MS_BIND | MS_RDONLY)


def enter_root(root: str) -> None:
    """`pivot_root` into `root` and detach everything outside it.

    The sequence is fixed by the kernel's preconditions, each of which produces
    EINVAL if skipped:

      1. `root` must be a *mount point*, so bind-mount it onto itself.
      2. `put_old` must live inside `root`.
      3. After pivoting, chdir to "/" or the process keeps a cwd on the old root.
      4. Detach the old root lazily, then remove the directory. Until this
         happens the host filesystem is still reachable at `/.oldroot`, which
         means the container is not yet isolated at all.
    """
    mount(root, root, None, MS_BIND | MS_REC)
    old_root = os.path.join(root, ".oldroot")
    os.makedirs(old_root, exist_ok=True)

    pivot_root(root, old_root)
    os.chdir("/")
    umount("/.oldroot", MNT_DETACH)
    try:
        os.rmdir("/.oldroot")
    except OSError:
        pass


def apply_mounts(root: str, mounts: list[Mount]) -> list[str]:
    """Apply mounts in order, returning the targets that were skipped."""
    skipped: list[str] = []
    for entry in mounts:
        if not entry.apply(root):
            skipped.append(entry.target)
    return skipped


def write_container_files(root: str, hostname: str, extra_hosts: dict[str, str] | None = None) -> None:
    """The three files that make name resolution behave inside a container.

    An image built `FROM scratch` has none of them, and the failure mode is a
    process that hangs on DNS rather than one that reports a missing file.
    """
    etc = os.path.join(root, "etc")
    os.makedirs(etc, exist_ok=True)

    hosts = ["127.0.0.1\tlocalhost", f"127.0.1.1\t{hostname}", "::1\tlocalhost ip6-localhost ip6-loopback"]
    for name, address in (extra_hosts or {}).items():
        hosts.append(f"{address}\t{name}")
    _write_if_absent(os.path.join(etc, "hosts"), "\n".join(hosts) + "\n")
    _write_if_absent(os.path.join(etc, "hostname"), hostname + "\n")

    resolv = "/etc/resolv.conf"
    if os.path.exists(resolv):
        try:
            with open(resolv) as handle:
                content = handle.read()
        except OSError:
            content = "nameserver 1.1.1.1\n"
    else:
        content = "nameserver 1.1.1.1\n"
    _write_if_absent(os.path.join(etc, "resolv.conf"), content)


def _write_if_absent(path: str, content: str) -> None:
    # The image may ship its own; overwriting it would be the runtime silently
    # editing the user's filesystem.
    if os.path.exists(path):
        return
    try:
        with open(path, "w") as handle:
            handle.write(content)
    except OSError:
        pass
