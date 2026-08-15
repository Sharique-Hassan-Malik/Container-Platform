"""Capability gates for the container tests, importable by name.

These lived in `conftest.py` and were imported as `from conftest import ...`,
which resolves to whichever conftest is nearest — the repository root's, once
every module is collected in one run. `tests` is itself ambiguous — the repository root has one too — so the
helpers live at the module root under a name nothing else uses.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile
import threading
from pathlib import Path

import pytest

from minicon.linux import overlayfs_in_userns, userns_available

BUSYBOX_CANDIDATES = ("/bin/busybox", "/usr/bin/busybox", "/sbin/busybox")
APPLETS = ("sh", "cat", "ls", "echo", "sleep", "hostname", "ip", "mount", "id", "ps", "true", "false", "wc", "head")


def find_busybox() -> str | None:
    for path in BUSYBOX_CANDIDATES:
        if os.path.exists(path):
            return path
    return shutil.which("busybox")


def _requires(reason_check) -> pytest.MarkDecorator:
    ok, reason = reason_check()
    return pytest.mark.skipif(not ok, reason=reason)


def os_thread_count() -> int:
    """Threads as the *kernel* sees them.

    `threading.active_count()` only counts threads Python created. gRPC, and
    any other C extension with its own pool, are invisible to it — and they are
    exactly the ones that break `unshare`. /proc is the only source that counts
    them.
    """
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("Threads:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return threading.active_count()


def single_threaded() -> tuple[bool, str]:
    """Whether this process can still create a user namespace.

    `unshare(CLONE_NEWUSER)` fails with EINVAL in a multi-threaded process, and
    the kernel gives no way to undo that. Running these tests alone is fine;
    running them after a module whose tests start a gRPC server or a broker
    thread is not, and the failure is an unhelpful `Invalid argument` deep in
    the helper. Checked as a capability so the skip explains itself.
    """
    count = os_thread_count()
    if count > 1:
        return False, (
            f"user namespaces need a single-threaded process; the kernel reports "
            f"{count} threads in this one (a C extension such as gRPC keeps its "
            f"own pool). Run this module's tests on their own."
        )
    return True, ""


@pytest.fixture
def userns_capable() -> None:
    """Skip at *run* time if a user namespace cannot be created right now.

    Kernel support is a static fact and could be a `skipif`. Thread count is
    not: it is single-threaded at collection and multi-threaded by the time a
    later module's gRPC server or broker thread is running, so the check has to
    happen when the test executes.
    """
    ok, reason = userns_available()
    if not ok:
        pytest.skip(reason)
    ok, reason = single_threaded()
    if not ok:
        pytest.skip(reason)


# Applied by the tests as a decorator; a fixture request rather than a skipif
# so both halves of the capability are evaluated when the test runs.
requires_userns = pytest.mark.usefixtures("userns_capable")
requires_single_threaded = _requires(single_threaded)
requires_overlayfs = pytest.mark.skipif(
    not overlayfs_in_userns(), reason="unprivileged overlayfs needs Linux 5.11+"
)
requires_busybox = pytest.mark.skipif(find_busybox() is None, reason="busybox not installed")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def make_layer(members: list[tuple[str, bytes | None, int, str | None]]) -> tuple[bytes, str, str]:
    """Build one gzipped layer tar.

    Each member is (path, content, mode, symlink_target). `content is None` and
    no target means a directory. Returns (blob, digest, diff_id).
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for path, content, mode, link_target in members:
            info = tarfile.TarInfo(path)
            info.mode = mode
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            if link_target is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link_target
                tar.addfile(info)
            elif content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    raw = buffer.getvalue()
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0) as gz:
        gz.write(raw)
    blob = compressed.getvalue()
    return blob, _digest(blob), _digest(raw)


def write_image(root: str, reference: str, layers: list[bytes], diff_ids: list[str], process: dict) -> str:
    """Assemble an OCI image layout by hand. The format is the whole dependency."""
    blobs = os.path.join(root, "blobs", "sha256")
    os.makedirs(blobs, exist_ok=True)

    def put(data: bytes) -> tuple[str, int]:
        digest = _digest(data)
        with open(os.path.join(blobs, digest.split(":")[1]), "wb") as handle:
            handle.write(data)
        return digest, len(data)

    descriptors = []
    for blob in layers:
        digest, size = put(blob)
        descriptors.append({
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": digest,
            "size": size,
        })

    config = _canonical({
        "created": "1970-01-01T00:00:00Z",
        "architecture": "amd64",
        "os": "linux",
        "config": process,
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
        "history": [{"created": "1970-01-01T00:00:00Z", "created_by": "test"}],
    })
    config_digest, config_size = put(config)

    manifest = _canonical({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": config_size,
        },
        "layers": descriptors,
    })
    manifest_digest, manifest_size = put(manifest)

    index_path = os.path.join(root, "index.json")
    index = {"schemaVersion": 2, "manifests": []}
    if os.path.exists(index_path):
        with open(index_path) as handle:
            index = json.load(handle)
    index["manifests"] = [
        m for m in index["manifests"]
        if m.get("annotations", {}).get("org.opencontainers.image.ref.name") != reference
    ]
    index["manifests"].append({
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": manifest_digest,
        "size": manifest_size,
        "annotations": {"org.opencontainers.image.ref.name": reference},
    })
    with open(index_path, "w") as handle:
        json.dump(index, handle)
    with open(os.path.join(root, "oci-layout"), "w") as handle:
        json.dump({"imageLayoutVersion": "1.0.0"}, handle)
    return manifest_digest


def busybox_layer() -> tuple[bytes, str, str]:
    path = find_busybox()
    assert path is not None
    with open(path, "rb") as handle:
        binary = handle.read()
    members: list[tuple[str, bytes | None, int, str | None]] = [
        ("bin", None, 0o755, None),
        ("bin/busybox", binary, 0o755, None),
        ("proc", None, 0o755, None),
        ("sys", None, 0o755, None),
        ("dev", None, 0o755, None),
        ("tmp", None, 0o777, None),
        ("etc", None, 0o755, None),
        ("app", None, 0o755, None),
    ]
    for applet in APPLETS:
        members.append((f"bin/{applet}", None, 0o777, "/bin/busybox"))
    return make_layer(members)


@pytest.fixture(scope="session")
def image_store(tmp_path_factory):
    """A store holding `busybox:v1` and a two-layer `app:v1` on top of it."""
    if find_busybox() is None:
        pytest.skip("busybox not installed")
    root = str(tmp_path_factory.mktemp("oci"))
    base_blob, _, base_diff = busybox_layer()
    write_image(
        root, "busybox:v1", [base_blob], [base_diff],
        {"Env": ["PATH=/bin"], "Entrypoint": ["/bin/sh", "-c", "echo hello"], "WorkingDir": "/"},
    )

    app_blob, _, app_diff = make_layer([
        ("app", None, 0o755, None),
        ("app/run.sh", b"#!/bin/sh\necho container-ok\n", 0o755, None),
    ])
    write_image(
        root, "app:v1", [base_blob, app_blob], [base_diff, app_diff],
        {"Env": ["PATH=/bin"], "Entrypoint": ["/bin/sh", "/app/run.sh"], "WorkingDir": "/app"},
    )
    return root


@pytest.fixture
def workspace(tmp_path):
    return str(tmp_path / "run")
