"""Smoke test for the Resolve bridge — run from outside Resolve.

Usage (with Resolve running and scripting enabled to "Local"):
    py -3.12 scripts\\smoke_test.py

Exit codes:
    0 - success
    2 - SDK paths not found on disk
    3 - Resolve is not running / scripting disabled
    4 - other unexpected error
"""

from __future__ import annotations

import os
import sys

# Make the in-tree package importable without installing.
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

from slideshow.resolve_bridge import _smoke_test  # noqa: E402

if __name__ == "__main__":
    sys.exit(_smoke_test())
