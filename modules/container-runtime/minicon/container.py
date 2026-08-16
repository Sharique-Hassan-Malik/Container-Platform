"""Container lifecycle: create, start, wait, kill, delete.

The whole thing is a dance between three processes, and the shape is forced by
one kernel rule: `unshare(CLONE_NEWPID)` does not move the caller into the new
PID namespace, only its children. So the process that becomes PID 1 must be a
*grandchild* of the runtime.

    runtime (P)                 child (C)                    init (G)
    ────────────────────────────────────────────────────────────────────────
    prepare rootfs, cgroup
    fork ────────────────────▶  join cgroup leaf
                                unshare(user|mnt|pid|net|uts|ipc|cgroup)
                   ◀── "ready" ─┤
    write uid_map/gid_map
    ├── "go" ──────────────────▶ (now uid 0 in its own user namespace)
                                fork ──────────────────────▶ PID 1
                                                             mount overlay
                                                             mount /proc /dev /sys
                                                             pivot_root
                                                             sethostname, lo up
                                                             exec entrypoint
                                waitpid(G) ◀────────────────
    waitpid(C) ◀── exit status ─┤

C exists only because of that PID-namespace rule, and it earns its keep: it is
outside the container's PID namespace but inside its user namespace, which makes
it the only process that can both observe G exiting and be signalled by the
runtime.

The maps must be written by P, not by C. An unprivileged process may write only
one mapping line, naming its own UID -- and after `unshare(CLONE_NEWUSER)` C's
UID has already become `nobody`, so it can no longer name it. P still can.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import shutil
import signal
import time
from dataclasses import asdict, dataclass, field

from . import idmap, linux
from .cgroups import Cgroup, CgroupUnavailable, Limits, Usage
from .image import Image, ImageStore, LayerCache
from .mounts import (
    OverlayRoot, apply_mounts, default_mounts, enter_root,
    remount_root_readonly, write_container_files,
)
from .netlink import Netlink

CONTAINER_NAMESPACES = (
    linux.CLONE_NEWUSER
    | linux.CLONE_NEWNS
    | linux.CLONE_NEWPID
    | linux.CLONE_NEWNET
    | linux.CLONE_NEWUTS
    | linux.CLONE_NEWIPC
    | linux.CLONE_NEWCGROUP
)


@dataclass
class Phases:
    """Where container startup time goes, from the runtime's side.

    Reported alongside the payload's own `load` and `first token` markers so a
    cold start can be attributed to the runtime or to the model, which are
    optimised by completely different work.
    """

    # Measured by the runtime process
    resolve_s: float = 0.0
    unpack_s: float = 0.0
    cgroup_s: float = 0.0
    namespace_s: float = 0.0
    idmap_s: float = 0.0
    setup_s: float = 0.0        # "go" -> init is ready to exec

    # Measured by PID 1 and reported back over the ready pipe. These are a
    # breakdown *of* setup_s, not additions to it -- timing them from the parent
    # is impossible, since they happen inside namespaces it cannot observe.
    overlay_s: float = 0.0
    mounts_s: float = 0.0
    pivot_s: float = 0.0

    @property
    def total_s(self) -> float:
        return self.resolve_s + self.unpack_s + self.cgroup_s + self.namespace_s + self.idmap_s + self.setup_s

    def table(self) -> str:
        rows = [
            ("resolve image", self.resolve_s),
            ("unpack layers", self.unpack_s),
            ("create cgroup", self.cgroup_s),
            ("create namespaces", self.namespace_s),
            ("write id maps", self.idmap_s),
            ("container setup", self.setup_s),
            ("  . mount overlay", self.overlay_s),
            ("  . mount /proc /dev /sys", self.mounts_s),
            ("  . pivot_root", self.pivot_s),
        ]
        width = max(len(name) for name, _ in rows)
        lines = [
            f"{name:<{width}}  {value * 1000:7.1f} ms" + ("" if not name.startswith("  .") else "")
            for name, value in rows
        ]
        lines.append("-" * (width + 12))
        lines.append(f"{'runtime total':<{width}}  {self.total_s * 1000:7.1f} ms")
        lines.append("(indented rows break down container setup; they are not added again)")
        return "\n".join(lines)


@dataclass
class ContainerConfig:
    image: str
    name: str = ""
    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = ""
    user: str = ""
    hostname: str = ""
    limits: Limits = field(default_factory=Limits)
    readonly_root: bool = False
    use_init: bool = False
    network: str = "private"        # "private" (lo only) or "none"
    tmpfs_size: str = "64m"
    extra_hosts: dict[str, str] = field(default_factory=dict)
    # Host paths for the workload's output. Opened before pivot_root, because
    # afterwards the host filesystem is gone -- which is the point.
    stdout_path: str = ""
    stderr_path: str = ""


@dataclass
class ContainerState:
    id: str
    name: str
    image: str
    status: str = "created"
    pid: int = 0
    init_pid: int = 0
    exit_code: int | None = None
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    bundle: str = ""
    cgroup: str = ""
    argv: list[str] = field(default_factory=list)
    id_mapping: str = ""
    phases: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


class ContainerError(RuntimeError):
    pass


class Container:
    def __init__(self, config: ContainerConfig, store: ImageStore, workspace: str):
        self.config = config
        self.store = store
        self.workspace = os.path.abspath(workspace)
        self.id = secrets.token_hex(8)
        self.name = config.name or f"minicon-{self.id[:8]}"
        self.bundle = os.path.join(self.workspace, "containers", self.name)
        self.phases = Phases()
        self.image: Image | None = None
        self.overlay: OverlayRoot | None = None
        self.cgroup: Cgroup | None = None
        self.mapping: idmap.IdMapping | None = None
        self.child_pid = 0
        self.state = ContainerState(id=self.id, name=self.name, image=config.image, bundle=self.bundle)
        self._skipped_mounts: list[str] = []
        self._skipped_limits: list[str] = []

    # -- preparation ---------------------------------------------------------

    def create(self) -> "Container":
        started = time.perf_counter()
        self.image = self.store.get(self.config.image)
        self.phases.resolve_s = time.perf_counter() - started

        os.makedirs(self.bundle, exist_ok=True)
        cache = LayerCache(os.path.join(self.workspace, "layers"))
        started = time.perf_counter()
        lowers = cache.ensure(self.store, self.image)
        self.phases.unpack_s = time.perf_counter() - started
        self.layer_cache = cache

        self.overlay = OverlayRoot.prepare(self.bundle, lowers)

        started = time.perf_counter()
        try:
            self.cgroup = Cgroup(f"minicon-{self.name}").create()
            self._skipped_limits = self.cgroup.apply(self.config.limits)
            self.state.cgroup = self.cgroup.leaf
        except (CgroupUnavailable, OSError) as exc:
            # A container with no limits is still a container. Losing isolation
            # silently would not be acceptable; losing limits is, if it is said
            # out loud.
            self.cgroup = None
            self._skipped_limits = [f"cgroup unavailable: {exc}"]
        self.phases.cgroup_s = time.perf_counter() - started

        self.mapping = idmap.plan_for_user(self.config.user or self.image.user)
        self.state.id_mapping = self.mapping.describe()
        self.state.argv = self.config.argv or self.image.argv
        self.state.created_at = time.time()
        self._write_state()
        return self

    @property
    def skipped(self) -> dict[str, list[str]]:
        return {"mounts": self._skipped_mounts, "limits": self._skipped_limits}

    # -- start ---------------------------------------------------------------

    def start(self) -> int:
        """Fork the container. Returns the PID of the intermediate child C."""
        if self.image is None or self.overlay is None:
            raise ContainerError("create() must run before start()")

        argv = self.config.argv or self.image.argv
        if not argv:
            raise ContainerError(f"{self.config.image} has no entrypoint and none was given")

        ns_ready_r, ns_ready_w = os.pipe()      # C -> P: namespaces exist
        go_r, go_w = os.pipe()                  # P -> C: maps are installed
        started_r, started_w = os.pipe()        # G -> P: about to exec

        namespace_started = time.perf_counter()
        pid = os.fork()
        if pid == 0:
            os.close(ns_ready_r)
            os.close(go_w)
            os.close(started_r)
            self._child(ns_ready_w, go_r, started_w, argv)
            os._exit(127)                        # unreachable

        os.close(ns_ready_w)
        os.close(go_r)
        os.close(started_w)
        self.child_pid = pid

        signal_byte = os.read(ns_ready_r, 1)
        if signal_byte != b"R":
            detail = b""
            if signal_byte == b"E":
                while chunk := os.read(ns_ready_r, 4096):
                    detail += chunk
            os.close(ns_ready_r)
            os.close(go_w)
            os.waitpid(pid, 0)
            raise ContainerError(
                detail.decode() or "container child died before creating its namespaces")
        os.close(ns_ready_r)
        self.phases.namespace_s = time.perf_counter() - namespace_started

        idmap_started = time.perf_counter()
        try:
            idmap.write_maps(pid, self.mapping)
        except OSError as exc:
            os.close(go_w)
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            available, reason = linux.userns_available()
            raise ContainerError(
                f"could not write the ID mapping ({exc.strerror}). "
                + ("" if available else f"User namespaces are unavailable: {reason}.")
            ) from exc

        self.phases.idmap_s = time.perf_counter() - idmap_started

        setup_started = time.perf_counter()
        os.write(go_w, b"G")
        os.close(go_w)

        report = _read_line(started_r)
        os.close(started_r)
        self.phases.setup_s = time.perf_counter() - setup_started
        if not report:
            self.state.status = "failed"
            self._write_state()
            raise ContainerError(
                "container init exited before exec. Common causes: the image has no "
                "/proc mountpoint, overlayfs is unavailable, or the entrypoint does not "
                "exist inside the rootfs."
            )
        # PID 1 timed the phases the parent cannot see from outside the namespaces.
        for key in ("overlay_s", "mounts_s", "pivot_s"):
            setattr(self.phases, key, float(report.get(key, 0.0)))
        self._skipped_mounts = list(report.get("skipped_mounts", []))

        self.state.pid = pid
        self.state.status = "running"
        self.state.started_at = time.time()
        self.state.phases = asdict(self.phases)
        self._write_state()
        return pid

    # -- the two inner processes --------------------------------------------

    def _child(self, ns_ready_w: int, go_r: int, started_w: int, argv: list[str]) -> None:
        """Process C: owns the namespaces, supervises PID 1, never returns."""
        try:
            if self.cgroup is not None:
                # Join before unsharing the cgroup namespace, so PID 1 sees its
                # own cgroup as "/" rather than the host's full path.
                self.cgroup.add_process(os.getpid())

            try:
                linux.unshare(CONTAINER_NAMESPACES)
            except OSError as exc:
                # EINVAL here means this child is not alone in its thread
                # group, which a forked child is supposed to be. Say what put
                # the threads there rather than letting an "Invalid argument"
                # surface from four frames down.
                _, hazard = linux.fork_thread_hazard()
                detail = f"{exc}" + (f" — {hazard}" if hazard else "")
                os.write(ns_ready_w, b"E" + detail.encode()[:3000])
                os.close(ns_ready_w)
                os._exit(125)
            os.write(ns_ready_w, b"R")
            os.close(ns_ready_w)

            if os.read(go_r, 1) != b"G":
                os._exit(126)
            os.close(go_r)

            init_pid = os.fork()
            if init_pid == 0:
                self._init(started_w, argv)
                os._exit(127)

            os.close(started_w)

            # C must relay signals rather than act on them. Dying here would
            # orphan PID 1 -- the container would keep running with nothing
            # supervising it, and `kill()` would report success.
            def relay(signum, _frame):
                try:
                    os.kill(init_pid, signum)
                except ProcessLookupError:
                    pass

            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT,
                           signal.SIGUSR1, signal.SIGUSR2):
                signal.signal(signum, relay)

            while True:
                try:
                    _, status = os.waitpid(init_pid, 0)
                    break
                except InterruptedError:
                    # A relayed signal interrupted the wait; the child is still
                    # running and still ours to reap.
                    continue
            os._exit(os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status))
        except BaseException:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            os._exit(125)

    def _init(self, started_w: int, argv: list[str]) -> None:
        """Process G: PID 1 inside the container."""
        assert self.image is not None and self.overlay is not None
        # Opened here, while the host filesystem is still reachable.
        output_fds = [
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644) if path else -1
            for path in (self.config.stdout_path, self.config.stderr_path)
        ]
        started = time.perf_counter()
        linux.make_root_private()
        self.overlay.mount()
        overlay_s = time.perf_counter() - started

        merged = self.overlay.merged
        hostname = self.config.hostname or self.name
        started = time.perf_counter()
        write_container_files(merged, hostname, self.config.extra_hosts)
        skipped_mounts = apply_mounts(merged, default_mounts(tmpfs_size=self.config.tmpfs_size))
        mounts_s = time.perf_counter() - started

        started = time.perf_counter()
        enter_root(merged)
        pivot_s = time.perf_counter() - started
        linux.sethostname(hostname)
        if self.config.readonly_root:
            # Only now: pivot_root needed a writable root to park the old one in.
            remount_root_readonly()

        if self.config.network == "private":
            try:
                with Netlink() as netlink:
                    netlink.set_link_up("lo")
            except OSError:
                # A container with a down loopback still runs; anything binding
                # 127.0.0.1 will not.
                pass

        env = dict(self.image.env)
        env.update(self.config.env)
        env.setdefault("HOME", "/root")
        env.setdefault("HOSTNAME", hostname)

        workdir = self.config.workdir or self.image.working_dir or "/"
        try:
            os.chdir(workdir)
        except OSError:
            os.makedirs(workdir, exist_ok=True)
            os.chdir(workdir)

        os.write(
            started_w,
            (json.dumps({
                "overlay_s": overlay_s,
                "mounts_s": mounts_s,
                "pivot_s": pivot_s,
                "skipped_mounts": skipped_mounts,
            }) + "\n").encode(),
        )
        os.close(started_w)

        for target, fd in enumerate(output_fds, start=1):
            if fd >= 0:
                os.dup2(fd, target)
                os.close(fd)

        if self.config.use_init:
            _run_as_init(argv, env)
        else:
            _exec(argv, env)

    # -- lifecycle -----------------------------------------------------------

    def wait(self, timeout: float | None = None) -> int:
        if not self.child_pid:
            raise ContainerError("container is not running")
        deadline = None if timeout is None else time.time() + timeout
        while True:
            pid, status = os.waitpid(self.child_pid, os.WNOHANG if deadline else 0)
            if pid:
                code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)
                self.state.exit_code = code
                self.state.status = "exited"
                self.state.finished_at = time.time()
                self._write_state()
                return code
            if deadline and time.time() > deadline:
                raise TimeoutError(f"container {self.name} still running after {timeout}s")
            time.sleep(0.01)

    def kill(self, sig: int = signal.SIGTERM, grace: float = 10.0) -> int:
        """Signal the container, then make sure it is actually gone.

        Signalling PID 1 is a request, not a guarantee: a shell-form entrypoint
        makes `/bin/sh` PID 1, and `sh` does not forward signals to its child.
        The cgroup is the backstop -- it enumerates every process regardless of
        what PID 1 did or failed to do.
        """
        if not self.child_pid:
            return 0
        try:
            os.kill(self.child_pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.time() + grace
        while time.time() < deadline:
            try:
                pid, status = os.waitpid(self.child_pid, os.WNOHANG)
            except ChildProcessError:
                break
            if pid:
                code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)
                self.state.exit_code = code
                self.state.status = "exited"
                self._write_state()
                return code
            time.sleep(0.02)

        if self.cgroup is not None:
            self.cgroup.kill_all()
        try:
            os.kill(self.child_pid, signal.SIGKILL)
            os.waitpid(self.child_pid, 0)
        except OSError:
            pass
        self.state.status = "killed"
        self._write_state()
        return 137

    def usage(self) -> Usage:
        return self.cgroup.usage() if self.cgroup else Usage()

    def delete(self) -> None:
        if self.cgroup is not None:
            self.cgroup.kill_all()
            self.cgroup.destroy()
        if self.overlay is not None:
            self.overlay.cleanup()
        shutil.rmtree(self.bundle, ignore_errors=True)

    def __enter__(self) -> "Container":
        return self

    def __exit__(self, *_) -> None:
        if self.state.status == "running":
            self.kill()
        self.delete()

    # -- state ---------------------------------------------------------------

    def _write_state(self) -> None:
        os.makedirs(self.bundle, exist_ok=True)
        path = os.path.join(self.bundle, "state.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as handle:
            json.dump(self.state.to_json(), handle, indent=2)
        os.replace(tmp, path)


# ---------------------------------------------------------------------------
# PID 1
# ---------------------------------------------------------------------------


def _read_line(fd: int, limit: int = 65536) -> dict:
    """Read PID 1's readiness report. An empty read means it died first."""
    buffer = b""
    while b"\n" not in buffer and len(buffer) < limit:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        buffer += chunk
    if not buffer.strip():
        return {}
    try:
        return json.loads(buffer.split(b"\n", 1)[0])
    except json.JSONDecodeError:
        return {}


def _exec(argv: list[str], env: dict[str, str]) -> None:
    """Become the workload. PID 1 is now the application itself.

    This is the default because it is what the image asked for, but it hands
    the application two jobs it was probably not written to do: reaping orphaned
    children, and terminating on SIGTERM. PID 1 gets no default signal
    dispositions, so a process that never installed a SIGTERM handler ignores
    it -- and every shutdown becomes a SIGKILL after the grace period.
    """
    try:
        os.execvpe(argv[0], argv, env)
    except OSError as exc:
        message = f"minicon: exec {argv[0]}: {exc.strerror}\n"
        os.write(2, message.encode())
        os._exit(127 if exc.errno == errno.ENOENT else 126)


def _run_as_init(argv: list[str], env: dict[str, str]) -> None:
    """Stay PID 1 and supervise the workload: forward signals, reap orphans.

    Fifty lines that fix the two problems above. Orphaned processes anywhere in
    the container are re-parented to PID 1, and if nobody reaps them the process
    table fills with zombies until `pids.max` is hit -- a container that stops
    accepting work while looking perfectly healthy.
    """
    child = os.fork()
    if child == 0:
        _exec(argv, env)

    forwarded = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1, signal.SIGUSR2)

    def forward(signum, _frame):
        try:
            os.kill(child, signum)
        except ProcessLookupError:
            pass

    for signum in forwarded:
        signal.signal(signum, forward)

    while True:
        try:
            pid, status = os.wait()
        except ChildProcessError:
            os._exit(0)
        except InterruptedError:
            continue
        if pid == child:
            # Reap whatever else is pending, then leave. Exiting immediately
            # would kill the namespace out from under still-running siblings.
            while True:
                try:
                    if os.waitpid(-1, os.WNOHANG)[0] == 0:
                        break
                except ChildProcessError:
                    break
            os._exit(os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status))
