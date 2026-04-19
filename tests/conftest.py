"""
pytest config for the cHAP Seller Tracker test suite.

Adds the project root to sys.path so `import analytics`, `import normalize`,
etc. work without an editable install. Kept deliberately tiny — we don't
want tests depending on a package layout we haven't adopted.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `tests/` sits directly under the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
