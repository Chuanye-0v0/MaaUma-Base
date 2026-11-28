"""Portable settings bound by the selected controller before importing task owners."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import sys

_p = Path(__file__).resolve()
while _p.name != "bridges" and _p != _p.parent:
    _p = _p.parent
_MAIN_BACKEND = str(_p.parent)
_MAIN_PATH = _p.parent / "config" / "settings.py"
if _MAIN_BACKEND not in sys.path:
    sys.path.append(_MAIN_BACKEND)

_MOD_NAME = "_main_config_settings"
if "config.settings" in sys.modules and getattr(sys.modules["config.settings"], "__file__", None) == str(_MAIN_PATH):
    _mod = sys.modules["config.settings"]
elif _MOD_NAME in sys.modules:
    _mod = sys.modules[_MOD_NAME]
else:
    _spec = importlib.util.spec_from_file_location(_MOD_NAME, _MAIN_PATH)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_MOD_NAME] = _mod
    _spec.loader.exec_module(_mod)

globals().update({k: getattr(_mod, k) for k in dir(_mod) if not k.startswith("__") or k in ("__all__",)})

try:
    from desktop.umaa_public_context import endpoint as _public_endpoint
    ADB_ENDPOINT = _public_endpoint()
    ADB_PORT = int(_public_endpoint().rpartition(":")[2])
except Exception:
    pass
