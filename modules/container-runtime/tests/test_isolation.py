"""Isolation, asserted against the kernel rather than against `unshare`'s return code.

Two processes share a namespace exactly when `/proc/<pid>/ns/<type>` reads back
the same `type:[inode]` string. Every claim below is checked that way, or by
observing something the container genuinely cannot see.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

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


# ---------------------------------------------------------------------------
# the shared layer cache under concurrency
# ---------------------------------------------------------------------------


def test_concurrent_unpacks_of_one_layer_do_not_destroy_each_other(image_store, tmp_path):
    """Four replicas of one image start at once and all find the layer missing.

    Without a per-layer lock they all unpack into the same directory, each
    beginning by deleting what the others are using, and whichever container
    loses gets `exec: No such file or directory` — intermittently, on a
    rollout, which is the worst way to find a bug.

    Runs the real unpack path in four processes, then checks the layer is
    complete and every file readable.
    """
    import concurrent.futures

    from minicon.image import ImageStore, LayerCache

    store = ImageStore(image_store)
    image = store.get("app:v1")
    cache_root = str(tmp_path / "layers")

    def unpack_all() -> list[str]:
        return LayerCache(cache_root).ensure(store, image)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = [future.result() for future in
                   [pool.submit(unpack_all) for _ in range(4)]]

    # Every caller must get the same lowerdirs, and each must really be there.
    assert len({tuple(r) for r in results}) == 1, "callers disagreed about the layers"
    for lower in results[0]:
        assert os.path.isdir(lower), f"{lower} vanished"
        for root, _dirs, files in os.walk(lower):
            for name in files:
                path = os.path.join(root, name)
                if os.path.islink(path):
                    continue
                assert os.path.exists(path), f"{path} was removed mid-unpack"


def test_a_second_unpack_leaves_a_ready_layer_alone(image_store, tmp_path):
    """The check and the unpack are one step, so an already-ready layer is not
    re-extracted underneath a container that is using it."""
    from minicon.image import ImageStore, LayerCache

    store = ImageStore(image_store)
    image = store.get("app:v1")
    cache_root = str(tmp_path / "layers")

    first = LayerCache(cache_root)
    first.ensure(store, image)
    assert first.unpacked, "nothing was unpacked on a cold cache"

    witness = os.path.join(first.path_for(image.layer_digests[0]), "witness")
    with open(witness, "w") as handle:
        handle.write("still here")

    second = LayerCache(cache_root)
    second.ensure(store, image)
    assert second.unpacked == [], "a ready layer was unpacked again"
    assert os.path.exists(witness), "a ready layer was deleted and re-extracted"


# ---------------------------------------------------------------------------
# run_in_userns when forking cannot give a single-threaded child
# ---------------------------------------------------------------------------


def _remove_the_env_target() -> None:
    """Module level, so it is picklable and can cross an exec."""
    import shutil

    shutil.rmtree(os.environ["MINICON_TEST_TARGET"], ignore_errors=True)


@requires_userns
def test_run_in_userns_falls_back_to_a_fresh_process_under_the_fork_hazard(tmp_path, monkeypatch):
    """gRPC serving means a forked child is not single-threaded, so the fork
    path cannot work. A picklable callable goes through an exec instead."""
    pytest.importorskip("grpc")
    import grpc
    from concurrent import futures

    from minicon import linux
    from minicon.image import run_in_userns

    monkeypatch.setenv("GRPC_ENABLE_FORK_SUPPORT", "1")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        assert linux.fork_thread_hazard()[0], "the hazard did not materialise"

        target = tmp_path / "doomed"
        target.mkdir()
        monkeypatch.setenv("MINICON_TEST_TARGET", str(target))
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))

        run_in_userns(_remove_the_env_target)
        assert not target.exists()
    finally:
        server.stop(0).wait()


@requires_userns
def test_an_unpicklable_callable_under_the_hazard_says_why(monkeypatch):
    """A lambda cannot cross an exec. The error must name the cause and the
    alternative, not surface an EINVAL from inside the helper."""
    pytest.importorskip("grpc")
    import grpc
    from concurrent import futures

    from minicon.image import run_in_userns

    monkeypatch.setenv("GRPC_ENABLE_FORK_SUPPORT", "1")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        with pytest.raises(RuntimeError, match="run_helper"):
            run_in_userns(lambda: None)
    finally:
        server.stop(0).wait()
