"""Put the worker's flat modules (config, connection, worker, deploy) on the path."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
