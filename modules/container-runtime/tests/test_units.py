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
