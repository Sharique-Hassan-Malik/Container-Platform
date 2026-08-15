"""Scheduling, rollouts, rollback and node failure, against a running control plane."""

from __future__ import annotations

import threading
import time

import pytest

from orchestrator import (
    ControlPlane,
    MemoryStore,
    SimulatedRuntime,
    deployment_spec,
    new_object,
    node_spec,
    pod_spec,
)
from orchestrator.objects import CRASHLOOP, READY
from orchestrator.scheduler import Scheduler
from orchestrator.workloads import revisions, rollback


@pytest.fixture
def plane():
    control = ControlPlane(MemoryStore(), runtime=SimulatedRuntime())
    yield control
    control.stop()


def deployment(image="serve:v1", replicas=4, *, fail=False, ready_delay=0.02,
               surge=1, unavailable=0, cpu=0.1, min_ready=0.0):
    return deployment_spec(
        replicas=replicas,
        template={"labels": {"app": "serve"}, "spec": pod_spec(
            image, readiness_delay_s=ready_delay, fail=fail, cpu=cpu)},
        selector={"app": "serve"},
        max_surge=surge,
        max_unavailable=unavailable,
        min_ready_seconds=min_ready,
    )


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------


def test_pods_are_placed_and_become_ready(plane):
    plane.with_nodes(3).start()
    plane.apply_deployment("serve", deployment())
    assert plane.wait_available("serve", 4, timeout=15)
    assert all(pod.spec.get("nodeName") for pod in plane.pods({"app": "serve"}))


def test_replicas_are_spread_across_nodes(plane):
    plane.with_nodes(3).start()
    plane.apply_deployment("serve", deployment(replicas=3))
    assert plane.wait_available("serve", 3, timeout=15)
    placement = plane.placement({"app": "serve"})
    assert len(placement) == 3          # anti-affinity put one on each
    assert set(placement.values()) == {1}


def test_binpacking_concentrates_instead(plane):
    packed = ControlPlane(MemoryStore(), runtime=SimulatedRuntime(), spread=False)
    try:
        packed.with_nodes(3).start()
        packed.apply_deployment("serve", deployment(replicas=3))
        assert packed.wait_available("serve", 3, timeout=15)
        placement = packed.placement({"app": "serve"})
        assert len(placement) < 3       # fewer nodes used than spread would
    finally:
        packed.stop()


def test_a_pod_that_does_not_fit_stays_pending(plane):
    plane.add_node("small", cpu=0.5, memory_mb=256)
    plane.start()
    plane.apply_deployment("serve", deployment(replicas=2, cpu=0.4))
    assert plane.wait(lambda: plane.available({"app": "serve"}) >= 1, timeout=10)
    assert not plane.wait(lambda: plane.available({"app": "serve"}) >= 2, timeout=1.5)
    assert any("insufficient cpu" in reason for reason in plane.scheduler.unschedulable.values())


def test_a_pending_pod_is_placed_when_capacity_appears(plane):
    plane.add_node("small", cpu=0.5, memory_mb=256)
    plane.start()
    plane.apply_deployment("serve", deployment(replicas=2, cpu=0.4))
    assert plane.wait(lambda: plane.available({"app": "serve"}) >= 1, timeout=10)
    plane.add_node("roomy", cpu=4.0)
    assert plane.wait_available("serve", 2, timeout=15)


def test_a_cordoned_node_receives_nothing(plane):
    plane.start()
    node = new_object("Node", "cordoned", node_spec(cpu=8.0, unschedulable=True))
    node.status = {"ready": True, "heartbeat": time.time()}
    plane.store.create(node)
    plane.add_node("open", cpu=8.0)
    plane.apply_deployment("serve", deployment(replicas=2))
    assert plane.wait_available("serve", 2, timeout=15)
    assert "cordoned" not in plane.placement({"app": "serve"})


def test_scheduler_only_writes_node_name(plane):
    plane.with_nodes(2).start()
    plane.apply_deployment("serve", deployment(replicas=1))
    assert plane.wait_available("serve", 1, timeout=15)
    pod = plane.pods({"app": "serve"})[0]
    # The scheduler must not have started anything -- that is the agent's job.
    assert pod.spec["image"] == "serve:v1"
    assert pod.status["node"] == pod.spec["nodeName"]


# ---------------------------------------------------------------------------
# rollouts
# ---------------------------------------------------------------------------


def test_rolling_update_replaces_every_pod(plane):
    plane.with_nodes(3).start()
    plane.apply_deployment("serve", deployment("serve:v1"))
    assert plane.wait_available("serve", 4, timeout=15)
    plane.apply_deployment("serve", deployment("serve:v2"))
    assert plane.wait_complete("serve", timeout=20)
    images = {pod.spec["image"] for pod in plane.pods({"app": "serve"})}
    assert images == {"serve:v2"}


def test_max_surge_is_never_exceeded(plane):
    plane.with_nodes(3).start()
    plane.apply_deployment("serve", deployment("serve:v1", replicas=4, surge=1))
    assert plane.wait_available("serve", 4, timeout=15)

    peak = [0]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            peak[0] = max(peak[0], len(plane.pods({"app": "serve"})))
            time.sleep(0.002)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    plane.apply_deployment("serve", deployment("serve:v2", replicas=4, surge=1))
    assert plane.wait_complete("serve", timeout=20)
    stop.set()
    sampler.join()
    assert peak[0] <= 5


def test_zero_max_unavailable_keeps_the_service_up(plane):
    plane.with_nodes(3).start()
    plane.apply_deployment("serve", deployment("serve:v1", ready_delay=0.05, unavailable=0))
    assert plane.wait_available("serve", 4, timeout=15)

    # Observed from the store's watch, which fires synchronously on every
    # write. A polling sampler thread aliases badly under GIL contention and
    # will happily miss the dip it exists to catch.
    samples = []

    def on_event(event):
        if event.object.kind == "Pod":
            samples.append(plane.available({"app": "serve"}))

    cancel = plane.store.watch(on_event)
    plane.apply_deployment("serve", deployment("serve:v2", ready_delay=0.05, unavailable=0))
    assert plane.wait_complete("serve", timeout=25)
    cancel()

    assert samples, "no pod events were observed during the rollout"
    assert min(samples) >= 4, f"availability dipped to {min(samples)} with maxUnavailable=0"
    assert max(samples) <= 5, "maxSurge=1 was exceeded"


def test_a_broken_rollout_stalls_without_taking_the_service_down(plane):
    plane.with_nodes(3).start()
    plane.apply_deployment("serve", deployment("serve:v1"))
    assert plane.wait_available("serve", 4, timeout=15)

    plane.apply_deployment("serve", deployment("serve:v2-broken", fail=True))
    time.sleep(1.5)
    status = plane.store.get("Deployment/default/serve").status
    assert status["complete"] is False
    assert status["availableReplicas"] == 4          # the old version still serves
    assert any(p.status.get("phase") == CRASHLOOP for p in plane.pods({"app": "serve"}))


def test_crashing_pods_do_not_multiply(plane):
    """A crash must restart in place, not spawn an endless stream of replacements."""
    plane.with_nodes(3).start()
    plane.apply_deployment("serve", deployment("serve:v1"))
    assert plane.wait_available("serve", 4, timeout=15)
    plane.apply_deployment("serve", deployment("serve:broken", fail=True, surge=1))
    time.sleep(2.0)
    assert len(plane.pods({"app": "serve"})) <= 5
    crashed = [p for p in plane.pods({"app": "serve"}) if p.status.get("phase") == CRASHLOOP]
    assert crashed and max(p.status.get("restartCount", 0) for p in crashed) > 1


def test_rollback_reuses_the_previous_revision(plane):
    plane.with_nodes(3).start()
    plane.apply_deployment("serve", deployment("serve:v1"))
    assert plane.wait_available("serve", 4, timeout=15)
    plane.apply_deployment("serve", deployment("serve:v2"))
    assert plane.wait_complete("serve", timeout=20)
    plane.apply_deployment("serve", deployment("serve:v3-broken", fail=True))
    time.sleep(1.0)

    before = {rs.meta.name for _, rs in revisions(plane.store, "Deployment/default/serve")}
    restored = rollback(plane.store, "Deployment/default/serve")
    assert plane.wait_complete("serve", timeout=20)
    after = {rs.meta.name for _, rs in revisions(plane.store, "Deployment/default/serve")}

    assert restored == 2
    # Rolling back re-selects an existing ReplicaSet rather than minting one.
    assert after == before
    assert {pod.spec["image"] for pod in plane.pods({"app": "serve"})} == {"serve:v2"}


def test_rollback_to_a_named_revision(plane):
    plane.with_nodes(3).start()
    plane.apply_deployment("serve", deployment("serve:v1"))
    assert plane.wait_available("serve", 4, timeout=15)
    plane.apply_deployment("serve", deployment("serve:v2"))
    assert plane.wait_complete("serve", timeout=20)
    assert rollback(plane.store, "Deployment/default/serve", to_revision=1) == 1
    assert plane.wait_complete("serve", timeout=20)
    assert {pod.spec["image"] for pod in plane.pods({"app": "serve"})} == {"serve:v1"}


def test_rollback_with_no_history_is_refused(plane):
    plane.with_nodes(2).start()
    plane.apply_deployment("serve", deployment("serve:v1"))
    assert plane.wait_available("serve", 4, timeout=15)
    with pytest.raises(ValueError, match="nothing to roll back"):
        rollback(plane.store, "Deployment/default/serve")


def test_scaling_up_and_down(plane):
    plane.with_nodes(3).start()
    plane.apply_deployment("serve", deployment(replicas=2))
    assert plane.wait_available("serve", 2, timeout=15)
    plane.apply_deployment("serve", deployment(replicas=5))
    assert plane.wait_available("serve", 5, timeout=20)
    plane.apply_deployment("serve", deployment(replicas=1))
    assert plane.wait(lambda: len(plane.pods({"app": "serve"})) == 1, timeout=15)


def test_min_ready_seconds_delays_availability(plane):
    plane.with_nodes(3).start()
    plane.apply_deployment("serve", deployment(replicas=2, ready_delay=0.0, min_ready=0.4))
    assert plane.wait(lambda: sum(1 for p in plane.pods({"app": "serve"})
                                  if p.status.get("phase") == READY) >= 2, timeout=15)
    # Ready, but not yet available: minReadySeconds has not elapsed.
    assert plane.available({"app": "serve"}, min_ready_seconds=0.4) < 2
    assert plane.wait(lambda: plane.available({"app": "serve"}, 0.4) >= 2, timeout=5)


# ---------------------------------------------------------------------------
# failure
# ---------------------------------------------------------------------------


def test_a_dead_node_is_detected_and_its_pods_rescheduled(plane):
    control = ControlPlane(MemoryStore(), runtime=SimulatedRuntime(), node_timeout=0.4)
    try:
        control.with_nodes(3).start()
        control.apply_deployment("serve", deployment(replicas=3))
        assert control.wait_available("serve", 3, timeout=15)
        victim = max(control.placement({"app": "serve"}).items(), key=lambda kv: kv[1])[0]

        control.remove_node(victim)          # crash: no cleanup, no event
        assert control.wait(
            lambda: control.store.get(f"Node/default/{victim}").status.get("ready") is False,
            timeout=5,
        ), "node failure was never detected"
        assert control.wait(lambda: control.available({"app": "serve"}) >= 3, timeout=15)
        assert victim not in control.placement({"app": "serve"})
    finally:
        control.stop()


def test_a_crashed_pod_is_restarted_in_place(plane):
    runtime = SimulatedRuntime()
    control = ControlPlane(MemoryStore(), runtime=runtime)
    try:
        control.with_nodes(2).start()
        control.apply_deployment("serve", deployment(replicas=2))
        assert control.wait_available("serve", 2, timeout=15)
        victim = control.pods({"app": "serve"})[0]
        names_before = {pod.meta.name for pod in control.pods({"app": "serve"})}

        runtime.crash(victim.key)
        assert control.wait(
            lambda: control.store.get(victim.key).status.get("restartCount", 0) >= 1, timeout=5
        )
        # Same pod object, restarted -- not a replacement with a new name.
        assert {pod.meta.name for pod in control.pods({"app": "serve"})} == names_before
    finally:
        control.stop()
