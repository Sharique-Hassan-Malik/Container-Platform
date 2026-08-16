"""Make every module importable when pytest runs from the repository root.

Each module folder under `modules/` is its own source root — that is what lets
it be run standalone from its own directory. Collecting all of their tests in
one pytest run therefore needs each of those folders on `sys.path`, which is
what this does.

Without it, `pytest` from the root fails to collect while `cd modules/<name> &&
pytest` passes, which is a confusing way to find out about the layout.
"""

import os
import sys
from pathlib import Path

# The same setting `ctl/__init__.py` applies, for the same reason, applied here
# too because pytest collects the modules' own tests without importing `ctl`.
# Without it, the Raft tests leave gRPC serving and every later container test
# fails on unshare(CLONE_NEWUSER) with EINVAL. See minicon.linux.fork_thread_hazard.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")

REPO_ROOT = Path(__file__).resolve().parent
MODULES_ROOT = REPO_ROOT / "modules"

for path in [REPO_ROOT, *sorted(p for p in MODULES_ROOT.iterdir() if p.is_dir())]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# A couple of modules keep their package one level deeper than the module root.
for nested in ("",):
    if nested:
        extra = MODULES_ROOT / nested
        if extra.is_dir() and str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
