#!/usr/bin/env python3
"""Layer ordering and reproducibility, measured as a 2x2.

    python3 bench/layer_order.py --preset medium

Two knobs, tested in every combination:

  * **order** -- does `COPY model.bin` come before or after `COPY app`?
  * **determinism** -- are tar mtimes and the gzip header pinned, or left to the
    wall clock the way a plain `tar | gzip` leaves them?

Then one line of application code changes and the image is rebuilt and pushed to
a registry that already holds the previous version. The registry accepts only
blobs it lacks, so the bytes it accepts are exactly what a deploy costs.

The two knobs turn out to control different costs, which is the point of running
all four cells rather than the two anyone expects:

    ordering     -> whether the weights layer is REBUILT   (CPU and wall clock)
    determinism  -> whether a rebuilt layer is RE-UPLOADED (network)

A reproducible build makes bad ordering merely slow. A non-reproducible one
makes it expensive on every push, forever.
"""

from __future__ import annotations

import argparse
import os
import time

from common import Fixture, add_common_args, human, table

from imagekit import ImageStore, copy_image

WEIGHTS_FIRST = """
FROM {base}
WORKDIR /app
COPY model.bin /app/model.bin
COPY app /app
USER 10001:10001
ENTRYPOINT ["python3", "/app/serve.py", "--weights", "/app/model.bin"]
"""

WEIGHTS_LAST = """
FROM {base}
WORKDIR /app
COPY app /app
COPY model.bin /app/model.bin
USER 10001:10001
ENTRYPOINT ["python3", "/app/serve.py", "--weights", "/app/model.bin"]
"""


def run_cell(fixture, base, context, order_label, template, deterministic):
    slug = f"{order_label.replace(' ', '-')}-{'det' if deterministic else 'nondet'}"
    dockerfile = template.format(base=base)
    registry = ImageStore(os.path.join(fixture.workdir, f"registry-{slug}"))

    def build(tag, version):
        fixture.write_code(context, version)
        builder = fixture.builder(context, cache=f"cache-{slug}")
        builder.deterministic = deterministic
        started = time.perf_counter()
        result = builder.build(dockerfile, tag)
        return result, time.perf_counter() - started

    # v1: nothing cached anywhere. Establishes what the registry already holds.
    v1, _ = build(f"{slug}:v1", 1)
    copy_image(fixture.store, registry, f"{slug}:v1")

    # v2: one line of application code differs. Everything else is identical.
    v2, elapsed = build(f"{slug}:v2", 2)
    push = copy_image(fixture.store, registry, f"{slug}:v2")

    return {
        "order": order_label,
        "deterministic": deterministic,
        "layers": len(v2.manifest.layers),
        "image": v2.image_size,
        "hits": f"{v2.cache_hits}/{len(v2.layer_steps)}",
        "rebuilt": v2.bytes_rebuilt,
        "build_s": elapsed,
        "pushed": push.bytes_sent,
        "v1": v1,
        "v2": v2,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    args = parser.parse_args()

    fixture = Fixture(args)
    base = fixture.base_image()
    context = fixture.context("order")

    cells = []
    for order_label, template in (("weights first", WEIGHTS_FIRST), ("weights last", WEIGHTS_LAST)):
        for deterministic in (True, False):
            cells.append(run_cell(fixture, base, context, order_label, template, deterministic))

    print(
        f"base={base}   checkpoint={human(os.path.getsize(fixture.model_path))}   "
        f"gzip level {args.compression}\n"
        "Cost of changing one line of application code and pushing:\n"
    )
    print(table(
        [[
            cell["order"],
            "yes" if cell["deterministic"] else "no",
            cell["hits"],
            human(cell["rebuilt"]),
            f"{cell['build_s']:.2f} s",
            human(cell["pushed"]),
        ] for cell in cells],
        ["order", "reproducible", "cache hits", "rebuilt", "build time", "re-pushed"],
    ))

    by_key = {(c["order"], c["deterministic"]): c for c in cells}
    good = by_key[("weights first", True)]
    slow = by_key[("weights last", True)]
    bad = by_key[("weights last", False)]

    print(f"""
Reading the table:

  weights first          the weights layer's cache key does not depend on the
                         app layer, so it is neither rebuilt nor re-pushed.
                         {human(good['rebuilt'])} rebuilt, {human(good['pushed'])} pushed.

  weights last + repro   the weights layer IS rebuilt ({human(slow['rebuilt'])}, {slow['build_s']:.2f} s of
                         it) but compresses to the same bytes, so its digest is
                         unchanged and the registry already has it: {human(slow['pushed'])} pushed.
                         Bad ordering costs {slow['build_s'] - good['build_s']:.2f} s per build and no network.

  weights last + no repro
                         same rebuild, but the tar carries fresh mtimes and the
                         gzip header carries the wall clock, so the digest moves
                         and the whole layer goes over the wire: {human(bad['pushed'])}.

Ordering controls rebuild cost. Reproducibility controls whether a rebuild also
costs a re-upload. Fixing only one of them fixes half the problem.""")

    print("\nPer-step detail, weights last (reproducible):")
    for step in slow["v2"].steps:
        if step.produces_layer:
            print(f"  {'HIT ' if step.cached else 'MISS'}  {human(step.layer_size):>9}  "
                  f"{step.duration_s * 1000:7.0f} ms  {step.instruction}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
