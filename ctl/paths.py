"""Make every module folder importable.

Each module under `modules/` is its own source root — `modules/raft-kv` holds
the `raft_kv` package — which is what lets a module be run straight from its
own directory. The platform composes them, so it puts all of those roots on
`sys.path` once, here.

This is the only place the layout is encoded. Everything else asks by module
name.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULES_ROOT = REPO_ROOT / "modules"

MODULES = (
    "orchestrator",
    "container-runtime",
    "image-toolkit",
    "raft-kv",
    "taskqueue",
)


def module_path(name: str) -> Path:
    return MODULES_ROOT / name


def add_modules(*names: str) -> list[str]:
    """Put module source roots on `sys.path`; returns the ones that exist."""
    found: list[str] = []
    for name in names or MODULES:
        folder = MODULES_ROOT / name
        if not folder.is_dir():
            continue
        found.append(name)
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    return found
