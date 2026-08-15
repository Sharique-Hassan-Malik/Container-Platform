"""cgroup v2: the resource half of a container.

Namespaces control what a process can *see*. Cgroups control what it can
*consume*. They are completely independent kernel subsystems -- a process can
have every namespace and no limits, or every limit and no isolation -- and
conflating them is how people end up with "containers" that OOM the host.

Two rules govern v2 and both bite immediately:

**No internal processes.** A cgroup may hold processes, or it may distribute
controllers to children, never both. So a container gets `<name>/` for policy
and `<name>/leaf/` for its processes, which looks like bureaucracy until the
first `EBUSY`.

**Controllers must be handed down explicitly.** A child sees only what its
parent wrote into `cgroup.subtree_control`, and a parent can only hand down
what it was given. Delegation is a chain from the root, which is why an
unprivileged runtime works inside `user@<uid>.service` -- systemd delegates
that subtree to the user -- and nowhere else.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

CGROUP_ROOT = "/sys/fs/cgroup"


@dataclass
class Limits:
    """What a container is allowed to consume. `None` means unlimited."""

    memory_bytes: int | None = None
    memory_high_bytes: int | None = None
    # Setting this to 0 is what turns `memory.max` from a throttle into a
    # killer: reclaimable pages have nowhere to go, so the kernel must OOM.
    memory_swap_bytes: int | None = None
    cpu_quota: float | None = None       # in cores: 0.5 = half a core
    cpu_weight: int | None = None        # 1..10000, relative share under contention
    pids_max: int | None = None
    io_weight: int | None = None

    def is_empty(self) -> bool:
        return all(
            getattr(self, f) is None
            for f in ("memory_bytes", "memory_high_bytes", "memory_swap_bytes",
                      "cpu_quota", "cpu_weight", "pids_max", "io_weight")
        )


@dataclass
class Usage:
    memory_current: int = 0
    memory_peak: int = 0
    memory_max_events: int = 0
    cpu_usage_us: int = 0
    pids_current: int = 0
    memory_events: dict[str, int] = None  # type: ignore[assignment]

    @property
    def oom_kills(self) -> int:
        return (self.memory_events or {}).get("oom_kill", 0)

    @property
    def throttled_high(self) -> int:
        """Times the kernel stalled the cgroup at `memory.high` instead of killing it."""
        return (self.memory_events or {}).get("high", 0)


class CgroupUnavailable(RuntimeError):
    pass


def delegated_root() -> str:
    """The highest cgroup this user may write to.

    Preference order matters. `user@<uid>.service` is the subtree systemd
    delegates, and it carries the `cpu` controller. The caller's *own* cgroup is
    the fallback -- always writable, but often missing controllers because
    nothing enabled them further up. Reporting which one was used explains why
    a CPU limit silently did nothing.
    """
    uid = os.getuid()
    candidates = [
        f"{CGROUP_ROOT}/user.slice/user-{uid}.slice/user@{uid}.service",
        _own_cgroup(),
    ]
    for candidate in candidates:
        if candidate and os.access(os.path.join(candidate, "cgroup.procs"), os.W_OK):
            return candidate
    raise CgroupUnavailable(
        f"no writable cgroup v2 directory for uid {uid}. "
        f"Tried {candidates}. cgroup v2 must be mounted at {CGROUP_ROOT} and delegated "
        "(systemd does this for user@.service; see systemd.resource-control(5))."
    )


def _own_cgroup() -> str | None:
    try:
        with open("/proc/self/cgroup") as handle:
            for line in handle:
                if line.startswith("0::"):
                    return os.path.join(CGROUP_ROOT, line.strip()[3:].lstrip("/"))
    except OSError:
        pass
    return None


def available_controllers(path: str) -> set[str]:
    try:
        with open(os.path.join(path, "cgroup.controllers")) as handle:
            return set(handle.read().split())
    except OSError:
        return set()


class Cgroup:
    """One container's cgroup: `<parent>/<name>/` with a `leaf/` for processes."""

    def __init__(self, name: str, parent: str | None = None):
        self.parent = parent or delegated_root()
        self.name = name
        self.path = os.path.join(self.parent, name)
        self.leaf = os.path.join(self.path, "leaf")
        self.enabled: set[str] = set()

    # -- lifecycle -----------------------------------------------------------

    def create(self, limits: Limits | None = None) -> "Cgroup":
        os.makedirs(self.path, exist_ok=True)
        wanted = available_controllers(self.path)
        # Only ask for controllers the parent actually granted; requesting one
        # it does not have makes the whole write fail with ENOENT, taking the
        # working controllers down with it.
        if wanted:
            try:
                self._write("cgroup.subtree_control", " ".join(f"+{c}" for c in sorted(wanted)))
                self.enabled = wanted
            except OSError:
                for controller in sorted(wanted):
                    try:
                        self._write("cgroup.subtree_control", f"+{controller}")
                        self.enabled.add(controller)
                    except OSError:
                        continue
        os.makedirs(self.leaf, exist_ok=True)
        if limits and not limits.is_empty():
            self.apply(limits)
        return self

    def apply(self, limits: Limits) -> list[str]:
        """Write limits to the leaf. Returns the names that could not be set."""
        skipped: list[str] = []
        settings = [
            ("memory", "memory.max", limits.memory_bytes, lambda v: str(int(v))),
            ("memory", "memory.high", limits.memory_high_bytes, lambda v: str(int(v))),
            ("memory", "memory.swap.max", limits.memory_swap_bytes, lambda v: str(int(v))),
            ("cpu", "cpu.max", limits.cpu_quota, lambda v: f"{int(v * 100_000)} 100000"),
            ("cpu", "cpu.weight", limits.cpu_weight, lambda v: str(int(v))),
            ("pids", "pids.max", limits.pids_max, lambda v: str(int(v))),
            ("io", "io.weight", limits.io_weight, lambda v: str(int(v))),
        ]
        for controller, filename, value, render in settings:
            if value is None:
                continue
            if controller not in self.enabled:
                skipped.append(f"{filename} (controller {controller!r} not delegated)")
                continue
            try:
                self._write_leaf(filename, render(value))
            except OSError as exc:
                skipped.append(f"{filename} ({exc.strerror})")
        return skipped

    def add_process(self, pid: int) -> None:
        """Move a process into the leaf.

        Migration needs write access to the destination *and* to the common
        ancestor of source and destination. Running under `user@<uid>.service`
        satisfies both; running from a login session scope does not, and fails
        with EACCES for a reason no error message explains.
        """
        self._write_leaf("cgroup.procs", str(pid))

    def usage(self) -> Usage:
        return Usage(
            memory_current=self._read_int("memory.current"),
            memory_peak=self._read_int("memory.peak"),
            memory_max_events=self._read_keyed("memory.events").get("max", 0),
            cpu_usage_us=self._read_keyed("cpu.stat").get("usage_usec", 0),
            pids_current=self._read_int("pids.current"),
            memory_events=self._read_keyed("memory.events"),
        )

    def processes(self) -> list[int]:
        try:
            with open(os.path.join(self.leaf, "cgroup.procs")) as handle:
                return [int(line) for line in handle if line.strip()]
        except OSError:
            return []

    def kill_all(self) -> None:
        """`cgroup.kill` removes every process at once, with no PID races.

        The alternative -- read cgroup.procs, signal each PID -- loses to a
        process that forks between the read and the kill. Linux 5.14+.
        """
        try:
            self._write("cgroup.kill", "1")
            return
        except OSError:
            pass
        import signal

        for pid in self.processes():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                continue

    def destroy(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                os.rmdir(self.leaf)
                break
            except FileNotFoundError:
                break
            except OSError:
                # EBUSY: a process is still exiting. Nothing to do but wait.
                time.sleep(0.02)
        try:
            os.rmdir(self.path)
        except OSError:
            pass

    # -- io ------------------------------------------------------------------

    def _write(self, filename: str, value: str) -> None:
        with open(os.path.join(self.path, filename), "w") as handle:
            handle.write(value)

    def _write_leaf(self, filename: str, value: str) -> None:
        with open(os.path.join(self.leaf, filename), "w") as handle:
            handle.write(value)

    def _read_int(self, filename: str) -> int:
        try:
            with open(os.path.join(self.leaf, filename)) as handle:
                text = handle.read().strip()
            return 0 if text == "max" else int(text)
        except (OSError, ValueError):
            return 0

    def _read_keyed(self, filename: str) -> dict[str, int]:
        out: dict[str, int] = {}
        try:
            with open(os.path.join(self.leaf, filename)) as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) == 2:
                        try:
                            out[parts[0]] = int(parts[1])
                        except ValueError:
                            continue
        except OSError:
            pass
        return out
