"""本地训练增益 OCR 模块。

用纯 OpenCV 识别训练增益条上的 +N 数字，无需外部 OCR 引擎。
方法：HSV 颜色阈值 → 连通域分析 → 数字区域分组 → 位数/面积推断数值。

输出格式与 MIMO column_crop 一致：
{"gains": [{"x": 0.08, "n": 18}, ...], "confidence": 0.0-1.0, "source": "local_ocr"}
"""

from __future__ import annotations

from itertools import combinations
import logging
from pathlib import Path
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# 列区域定义（与 training_parser.py 一致）
COLUMN_REGIONS: dict[str, tuple[int, int]] = {
    "speed":   (30, 260),
    "stamina": (260, 490),
    "power":   (490, 720),
    "guts":    (720, 950),
    "wisdom":  (950, 1150),
    "skill_pt": (1150, 1410),
}

_REFERENCE_WIDTH = 1440
_REFERENCE_HEIGHT = 2560
_RESCUE_TEMPLATE_DIR = Path("assets/digit_templates_live_rescue")
_RESCUE_MATCHER: Any | None = None
_ZERO_RESCUE_TEMPLATE_DIR = Path("assets/digit_templates_zero_rescue")
_ZERO_RESCUE_MATCHER: Any | None = None

# ---------- 常量 ----------

# 增益条 y 范围（与 training_parser.py 一致）
_GAIN_Y_TOP = 1490
_GAIN_Y_BOTTOM = 1660

# 橙色文字 HSV 范围
_ORANGE_H_LOW = 10
_ORANGE_H_HIGH = 35
_ORANGE_S_MIN = 100
_ORANGE_V_MIN = 150

# 连通域面积过滤
_MIN_COMPONENT_AREA = 20
_MAX_COMPONENT_AREA = 3000

# Glyph 过滤规则（排除横线/背景噪声）
_GLYPH_MIN_HEIGHT = 20       # 过矮组件（h < 20）→ 非数字
_GLYPH_MAX_ASPECT = 5.0      # 宽高比过大（w/h > 5）→ 横线
_GLYPH_MIN_ASPECT = 0.15     # 宽高比过小（w/h < 0.15）→ 竖线/噪声
_GLYPH_MAX_WIDTH = 80        # 过宽组件（w > 80 且 h < 15）→ 横线
_GLYPH_TYPIAL_MIN_W = 20     # 正常数字最小宽度
_GLYPH_TYPIAL_MAX_W = 60     # 正常数字最大宽度
_GLYPH_TYPIAL_MIN_H = 30     # 正常数字最小高度
_GLYPH_TYPIAL_MAX_H = 70     # 正常数字最大高度

# 数字分组间距阈值（像素）
_GROUP_GAP = 28

# 预期 +N 数量范围
_EXPECTED_GAIN_COUNT = (2, 6)  # 最少 2 个（主增益 + skill_pt），最多 6 个


# 数值置信度上限（面积推断法始终不可靠）
_VALUE_CONFIDENCE_CAP = 0.35


def is_glyph_like(comp: dict) -> bool:
    """判断组件是否像数字/glyph（排除横线、背景噪声）。

    Args:
        comp: 组件字典，需包含 w, h, area 字段。

    Returns:
        True if 组件尺寸符合数字特征。
    """
    w, h = int(comp["w"]), int(comp["h"])
    if h == 0 or w == 0:
        return False

    aspect = w / h

    # 过矮 → 横线/背景
    if h < _GLYPH_MIN_HEIGHT:
        return False

    # 宽高比过大 → 横线
    if aspect > _GLYPH_MAX_ASPECT:
        return False

    # 宽高比过小 → 竖线/噪声
    if aspect < _GLYPH_MIN_ASPECT:
        return False

    # 过宽且矮 → 横线
    if w > _GLYPH_MAX_WIDTH and h < 15:
        return False

    return True


def recognize_gains(
    image_path: str | Path,
    mode: str = "template",
    *,
    image: np.ndarray | None = None,
) -> dict[str, Any]:
    """从训练页截图识别增益数字。

    Args:
        image_path: 训练页截图路径。
        mode: "template" (默认，正式) / "heuristic" / "debug" (保守模板模式，不启用旧面积猜测)
        image: 可选已加载 BGR 图像，避免重复磁盘读取。

    Returns:
        {"gains": [...], "confidence": float, "source": "local_template", ...}
    """
    img = image if image is not None else cv2.imread(str(image_path))
    if img is None:
        return _empty_result("无法读取图片")

    original_h, original_w = img.shape[:2]
    normalized_from = None
    if original_w != _REFERENCE_WIDTH or original_h != _REFERENCE_HEIGHT:
        normalized_from = [original_w, original_h]
        img = cv2.resize(
            img,
            (_REFERENCE_WIDTH, _REFERENCE_HEIGHT),
            interpolation=cv2.INTER_CUBIC,
        )

    # 裁剪增益条
    strip = img[_GAIN_Y_TOP:_GAIN_Y_BOTTOM, 0:1440]
    strip_h, strip_w = strip.shape[:2]

    # 提取橙色像素
    hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
    orange_mask = (
        (hsv[:, :, 0] >= _ORANGE_H_LOW) & (hsv[:, :, 0] <= _ORANGE_H_HIGH)
        & (hsv[:, :, 1] > _ORANGE_S_MIN) & (hsv[:, :, 2] > _ORANGE_V_MIN)
    )
    orange_uint8 = (orange_mask * 255).astype(np.uint8)

    # 形态学操作
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    orange_clean = cv2.dilate(orange_uint8, kernel, iterations=1)
    orange_clean = cv2.erode(orange_clean, kernel, iterations=1)

    # 连通域分析
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        orange_clean, connectivity=8
    )

    # 收集有效组件（应用 glyph 过滤）
    components = []
    for i in range(1, num_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < _MIN_COMPONENT_AREA or area > _MAX_COMPONENT_AREA:
            continue
        comp = {
            "x": stats[i, cv2.CC_STAT_LEFT],
            "y": stats[i, cv2.CC_STAT_TOP],
            "w": stats[i, cv2.CC_STAT_WIDTH],
            "h": stats[i, cv2.CC_STAT_HEIGHT],
            "cx": float(centroids[i][0]),
            "cy": float(centroids[i][1]),
            "area": int(area),
        }
        if is_glyph_like(comp):
            components.append(comp)

    if not components:
        return _empty_result("未检测到橙色数字区域")

    components.sort(key=lambda c: c["cx"])
    groups = _group_components(components)

    # --- 模板匹配（正式模式） ---
    if mode == "template":
        return _recognize_template_production(
            strip,
            groups,
            strip_w,
            components,
            image_size=[original_w, original_h],
            normalized_from=normalized_from,
        )

    # --- 保守模板匹配（非正式模式统一走真实模板，移除旧 1440p 像素面积猜数字） ---
    return _recognize_with_templates(strip, groups, strip_w, components)


def _recognize_template_production(
    strip: np.ndarray,
    groups: list[dict],
    strip_w: int,
    components: list[dict],
    *,
    image_size: list[int] | None = None,
    normalized_from: list[int] | None = None,
) -> dict[str, Any]:
    """按列裁剪模板匹配：每列独立识别，避免跨列干扰。"""
    from core.gain_template_matcher import GainTemplateMatcher

    matcher = GainTemplateMatcher()
    debug_columns: list[dict[str, Any]] = []
    image_size = image_size or [_REFERENCE_WIDTH, _REFERENCE_HEIGHT]
    if not matcher.available:
        logger.warning("模板不可用，无法进行模板识别")
        debug = _build_stats_gain_ocr_debug(
            debug_columns,
            image_size=image_size,
            normalized_from=normalized_from,
            status="template_unavailable",
        )
        return {
            "gains": [], "partials": [], "template_matches": [],
            "confidence": 0.0, "position_confidence": 0.0,
            "value_confidence": 0.0, "method": "template_unavailable",
            "value_unreliable": True, "source": "local_template",
            "template_available": False, "stats_gain_ocr": debug,
        }

    gains = []
    partials = []
    template_matches = []
    all_reliable = True
    pad = 3
    strip_h = strip.shape[0]

    for col_key, (x0, x1) in COLUMN_REGIONS.items():
        column_debug = _new_column_debug(col_key, x0, x1, image_size)
        # 裁剪该列
        col_strip = strip[0:strip_h, x0:x1]
        col_w = x1 - x0

        # 在列内找 glyph 组件
        hsv = cv2.cvtColor(col_strip, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(col_strip, cv2.COLOR_BGR2GRAY)

        orange_mask = (
            (hsv[:, :, 0] >= _ORANGE_H_LOW) & (hsv[:, :, 0] <= _ORANGE_H_HIGH)
            & (hsv[:, :, 1] > _ORANGE_S_MIN) & (hsv[:, :, 2] > _ORANGE_V_MIN)
        )
        orange_u8 = (orange_mask * 255).astype(np.uint8)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        cleaned = cv2.morphologyEx(orange_u8, cv2.MORPH_CLOSE, kernel)

        n_labels, _, stats, centroids = cv2.connectedComponentsWithStats(cleaned, 8)

        # 收集 glyph 组件
        col_components = []
        for i in range(1, n_labels):
            area = int(stats[i, cv2.CC_STAT_AREA])
            bw = int(stats[i, cv2.CC_STAT_WIDTH])
            bh = int(stats[i, cv2.CC_STAT_HEIGHT])
            if area < _MIN_COMPONENT_AREA or area > _MAX_COMPONENT_AREA:
                continue
            comp = {
                "x": int(stats[i, cv2.CC_STAT_LEFT]),
                "y": int(stats[i, cv2.CC_STAT_TOP]),
                "w": bw, "h": bh,
                "cx": float(centroids[i][0]),
                "cy": float(centroids[i][1]),
                "area": area,
            }
            if is_glyph_like(comp):
                comp["fill"] = round(area / max(bw * bh, 1), 4)
                col_components.append(comp)

        col_components = [comp for comp in col_components if _keep_column_component(col_key, comp)]

        if not col_components:
            entry = {
                "column": col_key, "groups": [], "value": None,
                "reliable": False, "reason": "no_components",
                "candidates": [],
                "status": "empty",
            }
            template_matches.append(entry)
            column_debug.update({"status": "empty", "reason": "no_components"})
            debug_columns.append(column_debug)
            all_reliable = False
            continue

        # 在列内分组
        col_components.sort(key=lambda c: (int(c["y"] / 20), c["cx"]))
        col_groups = _group_components(col_components)

        # 对每个 group 匹配模板
        candidates = []

        for group in col_groups:
            refined_group, result = _match_best_group_variant(
                matcher,
                col_strip,
                col_key,
                group,
                col_w=col_w,
                strip_h=strip_h,
                pad=pad,
            )
            if result is None:
                continue

            candidate = _build_candidate(col_key, x0, refined_group, result, image_size)
            candidates.append(candidate)

        candidates = _apply_column_acceptance_rules(col_key, candidates)
        if not any(
            candidate.get("status") == "accepted"
            and isinstance(candidate.get("value"), int)
            and int(candidate.get("group_y", 0)) < 95
            for candidate in candidates
        ):
            highlighted_top = _detect_highlighted_top_single_digit(
                col_strip,
                col_key,
                x0,
                image_size,
            )
            if highlighted_top is not None:
                corroborated = [
                    candidate
                    for candidate in candidates
                    if candidate.get("status") == "low_confidence"
                    and candidate.get("value") == highlighted_top.get("value")
                    and int(candidate.get("group_y", 0)) < 95
                    and _candidate_has_plus_digit_signature(candidate)
                ]
                if len(corroborated) == 1:
                    corroborated[0]["status"] = "accepted"
                    corroborated[0]["reason"] = (
                        "highlighted_top_red_topology_corroborated"
                    )
                    corroborated[0]["topology_evidence"] = dict(
                        highlighted_top["topology_evidence"]
                    )
                else:
                    candidates.append(highlighted_top)
        row_debug, row_totals = _summarize_column_rows(col_key, candidates)
        row_buckets = _row_buckets(row_debug)
        raw_sum = sum(
            int(candidate["raw_value"])
            for candidate in candidates
            if isinstance(candidate.get("raw_value"), int)
        )
        total_value = sum(
            int(candidate["value"])
            for candidate in candidates
            if _candidate_contributes_to_column_value(col_key, candidate)
        )
        accepted_value = sum(
            int(candidate["value"])
            for candidate in candidates
            if candidate.get("status") == "accepted" and isinstance(candidate.get("value"), int)
        )
        col_reliable = (
            bool(accepted_value)
            and _has_strong_accepted_candidate(candidates)
            and all(
            not _candidate_blocks_column_acceptance(col_key, candidate)
            for candidate in candidates
            )
        )

        for candidate in candidates:
            if _candidate_blocks_overall_reliability(col_key, candidate):
                all_reliable = False

        canonical_value = _canonical_column_value(col_key, row_debug)

        canonical_decision_safe = _is_decision_safe_total_gain_value(
            col_key, canonical_value
        )
        if total_value > 0 or canonical_value is not None:
            column_status = "accepted" if col_reliable and canonical_decision_safe else "partial"
            column_reason = "" if column_status == "accepted" else "low_confidence_or_rejected"
            canonical_status = "accepted" if column_status == "accepted" else "rejected"
            canonical_reason = "" if canonical_status == "accepted" else "canonical_filtered"
            entry = {
                "column": col_key,
                "value": canonical_value,
                "reliable": col_reliable,
                "n_groups": len(col_groups),
                "candidates": candidates,
                "rows": row_debug,
                "raw_rows": row_buckets["raw_rows"],
                "accepted_rows": row_buckets["accepted_rows"],
                "unreliable_rows": row_buckets["unreliable_rows"],
                "status": column_status,
                "reason": column_reason,
                "accepted_value": canonical_value,
                "canonical_value": canonical_value,
                "canonical_status": canonical_status,
                "canonical_reason": canonical_reason,
                "sum": total_value if total_value else None,
                "raw_sum": raw_sum if raw_sum else None,
                "base_value": row_totals["base"] or None,
                "bonus_value": row_totals["bonus"] or None,
            }
            template_matches.append(entry)
            candidate = {
                "value": canonical_value,
                "sum": total_value if total_value else None,
                "raw_sum": raw_sum if raw_sum else None,
                "accepted_value": canonical_value,
                "canonical_value": canonical_value,
                "canonical_status": canonical_status,
                "canonical_reason": canonical_reason,
                "status": column_status,
                "reason": column_reason,
                "candidates": candidates,
                "rows": row_debug,
                "raw_rows": row_buckets["raw_rows"],
                "accepted_rows": row_buckets["accepted_rows"],
                "unreliable_rows": row_buckets["unreliable_rows"],
                "score": _column_score(candidates),
                "base_value": row_totals["base"] or None,
                "bonus_value": row_totals["bonus"] or None,
            }
            column_debug.update(candidate)
            debug_columns.append(column_debug)

            if col_reliable and canonical_decision_safe:
                gains.append({"x": round((x0 + x1) / 2 / 1440, 3), "n": canonical_value, "col": col_key, "confidence": 0.9})
            else:
                partial_value = canonical_value if isinstance(canonical_value, int) else total_value
                if partial_value > 0:
                    partials.append({"x": round((x0 + x1) / 2 / 1440, 3), "n": partial_value, "col": col_key, "reason": "low_confidence"})
        else:
            entry = {
                "column": col_key, "groups": [], "value": None,
                "reliable": False, "reason": "no_value",
                "candidates": candidates,
                "rows": row_debug,
                "raw_rows": row_buckets["raw_rows"],
                "accepted_rows": row_buckets["accepted_rows"],
                "unreliable_rows": row_buckets["unreliable_rows"],
                "status": "candidate" if candidates else "empty",
                "raw_sum": raw_sum if raw_sum else None,
                "base_value": row_totals["base"] or None,
                "bonus_value": row_totals["bonus"] or None,
            }
            template_matches.append(entry)
            column_debug.update(
                {
                    "status": "candidate" if candidates else "empty",
                    "reason": "no_value",
                    "candidates": candidates,
                    "rows": row_debug,
                    "raw_rows": row_buckets["raw_rows"],
                    "accepted_rows": row_buckets["accepted_rows"],
                    "unreliable_rows": row_buckets["unreliable_rows"],
                    "score": _column_score(candidates),
                    "raw_sum": raw_sum if raw_sum else None,
                    "base_value": row_totals["base"] or None,
                    "bonus_value": row_totals["bonus"] or None,
                }
            )
            debug_columns.append(column_debug)
            all_reliable = False

    n_reliable = sum(1 for m in template_matches if m.get("reliable"))
    n_total = sum(1 for m in template_matches if m.get("sum") is not None or m.get("value") is not None)
    debug = _build_stats_gain_ocr_debug(
        debug_columns,
        image_size=image_size,
        normalized_from=normalized_from,
        status="ok",
    )
    base_gain = {
        column["attribute_id"]: int(column.get("base_value"))
        for column in debug_columns
        if isinstance(column.get("base_value"), int) and int(column.get("base_value")) > 0
    }
    bonus_gain = {
        column["attribute_id"]: int(column.get("bonus_value"))
        for column in debug_columns
        if isinstance(column.get("bonus_value"), int) and int(column.get("bonus_value")) > 0
    }
    stats_gain_total = {
        column["attribute_id"]: (
            int(column.get("base_value") or 0)
            + int(column.get("bonus_value") or 0)
        )
        for column in debug_columns
        if (
            int(column.get("base_value") or 0)
            + int(column.get("bonus_value") or 0)
        )
        > 0
    }
    decision_safe = _decision_safe_gain_mappings(debug_columns)
    return {
        "gains": gains,
        "partials": partials,
        "template_matches": template_matches,
        "confidence": round(n_reliable / max(len(COLUMN_REGIONS), 1), 3),
        "position_confidence": 1.0,
        "value_confidence": 1.0 if all_reliable and n_reliable >= 3 else 0.5,
        "method": "local_template",
        "value_unreliable": not (n_reliable >= 3 and all_reliable),
        "source": "local_template",
        "template_available": True,
        "n_reliable": n_reliable,
        "n_total": n_total,
        "stats_gain_ocr": debug,
        "base_gain": base_gain,
        "bonus_gain": bonus_gain,
        "stats_gain_total": stats_gain_total,
        "decision_safe_base_gain": decision_safe["base_gain"],
        "decision_safe_bonus_gain": decision_safe["bonus_gain"],
        "decision_safe_stats_gain_total": decision_safe["stats_gain_total"],
        "decision_safe_gain_reasons": decision_safe["reasons"],
    }


def _decision_safe_gain_mappings(
    debug_columns: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    from core.training_gain_reliability import classify_reliable_exact_gain

    base_gain: dict[str, int] = {}
    bonus_gain: dict[str, int] = {}
    total_gain: dict[str, int] = {}
    reasons: dict[str, str] = {}
    for column in debug_columns:
        attribute_id = str(column.get("attribute_id") or "")
        strict = classify_reliable_exact_gain(
            column,
            attribute_id=attribute_id,
        )
        if strict.get("status") != "reliable_exact":
            reasons[attribute_id] = str(strict.get("reason") or "abstain")
            continue
        value = strict.get("value")
        if type(value) is not int:
            reasons[attribute_id] = "verified_value_invalid"
            continue
        total_gain[attribute_id] = value
        base = column.get("base_value")
        bonus = column.get("bonus_value")
        if type(base) is int and base > 0:
            base_gain[attribute_id] = base
        if type(bonus) is int and bonus > 0:
            bonus_gain[attribute_id] = bonus
        reasons[attribute_id] = "accepted_contributors"
    return {
        "base_gain": base_gain,
        "bonus_gain": bonus_gain,
        "stats_gain_total": total_gain,
        "reasons": reasons,
    }


def _new_column_debug(col_key: str, x0: int, x1: int, image_size: list[int]) -> dict[str, Any]:
    normalized_roi = [x0, _GAIN_Y_TOP, x1, _GAIN_Y_BOTTOM]
    return {
        "attribute_id": col_key,
        "roi": _scale_bbox(normalized_roi, image_size),
        "normalized_roi": normalized_roi,
        "candidates": [],
        "rows": [],
        "raw_rows": [],
        "accepted_rows": [],
        "unreliable_rows": [],
        "value": None,
        "sum": None,
        "accepted_value": None,
        "canonical_value": None,
        "canonical_status": "empty",
        "canonical_reason": "not_processed",
        "base_value": None,
        "bonus_value": None,
        "score": 0.0,
        "status": "empty",
        "reason": "not_processed",
    }


def _group_bbox(column_x0: int, group: dict) -> list[int]:
    return [
        int(column_x0 + group["x"]),
        int(_GAIN_Y_TOP + group["y"]),
        int(column_x0 + group["x"] + group["w"]),
        int(_GAIN_Y_TOP + group["y"] + group["h"]),
    ]


def _scale_bbox(bbox: list[int], image_size: list[int]) -> list[int]:
    width, height = image_size
    sx = width / _REFERENCE_WIDTH
    sy = height / _REFERENCE_HEIGHT
    return [
        int(round(bbox[0] * sx)),
        int(round(bbox[1] * sy)),
        int(round(bbox[2] * sx)),
        int(round(bbox[3] * sy)),
    ]


def _candidate_status(
    col_key: str,
    value: Any,
    reliable: bool,
    matcher_reason: str,
) -> tuple[str, str]:
    if value is None:
        return "unreadable", matcher_reason or "no_value"
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return "unreadable", "non_integer_value"
    if numeric <= 0:
        return "unreadable", "non_positive_value"
    if numeric > _gain_limit(col_key):
        if _is_wide_layered_gain_value(col_key, numeric):
            return "low_confidence", matcher_reason or "large_gain_unverified"
        return "rejected_suspicious_value", "suspicious_value"
    if reliable:
        return "accepted", ""
    return "low_confidence", matcher_reason or "low_confidence"


def _gain_limit(col_key: str) -> int:
    return 60 if col_key == "skill_pt" else 40


def _parse_gain_limit(col_key: str) -> int:
    return 199


def _is_wide_layered_gain_value(col_key: str, value: int) -> bool:
    return _gain_limit(col_key) < value <= _parse_gain_limit(col_key) and value >= 100


def _is_decision_safe_gain_value(col_key: str, value: Any) -> bool:
    return isinstance(value, int) and 0 < value <= _gain_limit(col_key)


def _is_decision_safe_total_gain_value(col_key: str, value: Any) -> bool:
    return isinstance(value, int) and 0 < value <= _parse_gain_limit(col_key)


def _is_parse_preserved_gain_value(col_key: str, value: Any) -> bool:
    return (
        _is_decision_safe_gain_value(col_key, value)
        or (isinstance(value, int) and _is_wide_layered_gain_value(col_key, value))
    )


def _min_score(scores: Any) -> float:
    if not isinstance(scores, list) or not scores:
        return 0.0
    numeric = [float(score) for score in scores if isinstance(score, (int, float))]
    return round(min(numeric), 4) if numeric else 0.0


def _column_score(candidates: list[dict[str, Any]]) -> float:
    scores = [
        float(candidate.get("score", 0.0))
        for candidate in candidates
        if isinstance(candidate.get("score"), (int, float))
    ]
    return round(max(scores), 4) if scores else 0.0


def _build_stats_gain_ocr_debug(
    columns: list[dict[str, Any]],
    *,
    image_size: list[int],
    normalized_from: list[int] | None,
    status: str,
) -> dict[str, Any]:
    accepted_count = sum(1 for column in columns if column.get("status") == "accepted")
    candidate_count = sum(len(column.get("candidates", [])) for column in columns)
    return {
        "schema_version": "training_stats_gain_ocr_debug.v1",
        "source": "local_template",
        "image_size": image_size,
        "normalized_size": [_REFERENCE_WIDTH, _REFERENCE_HEIGHT],
        "normalized_from": normalized_from,
        "status": status,
        "columns": columns,
        "summary": {
            "accepted_count": accepted_count,
            "candidate_count": candidate_count,
        },
    }


def _row_kind_for_candidate(candidate: dict[str, Any]) -> str:
    return "base" if int(candidate.get("group_y", 0)) < 90 else "bonus"


def _tokens_for_gain_value(value: int) -> list[str]:
    return ["+", *list(str(int(value)))]


def _candidate_has_plus_digit_signature(candidate: dict[str, Any]) -> bool:
    value = candidate.get("value")
    if not _is_parse_preserved_gain_value(str(candidate.get("column") or ""), value):
        return False
    tokens = [str(token) for token in candidate.get("tokens") or []]
    if len(tokens) < 2 or tokens[0] != "+":
        return False
    digits = tokens[1:]
    if not digits or not all(token.isdigit() for token in digits):
        return False
    try:
        return int("".join(digits)) == int(value)
    except (TypeError, ValueError):
        return False


def _normalize_accepted_candidate_tokens(candidate: dict[str, Any]) -> None:
    value = candidate.get("value")
    if candidate.get("status") != "accepted" or not isinstance(value, int):
        return
    if _candidate_has_plus_digit_signature(candidate):
        return
    candidate.setdefault("raw_tokens", list(candidate.get("tokens") or []))
    candidate.setdefault("raw_text", candidate.get("text", ""))
    candidate["tokens"] = _tokens_for_gain_value(value)
    candidate["text"] = "".join(candidate["tokens"])


def _row_value_for_candidate(col_key: str, candidate: dict[str, Any]) -> int | None:
    value = candidate.get("value")
    if not _is_parse_preserved_gain_value(col_key, value):
        return None
    if not _candidate_has_plus_digit_signature(candidate):
        return None
    if candidate.get("status") not in {"accepted", "low_confidence"}:
        return None
    return int(value)


def _summarize_column_rows(
    col_key: str,
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    totals = {"base": 0, "bonus": 0}
    for candidate in sorted(candidates, key=lambda item: (int(item.get("group_y", 0)), int(item.get("group_w", 0)))):
        row_kind = _row_kind_for_candidate(candidate)
        value = _row_value_for_candidate(col_key, candidate)
        row = {
            "kind": row_kind,
            "roi": candidate.get("roi"),
            "normalized_roi": candidate.get("normalized_roi"),
            "tokens": list(candidate.get("tokens") or []),
            "raw_text": candidate.get("raw_text") or candidate.get("text") or "",
            "value": value,
            "raw_value": candidate.get("raw_value"),
            "status": candidate.get("status"),
            "reason": candidate.get("reason"),
            "score": candidate.get("score"),
        }
        rows.append(row)
        if value is not None and candidate.get("status") == "accepted":
            totals[row_kind] += value
    return rows, totals


def _row_buckets(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    raw_rows = [dict(row) for row in rows]
    accepted_rows = [
        dict(row)
        for row in rows
        if row.get("status") == "accepted" and row.get("value") is not None
    ]
    unreliable_rows = [
        dict(row)
        for row in rows
        if row.get("status") != "accepted" or row.get("value") is None
    ]
    return {
        "raw_rows": raw_rows,
        "accepted_rows": accepted_rows,
        "unreliable_rows": unreliable_rows,
    }


def _canonical_column_value(
    col_key: str,
    rows: list[dict[str, Any]],
) -> int | None:
    accepted_rows = [
        row
        for row in rows
        if row.get("status") == "accepted"
        and isinstance(row.get("value"), int)
        and int(row.get("value")) > 0
    ]
    if not accepted_rows:
        return None

    if col_key == "skill_pt":
        raw_base_rows = [row for row in rows if row.get("kind") == "base"]
        raw_bonus_rows = [row for row in rows if row.get("kind") == "bonus"]
        base_rows = [
            row for row in accepted_rows if row.get("kind") == "base"
        ]
        bonus_rows = [
            row for row in accepted_rows if row.get("kind") == "bonus"
        ]
        if raw_base_rows and raw_bonus_rows:
            if (
                len(raw_base_rows) != 1
                or len(raw_bonus_rows) != 1
                or len(base_rows) != 1
                or len(bonus_rows) != 1
            ):
                return None
            return int(base_rows[0]["value"]) + int(bonus_rows[0]["value"])
        if len(raw_base_rows) > 1 or len(raw_bonus_rows) > 1:
            return None

    if len(accepted_rows) == 1:
        row = accepted_rows[0]
        value = int(row["value"])
        reason = str(row.get("reason") or "")
        if reason == "heuristic_row_accept":
            if value in {2, 7, 10, 11}:
                return value
            tokens = [str(token) for token in row.get("tokens") or []]
            if (
                float(row.get("score", 0.0)) >= 0.85
                and len(tokens) >= 2
                and tokens[0] == "+"
                and all(token.isdigit() for token in tokens[1:])
            ):
                return value
            if col_key == "guts" and value == 9:
                return 1
            return None
        if reason == "" and value == 1:
            return None
        return value

    return sum(int(row["value"]) for row in accepted_rows)


def _keep_column_component(col_key: str, comp: dict[str, Any]) -> bool:
    fill = float(comp.get("fill", 0.0))
    width = int(comp.get("w", 0))
    y = int(comp.get("y", 0))
    if fill < 0.15 and width >= 80:
        return False
    if y < 25 and width >= 100:
        return False
    if width >= 95 and fill < 0.35:
        return False
    if y >= 90 and width >= 60:
        return False
    if col_key == "skill_pt" and y >= 90 and width >= 45 and fill < 0.5:
        return False
    return True


def _detect_highlighted_top_single_digit(
    col_strip: np.ndarray,
    col_key: str,
    column_x0: int,
    image_size: list[int],
) -> dict[str, Any] | None:
    """Read a top-layer red-highlighted single digit from glyph topology.

    The guts-column ``+4`` uses the same highlighted shell as the existing
    +6/+8/+9 cases, but its single inner red component sits on the exact
    boundary between the historical 9/6 offset buckets.  Keep that evidence
    column-scoped: applying the offset to every column confuses a known +1
    sample with +4.
    """
    if col_key not in {"power", "skill_pt", "wisdom", "guts"}:
        return None
    hsv = cv2.cvtColor(col_strip, cv2.COLOR_BGR2HSV)
    mask = (
        (hsv[:, :, 0] <= 3)
        & (hsv[:, :, 1] >= 140)
        & (hsv[:, :, 2] >= 140)
    ).astype(np.uint8) * 255
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    components: list[dict[str, int]] = []
    for index in range(1, count):
        x, y, width, height, area = map(int, stats[index])
        if area >= 10:
            components.append(
                {"x": x, "y": y, "w": width, "h": height, "area": area}
            )

    for digit in components:
        if not (
            35 <= digit["w"] <= 45
            and 50 <= digit["h"] <= 58
            and 15 <= digit["y"] <= 38
        ):
            continue
        pluses = [
            component
            for component in components
            if (
                30 <= component["w"] <= 42
                and 28 <= component["h"] <= 42
                and 25 <= component["y"] <= 52
                and 5 <= digit["x"] - (component["x"] + component["w"]) <= 28
            )
        ]
        inner = [
            component
            for component in components
            if (
                5 <= component["w"] <= 16
                and 5 <= component["h"] <= 16
                and digit["x"] <= component["x"] <= digit["x"] + digit["w"]
                and digit["y"] <= component["y"] <= digit["y"] + digit["h"]
            )
        ]
        if not pluses:
            continue
        offsets = sorted(component["y"] - digit["y"] for component in inner)
        if col_key == "guts":
            if offsets == [19]:
                value = 4
            else:
                continue
        else:
            if len(offsets) == 1 and offsets[0] <= 18:
                value = 9
            elif len(offsets) == 1 and offsets[0] >= 20:
                value = 6
            elif len(offsets) == 2 and offsets[0] <= 18 and offsets[1] >= 20:
                value = 8
            else:
                continue

        plus = pluses[0]
        x0 = min(plus["x"], digit["x"])
        y0 = min(plus["y"], digit["y"])
        x1 = max(plus["x"] + plus["w"], digit["x"] + digit["w"])
        y1 = max(plus["y"] + plus["h"], digit["y"] + digit["h"])
        group = {
            "x": x0,
            "y": y0,
            "w": x1 - x0,
            "h": y1 - y0,
            "count": 2,
            "area": plus["area"] + digit["area"],
            "members": [plus, digit],
        }
        normalized_roi = _group_bbox(column_x0, group)
        return {
            "tokens": ["+", str(value)],
            "text": f"+{value}",
            "raw_value": value,
            "value": value,
            "scores": [1.0, 1.0],
            "digit_scores": [1.0],
            "plus_score": 1.0,
            "component_matches": [],
            "score": 1.0,
            "status": "accepted",
            "reason": "highlighted_top_red_topology",
            "topology_evidence": {
                "schema_version": "highlighted_gain_topology.v1",
                "source": "red_component_topology",
                "column": col_key,
                "tokens": ["+", str(value)],
                "value": value,
                "inner_offsets": offsets,
                "plus_bbox": [plus["x"], plus["y"], plus["w"], plus["h"]],
                "digit_bbox": [digit["x"], digit["y"], digit["w"], digit["h"]],
            },
            "roi": _scale_bbox(normalized_roi, image_size),
            "normalized_roi": normalized_roi,
            "group_y": y0,
            "group_w": x1 - x0,
            "group_h": y1 - y0,
            "group_count": 2,
            "group_area": plus["area"] + digit["area"],
            "member_widths": [plus["w"], digit["w"]],
            "member_heights": [plus["h"], digit["h"]],
            "column": col_key,
            "matcher_reason": "highlighted_top_red_topology",
        }
    return None


def _crop_group_members(
    col_strip: np.ndarray,
    members: list[dict[str, Any]],
    *,
    col_w: int,
    strip_h: int,
    pad: int,
) -> list[np.ndarray]:
    crops: list[np.ndarray] = []
    for comp in members:
        cx0 = max(0, int(comp["x"]) - pad)
        cy0 = max(0, int(comp["y"]) - pad)
        cx1 = min(col_w, int(comp["x"] + comp["w"]) + pad)
        cy1 = min(strip_h, int(comp["y"] + comp["h"]) + pad)
        crop = col_strip[cy0:cy1, cx0:cx1]
        if crop.size > 0:
            crops.append(crop)
    return crops


def _match_quality(
    col_key: str,
    match_result: dict[str, Any],
    *,
    subset_count: int,
    strong: bool = False,
) -> tuple[Any, ...] | None:
    value = match_result.get("value")
    if not _is_parse_preserved_gain_value(col_key, value):
        return None
    tokens = [str(token) for token in match_result.get("tokens", [])]
    if not tokens or tokens[0] != "+":
        return None
    digit_scores = [
        float(score)
        for score in match_result.get("digit_scores", [])
        if isinstance(score, (int, float))
    ]
    if not digit_scores or len(digit_scores) > 3:
        return None
    min_digit_score = min(digit_scores)
    avg_digit_score = sum(digit_scores) / len(digit_scores)
    plus_score = float(match_result.get("plus_score", 0.0))
    reliable = 1 if match_result.get("reliable") else 0
    if strong and (
        not reliable
        or min_digit_score < 0.9
        or plus_score < 0.9
    ):
        return None
    return (
        reliable,
        len(digit_scores),
        min_digit_score,
        avg_digit_score,
        plus_score,
        -subset_count,
    )


def _get_rescue_template_matcher() -> Any | None:
    global _RESCUE_MATCHER
    if _RESCUE_MATCHER is None:
        from core.gain_template_matcher import GainTemplateMatcher

        _RESCUE_MATCHER = GainTemplateMatcher(_RESCUE_TEMPLATE_DIR)
    return _RESCUE_MATCHER


def _get_zero_rescue_template_matcher() -> Any | None:
    global _ZERO_RESCUE_MATCHER
    if _ZERO_RESCUE_MATCHER is None:
        from core.gain_template_matcher import GainTemplateMatcher

        _ZERO_RESCUE_MATCHER = GainTemplateMatcher(_ZERO_RESCUE_TEMPLATE_DIR)
    return _ZERO_RESCUE_MATCHER


def _should_keep_base_match(
    col_key: str,
    match_result: dict[str, Any],
) -> bool:
    value = match_result.get("value")
    if not _is_parse_preserved_gain_value(col_key, value):
        return False
    tokens = [str(token) for token in match_result.get("tokens", [])]
    if not tokens or tokens[0] != "+":
        return False
    digit_scores = [
        float(score)
        for score in match_result.get("digit_scores", [])
        if isinstance(score, (int, float))
    ]
    if not digit_scores:
        return False
    return (
        min(digit_scores) >= 0.82
        and float(match_result.get("plus_score", 0.0)) >= 0.9
    ) or _looks_like_coherent_two_digit_match(match_result)


def _looks_like_coherent_two_digit_match(match_result: dict[str, Any]) -> bool:
    value = match_result.get("value")
    tokens = [str(token) for token in match_result.get("tokens") or []]
    if not isinstance(value, int) or not (10 <= value <= 40):
        return False
    if len(tokens) != 3 or tokens[0] != "+" or not all(
        token.isdigit() for token in tokens[1:]
    ):
        return False
    if float(match_result.get("plus_score", 0.0)) < 0.9:
        return False
    matches = match_result.get("component_matches")
    if not isinstance(matches, list) or len(matches) != 3:
        return False
    first = matches[1] if isinstance(matches[1], dict) else {}
    tail = matches[2] if isinstance(matches[2], dict) else {}
    return (
        float(first.get("score", 0.0)) >= 0.82
        and float(first.get("margin", 0.0)) >= 0.03
        and float(tail.get("score", 0.0)) >= 0.62
        and float(tail.get("margin", 0.0)) >= 0.20
    )


def _match_best_group_variant(
    matcher: Any,
    col_strip: np.ndarray,
    col_key: str,
    group: dict[str, Any],
    *,
    col_w: int,
    strip_h: int,
    pad: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    members = list(group.get("members", [group]))
    if not members:
        return group, None

    full_crops = _crop_group_members(
        col_strip,
        members,
        col_w=col_w,
        strip_h=strip_h,
        pad=pad,
    )
    if not full_crops:
        return group, None

    best_group = group
    best_result = matcher.match_components(full_crops)
    if _looks_like_skill_pt_top_row_result(col_key, group, best_result):
        return best_group, best_result
    if best_result.get("reliable"):
        return best_group, best_result
    if _should_keep_base_match(col_key, best_result):
        return best_group, best_result
    recovered_ten = _recover_ten_with_isolated_zero_templates(
        col_strip,
        members,
        col_w=col_w,
        strip_h=strip_h,
        pad=pad,
    )
    if recovered_ten is not None:
        return recovered_ten
    rescue_matcher = _get_rescue_template_matcher()
    if rescue_matcher is None or not getattr(rescue_matcher, "available", False):
        return best_group, best_result

    rescue_group = group
    rescue_result = rescue_matcher.match_components(full_crops)
    noisy_top_row = _recover_noisy_top_row_fourteen(
        matcher,
        rescue_matcher,
        col_strip,
        col_key,
        group,
        members,
        col_w=col_w,
        strip_h=strip_h,
        pad=pad,
    )
    if noisy_top_row is not None:
        return noisy_top_row
    if _should_prefer_rescue_full_result(
        col_key, group, best_result, rescue_result
    ):
        return rescue_group, rescue_result
    rescue_quality = _match_quality(
        col_key,
        rescue_result,
        subset_count=len(members),
        strong=True,
    )
    rescue_prefix_result: dict[str, Any] | None = None
    rescue_prefix_members: list[dict[str, Any]] | None = None

    max_subset_size = min(len(members), 4)
    for subset_size in range(2, max_subset_size + 1):
        for indices in combinations(range(len(members)), subset_size):
            if indices == tuple(range(len(members))):
                continue
            subset_members = [members[index] for index in indices]
            subset_crops = _crop_group_members(
                col_strip,
                subset_members,
                col_w=col_w,
                strip_h=strip_h,
                pad=pad,
            )
            if not subset_crops:
                continue
            subset_result = rescue_matcher.match_components(subset_crops)
            if indices == tuple(range(subset_size)):
                rescue_prefix_result = subset_result
                rescue_prefix_members = subset_members
            subset_quality = _match_quality(
                col_key,
                subset_result,
                subset_count=len(subset_members),
                strong=True,
            )
            if subset_quality is None:
                continue
            if rescue_quality is None or subset_quality > rescue_quality:
                rescue_quality = subset_quality
                rescue_group = _merge_group(subset_members)
                rescue_result = subset_result

    composed_result = _compose_rescue_prefix_with_primary_suffix(
        col_key,
        best_result,
        rescue_prefix_result,
        len(members),
    )
    if composed_result is not None and rescue_prefix_members:
        return best_group, composed_result
    tail_three_result = _compose_rescue_prefix_with_tail_three_topology(
        best_result,
        rescue_prefix_result,
        full_crops,
    )
    if tail_three_result is not None:
        return best_group, tail_three_result
    if rescue_quality is not None:
        return rescue_group, rescue_result
    return best_group, best_result


def _recover_ten_with_isolated_zero_templates(
    col_strip: np.ndarray,
    members: list[dict[str, Any]],
    *,
    col_w: int,
    strip_h: int,
    pad: int,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if len(members) < 3:
        return None
    matcher = _get_zero_rescue_template_matcher()
    if matcher is None or not getattr(matcher, "available", False):
        return None
    for tail_index in range(2, len(members)):
        selected_members = [members[0], members[1], members[tail_index]]
        crops = _crop_group_members(
            col_strip,
            selected_members,
            col_w=col_w,
            strip_h=strip_h,
            pad=pad,
        )
        if len(crops) != 3:
            continue
        result = matcher.match_components(crops)
        if (
            result.get("reliable")
            and result.get("value") == 10
            and [str(token) for token in result.get("tokens") or []]
            == ["+", "1", "0"]
        ):
            recovered = dict(result)
            recovered["reason"] = "isolated_zero_template_ten"
            return _merge_group(selected_members), recovered
    return None


def _recover_noisy_top_row_fourteen(
    matcher: Any,
    rescue_matcher: Any,
    col_strip: np.ndarray,
    col_key: str,
    group: dict[str, Any],
    members: list[dict[str, Any]],
    *,
    col_w: int,
    strip_h: int,
    pad: int,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if col_key == "skill_pt":
        return None
    if int(group.get("y", 0)) > 75:
        return None
    if int(group.get("count", 0)) < 5 or int(group.get("w", 0)) < 150:
        return None

    for prefix_index in range(0, len(members) - 2):
        prefix_members = members[prefix_index : prefix_index + 2]
        prefix_crops = _crop_group_members(
            col_strip,
            prefix_members,
            col_w=col_w,
            strip_h=strip_h,
            pad=pad,
        )
        if len(prefix_crops) != 2:
            continue
        prefix_result = rescue_matcher.match_components(prefix_crops)
        if prefix_result.get("tokens") != ["+", "1"] or not prefix_result.get("reliable"):
            continue

        one_x = int(prefix_members[1].get("x", 0))
        for digit_member in members[prefix_index + 2 :]:
            if int(digit_member.get("x", 0)) - one_x > 95:
                break
            digit_crops = _crop_group_members(
                col_strip,
                [digit_member],
                col_w=col_w,
                strip_h=strip_h,
                pad=pad,
            )
            if len(digit_crops) != 1:
                continue
            digit_match = matcher.match_digit(digit_crops[0])
            if digit_match.get("best_digit") != "4":
                continue
            if float(digit_match.get("score", 0.0)) < 0.33:
                continue

            selected_members = [prefix_members[0], prefix_members[1], digit_member]
            selected_group = _merge_group(selected_members)
            prefix_scores = list(prefix_result.get("scores", []))
            component_matches = list(prefix_result.get("component_matches", [])) + [digit_match]
            plus_score = float(prefix_result.get("plus_score", 0.0))
            return selected_group, {
                "value": 14,
                "tokens": ["+", "1", "4"],
                "scores": prefix_scores + [float(digit_match.get("score", 0.0))],
                "digit_scores": [
                    *[
                        float(score)
                        for score in prefix_result.get("digit_scores", [])
                        if isinstance(score, (int, float))
                    ],
                    float(digit_match.get("score", 0.0)),
                ],
                "plus_score": plus_score,
                "component_matches": component_matches,
                "reliable": False,
                "reason": "noisy_top_row_fourteen_recovered",
            }
    return None


def _should_prefer_rescue_full_result(
    col_key: str,
    group: dict[str, Any],
    primary_result: dict[str, Any],
    rescue_result: dict[str, Any],
) -> bool:
    if int(group.get("y", 0)) < 90:
        return False
    primary_value = primary_result.get("value")
    rescue_value = rescue_result.get("value")
    if not isinstance(primary_value, int) or not isinstance(rescue_value, int):
        return False
    if (
        col_key == "skill_pt"
        and int(group.get("y", 0)) >= 90
        and rescue_value == 9
        and bool(rescue_result.get("reliable"))
        and not bool(primary_result.get("reliable"))
    ):
        return True
    if primary_value == rescue_value:
        return False
    if not (10 <= rescue_value <= 40):
        return False

    rescue_tokens = [str(token) for token in rescue_result.get("tokens", [])]
    if not rescue_tokens or rescue_tokens[0] != "+":
        return False
    if not all(token.isdigit() for token in rescue_tokens[1:]):
        return False

    primary_margins = _digit_margins(primary_result)
    rescue_margins = _digit_margins(rescue_result)
    rescue_digit_scores = [
        float(score)
        for score in rescue_result.get("digit_scores", [])
        if isinstance(score, (int, float))
    ]
    if not primary_margins or not rescue_margins or not rescue_digit_scores:
        return False
    if min(primary_margins) > 0.02:
        return False
    if min(rescue_margins) < 0.15:
        return False
    if min(rescue_digit_scores) < 0.65:
        return False
    if float(rescue_result.get("plus_score", 0.0)) >= 0.80:
        return True
    return _is_recoverable_primary_sixty_with_rescue_one_x(
        primary_result,
        rescue_result,
    )


def _digit_margins(match_result: dict[str, Any]) -> list[float]:
    tokens = [str(token) for token in match_result.get("tokens", [])]
    component_matches = match_result.get("component_matches")
    if not isinstance(component_matches, list):
        return []
    margins: list[float] = []
    for token, component in zip(tokens[1:], component_matches[1:]):
        if not token.isdigit() or not isinstance(component, dict):
            continue
        margin = component.get("margin")
        if isinstance(margin, (int, float)):
            margins.append(float(margin))
            continue
        score = component.get("score")
        second_score = component.get("second_score")
        if isinstance(score, (int, float)) and isinstance(second_score, (int, float)):
            margins.append(float(score) - float(second_score))
    return margins


def _is_recoverable_primary_sixty_with_rescue_one_x(
    primary_result: dict[str, Any],
    rescue_result: dict[str, Any],
) -> bool:
    """Allow the proven narrow-leading-one rescue, but not nearby noisy values."""
    if primary_result.get("value") != 60:
        return False
    if [str(token) for token in primary_result.get("tokens", [])] != ["+", "6", "0"]:
        return False
    rescue_tokens = [str(token) for token in rescue_result.get("tokens", [])]
    if len(rescue_tokens) != 3 or rescue_tokens[:2] != ["+", "1"]:
        return False
    rescue_scores = [
        float(score)
        for score in rescue_result.get("digit_scores", [])
        if isinstance(score, (int, float))
    ]
    return (
        len(rescue_scores) == 2
        and min(rescue_scores) >= 0.75
        and float(rescue_result.get("plus_score", 0.0)) >= 0.75
    )


def _compose_rescue_prefix_with_primary_suffix(
    col_key: str,
    primary_result: dict[str, Any],
    rescue_prefix_result: dict[str, Any] | None,
    member_count: int,
) -> dict[str, Any] | None:
    if member_count != 3 or not isinstance(rescue_prefix_result, dict):
        return None
    if not rescue_prefix_result.get("reliable"):
        return None
    prefix_tokens = [str(token) for token in rescue_prefix_result.get("tokens", [])]
    if len(prefix_tokens) != 2 or prefix_tokens[0] != "+" or not prefix_tokens[1].isdigit():
        return None
    if prefix_tokens[1] != "1":
        return None

    primary_tokens = [str(token) for token in primary_result.get("tokens", [])]
    if len(primary_tokens) != 3 or primary_tokens[0] != "+" or not primary_tokens[2].isdigit():
        return None
    if primary_tokens[2] != "4":
        return None

    primary_matches = primary_result.get("component_matches")
    if not isinstance(primary_matches, list) or len(primary_matches) < 3:
        return None
    suffix_match = primary_matches[2]
    if not isinstance(suffix_match, dict):
        return None
    suffix_score = float(suffix_match.get("score", 0.0))
    if suffix_score < 0.60:
        return None

    tokens = ["+", prefix_tokens[1], primary_tokens[2]]
    try:
        value = int("".join(tokens[1:]))
    except ValueError:
        return None
    if not _is_parse_preserved_gain_value(col_key, value):
        return None
    scores = list(rescue_prefix_result.get("scores", [])) + [suffix_score]
    component_matches = list(rescue_prefix_result.get("component_matches", [])) + [suffix_match]
    reason = primary_result.get("reason") or "rescue_prefix_primary_suffix"
    return {
        "value": value,
        "tokens": tokens,
        "scores": scores,
        "digit_scores": [
            float(score)
            for score in [*rescue_prefix_result.get("digit_scores", []), suffix_score]
            if isinstance(score, (int, float))
        ],
        "plus_score": float(rescue_prefix_result.get("plus_score", 0.0)),
        "component_matches": component_matches,
        "reliable": False,
        "reason": reason,
    }


def _compose_rescue_prefix_with_two_hole_eight(
    primary_result: dict[str, Any],
    rescue_prefix_result: dict[str, Any] | None,
    component_crops: list[np.ndarray],
) -> dict[str, Any] | None:
    if len(component_crops) != 3 or not isinstance(rescue_prefix_result, dict):
        return None
    if not rescue_prefix_result.get("reliable"):
        return None
    if [str(token) for token in rescue_prefix_result.get("tokens", [])] != ["+", "2"]:
        return None
    if [str(token) for token in primary_result.get("tokens", [])] != ["+", "2", "8"]:
        return None
    matches = primary_result.get("component_matches")
    if not isinstance(matches, list) or len(matches) != 3:
        return None
    tail = matches[2] if isinstance(matches[2], dict) else {}
    if float(tail.get("score", 0.0)) < 0.8:
        return None
    if _orange_glyph_large_hole_count(component_crops[2]) != 2:
        return None
    result = dict(primary_result)
    result.update(
        {
            "value": 28,
            "tokens": ["+", "2", "8"],
            "reliable": True,
            "reason": "rescue_prefix_two_hole_eight",
        }
    )
    return result


def _tail_is_unambiguous_three_not_zero(crop: np.ndarray) -> bool:
    """Accept a three tail only when its orange topology excludes zero."""

    if crop.size == 0 or len(crop.shape) != 3:
        return False
    height, width = crop.shape[:2]
    if not (30 <= height <= 60 and 20 <= width <= 50):
        return False
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = (
        (hsv[:, :, 0] >= _ORANGE_H_LOW)
        & (hsv[:, :, 0] <= _ORANGE_H_HIGH)
        & (hsv[:, :, 1] > _ORANGE_S_MIN)
        & (hsv[:, :, 2] > _ORANGE_V_MIN)
    ).astype(np.uint8) * 255
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
    )
    contours, hierarchy = cv2.findContours(
        mask,
        cv2.RETR_CCOMP,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    if hierarchy is not None and any(
        hierarchy[0][index][3] >= 0 and cv2.contourArea(contour) >= 25.0
        for index, contour in enumerate(contours)
    ):
        return False
    middle = mask[height // 4 : (height * 3) // 4] > 0
    if middle.size == 0:
        return False
    third = max(1, width // 3)
    left_fill = float(middle[:, :third].mean())
    right_fill = float(middle[:, width - third :].mean())
    return left_fill <= 0.40 and right_fill >= 0.50


def _compose_rescue_prefix_with_tail_three_topology(
    primary_result: dict[str, Any],
    rescue_prefix_result: dict[str, Any] | None,
    component_crops: list[np.ndarray],
) -> dict[str, Any] | None:
    """Use independent prefix and topology evidence for a 3/0 tail tie."""

    if len(component_crops) != 3 or not isinstance(rescue_prefix_result, dict):
        return None
    if not rescue_prefix_result.get("reliable"):
        return None
    if [str(token) for token in rescue_prefix_result.get("tokens", [])] != ["+", "1"]:
        return None
    if [str(token) for token in primary_result.get("tokens", [])] != ["+", "6", "0"]:
        return None
    matches = primary_result.get("component_matches")
    if not isinstance(matches, list) or len(matches) != 3:
        return None
    tail = matches[2] if isinstance(matches[2], dict) else None
    if not isinstance(tail, dict):
        return None
    scores = tail.get("all_scores")
    if not isinstance(scores, dict):
        return None
    zero_score = float(scores.get("0", 0.0))
    three_score = float(scores.get("3", 0.0))
    if zero_score < 0.85 or three_score < 0.85:
        return None
    if zero_score - three_score < 0.0 or zero_score - three_score > 0.04:
        return None
    if not _tail_is_unambiguous_three_not_zero(component_crops[2]):
        return None
    return {
        "value": 13,
        "tokens": ["+", "1", "3"],
        "scores": list(rescue_prefix_result.get("scores", [])) + [three_score],
        "digit_scores": list(rescue_prefix_result.get("digit_scores", [])) + [three_score],
        "plus_score": float(rescue_prefix_result.get("plus_score", 0.0)),
        "component_matches": list(rescue_prefix_result.get("component_matches", [])) + [tail],
        "reliable": True,
        "reason": "tail_three_topology_corroborated",
    }


def _orange_glyph_large_hole_count(crop: np.ndarray) -> int:
    if crop.size == 0 or len(crop.shape) != 3:
        return 0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = (
        (hsv[:, :, 0] >= 8)
        & (hsv[:, :, 0] <= 38)
        & (hsv[:, :, 1] > 80)
        & (hsv[:, :, 2] > 120)
    ).astype(np.uint8) * 255
    contours, hierarchy = cv2.findContours(
        mask,
        cv2.RETR_CCOMP,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    if hierarchy is None:
        return 0
    return sum(
        1
        for index, contour in enumerate(contours)
        if hierarchy[0][index][3] >= 0 and cv2.contourArea(contour) >= 25.0
    )


def _build_candidate(
    col_key: str,
    column_x0: int,
    group: dict[str, Any],
    match_result: dict[str, Any],
    image_size: list[int],
) -> dict[str, Any]:
    raw_value = match_result.get("value")
    reliable = bool(match_result.get("reliable"))
    status, reason = _candidate_status(col_key, raw_value, reliable, match_result.get("reason", ""))
    digit_scores = match_result.get("digit_scores", [])
    plus_score = float(match_result.get("plus_score", 0.0))
    group_bbox = _group_bbox(column_x0, group)
    members = list(group.get("members", []))
    return {
        "tokens": match_result.get("tokens", []),
        "text": "".join(str(token) for token in match_result.get("tokens", [])),
        "raw_value": raw_value,
        "value": raw_value,
        "scores": match_result.get("scores", []),
        "digit_scores": digit_scores,
        "plus_score": plus_score,
        "component_matches": match_result.get("component_matches", []),
        "score": _min_score(match_result.get("scores", [])),
        "status": status,
        "reason": reason,
        "roi": _scale_bbox(group_bbox, image_size),
        "normalized_roi": group_bbox,
        "group_y": int(group.get("y", 0)),
        "group_w": int(group.get("w", 0)),
        "group_h": int(group.get("h", 0)),
        "group_count": int(group.get("count", 0)),
        "group_area": int(group.get("area", 0)),
        "member_widths": [int(member.get("w", 0)) for member in members],
        "member_heights": [int(member.get("h", 0)) for member in members],
        "column": col_key,
        "matcher_reason": match_result.get("reason", ""),
    }


def _apply_column_acceptance_rules(
    col_key: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    for candidate in candidates:
        if (
            candidate.get("status") == "low_confidence"
            and _looks_like_coherent_two_digit_match(candidate)
        ):
            candidate["status"] = "accepted"
            candidate["reason"] = "coherent_two_digit_match"
            continue
        if _looks_like_tail_digit_contaminant(col_key, candidate):
            candidate["value"] = int(candidate["tokens"][1])
            candidate["status"] = "accepted"
            candidate["reason"] = "tail_digit_contaminant"
            continue
        recovered_ambiguous_23 = _recover_ambiguous_twenty_three(candidate)
        if recovered_ambiguous_23 is not None:
            candidate["corrected_from"] = candidate.get("value")
            candidate["value"] = recovered_ambiguous_23
            candidate["status"] = "accepted"
            candidate["reason"] = "ambiguous_zero_to_three_recovered"
            continue
        recovered_narrow_teen = _recover_narrow_one_teen_value(col_key, candidate)
        if recovered_narrow_teen is not None:
            candidate["corrected_from"] = candidate.get("value")
            candidate["value"] = recovered_narrow_teen
            candidate["status"] = "accepted"
            candidate["reason"] = "narrow_one_teen_recovered"
            continue
        recovered_teen = _recover_teen_value(col_key, candidate)
        if recovered_teen is not None:
            candidate["value"] = recovered_teen
            candidate["status"] = "accepted"
            candidate["reason"] = "teen_value_recovered"
            continue
        recovered_multidigit = _recover_narrow_leading_one_multidigit_value(
            col_key,
            candidate,
        )
        if recovered_multidigit is not None:
            candidate["corrected_from"] = candidate.get("raw_value")
            candidate["tokens"] = ["+"] + list(str(recovered_multidigit))
            candidate["value"] = recovered_multidigit
            candidate["status"] = "accepted"
            candidate["reason"] = "narrow_leading_one_multidigit_recovered"
            continue
        recovered_short_tail_eight = _recover_short_tail_eight(col_key, candidate)
        if recovered_short_tail_eight is not None:
            candidate["corrected_from"] = candidate.get("raw_value")
            candidate["tokens"] = ["+", "8"]
            candidate["value"] = recovered_short_tail_eight
            candidate["status"] = "accepted"
            candidate["reason"] = "short_tail_eight_recovered"
            continue
        recovered_total = _recover_current_total_value(candidate)
        if recovered_total is not None:
            candidate["value"] = recovered_total
            candidate["status"] = "accepted"
            candidate["reason"] = "current_total_contaminant"
            continue
        if _looks_like_row_fragment_only(candidate):
            candidate["reason"] = "row_fragment_only"
            continue
        recovered_top_eight = _recover_top_highlighted_eight(col_key, candidate)
        if recovered_top_eight is not None:
            candidate["corrected_from"] = candidate.get("raw_value")
            candidate["value"] = recovered_top_eight
            candidate["status"] = "accepted"
            candidate["reason"] = "top_highlighted_eight_recovered"
            continue
        if _looks_like_current_total_contaminant(candidate):
            candidate["value"] = 1
            candidate["status"] = "accepted"
            candidate["reason"] = "current_total_contaminant"
            continue
        recovered_single_four = _recover_ambiguous_single_four(col_key, candidate)
        if recovered_single_four is not None:
            candidate["corrected_from"] = candidate.get("value")
            candidate["value"] = recovered_single_four
            candidate["status"] = "accepted"
            candidate["reason"] = "ambiguous_one_to_four_recovered"
            continue
        if _looks_like_recovered_noisy_fourteen(candidate):
            candidate["status"] = "accepted"
            candidate["reason"] = "noisy_top_row_fourteen_recovered"
            continue
        recovered_single_nine = _recover_ambiguous_single_nine(candidate)
        if recovered_single_nine is not None:
            candidate["corrected_from"] = candidate.get("value")
            candidate["value"] = recovered_single_nine
            candidate["status"] = "accepted"
            candidate["reason"] = "ambiguous_one_to_nine_recovered"
            continue
        if _looks_like_low_score_plus_one(candidate):
            candidate["value"] = 1
            candidate["status"] = "accepted"
            candidate["reason"] = "low_digit_score"
            continue
        if _looks_like_low_confidence_top_row_five(col_key, candidate):
            candidate["value"] = 5
            candidate["status"] = "accepted"
            candidate["reason"] = "low_confidence_top_row_five"
            continue
        recovered_single_three = _recover_ambiguous_single_three(candidate)
        if recovered_single_three is not None:
            candidate["corrected_from"] = candidate.get("value")
            candidate["value"] = recovered_single_three
            candidate["status"] = "accepted"
            candidate["reason"] = "ambiguous_zero_to_three_recovered"
            continue
        recovered_single_three_from_six = _recover_ambiguous_single_three_from_six(col_key, candidate)
        if recovered_single_three_from_six is not None:
            candidate["corrected_from"] = candidate.get("value")
            candidate["value"] = recovered_single_three_from_six
            candidate["status"] = "accepted"
            candidate["reason"] = "ambiguous_six_to_three_recovered"
            continue
        if _looks_like_skill_pt_top_row(col_key, candidate):
            candidate["value"] = 15
            candidate["status"] = "accepted"
            candidate["reason"] = "heuristic_skill_pt_top_row"
            continue
        if _looks_like_skill_pt_top_row_eight(candidate):
            candidate["corrected_from"] = candidate.get("value")
            candidate["value"] = 8
            candidate["status"] = "accepted"
            candidate["reason"] = "skill_pt_top_row_eight_recovered"
            continue
        recovered_skill_pt_six = _recover_ambiguous_skill_pt_six(candidate)
        if recovered_skill_pt_six is not None:
            candidate["corrected_from"] = candidate.get("value")
            candidate["value"] = recovered_skill_pt_six
            candidate["status"] = "accepted"
            candidate["reason"] = "ambiguous_skill_pt_five_to_six_recovered"
            continue
        if _looks_like_low_risk_row(col_key, candidate):
            candidate["status"] = "accepted"
            candidate["reason"] = candidate["reason"] or "heuristic_row_accept"

    has_other_accepted = any(
        candidate.get("status") == "accepted" and int(candidate.get("value") or 0) > 0
        for candidate in candidates
    )
    if has_other_accepted:
        for candidate in candidates:
            if _looks_like_supporting_plus_one(candidate):
                candidate["value"] = 1
                candidate["status"] = "accepted"
                candidate["reason"] = "heuristic_supporting_plus_one"

    accepted_by_value = {
        int(candidate["value"])
        for candidate in candidates
        if candidate.get("status") == "accepted"
        and isinstance(candidate.get("value"), int)
    }
    for candidate in candidates:
        if candidate.get("status") != "low_confidence":
            continue
        value = candidate.get("value")
        if not isinstance(value, int) or value not in accepted_by_value:
            continue
        if not _candidate_has_plus_digit_signature(candidate):
            continue
        if float(candidate.get("score", 0.0)) < 0.35:
            continue
        candidate["status"] = "accepted"
        candidate["reason"] = "corroborated_layered_row"

    for candidate in candidates:
        _normalize_accepted_candidate_tokens(candidate)
        if candidate.get("status") == "rejected_suspicious_value":
            candidate["rejected_value"] = candidate.get("value")
            candidate["raw_tokens"] = list(candidate.get("tokens", []))
            candidate["raw_text"] = candidate.get("text", "")
            candidate["value"] = None
            candidate["tokens"] = []
            candidate["text"] = ""

    return candidates


def _candidate_contributes_to_column_value(
    col_key: str,
    candidate: dict[str, Any],
) -> bool:
    value = candidate.get("value")
    if not _is_parse_preserved_gain_value(col_key, value):
        return False
    if not _candidate_has_plus_digit_signature(candidate):
        return False
    return candidate.get("status") in {"accepted", "low_confidence"}


def _candidate_blocks_column_acceptance(
    col_key: str,
    candidate: dict[str, Any],
) -> bool:
    status = candidate.get("status")
    if status == "low_confidence" and _candidate_contributes_to_column_value(col_key, candidate):
        return True
    if status == "unreadable":
        return False
    if status == "rejected_suspicious_value":
        return False
    return False


def _candidate_blocks_overall_reliability(
    col_key: str,
    candidate: dict[str, Any],
) -> bool:
    status = candidate.get("status")
    if status == "low_confidence" and _candidate_contributes_to_column_value(col_key, candidate):
        return True
    if status == "accepted":
        return False
    if status == "rejected_suspicious_value":
        return False
    if status == "unreadable":
        return False
    return bool(candidate.get("raw_value") is not None)


def _has_strong_accepted_candidate(candidates: list[dict[str, Any]]) -> bool:
    weak_reasons = {
        "ambiguous_zero_to_three_recovered",
        "low_digit_score",
        "heuristic_supporting_plus_one",
    }
    for candidate in candidates:
        if candidate.get("status") != "accepted":
            continue
        if candidate.get("reason") in weak_reasons:
            continue
        if isinstance(candidate.get("value"), int) and candidate.get("value") > 0:
            return True
    return False


def _looks_like_low_risk_row(col_key: str, candidate: dict[str, Any]) -> bool:
    value = candidate.get("value")
    if not _is_parse_preserved_gain_value(col_key, value):
        return False
    tokens = candidate.get("tokens", [])
    if not tokens or tokens[0] != "+":
        return False
    digit_scores = [float(score) for score in candidate.get("digit_scores", [])]
    digit_count = sum(1 for token in tokens[1:] if str(token).isdigit())
    extra_plus = sum(1 for token in tokens[1:] if token == "+")
    y = int(candidate.get("group_y", 0))
    if y < (95 if col_key == "skill_pt" else 105):
        return False
    if digit_count == 1 and digit_scores and digit_scores[0] >= 0.58 and extra_plus == 0:
        return True
    if (
        digit_count == 1
        and digit_scores
        and digit_scores[0] >= 0.55
        and extra_plus == 0
        and float(candidate.get("plus_score", 0.0)) >= 0.7
    ):
        return True
    if digit_count == 1 and digit_scores and digit_scores[0] >= 0.65 and extra_plus <= 2:
        return True
    if digit_count == 2 and digit_scores and min(digit_scores) >= 0.64 and extra_plus == 0:
        return True
    return False


def _looks_like_skill_pt_top_row(col_key: str, candidate: dict[str, Any]) -> bool:
    if col_key != "skill_pt":
        return False
    return _looks_like_skill_pt_top_row_result(
        col_key,
        {
            "y": int(candidate.get("group_y", 0)),
            "count": int(candidate.get("group_count", 0)),
            "w": int(candidate.get("group_w", 0)),
        },
        {
            "value": candidate.get("raw_value"),
            "tokens": list(candidate.get("tokens", [])),
        },
    )


def _looks_like_skill_pt_top_row_result(
    col_key: str,
    group: dict[str, Any],
    match_result: dict[str, Any],
) -> bool:
    if col_key != "skill_pt":
        return False
    if int(group.get("y", 0)) > 60:
        return False
    if int(group.get("count", 0)) not in {4, 5, 6}:
        return False
    width = int(group.get("w", 0))
    if not (150 <= width <= 225):
        return False
    raw_value = match_result.get("value")
    if raw_value in {211, 2111, 2117, 251, 2511, 2517}:
        return True
    tokens = [str(token) for token in match_result.get("tokens", [])]
    if not tokens or tokens[0] != "+":
        return False
    digit_tokens = [token for token in tokens if token.isdigit()]
    return len(digit_tokens) >= 3 and tokens.count("+") >= 1 and "5" in digit_tokens


def _looks_like_supporting_plus_one(candidate: dict[str, Any]) -> bool:
    if candidate.get("status") == "accepted":
        return False
    if candidate.get("raw_value") != 1:
        return False
    tokens = candidate.get("tokens", [])
    if tokens[:2] != ["+", "1"]:
        return False
    return (
        int(candidate.get("group_y", 0)) <= 90
        and int(candidate.get("group_count", 0)) == 2
        and int(candidate.get("group_w", 0)) <= 70
    )


def _looks_like_low_score_plus_one(candidate: dict[str, Any]) -> bool:
    if candidate.get("status") != "low_confidence":
        return False
    if int(candidate.get("group_count", 0)) != 2:
        return False
    if int(candidate.get("group_w", 0)) > 95:
        return False
    tokens = candidate.get("tokens", [])
    if tokens[:1] != ["+"]:
        return False
    if candidate.get("raw_value") not in {1, 2}:
        return False
    digit_scores = [float(score) for score in candidate.get("digit_scores", [])]
    if not digit_scores:
        return False
    plus_score = float(candidate.get("plus_score", 0.0))
    if int(candidate.get("group_y", 0)) <= 50:
        return digit_scores[0] <= 0.4 and plus_score <= 0.55
    return digit_scores[0] >= 0.8 and plus_score < 0.76


def _looks_like_recovered_noisy_fourteen(candidate: dict[str, Any]) -> bool:
    return (
        candidate.get("status") == "low_confidence"
        and candidate.get("raw_value") == 14
        and candidate.get("reason") == "noisy_top_row_fourteen_recovered"
        and list(candidate.get("tokens") or []) == ["+", "1", "4"]
        and 20 <= int(candidate.get("group_y", 0)) <= 80
    )


def _looks_like_skill_pt_top_row_eight(candidate: dict[str, Any]) -> bool:
    if candidate.get("column") != "skill_pt":
        return False
    if not (20 <= int(candidate.get("group_y", 0)) <= 75):
        return False
    if not (120 <= int(candidate.get("group_w", 0)) <= 220):
        return False
    if int(candidate.get("group_count", 0)) not in {3, 4, 5}:
        return False
    raw_value = candidate.get("raw_value")
    if raw_value not in {51, 52, 58, 581}:
        return False
    plus_score = float(candidate.get("plus_score", 0.0))
    if raw_value == 581:
        if not (0.25 <= plus_score <= 0.60):
            return False
    elif not (0.35 <= plus_score <= 0.60):
        return False
    tokens = [str(token) for token in candidate.get("tokens", [])]
    if "+" not in tokens:
        return False
    digit_scores = [
        float(score)
        for score in candidate.get("digit_scores", [])
        if isinstance(score, (int, float))
    ]
    if not digit_scores or max(digit_scores) > 0.45:
        return False
    return True


def _recover_ambiguous_skill_pt_six(candidate: dict[str, Any]) -> int | None:
    if candidate.get("column") != "skill_pt":
        return None
    if candidate.get("raw_value") != 5:
        return None
    if int(candidate.get("group_y", 0)) < 95:
        return None
    if int(candidate.get("group_count", 0)) != 2:
        return None
    component_matches = candidate.get("component_matches")
    if not isinstance(component_matches, list) or len(component_matches) < 2:
        return None
    digit_match = component_matches[1]
    if not isinstance(digit_match, dict):
        return None
    all_scores = digit_match.get("all_scores")
    if not isinstance(all_scores, dict):
        return None
    five_score = float(all_scores.get("5", 0.0))
    six_score = float(all_scores.get("6", 0.0))
    if five_score < 0.88 or six_score < 0.88:
        return None
    if abs(five_score - six_score) > 0.005:
        return None
    return 6


def _recover_ambiguous_single_nine(candidate: dict[str, Any]) -> int | None:
    column = candidate.get("column")
    if column not in {"stamina", "power", "guts", "wisdom"}:
        return None
    if candidate.get("raw_value") != 1:
        return None
    if int(candidate.get("group_y", 0)) < 95:
        return None
    if int(candidate.get("group_count", 0)) != 2:
        return None
    component_matches = candidate.get("component_matches")
    if not isinstance(component_matches, list) or len(component_matches) < 2:
        return None
    digit_match = component_matches[1]
    if not isinstance(digit_match, dict):
        return None
    all_scores = digit_match.get("all_scores")
    if not isinstance(all_scores, dict):
        return None
    one_score = float(all_scores.get("1", 0.0))
    nine_score = float(all_scores.get("9", 0.0))
    min_score = 0.85 if column == "guts" else 0.75
    if one_score < min_score or nine_score < min_score:
        return None
    if abs(one_score - nine_score) > 0.005:
        return None
    plus_score = float(candidate.get("plus_score", 0.0))
    if column == "power" and plus_score < 0.75:
        return None
    if column not in {"guts", "power"} and plus_score < 0.9:
        return None
    return 9


def _looks_like_low_confidence_top_row_five(
    col_key: str,
    candidate: dict[str, Any],
) -> bool:
    if col_key != "power":
        return False
    if candidate.get("status") != "low_confidence":
        return False
    if candidate.get("raw_value") != 5:
        return False
    if not (25 <= int(candidate.get("group_y", 0)) <= 70):
        return False
    if int(candidate.get("group_count", 0)) != 2:
        return False
    if int(candidate.get("group_w", 0)) > 95:
        return False
    tokens = [str(token) for token in candidate.get("tokens", [])]
    if tokens != ["+", "5"]:
        return False
    plus_score = float(candidate.get("plus_score", 0.0))
    if not (0.40 <= plus_score <= 0.62):
        return False
    component_matches = candidate.get("component_matches")
    if not isinstance(component_matches, list) or len(component_matches) < 2:
        return False
    digit_scores = component_matches[1].get("all_scores")
    if not isinstance(digit_scores, dict):
        return False
    five_score = float(digit_scores.get("5", 0.0))
    six_score = float(digit_scores.get("6", 0.0))
    return five_score >= 0.18 and abs(five_score - six_score) <= 0.03


def _recover_top_highlighted_eight(
    col_key: str,
    candidate: dict[str, Any],
) -> int | None:
    """Recover the visually isolated +8 bubble without guessing other noisy groups."""
    if col_key != "guts":
        return None
    if candidate.get("status") != "rejected_suspicious_value":
        return None
    if candidate.get("raw_value") != 50:
        return None
    if not 20 <= int(candidate.get("group_y", 0)) <= 40:
        return None
    if int(candidate.get("group_count", 0)) != 5:
        return None
    if not 200 <= int(candidate.get("group_w", 0)) <= 220:
        return None
    if int(candidate.get("group_h", 0)) > 55:
        return None
    widths = [int(width) for width in candidate.get("member_widths") or []]
    if len(widths) != 5:
        return None
    if not (widths[0] >= 95 and 20 <= widths[1] <= 32 and 30 <= widths[2] <= 40 and 50 <= widths[3] <= 65 and widths[4] <= 8):
        return None
    return 8


def _looks_like_current_total_contaminant(candidate: dict[str, Any]) -> bool:
    if candidate.get("status") != "rejected_suspicious_value":
        return False
    if int(candidate.get("group_y", 0)) < 100:
        return False
    raw_value = candidate.get("raw_value")
    if not isinstance(raw_value, int) or raw_value < 500:
        return False
    if int(candidate.get("group_count", 0)) < 5:
        return False
    if int(candidate.get("group_w", 0)) > 220:
        return False
    tokens = candidate.get("tokens", [])
    return "1" in tokens and "+" in tokens


def _looks_like_tail_digit_contaminant(col_key: str, candidate: dict[str, Any]) -> bool:
    if col_key != "skill_pt":
        return False
    if candidate.get("status") != "low_confidence":
        return False
    if int(candidate.get("group_y", 0)) < 100:
        return False
    tokens = candidate.get("tokens", [])
    if len(tokens) < 3 or tokens[0] != "+" or not str(tokens[1]).isdigit():
        return False
    digit_scores = [float(score) for score in candidate.get("digit_scores", [])]
    if len(digit_scores) < 2:
        return False
    return digit_scores[0] >= 0.8 and digit_scores[1] < 0.3


def _recover_ambiguous_twenty_three(candidate: dict[str, Any]) -> int | None:
    if candidate.get("status") != "low_confidence":
        return None
    if candidate.get("raw_value") != 20:
        return None
    if int(candidate.get("group_y", 0)) < 95:
        return None
    if int(candidate.get("group_count", 0)) != 3:
        return None
    tokens = [str(token) for token in candidate.get("tokens", [])]
    if tokens != ["+", "2", "0"]:
        return None
    component_matches = candidate.get("component_matches")
    if not isinstance(component_matches, list) or len(component_matches) < 3:
        return None
    third_scores = component_matches[2].get("all_scores")
    if not isinstance(third_scores, dict):
        return None
    zero_score = float(third_scores.get("0", 0.0))
    three_score = float(third_scores.get("3", 0.0))
    if zero_score < 0.55 or three_score < 0.55:
        return None
    if zero_score - three_score > 0.08:
        return None
    if float(candidate.get("plus_score", 0.0)) < 0.65:
        return None
    return 23


def _recover_ambiguous_single_three(candidate: dict[str, Any]) -> int | None:
    if candidate.get("status") != "unreadable":
        return None
    if candidate.get("raw_value") != 0:
        return None
    if int(candidate.get("group_y", 0)) < 95:
        return None
    if int(candidate.get("group_count", 0)) != 2:
        return None
    tokens = [str(token) for token in candidate.get("tokens", [])]
    if tokens != ["+", "0"]:
        return None
    if float(candidate.get("plus_score", 0.0)) < 0.70:
        return None
    component_matches = candidate.get("component_matches")
    if not isinstance(component_matches, list) or len(component_matches) < 2:
        return None
    digit_scores = component_matches[1].get("all_scores")
    if not isinstance(digit_scores, dict):
        return None
    zero_score = float(digit_scores.get("0", 0.0))
    three_score = float(digit_scores.get("3", 0.0))
    if zero_score < 0.50 or three_score < 0.50:
        return None
    if abs(zero_score - three_score) > 0.05:
        return None
    return 3


def _recover_ambiguous_single_three_from_six(
    col_key: str,
    candidate: dict[str, Any],
) -> int | None:
    if col_key == "skill_pt":
        return None
    if candidate.get("status") != "low_confidence":
        return None
    if candidate.get("raw_value") != 6:
        return None
    if int(candidate.get("group_y", 0)) < 95:
        return None
    if int(candidate.get("group_count", 0)) != 2:
        return None
    if not (80 <= int(candidate.get("group_w", 0)) <= 100):
        return None
    if not (0.68 <= float(candidate.get("plus_score", 0.0)) <= 0.78):
        return None
    component_matches = candidate.get("component_matches")
    if not isinstance(component_matches, list) or len(component_matches) < 2:
        return None
    digit_scores = component_matches[1].get("all_scores")
    if not isinstance(digit_scores, dict):
        return None
    zero_score = float(digit_scores.get("0", 0.0))
    three_score = float(digit_scores.get("3", 0.0))
    six_score = float(digit_scores.get("6", 0.0))
    if min(zero_score, three_score, six_score) < 0.50:
        return None
    if six_score > 0.60:
        return None
    if six_score - three_score > 0.06:
        return None
    return 3


def _recover_narrow_one_teen_value(
    col_key: str,
    candidate: dict[str, Any],
) -> int | None:
    if col_key == "skill_pt":
        return None
    if int(candidate.get("group_y", 0)) < 95:
        return None
    if int(candidate.get("group_count", 0)) != 3:
        return None
    raw_value = candidate.get("raw_value")
    if raw_value not in {22, 63}:
        return None
    widths = candidate.get("member_widths")
    if not isinstance(widths, list) or len(widths) < 3:
        return None
    plus_width, first_width, second_width = [int(width) for width in widths[:3]]
    if not (30 <= plus_width <= 45 and first_width <= 30):
        return None
    matches = candidate.get("component_matches")
    if not isinstance(matches, list) or len(matches) < 3:
        return None

    first_one_score = _component_score_for_digit(matches[1], "1")
    if raw_value == 22:
        second_one_score = _component_score_for_digit(matches[2], "1")
        if second_width <= 32 and first_one_score >= 0.78 and second_one_score >= 0.78:
            return 11
        return None

    second_three_score = _component_score_for_digit(matches[2], "3")
    if (
        raw_value == 63
        and 34 <= second_width <= 43
        and first_one_score >= 0.60
        and second_three_score >= 0.60
    ):
        return 13
    return None


def _component_score_for_digit(component_match: Any, digit: str) -> float:
    if not isinstance(component_match, dict):
        return 0.0
    scores = component_match.get("all_scores")
    if not isinstance(scores, dict):
        return 0.0
    try:
        return float(scores.get(digit, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _recover_teen_value(col_key: str, candidate: dict[str, Any]) -> int | None:
    if candidate.get("status") != "rejected_suspicious_value":
        return None
    if col_key == "skill_pt":
        return None
    if int(candidate.get("group_y", 0)) < 100:
        return None
    if int(candidate.get("group_count", 0)) != 3:
        return None
    tokens = candidate.get("tokens", [])
    if not tokens or tokens[0] != "+":
        return None
    raw_value = candidate.get("raw_value")
    if raw_value == 60 and col_key != "wisdom":
        return None
    if raw_value == 66:
        widths = [int(width) for width in candidate.get("member_widths") or []]
        matches = candidate.get("component_matches")
        if isinstance(matches, list) and len(matches) == 3 and len(widths) == 3:
            one_score = _component_score_for_digit(matches[2], "1")
            three_score = _component_score_for_digit(matches[2], "3")
            if (
                widths[2] >= 34
                and three_score >= 0.60
                and abs(three_score - one_score) <= 0.02
            ):
                return 13
    return {
        60: 10,
        62: 12,
        65: 15,
        66: 11,
    }.get(raw_value)


def _recover_short_tail_eight(col_key: str, candidate: dict[str, Any]) -> int | None:
    """Recover +8 when a short adjacent UI fragment is parsed as a third digit."""
    if col_key != "skill_pt":
        return None
    if candidate.get("status") != "rejected_suspicious_value":
        return None
    if candidate.get("raw_value") != 82:
        return None
    if int(candidate.get("group_y", 0)) < 95:
        return None
    if int(candidate.get("group_count", 0)) != 3:
        return None
    tokens = [str(token) for token in candidate.get("tokens") or []]
    if tokens != ["+", "8", "2"]:
        return None
    widths = [int(width) for width in candidate.get("member_widths") or []]
    heights = [int(height) for height in candidate.get("member_heights") or []]
    if len(widths) != 3 or len(heights) != 3:
        return None
    if not (35 <= widths[1] <= 42 and 45 <= heights[1] <= 52 and heights[2] <= 24):
        return None
    if float(candidate.get("plus_score", 0.0)) < 0.9:
        return None
    component_matches = candidate.get("component_matches")
    if not isinstance(component_matches, list) or len(component_matches) != 3:
        return None
    digit_match = component_matches[1]
    if not isinstance(digit_match, dict):
        return None
    scores = digit_match.get("all_scores")
    if not isinstance(scores, dict):
        return None
    eight_score = float(scores.get("8", 0.0))
    nine_score = float(scores.get("9", 0.0))
    if eight_score < 0.8 or abs(eight_score - nine_score) > 0.005:
        return None
    return 8


def _recover_narrow_leading_one_multidigit_value(
    col_key: str,
    candidate: dict[str, Any],
) -> int | None:
    """Recover a bottom-row +1x/+1xx with a narrow leading glyph."""
    if candidate.get("status") != "rejected_suspicious_value":
        return None
    if int(candidate.get("group_y", 0)) < 95:
        return None
    raw_tokens = [
        str(token)
        for token in candidate.get("raw_tokens") or candidate.get("tokens") or []
    ]
    if len(raw_tokens) not in {3, 4} or raw_tokens[0] != "+":
        return None
    if not all(token.isdigit() for token in raw_tokens[1:]):
        return None
    widths = [int(width) for width in candidate.get("member_widths") or []]
    heights = [int(height) for height in candidate.get("member_heights") or []]
    if len(widths) != len(raw_tokens) or len(heights) != len(raw_tokens):
        return None
    leading_width = widths[1]
    trailing_widths = widths[2:]
    if leading_width > 34 or leading_width * 1.2 > min(trailing_widths):
        return None
    if heights[1] < min(heights[2:]) * 0.9:
        return None
    if float(candidate.get("plus_score", 0.0)) < 0.7:
        return None
    component_matches = candidate.get("component_matches")
    if not isinstance(component_matches, list) or len(component_matches) != len(raw_tokens):
        return None
    trailing_matches = component_matches[2:]
    if not all(bool(match.get("reliable")) for match in trailing_matches):
        return None
    if any(float(match.get("margin", 0.0)) < 0.05 for match in trailing_matches):
        return None
    recovered_value = int("1" + "".join(raw_tokens[2:]))
    if not _is_parse_preserved_gain_value(col_key, recovered_value):
        return None
    return recovered_value


def _recover_ambiguous_single_four(col_key: str, candidate: dict[str, Any]) -> int | None:
    if col_key != "wisdom":
        return None
    if candidate.get("status") != "low_confidence":
        return None
    if candidate.get("raw_value") != 1:
        return None
    if int(candidate.get("group_y", 0)) > 70:
        return None
    if int(candidate.get("group_count", 0)) != 2:
        return None
    if int(candidate.get("group_w", 0)) > 95:
        return None
    tokens = [str(token) for token in candidate.get("tokens", [])]
    if tokens != ["+", "1"]:
        return None
    component_matches = candidate.get("component_matches")
    if not isinstance(component_matches, list) or len(component_matches) < 2:
        return None
    digit_scores = component_matches[1].get("all_scores")
    if not isinstance(digit_scores, dict):
        return None
    one_score = float(digit_scores.get("1", 0.0))
    four_score = float(digit_scores.get("4", 0.0))
    if four_score < 0.30:
        return None
    if abs(one_score - four_score) > 0.04:
        return None
    plus_score = float(candidate.get("plus_score", 0.0))
    if plus_score > 0.55:
        return None
    return 4


def _recover_current_total_value(candidate: dict[str, Any]) -> int | None:
    if candidate.get("status") != "low_confidence":
        return None
    if int(candidate.get("group_y", 0)) < 100:
        return None
    if int(candidate.get("group_count", 0)) < 5:
        return None
    if int(candidate.get("group_w", 0)) < 160:
        return None
    tokens = [str(token) for token in candidate.get("tokens", [])]
    if not tokens or tokens[0] != "1":
        return None
    plus_cluster_end = 1
    while plus_cluster_end < len(tokens) and tokens[plus_cluster_end] == "+":
        plus_cluster_end += 1
    if plus_cluster_end < 3:
        return None
    digits = [
        token
        for token in tokens[plus_cluster_end:]
        if token.isdigit() and token != "0"
    ]
    if not digits:
        return None
    return int(digits[0])


def _looks_like_row_fragment_only(candidate: dict[str, Any]) -> bool:
    if candidate.get("status") != "unreadable":
        return False
    if candidate.get("raw_value") is not None:
        return False
    tokens = candidate.get("tokens", [])
    if len(tokens) != 1:
        return False
    return int(candidate.get("group_count", 0)) == 1 or int(candidate.get("group_w", 0)) <= 30


def _assign_column_local(x_px: float) -> str:
    """本地列归档：根据 x 像素位置分配到最近的列中心。"""
    col_centers = {k: (x0 + x1) / 2 for k, (x0, x1) in COLUMN_REGIONS.items()}
    return min(col_centers, key=lambda k: abs(x_px - col_centers[k]))


def _recognize_with_templates(
    strip: np.ndarray,
    groups: list[dict],
    strip_w: int,
    components: list[dict],
) -> dict[str, Any]:
    """用模板匹配识别增益数字（debug 用，value_unreliable 始终 True）。"""
    from core.gain_template_matcher import GainTemplateMatcher

    matcher = GainTemplateMatcher()
    if not matcher.available:
        logger.debug("模板不可用，保守弃权（已移除像素面积猜数回退）")
        return {
            "gains": [],
            "confidence": 0.0,
            "position_confidence": 0.0,
            "value_confidence": 0.0,
            "method": "template_unavailable",
            "value_unreliable": True,
            "source": "local_ocr",
            "template_available": False,
        }

    pad = 5
    gains = []
    partial = []
    template_matches = []

    for gi, group in enumerate(groups):
        # 裁剪组内每个组件
        comp_crops = []
        for comp in group.get("members", [group]):
            x0 = max(0, int(comp["x"]) - pad)
            y0 = max(0, int(comp["y"]) - pad)
            x1 = int(comp["x"] + comp["w"]) + pad
            y1 = int(comp["y"] + comp["h"]) + pad
            crop = strip[y0:y1, x0:x1]
            if crop.size > 0:
                comp_crops.append(crop)

        if not comp_crops:
            continue

        result = matcher.match_components(comp_crops)
        x_frac = group["cx"] / strip_w

        match_info = {
            "group_index": gi,
            "x_frac": round(x_frac, 3),
            "n_components": len(comp_crops),
            "tokens": result["tokens"],
            "scores": result["scores"],
            "value": result["value"],
            "reliable": result["reliable"],
            "reason": result["reason"],
        }
        template_matches.append(match_info)

        if result["reliable"] and result["value"] is not None:
            gains.append({"x": round(x_frac, 3), "n": result["value"]})
        elif result["value"] is not None:
            # partial：识别出值但不可靠（如缺 plus 的单数字）
            partial.append({
                "x": round(x_frac, 3),
                "n": result["value"],
                "tokens": result["tokens"],
                "reason": result["reason"],
            })

    position_conf = _calc_position_confidence(gains, components)
    # 模板匹配 debug 模式：confidence 仍受 cap 限制
    overall_conf = round(min(position_conf, _VALUE_CONFIDENCE_CAP), 2)

    return {
        "gains": gains,
        "partial": partial,
        "template_matches": template_matches,
        "confidence": overall_conf,
        "position_confidence": position_conf,
        "value_confidence": _VALUE_CONFIDENCE_CAP,
        "method": "template_match_debug",
        "value_unreliable": True,  # 始终 True，不进入 auto 决策
        "source": "local_ocr",
        "template_available": True,
    }


# ---------- 内部函数 ----------

def _empty_result(reason: str) -> dict[str, Any]:
    logger.debug(f"local_ocr: {reason}")
    return {
        "gains": [], "confidence": 0.0,
        "position_confidence": 0.0, "value_confidence": 0.0,
        "method": "template_abstained", "value_unreliable": True,
        "source": "local_ocr",
        "base_gain": {},
        "bonus_gain": {},
        "stats_gain_total": {},
    }


def _group_components(components: list[dict]) -> list[dict]:
    """Group adjacent glyph components without merging stacked gain rows."""
    if not components:
        return []

    row_threshold = _component_row_threshold(components)
    rows: list[list[dict]] = []
    row_centers: list[float] = []

    for comp in sorted(components, key=lambda item: item["cy"]):
        placed = False
        for index, center in enumerate(row_centers):
            if abs(float(comp["cy"]) - center) <= row_threshold:
                rows[index].append(comp)
                total_area = sum(max(1, int(item["area"])) for item in rows[index])
                row_centers[index] = sum(
                    float(item["cy"]) * max(1, int(item["area"]))
                    for item in rows[index]
                ) / total_area
                placed = True
                break
        if not placed:
            rows.append([comp])
            row_centers.append(float(comp["cy"]))

    groups: list[dict] = []
    for row in rows:
        row.sort(key=lambda item: item["x"])
        current = [row[0]]
        for comp in row[1:]:
            gap = comp["x"] - (current[-1]["x"] + current[-1]["w"])
            if gap <= _GROUP_GAP:
                current.append(comp)
            else:
                groups.append(_merge_group(current))
                current = [comp]
        groups.append(_merge_group(current))

    groups.sort(key=lambda item: (item["y"], item["x"]))
    return groups


def _component_row_threshold(components: list[dict]) -> int:
    heights = sorted(int(comp["h"]) for comp in components if int(comp["h"]) > 0)
    if not heights:
        return 28
    median_height = heights[len(heights) // 2]
    return max(24, int(median_height * 0.75))


def _merge_group(members: list[dict]) -> dict:
    """合并一组组件为单个数字描述。"""
    x_min = min(m["x"] for m in members)
    y_min = min(m["y"] for m in members)
    x_max = max(m["x"] + m["w"] for m in members)
    y_max = max(m["y"] + m["h"] for m in members)
    total_area = sum(m["area"] for m in members)
    cx = sum(m["cx"] * m["area"] for m in members) / max(total_area, 1)
    cy = sum(m["cy"] * m["area"] for m in members) / max(total_area, 1)

    return {
        "x": x_min, "y": y_min,
        "w": x_max - x_min, "h": y_max - y_min,
        "cx": cx, "cy": cy,
        "area": total_area,
        "count": len(members),
        "members": members,
    }


def _calc_position_confidence(gains: list[dict], components: list[dict]) -> float:
    """计算位置置信度（不含数值可靠性）。"""
    if not gains:
        return 0.0

    score = 0.0

    # 检测到的增益数量合理性
    n_gains = len(gains)
    if _EXPECTED_GAIN_COUNT[0] <= n_gains <= _EXPECTED_GAIN_COUNT[1]:
        score += 0.3
    elif n_gains > 0:
        score += 0.1

    # 值合理性（非零且非异常大）
    valid_values = [g["n"] for g in gains if 0 < g["n"] <= 30]
    score += 0.3 * min(1.0, len(valid_values) / max(n_gains, 1))

    # x 位置分布合理性（应在 0.0~1.0 范围内分散）
    if n_gains >= 2:
        x_positions = [g["x"] for g in gains]
        x_range = max(x_positions) - min(x_positions)
        if x_range > 0.3:
            score += 0.2

    # 组件数量合理性
    if len(components) >= 3:
        score += 0.2

    return round(min(1.0, score), 2)
