"""Compatibility re-export shim for core.skill_planner.preferences."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import sys

_p = Path(__file__).resolve()
while _p.name != "bridges" and _p != _p.parent:
    _p = _p.parent
_MAIN_BACKEND = str(_p.parent)
_MAIN_PATH = _p.parent / 'core/skill_planner/preferences.py'
if _MAIN_BACKEND not in sys.path:
    sys.path.append(_MAIN_BACKEND)

_MOD_NAME = '_main_core_skill_planner_preferences'
if 'core.skill_planner.preferences' in sys.modules and getattr(sys.modules['core.skill_planner.preferences'], "__file__", None) == str(_MAIN_PATH):
    _mod = sys.modules['core.skill_planner.preferences']
elif _MOD_NAME in sys.modules:
    _mod = sys.modules[_MOD_NAME]
else:
    _spec = importlib.util.spec_from_file_location(_MOD_NAME, _MAIN_PATH)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_MOD_NAME] = _mod
    _spec.loader.exec_module(_mod)

globals().update({k: getattr(_mod, k) for k in dir(_mod) if not k.startswith("__") or k in ("__all__",)})
