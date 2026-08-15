"""Locate sibling modules in this repository, without depending on the platform.

The control plane's optional backends — Raft for cluster state, the container
runtime for pods — live in their own module folders beside this one. Adding
them to `sys.path` here means the backends work when the orchestrator is run
straight from its own directory, exactly as they do through the platform CLI,
so "works standalone" and "works integrated" cannot drift apart.

Nothing here is required. If a sibling is absent the import simply fails later
and the caller falls back — an in-memory store, a simulated runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

# modules/orchestrator/orchestrator/_siblings.py -> modules/
_MODULES_ROOT = Path(__file__).resolve().parents[2]

SIBLINGS = ("raft-kv", "container-runtime", "image-toolkit", "taskqueue")


def add_siblings(*names: str) -> list[str]:
    """Put sibling module folders on `sys.path`. Returns the ones found."""
    found: list[str] = []
    for name in names or SIBLINGS:
        folder = _MODULES_ROOT / name
        if not folder.is_dir():
            continue
        found.append(name)
        if str(folder) not in sys.path:
            sys.path.append(str(folder))
    return found


def sibling_path(name: str) -> Path:
    return _MODULES_ROOT / name
