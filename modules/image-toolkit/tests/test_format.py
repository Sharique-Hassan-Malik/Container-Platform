"""Digests, layers, configs, manifests -- the format, independent of the builder."""

from __future__ import annotations

import gzip
import io
import os
import struct
import tarfile

import pytest

from imagekit import Descriptor, ImageConfig, Index, Manifest, chain_id, digest_of, extract_layer, make_layer
from imagekit.config import HealthCheck
from imagekit.digest import canonical_json, digest_path, sha256_hex
from imagekit.layer import gzip_deterministic, write_tar


# ---------------------------------------------------------------------------
# digests
# ---------------------------------------------------------------------------


def test_digest_is_sha256_of_content():
    assert digest_of(b"hello") == "sha256:" + sha256_hex(b"hello")


@pytest.mark.parametrize("bad", ["abc", "sha512:" + "0" * 64, "sha256:xyz", "sha256:" + "0" * 63])
def test_malformed_digests_rejected(bad):
    with pytest.raises(ValueError):
        digest_path(bad)


def test_chain_id_matches_oci_definition():
    a, b = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    assert chain_id([a]) == a
    assert chain_id([a, b]) == "sha256:" + sha256_hex(f"{a} {b}".encode())


def test_chain_id_is_order_sensitive():
    a, b = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    assert chain_id([a, b]) != chain_id([b, a])


def test_canonical_json_ignores_key_insertion_order():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert b" " not in canonical_json({"a": 1, "b": 2})


# ---------------------------------------------------------------------------
# layers
# ---------------------------------------------------------------------------


def _tree(tmp_path, files: dict[str, bytes]) -> str:
    root = tmp_path / "tree"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return str(root)


def test_layer_has_two_distinct_digests(tmp_path):
    root = _tree(tmp_path, {"a.txt": b"x" * 4096})
    layer = make_layer([("a.txt", os.path.join(root, "a.txt"))])
    # diff_id names the tar, digest names the gzip. Conflating them is the
    # classic bug: the config would list a blob nobody can fetch.
    assert layer.diff_id != layer.digest
    assert layer.size < layer.uncompressed_size


def test_identical_content_gives_identical_digest(tmp_path):
    first = _tree(tmp_path / "a", {"f": b"same"})
    second = _tree(tmp_path / "b", {"f": b"same"})
    os.utime(os.path.join(second, "f"), (12345, 12345))
    left = make_layer([("f", os.path.join(first, "f"))])
    right = make_layer([("f", os.path.join(second, "f"))])
    assert left.digest == right.digest


def test_nondeterministic_layers_change_digest_for_same_content(tmp_path):
    root = _tree(tmp_path, {"f": b"same"})
    members = [("f", os.path.join(root, "f"))]
    a = make_layer(members, deterministic=False)
    import time

    time.sleep(1.05)  # the gzip header stores whole seconds
    b = make_layer(members, deterministic=False)
    assert a.digest != b.digest
    assert make_layer(members).digest == make_layer(members).digest


def test_tar_normalises_ownership_and_time(tmp_path):
    root = _tree(tmp_path, {"f": b"x"})
    raw = write_tar([("f", os.path.join(root, "f"))])
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        info = tar.getmember("f")
    assert (info.uid, info.gid, info.uname, info.gname, info.mtime) == (0, 0, "", "", 0)


def test_gzip_header_mtime_is_zeroed():
    blob = gzip_deterministic(b"payload")
    # bytes 4..8 of a gzip header are MTIME, little endian.
    assert struct.unpack("<I", blob[4:8])[0] == 0
    assert gzip.decompress(blob) == b"payload"


def test_member_order_does_not_depend_on_argument_order(tmp_path):
    root = _tree(tmp_path, {"b": b"2", "a": b"1"})
    forward = [("a", os.path.join(root, "a")), ("b", os.path.join(root, "b"))]
    assert make_layer(forward).digest == make_layer(forward).digest


def test_whiteout_deletes_on_extract(tmp_path):
    lower = _tree(tmp_path, {"keep": b"1", "gone": b"2"})
    dest = str(tmp_path / "root")
    extract_layer(make_layer([(n, os.path.join(lower, n)) for n in ("keep", "gone")]).blob, dest)
    assert os.path.exists(os.path.join(dest, "gone"))

    extract_layer(make_layer([], whiteouts=["gone"]).blob, dest)
    assert os.path.exists(os.path.join(dest, "keep"))
    assert not os.path.exists(os.path.join(dest, "gone"))
    # The marker itself must never land in the filesystem.
    assert not os.path.exists(os.path.join(dest, ".wh.gone"))


def test_whiteout_removes_a_whole_directory(tmp_path):
    src = _tree(tmp_path, {"d/one": b"1", "d/two": b"2"})
    dest = str(tmp_path / "root")
    members = [("d", os.path.join(src, "d"))] + [(f"d/{n}", os.path.join(src, "d", n)) for n in ("one", "two")]
    extract_layer(make_layer(members).blob, dest)
    extract_layer(make_layer([], whiteouts=["d"]).blob, dest)
    assert not os.path.exists(os.path.join(dest, "d"))


def test_layer_cannot_escape_the_root(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("../escaped")
        info.size = 0
        tar.addfile(info)
    blob = gzip_deterministic(buf.getvalue())
    with pytest.raises(ValueError, match="escapes root"):
        extract_layer(blob, str(tmp_path / "root"))


def test_uncompressed_layer_uses_tar_media_type(tmp_path):
    root = _tree(tmp_path, {"f": b"x" * 100})
    layer = make_layer([("f", os.path.join(root, "f"))], compress=False)
    assert layer.media_type.endswith("tar")
    assert layer.diff_id == layer.digest  # no compression, so both name the same bytes


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_round_trips():
    config = ImageConfig(
        env={"A": "1", "B": "2"},
        entrypoint=["python3", "serve.py"],
        user="10001:10001",
        exposed_ports=["8080/tcp"],
        healthcheck=HealthCheck(test=["CMD", "true"], interval_s=15.0, retries=2),
        diff_ids=["sha256:" + "a" * 64],
    )
    restored = ImageConfig.from_json(config.to_json())
    assert restored.env == config.env
    assert restored.entrypoint == config.entrypoint
    assert restored.user == config.user
    assert restored.healthcheck.interval_s == 15.0
    assert restored.healthcheck.retries == 2


def test_healthcheck_durations_are_nanoseconds_on_the_wire():
    assert HealthCheck(test=["CMD", "true"], interval_s=30.0).to_json()["Interval"] == 30_000_000_000


def test_config_digest_is_stable_across_serialisations():
    config = ImageConfig(env={"B": "2", "A": "1"}, diff_ids=["sha256:" + "a" * 64])
    assert config.serialise()[1].digest == config.copy().serialise()[1].digest


@pytest.mark.parametrize("user,expected", [("", True), ("root", True), ("0", True), ("10001", False)])
def test_runs_as_root_detection(user, expected):
    assert ImageConfig(user=user).runs_as_root() is expected


# ---------------------------------------------------------------------------
# manifest / index
# ---------------------------------------------------------------------------


def _descriptor(char: str, size: int = 10) -> Descriptor:
    return Descriptor("application/octet-stream", "sha256:" + char * 64, size)


def test_manifest_round_trips_and_sums_layers():
    manifest = Manifest(config=_descriptor("c", 5), layers=[_descriptor("a", 10), _descriptor("b", 20)])
    assert manifest.total_layer_bytes == 30
    assert Manifest.from_json(manifest.to_json()).layers == manifest.layers


def test_index_retag_replaces_rather_than_appends():
    index = Index()
    index.upsert("app:v1", _descriptor("a"))
    index.upsert("app:v1", _descriptor("b"))
    assert len(index.manifests) == 1
    assert index.find("app:v1").digest == "sha256:" + "b" * 64
    assert index.find("app:v2") is None
