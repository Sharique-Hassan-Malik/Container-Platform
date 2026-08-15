"""Cold start, broken into the four phases that actually consume the time.

    pull ──▶ extract ──▶ load ──▶ first token

Scaling a model service to zero, or rolling one node at a time, makes this
number the user-visible cost of a deploy. Aggregate "startup time" hides which
phase to attack: pulling is fixed by layer caching, extracting by compression
choice, loading by file format, and first token by the model itself. They have
nothing in common except appearing in the same stopwatch.

The pull phase is measured twice on purpose:

  * `pull_s` -- wall clock for the real blob copy on this machine's disk
  * `pull_modelled_s` -- bytes / bandwidth for a stated link speed

A local copy is not a network fetch, and reporting it as one would overstate how
fast a real cold start is. The modelled figure is arithmetic on measured bytes,
and is labelled as such wherever it is printed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field

from .store import ImageStore, copy_image

# Marker the payload prints so the parent can time phases inside the process.
# stdout is line-buffered through a pipe, so the payload must flush.
EVENT_PREFIX = "@@IMAGEKIT@@ "


@dataclass
class ColdStartResult:
    reference: str
    pull_s: float = 0.0
    extract_s: float = 0.0
    load_s: float = 0.0
    first_token_s: float = 0.0
    spawn_s: float = 0.0

    pull_bytes: int = 0
    pull_bytes_reused: int = 0
    fetch_bytes: int = 0
    rootfs_bytes: int = 0
    layers: int = 0

    bandwidth_mbps: float = 200.0
    events: dict[str, float] = field(default_factory=dict)
    token: str = ""

    @property
    def pull_modelled_s(self) -> float:
        """Transfer time for the bytes actually sent, at `bandwidth_mbps`."""
        return (self.pull_bytes * 8) / (self.bandwidth_mbps * 1e6)

    @property
    def fetch_modelled_s(self) -> float:
        """Same arithmetic for a weights-at-startup download.

        Charged at the same bandwidth as the image pull, because otherwise
        "keep the image small" wins by moving bytes onto a link the benchmark
        pretends is free. The measured `load_s` already contains the local copy;
        this is the extra a real object-store fetch would add on top.
        """
        return (self.fetch_bytes * 8) / (self.bandwidth_mbps * 1e6)

    @property
    def total_s(self) -> float:
        return self.pull_s + self.extract_s + self.spawn_s + self.load_s + self.first_token_s

    @property
    def total_modelled_s(self) -> float:
        return (
            self.pull_modelled_s + self.fetch_modelled_s + self.extract_s
            + self.spawn_s + self.load_s + self.first_token_s
        )

    def table(self) -> str:
        rows = [
            ("pull (local copy)", self.pull_s),
            (f"pull (modelled @ {self.bandwidth_mbps:.0f} Mbps)", self.pull_modelled_s),
            (f"weights fetch (modelled @ {self.bandwidth_mbps:.0f} Mbps)", self.fetch_modelled_s),
            ("extract", self.extract_s),
            ("spawn", self.spawn_s),
            ("load weights", self.load_s),
            ("first token", self.first_token_s),
        ]
        width = max(len(name) for name, _ in rows)
        lines = [f"{name:<{width}}  {value * 1000:8.1f} ms" for name, value in rows]
        lines.append("-" * (width + 14))
        lines.append(f"{'total (measured)':<{width}}  {self.total_s * 1000:8.1f} ms")
        lines.append(f"{'total (modelled pull)':<{width}}  {self.total_modelled_s * 1000:8.1f} ms")
        return "\n".join(lines)

    def to_json(self) -> dict:
        out = asdict(self)
        out["pull_modelled_s"] = self.pull_modelled_s
        out["total_s"] = self.total_s
        return out


def measure(
    source: ImageStore,
    reference: str,
    workdir: str,
    *,
    node_store: str | None = None,
    bandwidth_mbps: float = 200.0,
    warm_layers: list[str] | None = None,
    timeout: float = 300.0,
    extra_args: list[str] | None = None,
    drop_caches: bool = True,
    cold_files: list[str] | None = None,
) -> ColdStartResult:
    """Run one cold start end to end and time each phase.

    `warm_layers` seeds the destination store with blobs a node might already
    hold from a previous image -- the difference between a first deploy and the
    fifth rolling update of the same service.
    """
    os.makedirs(workdir, exist_ok=True)
    node_root = node_store or os.path.join(workdir, "node-store")
    if os.path.exists(node_root):
        shutil.rmtree(node_root)
    node = ImageStore(node_root)

    for digest in warm_layers or []:
        if source.has_blob(digest):
            node.put_blob(source.get_blob(digest))

    result = ColdStartResult(reference=reference, bandwidth_mbps=bandwidth_mbps)

    started = time.perf_counter()
    stats = copy_image(source, node, reference)
    result.pull_s = time.perf_counter() - started
    result.pull_bytes = stats.bytes_sent
    result.pull_bytes_reused = stats.bytes_skipped

    rootfs = os.path.join(workdir, "rootfs")
    started = time.perf_counter()
    node.unpack(reference, rootfs)
    result.extract_s = time.perf_counter() - started

    if drop_caches:
        # Everything just written is in page cache, so an "eager load" would be
        # a memcpy and an "mmap load" would take only minor faults. Both phases
        # would then measure RAM bandwidth and be reported as disk numbers.
        drop_page_cache(rootfs)
        for extra in cold_files or []:
            drop_page_cache(extra)

    manifest = node.get_manifest(reference)
    result.layers = len(manifest.layers)
    result.rootfs_bytes = _tree_size(rootfs)

    config = node.get_config(reference)
    argv = list(config.entrypoint) + list(config.cmd) + list(extra_args or [])
    if not argv:
        raise ValueError(f"{reference} has no entrypoint to start")

    _run_payload(argv, rootfs, config, result, timeout)
    return result


def _run_payload(argv: list[str], rootfs: str, config, result: ColdStartResult, timeout: float) -> None:
    """Start the image's entrypoint and read its phase markers.

    The process runs on the host with the unpacked rootfs as its working
    directory -- enough to time a real load of real bytes, but not isolated.
    `mini-container-runtime` (#19) runs this same rootfs under namespaces and
    reports the same four phases plus the runtime's own setup cost.
    """
    env = dict(os.environ)
    env.update(config.env)
    env["IMAGEKIT_ROOTFS"] = rootfs
    env["PYTHONUNBUFFERED"] = "1"

    argv = [_rebase(a.replace("$ROOTFS", rootfs), rootfs) for a in argv]
    spawn_started = time.perf_counter()
    process = subprocess.Popen(
        argv,
        cwd=os.path.join(rootfs, config.working_dir.lstrip("/")) or rootfs,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    deadline = time.perf_counter() + timeout
    marks: dict[str, float] = {}
    assert process.stdout is not None
    for line in process.stdout:
        if time.perf_counter() > deadline:
            process.kill()
            raise TimeoutError(f"payload exceeded {timeout}s")
        if not line.startswith(EVENT_PREFIX):
            continue
        event = json.loads(line[len(EVENT_PREFIX):])
        marks[event["event"]] = time.perf_counter()
        if event["event"] == "loaded":
            # Non-zero only in the weights-at-startup case; the modelled
            # transfer for these bytes is added to the total.
            result.fetch_bytes = int(event.get("fetched", 0))
        if event["event"] == "first_token":
            result.token = str(event.get("token", ""))
            break

    process.stdout.close()
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    stderr = process.stderr.read() if process.stderr else ""
    if "started" not in marks:
        raise RuntimeError(f"payload never reported 'started'.\nargv={argv}\nstderr:\n{stderr}")
    if "first_token" not in marks:
        raise RuntimeError(f"payload never reported 'first_token'.\nargv={argv}\nstderr:\n{stderr}")

    result.spawn_s = marks["started"] - spawn_started
    result.load_s = marks["loaded"] - marks["started"]
    result.first_token_s = marks["first_token"] - marks["loaded"]
    result.events = {k: v - spawn_started for k, v in marks.items()}


def drop_page_cache(path: str) -> int:
    """Evict a file or tree from the page cache, without root.

    `POSIX_FADV_DONTNEED` drops only *clean* pages, so each file is flushed
    first -- otherwise the pages just written by `unpack` stay resident and the
    next read is served from RAM. This is the unprivileged half of what
    `echo 3 > /proc/sys/vm/drop_caches` does, scoped to files this process owns.

    Returns the number of files evicted, which is worth checking: on tmpfs
    there are no backing pages to drop and the call silently does nothing.
    """
    targets = []
    if os.path.isdir(path):
        for dirpath, _, filenames in os.walk(path):
            targets.extend(os.path.join(dirpath, name) for name in filenames)
    elif os.path.exists(path):
        targets.append(path)

    dropped = 0
    for target in targets:
        if os.path.islink(target):
            continue
        try:
            fd = os.open(target, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            dropped += 1
        except OSError:
            pass
        finally:
            os.close(fd)
    return dropped


def _rebase(argument: str, rootfs: str) -> str:
    """Rewrite an in-image absolute path to where it lives on the host.

    An image's entrypoint says `/app/serve.py` because inside a container that
    *is* the path. Running the same command here, with no pivot_root, it is not.
    Rewriting arguments that resolve under the rootfs is the smallest shim that
    makes the timings real without pretending the process is contained.

    The interpreter itself is deliberately not rewritten: this runner uses the
    host's `python3`, so the base image's own interpreter is never exercised.
    `mini-container-runtime` (#19) removes both caveats by actually entering the
    root, and reports the same four phases for comparison.
    """
    if not argument.startswith("/"):
        return argument
    candidate = os.path.join(rootfs, argument.lstrip("/"))
    return candidate if os.path.exists(candidate) else argument


def _tree_size(root: str) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                total += os.path.getsize(path)
    return total
