"""Registry and Strategy Pack SPI resolver for tiered skill ladder presets.

No event-specific meta skill lists live in the public open-source base.
Optional external strategy packs supply named ``skill_ladder_presets`` via
``larc_strategy_extension``, while users can also build or customize ladders
directly in the UI / JSON configuration.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys


TIERED_LADDER_PRESETS: dict[str, dict] = {}


def _strategy_extension():
    for mod_name in (
        "core.auto_trainer.larc_strategy_extension",
        "_pacer_larc_strategy_extension",
    ):
        cached = sys.modules.get(mod_name)
        if cached is not None:
            return cached
    try:
        from core.auto_trainer import larc_strategy_extension

        return larc_strategy_extension
    except ImportError:
        path = Path(__file__).resolve().parents[1] / "auto_trainer/larc_strategy_extension.py"
        spec = importlib.util.spec_from_file_location("_pacer_larc_strategy_extension", path)
        if spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


def list_tiered_ladder_presets() -> list[dict]:
    """Return metadata for built-in and installed strategy-pack skill ladder presets."""
    out: list[dict] = []
    seen: set[str] = set()
    for key, val in TIERED_LADDER_PRESETS.items():
        seen.add(key)
        out.append({
            "id": key,
            "name": str(val.get("name") or key),
            "summary": str(val.get("summary") or ""),
            "pack_id": "builtin",
        })
    ext = _strategy_extension()
    reg = ext.ensure_bootstrapped()
    for item in reg.skill_ladder_presets:
        pid = str(item.get("id") or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append({
            key: item[key]
            for key in ("id", "name", "summary", "pack_id")
            if item.get(key) is not None
        })
    return out


def resolve_tiered_ladder_preset(preset_key: str) -> dict:
    """Return a deep copy of a registered tiered-ladder skill preset."""
    if not isinstance(preset_key, str) or not preset_key.strip():
        raise ValueError("arc_policy_ladder_preset_id_invalid")
    clean = preset_key.strip()
    if clean in TIERED_LADDER_PRESETS:
        return deepcopy(TIERED_LADDER_PRESETS[clean])
    ext = _strategy_extension()
    reg = ext.ensure_bootstrapped()
    policy = reg.get_skill_ladder_preset(clean)
    if policy is not None:
        return deepcopy(policy)
    raise ValueError("arc_policy_ladder_preset_id_invalid")
