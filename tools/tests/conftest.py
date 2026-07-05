"""Pytest configuration: make the repo root importable so `tools/` resolves.

We insert the repo root (parent of this `tools/tests/` directory) into
sys.path so `from tools.lib.log import ...` works when pytest is run from
the repo root.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
