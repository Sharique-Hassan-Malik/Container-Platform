"""On-disk OCI image layout, and transfer between two of them.

    <root>/oci-layout          {"imageLayoutVersion": "1.0.0"}
    <root>/index.json          tags -> manifest descriptors
    <root>/blobs/sha256/<hex>  every config, manifest and layer

`copy_image` is the interesting function. It is how a push and a pull both work
in a real registry: ask the destination which blobs it already has, send only
the rest. That single rule is what makes layer ordering matter, and the
`TransferStats` it returns is the measurement the whole project is built around.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field

from .digest import Descriptor, canonical_json, digest_of, digest_path
from .manifest import LAYOUT_VERSION, MEDIA_MANIFEST, Index, Manifest


@dataclass
class TransferStats:
    """What a push or pull actually cost."""

    blobs_sent: int = 0
    blobs_skipped: int = 0
    bytes_sent: int = 0
    bytes_skipped: int = 0
    sent_digests: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return self.bytes_sent + self.bytes_skipped

    @property
    def cache_hit_ratio(self) -> float:
        if self.total_bytes == 0:
            return 1.0
        return self.bytes_skipped / self.total_bytes

    def __str__(self) -> str:
        return (
            f"{self.blobs_sent} blobs / {self.bytes_sent / 1e6:.1f} MB transferred, "
            f"{self.blobs_skipped} blobs / {self.bytes_skipped / 1e6:.1f} MB reused "
            f"({self.cache_hit_ratio:.0%} cached)"
        )


class ImageStore:
    """An OCI image layout directory."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.blob_dir = os.path.join(self.root, "blobs", "sha256")
        os.makedirs(self.blob_dir, exist_ok=True)
        layout_file = os.path.join(self.root, "oci-layout")
        if not os.path.exists(layout_file):
            with open(layout_file, "w") as handle:
                json.dump({"imageLayoutVersion": LAYOUT_VERSION}, handle)
        if not os.path.exists(self.index_file):
            self._write_index(Index())

    # ---- blobs -------------------------------------------------------------

    @property
    def index_file(self) -> str:
        return os.path.join(self.root, "index.json")

    def blob_path(self, digest: str) -> str:
        _, encoded = digest_path(digest)
        return os.path.join(self.blob_dir, encoded)

    def has_blob(self, digest: str) -> bool:
        return os.path.exists(self.blob_path(digest))

    def blob_size(self, digest: str) -> int:
        return os.path.getsize(self.blob_path(digest))

    def put_blob(self, data: bytes) -> Descriptor:
        """Write a blob, addressed by its own hash.

        Writing an existing digest is a no-op by construction: same content,
        same path, same bytes. That is deduplication -- there is no separate
        mechanism for it.
        """
        digest = digest_of(data)
        path = self.blob_path(digest)
        if not os.path.exists(path):
            tmp = path + ".tmp"
            with open(tmp, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        return Descriptor("application/octet-stream", digest, len(data))

    def get_blob(self, digest: str) -> bytes:
        with open(self.blob_path(digest), "rb") as handle:
            data = handle.read()
        # Verification is the point of content addressing; skipping it would
        # make the store a plain cache with extra steps.
        if digest_of(data) != digest:
            raise ValueError(f"blob {digest} failed digest verification")
        return data

    # ---- index -------------------------------------------------------------

    def read_index(self) -> Index:
        with open(self.index_file) as handle:
            return Index.from_json(json.load(handle))

    def _write_index(self, index: Index) -> None:
        tmp = self.index_file + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(canonical_json(index.to_json()))
        os.replace(tmp, self.index_file)

    def tags(self) -> list[str]:
        from .manifest import ANNOTATION_REF

        return sorted(
            d.annotations[ANNOTATION_REF]
            for d in self.read_index().manifests
            if ANNOTATION_REF in d.annotations
        )

    # ---- images ------------------------------------------------------------

    def put_image(self, reference: str, manifest: Manifest) -> Descriptor:
        """Store a manifest and bind a tag to it. Layers must already be present."""
        for layer in manifest.layers:
            if not self.has_blob(layer.digest):
                raise ValueError(f"layer {layer.digest} missing; push blobs before the manifest")
        if not self.has_blob(manifest.config.digest):
            raise ValueError(f"config {manifest.config.digest} missing")
        blob, descriptor = manifest.serialise()
        self.put_blob(blob)
        index = self.read_index()
        index.upsert(reference, descriptor)
        self._write_index(index)
        return descriptor

    def get_manifest(self, reference: str) -> Manifest:
        index = self.read_index()
        descriptor = index.find(reference)
        if descriptor is None:
            # A digest is also a valid reference, and is how a rollback pins an
            # exact image rather than whatever the tag points at today.
            if reference.startswith("sha256:") and self.has_blob(reference):
                return Manifest.from_json(json.loads(self.get_blob(reference)))
            raise KeyError(f"no such image: {reference} (have {self.tags()})")
        return Manifest.from_json(json.loads(self.get_blob(descriptor.digest)))

    def get_config(self, reference: str):
        from .config import ImageConfig

        manifest = self.get_manifest(reference)
        return ImageConfig.from_json(json.loads(self.get_blob(manifest.config.digest)))

    def image_size(self, reference: str) -> int:
        """Total compressed bytes a fresh puller would download."""
        manifest = self.get_manifest(reference)
        return manifest.total_layer_bytes + manifest.config.size

    def unpack(self, reference: str, dest: str) -> None:
        """Materialise an image's root filesystem by applying layers in order."""
        from .layer import MEDIA_LAYER_GZIP, extract_layer

        manifest = self.get_manifest(reference)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        os.makedirs(dest, exist_ok=True)
        for descriptor in manifest.layers:
            extract_layer(
                self.get_blob(descriptor.digest),
                dest,
                compressed=descriptor.media_type == MEDIA_LAYER_GZIP,
            )


def copy_image(src: ImageStore, dst: ImageStore, reference: str) -> TransferStats:
    """Transfer an image, sending only blobs the destination lacks.

    This is both `push` and `pull` -- they are the same operation with the
    stores swapped, which is why a registry needs no separate code path for
    each. The stats show where the bytes went.
    """
    manifest = src.get_manifest(reference)
    stats = TransferStats()

    for descriptor in list(manifest.layers) + [manifest.config]:
        if dst.has_blob(descriptor.digest):
            stats.blobs_skipped += 1
            stats.bytes_skipped += descriptor.size
            continue
        dst.put_blob(src.get_blob(descriptor.digest))
        stats.blobs_sent += 1
        stats.bytes_sent += descriptor.size
        stats.sent_digests.append(descriptor.digest)

    blob, descriptor = manifest.serialise()
    dst.put_blob(blob)
    index = dst.read_index()
    index.upsert(reference, Descriptor(MEDIA_MANIFEST, descriptor.digest, descriptor.size))
    dst._write_index(index)
    return stats
