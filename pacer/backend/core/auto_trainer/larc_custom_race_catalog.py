"""CN career graded race catalog & running-style helpers for L'Arc custom scheduling."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from core.auto_trainer.larc_calendar import period_ordinal

CATALOG_PATH = Path(__file__).resolve().parent / "assets/cn_larc_custom_races_v1.json"
SUPPORTED_RUNNING_STYLES = ("keep", "leader", "front", "middle", "back")
SCENARIO_RACE_KEYS = (
    "scenario.debut",
    "scenario.derby",
    "scenario.takarazuka",
    "scenario.overseas",
)
RUNNING_STYLE_LABELS = {
    "keep": "保持游戏当前",
    "leader": "领跑（逃）",
    "front": "跟前（先）",
    "middle": "居中（差）",
    "back": "后追（追）",
}
_STYLE_ALIASES = {
    "betweener": "middle",
    "chaser": "back",
    "default": "keep",
}
MAX_CUSTOM_RACES = 24


@lru_cache(maxsize=1)
def _raw_catalog() -> dict[str, Any]:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("schema_version") != "uma.cn_larc_custom_races.v1":
        raise ValueError("custom_race_catalog_invalid")
    by_id = {row["race_id"]: row for row in data.get("races", [])}
    return {"catalog": data, "by_id": by_id}


def load_catalog() -> dict[str, Any]:
    return deepcopy(_raw_catalog()["catalog"])


def race_by_id(race_id: str) -> dict[str, Any] | None:
    row = _raw_catalog()["by_id"].get(race_id)
    return deepcopy(row) if row is not None else None


def normalize_running_style(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("比赛跑法格式不正确。")
    cleaned = value.strip().lower()
    if not cleaned or cleaned in ("inherit", "auto"):
        return None
    cleaned = _STYLE_ALIASES.get(cleaned, cleaned)
    if cleaned not in SUPPORTED_RUNNING_STYLES:
        raise ValueError(f"不支持的比赛跑法：{value}")
    return cleaned


def normalize_race_running_styles(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("特定比赛跑法配置格式不正确。")
    by_id = _raw_catalog()["by_id"]
    allowed_special = set(SCENARIO_RACE_KEYS)
    result: dict[str, str] = {}
    for raw_key, raw_style in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError("特定比赛跑法缺少赛事标识。")
        key = raw_key.strip()
        if key not in allowed_special and key not in by_id:
            raise ValueError(f"特定比赛跑法包含未知赛事标识：{key}")
        norm_style = normalize_running_style(raw_style)
        if norm_style is not None:
            result[key] = norm_style
    return result


def detect_scenario_race_key(*, visible_text: str = "", cut_debut: bool = False) -> str | None:
    if cut_debut:
        return "scenario.debut"
    compact = "".join(str(visible_text or "").split())
    if not compact:
        return None
    if "初级新人战" in compact:
        return "scenario.debut"
    if "东京优骏" in compact or "日本德比" in compact:
        return "scenario.derby"
    if "宝冢纪念" in compact or "宝塚纪念" in compact:
        return "scenario.takarazuka"
    if any(tok in compact for tok in ("凯旋门", "尼尔", "福伊")):
        return "scenario.overseas"
    return None


def resolve_configured_running_style(
    goal: Any,
    *,
    custom_race: dict[str, Any] | None = None,
    visible_text: str = "",
    cut_debut: bool = False,
) -> tuple[str | None, str]:
    """Return `(configured_style, source)` where `configured_style` may be `'keep'`, `'leader'`, etc., or `None` if unset."""
    per_race = getattr(goal, "race_running_styles", None)
    if not isinstance(per_race, dict):
        per_race = {}
    if custom_race is not None:
        inline_style = normalize_running_style(custom_race.get("running_style"))
        if inline_style is not None:
            return inline_style, f"custom_race_inline:{custom_race.get('race_id')}"
        rid = str(custom_race.get("race_id") or "")
        if rid and rid in per_race:
            return per_race[rid], f"race_running_styles:{rid}"
    else:
        skey = detect_scenario_race_key(visible_text=visible_text, cut_debut=cut_debut)
        if skey and skey in per_race:
            return per_race[skey], f"race_running_styles:{skey}"
    default_style = normalize_running_style(getattr(goal, "running_style", None))
    if default_style is not None:
        return default_style, "default_running_style"
    return None, "unset"


def normalize_custom_races(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("自定义加赛列表格式不正确。")
    by_id = _raw_catalog()["by_id"]
    seen_ids: set[str] = set()
    by_ordinal: dict[int, str] = {}
    selected_rows: list[dict[str, Any]] = []
    for raw in value:
        if isinstance(raw, dict):
            race_id = raw.get("race_id")
        else:
            race_id = raw
        if not isinstance(race_id, str) or not race_id.strip():
            raise ValueError("自定义加赛条目缺少赛事标识。")
        race_id = race_id.strip()
        if race_id in seen_ids:
            continue
        row = by_id.get(race_id)
        if row is None:
            raise ValueError(f"未找到指定的比赛：{race_id}")
        if not row.get("larc_selectable"):
            reason = row.get("larc_conflict_reason") or "该回合不可自定义加赛"
            raise ValueError(f"{row['display_name_zh']}（{row['date_label_zh']}）不可选：{reason}")
        ordinal = int(row["larc_ordinal"])
        if ordinal in by_ordinal:
            other = by_id[by_ordinal[ordinal]]
            raise ValueError(
                f"同一回合（{row['date_label_zh']}）最多只能安排一场加赛："
                f"{other['display_name_zh']} 与 {row['display_name_zh']} 冲突。"
            )
        seen_ids.add(race_id)
        by_ordinal[ordinal] = race_id
        selected_rows.append(row)
    if len(selected_rows) > MAX_CUSTOM_RACES:
        raise ValueError(f"单局最多支持添加 {MAX_CUSTOM_RACES} 场自定义比赛。")
    selected_rows.sort(key=lambda r: int(r["larc_ordinal"]))
    return [str(r["race_id"]) for r in selected_rows]


def due_custom_race_for_period(custom_races: list[str] | tuple[str, ...] | None, period: Any) -> dict[str, Any] | None:
    if not custom_races or not isinstance(period, dict) or period.get("status") != "verified":
        return None
    evidence = period.get("evidence") or {}
    if isinstance(evidence, dict) and (
        evidence.get("date_visible") is False
        or evidence.get("date_reader") == "verified_scenario_milestone"
    ):
        return None
    val = period.get("value")
    if not isinstance(val, dict):
        return None
    normalized_val = dict(val)
    if normalized_val.get("half") in (1, "1"):
        normalized_val["half"] = "first"
    elif normalized_val.get("half") in (2, "2"):
        normalized_val["half"] = "second"
    ordinal = period_ordinal(normalized_val)
    if ordinal is None:
        return None
    by_id = _raw_catalog()["by_id"]
    for race_id in custom_races:
        row = by_id.get(race_id)
        if row is not None and row.get("larc_selectable") and int(row["larc_ordinal"]) == ordinal:
            return deepcopy(row)
    return None
