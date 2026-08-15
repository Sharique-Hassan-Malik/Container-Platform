"""Isolation, asserted against the kernel rather than against `unshare`'s return code.

Two processes share a namespace exactly when `/proc/<pid>/ns/<type>` reads back
the same `type:[inode]` string. Every claim below is checked that way, or by
observing something the container genuinely cannot see.
"""

from __future__ import annotations

import json
import os

import pytest

from container_testcaps import requires_busybox, requires_overlayfs, requires_userns
from minicon import Container, ContainerConfig, ImageStore, Limits, namespace_ids

pytestmark = [requires_userns, requires_overlayfs, requires_busybox]


def run(image_store, workspace, tmp_path, argv, **kwargs):
    """Run a container to completion and return (exit_code, stdout)."""
    out = str(tmp_path / f"out-{abs(hash(tuple(argv))) % 10**8}.txt")
    config = ContainerConfig(image=kwargs.pop("image", "busybox:v1"), argv=argv, stdout_path=out, **kwargs)
    container = Container(config, ImageStore(image_store), workspace).create()
    try:
        container.start()
        code = container.wait(timeout=60)
    finally:
        container.delete()
    with open(out) as handle:
        return code, handle.read()


# ---------------------------------------------------------------------------
# it runs at all
# ---------------------------------------------------------------------------


def test_container_runs_the_image_entrypoint(image_store, workspace, tmp_path):
    config = ContainerConfig(image="app:v1", stdout_path=str(tmp_path / "o.txt"))
    container = Container(config, ImageStore(image_store), workspace).create()
    try:
        container.start()
        assert container.wait(timeout=60) == 0
    finally:
        container.delete()
    with open(tmp_path / "o.txt") as handle:
        assert handle.read().strip() == "container-ok"


def test_exit_code_propagates(image_store, workspace, tmp_path):
    code, _ = run(image_store, workspace, tmp_path, ["/bin/sh", "-c", "exit 42"])
    assert code == 42


def test_missing_entrypoint_is_reported_not_hung(image_store, workspace, tmp_path):
    code, _ = run(image_store, workspace, tmp_path, ["/bin/does-not-exist"])
    assert code == 127


# ---------------------------------------------------------------------------
# namespaces
# ---------------------------------------------------------------------------


def test_every_namespace_differs_from_the_host(image_store, workspace, tmp_path):
    code, output = run(
        image_store, workspace, tmp_path,
        ["/bin/sh", "-c", "for n in user mnt pid net uts ipc cgroup; do echo $n=$(readlink /proc/self/ns/$n); done"],
    )
    assert code == 0
    inside = dict(line.split("=", 1) for line in output.strip().splitlines())
    outside = namespace_ids()
    for kind, value in inside.items():
        assert value != outside[kind], f"{kind} namespace was not isolated"


def test_pid_namespace_hides_host_processes(image_store, workspace, tmp_path):
    code, output = run(
        image_store, workspace, tmp_path,
        ["/bin/sh", "-c", "echo pid=$$; ls /proc | grep -c '^[0-9]*$'"],
    )
    assert code == 0
    lines = output.strip().splitlines()
    assert lines[0] == "pid=1"          # the entrypoint is PID 1
    assert int(lines[1]) <= 3           # itself, and the `ls`/`grep` pipeline


def test_uts_namespace_gives_its_own_hostname(image_store, workspace, tmp_path):
    code, output = run(image_store, workspace, tmp_path, ["/bin/hostname"], hostname="isolated-box")
    assert code == 0 and output.strip() == "isolated-box"
    assert os.uname().nodename != "isolated-box"


def test_network_namespace_has_only_loopback(image_store, workspace, tmp_path):
    code, output = run(image_store, workspace, tmp_path, ["/bin/sh", "-c", "ip -o link | wc -l"])
    assert code == 0
    assert int(output.strip()) == 1


def test_loopback_is_brought_up(image_store, workspace, tmp_path):
    code, output = run(image_store, workspace, tmp_path, ["/bin/sh", "-c", "ip -o link show lo"])
    assert code == 0 and "UP" in output


def test_mount_namespace_root_is_the_image(image_store, workspace, tmp_path):
    code, output = run(image_store, workspace, tmp_path, ["/bin/sh", "-c", "ls /"])
    assert code == 0
    entries = set(output.split())
    assert {"bin", "proc", "dev", "tmp"} <= entries
    # The host's root has directories the image does not.
    assert "home" not in entries or "root" not in entries
    assert ".oldroot" not in entries      # pivot_root cleaned up after itself


def test_user_namespace_maps_container_root_to_the_calling_user(image_store, workspace, tmp_path):
    code, output = run(image_store, workspace, tmp_path, ["/bin/id", "-u"])
    assert code == 0
    assert output.strip() == "0"          # root inside
    assert os.getuid() != 0               # not root outside


def test_single_mapping_runs_as_the_requested_uid(image_store, workspace, tmp_path):
    """`USER 10001` without newuidmap: uid 10001 exists, uid 0 does not."""
    from minicon import idmap

    if idmap.helper_available():
        pytest.skip("newuidmap installed: the subid strategy would be chosen instead")
    code, output = run(image_store, workspace, tmp_path, ["/bin/id", "-u"], user="10001:10001")
    assert code == 0
    assert output.strip() == "10001"


# ---------------------------------------------------------------------------
# filesystem
# ---------------------------------------------------------------------------


def test_writes_land_in_the_upper_layer_only(image_store, workspace, tmp_path):
    config = ContainerConfig(
        image="busybox:v1",
        argv=["/bin/sh", "-c", "echo written > /app/new.txt"],
        stdout_path=str(tmp_path / "o.txt"),
    )
    container = Container(config, ImageStore(image_store), workspace).create()
    lower = container.overlay.lowers[-1]
    try:
        container.start()
        assert container.wait(timeout=60) == 0
        assert "/app/new.txt" in container.overlay.diff()
        with open(os.path.join(container.overlay.upper, "app/new.txt")) as handle:
            assert handle.read().strip() == "written"
        # The shared layer cache must be untouched, or container two inherits
        # container one's writes.
        assert not os.path.exists(os.path.join(lower, "app/new.txt"))
    finally:
        container.delete()


def test_two_containers_share_layers_and_not_writes(image_store, workspace, tmp_path):
    store = ImageStore(image_store)
    first = Container(
        ContainerConfig(image="busybox:v1", argv=["/bin/sh", "-c", "echo one > /app/x"], name="c1"),
        store, workspace,
    ).create()
    second = Container(
        ContainerConfig(
            image="busybox:v1", argv=["/bin/sh", "-c", "cat /app/x 2>/dev/null || echo absent"],
            name="c2", stdout_path=str(tmp_path / "o.txt"),
        ),
        store, workspace,
    ).create()
    try:
        assert first.overlay.lowers == second.overlay.lowers   # same unpacked bytes
        assert first.overlay.upper != second.overlay.upper     # separate writes
        first.start()
        first.wait(timeout=60)
        second.start()
        second.wait(timeout=60)
    finally:
        first.delete()
        second.delete()
    with open(tmp_path / "o.txt") as handle:
        assert handle.read().strip() == "absent"


def test_readonly_root_rejects_writes(image_store, workspace, tmp_path):
    code, _ = run(
        image_store, workspace, tmp_path,
        ["/bin/sh", "-c", "echo x > /app/blocked 2>/dev/null"],
        readonly_root=True,
    )
    assert code != 0


def test_tmp_is_writable_even_with_a_readonly_root(image_store, workspace, tmp_path):
    code, output = run(
        image_store, workspace, tmp_path,
        ["/bin/sh", "-c", "echo x > /tmp/ok && cat /tmp/ok"],
        readonly_root=True,
    )
    assert code == 0 and output.strip() == "x"


def test_proc_is_mounted_and_shows_the_container(image_store, workspace, tmp_path):
    code, output = run(image_store, workspace, tmp_path, ["/bin/cat", "/proc/self/status"])
    assert code == 0 and "Name:" in output


def test_dev_null_is_usable(image_store, workspace, tmp_path):
    code, output = run(image_store, workspace, tmp_path, ["/bin/sh", "-c", "echo discard > /dev/null; echo ok"])
    assert code == 0 and output.strip() == "ok"


# ---------------------------------------------------------------------------
# phases and state
# ---------------------------------------------------------------------------


def test_phases_are_populated_and_consistent(image_store, workspace, tmp_path):
    config = ContainerConfig(image="busybox:v1", argv=["/bin/true"])
    container = Container(config, ImageStore(image_store), workspace).create()
    try:
        container.start()
        container.wait(timeout=60)
    finally:
        container.delete()
    phases = container.phases
    assert phases.namespace_s > 0 and phases.setup_s > 0
    assert phases.overlay_s > 0 and phases.pivot_s > 0
    # The inner phases are a breakdown of setup, so they cannot exceed it.
    assert phases.overlay_s + phases.mounts_s + phases.pivot_s <= phases.setup_s + 1e-3


def test_second_container_skips_layer_unpacking(image_store, workspace, tmp_path):
    store = ImageStore(image_store)
    first = Container(ContainerConfig(image="busybox:v1", argv=["/bin/true"], name="a"), store, workspace).create()
    second = Container(ContainerConfig(image="busybox:v1", argv=["/bin/true"], name="b"), store, workspace).create()
    try:
        assert second.phases.unpack_s < first.phases.unpack_s
        assert second.layer_cache.unpacked == []
    finally:
        first.delete()
        second.delete()


def test_state_file_is_written(image_store, workspace, tmp_path):
    config = ContainerConfig(image="busybox:v1", argv=["/bin/true"], name="stateful")
    container = Container(config, ImageStore(image_store), workspace).create()
    try:
        container.start()
        container.wait(timeout=60)
        with open(os.path.join(container.bundle, "state.json")) as handle:
            state = json.load(handle)
        assert state["name"] == "stateful"
        assert state["status"] == "exited"
        assert state["exit_code"] == 0
    finally:
        container.delete()


def test_delete_removes_the_bundle(image_store, workspace, tmp_path):
    config = ContainerConfig(image="busybox:v1", argv=["/bin/true"], name="ephemeral")
    container = Container(config, ImageStore(image_store), workspace).create()
    container.start()
    container.wait(timeout=60)
    bundle = container.bundle
    container.delete()
    assert not os.path.exists(bundle)


def test_context_manager_cleans_up(image_store, workspace, tmp_path):
    store = ImageStore(image_store)
    with Container(ContainerConfig(image="busybox:v1", argv=["/bin/true"]), store, workspace).create() as container:
        container.start()
        container.wait(timeout=60)
        bundle = container.bundle
    assert not os.path.exists(bundle)
