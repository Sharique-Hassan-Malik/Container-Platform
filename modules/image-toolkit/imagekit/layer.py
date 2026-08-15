"""Layer construction: deterministic tar, deterministic gzip, whiteouts.

A layer is a tar archive of a filesystem *diff*, gzipped. Two properties matter
and neither is automatic:

**Determinism.** `tar` records mtimes, uids, usernames and directory ordering.
All four vary between machines and between runs, so the naive archive of an
unchanged tree hashes differently every time, which destroys both build caching
and reproducibility. Everything variable is normalised here.

**Deletions.** A tar can only say "this file exists". Removing a file in a layer
is encoded as a sibling marker named ``.wh.<name>``; clearing a whole directory
is ``.wh..wh..opq``. The extractor interprets those instead of unpacking them.
"""

from __future__ import annotations

import gzip
import io
import os
import posixpath
import tarfile
from dataclasses import dataclass

from .digest import digest_of, sha256_hex

WHITEOUT_PREFIX = ".wh."
OPAQUE_MARKER = ".wh..wh..opq"

MEDIA_LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
MEDIA_LAYER_TAR = "application/vnd.oci.image.layer.v1.tar"

# Fixed epoch for every archive member. The OCI spec has nothing to say about
# mtimes; reproducible-build tooling has settled on zeroing them, and honouring
# SOURCE_DATE_EPOCH when the caller wants a real timestamp.
FIXED_MTIME = 0


@dataclass(frozen=True)
class Layer:
    """A built layer, addressed both ways (see digest.py for why)."""

    diff_id: str          # sha256 of the uncompressed tar
    digest: str           # sha256 of the gzipped blob
    size: int             # size of the gzipped blob
    uncompressed_size: int
    blob: bytes
    media_type: str = MEDIA_LAYER_GZIP

    @property
    def compression_ratio(self) -> float:
        if self.uncompressed_size == 0:
            return 1.0
        return self.size / self.uncompressed_size


def _normalise(info: tarfile.TarInfo, mtime: int) -> tarfile.TarInfo:
    """Strip every field that varies between machines but not between contents.

    Ownership becomes root:root with empty names (a numeric uid that happens to
    exist on the build host is meaningless inside the image, and a *name* forces
    a PAX header on some tars). Modes are reduced to the executable bit, because
    umask differences otherwise leak into the digest.
    """
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = mtime
    if info.isdir():
        info.mode = 0o755
    elif info.issym():
        info.mode = 0o777
    else:
        info.mode = 0o755 if (info.mode & 0o100) else 0o644
    return info


def _sorted_walk(root: str) -> list[tuple[str, str]]:
    """Yield (archive_path, filesystem_path) in a stable, parent-before-child order.

    ``os.walk`` returns directory entries in inode order, which differs between
    two directories holding identical files. Sorting is what makes the tar
    byte-identical across machines.
    """
    entries: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        for name in dirnames + filenames:
            fs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(fs_path, root)
            entries.append((rel.replace(os.sep, "/"), fs_path))
    entries.sort(key=lambda pair: pair[0])
    return entries


def write_tar(
    members: list[tuple[str, str]],
    *,
    whiteouts: list[str] | None = None,
    mtime: int = FIXED_MTIME,
) -> bytes:
    """Build one uncompressed layer tar.

    ``members`` is a list of (archive_path, filesystem_path) pairs, already in
    the order they should appear. ``whiteouts`` are archive paths to mark as
    deleted; they are emitted as zero-length ``.wh.`` markers next to where the
    file used to be.
    """
    buf = io.BytesIO()
    # format=GNU_FORMAT keeps long paths working without PAX headers, whose
    # embedded timestamps would reintroduce nondeterminism.
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for archive_path, fs_path in members:
            info = tar.gettarinfo(fs_path, arcname=archive_path)
            _normalise(info, mtime)
            if info.isreg():
                with open(fs_path, "rb") as handle:
                    tar.addfile(info, handle)
            else:
                # Directories, symlinks and devices carry no payload stream.
                tar.addfile(info)
        for path in sorted(whiteouts or []):
            parent, name = posixpath.split(path)
            marker = posixpath.join(parent, WHITEOUT_PREFIX + name) if parent else WHITEOUT_PREFIX + name
            info = tarfile.TarInfo(marker)
            info.size = 0
            info.type = tarfile.REGTYPE
            _normalise(info, mtime)
            info.mode = 0o644
            tar.addfile(info)
    return buf.getvalue()


def gzip_deterministic(data: bytes, level: int = 6, mtime: int = 0) -> bytes:
    """Gzip with the header's MTIME field pinned.

    The gzip *container* stores a timestamp independently of anything in the
    payload, so compressing identical bytes twice normally yields two different
    blobs and therefore two different layer digests. Passing ``mtime=0`` and an
    empty filename removes both variable fields; the OS byte CPython writes is
    already the constant 0xFF ("unknown").

    ``mtime`` is a parameter rather than a constant so `bench/layer_order.py`
    can build the non-reproducible variant and show what it costs.
    """
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=level, mtime=mtime) as gz:
        gz.write(data)
    return buf.getvalue()


def make_layer(
    members: list[tuple[str, str]],
    *,
    whiteouts: list[str] | None = None,
    compress: bool = True,
    level: int = 6,
    deterministic: bool = True,
) -> Layer:
    """Build a layer and compute both of its digests in one pass.

    With ``deterministic=False`` the archive carries real mtimes and the gzip
    header carries the wall clock, which is what an unconfigured `tar | gzip`
    produces. Identical content then hashes differently on every build -- so
    every rebuild re-uploads every layer, whether or not anything changed.
    """
    import time as _time

    mtime = FIXED_MTIME if deterministic else int(_time.time())
    tar_bytes = write_tar(members, whiteouts=whiteouts, mtime=mtime)
    diff_id = "sha256:" + sha256_hex(tar_bytes)
    if not compress:
        return Layer(
            diff_id=diff_id,
            digest=diff_id,
            size=len(tar_bytes),
            uncompressed_size=len(tar_bytes),
            blob=tar_bytes,
            media_type=MEDIA_LAYER_TAR,
        )
    blob = gzip_deterministic(tar_bytes, level=level, mtime=mtime)
    return Layer(
        diff_id=diff_id,
        digest=digest_of(blob),
        size=len(blob),
        uncompressed_size=len(tar_bytes),
        blob=blob,
    )


def layer_from_directory(root: str, **kwargs) -> Layer:
    """Build a layer containing an entire directory tree."""
    return make_layer(_sorted_walk(root), **kwargs)


def extract_layer(blob: bytes, dest: str, *, compressed: bool = True) -> None:
    """Apply one layer to a root filesystem, honouring whiteouts.

    Order matters: a whiteout must delete the path *before* later members of the
    same layer are unpacked, otherwise a layer that replaces a directory with a
    file would delete what it just wrote. Tar order guarantees the marker
    precedes nothing in particular, so whiteouts are collected in a first pass.
    """
    raw = gzip.decompress(blob) if compressed else blob
    os.makedirs(dest, exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
        members = tar.getmembers()

        for info in members:
            parent, name = posixpath.split(info.name)
            if name == OPAQUE_MARKER:
                target = os.path.join(dest, parent) if parent else dest
                if os.path.isdir(target):
                    for entry in os.listdir(target):
                        _remove(os.path.join(target, entry))
            elif name.startswith(WHITEOUT_PREFIX):
                real = posixpath.join(parent, name[len(WHITEOUT_PREFIX):]) if parent else name[len(WHITEOUT_PREFIX):]
                _remove(os.path.join(dest, real))

        for info in members:
            name = posixpath.basename(info.name)
            if name.startswith(WHITEOUT_PREFIX):
                continue
            target = os.path.join(dest, info.name)
            if not _within(dest, target):
                # A layer claiming ../../etc/passwd is a real registry attack,
                # not a hypothetical one. Refuse rather than sanitise.
                raise ValueError(f"layer escapes root: {info.name!r}")
            # A replaced path may be a directory where the new entry is a file.
            if os.path.lexists(target) and not (info.isdir() and os.path.isdir(target)):
                _remove(target)
            # `filter="tar"` is belt-and-braces on top of the escape check
            # above: it also drops setuid bits and absolute paths, and it is the
            # default from Python 3.14 anyway.
            tar.extract(info, dest, set_attrs=False, filter="tar")
            if info.isreg() or info.isdir():
                os.chmod(target, info.mode)


def _within(root: str, path: str) -> bool:
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(path)
    return path_abs == root_abs or path_abs.startswith(root_abs + os.sep)


def _remove(path: str) -> None:
    import shutil

    if not os.path.lexists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)
