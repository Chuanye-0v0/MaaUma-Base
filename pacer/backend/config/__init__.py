"""Root config package with automatic bridges namespace fallback."""
from __future__ import annotations
from pathlib import Path

_bridges_pkg = Path(__file__).resolve().parents[1] / "bridges" / "skills" / "backend" / 'config'
if _bridges_pkg.is_dir() and str(_bridges_pkg) not in __path__:
    __path__.append(str(_bridges_pkg))
