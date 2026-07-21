"""Make the repo root importable so ``from tools import ...`` works under pytest
regardless of the current working directory or install state."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
