"""
Root conftest.py — ensures the repo root is on sys.path so that
`from tools.<module> import ...` works in tests without per-file path hacks.

Note: older test files in tests/ each do their own
`sys.path.insert(0, str(ROOT / "tools"))` so they can import bare module
names (e.g. `from config_loader import ...`). Both patterns coexist; this
conftest does not replace those per-file inserts.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
