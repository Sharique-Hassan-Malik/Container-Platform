#!/usr/bin/env python3
"""Base images: what "slim" and "distroless" actually cost and actually break.

    python3 bench/base_images.py --preset small

There is no Docker here and no registry to pull `python:3.12-slim` from, so the
three bases are constructed from this host's own interpreter (`examples/mkbase.py`):
the whole standard library, a trimmed one, or only the modules the payload was
observed to import. Each is smoke-tested by running numpy out of it before it
becomes an image.

Two results, and the second is the one that causes incidents:

  * how much a smaller base actually saves once numpy is in the picture
  * a shell-form HEALTHCHECK on a distroless base can never run, because there
    is no `/bin/sh` to run it -- and nothing reports an error, the container
    simply never becomes healthy
"""

from __future__ import annotations

import argparse
import os
import sys

from common import Fixture, add_common_args, human, table

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples"))

APP = """
FROM {base}
WORKDIR /app
COPY model.bin /app/model.bin
COPY app /app
USER 10001:10001
{healthcheck}
ENTRYPOINT ["python3", "/app/serve.py", "--weights", "/app/model.bin"]
"""

EXEC_FORM = 'HEALTHCHECK --interval=10s CMD ["python3", "/app/healthz.py"]'
SHELL_FORM = "HEALTHCHECK --interval=10s CMD python3 /app/healthz.py"

# A builder stage that produces an artifact, and a final stage that keeps only
# the artifact. The builder's own bulk never reaches the runtime image.
MULTISTAGE = """
FROM {full} AS builder
WORKDIR /build
COPY model.bin /build/model.bin
COPY app /build
RUN python3 -c "import os,sys; src=os.environ['IMAGEKIT_ROOTFS']+'/build/model.bin'; \
open(os.environ['IMAGEKIT_ROOTFS']+'/build/model.packed','wb').write(open(src,'rb').read())"

FROM {distroless}
WORKDIR /app
COPY --from=builder /build/model.packed /app/model.bin
COPY app /app
USER 10001:10001
ENTRYPOINT ["python3", "/app/serve.py", "--weights", "/app/model.bin"]
"""


def healthcheck_executable(rootfs: str, config) -> tuple[bool, str]:
    """Can this image actually run its own healthcheck?

    An exec-form check runs its argv directly; a shell-form check is rewritten
    by the runtime to `/bin/sh -c "..."`. The second one needs a shell to exist
    inside the image, which is exactly what a distroless base removes.
    """
    if config.healthcheck is None:
        return False, "no healthcheck defined"
    test = config.healthcheck.test
    if not test:
        return False, "empty healthcheck"
    if test[0] == "CMD-SHELL":
        shell = os.path.join(rootfs, "bin/sh")
        if not os.path.lexists(shell):
            return False, "shell form, but /bin/sh is not in the image"
        return True, "shell form, /bin/sh present"
    program = test[1] if test[0] == "CMD" else test[0]
    if program.startswith("/"):
        found = os.path.lexists(os.path.join(rootfs, program.lstrip("/")))
    else:
        found = any(
            os.path.lexists(os.path.join(rootfs, d, program))
            for d in ("usr/local/bin", "usr/bin", "bin")
        )
    return found, f"exec form, {program} {'found' if found else 'MISSING'}"


def main() -> int:
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    args = parser.parse_args()
    fixture = Fixture(args)
    context = fixture.context("bases")
    with open(os.path.join(context, "app", "healthz.py"), "w") as handle:
        handle.write("import sys\nsys.exit(0)\n")

    # -- 1. the bases themselves --------------------------------------------
    rows = []
    for kind in ("full", "slim", "distroless"):
        rootfs = fixture.base_rootfs(kind)
        reference = fixture.base_image(kind)
        manifest = fixture.store.get_manifest(reference)
        raw = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, _, fs in os.walk(rootfs) for f in fs
            if not os.path.islink(os.path.join(dp, f))
        )
        files = sum(len(fs) for _, _, fs in os.walk(rootfs))
        rows.append([
            kind, human(raw), human(manifest.total_layer_bytes),
            f"{manifest.total_layer_bytes / raw:.2f}", str(files),
            "yes" if os.path.lexists(os.path.join(rootfs, "bin/sh")) else "no",
        ])
    print("Base root filesystems, built from this host's interpreter:\n")
    print(table(rows, ["base", "unpacked", "compressed", "ratio", "files", "/bin/sh"]))

    # -- 2. the same application on each -------------------------------------
    print("\n\nThe same model server on each base:\n")
    rows = []
    images = {}
    for kind in ("full", "slim", "distroless"):
        base = fixture.base_image(kind)
        reference = f"app-{kind}:v1"
        result = fixture.builder(context, cache=f"cache-base-{kind}").build(
            APP.format(base=base, healthcheck=EXEC_FORM), reference
        )
        images[kind] = result
        rows.append([kind, str(len(result.manifest.layers)), human(result.image_size)])
    biggest = images["full"].image_size
    for row, kind in zip(rows, ("full", "slim", "distroless")):
        row.append(f"{(1 - images[kind].image_size / biggest) * 100:.0f}%")
    print(table(rows, ["base", "layers", "image", "saved vs full"]))

    # -- 3. the healthcheck trap ---------------------------------------------
    print("\n\nHealthcheck form vs base -- can the image run its own check?\n")
    rows = []
    for kind in ("full", "slim", "distroless"):
        base = fixture.base_image(kind)
        rootfs = os.path.join(fixture.workdir, f"probe-{kind}")
        for form_name, form in (("exec", EXEC_FORM), ("shell", SHELL_FORM)):
            reference = f"hc-{kind}-{form_name}:v1"
            fixture.builder(context, cache=f"cache-hc-{kind}-{form_name}").build(
                APP.format(base=base, healthcheck=form), reference
            )
            fixture.store.unpack(reference, rootfs)
            config = fixture.store.get_config(reference)
            ok, why = healthcheck_executable(rootfs, config)
            rows.append([kind, form_name, "runs" if ok else "NEVER RUNS", why])
    print(table(rows, ["base", "form", "verdict", "reason"]))
    print(
        "\nThe distroless + shell-form cell is the whole warning. The image builds,\n"
        "the manifest is valid, the container starts and serves traffic -- and the\n"
        "healthcheck can never succeed, so an orchestrator kills the pod on a loop\n"
        "while the application logs nothing. Write healthchecks in exec form."
    )

    # -- 4. multi-stage -------------------------------------------------------
    print("\n\nMulti-stage: a fat builder, a thin runtime.\n")
    multi = fixture.builder(context, cache="cache-multi").build(
        MULTISTAGE.format(full=fixture.base_image("full"), distroless=fixture.base_image("distroless")),
        "app-multistage:v1",
    )
    single = images["full"]
    print(table(
        [
            ["single stage (full base)", str(len(single.manifest.layers)), human(single.image_size)],
            ["multi-stage (build on full, ship distroless)", str(len(multi.manifest.layers)), human(multi.image_size)],
        ],
        ["build", "layers", "shipped image"],
    ))
    print(
        f"\nThe builder stage ran a RUN instruction against the full base and produced\n"
        f"an artifact; only that artifact crossed into the final image. {multi.stages_built} stages were\n"
        f"built, {human(single.image_size - multi.image_size)} of build tooling was left behind, and the manifest of the\n"
        f"shipped image references no layer the builder created except the copied one."
    )

    print("\n\nLint findings on the shipped image:")
    for finding in multi.findings or ["  (none)"]:
        print(f"  - {finding}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
