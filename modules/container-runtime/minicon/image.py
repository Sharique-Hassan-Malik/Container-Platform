"""Reading an OCI image layout, and unpacking layers into overlay lowerdirs.

This consumes exactly what `image-toolkit` produces -- an
`oci-layout` directory of content-addressed blobs -- but reads it with the
standard library rather than importing that project, because an image format
whose only reader is its own writer has not been tested against anything.

The interesting work is whiteout translation. OCI and overlayfs both express
"this file is deleted in this layer", and they disagree about how:

    OCI tar                     overlayfs
    .wh.<name>          ──▶     character device 0:0 named <name>
    .wh..wh..opq        ──▶     xattr overlay.opaque="y" on the directory

The translation needs `CAP_MKNOD`, which an unprivileged process has *inside a
user namespace it created*, so unpacking runs in a short-lived namespace helper
-- the same trick `podman unshare` exposes. Device nodes made this way cannot be
opened, which does not matter: an overlayfs whiteout is a marker the filesystem
inspects, never a device anyone reads.

Getting this right is what allows layers to be **shared** rather than merged. A
second container from the same image adds one empty upper directory and no
copying at all.
"""

from __future__ import annotations

import gzip
import io
import json
import json
import os
import subprocess
import sys
from pathlib import Path
import shutil
import stat
import tarfile
from dataclasses import dataclass, field

from . import idmap, linux

WHITEOUT_PREFIX = ".wh."
OPAQUE_MARKER = ".wh..wh..opq"
OPAQUE_XATTR = "user.overlay.opaque"
# Kept beside the layer directory, never inside it: anything written into
# the layer becomes a file in every container's root filesystem.
UNPACK_MARKER_SUFFIX = ".done"


@dataclass
class Image:
    reference: str
    root: str
    manifest: dict
    config: dict

    @property
    def layer_digests(self) -> list[str]:
        return [layer["digest"] for layer in self.manifest["layers"]]

    @property
    def diff_ids(self) -> list[str]:
        return list(self.config.get("rootfs", {}).get("diff_ids", []))

    @property
    def process(self) -> dict:
        return self.config.get("config", {}) or {}

    @property
    def entrypoint(self) -> list[str]:
        return list(self.process.get("Entrypoint", []) or [])

    @property
    def cmd(self) -> list[str]:
        return list(self.process.get("Cmd", []) or [])

    @property
    def argv(self) -> list[str]:
        return self.entrypoint + self.cmd

    @property
    def env(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in self.process.get("Env", []) or []:
            key, _, value = item.partition("=")
            out[key] = value
        return out

    @property
    def user(self) -> str:
        return self.process.get("User", "") or ""

    @property
    def working_dir(self) -> str:
        return self.process.get("WorkingDir", "/") or "/"

    @property
    def healthcheck(self) -> dict | None:
        return self.process.get("Healthcheck")

    @property
    def stop_signal(self) -> str:
        return self.process.get("StopSignal", "") or ""

    @property
    def total_size(self) -> int:
        return sum(layer["size"] for layer in self.manifest["layers"])


class ImageStore:
    """An OCI image layout on disk, read-only."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        if not os.path.exists(os.path.join(self.root, "index.json")):
            raise FileNotFoundError(f"{self.root} is not an OCI image layout (no index.json)")

    def blob_path(self, digest: str) -> str:
        algorithm, encoded = digest.split(":", 1)
        return os.path.join(self.root, "blobs", algorithm, encoded)

    def read_blob(self, digest: str) -> bytes:
        with open(self.blob_path(digest), "rb") as handle:
            return handle.read()

    def tags(self) -> list[str]:
        with open(os.path.join(self.root, "index.json")) as handle:
            index = json.load(handle)
        return sorted(
            m.get("annotations", {}).get("org.opencontainers.image.ref.name", "")
            for m in index.get("manifests", [])
            if m.get("annotations", {}).get("org.opencontainers.image.ref.name")
        )

    def get(self, reference: str) -> Image:
        with open(os.path.join(self.root, "index.json")) as handle:
            index = json.load(handle)
        digest = None
        for descriptor in index.get("manifests", []):
            if descriptor.get("annotations", {}).get("org.opencontainers.image.ref.name") == reference:
                digest = descriptor["digest"]
                break
        if digest is None and reference.startswith("sha256:"):
            digest = reference
        if digest is None:
            raise KeyError(f"no such image: {reference} (have {self.tags()})")
        manifest = json.loads(self.read_blob(digest))
        config = json.loads(self.read_blob(manifest["config"]["digest"]))
        return Image(reference=reference, root=self.root, manifest=manifest, config=config)


# ---------------------------------------------------------------------------
# unpacking
# ---------------------------------------------------------------------------


@dataclass
class LayerCache:
    """One directory per layer diff, reused by every container and image.

    Keyed by digest rather than by image, so two images sharing a base share the
    unpacked bytes on disk as well as in the registry.
    """

    root: str
    unpacked: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)

    def path_for(self, digest: str) -> str:
        return os.path.join(self.root, digest.replace(":", "_"))

    def marker_for(self, digest: str) -> str:
        return self.path_for(digest) + UNPACK_MARKER_SUFFIX

    def lock_for(self, digest: str) -> str:
        """Beside the layer directory, never inside it — unpacking deletes the
        directory, and a lock file you can delete is not a lock."""
        return self.path_for(digest) + ".lock"

    def is_ready(self, digest: str) -> bool:
        return os.path.exists(self.marker_for(digest))

    def lowerdirs(self, image: Image) -> list[str]:
        """Bottom-first, matching the manifest. `OverlayRoot` reverses them."""
        return [self.path_for(digest) for digest in image.layer_digests]

    def ensure(self, store: ImageStore, image: Image) -> list[str]:
        os.makedirs(self.root, exist_ok=True)
        missing = [d for d in image.layer_digests if not self.is_ready(d)]
        self.reused = [d for d in image.layer_digests if d not in missing]
        if missing:
            # One namespace for the whole batch: entering a user namespace costs
            # a fork and two /proc writes, and doing it per layer is measurable.
            run_helper("unpack", store_root=store.root, cache_root=self.root,
                       digests=missing)
            self.unpacked = missing
        for digest in image.layer_digests:
            if not self.is_ready(digest):
                raise RuntimeError(f"layer {digest} failed to unpack")
        return self.lowerdirs(image)


def _unpack_layers(store: ImageStore, cache: LayerCache, digests: list[str]) -> None:
    for digest in digests:
        target = cache.path_for(digest)
        shutil.rmtree(target, ignore_errors=True)
        os.makedirs(target, exist_ok=True)
        unpack_layer(store.read_blob(digest), target)
        with open(cache.marker_for(digest), "w") as handle:
            handle.write(digest)


def unpack_layer(blob: bytes, dest: str) -> dict[str, int]:
    """Unpack one layer, translating OCI whiteouts into overlayfs whiteouts."""
    raw = gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob
    counts = {"files": 0, "whiteouts": 0, "opaque": 0}

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
        for info in tar.getmembers():
            name = os.path.basename(info.name)
            target = os.path.join(dest, info.name)
            if not _within(dest, target):
                raise ValueError(f"layer escapes its directory: {info.name!r}")

            if name == OPAQUE_MARKER:
                directory = os.path.dirname(target) or dest
                os.makedirs(directory, exist_ok=True)
                _set_opaque(directory)
                counts["opaque"] += 1
                continue

            if name.startswith(WHITEOUT_PREFIX):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                _make_whiteout(os.path.join(os.path.dirname(target), name[len(WHITEOUT_PREFIX):]))
                counts["whiteouts"] += 1
                continue

            if os.path.lexists(target) and not (info.isdir() and os.path.isdir(target)):
                _remove(target)
            tar.extract(info, dest, set_attrs=False, filter="tar")
            if info.isreg() or info.isdir():
                os.chmod(target, info.mode)
            counts["files"] += 1
    return counts


def _make_whiteout(path: str) -> None:
    """A character device 0:0 -- overlayfs reads it as "deleted", never opens it."""
    if os.path.lexists(path):
        _remove(path)
    try:
        os.mknod(path, 0o600 | stat.S_IFCHR, os.makedev(0, 0))
    except OSError as exc:
        raise linux.NotSupported(
            f"cannot create an overlayfs whiteout at {path} ({exc.strerror}). "
            "mknod needs CAP_MKNOD, which requires being inside a user namespace "
            "this process created -- see run_in_userns()."
        ) from exc


def _set_opaque(directory: str) -> None:
    # `user.` rather than `trusted.` because the overlay is mounted with
    # `userxattr`; the trusted namespace needs real CAP_SYS_ADMIN.
    try:
        os.setxattr(directory, OPAQUE_XATTR, b"y")
    except OSError:
        # Not fatal: an opaque marker that is dropped shows deleted files from
        # a lower layer, which is wrong but recoverable, whereas failing the
        # unpack makes the image unusable.
        pass


def _within(root: str, path: str) -> bool:
    root_abs, path_abs = os.path.abspath(root), os.path.abspath(path)
    return path_abs == root_abs or path_abs.startswith(root_abs + os.sep)


def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# the namespace helper
# ---------------------------------------------------------------------------


def _thread_count() -> int:
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 1


def run_helper(operation: str, **spec) -> None:
    """Run one namespace operation in a freshly exec'd process.

    The robust path, and the one everything internal uses. `run_in_userns`
    below forks and unshares in the child, which is faster and fails outright
    when the parent has a `pthread_atfork` handler that starts threads in the
    child -- gRPC's does. See `nshelper` for the measurement.
    """
    helper_root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    # The caller's import roots as well as ours: a pickled callable is useless
    # in the child if the module that defines it cannot be imported there.
    roots = [helper_root, *(p for p in sys.path if p and os.path.isdir(p))]
    env["PYTHONPATH"] = os.pathsep.join(
        dict.fromkeys([*roots, *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]))
    # gRPC's fork handlers are the reason this helper exists; they have no
    # business running in it either.
    env["GRPC_ENABLE_FORK_SUPPORT"] = "0"

    completed = subprocess.run(
        [sys.executable, "-m", "minicon.nshelper"],
        # uid/gid measured here, outside the namespace: inside it this process
        # is unmapped and reads as 65534, which the kernel refuses to map.
        input=json.dumps({"op": operation, "uid": os.getuid(),
                          "gid": os.getgid(), **spec}),
        capture_output=True, text=True, env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            (completed.stderr or "").strip()
            or f"user namespace helper failed ({completed.returncode})")


def run_in_userns(function, mapping: idmap.IdMapping | None = None) -> None:
    """Run `function` in a throwaway user namespace where we are root.

    Needed because unpacking has to `mknod` and `chown` files the calling user
    cannot otherwise create. The child's exit status is the only channel back,
    so `function` returning normally means success and raising means failure --
    deliberately narrow, since anything richer would need to survive a fork.

    Forks and unshares in the child, which is the cheap path and the right one
    almost always. When the parent has fork handlers that start threads in the
    child -- gRPC's do while a server is running -- a forked child cannot
    unshare at all, and this falls back to `run_helper`, pickling `function`
    into a freshly exec'd process.

    That fallback needs `function` to be picklable and its module importable,
    so a lambda will not survive it. The error says so, rather than surfacing
    an EINVAL from four frames down. Prefer `run_helper` directly for anything
    expressible as data.
    """
    hazardous, hazard = linux.fork_thread_hazard()
    if hazardous:
        import base64
        import pickle

        try:
            payload = base64.b64encode(pickle.dumps(function)).decode()
        except (AttributeError, TypeError, pickle.PicklingError) as exc:
            raise RuntimeError(
                f"cannot enter a user namespace by forking here -- {hazard} "
                f"-- and {function!r} cannot be sent to a fresh process either "
                f"({exc}). Pass a module-level function, or use run_helper() "
                "with an operation from minicon.nshelper."
            ) from exc
        run_helper("call", callable=payload)
        return
    mapping = mapping or idmap.root_mapping()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        status = 0
        try:
            os.close(read_fd)
            threads = _thread_count()
            if threads > 1:
                raise RuntimeError(
                    f"this forked child has {threads} threads, so it cannot "
                    "unshare a user namespace (EINVAL). Something in the parent "
                    "registered a pthread_atfork handler that starts threads -- "
                    "gRPC does this while a server is running. Use run_helper(), "
                    "which execs a clean process."
                )
            linux.unshare(linux.CLONE_NEWUSER | linux.CLONE_NEWNS)
            idmap.write_maps(os.getpid(), mapping)
            linux.make_root_private()
            function()
        except BaseException as exc:  # noqa: BLE001 -- must not escape the fork
            try:
                os.write(write_fd, f"{type(exc).__name__}: {exc}".encode()[:4000])
            except OSError:
                pass
            status = 1
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass
            os._exit(status)

    os.close(write_fd)
    message = b""
    while True:
        chunk = os.read(read_fd, 4096)
        if not chunk:
            break
        message += chunk
    os.close(read_fd)
    _, wait_status = os.waitpid(pid, 0)
    if os.WEXITSTATUS(wait_status) != 0:
        raise RuntimeError(message.decode() or f"user namespace helper failed ({wait_status})")
