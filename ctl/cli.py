"""`ctl` — one command over the whole stack.

    ctl status                                  what this host can actually run
    ctl modules                                 the five modules and their own CLIs
    ctl image build ./ctx --tag serve:v1        -> image-toolkit
    ctl run serve:v1 -- /bin/echo hi            -> container-runtime
    ctl queue broker                            -> taskqueue
    ctl up --store raft --runtime container     control plane on real backends

`image`, `run` and `queue` delegate to the module's own CLI rather than
reimplementing it — the same code path you get by running that module
standalone.
"""

from __future__ import annotations

import argparse
import sys
import time

from . import backends, plane
from .paths import MODULES_ROOT, add_modules

add_modules()

_STANDALONE = {
    "orchestrator": "python -m pytest tests/   (library; see bench/)",
    "container-runtime": "python -m minicon run <image>",
    "image-toolkit": "python -m imagekit build <context>",
    "raft-kv": "python scripts/run_node.py",
    "taskqueue": "python tq.py broker",
}


def _cmd_status(args: argparse.Namespace) -> int:
    print("\n  backends\n")
    for seam in ("store", "runtime"):
        chosen = backends.best(seam)
        for backend in (b for b in backends.BACKENDS if b.seam == seam):
            usable, reason = backend.check()
            mark = "*" if backend.name == chosen.name else " "
            state = "ready" if usable else "unavailable"
            print(f"  {mark} {seam:8} {backend.name:11} {state:12} {reason}")
            print(f"      {'':8} {'':11} {backend.summary}")
        print()
    print(f"  * marks what `--store auto` / `--runtime auto` would pick here.\n")
    return 0


def _cmd_modules(args: argparse.Namespace) -> int:
    print()
    for name, invocation in _STANDALONE.items():
        present = (MODULES_ROOT / name).is_dir()
        print(f"  {name:20} {'present' if present else 'MISSING'}")
        print(f"  {'':20} cd modules/{name} && {invocation}")
    print()
    return 0


def _delegate(module: str, entry: str, argv: list[str]) -> int:
    """Hand off to a module's own CLI, unchanged."""
    import importlib

    add_modules(module)
    cli = importlib.import_module(entry)
    return int(cli.main(argv) or 0)


def _cmd_up(args: argparse.Namespace) -> int:
    from orchestrator import deployment_spec, pod_spec

    started = time.time()
    with plane.start(
        store=args.store,
        runtime=args.runtime,
        nodes=args.nodes,
        image_store=args.image_store,
    ) as cluster:
        for note in cluster.notes:
            print(f"  note: {note}")
        print(f"  control plane up on {cluster.description} "
              f"({args.nodes} nodes, {time.time() - started:.1f}s)")

        control = cluster.plane
        control.apply_deployment(args.name, deployment_spec(
            replicas=args.replicas,
            template={"labels": {"app": args.name}, "spec": pod_spec(args.image)},
            selector={"app": args.name},
            max_surge=1, max_unavailable=0,
        ))
        control.wait_available(args.name, args.replicas, timeout=args.timeout)
        print(f"  {args.name}: {args.replicas} replicas of {args.image} available")

        for pod in sorted(control.store.list("Pod"), key=lambda p: p.meta.name):
            node = pod.spec.get("nodeName") or pod.status.get("nodeName", "?")
            print(f"      {pod.meta.name:28} {pod.status.get('phase', '?'):10} {node}")

        if args.rollout:
            print(f"\n  rolling {args.name} to {args.rollout} "
                  f"(surge 1, unavailable 0)…")
            rolled = time.time()
            control.apply_deployment(args.name, deployment_spec(
                replicas=args.replicas,
                template={"labels": {"app": args.name}, "spec": pod_spec(args.rollout)},
                selector={"app": args.name},
                max_surge=1, max_unavailable=0,
            ))
            control.wait_available(args.name, args.replicas, timeout=args.timeout)
            print(f"  rollout complete in {time.time() - rolled:.1f}s — "
                  f"never below {args.replicas} available")

        if cluster.members:
            print(f"\n  cluster state replicated across "
                  f"{len(cluster.members)} Raft members")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctl",
        description="A container runtime, an image builder, a Raft store, a task "
                    "queue and the control plane that ties them together.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="what this host can run")
    sub.add_parser("modules", help="the modules and their standalone CLIs")

    # Listed so they show up in `ctl --help`; the arguments are never parsed
    # here — see main(), which hands them to the module's own parser intact so
    # `ctl image --help` shows imagekit's help, not this one's.
    for name, help_text in (("image", "build and inspect OCI images"),
                            ("run", "run one container"),
                            ("queue", "broker, worker and dashboard")):
        sub.add_parser(name, help=help_text, add_help=False)

    up = sub.add_parser("up", help="bring up a control plane and deploy")
    up.add_argument("--store", default="auto",
                    choices=["auto", *(b.name for b in backends.STORES)])
    up.add_argument("--runtime", default="auto",
                    choices=["auto", *(b.name for b in backends.RUNTIMES)])
    up.add_argument("--nodes", type=int, default=3)
    up.add_argument("--replicas", type=int, default=4)
    up.add_argument("--name", default="serve")
    up.add_argument("--image", default="serve:v1")
    up.add_argument("--rollout", metavar="IMAGE",
                    help="after the first deployment, roll to this image")
    up.add_argument("--image-store", default="./images")
    up.add_argument("--timeout", type=float, default=60.0)

    return parser


DELEGATED = {
    "image": ("image-toolkit", "imagekit.cli", []),
    "run": ("container-runtime", "minicon.cli", ["run"]),
    "queue": ("taskqueue", "taskqueue.cli", []),
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Delegated subcommands are dispatched before argparse sees them, so their
    # flags — including --help — reach the module's own parser untouched.
    if argv and argv[0] in DELEGATED:
        module, entry, prefix = DELEGATED[argv[0]]
        return _delegate(module, entry, [*prefix, *argv[1:]])

    args = build_parser().parse_args(argv)

    if args.command == "status":
        return _cmd_status(args)
    if args.command == "modules":
        return _cmd_modules(args)

    try:
        return _cmd_up(args)
    except (RuntimeError, KeyError) as exc:
        print(f"ctl: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
