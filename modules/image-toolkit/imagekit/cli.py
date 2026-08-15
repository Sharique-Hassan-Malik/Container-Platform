"""Command line: build, inspect, unpack, push, lint.

    python3 -m imagekit build -f Dockerfile -t serve:v1 .
    python3 -m imagekit inspect serve:v1
    python3 -m imagekit push serve:v1 --to ./registry
    python3 -m imagekit unpack serve:v1 ./rootfs
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .build import BuildCache, Builder, human, lint
from .store import ImageStore, copy_image


def cmd_build(args) -> int:
    store = ImageStore(args.store)
    dockerfile = args.file or os.path.join(args.context, "Dockerfile")
    with open(dockerfile) as handle:
        text = handle.read()
    builder = Builder(
        store,
        args.context,
        cache=BuildCache(args.cache or os.path.join(store.root, "build-cache.json")),
        compression_level=args.compression,
    )
    result = builder.build(text, args.tag)
    print(result.summary())
    if args.verbose:
        for step in result.steps:
            mark = "CACHED" if step.cached else ("      " if not step.produces_layer else "BUILD ")
            size = human(step.layer_size) if step.produces_layer else ""
            print(f"  {mark} {step.duration_s * 1000:7.1f} ms  {size:>9}  {step.instruction}")
    return 1 if (args.strict and result.findings) else 0


def cmd_inspect(args) -> int:
    store = ImageStore(args.store)
    manifest = store.get_manifest(args.reference)
    config = store.get_config(args.reference)
    payload = {
        "reference": args.reference,
        "manifest_digest": manifest.serialise()[1].digest,
        "config_digest": manifest.config.digest,
        "size": store.image_size(args.reference),
        "layers": [{"digest": d.digest, "size": d.size} for d in manifest.layers],
        "config": config.to_json(),
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_ls(args) -> int:
    store = ImageStore(args.store)
    for tag in store.tags():
        manifest = store.get_manifest(tag)
        print(f"{tag:<30} {len(manifest.layers):2d} layers  {human(store.image_size(tag)):>10}")
    return 0


def cmd_unpack(args) -> int:
    store = ImageStore(args.store)
    store.unpack(args.reference, args.dest)
    print(f"unpacked {args.reference} -> {args.dest}")
    return 0


def cmd_push(args) -> int:
    source = ImageStore(args.store)
    destination = ImageStore(args.to)
    stats = copy_image(source, destination, args.reference)
    print(f"{args.reference}: {stats}")
    return 0


def cmd_lint(args) -> int:
    store = ImageStore(args.store)
    from .build import BuildResult

    manifest = store.get_manifest(args.reference)
    findings = lint(BuildResult(args.reference, manifest, store.get_config(args.reference)))
    for finding in findings:
        print(f"warning: {finding}")
    if not findings:
        print("no findings")
    return 1 if findings else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="imagekit")
    parser.add_argument("--store", default="./images", help="OCI layout directory")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build")
    build.add_argument("context")
    build.add_argument("-f", "--file", default=None)
    build.add_argument("-t", "--tag", required=True)
    build.add_argument("--cache", default=None)
    build.add_argument("--compression", type=int, default=6)
    build.add_argument("-v", "--verbose", action="store_true")
    build.add_argument("--strict", action="store_true", help="exit non-zero on any lint finding")
    build.set_defaults(func=cmd_build)

    inspect = sub.add_parser("inspect")
    inspect.add_argument("reference")
    inspect.set_defaults(func=cmd_inspect)

    listing = sub.add_parser("ls")
    listing.set_defaults(func=cmd_ls)

    unpack = sub.add_parser("unpack")
    unpack.add_argument("reference")
    unpack.add_argument("dest")
    unpack.set_defaults(func=cmd_unpack)

    push = sub.add_parser("push")
    push.add_argument("reference")
    push.add_argument("--to", required=True)
    push.set_defaults(func=cmd_push)

    linting = sub.add_parser("lint")
    linting.add_argument("reference")
    linting.set_defaults(func=cmd_lint)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
