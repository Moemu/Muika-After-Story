#!/usr/bin/env python
"""Muika Core standalone process entry point.

Usage::

    python core_main.py [--host 127.0.0.1] [--port 8765]

Environment variables:
    MUIKA_CORE_HOST  -- WebSocket listen address (default 127.0.0.1)
    MUIKA_CORE_PORT  -- WebSocket listen port (default 8765)
"""

import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from muika.ipc.bootstrap import main  # noqa: E402

if __name__ == "__main__":
    main()
