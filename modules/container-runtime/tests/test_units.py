"""Pure logic: ID mapping, netlink encoding, overlay options, image parsing.

None of these need a namespace, so they run everywhere.
"""

from __future__ import annotations

import os
import struct

import pytest

from minicon import idmap, netlink
from minicon.cli import parse_size
from minicon.image import Image, ImageStore
from minicon.mounts import Mount, OverlayRoot, default_mounts


# ---------------------------------------------------------------------------
# id mapping
# ---------------------------------------------------------------------------


def test_root_mapping_is_a_single_line():
    mapping = idmap.root_mapping(1000, 1000)
    assert [r.render() for r in mapping.uids] == ["0 1000 1"]
    assert mapping.needs_helper is False
    assert mapping.container_root_uid() == 0


def test_single_mapping_names_a_nonzero_uid_inside():
    mapping = idmap.single_mapping(10001, 10002, 1000, 1000)
    assert mapping.uids[0].render() == "10001 1000 1"
    assert mapping.gids[0].render() == "10002 1000 1"
    # Still one line, so still writable without the setuid helper.
    assert mapping.needs_helper is False
    assert mapping.maps(10001) and not mapping.maps(0)


def test_subid_mapping_needs_the_helper():
    if not idmap.subid_available():
        pytest.skip("no /etc/subuid entry for this user")
    mapping = idmap.subid_mapping()
    assert len(mapping.uids) == 2
    assert mapping.needs_helper is True
    assert mapping.maps(0) and mapping.maps(10001)


@pytest.mark.parametrize("text,expected", [
    ("", (0, 0)), ("0", (0, 0)), ("root", (0, 0)),
    ("10001", (10001, 10001)), ("10001:10002", (10001, 10002)),
])
def test_parse_user(text, expected):
    assert idmap.parse_user(text) == expected


def test_parse_user_rejects_names():
    with pytest.raises(ValueError, match="names cannot be resolved"):
        idmap.parse_user("nobody")


def test_plan_for_root_user_uses_root_strategy():
    assert idmap.plan_for_user("0").strategy == "root"


def test_plan_for_nonroot_without_helper_falls_back_to_single():
    plan = idmap.plan_for_user("10001:10001", allow_helper=False)
    assert plan.strategy == "single"
    assert plan.uids[0].inside == 10001
    # The honest consequence: uid 0 does not exist inside such a container.
    assert not plan.maps(0)


def test_multi_range_mapping_reports_the_missing_helper():
    mapping = idmap.IdMapping(
        uids=[idmap.IdRange(0, 1000, 1), idmap.IdRange(1, 100000, 65535)],
        gids=[idmap.IdRange(0, 1000, 1), idmap.IdRange(1, 100000, 65535)],
        strategy="subid",
    )
    if idmap.helper_available():
        pytest.skip("newuidmap is installed, so this path does not raise")
    with pytest.raises(RuntimeError, match="uidmap"):
        idmap.write_maps(os.getpid(), mapping)


def test_helper_argv_matches_newuidmap_calling_convention():
    ranges = [idmap.IdRange(0, 1000, 1), idmap.IdRange(1, 100000, 65535)]
    assert idmap.helper_argv("newuidmap", 4242, ranges) == [
        "newuidmap", "4242", "0", "1000", "1", "1", "100000", "65535",
    ]


# ---------------------------------------------------------------------------
# netlink encoding
# ---------------------------------------------------------------------------


def test_attribute_is_length_prefixed_and_padded():
    encoded = netlink.attribute(netlink.IFLA_IFNAME, b"eth0\0")
    length, kind = struct.unpack_from("=HH", encoded, 0)
    assert (length, kind) == (9, netlink.IFLA_IFNAME)
    # Declared length excludes padding; the buffer is padded to a 4-byte boundary.
    assert len(encoded) == 12


def test_attribute_round_trips():
    payload = netlink.attribute(1, b"ab") + netlink.attribute(2, b"cdefg")
    assert netlink.parse_attributes(payload) == {1: b"ab", 2: b"cdefg"}


def test_parse_ignores_a_truncated_trailer():
    payload = netlink.attribute(1, b"ab") + b"\x40\x00"
    assert netlink.parse_attributes(payload) == {1: b"ab"}


@pytest.mark.parametrize("value,expected", [(0, 0), (1, 4), (4, 4), (5, 8), (9, 12)])
def test_align(value, expected):
    assert netlink.align(value) == expected


# ---------------------------------------------------------------------------
# overlay options
# ---------------------------------------------------------------------------


def test_lowerdirs_are_reversed_for_overlayfs(tmp_path):
    root = OverlayRoot.prepare(str(tmp_path), ["/layers/base", "/layers/app"])
    # OCI lists bottom-first; overlayfs takes top-first. Getting this backwards
    # makes the base image win every conflict.
    assert root.options().startswith("lowerdir=/layers/app:/layers/base,")
    assert "upperdir=" in root.options() and "workdir=" in root.options()


def test_overlay_rejects_a_path_it_cannot_express(tmp_path):
    root = OverlayRoot.prepare(str(tmp_path), ["/layers/with:colon"])
    with pytest.raises(ValueError, match="':' or ','"):
        root.options()


def test_overlay_needs_at_least_one_lower(tmp_path):
    with pytest.raises(ValueError, match="at least one lower"):
        OverlayRoot.prepare(str(tmp_path), []).options()


def test_default_mounts_cover_the_required_pseudo_filesystems():
    targets = [m.target for m in default_mounts()]
    for required in ("/proc", "/dev", "/tmp", "/dev/shm"):
        assert required in targets
    # Devices are bind-mounted rather than created: mknod of a real device is
    # not permitted in a user namespace.
    assert any(m.target == "/dev/null" and m.optional for m in default_mounts())


def test_readonly_root_is_not_a_mount_in_the_plan():
    """Sealing "/" cannot happen before pivot_root -- see remount_root_readonly.

    pivot_root has to mkdir the directory the old root is parked in, and that
    mkdir lands on the root being sealed. Ordering, not preference.
    """
    from minicon.mounts import remount_root_readonly

    assert all(m.target != "/" for m in default_mounts(readonly_root=True))
    assert callable(remount_root_readonly)


# ---------------------------------------------------------------------------
# image parsing
# ---------------------------------------------------------------------------


def test_image_reads_config_fields(image_store):
    image = ImageStore(image_store).get("app:v1")
    assert image.argv == ["/bin/sh", "/app/run.sh"]
    assert image.env["PATH"] == "/bin"
    assert image.working_dir == "/app"
    assert len(image.layer_digests) == 2
    assert len(image.diff_ids) == 2


def test_unknown_image_lists_what_exists(image_store):
    with pytest.raises(KeyError, match="app:v1"):
        ImageStore(image_store).get("nope:v1")


def test_store_rejects_a_directory_that_is_not_a_layout(tmp_path):
    with pytest.raises(FileNotFoundError, match="not an OCI image layout"):
        ImageStore(str(tmp_path))


def test_layers_are_shared_between_images(image_store):
    store = ImageStore(image_store)
    base = store.get("busybox:v1")
    app = store.get("app:v1")
    # app:v1 is built on busybox:v1, so its first layer is the same blob.
    assert app.layer_digests[0] == base.layer_digests[0]


@pytest.mark.parametrize("text,expected", [
    ("512", 512), ("1K", 1024), ("256M", 256 << 20), ("1G", 1 << 30), ("1.5G", int(1.5 * (1 << 30))),
])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


# ---------------------------------------------------------------------------
# why the namespace tests do not need a single-threaded process
# ---------------------------------------------------------------------------


def test_unshare_is_only_ever_called_after_a_fork():
    """The property that lets the namespace tests run alongside everything else.

    `unshare(CLONE_NEWUSER)` fails with EINVAL in a multi-threaded process, and
    a pytest run that has already started a gRPC server is multi-threaded. That
    used to skip forty-three real tests. It never needed to: every `unshare` in
    this codebase happens in a child of `fork()`, and a forked child has
    exactly one thread — the one that called fork.

    This asserts that invariant textually, because it is the kind of thing a
    later edit breaks silently: the tests would not fail, they would go back to
    being skipped, or start failing with an opaque `Invalid argument`.

    The reachability is one step removed in the runtime — `if pid == 0:` calls
    `self._child()`, which unshares — so this walks from the forked branches
    through the functions they call, to a fixed point.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sources = sorted([*(root / "minicon").glob("*.py"), *(root / "tests").glob("*.py")])

    def called_names(node) -> set[str]:
        names = set()
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                func = call.func
                names.add(func.attr if isinstance(func, ast.Attribute)
                          else getattr(func, "id", ""))
        return names - {""}

    trees = {path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
             for path in sources}
    functions = {}                       # name -> list of definition nodes
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.setdefault(node.name, []).append(node)

    # Seed: the body of every `if <pid> == 0:` branch runs in the child, both
    # the statements themselves and whatever they call.
    fork_only: set[str] = set()
    child_lines: set[int] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                    and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
                    and isinstance(test.comparators[0], ast.Constant)
                    and test.comparators[0].value == 0):
                for statement in node.body:
                    fork_only |= called_names(statement)
                    child_lines.update(range(statement.lineno,
                                             (statement.end_lineno or statement.lineno) + 1))

    # Fixed point: anything a fork-only function calls is also fork-only.
    changed = True
    while changed:
        changed = False
        for name in list(fork_only):
            for definition in functions.get(name, []):
                for called in called_names(definition):
                    if called not in fork_only:
                        fork_only.add(called)
                        changed = True

    # The wrapper in linux.py *is* unshare; it is not a caller of it.
    safe_definitions = {node for name in fork_only for node in functions.get(name, [])}
    safe_definitions |= set(functions.get("unshare", []))
    safe_lines = {line for node in safe_definitions
                  for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)}
    safe_lines |= child_lines

    offenders = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "unshare" and node.lineno not in safe_lines:
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")

    assert not offenders, (
        "unshare() reachable outside a forked child: " + ", ".join(offenders) +
        " — this fails with EINVAL whenever the process has more than one "
        "thread. Fork first; the child gets exactly one."
    )


def test_the_thread_count_probe_reads_the_kernel_not_python():
    """`threading.active_count()` cannot see a C extension's pool, and those are
    exactly the threads that break unshare."""
    import threading

    from container_testcaps import os_thread_count

    stop = threading.Event()
    threads = [threading.Thread(target=stop.wait, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    try:
        assert os_thread_count() >= 4
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)
