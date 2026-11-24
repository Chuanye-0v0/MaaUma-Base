"""Text hints for screen-state recognition and safety checks.

This module does not perform OCR by itself. It normalizes text produced by any
OCR backend, manual review, or multimodal reader into compact hints that can be
combined with local OpenCV perception.
"""

from __future__ import annotations

import re
from typing import Any

from core.grand_masters_overlay import text_has_grand_masters_crystal_reward


RESOURCE_TERMS = (
    "胡萝卜",
    "萝卜",
    "闹钟",
    "时钟",
    "ジュエル",
    "宝石",
    "付费",
    "有偿",
    "TP",
    "RP",
    "恢复体力",
    "活动加速",
    "商店",
    "商城",
    "抽卡",
    "招募",
    "卡池",
    "ガチャ",
    "募集",
    "扭蛋",
    "购买",
    "购入",
    "充值",
    "支付",
    "恢复TP",
    "恢复RP",
)


_RACE_PARTICIPANT_COMPOUND_EXCLUSIONS = (
    "参赛条件",
    "参赛计划",
    "参赛确认",
    "参赛资格",
    "参赛人数",
    "参赛限制",
    "参赛要求",
    "参赛马娘",
    "参赛选手",
)

_SCREEN_PATTERNS: tuple[dict[str, Any], ...] = (
    {
        "screen": "race_detail_confirm",
        "confidence": 0.90,
        "terms": ("要参加比赛吗", "赛事详情"),
        "boost_terms": ("取消", "参赛"),
        "reason": "race_detail_confirm_text",
    },
    {
        "screen": "race_result_ready",
        "confidence": 0.76,
        "terms": ("查看结果", "比赛结果", "结果"),
        "reason": "race_result_text",
    },
    {
        "screen": "race_result_ready",
        "confidence": 0.82,
        "terms": ("第1名", "第2名", "第3名", "結果"),
        "reason": "race_result_rank_text",
    },
    {
        "screen": "race_participant_list",
        "confidence": 0.74,
        "terms": ("参赛!", "参赛"),
        "boost_terms": ("人气第", "居中", "领跑", "跟前", "差行", "后追"),
        "reason": "race_participant_text",
    },
    {
        "screen": "training_menu",
        "confidence": 0.70,
        "terms": ("失败率", "训练", "体力"),
        "reason": "training_menu_text",
    },
    {
        "screen": "event_choice",
        "confidence": 0.72,
        "terms": ("选择", "上", "下"),
        "boost_terms": ("事件", "效果"),
        "reason": "event_choice_text",
    },
    {
        "screen": "training_failed",
        "confidence": 0.86,
        "terms": ("目标未达成", "育成结束", "未达成"),
        "reason": "training_failure_text",
    },
)


def analyze_screen_text(value: Any) -> dict[str, Any]:
    """Analyze visible/OCR text and return safety + screen-state hints."""
    visible_text = _flatten_text(value)
    normalized = _normalize_text(visible_text)
    resource_terms = _matched_terms(normalized, RESOURCE_TERMS)
    resource_terms = _filter_contextual_resource_false_positives(
        normalized,
        resource_terms,
    )
    if "\u65f6\u949f\u4e4b\u9488" in normalized:
        resource_terms = [
            term for term in resource_terms if term != "\u65f6\u949f"
        ]

    compact = "".join(normalized.split())
    lines = {line.strip() for line in normalized.splitlines()}
    retry_prompt = (any(term in compact for term in ("闹钟", "时钟"))
                    and (any(term in compact for term in ("重新挑战", "再次挑战", "继续挑战"))
                         or ("取消" in lines and bool(lines & {"购买", "使用", "重试", "重新挑战"}))))
    factor_redraw = (any(term in compact for term in ("重新获得因子", "重新获取因子"))
                     and any(term in compact for term in ("消耗", "花费", "所需")))
    if factor_redraw and not resource_terms:
        resource_terms = ["因子重抽"]
    # The small top-left title is sometimes missed while the date and the
    # rest of the career HUD are clear. Do not turn story mentions such as
    # 商店街 into a shop prompt solely because that one OCR label is absent.
    career_header = (bool(lines & {"养成", "育成", "训练"}) or bool(re.search(
        r"(?:初级|经典级|高级)(?:\d{1,2}月[上下]半月|出道前)", compact)))
    career_hud = (career_header
                  and all(term in compact for term in ("目标", "体力", "回合"))
                  and any(term in compact for term in ("跳过", "缩短版")))
    advice_header = "手纲小姐的建议" in lines
    spend_buttons = bool(lines & {"购买", "购入", "充值", "支付", "使用", "确认", "确定", "取消"})
    if (career_header and "萝卜磨成泥" in compact
            and all(term in compact for term in ("体力", "训练", "比赛"))):
        # The career menu can hide its goal line. Ignore this specific story
        # phrase, while preserving other resource text on the same frame.
        outside_story = re.sub(r"萝\s*卜\s*磨\s*成\s*泥", "", normalized)
        career_hud = not (
            spend_buttons
            or any(term in compact for term in ("使用", "消耗", "花费"))
            or _matched_terms(outside_story, RESOURCE_TERMS)
        )
    loading_advice = advice_header and not (spend_buttons or retry_prompt or factor_redraw)
    skill_spend_controls = (
        bool(lines & {"购买", "购入", "充值", "支付", "使用", "取消"})
        or any(term in compact for term in ("使用", "消耗", "花费", "补充", "是否恢复"))
    )
    skill_description_recovery = (
        set(resource_terms) == {"恢复体力"}
        and not (skill_spend_controls or retry_prompt or factor_redraw)
        and any(term in compact for term in ("启发等级", "已获得", "当前技能点数"))
        and any(term in compact for term in ("赛事", "弯道", "直道", "梯队", "%OFF", "跳过", "缩短版"))
    )
    keyword_policy = ("disabled_in_career" if career_hud and not (retry_prompt or factor_redraw)
                      else "disabled_in_loading_advice" if loading_advice
                      else "disabled_in_skill_list" if skill_description_recovery
                      else "enabled")
    # Story words are not spend controls. Career actions still require their
    # own page/layout contract; retry and factor-redraw prompts remain blocked.
    matched_resource_terms = list(resource_terms)
    if keyword_policy in {"disabled_in_career", "disabled_in_loading_advice", "disabled_in_skill_list"}:
        resource_terms = []

    hints: list[dict[str, Any]] = []
    if loading_advice:
        # A loading tip can mention shops and items. It grants only a wait,
        # never authority to tap a purchase or a resource confirmation.
        hints.append({"screen": "loading", "confidence": 0.96,
                      "matched_terms": ["手纲小姐的建议"], "reason": "loading_advice_text"})
    if resource_terms:
        hints.append(
            {
                "screen": "resource_prompt",
                "confidence": 0.97,
                "matched_terms": resource_terms,
                "reason": "paid_or_rare_resource_text",
            }
        )
    if text_has_grand_masters_crystal_reward(visible_text):
        hints.append(
            {
                "screen": "scenario_reward_continue",
                "confidence": 0.94,
                "matched_terms": ["grand_masters_crystal_reward"],
                "reason": "grand_masters_crystal_reward_text",
            }
        )
    title_phrase = "点击屏幕开始" in normalized
    title_signature = bool(
        "cygames" in normalized.lower()
        and re.search(r"\bver\.\d+(?:\.\d+){1,3}", normalized, re.IGNORECASE)
    )
    if title_phrase or title_signature:
        hints.append(
            {
                "screen": "title_screen",
                "confidence": 0.98 if title_phrase else 0.96,
                "matched_terms": (
                    ["点击屏幕开始"] if title_phrase else ["Cygames", "Ver."]
                ),
                "reason": "title_screen_text",
            }
        )

    for pattern in _SCREEN_PATTERNS:
        match_text = normalized
        if pattern.get("screen") == "race_participant_list":
            for compound in _RACE_PARTICIPANT_COMPOUND_EXCLUSIONS:
                match_text = match_text.replace(compound, " ")
        terms = _matched_terms(match_text, pattern["terms"])
        if not terms:
            continue
        boost_terms = _matched_terms(normalized, pattern.get("boost_terms", ()))
        if (
            pattern.get("reason") == "event_choice_text"
            and not boost_terms
            and len(terms) == 1
            and terms[0] in {"上", "下"}
        ):
            continue
        if pattern.get("reason") == "race_result_rank_text" and not _rank_text_has_result_context(normalized):
            continue
        confidence = float(pattern["confidence"])
        if boost_terms:
            confidence = min(0.95, confidence + 0.06)
        hints.append(
            {
                "screen": pattern["screen"],
                "confidence": round(confidence, 3),
                "matched_terms": terms + boost_terms,
                "reason": pattern["reason"],
            }
        )

    hints.sort(key=lambda item: float(item.get("confidence", 0.0)), reverse=True)
    primary_hint = hints[0] if hints else None
    return {
        "visible_text": visible_text,
        "normalized_text": normalized,
        "dangerous": bool(resource_terms),
        "resource_terms": resource_terms,
        "matched_resource_terms": matched_resource_terms,
        "resource_keyword_policy": keyword_policy,
        "screen_hints": hints,
        "primary_hint": primary_hint,
    }


def _filter_contextual_resource_false_positives(
    normalized: str,
    resource_terms: list[str],
) -> list[str]:
    """Keep resource matching fail-closed without treating race names as prompts."""
    gem = "\u5b9d\u77f3"
    if gem not in resource_terms:
        return resource_terms

    action_context = re.search(
        r"(?:\u4f7f\u7528|\u6d88\u8017|\u8d2d\u4e70|\u4e70\u5165|"
        r"\u652f\u4ed8|\u5145\u503c|\u5151\u6362|\u6062\u590d|"
        r"\u4e0d\u8db3|\u9700\u8981|\u6240\u9700|\u82b1\u8d39|"
        r"\u8865\u5145).{0,12}\u5b9d\u77f3|"
        r"\u5b9d\u77f3.{0,12}(?:\u4f7f\u7528|\u6d88\u8017|"
        r"\u8d2d\u4e70|\u4e70\u5165|\u652f\u4ed8|\u5145\u503c|"
        r"\u5151\u6362|\u6062\u590d|\u4e0d\u8db3|\u9700\u8981|"
        r"\u6240\u9700|\u82b1\u8d39|\u8865\u5145)",
        normalized,
    )
    race_result_context = (
        "\u4eba\u6c14\u7b2c" in normalized
        and (
            "\u6bd4\u8d5b\u7ed3\u679c" in normalized
            or "\u8349\u5730" in normalized
            or "\u8eab\u4f4d" in normalized
            or "\u5c45\u4e2d" in normalized
            or "\u8ddf\u524d" in normalized
        )
    )
    race_commentary_context = re.search(
        r"\u5b9e\u51b5(?:.|\n){0,120}?\u73b0\s*\u5c45\s*\u7b2c\s*"
        r"[0-9\uff10-\uff19]+\s*\u7684\s*\u662f\s*[0-9\uff10-\uff19]+\s*\u53f7",
        normalized,
    )
    if (race_result_context or race_commentary_context) and action_context is None:
        return [term for term in resource_terms if term != gem]
    return resource_terms


def fuse_perception_with_text(
    perception: dict[str, Any],
    screen_text: dict[str, Any] | str | list[Any] | tuple[Any, ...] | None,
) -> dict[str, Any]:
    """Attach text analysis and cautiously upgrade low-confidence screens."""
    analysis = screen_text if isinstance(screen_text, dict) else analyze_screen_text(screen_text)
    fused = dict(perception)
    fused["screen_text"] = analysis

    primary = analysis.get("primary_hint") if isinstance(analysis, dict) else None
    if not isinstance(primary, dict):
        return fused

    if analysis.get("dangerous"):
        fused["screen"] = "resource_prompt"
        fused["confidence"] = max(float(fused.get("confidence", 0.0) or 0.0), 0.97)
        return fused

    current_screen = str(fused.get("screen", "unknown"))
    current_confidence = float(fused.get("confidence", 0.0) or 0.0)
    hint_confidence = float(primary.get("confidence", 0.0) or 0.0)
    can_override = current_screen in {"unknown", "portrait_ui", "landscape_ui", "bottom_action"}
    ambiguous_bottom_action = (
        current_screen == "bottom_action"
        and hint_confidence >= 0.72
        and hint_confidence >= current_confidence + 0.03
    )
    if can_override and (current_confidence < 0.65 or ambiguous_bottom_action) and hint_confidence >= 0.72:
        fused["screen"] = str(primary.get("screen", current_screen))
        fused["confidence"] = hint_confidence
        fused["screen_text_override"] = True

    return fused


def resource_terms_from_state(state: dict[str, Any]) -> list[str]:
    """Collect paid/rare-resource terms from flat and nested state text."""
    text_values: list[Any] = []
    for key in ("text", "ocr_text", "visible_text", "summary"):
        if key in state:
            text_values.append(state.get(key))

    nested = state.get("screen_text")
    explicit_terms: list[str] = []
    if isinstance(nested, dict):
        text_values.extend(
            nested.get(key)
            for key in ("visible_text", "normalized_text", "summary")
            if key in nested
        )
        terms = nested.get("resource_terms")
        if isinstance(terms, list):
            explicit_terms.extend(str(term) for term in terms)
    elif nested is not None:
        text_values.append(nested)

    normalized = _normalize_text(_flatten_text(text_values))
    analysis = analyze_screen_text(normalized)
    if analysis["resource_keyword_policy"] in {"disabled_in_career", "disabled_in_loading_advice", "disabled_in_skill_list"}:
        return []
    terms = _matched_terms(normalized, RESOURCE_TERMS)
    return _unique([*terms, *explicit_terms, *analysis["resource_terms"]])


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("text", "ocr_text", "visible_text", "normalized_text", "summary"):
            if key in value:
                parts.append(_flatten_text(value.get(key)))
        hints = value.get("screen_hints")
        if isinstance(hints, list):
            parts.extend(_flatten_text(hint) for hint in hints)
        return "\n".join(part for part in parts if part)
    if isinstance(value, (list, tuple, set)):
        return "\n".join(_flatten_text(item) for item in value if item is not None)
    return str(value)


def _normalize_text(text: str) -> str:
    return (
        text.replace("！", "!")
        .replace("　", " ")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


def _matched_terms(text: str, terms: tuple[str, ...] | list[str]) -> list[str]:
    upper = text.upper()
    matched: list[str] = []
    for term in terms:
        needle = str(term).upper()
        if needle in {"TP", "RP"}:
            # OCR commonly emits stray lowercase syllables (for example ``tp``)
            # on result screens.  The game renders these resource abbreviations
            # in uppercase, so preserve case for the two very short tokens while
            # retaining their ASCII token boundaries.
            token_pattern = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z0-9])"
            )
            short_token_found = False
            for line in text.splitlines() or [text]:
                for token_match in token_pattern.finditer(line):
                    stripped = line.strip(" \t:：,，.!！?？()（）[]【】")
                    before = line[token_match.start() - 1 : token_match.start()]
                    after = line[token_match.end() : token_match.end() + 1]
                    adjacent_cjk = bool(
                        re.search(r"[\u3400-\u9fff]", before + after)
                    )
                    resource_context = any(
                        marker in line
                        for marker in (
                            "恢复",
                            "回复",
                            "不足",
                            "使用",
                            "消耗",
                            "补充",
                            "购买",
                            "宝石",
                            "萝卜",
                        )
                    )
                    if stripped == needle or adjacent_cjk or resource_context:
                        short_token_found = True
                        break
                if short_token_found:
                    break
            if short_token_found:
                matched.append(term)
            continue
        if needle in upper:
            matched.append(term)
    return matched


def _rank_text_has_result_context(text: str) -> bool:
    result_terms = ("结果", "結果", "比赛结果", "查看结果", "排名", "成绩")
    goal_terms = ("目标", "达成条件", "参赛条件", "中获得第", "获得第")
    return any(term in text for term in result_terms) and not any(term in text for term in goal_terms)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
