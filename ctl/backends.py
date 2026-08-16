"""What this host can actually run, and what it falls back to.

The control plane has two pluggable seams — where cluster state lives, and how
a pod is executed — and each has a real implementation and a simulated one. The
real ones need things a laptop may not have: gRPC for Raft, unprivileged user
namespaces and overlayfs for containers.

The important property is that an unavailable backend is *named and explained*,
never silently swapped. A rollout benchmark run against `SimulatedRuntime`
because overlayfs was missing is a number about nothing, and the only way to
know is for the tool to say which backend it used.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .paths import MODULES_ROOT, add_modules

add_modules()


@dataclass(frozen=True)
class Backend:
    name: str
    seam: str                    # "store" | "runtime"
    module: str                  # which module folder provides it
    summary: str
    simulated: bool = False
    # Lower is preferred when picking automatically. Explicit, because
    # "most capable" is not something declaration order should imply.
    rank: int = 0

    def check(self) -> tuple[bool, str]:
        """(usable here, reason if not)."""
        return _CHECKS[self.name]()


def _memory_ok() -> tuple[bool, str]:
    return True, ""


def _raft_ok() -> tuple[bool, str]:
    if not (MODULES_ROOT / "raft-kv").is_dir():
        return False, "the raft-kv module is not in this repository"
    if importlib.util.find_spec("grpc") is None:
        return False, "grpcio is not installed (pip install grpcio)"
    if importlib.util.find_spec("raft_kv") is None:
        return False, "raft_kv could not be imported"
    return True, ""


def _simulated_ok() -> tuple[bool, str]:
    return True, ""


def _process_ok() -> tuple[bool, str]:
    return True, ""


def _container_ok() -> tuple[bool, str]:
    if not (MODULES_ROOT / "container-runtime").is_dir():
        return False, "the container-runtime module is not in this repository"
    try:
        from minicon.linux import overlayfs_in_userns, userns_available
    except ImportError as exc:
        return False, f"minicon could not be imported ({exc})"

    usable, reason = userns_available()
    if not usable:
        return False, f"unprivileged user namespaces unavailable: {reason}"
    if not overlayfs_in_userns():
        return False, "overlayfs is not usable inside a user namespace here"
    return True, ""


_CHECKS: dict[str, Callable[[], tuple[bool, str]]] = {
    "memory": _memory_ok,
    "raft": _raft_ok,
    "simulated": _simulated_ok,
    "process": _process_ok,
    "container": _container_ok,
}


BACKENDS: tuple[Backend, ...] = (
    Backend("memory", "store", "orchestrator",
            "Single-process object store. Correct, not replicated.",
            simulated=True, rank=1),
    Backend("raft", "store", "raft-kv",
            "Cluster state replicated through Raft — the etcd-shaped seam.",
            rank=0),
    Backend("simulated", "runtime", "orchestrator",
            "In-process pods with exact timing. What the tests use.",
            simulated=True, rank=2),
    Backend("process", "runtime", "orchestrator",
            "One real subprocess per pod. No isolation.", rank=1),
    Backend("container", "runtime", "container-runtime",
            "One real container per pod: namespaces, cgroups, overlayfs.", rank=0),
)

STORES = tuple(b for b in BACKENDS if b.seam == "store")
RUNTIMES = tuple(b for b in BACKENDS if b.seam == "runtime")


def get(seam: str, name: str) -> Backend:
    for backend in BACKENDS:
        if backend.seam == seam and backend.name == name:
            return backend
    known = ", ".join(b.name for b in BACKENDS if b.seam == seam)
    raise KeyError(f"unknown {seam} backend {name!r}; choose from {known}")


def best(seam: str) -> Backend:
    """The most capable backend that works here, preferring a real one."""
    candidates = sorted((b for b in BACKENDS if b.seam == seam), key=lambda b: b.rank)
    for backend in candidates:
        usable, _ = backend.check()
        if usable:
            return backend
    return candidates[-1]


def build_store(name: str, **options):
    """Construct a cluster store. Raises if the chosen backend is unusable."""
    backend = get("store", name)
    usable, reason = backend.check()
    if not usable:
        raise RuntimeError(f"store backend {name!r} unavailable: {reason}")

    from orchestrator import MemoryStore

    if name == "memory":
        return MemoryStore()

    from orchestrator import RaftStore

    return RaftStore(**options)


def build_runtime(name: str, **options):
    """Construct a pod runtime. Raises if the chosen backend is unusable."""
    backend = get("runtime", name)
    usable, reason = backend.check()
    if not usable:
        raise RuntimeError(f"runtime backend {name!r} unavailable: {reason}")

    from orchestrator import ContainerRuntime, ProcessRuntime, SimulatedRuntime

    # Each backend takes only what it can use — the caller passes one options
    # bag for all three rather than knowing which keys apply where.
    if name == "simulated":
        return SimulatedRuntime(start_latency=float(options.get("start_latency", 0.0)))
    if name == "process":
        return ProcessRuntime()

    # Checked here rather than in `check()`: the runtime is perfectly usable on
    # this host, you just have not built an image yet. Those are different
    # facts, and reporting the second as "container backend unavailable" in
    # `ctl status` would be a lie. Checked before construction all the same,
    # because otherwise it surfaces as a FileNotFoundError from three frames
    # inside the runtime, naming a path and no way to fix it.
    image_store = Path(options.get("image_store", "./images"))
    if not (image_store / "index.json").is_file():
        raise RuntimeError(
            f"runtime backend 'container' needs an OCI image layout at "
            f"{image_store}, and {image_store / 'index.json'} is not there. "
            f"Build one:\n"
            f"    ctl image --store {image_store} build ./context -t serve:v1\n"
            f"or point --image-store at a layout you already have."
        )
    return ContainerRuntime(
        image_store=str(image_store),
        workspace=str(options.get("workspace", "./.state/containers")),
    )
