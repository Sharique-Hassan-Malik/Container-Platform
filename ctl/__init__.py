"""ctl — the control plane and the pieces it runs on, as one platform.

    from ctl import backends, plane

    control = plane.start(store="raft", runtime="container", nodes=3)

Five modules live under `modules/`, each usable on its own. This package is
what makes them a system: it selects backends, reports honestly about which
ones this host can actually run, and provides one CLI over the whole stack.
"""

from . import backends, paths

__version__ = "1.0.0"
__all__ = ["backends", "paths"]
