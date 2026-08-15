#!/usr/bin/env python3
"""Cold start, phase by phase, for the packaging choices that move the numbers.

    python3 bench/coldstart.py --preset medium

Six scenarios, one stopwatch, four phases each:

    pull ──▶ extract ──▶ spawn ──▶ load ──▶ first token

Two things make the numbers trustworthy rather than flattering:

**The page cache is dropped between phases.** Everything `extract` just wrote is
resident in RAM, so an un-evicted "load" measures memcpy and an un-evicted
"mmap" takes only minor faults. Both would be reported as disk numbers. Each run
flushes and evicts the unpacked tree first (`POSIX_FADV_DONTNEED`, no root
needed).

**Bytes are charged wherever they move.** A weights-at-startup fetch is a
download too. Charging it at the same modelled bandwidth as the image pull is
the only way the "keep your images small" advice gets tested rather than assumed.
"""

from __future__ import annotations

import argparse
import os
import shutil

from common import Fixture, add_common_args, human, table

from imagekit import measure

IN_IMAGE = """
FROM {base}
WORKDIR /app
COPY model.bin /app/model.bin
COPY app /app
USER 10001:10001
HEALTHCHECK --interval=10s --timeout=2s CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"]
ENTRYPOINT ["python3", "/app/serve.py", "--weights", "/app/model.bin"]
"""

AT_STARTUP = """
FROM {base}
WORKDIR /app
COPY app /app
USER 10001:10001
HEALTHCHECK --interval=10s --timeout=2s CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"]
ENTRYPOINT ["python3", "/app/serve.py"]
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    parser.add_argument("--bandwidth", type=float, default=200.0, help="Mbps for modelled transfers")
    parser.add_argument("--repeat", type=int, default=3, help="runs per scenario; the median is reported")
    args = parser.parse_args()

    fixture = Fixture(args)
    base = fixture.base_image()
    model = fixture.model()
    workdir = os.path.join(fixture.workdir, "coldstart")

    with_model = fixture.context("cs-in-image", with_model=True)
    without_model = fixture.context("cs-at-startup", with_model=False)

    fixture.builder(with_model, cache="cache-cs-in").build(IN_IMAGE.format(base=base), "cs-in-image:v1")
    fixture.builder(without_model, cache="cache-cs-out").build(AT_STARTUP.format(base=base), "cs-at-startup:v1")

    # The same image with the weights layer stored rather than deflated. Random
    # float32 barely compresses, so this trades ~nothing in size for a large
    # slice of extract time.
    stored = fixture.builder(with_model, cache="cache-cs-stored")
    stored.compression_level = 0
    stored.build(IN_IMAGE.format(base=base), "cs-in-image-stored:v1")

    base_layers = [d.digest for d in fixture.store.get_manifest(base).layers]
    all_layers = [d.digest for d in fixture.store.get_manifest("cs-in-image:v1").layers]
    stored_layers = [d.digest for d in fixture.store.get_manifest("cs-in-image-stored:v1").layers]

    scenarios = [
        ("in image, cold node", "cs-in-image:v1", [], ["--load", "eager"]),
        ("in image, base cached", "cs-in-image:v1", base_layers, ["--load", "eager"]),
        ("in image, all cached", "cs-in-image:v1", all_layers, ["--load", "eager"]),
        ("in image, gzip level 0", "cs-in-image-stored:v1", stored_layers, ["--load", "eager"]),
        ("in image, mmap load", "cs-in-image:v1", base_layers, ["--load", "mmap"]),
        ("at startup, any start", "cs-at-startup:v1", base_layers,
         ["--fetch-from", model, "--weights", "$ROOTFS/tmp/model.bin", "--load", "eager"]),
    ]

    rows, results = [], {}
    for label, reference, warm, extra in scenarios:
        samples = []
        for _ in range(args.repeat):
            run_dir = os.path.join(workdir, label.replace(" ", "-").replace(",", ""))
            if os.path.exists(run_dir):
                shutil.rmtree(run_dir)
            samples.append(measure(
                fixture.store, reference, run_dir,
                warm_layers=warm, bandwidth_mbps=args.bandwidth,
                extra_args=extra, cold_files=[model],
            ))
        samples.sort(key=lambda r: r.total_modelled_s)
        result = samples[len(samples) // 2]
        results[label] = result
        moved = result.pull_bytes + result.fetch_bytes
        rows.append([
            label,
            human(fixture.store.image_size(reference)),
            human(moved),
            f"{(result.pull_modelled_s + result.fetch_modelled_s) * 1000:.0f}",
            f"{result.extract_s * 1000:.0f}",
            f"{result.spawn_s * 1000:.0f}",
            f"{result.load_s * 1000:.0f}",
            f"{result.first_token_s * 1000:.0f}",
            f"{result.total_modelled_s * 1000:.0f}",
        ])

    print(
        f"base={base}   checkpoint={human(os.path.getsize(model))}   "
        f"transfers modelled at {args.bandwidth:.0f} Mbps   median of {args.repeat}\n"
        "All times in ms. Page cache evicted before every run.\n"
    )
    print(table(rows, ["scenario", "image", "moved", "transfer", "extract", "spawn", "load", "first tok", "TOTAL"]))

    cold = results["in image, cold node"]
    warm = results["in image, base cached"]
    hot = results["in image, all cached"]
    level0 = results["in image, gzip level 0"]
    mapped = results["in image, mmap load"]
    fetched = results["at startup, any start"]

    def ms(value: float) -> str:
        return f"{value * 1000:.0f} ms"

    # Both of these rows run fully cached, so their extract times are the only
    # phase that differs and the comparison isolates the codec.
    size_gzip = fixture.store.image_size("cs-in-image:v1")
    size_stored = fixture.store.image_size("cs-in-image-stored:v1")
    extract_saved = level0.extract_s - hot.extract_s

    print(f"""
Findings, in the order they change a decision:

1. Weights at startup wins the FIRST start and loses every one after it.
   First start on a node with only the base cached: {ms(fetched.total_modelled_s)} versus
   {ms(warm.total_modelled_s)} for weights-in-image. But the in-image path is a layer, so the
   second start on that node costs {ms(hot.total_modelled_s)} -- {hot.pull_bytes + hot.fetch_bytes} bytes move.
   The at-startup path re-downloads {human(fetched.fetch_bytes)} on every single start,
   including every scale-from-zero. Break-even is two starts per node.

2. Compressing weights buys almost nothing and costs every start.
   Comparing the two fully-cached rows, where extract is the only phase that
   differs: gzip level 6 gives a {human(size_gzip)} image that extracts in {ms(hot.extract_s)};
   level 0 (stored) gives {human(size_stored)} and extracts in {ms(level0.extract_s)}.
   Deflate buys {human(size_stored - size_gzip)} ({(1 - size_gzip / size_stored) * 100:.0f}% of the image) and charges
   {ms(-extract_saved)} of CPU for it on every container start. float32 weights are
   close to incompressible; the default level is tuned for source code.

3. mmap does not make loading faster, it makes it someone else's phase.
   load {ms(warm.load_s)} -> {ms(mapped.load_s)}, first token {ms(warm.first_token_s)} -> {ms(mapped.first_token_s)}.
   Total {ms(warm.total_modelled_s)} -> {ms(mapped.total_modelled_s)}. A dashboard showing "model load time"
   would record a {warm.load_s / max(mapped.load_s, 1e-9):.0f}x improvement that the user never experiences.
   mmap pays off when the process does not touch every weight -- not here,
   where the first forward pass touches all of them.

4. The base layer is the cheapest thing to get right.
   Cold node {ms(cold.total_modelled_s)} -> base cached {ms(warm.total_modelled_s)}, for no change to the
   application at all. Sharing one base across services means the first pull on
   a node is the only expensive one.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
