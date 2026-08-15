"""Content addressing.

Every byte string that enters an image is named by its own SHA-256. That is the
single idea the whole format rests on: a *descriptor* (media type + digest +
size) is a globally valid pointer to content, so two registries, two hosts and
two builds refer to the same layer by the same name without coordinating.

The subtlety that bites people is that a layer has **two** digests:

    diff_id  = sha256(uncompressed tar)   -- what the image config lists
    digest   = sha256(compressed blob)    -- what the manifest lists

They are not interchangeable. `rootfs.diff_ids` uses the first because the
runtime cares about the filesystem content, which must not change if someone
re-compresses the blob at a different level. The manifest uses the second
because the registry stores and verifies the bytes it actually transfers.
`layer.py` computes both in one pass.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_of(data: bytes) -> str:
    """Canonical digest string for a blob: ``sha256:<64 hex chars>``."""
    return "sha256:" + sha256_hex(data)


def digest_path(digest: str) -> tuple[str, str]:
    """Split ``sha256:abc...`` into the (algorithm, encoded) pair used on disk.

    The OCI layout stores blobs at ``blobs/<algorithm>/<encoded>``, which is why
    the separator is a colon and not a slash: the digest is one opaque token.
    """
    if ":" not in digest:
        raise ValueError(f"malformed digest, expected 'alg:hex': {digest!r}")
    algorithm, encoded = digest.split(":", 1)
    if algorithm != "sha256":
        raise ValueError(f"unsupported digest algorithm: {algorithm!r}")
    if len(encoded) != 64 or not all(c in "0123456789abcdef" for c in encoded):
        raise ValueError(f"malformed sha256 digest: {digest!r}")
    return algorithm, encoded


def canonical_json(obj: Any) -> bytes:
    """Serialise deterministically.

    A config blob is addressed by the hash of its own bytes, so key order and
    whitespace are load-bearing: re-serialising the same object differently
    changes the image ID. Sorted keys and no spaces is the same convention the
    OCI spec recommends for canonical form.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class Descriptor:
    """An OCI content descriptor: the pointer type used everywhere."""

    media_type: str
    digest: str
    size: int
    annotations: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mediaType": self.media_type,
            "digest": self.digest,
            "size": self.size,
        }
        if self.annotations:
            out["annotations"] = dict(self.annotations)
        return out

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "Descriptor":
        return cls(
            media_type=obj["mediaType"],
            digest=obj["digest"],
            size=int(obj["size"]),
            annotations=dict(obj.get("annotations", {})),
        )


def chain_id(diff_ids: list[str]) -> str:
    """Fold a list of diff_ids into the identity of the resulting filesystem.

    Defined by the OCI spec as::

        ChainID([a])        = a
        ChainID([a, b, ..]) = ChainID([sha256(a + " " + b), ..])

    A layer digest names *a diff*; a chain ID names *the state after applying a
    prefix of the diffs*. The build cache has to key on the second, because
    replaying the same instruction on top of a different base must not hit.
    """
    if not diff_ids:
        raise ValueError("chain_id requires at least one diff_id")
    result = diff_ids[0]
    for diff_id in diff_ids[1:]:
        result = "sha256:" + sha256_hex(f"{result} {diff_id}".encode())
    return result
