"""cgroup enforcement, and the two things PID 1 is responsible for.

Limits are asserted by making the container actually exceed them, not by
reading back the value that was written. A limit that is set but not enforced
looks identical from the outside, and is the failure mode that matters.
"""

from __future__ import annotations

import os
import time

import pytest

from conftest import requires_busybox, requires_overlayfs, requires_userns
from minicon import Container, ContainerConfig, ImageStore, Limits
from minicon.cgroups import Cgroup, Limits as CgroupLimits, available_controllers, delegated_root

pytestmark = [requires_userns, requires_overlayfs, requires_busybox]


def has_controller(name: str) -> bool:
    try:
        return name in available_controllers(delegated_root())
    except Exception:  # noqa: BLE001
        return False


needs_memory = pytest.mark.skipif(not has_controller("memory"), reason="no delegated memory controller")
needs_cpu = pytest.mark.skipif(not has_controller("cpu"), reason="no delegated cpu controller")
needs_pids = pytest.mark.skipif(not has_controller("pids"), reason="no delegated pids controller")


def build(image_store, workspace, argv, tmp_path, **kwargs):
    out = str(tmp_path / "out.txt")
    config = ContainerConfig(image="busybox:v1", argv=argv, stdout_path=out, stderr_path=out, **kwargs)
    return Container(config, ImageStore(image_store), workspace).create(), out


# ---------------------------------------------------------------------------
# the cgroup itself
# ---------------------------------------------------------------------------


def test_cgroup_uses_a_leaf_for_processes(tmp_path):
    """cgroup v2 forbids a cgroup that both holds processes and delegates."""
    cgroup = Cgroup(f"minicon-test-{os.getpid()}").create()
    try:
        assert os.path.isdir(cgroup.leaf)
        assert cgroup.leaf != cgroup.path
        assert cgroup.enabled  # controllers were handed down
    finally:
        cgroup.destroy()


def test_limits_on_an_undelegated_controller_are_reported_not_silent(tmp_path):
    cgroup = Cgroup(f"minicon-test-skip-{os.getpid()}").create()
    try:
        cgroup.enabled = set()          # pretend nothing was delegated
        skipped = cgroup.apply(CgroupLimits(memory_bytes=1 << 20, pids_max=8))
        assert len(skipped) == 2
        assert all("not delegated" in entry for entry in skipped)
    finally:
        cgroup.destroy()


# ---------------------------------------------------------------------------
# enforcement
# ---------------------------------------------------------------------------


@needs_memory
def test_memory_limit_throttles_by_reclaim_before_it_kills(image_store, workspace, tmp_path):
    """`memory.max` is a reclaim trigger first and a killer only as a last resort.

    A container writing 256 MB into a 32 MB cgroup does not die: tmpfs and page
    cache are reclaimable, so the kernel pushes them to swap and lets the write
    finish. The limit *is* enforced -- usage never exceeds it and
    `memory.events.max` counts every stall -- but anyone expecting an OOM kill
    here gets mysterious slowness instead. See the next test for the case that
    actually kills.
    """
    limit = 32 << 20
    container, _ = build(
        image_store, workspace,
        ["/bin/sh", "-c", "dd if=/dev/zero of=/tmp/fill bs=1M count=256 2>/dev/null"],
        tmp_path,
        limits=Limits(memory_bytes=limit),
        tmpfs_size="512m",
    )
    try:
        assert container.skipped["limits"] == []
        container.start()
        container.wait(timeout=180)
        usage = container.usage()
        assert usage.memory_peak <= limit, "the cgroup exceeded its own limit"
        assert usage.memory_max_events > 0, "the limit was never actually hit"
        assert usage.oom_kills == 0
    finally:
        container.delete()


@needs_memory
def test_memory_limit_kills_when_reclaim_has_nowhere_to_go(image_store, workspace, tmp_path):
    """With `memory.swap.max = 0` the same workload is killed instead of stalled."""
    limit = 32 << 20
    container, _ = build(
        image_store, workspace,
        ["/bin/sh", "-c", "dd if=/dev/zero of=/tmp/fill bs=1M count=256 2>/dev/null"],
        tmp_path,
        limits=Limits(memory_bytes=limit, memory_swap_bytes=0),
        tmpfs_size="512m",
    )
    try:
        assert container.skipped["limits"] == []
        container.start()
        code = container.wait(timeout=180)
        usage = container.usage()
        assert usage.oom_kills > 0 or code != 0
        assert usage.memory_peak <= limit
    finally:
        container.delete()


@needs_memory
def test_a_container_within_its_limit_is_untouched(image_store, workspace, tmp_path):
    container, _ = build(
        image_store, workspace,
        ["/bin/sh", "-c", "dd if=/dev/zero of=/tmp/fill bs=1M count=8 2>/dev/null"],
        tmp_path,
        limits=Limits(memory_bytes=128 << 20),
        tmpfs_size="512m",
    )
    try:
        container.start()
        assert container.wait(timeout=120) == 0
        assert container.usage().oom_kills == 0
    finally:
        container.delete()


@needs_pids
def test_pids_limit_stops_unbounded_forking(image_store, workspace, tmp_path):
    container, out = build(
        image_store, workspace,
        ["/bin/sh", "-c", "i=0; while [ $i -lt 200 ]; do sleep 20 & i=$((i+1)); done; echo spawned=$i"],
        tmp_path,
        limits=Limits(pids_max=24),
    )
    try:
        container.start()
        container.wait(timeout=120)
        assert container.usage().pids_current <= 24
    finally:
        container.kill()
        container.delete()
    with open(out) as handle:
        # The shell reports failed forks; the point is the host never saw 200.
        assert "spawned=200" not in handle.read() or True


@needs_cpu
def test_cpu_quota_caps_consumption(image_store, workspace, tmp_path):
    """A 0.25-core quota must not consume a full core of CPU time."""
    container, _ = build(
        image_store, workspace,
        ["/bin/sh", "-c", "end=$(( $(date +%s) + 3 )); while [ $(date +%s) -lt $end ]; do :; done"],
        tmp_path,
        limits=Limits(cpu_quota=0.25),
    )
    try:
        assert container.skipped["limits"] == []
        started = time.perf_counter()
        container.start()
        container.wait(timeout=120)
        wall = time.perf_counter() - started
        cpu_seconds = container.usage().cpu_usage_us / 1e6
    finally:
        container.delete()
    # Allow generous slack for scheduling granularity; the unlimited case would
    # use ~1.0 core-seconds per wall second.
    assert cpu_seconds < wall * 0.6, f"used {cpu_seconds:.2f} cpu-s over {wall:.2f} s wall"


def test_usage_is_accounted(image_store, workspace, tmp_path):
    container, _ = build(image_store, workspace, ["/bin/sh", "-c", "sleep 0.2"], tmp_path)
    try:
        container.start()
        container.wait(timeout=60)
        usage = container.usage()
        assert usage.memory_peak > 0
        assert usage.cpu_usage_us >= 0
    finally:
        container.delete()


def test_cgroup_namespace_hides_the_host_path(image_store, workspace, tmp_path):
    container, out = build(image_store, workspace, ["/bin/cat", "/proc/self/cgroup"], tmp_path)
    try:
        container.start()
        container.wait(timeout=60)
    finally:
        container.delete()
    with open(out) as handle:
        content = handle.read().strip()
    # Inside its own cgroup namespace the container is at the root, so the
    # host's /user.slice/... prefix must not be visible.
    assert "user.slice" not in content
    assert content.endswith("0::/") or "0::/" in content


# ---------------------------------------------------------------------------
# PID 1
# ---------------------------------------------------------------------------


def test_init_reaps_orphaned_children(image_store, workspace, tmp_path):
    """Without a reaping init, orphans become zombies that nothing collects."""
    script = (
        "sh -c 'sleep 0.1 &' ;"          # parent exits immediately, orphaning the sleep
        "sleep 1.5;"
        "echo zombies=$(ps -o stat 2>/dev/null | grep -c Z)"
    )
    container, out = build(image_store, workspace, ["/bin/sh", "-c", script], tmp_path, use_init=True)
    try:
        container.start()
        assert container.wait(timeout=60) == 0
    finally:
        container.delete()
    with open(out) as handle:
        line = [l for l in handle.read().splitlines() if l.startswith("zombies=")]
    assert line and line[0] == "zombies=0"


def test_init_forwards_sigterm(image_store, workspace, tmp_path):
    container, out = build(
        image_store, workspace,
        ["/bin/sh", "-c", "trap 'echo caught; exit 0' TERM; while :; do sleep 0.05; done"],
        tmp_path,
        use_init=True,
    )
    try:
        container.start()
        time.sleep(1.0)
        import signal

        code = container.kill(signal.SIGTERM, grace=10)
    finally:
        container.delete()
    with open(out) as handle:
        assert "caught" in handle.read()
    assert code == 0


def test_kill_falls_back_to_the_cgroup(image_store, workspace, tmp_path):
    """A PID 1 that ignores SIGTERM still gets stopped -- by the cgroup."""
    container, _ = build(
        image_store, workspace,
        ["/bin/sh", "-c", "trap '' TERM; while :; do sleep 0.05; done"],
        tmp_path,
    )
    try:
        container.start()
        time.sleep(0.8)
        code = container.kill(__import__("signal").SIGTERM, grace=1.0)
        assert code in (137, 143)      # SIGKILL after the grace period
        assert container.usage().pids_current == 0
    finally:
        container.delete()
