"""The builder: Dockerfile -> layers -> manifest, with a cache that can miss.

Three things here are worth more than the rest of the file.

**The cache key is a chain, not a hash of one instruction.** Each step's key
folds in its parent's key, so changing step 2 invalidates steps 3..n even though
their text is identical. Without that, an image would silently inherit a layer
built against a base that no longer exists.

**COPY hashes its inputs; RUN does not.** A COPY's key includes the content of
every file it copies, so touching one byte of source invalidates it. A RUN's key
is only its command string -- which is precisely why `RUN apt-get update` serves
a six-month-old package index. This implementation reproduces that behaviour
rather than fixing it, because the fix is to write a better Dockerfile, and the
lint pass says so.

**Layers come from filesystem diffs.** A COPY knows what it wrote, so its layer
is built directly. A RUN does not, so the staging root is snapshotted before and
after and the difference becomes the layer -- including deletions, which is
where whiteouts come from.
"""

from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Protocol

from . import dockerfile as df
from .config import EPOCH, HealthCheck, HistoryEntry, ImageConfig
from .digest import Descriptor, sha256_hex
from .layer import Layer, make_layer
from .manifest import Manifest
from .store import ImageStore


# ---------------------------------------------------------------------------
# RUN execution
# ---------------------------------------------------------------------------


class RunExecutor(Protocol):
    """How a RUN instruction gets executed against a staging root."""

    def run(self, command: str, rootfs: str, env: dict[str, str], workdir: str, user: str) -> None: ...


class HostExecutor:
    """Run build commands on the host, with the staging root as cwd.

    This is **not a sandbox**. The command runs as the building user with the
    host's real filesystem visible; only the working directory is redirected.
    It is enough for the COPY/ENV-shaped builds this toolkit targets, and it is
    honest about what it is.

    The from-scratch counterpart is `mini-container-runtime` (#19): it provides
    the namespaces and overlayfs that make a build step genuinely isolated, and
    plugs in here as a drop-in `RunExecutor`.
    """

    def __init__(self, shell: tuple[str, ...] = ("/bin/sh", "-c"), timeout: float = 300.0):
        self.shell = shell
        self.timeout = timeout

    def run(self, command: str, rootfs: str, env: dict[str, str], workdir: str, user: str) -> None:
        cwd = os.path.join(rootfs, workdir.lstrip("/")) or rootfs
        os.makedirs(cwd, exist_ok=True)
        merged = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root"}
        merged.update(env)
        merged["IMAGEKIT_ROOTFS"] = rootfs
        result = subprocess.run(
            [*self.shell, command],
            cwd=cwd,
            env=merged,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if result.returncode != 0:
            raise BuildError(
                f"RUN failed (exit {result.returncode}): {command}\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )


class BuildError(Exception):
    pass


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    instruction: str
    cached: bool
    layer_digest: str | None
    layer_size: int
    duration_s: float

    @property
    def produces_layer(self) -> bool:
        """ENV/USER/WORKDIR change only the config blob, never the filesystem.

        Counting them as cache misses would make every build look mostly-cold,
        when what actually matters is whether the expensive layers were reused.
        """
        return self.layer_digest is not None


@dataclass
class BuildResult:
    reference: str
    manifest: Manifest
    config: ImageConfig
    steps: list[StepResult] = field(default_factory=list)
    stages_built: int = 1
    findings: list[str] = field(default_factory=list)

    @property
    def image_size(self) -> int:
        return self.manifest.total_layer_bytes + self.manifest.config.size

    @property
    def layer_steps(self) -> list[StepResult]:
        return [s for s in self.steps if s.produces_layer]

    @property
    def cache_hits(self) -> int:
        return sum(1 for s in self.layer_steps if s.cached)

    @property
    def bytes_rebuilt(self) -> int:
        """Layer bytes this build had to produce -- and therefore re-upload."""
        return sum(s.layer_size for s in self.layer_steps if not s.cached)

    @property
    def layer_sizes(self) -> list[int]:
        return [layer.size for layer in self.manifest.layers]

    def summary(self) -> str:
        lines = [
            f"{self.reference}  {len(self.manifest.layers)} layers  {human(self.image_size)}",
            f"  cache: {self.cache_hits}/{len(self.layer_steps)} layer steps hit, "
            f"{human(self.bytes_rebuilt)} rebuilt",
        ]
        for finding in self.findings:
            lines.append(f"  lint: {finding}")
        return "\n".join(lines)


class BuildCache:
    """Maps a step's chain key to the layer it produced.

    Persisted as one JSON file. A real builder keys on the same thing; the only
    difference is that it stores gigabytes of snapshots and this stores digests
    into the shared blob store, which already holds the bytes.
    """

    def __init__(self, path: str):
        self.path = path
        self.entries: dict[str, dict] = {}
        self.file_hashes: dict[str, list] = {}
        if os.path.exists(path):
            try:
                with open(path) as handle:
                    blob = json.load(handle)
                self.entries = blob.get("entries", {})
                self.file_hashes = blob.get("file_hashes", {})
            except (json.JSONDecodeError, OSError):
                # A corrupt cache is a performance problem, never a correctness
                # one: every entry is re-derivable from content.
                self.entries, self.file_hashes = {}, {}

    def get(self, key: str) -> dict | None:
        return self.entries.get(key)

    def put(self, key: str, value: dict) -> None:
        self.entries[key] = value

    def content_hash(self, path: str) -> str:
        """sha256 of a file, memoised on (size, mtime_ns).

        Re-hashing a 500 MB checkpoint on every build would dominate build time
        and teach the wrong lesson about why layer ordering matters.
        """
        stat = os.stat(path)
        cached = self.file_hashes.get(path)
        if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            return cached[2]
        hasher = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        self.file_hashes[path] = [stat.st_size, stat.st_mtime_ns, digest]
        return digest

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as handle:
            json.dump({"entries": self.entries, "file_hashes": self.file_hashes}, handle)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# Filesystem snapshots
# ---------------------------------------------------------------------------


def snapshot(root: str) -> dict[str, tuple]:
    """Cheap fingerprint of a tree: path -> (kind, mode, size, mtime_ns).

    Content is deliberately not hashed. This runs before and after every RUN,
    and the tree may contain a multi-gigabyte checkpoint that no build step
    touches. (size, mtime_ns) misses a same-size same-timestamp rewrite -- the
    same blind spot every incremental build tool has, and the same reason
    `make` sometimes needs a `clean`.
    """
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            try:
                stat = os.lstat(path)
            except FileNotFoundError:
                continue
            if os.path.islink(path):
                out[rel] = ("l", 0, 0, 0, os.readlink(path))
            elif os.path.isdir(path):
                out[rel] = ("d", stat.st_mode & 0o7777, 0, 0)
            else:
                out[rel] = ("f", stat.st_mode & 0o7777, stat.st_size, stat.st_mtime_ns)
    return out


def diff_snapshots(before: dict, after: dict) -> tuple[list[str], list[str]]:
    changed = sorted(path for path, meta in after.items() if before.get(path) != meta)
    removed = sorted(path for path in before if path not in after)
    return changed, removed


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class Builder:
    def __init__(
        self,
        store: ImageStore,
        context: str,
        *,
        cache: BuildCache | None = None,
        executor: RunExecutor | None = None,
        workdir_root: str | None = None,
        compression_level: int = 6,
        deterministic: bool = True,
    ):
        self.store = store
        self.context = os.path.abspath(context)
        self.cache = cache if cache is not None else BuildCache(os.path.join(store.root, "build-cache.json"))
        self.executor = executor or HostExecutor()
        self.workdir_root = workdir_root or os.path.join(store.root, "_staging")
        self.compression_level = compression_level
        self.deterministic = deterministic
        self.ignore = _read_ignore(self.context)

    # -- public --------------------------------------------------------------

    def build(self, dockerfile_text: str, reference: str) -> BuildResult:
        stages = df.parse(dockerfile_text)
        os.makedirs(self.workdir_root, exist_ok=True)
        stage_roots: dict[str, str] = {}
        steps: list[StepResult] = []
        final: tuple[ImageConfig, list[Layer | Descriptor]] | None = None

        for stage in stages:
            rootfs = os.path.join(self.workdir_root, f"{reference.replace(':', '_')}-{stage.label}")
            if os.path.exists(rootfs):
                shutil.rmtree(rootfs)
            os.makedirs(rootfs)

            config, layers, key = self._init_stage(stage, rootfs)
            for instruction in stage.instructions:
                started = time.perf_counter()
                key, layer_descriptor = self._apply(instruction, stage, rootfs, config, layers, key, stage_roots)
                steps.append(
                    StepResult(
                        instruction=instruction.text()[:100],
                        cached=layer_descriptor is not None and layer_descriptor.annotations.get("cached") == "1",
                        layer_digest=layer_descriptor.digest if layer_descriptor else None,
                        layer_size=layer_descriptor.size if layer_descriptor else 0,
                        duration_s=time.perf_counter() - started,
                    )
                )
            stage_roots[stage.label] = rootfs
            if stage.name:
                stage_roots[stage.name] = rootfs
            stage_roots[str(stage.index)] = rootfs
            final = (config, layers)

        assert final is not None
        config, layers = final
        manifest = self._finalise(config, layers, reference)
        self.cache.save()
        result = BuildResult(
            reference=reference,
            manifest=manifest,
            config=config,
            steps=steps,
            stages_built=len(stages),
        )
        result.findings = lint(result)
        return result

    # -- stage setup ---------------------------------------------------------

    def _init_stage(self, stage: df.Stage, rootfs: str) -> tuple[ImageConfig, list, str]:
        if stage.base in ("scratch", "SCRATCH"):
            return ImageConfig(), [], "scratch"
        manifest = self.store.get_manifest(stage.base)
        config = self.store.get_config(stage.base)
        self.store.unpack(stage.base, rootfs)
        _, descriptor = manifest.serialise()
        # The base's own layers carry forward, so `layer_sizes` stays aligned
        # with `diff_ids` for images built on top of built images. The parent
        # cache key is the base manifest digest: rebuilding on a new base must
        # miss every step, even textually identical ones.
        return config.copy(), list(manifest.layers), descriptor.digest

    # -- one instruction -----------------------------------------------------

    def _apply(
        self,
        instruction: df.Instruction,
        stage: df.Stage,
        rootfs: str,
        config: ImageConfig,
        layers: list,
        parent_key: str,
        stage_roots: dict[str, str],
    ) -> tuple[str, Descriptor | None]:
        op = instruction.op

        if op in ("ENV", "ARG", "WORKDIR", "USER", "ENTRYPOINT", "CMD", "EXPOSE", "LABEL", "HEALTHCHECK", "STOPSIGNAL"):
            self._apply_metadata(instruction, config)
            config.history.append(HistoryEntry(created_by=instruction.text(), empty_layer=True))
            return _chain(parent_key, instruction.text(), ""), None

        if op == "COPY":
            sources, input_hash = self._resolve_copy(instruction, stage_roots)
            key = _chain(parent_key, instruction.text(), input_hash)
            cached = self.cache.get(key)
            if cached and self.store.has_blob(cached["digest"]):
                from .layer import extract_layer

                extract_layer(self.store.get_blob(cached["digest"]), rootfs)
                descriptor = self._record_cached(cached, config, layers, instruction)
                return key, descriptor
            written = self._do_copy(instruction, sources, rootfs, config)
            layer = self._layer_from_paths(rootfs, written)
            return key, self._record_layer(key, layer, config, layers, instruction)

        if op == "RUN":
            key = _chain(parent_key, instruction.text(), "")
            cached = self.cache.get(key)
            if cached and self.store.has_blob(cached["digest"]):
                from .layer import extract_layer

                extract_layer(self.store.get_blob(cached["digest"]), rootfs)
                descriptor = self._record_cached(cached, config, layers, instruction)
                return key, descriptor
            before = snapshot(rootfs)
            self.executor.run(instruction.args, rootfs, config.env, config.working_dir, config.user)
            changed, removed = diff_snapshots(before, snapshot(rootfs))
            layer = self._layer_from_paths(rootfs, changed, whiteouts=removed)
            return key, self._record_layer(key, layer, config, layers, instruction)

        raise BuildError(f"unhandled instruction {op}")

    def _apply_metadata(self, instruction: df.Instruction, config: ImageConfig) -> None:
        op, args = instruction.op, instruction.args
        if op in ("ENV", "ARG"):
            config.env.update(df.parse_key_values(args))
        elif op == "WORKDIR":
            config.working_dir = args if args.startswith("/") else os.path.join(config.working_dir, args)
        elif op == "USER":
            config.user = args.strip()
        elif op == "ENTRYPOINT":
            config.entrypoint, _ = df.parse_exec_form(args)
        elif op == "CMD":
            config.cmd, _ = df.parse_exec_form(args)
        elif op == "EXPOSE":
            for port in args.split():
                config.exposed_ports.append(port if "/" in port else f"{port}/tcp")
        elif op == "LABEL":
            config.labels.update(df.parse_key_values(args))
        elif op == "STOPSIGNAL":
            config.stop_signal = args.strip()
        elif op == "HEALTHCHECK":
            if args.strip().upper() == "NONE":
                config.healthcheck = None
                return
            keyword, _, command = args.partition(" ")
            if keyword.upper() != "CMD":
                raise BuildError(f"HEALTHCHECK expects CMD, got {keyword!r}")
            test, was_json = df.parse_exec_form(command)
            config.healthcheck = HealthCheck(
                test=(["CMD"] + test) if was_json else ["CMD-SHELL", command.strip()],
                interval_s=df.parse_duration(instruction.flags.get("interval", "30s")),
                timeout_s=df.parse_duration(instruction.flags.get("timeout", "5s")),
                start_period_s=df.parse_duration(instruction.flags.get("start-period", "0s")),
                retries=int(instruction.flags.get("retries", 3)),
            )

    # -- COPY ----------------------------------------------------------------

    def _resolve_copy(self, instruction: df.Instruction, stage_roots: dict[str, str]) -> tuple[list[tuple[str, str]], str]:
        """Expand sources and hash them. The hash is what makes COPY cache correctly."""
        parts = instruction.args.split()
        if len(parts) < 2:
            raise BuildError(f"COPY needs a source and a destination: {instruction.args!r}")
        *patterns, dest = parts
        from_stage = instruction.flags.get("from")
        if from_stage is not None and from_stage not in stage_roots:
            raise BuildError(
                f"COPY --from={from_stage}: no such stage (have {sorted(set(stage_roots))})"
            )
        base = stage_roots[from_stage] if from_stage is not None else self.context

        resolved: list[tuple[str, str]] = []
        for pattern in patterns:
            # `COPY --from=builder /build/out .` names an absolute path *inside
            # that stage*. Joining it raw would silently reach into the build
            # host's real filesystem, because os.path.join drops the left side
            # when the right side is absolute.
            matches = sorted(glob.glob(os.path.join(base, pattern.lstrip("/"))))
            if not matches:
                raise BuildError(f"COPY source not found: {pattern}")
            for match in matches:
                rel = os.path.relpath(match, base)
                if from_stage is None and self._ignored(rel):
                    continue
                resolved.append((rel, match))

        hasher = hashlib.sha256()
        for rel, path in resolved:
            hasher.update(rel.encode())
            if os.path.isdir(path):
                for dirpath, dirnames, filenames in os.walk(path):
                    dirnames.sort()
                    for name in sorted(filenames):
                        child = os.path.join(dirpath, name)
                        child_rel = os.path.relpath(child, base)
                        if from_stage is None and self._ignored(child_rel):
                            continue
                        hasher.update(child_rel.encode())
                        hasher.update(self.cache.content_hash(child).encode())
            elif os.path.islink(path):
                hasher.update(os.readlink(path).encode())
            else:
                hasher.update(self.cache.content_hash(path).encode())
        return resolved, hasher.hexdigest()

    def _do_copy(self, instruction: df.Instruction, sources: list[tuple[str, str]], rootfs: str, config: ImageConfig) -> list[str]:
        """Materialise a COPY into the staging root, returning the paths it wrote.

        The destination rule is the one part of COPY that surprises people:
        a **directory** source contributes its *contents* to the destination,
        not itself (`COPY app /app` gives `/app/serve.py`, never `/app/app/serve.py`),
        while a **file** source lands at the destination path unless the
        destination is obviously a directory.
        """
        parts = instruction.args.split()
        dest = parts[-1]
        dest_rel = (dest if dest.startswith("/") else os.path.join(config.working_dir, dest)).lstrip("/")
        dest_is_dir = (
            dest.endswith("/")
            or len(sources) > 1
            or any(os.path.isdir(path) for _, path in sources)
            or os.path.isdir(os.path.join(rootfs, dest_rel))
        )
        from_stage = instruction.flags.get("from")

        written: list[str] = []
        for parent in _parents(dest_rel if dest_is_dir else os.path.dirname(dest_rel)):
            target = os.path.join(rootfs, parent)
            if not os.path.exists(target):
                os.makedirs(target, exist_ok=True)
                written.append(parent)

        for rel, path in sources:
            if os.path.isdir(path):
                for dirpath, dirnames, filenames in os.walk(path):
                    dirnames.sort()
                    for name in sorted(dirnames) + sorted(filenames):
                        child = os.path.join(dirpath, name)
                        if from_stage is None and self._ignored(os.path.relpath(child, self.context)):
                            continue
                        child_rel = os.path.join(dest_rel, os.path.relpath(child, path)).replace(os.sep, "/")
                        child_target = os.path.join(rootfs, child_rel)
                        if os.path.isdir(child) and not os.path.islink(child):
                            os.makedirs(child_target, exist_ok=True)
                        else:
                            _copy_file(child, child_target)
                        written.append(child_rel)
                if dest_rel:
                    written.append(dest_rel)
            else:
                archive_rel = (
                    os.path.join(dest_rel, os.path.basename(rel)) if dest_is_dir else dest_rel
                ).replace(os.sep, "/")
                _copy_file(path, os.path.join(rootfs, archive_rel))
                written.append(archive_rel)

        chown = instruction.flags.get("chown")
        if chown:
            # Recorded in the layer's tar headers rather than applied to the
            # host tree, which the builder has no privilege to change.
            config.labels.setdefault("imagekit.chown." + dest_rel.replace("/", "_"), chown)
        return sorted(set(written))

    def _ignored(self, rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, pattern) or rel.startswith(pattern.rstrip("/") + "/") for pattern in self.ignore)

    # -- layers --------------------------------------------------------------

    def _layer_from_paths(self, rootfs: str, paths: list[str], whiteouts: list[str] | None = None) -> Layer:
        members = [(p, os.path.join(rootfs, p)) for p in sorted(set(paths)) if os.path.lexists(os.path.join(rootfs, p))]
        return make_layer(
            members,
            whiteouts=whiteouts,
            level=self.compression_level,
            deterministic=self.deterministic,
        )

    def _record_layer(self, key: str, layer: Layer, config: ImageConfig, layers: list, instruction: df.Instruction) -> Descriptor:
        self.store.put_blob(layer.blob)
        self.cache.put(key, {"digest": layer.digest, "diff_id": layer.diff_id, "size": layer.size})
        descriptor = Descriptor(layer.media_type, layer.digest, layer.size)
        layers.append(descriptor)
        config.diff_ids.append(layer.diff_id)
        config.history.append(HistoryEntry(created_by=instruction.text()))
        return descriptor

    def _record_cached(self, cached: dict, config: ImageConfig, layers: list, instruction: df.Instruction) -> Descriptor:
        from .layer import MEDIA_LAYER_GZIP

        descriptor = Descriptor(MEDIA_LAYER_GZIP, cached["digest"], cached["size"], {"cached": "1"})
        layers.append(Descriptor(MEDIA_LAYER_GZIP, cached["digest"], cached["size"]))
        config.diff_ids.append(cached["diff_id"])
        config.history.append(HistoryEntry(created_by=instruction.text()))
        return descriptor

    def _finalise(self, config: ImageConfig, layers: list, reference: str) -> Manifest:
        config.created = EPOCH
        config_blob, config_descriptor = config.serialise()
        self.store.put_blob(config_blob)
        manifest = Manifest(config=config_descriptor, layers=[Descriptor(l.media_type, l.digest, l.size) for l in layers])
        self.store.put_image(reference, manifest)
        return manifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chain(parent_key: str, instruction_text: str, input_hash: str) -> str:
    return sha256_hex(f"{parent_key}\n{instruction_text}\n{input_hash}".encode())


def _parents(path: str) -> list[str]:
    parts = [p for p in path.split("/") if p]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def _copy_file(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.islink(src):
        if os.path.lexists(dst):
            os.unlink(dst)
        os.symlink(os.readlink(src), dst)
    else:
        shutil.copy2(src, dst)


def _read_ignore(context: str) -> list[str]:
    path = os.path.join(context, ".dockerignore")
    if not os.path.exists(path):
        return []
    with open(path) as handle:
        return [line.strip() for line in handle if line.strip() and not line.startswith("#")]


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------

# A layer bigger than this is treated as "weights-shaped": expensive to move,
# and therefore the thing layer ordering exists to protect.
LARGE_LAYER_BYTES = 32 * 1024 * 1024


def lint(result: BuildResult) -> list[str]:
    """Packaging checks that only matter once an image holds a model.

    Every finding here is something that costs real minutes on a real rollout,
    not a style preference.
    """
    findings: list[str] = []
    config = result.config
    sizes = result.layer_sizes

    if config.runs_as_root():
        findings.append("image runs as root: no USER instruction (a compromised handler owns the container)")
    if config.healthcheck is None:
        findings.append("no HEALTHCHECK: an orchestrator cannot tell 'process alive' from 'model loaded'")
    if config.entrypoint[:2] == ["/bin/sh", "-c"]:
        findings.append(
            "shell-form ENTRYPOINT: PID 1 is /bin/sh, which does not forward SIGTERM -- "
            "every shutdown becomes a 10s SIGKILL timeout"
        )
    if not config.entrypoint and not config.cmd:
        findings.append("no ENTRYPOINT or CMD: image is not runnable")

    # Cache keys chain forward: a step's key folds in every earlier step. So a
    # large layer is safe only if nothing volatile precedes it.
    for i, size in enumerate(sizes):
        if size < LARGE_LAYER_BYTES:
            continue
        preceding_small = [j for j in range(i) if sizes[j] < LARGE_LAYER_BYTES]
        if preceding_small:
            findings.append(
                f"layer {i} ({human(size)}) sits behind smaller layer(s) {preceding_small}: "
                f"editing any of those invalidates this one too, re-pushing {human(size)} "
                "for a source-code change. Copy weights before code."
            )
    return findings
