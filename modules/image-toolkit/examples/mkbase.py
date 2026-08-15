#!/usr/bin/env python3
"""Construct base root filesystems from the host, at three levels of fat.

There is no Docker on this machine and no registry to pull `python:3.12-slim`
from, so the bases are built the way those images are built underneath: copy an
interpreter, close over its shared-library dependencies, and decide how much of
the standard library survives.

    full         everything: whole stdlib, a shell, coreutils
    slim         stdlib minus the parts nothing imports at runtime, still a shell
    distroless   only the modules the payload actually imports, and no shell

The third one is not just smaller. It has no `/bin/sh`, which means a
`HEALTHCHECK CMD curl ...` written in shell form silently never runs -- the
single most common way a distroless migration breaks a service. `bench/base_images.py`
demonstrates exactly that.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import sysconfig

KINDS = ("full", "slim", "distroless")

# Stdlib subtrees that exist for development, not for running a server.
SLIM_DROP = {
    "test", "tests", "idlelib", "tkinter", "ensurepip", "lib2to3",
    "pydoc_data", "turtledemo", "__pycache__", "distutils",
}
SLIM_DROP_PREFIX = ("config-",)   # 28 MB of static library for building extensions


def ldd_closure(binaries: list[str]) -> set[str]:
    """Every shared object the given binaries need, transitively.

    `ldd` already resolves transitively, so one pass per binary is enough. The
    loader itself (`ld-linux-*.so`) appears without an arrow and is easy to
    drop by accident -- an image missing it fails with the famously unhelpful
    "no such file or directory" on a file that plainly exists.
    """
    found: set[str] = set()
    for binary in binaries:
        try:
            output = subprocess.run(["ldd", binary], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in output.splitlines():
            line = line.strip()
            if "=>" in line:
                path = line.split("=>", 1)[1].strip().split(" ")[0]
            elif line.startswith("/"):
                path = line.split(" ")[0]
            else:
                continue
            if path.startswith("/") and os.path.exists(path):
                found.add(path)
    return found


def traced_modules(script: str, args: list[str]) -> set[str]:
    """Run the payload and record which files ended up in sys.modules.

    Static import analysis misses everything imported inside a function, and a
    distroless image built from a static analysis is a distroless image that
    crashes on the first request. Running the real thing is the only honest way
    to know.
    """
    # The output path is bound now, not read from sys.argv at exit: the payload
    # gets its own argv, so anything the atexit hook reads from sys.argv later
    # would be the payload's arguments.
    probe = (
        "import sys, json, runpy, atexit\n"
        "out = sys.argv[1]\n"
        "def dump():\n"
        "    paths = set()\n"
        "    for m in list(sys.modules.values()):\n"
        "        f = getattr(m, '__file__', None)\n"
        "        if f: paths.add(f)\n"
        "    open(out, 'w').write(json.dumps(sorted(paths)))\n"
        "atexit.register(dump)\n"
        "sys.argv = sys.argv[2:]\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    out_file = os.path.join(os.path.dirname(script) or ".", ".traced.json")
    subprocess.run(
        [sys.executable, "-c", probe, out_file, script, *args],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if not os.path.exists(out_file):
        return set()
    with open(out_file) as handle:
        paths = set(json.load(handle))
    os.unlink(out_file)
    return paths


def _copy(src: str, dst: str, root: str) -> None:
    """Copy one path into the image at `dst`, resolving symlinks *inside* `root`.

    Symlinks are preserved rather than flattened -- `libfoo.so.1 -> libfoo.so.1.2`
    is how the loader finds libraries, and a tree of copies is both larger and
    subtly wrong. But a preserved link needs its target present, so the chain is
    followed and each hop materialised at its own path within the image.

    The two cases differ: a relative target resolves next to the link, an
    absolute one (`/etc/python3.12/sitecustomize.py`) resolves at that same
    absolute path inside the image, which may be nowhere near the link.
    """
    if os.path.abspath(src) == os.path.abspath(dst):
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.islink(src):
        shutil.copy2(src, dst)
        return

    target = os.readlink(src)
    if os.path.lexists(dst):
        os.unlink(dst)
    os.symlink(target, dst)
    if target.startswith("/"):
        resolved, resolved_dst = target, os.path.join(root, target.lstrip("/"))
    else:
        resolved = os.path.join(os.path.dirname(src), target)
        resolved_dst = os.path.join(os.path.dirname(dst), target)
    if os.path.exists(resolved) and not os.path.lexists(resolved_dst):
        _copy(resolved, resolved_dst, root)


def _copy_tree(src: str, dst: str, root: str, *, drop: set[str] = frozenset(), drop_prefix: tuple = ()) -> None:
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [
            d for d in dirnames
            if d not in drop and not any(d.startswith(p) for p in drop_prefix)
        ]
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            source = os.path.join(dirpath, name)
            _copy(source, os.path.join(dst, os.path.relpath(source, src)), root)


def build(kind: str, dest: str, *, payload: str | None = None, payload_args: list[str] | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown base kind {kind!r}")
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)

    exe = os.path.realpath(sys.executable)
    stdlib = sysconfig.get_paths()["stdlib"]
    stdlib_rel = os.path.relpath(stdlib, "/")
    binaries = [exe]

    # -- interpreter ---------------------------------------------------------
    _copy(exe, os.path.join(dest, os.path.relpath(exe, "/")), dest)
    os.makedirs(os.path.join(dest, "usr/bin"), exist_ok=True)
    link = os.path.join(dest, "usr/bin/python3")
    if not os.path.lexists(link):
        os.symlink("/" + os.path.relpath(exe, "/"), link)

    # -- standard library ----------------------------------------------------
    if kind == "full":
        _copy_tree(stdlib, os.path.join(dest, stdlib_rel), dest)
    elif kind == "slim":
        _copy_tree(stdlib, os.path.join(dest, stdlib_rel), dest, drop=SLIM_DROP, drop_prefix=SLIM_DROP_PREFIX)
    else:
        if payload is None:
            raise ValueError("distroless needs --payload to trace")
        used = traced_modules(payload, payload_args or [])
        kept = 0
        for path in used:
            if path.startswith(stdlib) and os.path.exists(path):
                _copy(path, os.path.join(dest, os.path.relpath(path, "/")), dest)
                kept += 1
        # encodings is imported by the interpreter itself before user code runs,
        # so tracing sees only the codec that happened to be used.
        for extra in ("encodings", "lib-dynload"):
            source = os.path.join(stdlib, extra)
            if os.path.isdir(source):
                _copy_tree(source, os.path.join(dest, stdlib_rel, extra), dest)

    for dirpath, _, filenames in os.walk(os.path.join(dest, stdlib_rel)):
        for name in filenames:
            if name.endswith(".so"):
                binaries.append(os.path.join(dirpath, name))

    # -- site-packages: numpy ------------------------------------------------
    import numpy

    numpy_dir = os.path.dirname(numpy.__file__)
    site_rel = os.path.relpath(os.path.dirname(numpy_dir), "/")
    _copy_tree(numpy_dir, os.path.join(dest, site_rel, "numpy"), dest, drop={"tests", "__pycache__"} if kind != "full" else {"__pycache__"})
    # numpy ships its BLAS in a sibling .libs directory that ldd on the .so files
    # will point at; copy it wholesale rather than reasoning about RPATH.
    bundled = os.path.join(os.path.dirname(numpy_dir), "numpy.libs")
    if os.path.isdir(bundled):
        _copy_tree(bundled, os.path.join(dest, site_rel, "numpy.libs"), dest)
    for dirpath, _, filenames in os.walk(os.path.join(dest, site_rel)):
        for name in filenames:
            if name.endswith(".so") or ".so." in name:
                binaries.append(os.path.join(dirpath, name))

    # -- shared libraries ----------------------------------------------------
    for lib in ldd_closure(binaries):
        _copy(lib, os.path.join(dest, os.path.relpath(lib, "/")), dest)

    # -- shell and coreutils -------------------------------------------------
    if kind in ("full", "slim"):
        tools = ["/bin/sh", "/bin/dash", "/bin/ls", "/bin/cat", "/bin/echo"] if kind == "full" else ["/bin/sh", "/bin/dash"]
        present = [t for t in tools if os.path.exists(t)]
        for tool in present:
            _copy(tool, os.path.join(dest, os.path.relpath(tool, "/")), dest)
        for lib in ldd_closure([t for t in present if not os.path.islink(t)]):
            _copy(lib, os.path.join(dest, os.path.relpath(lib, "/")), dest)

    for directory in ("tmp", "proc", "sys", "dev", "app"):
        os.makedirs(os.path.join(dest, directory), exist_ok=True)

    return {
        "kind": kind,
        "root": dest,
        "bytes": _tree_size(dest),
        "files": sum(len(f) for _, _, f in os.walk(dest)),
        "has_shell": os.path.lexists(os.path.join(dest, "bin/sh")),
    }


def smoke_test(dest: str) -> tuple[bool, str]:
    """Prove the base can actually run numpy, without needing a container yet."""
    stdlib_rel = os.path.relpath(sysconfig.get_paths()["stdlib"], "/")
    import numpy

    site_rel = os.path.relpath(os.path.dirname(os.path.dirname(numpy.__file__)), "/")
    env = {
        "PYTHONHOME": os.path.join(dest, os.path.relpath(sys.prefix, "/")),
        "PYTHONPATH": os.pathsep.join([os.path.join(dest, stdlib_rel), os.path.join(dest, site_rel)]),
        "LD_LIBRARY_PATH": os.pathsep.join(
            os.path.join(dest, p) for p in ("lib/x86_64-linux-gnu", "usr/lib/x86_64-linux-gnu", "lib64")
        ),
        "PATH": "/usr/bin:/bin",
    }
    exe = os.path.join(dest, os.path.relpath(os.path.realpath(sys.executable), "/"))
    result = subprocess.run(
        [exe, "-c", "import numpy; print(numpy.zeros(3).sum())"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def _tree_size(root: str) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                total += os.path.getsize(path)
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument("dest")
    parser.add_argument("--payload", default=None, help="script to trace for the distroless base")
    parser.add_argument(
        "--payload-args",
        default="",
        help="arguments for --payload as one quoted string, e.g. \"--weights model.bin\"",
    )
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()

    import shlex

    info = build(args.kind, args.dest, payload=args.payload, payload_args=shlex.split(args.payload_args))
    print(f"{info['kind']:<12} {info['bytes'] / 1e6:7.1f} MB  {info['files']:5d} files  shell={info['has_shell']}")
    if args.smoke:
        ok, output = smoke_test(args.dest)
        print(f"  smoke test: {'PASS' if ok else 'FAIL'}  {output}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
