"""Shared fixture for the benchmarks: a base image, a checkpoint, an app.

Everything is built once into `--workdir` and reused, because constructing a
base root filesystem from the host takes tens of seconds and none of the
measurements are about that.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from imagekit import BuildCache, Builder, ImageStore, human, layer_from_directory  # noqa: E402

EXAMPLES = os.path.join(ROOT, "examples")
DEFAULT_WORKDIR = os.path.join(ROOT, ".bench")


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workdir", default=DEFAULT_WORKDIR)
    parser.add_argument("--preset", default="medium", help="checkpoint size: tiny|small|medium|gpt2-124m")
    parser.add_argument("--base", default="slim", help="base image: full|slim|distroless")
    parser.add_argument("--compression", type=int, default=6)
    parser.add_argument("--fresh", action="store_true", help="discard cached fixtures first")


class Fixture:
    def __init__(self, args):
        self.workdir = os.path.abspath(args.workdir)
        self.preset = args.preset
        self.base_kind = args.base
        self.compression = args.compression
        if getattr(args, "fresh", False) and os.path.exists(self.workdir):
            shutil.rmtree(self.workdir)
        os.makedirs(self.workdir, exist_ok=True)
        self.store = ImageStore(os.path.join(self.workdir, "images"))

    # -- fixtures ------------------------------------------------------------

    @property
    def model_path(self) -> str:
        return os.path.join(self.workdir, f"model-{self.preset}.bin")

    def model(self) -> str:
        if not os.path.exists(self.model_path):
            run([sys.executable, os.path.join(EXAMPLES, "make_model.py"), self.model_path, "--preset", self.preset])
        return self.model_path

    def base_rootfs(self, kind: str | None = None) -> str:
        kind = kind or self.base_kind
        dest = os.path.join(self.workdir, f"base-{kind}")
        if not os.path.isdir(dest):
            cmd = [sys.executable, os.path.join(EXAMPLES, "mkbase.py"), kind, dest]
            if kind == "distroless":
                cmd += ["--payload", os.path.join(EXAMPLES, "serve.py"),
                        "--payload-args", f"--weights {self.model()}"]
            run(cmd)
        return dest

    def base_image(self, kind: str | None = None) -> str:
        """Import a base rootfs as a single-layer image, the way `docker import` does."""
        kind = kind or self.base_kind
        reference = f"python-{kind}:base"
        if reference in self.store.tags():
            return reference
        rootfs = self.base_rootfs(kind)
        layer = layer_from_directory(rootfs, level=self.compression)
        self.store.put_blob(layer.blob)

        from imagekit import Descriptor, ImageConfig, Manifest

        config = ImageConfig(
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONUNBUFFERED": "1"},
            diff_ids=[layer.diff_id],
            working_dir="/app",
        )
        blob, config_descriptor = config.serialise()
        self.store.put_blob(blob)
        self.store.put_image(
            reference,
            Manifest(config=config_descriptor, layers=[Descriptor(layer.media_type, layer.digest, layer.size)]),
        )
        return reference

    def context(self, name: str = "app", *, with_model: bool = True, code_version: int = 1) -> str:
        """A build context: the payload script, some app code, and maybe the weights."""
        path = os.path.join(self.workdir, f"ctx-{name}")
        os.makedirs(os.path.join(path, "app"), exist_ok=True)
        shutil.copy2(os.path.join(EXAMPLES, "serve.py"), os.path.join(path, "app", "serve.py"))
        self.write_code(path, code_version)
        target = os.path.join(path, "model.bin")
        if with_model:
            if not os.path.exists(target) or os.path.getsize(target) != os.path.getsize(self.model()):
                shutil.copy2(self.model(), target)
        elif os.path.exists(target):
            os.unlink(target)
        return path

    def write_code(self, context: str, version: int) -> None:
        """Change one line of application code -- the everyday reason to rebuild."""
        with open(os.path.join(context, "app", "handler.py"), "w") as handle:
            handle.write(f'"""Request handler."""\nVERSION = {version}\n')

    def builder(self, context: str, *, cache: str = "cache") -> Builder:
        return Builder(
            self.store,
            context,
            cache=BuildCache(os.path.join(self.workdir, f"{cache}.json")),
            compression_level=self.compression,
        )


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    lines = ["  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join(lines)


__all__ = ["Fixture", "add_common_args", "human", "run", "table", "ROOT", "EXAMPLES"]
