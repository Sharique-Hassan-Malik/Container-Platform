"""Command line: run, images, ps, inspect, check.

    python3 -m minicon run --store ./images serve:v1
    python3 -m minicon run --store ./images --memory 256M --pids 64 serve:v1 -- python3 -c 'print(1)'
    python3 -m minicon check
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys

from .cgroups import Limits, available_controllers, delegated_root
from .container import Container, ContainerConfig, ContainerError
from .image import ImageStore
from .linux import namespace_ids, overlayfs_in_userns, userns_available
from .network import host_networking_available


def parse_size(text: str) -> int:
    """`256M`, `1G`, `1048576`."""
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    text = text.strip().upper().rstrip("B")
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text)


def cmd_run(args) -> int:
    store = ImageStore(args.store)
    limits = Limits(
        memory_bytes=parse_size(args.memory) if args.memory else None,
        cpu_quota=args.cpu,
        pids_max=args.pids,
    )
    config = ContainerConfig(
        image=args.image,
        name=args.name or "",
        argv=list(args.argv or []),
        env=dict(pair.split("=", 1) for pair in (args.env or [])),
        workdir=args.workdir or "",
        user=args.user or "",
        hostname=args.hostname or "",
        limits=limits,
        readonly_root=args.readonly,
        use_init=args.init,
        network="none" if args.no_network else "private",
    )
    container = Container(config, store, args.root).create()

    for label, entries in container.skipped.items():
        for entry in entries:
            print(f"warning: {label}: {entry}", file=sys.stderr)
    if args.verbose:
        print(f"id mapping: {container.mapping.describe()}", file=sys.stderr)

    try:
        container.start()
    except ContainerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        container.delete()
        return 1

    def forward(signum, _frame):
        container.kill(signum)

    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGTERM, forward)

    code = container.wait()
    if args.verbose:
        print(container.phases.table(), file=sys.stderr)
        usage = container.usage()
        print(
            f"peak memory {usage.memory_peak / 1e6:.1f} MB, cpu {usage.cpu_usage_us / 1000:.0f} ms, "
            f"oom kills {usage.oom_kills}",
            file=sys.stderr,
        )
    if not args.keep:
        container.delete()
    return code


def cmd_images(args) -> int:
    store = ImageStore(args.store)
    for tag in store.tags():
        image = store.get(tag)
        print(f"{tag:<32} {len(image.layer_digests):2d} layers  {image.total_size / 1e6:8.1f} MB  {' '.join(image.argv)}")
    return 0


def cmd_inspect(args) -> int:
    store = ImageStore(args.store)
    image = store.get(args.image)
    print(json.dumps({
        "reference": image.reference,
        "layers": image.layer_digests,
        "diff_ids": image.diff_ids,
        "entrypoint": image.entrypoint,
        "cmd": image.cmd,
        "env": image.env,
        "user": image.user,
        "working_dir": image.working_dir,
        "healthcheck": image.healthcheck,
    }, indent=2))
    return 0


def cmd_ps(args) -> int:
    root = os.path.join(args.root, "containers")
    if not os.path.isdir(root):
        return 0
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, "state.json")
        if not os.path.exists(path):
            continue
        with open(path) as handle:
            state = json.load(handle)
        print(f"{state['name']:<24} {state['status']:<9} pid={state['pid']:<8} {state['image']}")
    return 0


def cmd_check(args) -> int:
    """Report what this host can and cannot do, before anything fails obscurely."""
    ok, reason = userns_available()
    print(f"user namespaces      : {'yes' if ok else 'NO'} ({reason})")
    print(f"unprivileged overlayfs: {'yes' if overlayfs_in_userns() else 'NO'} (needs Linux 5.11+)")
    try:
        root = delegated_root()
        controllers = sorted(available_controllers(root))
        print(f"cgroup v2 delegation : yes ({root})")
        print(f"  controllers        : {', '.join(controllers) or 'none'}")
        for needed in ("memory", "cpu", "pids"):
            if needed not in controllers:
                print(f"  warning: no {needed!r} controller -- those limits will be ignored")
    except Exception as exc:  # noqa: BLE001
        print(f"cgroup v2 delegation : NO ({exc})")
    net_ok, net_reason = host_networking_available()
    print(f"host networking      : {'yes' if net_ok else 'no'} ({net_reason})")

    from .idmap import helper_available, subid_available

    print(f"newuidmap/newgidmap  : {'yes' if helper_available() else 'no'}"
          f"{'' if helper_available() else ' (install the uidmap package for multi-UID containers)'}")
    print(f"/etc/subuid entry    : {'yes' if subid_available() else 'no'}")
    print(f"\nthis process's namespaces: {json.dumps(namespace_ids(), indent=2)}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minicon")
    parser.add_argument("--store", default="./images", help="OCI image layout directory")
    parser.add_argument("--root", default="./.run", help="runtime state and layer cache")

    # The same two flags, accepted *after* the subcommand as well. Before is the
    # usual shape for a global, but `ctl run --store X ...` delegates here with
    # the subcommand already in place, and rejecting a flag purely on its
    # position is a worse answer than accepting it in both.
    #
    # SUPPRESS is what makes this safe: without it the subparser's own default
    # would overwrite a value given before the subcommand, so `--store X run`
    # would silently fall back to ./images.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", default=argparse.SUPPRESS,
                        help="OCI image layout directory")
    common.add_argument("--root", default=argparse.SUPPRESS,
                        help="runtime state and layer cache")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common])
    run.add_argument("image")
    run.add_argument("argv", nargs="*", help="override the image entrypoint")
    run.add_argument("--name")
    run.add_argument("--memory", help="e.g. 256M")
    run.add_argument("--cpu", type=float, help="cores, e.g. 0.5")
    run.add_argument("--pids", type=int)
    run.add_argument("--env", action="append")
    run.add_argument("--workdir")
    run.add_argument("--user")
    run.add_argument("--hostname")
    run.add_argument("--readonly", action="store_true")
    run.add_argument("--init", action="store_true", help="run a reaping init as PID 1")
    run.add_argument("--no-network", action="store_true")
    run.add_argument("--keep", action="store_true", help="do not delete the bundle on exit")
    run.add_argument("-v", "--verbose", action="store_true")
    run.set_defaults(func=cmd_run)

    images = sub.add_parser("images", parents=[common])
    images.set_defaults(func=cmd_images)

    inspect = sub.add_parser("inspect", parents=[common])
    inspect.add_argument("image")
    inspect.set_defaults(func=cmd_inspect)

    ps = sub.add_parser("ps", parents=[common])
    ps.set_defaults(func=cmd_ps)

    check = sub.add_parser("check", parents=[common])
    check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
