"""How a node actually runs a pod.

Three implementations behind one interface, because the control plane must not
care and the tests must be deterministic.

``SimulatedRuntime``  in-process, obeys `readinessDelaySeconds` and
                      `failOnStart` exactly. Used by tests and benchmarks so a
                      rollout can be measured without the noise of real process
                      startup.
``ProcessRuntime``    a real subprocess per pod.
``ContainerRuntime``  a real container per pod, via `container-runtime`.

The distinction between *alive* and *ready* is the interface's reason to exist.
Alive means the process has not exited. Ready means it will serve a request --
for a model server, seconds later, after weights have loaded. A control plane
that cannot tell them apart cannot roll out safely, so both are separate
methods rather than one `healthy()`.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from .objects import Object


@dataclass
class Handle:
    pod_key: str
    started_at: float
    pid: int = 0
    process: object = None
    container: object = None
    failed: bool = False
    ready_at: float = 0.0


class PodRuntime(Protocol):
    def start(self, pod: Object) -> Handle: ...
    def stop(self, handle: Handle) -> None: ...
    def alive(self, handle: Handle) -> bool: ...
    def ready(self, handle: Handle) -> bool: ...


class SimulatedRuntime:
    """Deterministic stand-in. No processes, exact timing.

    Real subprocess startup varies by tens of milliseconds, which is enough to
    make a rollout benchmark unreproducible and a rollout test flaky. Every
    behaviour that matters to the control plane -- start latency, readiness
    delay, startup failure, crashing later -- is expressible here exactly.
    """

    def __init__(self, start_latency: float = 0.0):
        self.start_latency = start_latency
        self.running: dict[str, Handle] = {}
        self.started = 0
        self.stopped = 0
        self._crashed: set[str] = set()
        self._lock = threading.Lock()

    def start(self, pod: Object) -> Handle:
        if self.start_latency:
            time.sleep(self.start_latency)
        handle = Handle(pod_key=pod.key, started_at=time.time(), pid=-1)
        handle.failed = bool(pod.spec.get("failOnStart"))
        handle.ready_at = handle.started_at + float(pod.spec.get("readinessDelaySeconds", 0.0))
        with self._lock:
            self.running[pod.key] = handle
            self.started += 1
        return handle

    def stop(self, handle: Handle) -> None:
        with self._lock:
            self.running.pop(handle.pod_key, None)
            self.stopped += 1

    def alive(self, handle: Handle) -> bool:
        return not handle.failed and handle.pod_key not in self._crashed

    def ready(self, handle: Handle) -> bool:
        return self.alive(handle) and time.time() >= handle.ready_at

    def crash(self, pod_key: str) -> None:
        """Fault injection: make a running pod fail without touching the store."""
        with self._lock:
            self._crashed.add(pod_key)


class ProcessRuntime:
    """One subprocess per pod.

    Readiness is a probe command rather than "the process is up", because those
    are different questions and only the second one is easy.
    """

    def __init__(self, workdir: str = "/tmp"):
        self.workdir = workdir
        self.started = 0
        self.stopped = 0

    def start(self, pod: Object) -> Handle:
        command = pod.spec.get("command") or ["sleep", "3600"]
        env = {**os.environ, **pod.spec.get("env", {})}
        process = subprocess.Popen(
            command, cwd=self.workdir, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.started += 1
        return Handle(
            pod_key=pod.key, started_at=time.time(), pid=process.pid, process=process,
            ready_at=time.time() + float(pod.spec.get("readinessDelaySeconds", 0.0)),
        )

    def stop(self, handle: Handle) -> None:
        process = handle.process
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        self.stopped += 1

    def alive(self, handle: Handle) -> bool:
        return handle.process is not None and handle.process.poll() is None

    def ready(self, handle: Handle) -> bool:
        return self.alive(handle) and time.time() >= handle.ready_at


class ContainerRuntime:
    """One real container per pod, via `container-runtime`.

    Kept optional on purpose. The control plane's correctness has nothing to do
    with how a pod is executed, and requiring unprivileged user namespaces to
    test a scheduler would be the wrong dependency -- so this is imported lazily
    and the tests use `SimulatedRuntime`.
    """

    def __init__(self, image_store: str, workspace: str = "./.state/containers"):
        from minicon import ImageStore  # noqa: PLC0415

        self.store = ImageStore(image_store)
        self.workspace = workspace
        self.started = 0
        self.stopped = 0

    @staticmethod
    def available() -> bool:
        try:
            import minicon  # noqa: F401,PLC0415
            from minicon.linux import overlayfs_in_userns, userns_available  # noqa: PLC0415
        except ImportError:
            return False
        return userns_available()[0] and overlayfs_in_userns()

    def start(self, pod: Object) -> Handle:
        from minicon import Container, ContainerConfig, Limits  # noqa: PLC0415

        resources = pod.spec.get("resources", {})
        config = ContainerConfig(
            image=pod.spec["image"],
            name=pod.meta.name,
            argv=list(pod.spec.get("command") or []),
            env=dict(pod.spec.get("env", {})),
            limits=Limits(
                memory_bytes=int(float(resources.get("memoryMB", 64)) * (1 << 20)),
                cpu_quota=float(resources.get("cpu", 0.1)),
                pids_max=128,
            ),
            use_init=True,
        )
        container = Container(config, self.store, self.workspace).create()
        container.start()
        self.started += 1
        return Handle(
            pod_key=pod.key, started_at=time.time(), pid=container.child_pid, container=container,
            ready_at=time.time() + float(pod.spec.get("readinessDelaySeconds", 0.0)),
        )

    def stop(self, handle: Handle) -> None:
        container = handle.container
        if container is None:
            return
        container.kill(grace=5.0)
        container.delete()
        self.stopped += 1

    def alive(self, handle: Handle) -> bool:
        container = handle.container
        if container is None:
            return False
        try:
            os.kill(container.child_pid, 0)
            return True
        except OSError:
            return False

    def ready(self, handle: Handle) -> bool:
        return self.alive(handle) and time.time() >= handle.ready_at
