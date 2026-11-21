"""Same-frame parser and verifier for the final skill-purchase dialog."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Mapping

import cv2
import numpy as np

from core.perception import Perception
from core.name_match import name_similarity
from core.skill_acquisition_parser import (
    _normalize_ocr_text,
    _ocr_data,
    _prepare_text_variants,
    resolve_tesseract_runtime,
)
from core.skill_purchase_executor import DialogVerification


_FORBIDDEN_RESOURCE_TERMS = (
    "萝卜",
    "付费",
    "充值",
    "购买体力",
    "恢复体力",
    "闹钟",
    "复活",
)


def inspect_skill_purchase_confirmation_dialog(
    image: Any, *, reviewed_rows: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a fail-closed current-run dialog state from one canonical frame."""

    rejected = _rejected_state()
    if not isinstance(image, np.ndarray) or image.shape != (1280, 720, 3):
        return rejected
    if not isinstance(reviewed_rows, Mapping) or len(reviewed_rows) != 1:
        return rejected
    review = next(iter(reviewed_rows.values()))
    if not _valid_review(review):
        return rejected
    header_ok = _bright_green_ratio(image[32:95, 8:713]) >= 0.75
    left_white = _white_ratio(image[1144:1222, 60:343])
    final_control = _final_green_control(image)
    if final_control is None:
        return rejected

    runtime = resolve_tesseract_runtime()
    title_candidates = _ocr_variants(image[25:100, 120:610], runtime=runtime)
    name_candidates = _ocr_variants(image[125:180, 130:430], runtime=runtime)
    description_candidates = _ocr_variants(
        image[175:250, 125:675], runtime=runtime
    )
    right_candidates = _ocr_variants(image[1135:1225, 375:665], runtime=runtime)
    left_candidates = _ocr_variants(image[1135:1225, 55:350], runtime=runtime)
    full_text, _full_confidence = _ocr_data(
        cv2.resize(image, None, fx=1.25, fy=1.25, interpolation=cv2.INTER_CUBIC),
        psm=6,
        runtime=runtime,
    )

    title_ok = _supported_exact(title_candidates, "技能获取确认", minimum=0.90, count=2)
    observed_name = str(review["observed_name"])
    high_margin_review = _high_margin_review_enabled(review)
    direct_parser_review = _direct_parser_review_enabled(review)
    name_ok = _supported_direct_exact_name(
        name_candidates, observed_name
    ) or (
        high_margin_review
        and _supported_exact_across_variants(
            name_candidates,
            observed_name,
            minimum=0.65,
            distinct_variants=2,
        )
    ) or (
        _supported_catalog_alias_name(name_candidates, review)
    )
    description_token = str(review["required_description_token"])
    description_minimum = 0.75 if high_margin_review else 0.80
    matching_descriptions = [
        candidate
        for candidate in description_candidates
        if (
            _description_token_matches(description_token, candidate.get("text"))
            or _full_description_exact(
                description_token, candidate.get("text")
            )
        )
        and isinstance(candidate.get("confidence"), (int, float))
        and float(candidate["confidence"]) >= description_minimum
    ]
    description_ok = _description_token_supported(
        description_token, description_candidates
    )
    full_description_once_ok = _supported_full_description_once(
        description_token,
        description_candidates,
        minimum=description_minimum,
    )
    identity_ok = _dialog_identity_evidence_accepted(
        name_ok=name_ok,
        description_ok=description_ok,
        full_description_once_ok=full_description_once_ok,
        high_margin_review=high_margin_review,
        direct_parser_review=direct_parser_review,
    )
    best_description = max(
        matching_descriptions,
        key=lambda candidate: float(candidate["confidence"]),
        default={"text": "", "confidence": 0.0},
    )
    right_text_ok = _supported_exact(
        right_candidates, "技能获取", minimum=0.70, count=1
    )
    left_text_ok = _supported_exact(left_candidates, "取消", minimum=0.70, count=1)
    resource_text = " ".join(
        [full_text]
        + [str(candidate["text"]) for candidate in title_candidates]
        + [str(candidate["text"]) for candidate in name_candidates]
        + [str(candidate["text"]) for candidate in description_candidates]
        + [str(candidate["text"]) for candidate in right_candidates]
        + [str(candidate["text"]) for candidate in left_candidates]
    )
    danger_flags = (
        ["forbidden_resource_text"]
        if _contains_forbidden_resource_terms(resource_text)
        else []
    )
    accepted = bool(
        title_ok
        and identity_ok
        and right_text_ok
        and left_text_ok
        and final_control is not None
        and not danger_flags
    )
    if not accepted:
        return {
            **rejected,
            "dialog_title_candidates": title_candidates,
            "name_candidates": name_candidates,
            "description_candidates": description_candidates,
            "dangerous_resource_flags": danger_flags,
            "structural_evidence": {
                "title_ok": title_ok,
                "name_ok": name_ok,
                "description_ok": description_ok,
                "full_description_once_ok": full_description_once_ok,
                "identity_ok": identity_ok,
                "right_text_ok": right_text_ok,
                "left_text_ok": left_text_ok,
                "header_ok": header_ok,
                "left_white_ratio": round(left_white, 4),
                "final_control_found": final_control is not None,
            },
        }
    return {
        "accepted": True,
        "page_kind": "confirmation_dialog",
        "screen_kind": "confirmation_dialog",
        "dialog_title": "技能获取确认",
        "dialog_title_candidates": title_candidates,
        "confirmed_item_ids": [str(review["item_id"])],
        "observed_name": observed_name,
        "name_candidates": name_candidates,
        "description": re.sub(r"\s+", " ", str(best_description["text"])).strip(),
        "description_confidence": round(float(best_description["confidence"]), 4),
        "cancel_control": {"x": 201, "y": 1183},
        "final_confirm_control": final_control,
        "dangerous_resource_flags": [],
        "currency_scope": "skill_points_only",
        "observed_currency": "skill_points",
    }


def inspect_skill_batch_purchase_confirmation_dialog(
    image: Any, *, reviewed_rows: Mapping[str, Any]
) -> dict[str, Any]:
    """Verify the current two-row batch dialog without trusting row names alone."""

    rejected = _rejected_state()
    if not isinstance(image, np.ndarray) or image.shape != (1280, 720, 3):
        return rejected
    if not isinstance(reviewed_rows, Mapping) or len(reviewed_rows) != 2:
        return rejected
    reviews = list(reviewed_rows.values())
    if not _valid_batch_reviews(reviews):
        return rejected

    header_ok = _bright_green_ratio(image[32:95, 8:713]) >= 0.75
    left_white = _white_ratio(image[1144:1222, 60:343])
    empty_tail_white = _white_ratio(image[490:1040, 16:704])
    slot_states = [_confirmation_row_slot_state(image, y)
                   for y in (125, 307, 489, 670, 851)]
    final_control = _final_green_control(image)
    if (
        slot_states != [True, True, False, False, False]
        or final_control is None
    ):
        return rejected

    runtime = resolve_tesseract_runtime()
    title_candidates = _ocr_variants(image[25:100, 120:610], runtime=runtime)
    right_candidates = _ocr_variants(
        image[1135:1225, 375:665], runtime=runtime
    )
    left_candidates = _ocr_variants(
        image[1135:1225, 55:350], runtime=runtime
    )
    row_crops = (
        (image[125:180, 130:675], image[175:250, 125:675]),
        (image[310:365, 130:675], image[355:435, 125:675]),
    )
    row_evidence: list[dict[str, Any]] = []
    for review, (name_crop, description_crop) in zip(reviews, row_crops):
        name_candidates = _ocr_variants(name_crop, runtime=runtime)
        description_candidates = _ocr_variants(
            description_crop, runtime=runtime
        )
        token = str(review["required_description_token"])
        matching = [
            candidate
            for candidate in description_candidates
            if _full_description_exact(token, candidate.get("text"))
            and isinstance(candidate.get("confidence"), (int, float))
            and not isinstance(candidate.get("confidence"), bool)
            and float(candidate["confidence"]) >= 0.80
        ]
        row_evidence.append(
            {
                "item_id": str(review["item_id"]),
                "name_candidates": name_candidates,
                "description_candidates": description_candidates,
                "description_ok": bool(matching),
                "description": (
                    re.sub(
                        r"\s+",
                        " ",
                        str(
                            max(
                                matching,
                                key=lambda candidate: float(
                                    candidate["confidence"]
                                ),
                                default={"text": ""},
                            )["text"]
                        ),
                    ).strip()
                ),
            }
        )

    title_ok = _supported_exact(
        title_candidates, "技能获取确认", minimum=0.90, count=2
    )
    right_text_ok = _supported_exact(
        right_candidates, "技能获取", minimum=0.70, count=1
    )
    left_text_ok = _supported_exact(
        left_candidates, "取消", minimum=0.70, count=1
    )
    full_text, _full_confidence = _ocr_data(
        cv2.resize(
            image, None, fx=1.25, fy=1.25, interpolation=cv2.INTER_CUBIC
        ),
        psm=6,
        runtime=runtime,
    )
    danger_flags = (
        ["forbidden_resource_text"]
        if _contains_forbidden_resource_terms(full_text)
        else []
    )
    accepted = bool(
        title_ok
        and right_text_ok
        and left_text_ok
        and all(row["description_ok"] for row in row_evidence)
        and not danger_flags
    )
    if not accepted:
        return {
            **rejected,
            "dialog_title_candidates": title_candidates,
            "batch_row_evidence": row_evidence,
            "dangerous_resource_flags": danger_flags,
            "structural_evidence": {
                "title_ok": title_ok,
                "right_text_ok": right_text_ok,
                "left_text_ok": left_text_ok,
                "description_rows_ok": [
                    row["description_ok"] for row in row_evidence
                ],
                "header_ok": header_ok,
                "left_white_ratio": round(left_white, 4),
                "empty_tail_white_ratio": round(empty_tail_white, 4),
                "row_slot_states": slot_states,
                "final_control_found": final_control is not None,
            },
        }
    return {
        "accepted": True,
        "page_kind": "confirmation_dialog",
        "screen_kind": "confirmation_dialog",
        "dialog_title": "技能获取确认",
        "dialog_title_candidates": title_candidates,
        "confirmed_item_ids": [row["item_id"] for row in row_evidence],
        "observed_names": [str(review["observed_name"]) for review in reviews],
        "descriptions": [row["description"] for row in row_evidence],
        "batch_row_evidence": row_evidence,
        "observed_row_count": 2,
        "no_extra_rows": True,
        "cancel_control": {"x": 201, "y": 1183},
        "final_confirm_control": final_control,
        "dangerous_resource_flags": [],
        "currency_scope": "skill_points_only",
        "observed_currency": "skill_points",
    }


_ALLOWED_AUTO_INCLUDED_LOWER_SKILLS_V1 = {
    "arc_master": "corner_adept_circle",
    "ears_in_wind": "near_danger",
    "ura_db_200341": "ura_db_200342",
}
_ALLOWED_AUTO_INCLUDED_LOWER_SKILLS_V2 = {
    **_ALLOWED_AUTO_INCLUDED_LOWER_SKILLS_V1,
    "cn:耳边风": "cn:临危不乱",
}
_ALLOWED_AUTO_INCLUDED_LOWER_SKILLS = {
    **_ALLOWED_AUTO_INCLUDED_LOWER_SKILLS_V2,
    "cn:曲线行家": "cn:弯道加速○",
    "cn:永不言弃": "cn:坚定之心",
}


def inspect_skill_multi_page_purchase_confirmation_dialog(
    images: Any, *, expected_rows: Any
) -> dict[str, Any]:
    """Close a scrollable confirmation dialog over an exact ordered skill chain."""

    rejected = _rejected_state()
    if (
        not isinstance(images, list)
        or len(images) < 2
        or not all(
            isinstance(image, np.ndarray) and image.shape == (1280, 720, 3)
            for image in images
        )
        or not _valid_multi_page_expected_rows(expected_rows)
    ):
        return rejected

    runtime = resolve_tesseract_runtime()
    page_row_indexes: list[list[int]] = []
    page_evidence: list[dict[str, Any]] = []
    all_resource_text: list[str] = []
    all_row_y_positions = (125, 307, 489, 670, 851)
    single_page_batch = len(expected_rows) <= len(all_row_y_positions)
    page_images = [images[-1]] if single_page_batch else images
    row_y_positions = (
        all_row_y_positions[: len(expected_rows)]
        if single_page_batch
        else all_row_y_positions
    )
    read_text = (
        _multi_page_rapidocr_variants
        if "chi_sim" not in getattr(runtime, "available_languages", {"chi_sim"})
        else _ocr_variants
    )
    for image in page_images:
        header_ok = _bright_green_ratio(image[32:95, 8:713]) >= 0.75
        left_white = _white_ratio(image[1144:1222, 60:343])
        final_control = _final_green_control(image)
        if final_control is None:
            return rejected

        title_candidates = read_text(
            image[25:100, 120:610], runtime=runtime
        )
        right_candidates = read_text(
            image[1135:1225, 375:665], runtime=runtime
        )
        left_candidates = read_text(
            image[1135:1225, 55:350], runtime=runtime
        )
        if not (
            _supported_exact(
                title_candidates, "技能获取确认", minimum=0.90, count=2
            )
            and _supported_exact(
                right_candidates, "技能获取", minimum=0.70, count=1
            )
            and _supported_exact(
                left_candidates, "取消", minimum=0.70, count=1
            )
        ):
            return rejected

        indexes: list[int] = []
        rows: list[dict[str, Any]] = []
        for y in row_y_positions:
            name_candidates = read_text(
                image[y : y + 55, 125:680], runtime=runtime
            )
            description_candidates = read_text(
                image[y + 50 : y + 130, 125:680], runtime=runtime
            )
            matches = _matching_multi_page_expected_indexes(
                expected_rows,
                name_candidates=name_candidates,
                description_candidates=description_candidates,
            )
            if len(matches) != 1:
                return {
                    **rejected,
                    "page_row_indexes": page_row_indexes,
                    "page_evidence": page_evidence,
                    "failed_row": {
                        "y": y,
                        "matching_expected_indexes": matches,
                        "name_candidates": name_candidates,
                        "description_candidates": description_candidates,
                    },
                }
            index = matches[0]
            indexes.append(index)
            rows.append(
                {
                    "expected_index": index,
                    "item_id": str(expected_rows[index]["item_id"]),
                    "name_candidates": name_candidates,
                    "description_candidates": description_candidates,
                }
            )
            all_resource_text.extend(
                str(candidate.get("raw_text", candidate.get("text", "")))
                for candidate in name_candidates + description_candidates
            )
        if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
            return rejected
        page_row_indexes.append(indexes)
        page_evidence.append(
            {
                "row_indexes": indexes,
                "rows": rows,
                "title_candidates": title_candidates,
            }
        )
        all_resource_text.extend(
            str(candidate.get("raw_text", candidate.get("text", "")))
            for candidate in title_candidates + right_candidates + left_candidates
        )

    if (
        page_row_indexes[0][0] != 0
        or page_row_indexes[-1][-1] != len(expected_rows) - 1
        or any(
            current[0] <= previous[0] or current[0] > previous[-1]
            for previous, current in zip(
                page_row_indexes, page_row_indexes[1:]
            )
        )
        or sorted({index for page in page_row_indexes for index in page})
        != list(range(len(expected_rows)))
    ):
        return rejected

    danger_flags = (
        ["forbidden_resource_text"]
        if _contains_forbidden_resource_terms(" ".join(all_resource_text))
        else []
    )
    if danger_flags:
        return {**rejected, "dangerous_resource_flags": danger_flags}
    planned_item_ids = [
        str(row["item_id"])
        for row in expected_rows
        if row["kind"] == "planned"
    ]
    auto_included_item_ids = [
        str(row["item_id"])
        for row in expected_rows
        if row["kind"] == "auto_included_lower"
    ]
    return {
        "accepted": True,
        "page_kind": "confirmation_dialog",
        "screen_kind": "confirmation_dialog",
        "dialog_title": "技能获取确认",
        "confirmed_item_ids": [
            str(row["item_id"]) for row in expected_rows
        ],
        "planned_item_ids": planned_item_ids,
        "auto_included_item_ids": auto_included_item_ids,
        "page_row_indexes": page_row_indexes,
        "page_evidence": page_evidence,
        "cancel_control": {"x": 201, "y": 1183},
        "final_confirm_control": _final_green_control(images[-1]),
        "dangerous_resource_flags": [],
        "currency_scope": "skill_points_only",
        "observed_currency": "skill_points",
    }


def _valid_multi_page_expected_rows(value: Any) -> bool:
    if not isinstance(value, list) or len(value) < 2:
        return False
    item_ids: set[str] = set()
    descriptions: set[str] = set()
    for index, row in enumerate(value):
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("item_id"), str)
            or not row["item_id"]
            or not isinstance(row.get("name"), str)
            or not row["name"]
            or not isinstance(row.get("description"), str)
            or not row["description"]
            or row.get("kind") not in {"planned", "auto_included_lower"}
            or row["item_id"] in item_ids
            or row["description"] in descriptions
        ):
            return False
        item_ids.add(row["item_id"])
        descriptions.add(row["description"])
        if row["kind"] == "planned":
            if "parent_item_id" in row:
                return False
            continue
        parent = row.get("parent_item_id")
        if (
            not isinstance(parent, str)
            or index == 0
            or value[index - 1].get("item_id") != parent
            or _ALLOWED_AUTO_INCLUDED_LOWER_SKILLS.get(parent)
            != row["item_id"]
        ):
            return False
    return True


def _valid_batch_reviews(reviews: list[Any]) -> bool:
    if len(reviews) != 2 or not all(_valid_review(review) for review in reviews):
        return False
    source_reports = {review.get("source_report_sha256") for review in reviews}
    initial_points = {review.get("expected_initial_skill_points") for review in reviews}
    if len(source_reports) != 1 or len(initial_points) != 1:
        return False
    initial = reviews[0].get("expected_initial_skill_points")
    if not isinstance(initial, int) or isinstance(initial, bool):
        return False
    required_evidence = {"name", "description", "cost", "rarity", "icon_color"}
    cumulative = 0
    source_pages: set[str] = set()
    row_indexes: list[int] = []
    for review in reviews:
        source_binding = review.get("source_binding")
        evidence = review.get("name_resolution_evidence")
        cost = review.get("observed_final_cost")
        if (
            review.get("name_binding_source")
            not in {
                "catalog_corroborated_ocr_alias",
                "strong_identity_binding_fallback",
            }
            or not isinstance(review.get("name_resolution_confidence"), (int, float))
            or isinstance(review.get("name_resolution_confidence"), bool)
            or float(review["name_resolution_confidence"]) < 0.95
            or not isinstance(review.get("name_resolution_margin"), (int, float))
            or isinstance(review.get("name_resolution_margin"), bool)
            or float(review["name_resolution_margin"]) < 0.25
            or review.get("name_resolution_evidence_dimensions") != 5
            or not isinstance(evidence, Mapping)
            or any(
                not isinstance(evidence.get(key), (int, float))
                or isinstance(evidence.get(key), bool)
                or float(evidence[key]) < 0.90
                for key in required_evidence
            )
            or not isinstance(cost, int)
            or isinstance(cost, bool)
            or cost <= 0
            or not isinstance(source_binding, Mapping)
            or not isinstance(source_binding.get("source_page"), str)
            or not isinstance(source_binding.get("row_index"), int)
            or isinstance(source_binding.get("row_index"), bool)
            or not _valid_bbox(source_binding.get("row_bbox"))
            or not isinstance(review.get("source_row_fingerprint"), str)
            or not review.get("source_row_fingerprint")
        ):
            return False
        cumulative += cost
        if (
            review.get("expected_selected_total") != cumulative
            or review.get("expected_remaining_skill_points")
            != initial - cumulative
        ):
            return False
        source_pages.add(source_binding["source_page"])
        row_indexes.append(source_binding["row_index"])
    return len(source_pages) == 1 and row_indexes == sorted(row_indexes)


def _dialog_identity_evidence_accepted(
    *,
    name_ok: bool,
    description_ok: bool,
    full_description_once_ok: bool,
    high_margin_review: bool,
    direct_parser_review: bool = False,
) -> bool:
    """Require an exact identity anchor plus independently readable description."""

    return bool(
        (
            name_ok
            and (
                description_ok
                or full_description_once_ok
                or direct_parser_review
            )
        )
        or (
            high_margin_review
            and full_description_once_ok
        )
    )


class CurrentRunSkillDialogVerifier:
    """Independently bind a parsed dialog to the one-purchase live plan."""

    def verify(
        self, dialog_state: Mapping[str, Any], plan: Mapping[str, Any]
    ) -> DialogVerification:
        if dialog_state.get("accepted") is not True:
            return DialogVerification(False, "skill_dialog_not_accepted")
        if (
            dialog_state.get("page_kind") != "confirmation_dialog"
            or dialog_state.get("screen_kind") != "confirmation_dialog"
            or dialog_state.get("dialog_title") != "技能获取确认"
        ):
            return DialogVerification(False, "skill_dialog_page_mismatch")
        purchases = plan.get("purchases")
        if not isinstance(purchases, list) or len(purchases) != 1:
            return DialogVerification(False, "skill_dialog_plan_not_unique")
        purchase = purchases[0]
        if not isinstance(purchase, Mapping):
            return DialogVerification(False, "skill_dialog_plan_invalid")
        expected_name = purchase.get("must_satisfy_ocr", {}).get("observed_name")
        if (
            dialog_state.get("confirmed_item_ids") != [purchase.get("item_id")]
            or name_similarity(dialog_state.get("observed_name"), expected_name) < 0.60
        ):
            return DialogVerification(False, "skill_dialog_identity_mismatch")
        if dialog_state.get("dangerous_resource_flags") != []:
            return DialogVerification(False, "skill_dialog_resource_signal")
        if not _valid_control(dialog_state.get("final_confirm_control")):
            return DialogVerification(False, "skill_dialog_final_control_invalid")
        return DialogVerification(True, "current_run_skill_dialog_verified")


class CurrentRunMultiPageSkillDialogVerifier:
    """Bind a closed multi-page dialog to the exact promoted purchase plan."""

    def __init__(self, expected_rows: Any) -> None:
        self.expected_rows = (
            [dict(row) for row in expected_rows]
            if _valid_multi_page_expected_rows(expected_rows)
            else []
        )

    def verify(
        self, dialog_state: Mapping[str, Any], plan: Mapping[str, Any]
    ) -> DialogVerification:
        if not self.expected_rows:
            return DialogVerification(False, "multi_page_expected_rows_invalid")
        if (
            dialog_state.get("accepted") is not True
            or dialog_state.get("page_kind") != "confirmation_dialog"
            or dialog_state.get("screen_kind") != "confirmation_dialog"
            or dialog_state.get("dialog_title") != "技能获取确认"
            or dialog_state.get("dangerous_resource_flags") != []
        ):
            return DialogVerification(False, "multi_page_skill_dialog_not_accepted")
        purchases = plan.get("purchases")
        if (
            not isinstance(purchases, list)
            or len(purchases) < 2
            or not all(isinstance(purchase, Mapping) for purchase in purchases)
        ):
            return DialogVerification(False, "multi_page_skill_plan_invalid")
        expected_all = [str(row["item_id"]) for row in self.expected_rows]
        expected_planned = [
            str(row["item_id"])
            for row in self.expected_rows
            if row["kind"] == "planned"
        ]
        expected_auto = [
            str(row["item_id"])
            for row in self.expected_rows
            if row["kind"] == "auto_included_lower"
        ]
        plan_ids = [str(purchase.get("item_id")) for purchase in purchases]
        if (
            plan_ids != expected_planned
            or dialog_state.get("confirmed_item_ids") != expected_all
            or dialog_state.get("planned_item_ids") != expected_planned
            or dialog_state.get("auto_included_item_ids") != expected_auto
        ):
            return DialogVerification(False, "multi_page_skill_identity_mismatch")
        page_indexes = dialog_state.get("page_row_indexes")
        if (
            not isinstance(page_indexes, list)
            or len(page_indexes) < 1
            or page_indexes[0][0] != 0
            or page_indexes[-1][-1] != len(expected_all) - 1
        ):
            return DialogVerification(False, "multi_page_skill_closure_missing")
        if not _valid_control(dialog_state.get("final_confirm_control")):
            return DialogVerification(
                False, "multi_page_skill_final_control_invalid"
            )
        return DialogVerification(
            True, "current_run_multi_page_skill_dialog_verified"
        )


def inspect_skill_purchase_success_dialog(
    image: Any, *, reviewed_rows: Mapping[str, Any]
) -> dict[str, Any]:
    """Verify the post-purchase success modal and expose only its close control."""

    rejected = _rejected_success_state()
    if not isinstance(reviewed_rows, Mapping) or len(reviewed_rows) != 1:
        return rejected
    review = next(iter(reviewed_rows.values()))
    if not _valid_review(review):
        return rejected
    allowed_icon_counts = (
        (1, 2)
        if review.get("rarity_or_icon_type") == "gold"
        else (1,)
    )
    for expected_icon_count in allowed_icon_counts:
        state = _inspect_skill_purchase_success_layout(
            image,
            confirmed_item_ids=[str(review["item_id"])],
            expected_icon_count=expected_icon_count,
        )
        if state.get("accepted") is True:
            return state
    return rejected


def inspect_skill_batch_purchase_success_dialog(
    image: Any, *, reviewed_rows: Mapping[str, Any]
) -> dict[str, Any]:
    """Verify a two-skill success modal after the exact batch confirmation."""

    rejected = _rejected_success_state()
    if not isinstance(reviewed_rows, Mapping) or len(reviewed_rows) != 2:
        return rejected
    reviews = list(reviewed_rows.values())
    if not _valid_batch_reviews(reviews):
        return rejected
    return _inspect_skill_purchase_success_layout(
        image,
        confirmed_item_ids=[str(review["item_id"]) for review in reviews],
        expected_icon_count=2,
    )


def inspect_skill_multi_purchase_success_dialog(
    image: Any, *, confirmed_item_ids: Any
) -> dict[str, Any]:
    """Verify a closed 2-6 icon success grid before exposing close."""

    rejected = _rejected_success_state()
    if (
        not isinstance(image, np.ndarray)
        or image.shape != (1280, 720, 3)
        or not isinstance(confirmed_item_ids, list)
        or not 2 <= len(confirmed_item_ids) <= 6
        or not all(
            isinstance(item_id, str) and item_id
            for item_id in confirmed_item_ids
        )
        or len(set(confirmed_item_ids)) != len(confirmed_item_ids)
    ):
        return rejected
    close_control = _outlined_enabled_control(image, (210, 785, 510, 887))
    grid_regions = [
        (30, 475),
        (160, 475),
        (290, 475),
        (420, 475),
        (550, 475),
        (30, 610),
        (160, 610),
        (290, 610),
        (420, 610),
        (550, 610),
    ]
    icon_regions = grid_regions[: len(confirmed_item_ids)]
    trailing_regions = grid_regions[len(confirmed_item_ids) :]
    icon_chroma = [
        _icon_chroma_ratio(image[y : y + 125, x : x + 125])
        for x, y in icon_regions
    ]
    trailing_chroma = [
        _icon_chroma_ratio(image[y : y + 125, x : x + 125])
        for x, y in trailing_regions
    ]
    if (
        close_control is None
        or any(ratio < 0.35 for ratio in icon_chroma)
        or any(ratio >= 0.20 for ratio in trailing_chroma)
    ):
        return rejected

    runtime = resolve_tesseract_runtime()
    read_text = (
        _multi_success_rapidocr_variants
        if "chi_sim" not in getattr(runtime, "available_languages", {"chi_sim"})
        else _ocr_variants
    )
    title = read_text(image[375:450, 115:610], runtime=runtime)
    status = read_text(image[735:790, 200:525], runtime=runtime)
    close = read_text(image[810:855, 275:445], runtime=runtime)
    title_ok = _supported_exact(
        title, "成功获得技能", minimum=0.90, count=2
    )
    status_ok = _supported_exact(
        status, "已获得技能", minimum=0.85, count=4
    )
    close_ok = _supported_exact(close, "关闭", minimum=0.40, count=2)
    recognized_text = " ".join(
        str(candidate.get("raw_text", candidate["text"]))
        for candidate in title + status + close
    )
    danger = (
        ["forbidden_resource_text"]
        if _contains_forbidden_resource_terms(recognized_text)
        else []
    )
    if not (title_ok and status_ok and close_ok) or danger:
        return rejected
    return {
        "accepted": True,
        "page_kind": "skill_purchase_success",
        "screen_kind": "skill_purchase_success",
        "dialog_title": "成功获得技能",
        "status_text": "已获得技能",
        "confirmed_item_ids": list(confirmed_item_ids),
        "icon_count": len(confirmed_item_ids),
        "icon_chroma": [round(value, 4) for value in icon_chroma],
        "trailing_chroma": [round(value, 4) for value in trailing_chroma],
        "close_control": close_control,
        "dangerous_resource_flags": [],
        "currency_scope": "skill_points_only",
        "observed_currency": "skill_points",
    }


def _icon_chroma_ratio(image: np.ndarray) -> float:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return float(
        np.mean(
            (hsv[:, :, 1] >= 120)
            & (hsv[:, :, 2] >= 150)
        )
    )


def _inspect_skill_purchase_success_layout(
    image: Any,
    *,
    confirmed_item_ids: list[str],
    expected_icon_count: int,
) -> dict[str, Any]:
    rejected = _rejected_success_state()
    if not isinstance(image, np.ndarray) or image.shape != (1280, 720, 3):
        return rejected
    close_control = _outlined_enabled_control(image, (210, 785, 510, 887))
    icon_chroma: list[float] = []
    for icon_index in range(expected_icon_count):
        x0 = 30 + 130 * icon_index
        x1 = x0 + 125
        icon_hsv = cv2.cvtColor(image[475:600, x0:x1], cv2.COLOR_BGR2HSV)
        icon_chroma.append(
            float(
                np.mean(
                    (icon_hsv[:, :, 1] >= 120)
                    & (icon_hsv[:, :, 2] >= 150)
                )
            )
        )
    trailing_x0 = 30 + 130 * expected_icon_count
    trailing_hsv = cv2.cvtColor(
        image[475:600, trailing_x0 : trailing_x0 + 125],
        cv2.COLOR_BGR2HSV,
    )
    trailing_chroma = float(
        np.mean(
            (trailing_hsv[:, :, 1] >= 120)
            & (trailing_hsv[:, :, 2] >= 150)
        )
    )
    if (
        close_control is None
        or any(ratio < 0.35 for ratio in icon_chroma)
        or trailing_chroma >= 0.20
    ):
        return rejected

    runtime = resolve_tesseract_runtime()
    title = _ocr_variants(image[375:450, 115:610], runtime=runtime)
    status = _ocr_variants(image[735:790, 200:525], runtime=runtime)
    close = _ocr_variants(image[810:855, 275:445], runtime=runtime)
    title_ok = _supported_exact(title, "成功获得技能", minimum=0.90, count=2)
    status_ok = _supported_exact(status, "已获得技能", minimum=0.85, count=4)
    close_ok = _supported_exact(close, "关闭", minimum=0.40, count=2)
    recognized_text = " ".join(
        str(candidate["text"]) for candidate in title + status + close
    )
    danger = (
        ["forbidden_resource_text"]
        if _contains_forbidden_resource_terms(recognized_text)
        else []
    )
    if not (title_ok and status_ok and close_ok) or danger:
        return rejected
    return {
        "accepted": True,
        "page_kind": "skill_purchase_success",
        "screen_kind": "skill_purchase_success",
        "dialog_title": "成功获得技能",
        "status_text": "已获得技能",
        "confirmed_item_ids": list(confirmed_item_ids),
        "success_icon_count": expected_icon_count,
        "close_control": close_control,
        "dangerous_resource_flags": [],
        "currency_scope": "skill_points_only",
        "observed_currency": "skill_points",
    }


def _multi_page_rapidocr_variants(image: np.ndarray, *, runtime: Any) -> list[dict[str, Any]]:
    """Use the already installed skill-page reader when Tesseract lacks Chinese."""
    from core.skill_acquisition_parser import _rapidocr_ocr_data, _rapidocr_join_text

    del runtime
    candidates = []
    for upscale in (2, 3):
        items = _rapidocr_ocr_data(image, upscale=upscale)
        text, confidence = _rapidocr_join_text(items, multi_line=True)
        candidates.append({
            "variant": f"rapidocr_up{upscale}", "text": _normalize_ocr_text(text),
            "confidence": round(float(confidence), 4), "raw_items": items,
            # Resource checks retain even tokens below the reader's text filter.
            "raw_text": " ".join(str(item.get("text", "")) for item in items),
        })
    return candidates


def _confirmation_row_slot_state(image: np.ndarray, y: int) -> bool | None:
    """Locate a complete card outline, never infer count from white fill."""
    crop = image[max(0, y-4):y+171, 24:694]
    edges = cv2.Canny(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), 15, 50)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if any(620 <= width <= 665 and 155 <= height <= 172 and -3 <= top-4 <= 5
           for _left, top, width, height in (cv2.boundingRect(c) for c in contours)):
        return True
    interior = image[y+6:y+159, 45:675]
    ink_edges = cv2.Canny(cv2.cvtColor(interior, cv2.COLOR_BGR2GRAY), 15, 50)
    components, _ = cv2.findContours(ink_edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if not any(max(cv2.boundingRect(c)[2:]) >= 8 for c in components):
        return False
    return None


def _outlined_enabled_control(
    image: np.ndarray, region: tuple[int, int, int, int]
) -> dict[str, int] | None:
    """One outlined control with enabled label ink; caller reads exact text."""
    x0, y0, x1, y1 = region
    crop = image[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    edges = cv2.Canny(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), 25, 80)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = [(x, y, width, height) for x, y, width, height in
             (cv2.boundingRect(c) for c in contours)
             if width >= .80*(x1-x0) and height >= .55*(y1-y0)
             and width <= x1-x0-2 and height <= y1-y0-2]
    if not boxes:
        return None
    centers = [(x+width/2, y+height/2) for x, y, width, height in boxes]
    if (max(p[0] for p in centers)-min(p[0] for p in centers) > 12
            or max(p[1] for p in centers)-min(p[1] for p in centers) > 12):
        return None
    x, y, width, height = max(boxes, key=lambda box: box[2]*box[3])
    label = crop[y+height//4:y+3*height//4, x+width//4:x+3*width//4]
    hsv = cv2.cvtColor(label, cv2.COLOR_BGR2HSV)
    background_value = float(np.percentile(hsv[:, :, 2], 85))
    if int(((hsv[:, :, 1] > 45) & (hsv[:, :, 2] < background_value-35)).sum()) < 20:
        return None
    return dict(x=x0+x+width//2, y=y0+y+height//2)


def _multi_success_rapidocr_variants(image: np.ndarray, *, runtime: Any) -> list[dict[str, Any]]:
    """Keep four actual preprocessing reads for the success-status evidence gate."""
    from core.skill_acquisition_parser import _rapidocr_ocr_data, _rapidocr_join_text

    del runtime
    candidates = []
    for variant, prepared in _prepare_text_variants(image):
        items = _rapidocr_ocr_data(prepared, upscale=1)
        text, confidence = _rapidocr_join_text(items, multi_line=True)
        candidates.append({
            "variant": variant, "text": _normalize_ocr_text(text),
            "confidence": round(float(confidence), 4), "raw_items": items,
            "raw_text": " ".join(str(item.get("text", "")) for item in items),
        })
    return candidates


def _ocr_variants(image: np.ndarray, *, runtime: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for variant, crop in _prepare_text_variants(image):
        for psm in (6, 7):
            text, confidence = _ocr_data(crop, psm=psm, runtime=runtime)
            normalized = _normalize_ocr_text(text)
            if normalized:
                candidates.append(
                    {
                        "variant": variant,
                        "psm": psm,
                        "text": normalized,
                        "confidence": round(float(confidence), 4),
                    }
                )
    return candidates


def _supported_exact(
    candidates: list[dict[str, Any]], expected: str, *, minimum: float, count: int
) -> bool:
    matches = [
        candidate
        for candidate in candidates
        if candidate.get("text") == expected
        and isinstance(candidate.get("confidence"), (int, float))
        and float(candidate["confidence"]) >= minimum
    ]
    return len(matches) >= count


def _supported_contains(
    candidates: list[dict[str, Any]], expected: str, *, minimum: float, count: int
) -> bool:
    matches = [
        candidate
        for candidate in candidates
        if expected in str(candidate.get("text", ""))
        and isinstance(candidate.get("confidence"), (int, float))
        and float(candidate["confidence"]) >= minimum
    ]
    return len(matches) >= count


def _supported_direct_exact_name(
    candidates: list[dict[str, Any]], expected: str
) -> bool:
    return _supported_exact(
        candidates, expected, minimum=0.80, count=2
    ) or _supported_exact_across_variants(
        candidates,
        expected,
        minimum=0.70,
        distinct_variants=3,
    )


def _supported_exact_across_variants(
    candidates: list[dict[str, Any]],
    expected: str,
    *,
    minimum: float,
    distinct_variants: int,
) -> bool:
    variants = {
        str(candidate["variant"])
        for candidate in candidates
        if candidate.get("text") == expected
        and isinstance(candidate.get("variant"), str)
        and isinstance(candidate.get("confidence"), (int, float))
        and not isinstance(candidate.get("confidence"), bool)
        and float(candidate["confidence"]) >= minimum
    }
    return len(variants) >= distinct_variants


def _supported_full_description_once(
    expected: str,
    candidates: list[dict[str, Any]],
    *,
    minimum: float,
) -> bool:
    return any(
        _full_description_exact(expected, candidate.get("text"))
        and isinstance(candidate.get("confidence"), (int, float))
        and not isinstance(candidate.get("confidence"), bool)
        and float(candidate["confidence"]) >= minimum
        for candidate in candidates
    )


def _supported_multi_page_expected_row(
    expected: Mapping[str, Any],
    *,
    name_candidates: list[dict[str, Any]],
    description_candidates: list[dict[str, Any]],
) -> bool:
    expected_description = str(expected.get("description", ""))
    if _supported_full_description_once(
        expected_description, description_candidates, minimum=0.70
    ):
        return True

    normalize = lambda value: re.sub(
        r"[\s，,。．.!！?？:：;；]+", "", str(value)
    ).replace("<", "").replace(">", "").casefold()
    expected_compact = normalize(expected_description)
    observed_descriptions = [
        normalize(candidate.get("text"))
        for candidate in description_candidates
        if isinstance(candidate.get("confidence"), (int, float))
        and not isinstance(candidate.get("confidence"), bool)
        and float(candidate["confidence"]) >= 0.70
    ]
    expected_name = expected.get("name")
    if not isinstance(expected_name, str) or not _supported_direct_exact_name(
        name_candidates, expected_name
    ):
        return False
    if any(
        min(len(expected_compact), len(observed)) >= 10
        and SequenceMatcher(None, expected_compact, observed).ratio() >= 0.88
        for observed in observed_descriptions
    ):
        return True

    return any(
        _common_suffix_length(expected_compact, observed) >= 6
        for observed in observed_descriptions
    )


def _matching_multi_page_expected_indexes(
    expected_rows: list[Mapping[str, Any]],
    *,
    name_candidates: list[dict[str, Any]],
    description_candidates: list[dict[str, Any]],
) -> list[int]:
    exact_matches = [
        index
        for index, expected in enumerate(expected_rows)
        if _supported_full_description_once(
            str(expected.get("description", "")),
            description_candidates,
            minimum=0.70,
        )
    ]
    if exact_matches:
        return exact_matches

    identity_supported_matches = [
        index
        for index, expected in enumerate(expected_rows)
        if _supported_multi_page_expected_row(
            expected,
            name_candidates=name_candidates,
            description_candidates=description_candidates,
        )
    ]
    if identity_supported_matches:
        return identity_supported_matches

    return [
        index
        for index, expected in enumerate(expected_rows)
        if _supported_high_similarity_description(
            str(expected.get("description", "")), description_candidates
        )
    ]


def _supported_high_similarity_description(
    expected: str, candidates: list[dict[str, Any]]
) -> bool:
    normalize = lambda value: re.sub(
        r"[\s，,。．.!！?？:：;；]+", "", str(value)
    ).replace("<", "").replace(">", "").casefold()
    expected_compact = normalize(expected)
    return any(
        isinstance(candidate.get("confidence"), (int, float))
        and not isinstance(candidate.get("confidence"), bool)
        and float(candidate["confidence"]) >= 0.70
        and min(len(expected_compact), len(normalize(candidate.get("text"))))
        >= 10
        and SequenceMatcher(
            None, expected_compact, normalize(candidate.get("text"))
        ).ratio()
        >= 0.88
        for candidate in candidates
    )


def _common_suffix_length(left: str, right: str) -> int:
    count = 0
    for left_character, right_character in zip(reversed(left), reversed(right)):
        if left_character != right_character:
            break
        count += 1
    return count


def _full_description_exact(expected: Any, observed: Any) -> bool:
    normalize = lambda value: re.sub(
        r"[\s，,。．.!！?？:：;；]+", "", str(value)
    ).replace("<", "").replace(">", "")
    expected_compact = normalize(expected)
    observed_compact = normalize(observed)
    return bool(expected_compact) and expected_compact == observed_compact


def _high_margin_review_enabled(review: Mapping[str, Any]) -> bool:
    confidence = review.get("name_resolution_confidence")
    margin = review.get("name_resolution_margin")
    dimensions = review.get("name_resolution_evidence_dimensions")
    return (
        review.get("name_binding_source") == "unique_high_margin_resolver"
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and float(confidence) >= 0.82
        and isinstance(margin, (int, float))
        and not isinstance(margin, bool)
        and float(margin) >= 0.50
        and isinstance(dimensions, int)
        and not isinstance(dimensions, bool)
        and dimensions >= 3
    )


def _direct_parser_review_enabled(review: Mapping[str, Any]) -> bool:
    confidence = review.get("name_resolution_confidence")
    margin = review.get("name_resolution_margin")
    dimensions = review.get("name_resolution_evidence_dimensions")
    evidence = review.get("name_resolution_evidence")
    required = {"name", "rarity", "icon_color"}
    return (
        review.get("name_binding_source") == "direct_parser_name"
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and float(confidence) >= 0.95
        and isinstance(margin, (int, float))
        and not isinstance(margin, bool)
        and float(margin) >= 0.25
        and isinstance(dimensions, int)
        and not isinstance(dimensions, bool)
        and dimensions >= len(required)
        and isinstance(evidence, Mapping)
        and all(
            isinstance(evidence.get(key), (int, float))
            and not isinstance(evidence.get(key), bool)
            and float(evidence[key]) >= 0.95
            for key in required
        )
    )


def _supported_catalog_alias_name(
    candidates: list[dict[str, Any]],
    review: Mapping[str, Any],
) -> bool:
    confidence = review.get("name_resolution_confidence")
    margin = review.get("name_resolution_margin")
    dimensions = review.get("name_resolution_evidence_dimensions")
    evidence = review.get("name_resolution_evidence")
    required = {"name", "description", "cost", "rarity", "icon_color"}
    if not (
        review.get("name_binding_source") == "catalog_corroborated_ocr_alias"
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and float(confidence) >= 0.95
        and isinstance(margin, (int, float))
        and not isinstance(margin, bool)
        and float(margin) >= 0.25
        and isinstance(dimensions, int)
        and not isinstance(dimensions, bool)
        and dimensions >= len(required)
        and isinstance(evidence, Mapping)
        and all(
            isinstance(evidence.get(key), (int, float))
            and not isinstance(evidence.get(key), bool)
            and float(evidence[key]) >= 0.95
            for key in required
        )
    ):
        return False
    aliases = review.get("observed_name_aliases")
    if not isinstance(aliases, list):
        return False
    trusted = {
        alias.get("text")
        for alias in aliases
        if isinstance(alias, Mapping)
        and isinstance(alias.get("text"), str)
        and alias.get("text")
        and isinstance(alias.get("confidence"), (int, float))
        and not isinstance(alias.get("confidence"), bool)
        and float(alias["confidence"]) >= 0.45
    }
    return any(
        _supported_exact(candidates, alias, minimum=0.40, count=2)
        for alias in trusted
    )


def _final_green_control(image: np.ndarray) -> dict[str, int] | None:
    candidates = []
    for button in Perception().detect_green_buttons(image):
        x, y, width, height = button.bbox
        center_x, center_y = button.center
        if (
            button.kind == "primary_green"
            and button.score >= 0.90
            and 350 <= x <= 390
            and 1120 <= y <= 1160
            and 250 <= width <= 320
            and 60 <= height <= 95
            and 495 <= center_x <= 540
            and 1160 <= center_y <= 1205
        ):
            candidates.append(button)
    if len(candidates) != 1:
        return None
    button = candidates[0]
    x, y, width, height = button.bbox
    if _bright_green_ratio(image[y : y + height, x : x + width]) < 0.75:
        return None
    return {"x": int(button.center[0]), "y": int(button.center[1])}


def _bright_green_ratio(image: np.ndarray) -> float:
    if image.size == 0:
        return 0.0
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return float(
        np.mean(
            (hsv[:, :, 0] >= 35)
            & (hsv[:, :, 0] <= 85)
            & (hsv[:, :, 1] >= 130)
            & (hsv[:, :, 2] >= 150)
        )
    )


def _white_ratio(image: np.ndarray) -> float:
    if image.size == 0:
        return 0.0
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return float(np.mean((hsv[:, :, 1] < 45) & (hsv[:, :, 2] > 180)))


def _contains_forbidden_resource_terms(text: str) -> bool:
    compact = _compact(text)
    return any(term in compact for term in _FORBIDDEN_RESOURCE_TERMS)


def _valid_review(review: Any) -> bool:
    return (
        isinstance(review, Mapping)
        and review.get("current_run_scope_only") is True
        and isinstance(review.get("item_id"), str)
        and isinstance(review.get("observed_name"), str)
        and isinstance(review.get("required_description_token"), str)
        and isinstance(review.get("source_report_sha256"), str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", review["source_report_sha256"]))
    )


def _valid_control(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("x"), int)
        and not isinstance(value.get("x"), bool)
        and isinstance(value.get("y"), int)
        and not isinstance(value.get("y"), bool)
    )


def _valid_bbox(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
        and value[0] < value[2]
        and value[1] < value[3]
    )


def _compact(text: Any) -> str:
    return re.sub(r"\s+", "", str(text))


def _description_token_matches(expected: Any, observed: Any) -> bool:
    punctuation = str.maketrans({"：": ":", "，": ":", ",": ":"})
    expected_compact = _compact(expected).translate(punctuation)
    observed_compact = _compact(observed).translate(punctuation)
    return bool(expected_compact) and expected_compact in observed_compact


def _description_token_supported(
    expected: Any, candidates: Any, *, minimum: float = 0.80
) -> bool:
    if not isinstance(candidates, list):
        return False
    usable = [
        candidate
        for candidate in candidates
        if isinstance(candidate, Mapping)
        and isinstance(candidate.get("text"), str)
        and isinstance(candidate.get("confidence"), (int, float))
        and not isinstance(candidate.get("confidence"), bool)
        and float(candidate["confidence"]) >= minimum
    ]
    full_matches = [
        candidate
        for candidate in usable
        if _description_token_matches(expected, candidate["text"])
    ]
    if len(full_matches) >= 2:
        return True
    punctuation = str.maketrans({"：": ":", "，": ":", ",": ":"})
    expected_compact = _compact(expected).translate(punctuation)
    qualifier = expected_compact.rsplit(":", 1)[-1]
    qualifier_support = [
        candidate
        for candidate in usable
        if qualifier and qualifier in _compact(candidate["text"]).translate(punctuation)
    ]
    return len(full_matches) == 1 and len(qualifier_support) >= 2


def _rejected_state() -> dict[str, Any]:
    return {
        "accepted": False,
        "page_kind": "unknown",
        "screen_kind": "unknown",
        "dialog_title": None,
        "confirmed_item_ids": [],
        "observed_name": None,
        "description": None,
        "cancel_control": None,
        "final_confirm_control": None,
        "dangerous_resource_flags": [],
        "currency_scope": "unknown",
        "observed_currency": "unknown",
    }


def _rejected_success_state() -> dict[str, Any]:
    return {
        "accepted": False,
        "page_kind": "unknown",
        "screen_kind": "unknown",
        "dialog_title": None,
        "status_text": None,
        "confirmed_item_ids": [],
        "close_control": None,
        "dangerous_resource_flags": [],
        "currency_scope": "unknown",
        "observed_currency": "unknown",
    }


__all__ = [
    "CurrentRunMultiPageSkillDialogVerifier",
    "CurrentRunSkillDialogVerifier",
    "inspect_skill_batch_purchase_confirmation_dialog",
    "inspect_skill_multi_page_purchase_confirmation_dialog",
    "inspect_skill_multi_purchase_success_dialog",
    "inspect_skill_batch_purchase_success_dialog",
    "inspect_skill_purchase_confirmation_dialog",
    "inspect_skill_purchase_success_dialog",
]
