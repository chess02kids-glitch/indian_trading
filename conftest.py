"""Pytest root conftest: make the repository importable without installation.

CI installs the package; this keeps ``pytest`` runnable from a bare checkout.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
