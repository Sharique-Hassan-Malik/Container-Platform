#!/usr/bin/env python3
"""What a container start costs, and what the tenth one costs.

    python3 bench/startup.py --count 20

Three questions:

  1. Where does the runtime spend startup time, phase by phase?
  2. What does the *second* container from an image cost, once its layers are
     already unpacked? This is the number that decides whether scale-to-zero is
     viable.
  3. How much disk does each additional container add? Overlay upper directories
     are supposed to make that nearly nothing; the claim is checked rather than
     repeated.

The image is built here from the host's busybox -- one statically linked binary,
so the fixture is honest and needs nothing installed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import statistics
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from conftest import busybox_layer, find_busybox, make_layer, write_image  # noqa: E402

from minicon import Container, ContainerConfig, ImageStore, Limits  # noqa: E402
from minicon.linux import overlayfs_in_userns, userns_available  # noqa: E402


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def tree_size(path: str) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                if not os.path.islink(full):
                    total += os.path.getsize(full)
            except OSError:
                continue
    return total


def table(rows, headers) -> str:
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    lines = ["  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=20, help="containers to start for the density test")
    parser.add_argument("--repeat", type=int, default=7, help="samples for the warm-start median")
    parser.add_argument("--workdir", default=None)
    parser.add_argument(
        "--weights-mb", type=int, default=64,
        help="size of a synthetic incompressible layer standing in for model weights",
    )
    args = parser.parse_args()

    ok, reason = userns_available()
    if not ok:
        print(f"cannot run: {reason}")
        return 1
    if not overlayfs_in_userns():
        print("cannot run: unprivileged overlayfs needs Linux 5.11+")
        return 1
    if find_busybox() is None:
        print("cannot run: busybox is not installed")
        return 1

    workdir = args.workdir or tempfile.mkdtemp(prefix="minicon-bench-")
    os.makedirs(workdir, exist_ok=True)
    store_root = os.path.join(workdir, "images")
    blob, _, diff = busybox_layer()
    layers, diffs = [blob], [diff]
    if args.weights_mb:
        # Random bytes: trained float32 weights barely compress, so a
        # compressible stand-in would understate both pull and unpack.
        weights_blob, _, weights_diff = make_layer([
            ("model", None, 0o755, None),
            ("model/weights.bin", os.urandom(args.weights_mb << 20), 0o644, None),
        ])
        layers.append(weights_blob)
        diffs.append(weights_diff)
    write_image(store_root, "busybox:v1", layers, diffs,
                {"Env": ["PATH=/bin"], "Entrypoint": ["/bin/true"], "WorkingDir": "/"})
    store = ImageStore(store_root)
    image_bytes = store.get("busybox:v1").total_size
    run_root = os.path.join(workdir, "run")

    # -- 1. cold start, phase by phase --------------------------------------
    cold = Container(ContainerConfig(image="busybox:v1", argv=["/bin/true"], name="cold"), store, run_root).create()
    cold.start()
    cold.wait(timeout=60)
    cold_phases = cold.phases
    cold.delete()

    # -- 2. warm starts ------------------------------------------------------
    samples = []
    for i in range(args.repeat):
        container = Container(
            ContainerConfig(image="busybox:v1", argv=["/bin/true"], name=f"warm{i}"), store, run_root
        ).create()
        started = time.perf_counter()
        container.start()
        container.wait(timeout=60)
        samples.append((time.perf_counter() - started, container.phases))
        container.delete()
    samples.sort(key=lambda pair: pair[0])
    warm_wall, warm_phases = samples[len(samples) // 2]

    print(f"image: busybox + {args.weights_mb} MB of weights "
          f"({human(image_bytes)} compressed, {len(layers)} layers)\n")
    print(table(
        [
            [name,
             f"{getattr(cold_phases, attr) * 1000:8.1f}",
             f"{getattr(warm_phases, attr) * 1000:8.1f}"]
            for name, attr in (
                ("resolve image", "resolve_s"),
                ("unpack layers", "unpack_s"),
                ("create cgroup", "cgroup_s"),
                ("create namespaces", "namespace_s"),
                ("write id maps", "idmap_s"),
                ("container setup", "setup_s"),
                ("  . mount overlay", "overlay_s"),
                ("  . mount /proc /dev /sys", "mounts_s"),
                ("  . pivot_root", "pivot_s"),
            )
        ] + [["TOTAL", f"{cold_phases.total_s * 1000:8.1f}", f"{warm_phases.total_s * 1000:8.1f}"]],
        ["phase", "first (ms)", "warm (ms)"],
    ))
    speedup = cold_phases.total_s / max(warm_phases.total_s, 1e-9)
    print(f"""
The first container pays {cold_phases.unpack_s * 1000:.0f} ms to unpack the image into the layer
cache. Every container after it reuses that directory read-only, so a warm
start is {warm_phases.total_s * 1000:.1f} ms -- {speedup:.0f}x faster, and the entire remaining cost is
namespace creation ({warm_phases.namespace_s * 1000:.1f} ms) and mounting /proc, /dev and /sys
({warm_phases.mounts_s * 1000:.1f} ms). Neither scales with image size.""")

    # -- 3. density ----------------------------------------------------------
    layers_bytes = tree_size(os.path.join(run_root, "layers"))
    containers = []
    start_times = []
    for i in range(args.count):
        container = Container(
            ContainerConfig(image="busybox:v1", argv=["/bin/sh", "-c", "sleep 30"], name=f"d{i}"),
            store, run_root,
        ).create()
        started = time.perf_counter()
        container.start()
        start_times.append(time.perf_counter() - started)
        containers.append(container)

    bundles_bytes = tree_size(os.path.join(run_root, "containers"))
    running = sum(1 for c in containers if c.state.status == "running")
    per_container = bundles_bytes / max(len(containers), 1)

    print(f"\n\n{args.count} containers from one image, all running at once:\n")
    print(table(
        [
            ["unpacked layer cache (shared)", human(layers_bytes)],
            [f"all {args.count} container bundles", human(bundles_bytes)],
            ["per additional container", human(per_container)],
            ["naive copy-per-container would be", human(layers_bytes * args.count)],
            ["median start", f"{statistics.median(start_times) * 1000:.1f} ms"],
            ["slowest start", f"{max(start_times) * 1000:.1f} ms"],
            ["running", f"{running}/{args.count}"],
        ],
        ["measure", "value"],
    ))
    ratio = (layers_bytes * args.count) / max(bundles_bytes, 1)
    print(f"""
Each container adds {human(per_container)}: an empty overlay upper directory, a work
directory and a state file. The image's {human(layers_bytes)} is unpacked once and
mounted read-only by all {args.count}, so {args.count} containers cost {ratio:.0f}x less disk than
copying the root filesystem per container would.

This is the mechanism behind scale-to-zero. Stopping a container and starting a
new one costs {statistics.median(start_times) * 1000:.0f} ms and no I/O proportional to the model.""")

    for container in containers:
        container.kill(grace=2.0)
        container.delete()
    if args.workdir is None:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
