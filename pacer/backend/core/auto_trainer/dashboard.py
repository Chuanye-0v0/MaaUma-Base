from __future__ import annotations

import json
from pathlib import Path
from typing import Any


TRAINING_TYPES = ("speed", "stamina", "power", "guts", "wisdom")
ROUTE_DIRS = ("advance", "race_route", "event_choice", "speed_toggle")
DECISION_UNRELIABLE_FIELDS = {
    "stats_gain_partial",
    "stats_gain_unreliable",
    "local_template_incomplete",
    "hint_detection_unreliable",
}
DEFAULT_DASHBOARD_RUN_LIMIT = 20
DASHBOARD_FEATURED_RUN_LIMIT = 1
DASHBOARD_FEATURE_SCAN_LIMIT = 200
DASHBOARD_TURN_LOAD_LIMIT = 8


def build_dashboard_snapshot(
    runs_root: str | Path,
    *,
    workspace_root: str | Path,
    limit: int = DEFAULT_DASHBOARD_RUN_LIMIT,
) -> dict[str, Any]:
    root = Path(runs_root)
    workspace = Path(workspace_root)
    run_dirs = [p for p in root.iterdir() if p.is_dir()] if root.exists() else []
    run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    selected_run_dirs = list(run_dirs[:limit])
    featured = [
        path
        for path in run_dirs[limit : limit + DASHBOARD_FEATURE_SCAN_LIMIT]
        if _has_multiple_turn_records(path)
    ][:DASHBOARD_FEATURED_RUN_LIMIT]
    selected_run_dirs.extend(featured)
    runs = [
        summarize_run(path, workspace_root=workspace, turn_limit=DASHBOARD_TURN_LOAD_LIMIT)
        for path in selected_run_dirs
    ]
    return {
        "schema_version": "ug_parent_dashboard.v1",
        "runs_root": _rel(root, workspace),
        "run_count": len(runs),
        "total_run_count": len(run_dirs),
        "runs": runs,
    }


def _has_multiple_turn_records(path: Path) -> bool:
    candidates = path / "turns"
    search_root = candidates if candidates.is_dir() else path
    found = 0
    try:
        for child in search_root.iterdir():
            if not child.is_dir():
                continue
            if search_root == candidates:
                is_turn = child.name.startswith("turn_")
            else:
                is_turn = "_turn_" in child.name or child.name.startswith("turn_") or _looks_like_turn_dir(child)
            if not is_turn:
                continue
            found += 1
            if found >= 2:
                return True
    except OSError:
        return False
    return False


def summarize_run(
    run_dir: str | Path,
    *,
    workspace_root: str | Path,
    turn_limit: int | None = None,
) -> dict[str, Any]:
    path = Path(run_dir)
    workspace = Path(workspace_root)
    turn_dirs = [
        p
        for p in path.iterdir()
        if p.is_dir() and ("_turn_" in p.name or p.name.startswith("turn_") or _looks_like_turn_dir(p))
    ] if path.exists() else []
    live_loop_turns = path / "turns"
    if live_loop_turns.is_dir():
        turn_dirs.extend(p for p in live_loop_turns.iterdir() if p.is_dir() and p.name.startswith("turn_"))
    if path.exists() and not turn_dirs and _looks_like_turn_dir(path):
        turn_dirs = [path]
    turn_dirs.sort(key=_turn_sort_key)
    total_turn_count = len(turn_dirs)
    if turn_limit is not None:
        turn_dirs = turn_dirs[-max(0, turn_limit) :]
    turns = [summarize_turn(turn, workspace_root=workspace) for turn in turn_dirs]
    standalone = _summarize_standalone_run(path, workspace)
    recent_logs = _summarize_recent_logs(path)
    display_title, display_subtitle = _run_display_text(
        path.name,
        turns,
        standalone,
        recent_logs,
        total_turn_count=total_turn_count,
    )
    return {
        "run_id": path.name,
        "display_title": display_title,
        "display_subtitle": display_subtitle,
        "path": _rel(path, workspace),
        "last_modified": path.stat().st_mtime if path.exists() else None,
        "turn_count": total_turn_count,
        "loaded_turn_count": len(turns),
        "turns": turns,
        "standalone": standalone,
        "recent_logs": recent_logs,
    }


def _run_display_text(
    run_id: str,
    turns: list[dict[str, Any]],
    standalone: dict[str, Any],
    recent_logs: list[dict[str, Any]],
    total_turn_count: int | None = None,
) -> tuple[str, str]:
    if turns:
        latest = turns[-1]
        title = latest.get("display_title") or _calendar_display_title(latest.get("game_state") or {})
        record_count = total_turn_count if total_turn_count is not None else len(turns)
        if not title:
            title = f"训练运行 · {record_count}条记录"
        return title, f"{run_id} · {record_count}条记录"

    return _standalone_display_title(run_id, standalone, recent_logs), run_id


def _calendar_display_title(game_state: dict[str, Any]) -> str:
    period = _calendar_period_text(game_state.get("game_calendar")) or _calendar_period_text(
        game_state.get("period_text")
    )
    turn = _int_or_none(game_state.get("turn"))
    if turn is None:
        turn = _int_or_none(game_state.get("turn_index"))
    parts = []
    if period:
        parts.append(period)
    if turn is not None:
        parts.append(f"第{turn}回合")
    return " · ".join(parts)


def _calendar_period_text(value: Any) -> str:
    if isinstance(value, dict):
        return _calendar_period_from_dict(value)
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    formatted = _format_period_id(text)
    if formatted:
        return formatted
    if "_" in text and text.isascii():
        return ""
    return text


def _format_period_id(text: str) -> str:
    parts = text.split("_")
    if len(parts) < 2:
        return ""

    year_key = "_".join(parts[:2])
    year_label = {
        "junior_year": "初级级",
        "classic_year": "经典级",
        "senior_year": "高级级",
    }.get(year_key)
    if not year_label:
        return ""

    rest = parts[2:]
    month_label = ""
    if rest:
        month_label = _month_label(rest[0])
        if month_label:
            rest = rest[1:]

    half_label = {
        "early": "上半",
        "late": "下半",
        "first_half": "上半",
        "second_half": "下半",
    }.get("_".join(rest), "")

    date_label = f"{month_label}{half_label}" if month_label or half_label else ""
    return f"{year_label} {date_label}".strip()


def _month_label(value: str) -> str:
    if value.isdigit():
        month = int(value)
        return f"{month}月" if 1 <= month <= 12 else ""
    return {
        "january": "1月",
        "february": "2月",
        "march": "3月",
        "april": "4月",
        "may": "5月",
        "june": "6月",
        "july": "7月",
        "august": "8月",
        "september": "9月",
        "october": "10月",
        "november": "11月",
        "december": "12月",
    }.get(value, "")


def _calendar_period_from_dict(value: dict[str, Any]) -> str:
    for key in ("display_text", "label", "period_text", "period", "id"):
        text = _calendar_period_text(value.get(key))
        if text:
            return text

    year_label = _year_label(value.get("year") or value.get("grade") or value.get("class"))
    month = value.get("month")
    month_label = _month_label(str(month)) if month is not None else ""
    half_label = _half_label(value.get("half") or value.get("phase"))
    date_label = f"{month_label}{half_label}" if month_label or half_label else ""
    return f"{year_label} {date_label}".strip()


def _year_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return {
        "junior": "初级级",
        "junior_year": "初级级",
        "classic": "经典级",
        "classic_year": "经典级",
        "senior": "高级级",
        "senior_year": "高级级",
        "初级级": "初级级",
        "经典级": "经典级",
        "高级级": "高级级",
    }.get(text, text if not text.isascii() else "")


def _half_label(value: Any) -> str:
    text = str(value or "").strip()
    return {
        "early": "上半",
        "late": "下半",
        "first_half": "上半",
        "second_half": "下半",
        "first": "上半",
        "second": "下半",
        "upper": "上半",
        "lower": "下半",
        "上半": "上半",
        "下半": "下半",
        "上半月": "上半月",
        "下半月": "下半月",
    }.get(text, "")


def _standalone_display_title(
    run_id: str,
    standalone: dict[str, Any],
    recent_logs: list[dict[str, Any]],
) -> str:
    screenshot_count = _int_or_none(standalone.get("screenshot_count")) or 0
    if screenshot_count:
        evidence = f"{screenshot_count}张截图"
    else:
        evidence = f"{len(recent_logs)}条记录"
    return f"{_standalone_kind(run_id)} · {evidence}"


def _standalone_kind(run_id: str) -> str:
    name = run_id.lower()
    if "probe" in name:
        return "探针截图"
    if "safe_advance" in name:
        return "安全推进"
    if "race_route" in name or "target_race" in name:
        return "赛事路线"
    if "event_choice" in name:
        return "事件选择"
    if "speed_toggle" in name:
        return "加速确认"
    if any(token in name for token in ("supervised", "auto_trainer", "one_turn", "training")):
        return "训练运行"
    return "独立运行"


def summarize_turn(turn_dir: str | Path, *, workspace_root: str | Path) -> dict[str, Any]:
    path = Path(turn_dir)
    workspace = Path(workspace_root)
    game_state = _summarize_game_state(path)
    display_title = _calendar_display_title(game_state) or path.name
    return {
        "turn_id": path.name,
        "display_title": display_title,
        "display_subtitle": path.name,
        "path": _rel(path, workspace),
        "game_state": game_state,
        "capture": _summarize_capture(path, workspace),
        "decision": _summarize_decision(path),
        "execution": _summarize_execution(path),
        "routes": {name: _summarize_route(path / name, workspace) for name in ROUTE_DIRS},
        "current_screenshot": _current_screenshot(path, workspace),
        "task_steps": _task_steps(path, workspace),
    }


def _summarize_capture(turn_dir: Path, workspace: Path) -> dict[str, Any]:
    capture_dir = turn_dir / "capture"
    sidecar_path = capture_dir / "training_options.generated.json"
    sidecar = _load_json(sidecar_path)
    options = sidecar.get("training_options", []) if isinstance(sidecar, dict) else []
    option_summaries = [_summarize_training_option(option, capture_dir, workspace) for option in options]
    montage = capture_dir / "five_tabs_montage.jpg"
    status, status_reason = _capture_status(capture_dir, sidecar_path, sidecar, options)
    screenshots = _summarize_capture_screenshots(capture_dir, workspace)
    if not option_summaries:
        option_summaries = _summarize_raw_tab_options(capture_dir, workspace)
    return {
        "status": status,
        "status_reason": status_reason,
        "option_count": len(option_summaries),
        "options": option_summaries,
        "montage": _rel(montage, workspace) if montage.exists() else None,
        "screenshot_count": len(screenshots),
        "last_screenshot": screenshots[-1]["path"] if screenshots else None,
        "screenshots": screenshots[-12:],
        "state_context": _rel(capture_dir / "state_context.json", workspace)
        if (capture_dir / "state_context.json").exists()
        else None,
    }


def _summarize_raw_tab_options(capture_dir: Path, workspace: Path) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for training_type in TRAINING_TYPES:
        image_path = capture_dir / "raw_720" / f"{training_type}.png"
        if not image_path.exists():
            continue
        options.append(
            {
                "training_type": training_type,
                "failure_rate": None,
                "support_count": None,
                "hint_count": None,
                "fragment_color": None,
                "fragment_available": None,
                "fragment_state": None,
                "fragment_count": None,
                "fragment_inventory": None,
                "wisdom_effect_active": None,
                "prospective_fragment_output": None,
                "confidence": None,
                "base_gain": {},
                "bonus_gain": {},
                "stats_gain_total": {},
                "stats_gain": {},
                "decision_summary_status": "reliable",
                "decision_summary_label": "决策摘要",
                "decision_summary_reliable": True,
                "stats_gain_ocr": None,
                "support_slot_count": 0,
                "support_slots": [],
                "unknown_fields": ["training_options_sidecar_missing"],
                "image": _rel(image_path, workspace),
                "image_path": _rel(image_path, workspace),
                "image_source_name": image_path.name,
            }
        )
    return options


def _summarize_capture_screenshots(capture_dir: Path, workspace: Path) -> list[dict[str, Any]]:
    raw_dir = capture_dir / "raw_720"
    if not raw_dir.exists():
        return []
    screenshots = sorted(raw_dir.glob("*.png"), key=lambda path: (path.stat().st_mtime, path.name))
    return [_capture_screenshot_summary(path, workspace) for path in screenshots]


def _capture_screenshot_summary(path: Path, workspace: Path) -> dict[str, Any]:
    stem = path.stem
    training_type = stem.split(".", 1)[0]
    kind = "attempt" if ".attempt-" in path.name else "final"
    return {
        "path": _rel(path, workspace),
        "name": path.name,
        "training_type": training_type if training_type in TRAINING_TYPES else None,
        "kind": kind,
        "label": _capture_screenshot_label(path.name, training_type, kind),
        "last_modified": path.stat().st_mtime,
    }


def _capture_screenshot_label(name: str, training_type: str, kind: str) -> str:
    training_label = {
        "speed": "速度",
        "stamina": "耐力",
        "power": "力量",
        "guts": "根性",
        "wisdom": "智力",
    }.get(training_type, training_type or name)
    if kind == "attempt":
        attempt = ""
        for part in name.split("."):
            if part.startswith("attempt-"):
                attempt = part.replace("attempt-", "尝试")
                break
        return f"{training_label} {attempt}".strip()
    return f"{training_label} 完成图".strip()


def _summarize_training_option(option: dict[str, Any], capture_dir: Path, workspace: Path) -> dict[str, Any]:
    raw_capture = _get(option, "raw_data", "capture") or _get(option, "capture") or {}
    training_type = option.get("training_type")
    image_path = _resolve_training_option_image(training_type, raw_capture, capture_dir)
    unknown_fields = option.get("unknown_fields") or []
    if not isinstance(unknown_fields, list):
        unknown_fields = []
    raw_support_slots = option.get("support_slots", [])
    support_slots = _summarize_support_slots(raw_support_slots)
    red_dot_count = _reliable_count(
        option.get("red_dot_count", option.get("hint_count")),
        unknown_fields,
        {"hint_detection_unreliable", "hint_count_unreliable"},
    )
    identity_candidates = _collect_identity_candidates(raw_support_slots)
    support_identity_summary = _support_identity_summary(support_slots)
    image = _rel(image_path, workspace) if image_path and image_path.exists() else None
    grand_masters = _get(option, "scenario_mechanics", "grand_masters")
    if not isinstance(grand_masters, dict):
        grand_masters = {}
    return {
        "training_type": training_type,
        "failure_rate": _reliable_failure_rate(option.get("failure_rate"), unknown_fields),
        "support_count": _reliable_count(
            option.get("support_count"),
            unknown_fields,
            {"support_detection_unreliable", "support_count_unreliable"},
        ),
        "visible_portrait_count": _reliable_count(
            option.get("visible_portrait_count"),
            unknown_fields,
            set(),
        ),
        "effective_support_count": _reliable_count(
            option.get("effective_support_count"),
            unknown_fields,
            set(),
        ),
        "non_support_npc_count": _reliable_count(
            option.get("non_support_npc_count"),
            unknown_fields,
            set(),
        ),
        "unresolved_portrait_count": _reliable_count(
            option.get("unresolved_portrait_count"),
            unknown_fields,
            set(),
        ),
        "hint_count": _reliable_count(
            option.get("hint_count"),
            unknown_fields,
            {"hint_detection_unreliable", "hint_count_unreliable"},
        ),
        "red_dot_count": red_dot_count,
        "support_right_lane_shadow": _summarize_support_right_lane_shadow(
            option.get("support_right_lane_shadow")
        ),
        "identity_candidates": identity_candidates,
        "support_identity_summary": support_identity_summary,
        "support_slot_summary": {
            "count": len(support_slots),
            "red_dot_count": red_dot_count,
            "identity_candidates": identity_candidates,
            "identity_summary": support_identity_summary,
        },
        "fragment_color": option.get("fragment_color"),
        "fragment_available": option.get("fragment_available"),
        "fragment_state": option.get("fragment_state"),
        "fragment_count": option.get("fragment_count"),
        "fragment_inventory": _dict_or_empty(grand_masters.get("fragment_inventory")),
        "wisdom_effect_active": grand_masters.get("wisdom_effect_active"),
        "prospective_fragment_output": _dict_or_empty(grand_masters.get("prospective_fragment_output")),
        "confidence": option.get("confidence"),
        "base_gain": _dict_or_empty(option.get("base_gain")),
        "bonus_gain": _dict_or_empty(option.get("bonus_gain")),
        "fragment_or_scenario_gain": _dict_or_empty(option.get("fragment_or_scenario_gain")),
        "stats_gain_total": _dict_or_empty(option.get("stats_gain_total")),
        "total_gain": _dict_or_empty(option.get("total_gain")),
        "gain_breakdown": _dict_or_empty(option.get("gain_breakdown")),
        "stats_gain": _reliable_stats_gain(option.get("stats_gain"), unknown_fields),
        "decision_summary_status": _decision_summary_status(unknown_fields),
        "decision_summary_label": _decision_summary_label(unknown_fields),
        "decision_summary_reliable": _decision_summary_status(unknown_fields) == "reliable",
        "stats_gain_ocr": _summarize_stats_gain_ocr(option.get("stats_gain_ocr")),
        "support_slot_count": len(support_slots),
        "support_slots": support_slots,
        "unknown_fields": unknown_fields,
        "image": image,
        "image_path": image,
        "image_source_name": image_path.name if image_path and image_path.exists() else None,
    }


def _reliable_failure_rate(value: Any, unknown_fields: list[Any]) -> int | None:
    if any(field in unknown_fields for field in ("failure_rate", "failure_rate_unreliable")):
        return None
    if not isinstance(value, int):
        return None
    if value < 0 or value > 100:
        return None
    return value


def _reliable_count(value: Any, unknown_fields: list[Any], unreliable_fields: set[str]) -> int | None:
    if any(field in unknown_fields for field in unreliable_fields):
        return None
    if not isinstance(value, int):
        return None
    if value < 0 or value > 6:
        return None
    return value


def _summarize_support_right_lane_shadow(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    if value.get("schema_version") != "support_right_lane_shadow_sidecar.v1":
        return {}
    if value.get("production_ready") is not False:
        return {}
    status = value.get("status")
    reason = value.get("reason")
    if status not in {"accepted", "abstained"}:
        return {}
    if not isinstance(reason, str) or not reason:
        return {}
    source_sha256 = value.get("source_sha256")
    if source_sha256 is not None and not (
        isinstance(source_sha256, str)
        and len(source_sha256) == 64
        and all(character in "0123456789abcdef" for character in source_sha256.lower())
    ):
        return {}
    heads = value.get("visible_portrait_count")
    hints = value.get("verified_red_exclamation_count")
    support_match = value.get("primary_support_count_match")
    hint_match = value.get("primary_red_dot_count_match")
    if status == "accepted":
        if value.get("recognizer_schema_version") != "support_right_lane_count_shadow.v1":
            return {}
        if not (
            type(heads) is int
            and 0 <= heads <= 6
            and type(hints) is int
            and 0 <= hints <= heads
            and (support_match is None or type(support_match) is bool)
            and (hint_match is None or type(hint_match) is bool)
            and source_sha256 is not None
        ):
            return {}
    elif any(
        item is not None for item in (heads, hints, support_match, hint_match)
    ):
        return {}
    allowed_fields = (
        "schema_version",
        "recognizer_schema_version",
        "status",
        "reason",
        "source_sha256",
        "visible_portrait_count",
        "verified_red_exclamation_count",
        "primary_support_count_match",
        "primary_red_dot_count_match",
        "production_ready",
    )
    return {key: value.get(key) for key in allowed_fields}


def _reliable_stats_gain(value: Any, unknown_fields: list[Any]) -> dict[str, Any]:
    unreliable_fields = {
        "local_template_incomplete",
        "local_ocr_value_unreliable",
        "stats_gain_unreliable",
    }
    if any(field in unknown_fields for field in unreliable_fields):
        return {}
    return value if isinstance(value, dict) else {}


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _decision_summary_status(unknown_fields: list[Any]) -> str:
    fields = {str(field) for field in unknown_fields}
    return "partial" if fields & DECISION_UNRELIABLE_FIELDS else "reliable"


def _decision_summary_label(unknown_fields: list[Any]) -> str:
    if _decision_summary_status(unknown_fields) == "partial":
        return "部分摘要 · 不计入可靠收益评分 · 仅供核对"
    return "决策摘要"


def _summarize_stats_gain_ocr(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    columns = value.get("columns")
    if isinstance(columns, list):
        summarized_columns = []
        for column in columns:
            if not isinstance(column, dict):
                continue
            summarized_columns.append(
                {
                    "attribute_id": column.get("attribute_id"),
                    "roi": column.get("roi"),
                    "value": column.get("value"),
                    "sum": column.get("sum"),
                    "accepted_value": column.get("accepted_value"),
                    "score": column.get("score"),
                    "status": column.get("status"),
                    "reason": column.get("reason"),
                    "candidates": column.get("candidates") if isinstance(column.get("candidates"), list) else [],
                }
            )
        copy = dict(value)
        copy["columns"] = summarized_columns
        return copy
    return dict(value)


def _summarize_support_slots(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    summarized: list[dict[str, Any]] = []
    for slot in value:
        if not isinstance(slot, dict):
            continue
        bond_bar = slot.get("bond_bar")
        bond_status = bond_bar.get("status") if isinstance(bond_bar, dict) else None
        identity = _summarize_support_identity(slot)
        identity_status = slot.get("identity_status")
        display_name = slot.get("identity_display_name")
        card_id = slot.get("card_id")
        candidate_name = _support_identity_candidate_name(slot)
        annotation_status = _support_identity_annotation_status(slot)
        summarized.append(
            {
                "slot": slot.get("slot"),
                "hint_badge": bool(slot.get("hint_badge")),
                "bond_status": bond_status,
                "bond_bar": _summarize_support_bond_bar(bond_bar),
                "character_id": slot.get("character_id"),
                "card_id": slot.get("card_id"),
                "matched_card_id": slot.get("matched_card_id"),
                "entity_kind": slot.get("entity_kind"),
                "is_effective_support": slot.get("is_effective_support"),
                "identity": identity,
                "identity_status": identity_status,
                "identity_state": slot.get("identity_state"),
                "identity_display_name": display_name,
                "identity_card_type": slot.get("identity_card_type"),
                "identity_candidate_name": candidate_name,
                "identity_annotation_status": annotation_status,
                "identity_annotation_label": _support_identity_annotation_label(
                    candidate_name=candidate_name,
                    annotation_status=annotation_status,
                    card_id=card_id,
                ),
                "identity_candidates": slot.get("identity_candidates") if isinstance(slot.get("identity_candidates"), list) else [],
                "confidence": slot.get("confidence"),
                "source": slot.get("source"),
            }
        )
    return summarized


def _summarize_support_identity(slot: dict[str, Any]) -> dict[str, Any]:
    raw_identity = slot.get("identity")
    identity = raw_identity if isinstance(raw_identity, dict) else {}
    values = {
        "matched_card_id": identity.get("matched_card_id", slot.get("matched_card_id")),
        "display_name": identity.get("display_name", slot.get("identity_display_name")),
        "status": identity.get("status", slot.get("identity_status")),
        "confidence": identity.get("confidence", slot.get("identity_confidence")),
    }
    return {key: value for key, value in values.items() if value is not None and value != ""}


def _summarize_support_bond_bar(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    if value.get("schema_version") != "bond_bar_measurement.v1":
        return {}
    summarized = {
        key: value[key]
        for key in (
            "schema_version",
            "measurement_status",
            "source",
            "fill_ratio_estimate",
            "bond_band",
            "friendship_ready",
            "confidence",
            "colored_area_ratio_shadow",
        )
        if key in value
    }
    evidence = value.get("evidence")
    if isinstance(evidence, dict):
        rejection_reasons = evidence.get("rejection_reasons")
        if isinstance(rejection_reasons, list):
            summarized["evidence"] = {
                "rejection_reasons": [
                    reason for reason in rejection_reasons if isinstance(reason, str)
                ]
            }
    return summarized


def _support_identity_summary(support_slots: list[dict[str, Any]]) -> dict[str, Any]:
    pending = [slot for slot in support_slots if slot.get("identity_annotation_status") == "pending"]
    known = [slot for slot in support_slots if slot.get("identity_annotation_status") == "known"]
    labels = [
        str(slot.get("identity_annotation_label"))
        for slot in support_slots
        if slot.get("identity_annotation_label")
    ]
    return {
        "slot_count": len(support_slots),
        "known_count": len(known),
        "pending_annotation_count": len(pending),
        "labels": labels,
    }


def _support_identity_annotation_status(slot: dict[str, Any]) -> str:
    if slot.get("identity_status") == "known" or slot.get("matched_card_id"):
        return "known"
    candidate_name = _support_identity_candidate_name(slot)
    return "pending" if candidate_name else "unknown"


def _support_identity_candidate_name(slot: dict[str, Any]) -> str:
    name = slot.get("identity_display_name")
    if isinstance(name, str) and name:
        return name
    candidates = slot.get("identity_candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            candidate_name = candidate.get("display_name") or candidate.get("card_name")
            if candidate_name:
                return str(candidate_name)
            candidate_id = candidate.get("card_id")
            if candidate_id:
                return str(candidate_id)
    card_id = slot.get("card_id")
    return str(card_id) if card_id else ""


def _support_identity_annotation_label(
    *,
    candidate_name: str,
    annotation_status: str,
    card_id: Any,
) -> str:
    if annotation_status == "known":
        return f"候选身份: {candidate_name or card_id or '-'}"
    if annotation_status == "pending":
        return f"候选身份: {candidate_name or card_id or '-'} / 待标注"
    return "候选身份: 未识别 / 待标注"


def _collect_identity_candidates(support_slots: Any) -> list[dict[str, Any]]:
    if not isinstance(support_slots, list):
        return []
    candidates: list[dict[str, Any]] = []
    for slot_index, slot in enumerate(support_slots):
        if not isinstance(slot, dict):
            continue
        slot_number = _int_or_none(slot.get("slot"))
        if slot_number is None:
            slot_number = slot_index
        raw_candidates = slot.get("identity_candidates")
        if isinstance(raw_candidates, list) and raw_candidates:
            for raw_candidate in raw_candidates:
                if not isinstance(raw_candidate, dict):
                    continue
                candidate = dict(raw_candidate)
                candidate["slot"] = slot_number
                candidates.append(candidate)
            continue

        identity = slot.get("identity")
        if not isinstance(identity, dict):
            identity = {}
        candidates.append(
            {
                "slot": slot_number,
                "card_id": slot.get("card_id"),
                "matched_card_id": slot.get("matched_card_id"),
                "character_id": slot.get("character_id", "unknown"),
                "display_name": slot.get("identity_display_name", ""),
                "type": slot.get("identity_card_type", "unknown"),
                "confidence": identity.get("confidence", slot.get("confidence", 0.0)),
                "status": identity.get("status", slot.get("identity_status", "unknown")),
                "identity_state": identity.get("identity_state", slot.get("identity_state", "unknown_identity")),
            }
        )
    return candidates


def _summarize_game_state(turn_dir: Path) -> dict[str, Any]:
    state = _load_json(turn_dir / "capture" / "state_context.json")
    if not isinstance(state, dict):
        state = {}
    sidecar = _load_json(turn_dir / "capture" / "training_options.generated.json")
    if not isinstance(sidecar, dict):
        sidecar = {}
    manifest = _load_json(turn_dir / "capture" / "execution_manifest.json")
    if not isinstance(manifest, dict):
        manifest = {}
    manifest_group = _first_manifest_group(manifest)
    scenario = state.get("scenario_state") if isinstance(state.get("scenario_state"), dict) else {}
    current_stats = state.get("current_stats") if isinstance(state.get("current_stats"), dict) else {}
    turn_index = _int_or_none(state.get("turn_index"))
    if turn_index is None:
        turn_index = _int_or_none(state.get("current_turn"))
    if turn_index is None:
        turn_index = _int_or_none(state.get("turn"))
    if turn_index is None:
        turn_index = _int_or_none(sidecar.get("turn_index"))
    if turn_index is None:
        turn_index = _int_or_none(_get(sidecar, "state_context", "turn_index"))
    if turn_index is None:
        turn_index = _int_or_none(manifest_group.get("turn_index"))
    if turn_index is None:
        turn_index = _turn_index_from_name(turn_dir.name)
    game_calendar = (
        state.get("game_calendar")
        or state.get("calendar")
        or scenario.get("game_calendar")
        or sidecar.get("game_calendar")
        or _get(sidecar, "state_context", "game_calendar")
        or manifest_group.get("game_calendar")
    )
    period_text = (
        _calendar_period_text(game_calendar)
        or _calendar_period_text(state.get("period_text") or state.get("period"))
        or _calendar_period_text(scenario.get("period_text") or scenario.get("period"))
        or _calendar_period_text(sidecar.get("period_text") or sidecar.get("period"))
        or _calendar_period_text(_get(sidecar, "state_context", "period_text"))
        or _calendar_period_text(manifest_group.get("period_text") or manifest_group.get("period"))
    )
    return {
        "screen": state.get("screen"),
        "turn": _int_or_none(state.get("turn")),
        "turn_index": turn_index,
        "period_text": period_text,
        "game_calendar": game_calendar,
        "action": (
            state.get("action")
            or sidecar.get("action")
            or _get(sidecar, "decision", "action")
            or manifest_group.get("action")
        ),
        "vitality": _int_or_none(state.get("vitality")),
        "mood": scenario.get("mood"),
        "fragment_count": _int_or_none(scenario.get("fragment_count")),
        "fragment_state": scenario.get("fragment_state"),
        "current_stats": dict(current_stats),
    }


def _summarize_decision(turn_dir: Path) -> dict[str, Any]:
    data = _load_first_json(
        turn_dir / "decision" / "turns" / "turn_000" / "decision.json",
        turn_dir / "decision.json",
    )
    if not isinstance(data, dict):
        return {}
    candidates = []
    for item in data.get("scored_candidates", [])[:10]:
        if not isinstance(item, dict):
            continue
        raw = item.get("raw_candidate") if isinstance(item.get("raw_candidate"), dict) else {}
        candidates.append(
            {
                "action_id": item.get("action_id"),
                "training_type": item.get("training_type") or raw.get("training_type"),
                "action": item.get("action") or raw.get("action"),
                "target": item.get("target"),
                "score": item.get("score"),
                "rejected": item.get("rejected"),
                "rejected_reason": _candidate_rejected_reason(item),
                "risk_flags": item.get("risk_flags") or [],
                "failure_rate": raw.get("failure_rate"),
            }
        )
    return {
        "action": data.get("action"),
        "target": data.get("target"),
        "confidence": data.get("confidence"),
        "reason": data.get("reason"),
        "risk_flags": data.get("risk_flags") or [],
        "scored_candidates": candidates,
    }


def _summarize_execution(turn_dir: Path) -> dict[str, Any]:
    data = _load_first_json(
        turn_dir / "decision" / "turns" / "turn_000" / "execution.json",
        turn_dir / "execution.json",
    )
    if not isinstance(data, dict):
        return {}
    return {
        "performed": data.get("performed"),
        "target": data.get("target"),
        "reason": data.get("reason"),
        "tap_plan": data.get("tap_plan") or [],
        "screen_size": data.get("screen_size"),
        "coordinate_reference_size": data.get("coordinate_reference_size"),
        "safety_checks": _summarize_safety_checks(data.get("safety_checks") or [], workspace=None),
        "safety_check_count": len(data.get("safety_checks") or []),
    }


def _summarize_route(route_dir: Path, workspace: Path) -> dict[str, Any]:
    if not route_dir.exists():
        return {"screenshot_count": 0, "last_screenshot": None}
    screenshots = sorted(route_dir.glob("*.png"))
    return {
        "screenshot_count": len(screenshots),
        "last_screenshot": _rel(screenshots[-1], workspace) if screenshots else None,
        "screenshots": [_rel(path, workspace) for path in screenshots[-8:]],
    }


def _summarize_standalone_run(run_dir: Path, workspace: Path) -> dict[str, Any]:
    if not run_dir.exists():
        return {"screenshot_count": 0, "last_screenshot": None, "screenshots": []}
    screenshots = sorted(run_dir.glob("*.png"))
    return {
        "screenshot_count": len(screenshots),
        "last_screenshot": _rel(screenshots[-1], workspace) if screenshots else None,
        "screenshots": [_rel(path, workspace) for path in screenshots[-12:]],
    }


def _capture_status(
    capture_dir: Path,
    sidecar_path: Path,
    sidecar: Any,
    options: Any,
) -> tuple[str, str]:
    if not capture_dir.exists():
        return "missing_capture", "capture directory not found"
    if not sidecar_path.exists():
        return "missing_sidecar", "training_options.generated.json not found"
    if not isinstance(sidecar, dict):
        return "invalid_sidecar", "training_options.generated.json could not be parsed"
    if not isinstance(options, list):
        return "invalid_sidecar", "training_options is not a list"
    if not options:
        return "empty", "training_options is empty"
    return "ok", ""


def _candidate_rejected_reason(item: dict[str, Any]) -> str | None:
    reason = item.get("rejected_reason") or item.get("reject_reason")
    if reason:
        return str(reason)
    if item.get("rejected") and item.get("reason"):
        return str(item.get("reason"))
    return None


def _resolve_capture_image(raw_capture: dict[str, Any], capture_dir: Path) -> Path | None:
    absolute = raw_capture.get("absolute_path")
    if absolute:
        return Path(absolute)
    relative = raw_capture.get("relative_path")
    if relative:
        return capture_dir / relative
    tab = raw_capture.get("tab")
    if tab in TRAINING_TYPES:
        return capture_dir / "raw_720" / f"{tab}.png"
    return None


def _resolve_training_option_image(training_type: Any, raw_capture: dict[str, Any], capture_dir: Path) -> Path | None:
    if training_type in TRAINING_TYPES:
        canonical = capture_dir / "raw_720" / f"{training_type}.png"
        if canonical.exists():
            return canonical
    resolved = _resolve_capture_image(raw_capture, capture_dir)
    if _image_belongs_to_other_training_type(resolved, training_type):
        return None
    return resolved


def _image_belongs_to_other_training_type(path: Path | None, training_type: Any) -> bool:
    if path is None or training_type not in TRAINING_TYPES:
        return False
    stem = path.stem.split(".", 1)[0]
    return stem in TRAINING_TYPES and stem != training_type


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return json.loads(path.read_text(encoding="utf-16"))
    except (json.JSONDecodeError, OSError):
        return None


def _load_first_json(*paths: Path) -> Any:
    for path in paths:
        data = _load_json(path)
        if data is not None:
            return data
    return None


def _first_manifest_group(manifest: dict[str, Any]) -> dict[str, Any]:
    groups = manifest.get("groups")
    if isinstance(groups, list) and groups and isinstance(groups[0], dict):
        return groups[0]
    return {}


def _current_screenshot(turn_dir: Path, workspace: Path) -> str | None:
    for route_name in reversed(ROUTE_DIRS):
        route = _summarize_route(turn_dir / route_name, workspace)
        if route.get("last_screenshot"):
            return route["last_screenshot"]
    execution = _load_first_json(
        turn_dir / "decision" / "turns" / "turn_000" / "execution.json",
        turn_dir / "execution.json",
    )
    if isinstance(execution, dict):
        checks = execution.get("safety_checks") or []
        for check in reversed(checks):
            image = check.get("image_path") if isinstance(check, dict) else None
            if image and Path(image).exists():
                return _rel(Path(image), workspace)
    montage = turn_dir / "capture" / "five_tabs_montage.jpg"
    if montage.exists():
        return _rel(montage, workspace)
    capture = _summarize_capture(turn_dir, workspace)
    if capture.get("last_screenshot"):
        return capture["last_screenshot"]
    return None


def _task_steps(turn_dir: Path, workspace: Path) -> list[dict[str, Any]]:
    capture = _summarize_capture(turn_dir, workspace)
    decision = _summarize_decision(turn_dir)
    execution = _summarize_execution(turn_dir)
    routes = {name: _summarize_route(turn_dir / name, workspace) for name in ROUTE_DIRS}
    steps: list[dict[str, Any]] = [
        {
            "phase": "capture",
            "label": "采集训练 Tab",
            "status": "ok" if capture.get("status") == "ok" else "warn",
            "reason": _capture_step_reason(capture),
            "screenshot": capture.get("montage") or capture.get("last_screenshot"),
        },
        {
            "phase": "recognize",
            "label": "识别训练收益",
            "status": "ok" if capture.get("option_count") else "warn",
            "reason": f"{capture.get('option_count', 0)} 个候选",
            "screenshot": capture.get("montage") or capture.get("last_screenshot"),
        },
        {
            "phase": "decide",
            "label": "AI 决策",
            "status": "ok" if decision.get("action") else "pending",
            "reason": decision.get("reason") or "-",
            "next_action": {
                "action": decision.get("action"),
                "target": decision.get("target"),
                "confidence": decision.get("confidence"),
            },
        },
        {
            "phase": "execute",
            "label": "点击执行",
            "status": "ok" if execution.get("performed") else "pending",
            "reason": execution.get("reason") or "-",
            "next_action": _first_tap(execution.get("tap_plan") or []),
            "recognition": _last_safety_check(execution.get("safety_checks") or []),
        },
    ]
    for name, route in routes.items():
        if route.get("screenshot_count"):
            steps.append(
                {
                    "phase": name,
                    "label": _route_label(name),
                    "status": "ok",
                    "reason": f"{route.get('screenshot_count', 0)} 张截图",
                    "screenshot": route.get("last_screenshot"),
                }
            )
    return steps


def _capture_step_reason(capture: dict[str, Any]) -> str:
    reason = capture.get("status_reason") or f"{capture.get('option_count', 0)} training options"
    screenshot_count = capture.get("screenshot_count") or 0
    if screenshot_count:
        return f"{reason} · {screenshot_count}张截图"
    return reason


def _summarize_safety_checks(checks: Any, workspace: Path | None) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    if not isinstance(checks, list):
        return summaries
    for check in checks:
        if not isinstance(check, dict):
            continue
        evidence = check.get("evidence") if isinstance(check.get("evidence"), dict) else {}
        image = check.get("image_path")
        if workspace is not None and image and Path(str(image)).exists():
            image = _rel(Path(str(image)), workspace)
        summaries.append(
            {
                "label": check.get("label"),
                "accepted": check.get("accepted"),
                "reason": check.get("reason"),
                "selected_tab": check.get("selected_tab"),
                "failures": check.get("failures") or [],
                "image_path": image,
                "perception": evidence.get("perception") if isinstance(evidence, dict) else None,
                "selected_tab_detection": (
                    evidence.get("selected_tab_detection") if isinstance(evidence, dict) else None
                ),
            }
        )
    return summaries


def _first_tap(tap_plan: Any) -> dict[str, Any] | None:
    if not isinstance(tap_plan, list) or not tap_plan:
        return None
    tap = tap_plan[0]
    return tap if isinstance(tap, dict) else None


def _last_safety_check(checks: Any) -> dict[str, Any] | None:
    if not isinstance(checks, list) or not checks:
        return None
    check = checks[-1]
    return check if isinstance(check, dict) else None


def _route_label(name: str) -> str:
    return {
        "advance": "安全推进",
        "race_route": "比赛路线",
        "event_choice": "事件选择",
        "speed_toggle": "加速确认",
    }.get(name, name)


def _summarize_recent_logs(run_dir: Path, *, max_lines: int = 80) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    if not run_dir.exists():
        return logs
    patterns = ("*_stdout.log", "*_stderr.log", "*.log")
    seen: set[Path] = set()
    for pattern in patterns:
        for path in sorted(run_dir.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            logs.append(
                {
                    "name": path.name,
                    "lines": lines[-max_lines:],
                    "line_count": len(lines),
                }
            )
            if len(logs) >= 4:
                return logs
    return logs


def _turn_sort_key(path: Path) -> tuple[int, str]:
    marker = "_turn_"
    if marker in path.name:
        tail = path.name.rsplit(marker, 1)[-1]
        if tail.isdigit():
            return int(tail), path.name
    if path.name.startswith("turn_"):
        tail = path.name.rsplit("_", 1)[-1]
        if tail.isdigit():
            return int(tail), path.name
    return 999999, path.name


def _turn_index_from_name(name: str) -> int | None:
    if name.startswith("turn_"):
        tail = name.rsplit("_", 1)[-1]
        return int(tail) if tail.isdigit() else None
    marker = "_turn_"
    if marker in name:
        tail = name.rsplit(marker, 1)[-1]
        return int(tail) if tail.isdigit() else None
    return None


def _looks_like_turn_dir(path: Path) -> bool:
    return (
        (path / "capture").is_dir()
        or (path / "decision" / "turns").is_dir()
        or (path / "decision.json").is_file()
        or (path / "execution.json").is_file()
    )


def _rel(path: Path, workspace: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace.resolve()))
    except ValueError:
        return str(path)


def _get(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return None
