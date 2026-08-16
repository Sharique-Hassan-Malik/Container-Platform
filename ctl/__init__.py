"""ctl — the control plane and the pieces it runs on, as one platform.

    from ctl import backends, plane

    control = plane.start(store="raft", runtime="container", nodes=3)

Five modules live under `modules/`, each usable on its own. This package is
what makes them a system: it selects backends, reports honestly about which
ones this host can actually run, and provides one CLI over the whole stack.
"""

import os

# Set before anything imports grpc, and this package is the first thing the CLI
# touches.
#
# The Raft store speaks gRPC and the container runtime builds namespaces by
# forking. Those two are incompatible by default: gRPC registers a
# `pthread_atfork` handler that recreates its polling threads in the child, and
# `unshare(CLONE_NEWUSER)` fails with EINVAL unless the caller is the only
# thread in its thread group. `ctl up --store raft --runtime container` runs
# both in one process, so without this the container backend fails with an
# "Invalid argument" that points nowhere near gRPC.
#
# Turning fork support off costs nothing here: the forked child unshares and
# execs the container. It never speaks gRPC.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")

from . import backends, paths  # noqa: E402

__version__ = "1.0.0"
__all__ = ["backends", "paths"]
