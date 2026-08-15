"""The store and the transfer that makes layer caching observable."""

from __future__ import annotations

import json
import os

import pytest

from imagekit import Descriptor, ImageConfig, ImageStore, Manifest, copy_image, make_layer
from imagekit.manifest import LAYOUT_VERSION


@pytest.fixture
def store(tmp_path):
    return ImageStore(str(tmp_path / "images"))


def _layer(tmp_path, name: str, content: bytes):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return make_layer([(name, str(path))])


def _image(store, tmp_path, reference: str, layers, **config_kwargs):
    descriptors = []
    for layer in layers:
        store.put_blob(layer.blob)
        descriptors.append(Descriptor(layer.media_type, layer.digest, layer.size))
    config = ImageConfig(diff_ids=[layer.diff_id for layer in layers], **config_kwargs)
    blob, config_descriptor = config.serialise()
    store.put_blob(blob)
    return store.put_image(reference, Manifest(config=config_descriptor, layers=descriptors))


def test_layout_files_are_created(store):
    with open(os.path.join(store.root, "oci-layout")) as handle:
        assert json.load(handle)["imageLayoutVersion"] == LAYOUT_VERSION
    assert os.path.isdir(os.path.join(store.root, "blobs", "sha256"))


def test_writing_the_same_content_twice_stores_one_blob(store):
    first = store.put_blob(b"same content")
    second = store.put_blob(b"same content")
    assert first.digest == second.digest
    assert len(os.listdir(store.blob_dir)) == 1


def test_blob_read_verifies_its_digest(store):
    descriptor = store.put_blob(b"trustworthy")
    with open(store.blob_path(descriptor.digest), "wb") as handle:
        handle.write(b"tampered")
    with pytest.raises(ValueError, match="digest verification"):
        store.get_blob(descriptor.digest)


def test_manifest_requires_its_layers_to_be_present(store, tmp_path):
    layer = _layer(tmp_path, "f", b"x")
    config = ImageConfig(diff_ids=[layer.diff_id])
    blob, config_descriptor = config.serialise()
    store.put_blob(blob)
    manifest = Manifest(config=config_descriptor, layers=[Descriptor(layer.media_type, layer.digest, layer.size)])
    with pytest.raises(ValueError, match="missing"):
        store.put_image("broken:v1", manifest)


def test_unknown_reference_lists_what_is_available(store, tmp_path):
    _image(store, tmp_path, "known:v1", [_layer(tmp_path, "f", b"x")])
    with pytest.raises(KeyError, match="known:v1"):
        store.get_manifest("missing:v1")


def test_an_image_can_be_addressed_by_manifest_digest(store, tmp_path):
    descriptor = _image(store, tmp_path, "app:v1", [_layer(tmp_path, "f", b"x")])
    assert store.get_manifest(descriptor.digest).config == store.get_manifest("app:v1").config


def test_retagging_moves_the_tag_without_copying_content(store, tmp_path):
    _image(store, tmp_path, "app:v1", [_layer(tmp_path, "f", b"one")])
    blobs_before = set(os.listdir(store.blob_dir))
    index = store.read_index()
    index.upsert("app:latest", index.find("app:v1"))
    store._write_index(index)
    assert set(os.listdir(store.blob_dir)) == blobs_before
    assert sorted(store.tags()) == ["app:latest", "app:v1"]


def test_later_layers_overwrite_earlier_ones(store, tmp_path):
    first = _layer(tmp_path / "a", "shared.txt", b"first")
    second = _layer(tmp_path / "b", "shared.txt", b"second")
    _image(store, tmp_path, "app:v1", [first, second])
    root = str(tmp_path / "root")
    store.unpack("app:v1", root)
    with open(os.path.join(root, "shared.txt")) as handle:
        assert handle.read() == "second"


def test_push_sends_everything_the_first_time(store, tmp_path):
    _image(store, tmp_path, "app:v1", [_layer(tmp_path, "f", b"x" * 5000)])
    registry = ImageStore(str(tmp_path / "registry"))
    stats = copy_image(store, registry, "app:v1")
    assert stats.blobs_skipped == 0
    assert stats.bytes_sent > 0
    assert registry.get_manifest("app:v1").layers == store.get_manifest("app:v1").layers


def test_push_of_a_shared_layer_sends_only_the_difference(store, tmp_path):
    # Incompressible on purpose: a run of identical bytes deflates to a few
    # hundred, which would make the "big" layer smaller than the two configs
    # and the comparison meaningless.
    shared = _layer(tmp_path / "shared", "base.bin", os.urandom(200_000))
    _image(store, tmp_path, "app:v1", [shared, _layer(tmp_path / "one", "app.py", b"one")])
    _image(store, tmp_path, "app:v2", [shared, _layer(tmp_path / "two", "app.py", b"two-ish")])

    registry = ImageStore(str(tmp_path / "registry"))
    copy_image(store, registry, "app:v1")
    second = copy_image(store, registry, "app:v2")

    assert shared.digest not in second.sent_digests
    assert second.bytes_skipped > second.bytes_sent
    assert second.cache_hit_ratio > 0.9


def test_transfer_stats_account_for_every_byte(store, tmp_path):
    _image(store, tmp_path, "app:v1", [_layer(tmp_path, "f", b"x" * 1000)])
    registry = ImageStore(str(tmp_path / "registry"))
    first = copy_image(store, registry, "app:v1")
    second = copy_image(store, registry, "app:v1")
    assert first.total_bytes == second.total_bytes
    assert second.bytes_sent == 0
    assert second.cache_hit_ratio == 1.0


def test_image_size_counts_layers_and_config(store, tmp_path):
    layer = _layer(tmp_path, "f", b"x" * 4096)
    _image(store, tmp_path, "app:v1", [layer])
    manifest = store.get_manifest("app:v1")
    assert store.image_size("app:v1") == layer.size + manifest.config.size
