#!/usr/bin/env python3
"""Task queue — standalone CLI.

    python tq.py broker
    python tq.py worker -I scripts.tasks --concurrency 8
    python tq.py submit scripts.tasks.add 2 3 --wait

The argument parsing lives in `taskqueue/cli.py` so the platform can delegate
to exactly this code path (`ctl queue broker`) rather than shelling out.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from taskqueue.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
