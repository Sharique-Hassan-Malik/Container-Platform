"""Manifest and index: the two documents that tie an image together.

    index.json  ──▶  manifest  ──┬──▶ config blob
                                 └──▶ layer blobs

The manifest is a flat list of descriptors, which is exactly why a registry can
answer "do you already have this?" per blob and transfer only the difference.
The index is a list of manifests keyed by platform and by tag annotation; a
single-platform image still needs one, because the layout spec makes
`index.json` the only entry point with a fixed name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .digest import Descriptor, canonical_json, digest_of

MEDIA_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_INDEX = "application/vnd.oci.image.index.v1+json"
ANNOTATION_REF = "org.opencontainers.image.ref.name"
LAYOUT_VERSION = "1.0.0"


@dataclass
class Manifest:
    config: Descriptor
    layers: list[Descriptor] = field(default_factory=list)
    annotations: dict[str, str] = field(default_factory=dict)
    subject: Descriptor | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schemaVersion": 2,
            "mediaType": MEDIA_MANIFEST,
            "config": self.config.to_json(),
            "layers": [layer.to_json() for layer in self.layers],
        }
        if self.subject is not None:
            out["subject"] = self.subject.to_json()
        if self.annotations:
            out["annotations"] = dict(sorted(self.annotations.items()))
        return out

    def serialise(self) -> tuple[bytes, Descriptor]:
        blob = canonical_json(self.to_json())
        return blob, Descriptor(MEDIA_MANIFEST, digest_of(blob), len(blob))

    @property
    def total_layer_bytes(self) -> int:
        return sum(layer.size for layer in self.layers)

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "Manifest":
        return cls(
            config=Descriptor.from_json(obj["config"]),
            layers=[Descriptor.from_json(d) for d in obj.get("layers", [])],
            annotations=dict(obj.get("annotations", {})),
            subject=Descriptor.from_json(obj["subject"]) if obj.get("subject") else None,
        )


@dataclass
class Index:
    manifests: list[Descriptor] = field(default_factory=list)
    annotations: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schemaVersion": 2,
            "mediaType": MEDIA_INDEX,
            "manifests": [m.to_json() for m in self.manifests],
        }
        if self.annotations:
            out["annotations"] = dict(sorted(self.annotations.items()))
        return out

    def serialise(self) -> tuple[bytes, Descriptor]:
        blob = canonical_json(self.to_json())
        return blob, Descriptor(MEDIA_INDEX, digest_of(blob), len(blob))

    def find(self, reference: str) -> Descriptor | None:
        for descriptor in self.manifests:
            if descriptor.annotations.get(ANNOTATION_REF) == reference:
                return descriptor
        return None

    def upsert(self, reference: str, descriptor: Descriptor) -> None:
        """Point a tag at a manifest, replacing any previous binding.

        Tags are mutable, blobs are not. Retagging rewrites one line of
        index.json and touches no content, which is the whole reason a
        content-addressed store can support moving tags at all.
        """
        tagged = Descriptor(
            descriptor.media_type,
            descriptor.digest,
            descriptor.size,
            {**descriptor.annotations, ANNOTATION_REF: reference},
        )
        for i, existing in enumerate(self.manifests):
            if existing.annotations.get(ANNOTATION_REF) == reference:
                self.manifests[i] = tagged
                return
        self.manifests.append(tagged)

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "Index":
        return cls(
            manifests=[Descriptor.from_json(d) for d in obj.get("manifests", [])],
            annotations=dict(obj.get("annotations", {})),
        )
