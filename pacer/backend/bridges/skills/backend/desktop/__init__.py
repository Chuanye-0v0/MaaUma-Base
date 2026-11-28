"""Public candidate package with main backend namespace fallback."""
from __future__ import annotations
from pathlib import Path

_p = Path(__file__).resolve()
while _p.name != "bridges" and _p != _p.parent:
    _p = _p.parent
_main_pkg = _p.parent / 'desktop'
if _main_pkg.is_dir() and str(_main_pkg) not in __path__:
    __path__.append(str(_main_pkg))
