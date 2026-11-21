"""Race day entry, race list selection, fan shortfall, and confirmation handlers."""
from __future__ import annotations

import hashlib
import re
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from core.frame_io import read_image_with_retry
from core.auto_trainer.action_table import ActionTable
from core.auto_trainer.career_goal_perception import is_goal_achievement_title
from core.auto_trainer.larc_pages import (
    LARC_SYSTEM_GOAL_IDS,
    annotate_larc_race_page,
    larc_race_transition_kind,
    plan_larc_explanation,
)
from core.auto_trainer.frame_safety import FormalActionSafetyGate
from core.auto_trainer.runtime_control import reobserving_step, controlled_sleep
from core.auto_trainer.race_clock_retry import _ordinary_result_blocked, retry_prompt_visible
from core.auto_trainer.route_handoff import handoff, is_route_handoff
from .._compat import (
    FrameSafetyAnalyzer,
    LocalRapidOCRScreenTextReader,
    Perception,
    RaceRouteExecutor,
    RaceRoutePlanner,
    SafeAdvancePlanner,
    TrainingMenuPrechecker,
    _current_clock_retry,
    analyze_screen_text,
    classify_landscape_race_screen,
    fuse_perception_with_text,
    match_main_menu_named_goal,
    rr_attr,
)
from ..models import (
    RACE_ENTRY_POST_OBSERVATION_RETRIES,
    RACE_LIST_GRADE_REOBSERVATION_RETRIES,
    RACE_READ_ONLY_REOBSERVATION_DELAY_SECONDS,
    RACE_ROUTE_CONFIRM_TOKEN,
    RACE_SCENE_WAIT_REASONS,
    RESULT_TRANSITION_RECAPTURE_DELAY_SECONDS,
    RESULT_TRANSITION_RECAPTURE_LIMIT,
    SUPPORTED_RACE_ROUTE_SCREEN_SIZES,
    RaceRoutePolicy,
    RaceRouteSession,
    _RACE_ACCOUNT_CONFLICT_TEXT,
    is_race_scene_wait_result,
    larc_exchange_entry_released,
)


class EntryAndListHandlerMixin:
    """Race list badge, fan goal shortfall, WBC/GUR, and confirmation handlers."""

    def _race_day_main_menu_decision(
        self,
        img: np.ndarray,
        policy: RaceRoutePolicy,
        evidence: dict[str, Any],
        *,
        scenario_final: bool = False,
    ) -> dict[str, Any]:
        if not policy.allow_enter_race:
            return self._stop("race_day:enter_not_allowed", evidence=evidence)
        height, width = img.shape[:2]
        decision = self._configured_decision(
            "race_day_main_menu",
            img,
            evidence,
        ) or self._tap(
            "enter_race_from_race_day",
            "race_day_main_menu",
            "race_day.practice",
            # The red strip above the card is a label, not the hit target.
            # Keep the tap in the center of the lower race card across the
            # reference 720x1280 portrait layout.
            (int(width * 0.50), int(height * 0.85)),
            evidence,
        )
        decision["post_tap_confirmation"] = "race_day_entry_race_list"
        if scenario_final:
            decision["scenario_final_verification"] = {
                "source": "grand_masters_final_race_day",
                "scenario": "grand_masters",
                "cup": "glory_goddess",
                "single_use": True,
            }
        elif policy.allow_locked_race_entry is True:
            image_path = Path(str(evidence.get("image", "")))
            if image_path.is_file():
                decision["locked_target_verification"] = {
                    "source": "race_day_main_menu.locked_target",
                    "action_id": "race_day.practice",
                    "source_image_sha256": hashlib.sha256(
                        image_path.read_bytes()
                    ).hexdigest(),
                    "single_use": True,
                }
                title = "".join(str(evidence.get("screen_text", {}).get("normalized_text", "")).split())
                if policy.scenario == "larc" and "代表交流赛" in title:
                    decision["locked_target_verification"]["race_kind"] = "larc_exchange"
        return decision

    @staticmethod
    def _looks_like_race_day_main_menu_action_layout(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        normalized = cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
        cyan = (
            (hsv[:, :, 0] >= 80)
            & (hsv[:, :, 0] <= 105)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 100)
        )
        red = (
            ((hsv[:, :, 0] <= 8) | (hsv[:, :, 0] >= 175))
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 100)
        )
        purple = (
            (hsv[:, :, 0] >= 125)
            & (hsv[:, :, 0] <= 150)
            & (hsv[:, :, 1] > 60)
            & (hsv[:, :, 2] > 80)
        )

        def ratio(
            mask: np.ndarray,
            x0: float,
            x1: float,
            y0: float,
            y1: float,
        ) -> float:
            crop = mask[
                int(1280 * y0) : int(1280 * y1),
                int(720 * x0) : int(720 * x1),
            ]
            return float(np.mean(crop)) if crop.size else 0.0

        left_cyan = ratio(cyan, 0.02, 0.30, 0.77, 0.91)
        center_red = ratio(red, 0.25, 0.75, 0.77, 0.91)
        right_red = ratio(red, 0.70, 0.98, 0.77, 0.91)
        center_purple = ratio(purple, 0.25, 0.75, 0.77, 0.91)
        return bool(
            left_cyan > 0.10
            and center_red > 0.12
            and right_red > 0.15
            and center_purple < 0.06
        )

    @staticmethod
    def _selected_race_card_evidence(img: np.ndarray) -> dict[str, Any] | None:
        """Classify the grade badge on the currently selected race card only."""

        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        badge = hsv[
            int(height * 0.545) : int(height * 0.605),
            int(width * 0.35) : int(width * 0.48),
        ]
        if badge.size == 0:
            return None
        blue = float(
            np.mean(
                (badge[:, :, 0] >= 85)
                & (badge[:, :, 0] <= 125)
                & (badge[:, :, 1] > 60)
                & (badge[:, :, 2] > 90)
            )
        )
        pink = float(
            np.mean(
                ((badge[:, :, 0] <= 8) | (badge[:, :, 0] >= 165))
                & (badge[:, :, 1] > 55)
                & (badge[:, :, 2] > 130)
            )
        )
        orange = float(
            np.mean(
                (badge[:, :, 0] >= 5)
                & (badge[:, :, 0] <= 35)
                & (badge[:, :, 1] > 60)
                & (badge[:, :, 2] > 100)
            )
        )
        green = float(
            np.mean(
                (badge[:, :, 0] >= 38)
                & (badge[:, :, 0] <= 92)
                & (badge[:, :, 1] > 70)
                & (badge[:, :, 2] > 80)
            )
        )
        if blue > 0.08 and pink < 0.10:
            grade = "G1"
        elif pink > 0.10:
            grade = "G2"
        elif green > 0.08:
            grade = "G3"
        elif orange > 0.10 and blue < 0.05 and pink < 0.05 and green < 0.05:
            grade = "OPEN"
        else:
            grade = "unknown"
        return {
            "source": "selected_race_card.grade_badge",
            "grade": grade,
            "grade_scores": {
                "blue": round(blue, 4),
                "pink": round(pink, 4),
                "orange": round(orange, 4),
                "green": round(green, 4),
            },
        }

    @staticmethod
    def _looks_like_race_list_postcondition(img: np.ndarray, visible_text: str | None = None) -> bool:
        """Identify the list only; its colors/grade cannot authorize entry.

        This is the destination of an already-authorized navigation, also used
        by read-only inspection. Race selection and entry keep their separate
        target, eligibility, enabled-button and confirmation checks.
        """
        if img is None or img.shape[1] > img.shape[0]:return False
        if visible_text is None:
            try:
                rows=LocalRapidOCRScreenTextReader().read_regions(img)
                visible_text='\n'.join(str(row[1]) for row in rows if len(row)>1)
            except Exception:return False
        text=''.join(str(visible_text).split())
        if '赛事一览' not in text:return False
        if any(term in text for term in ('参赛确认','是否参赛','是否使用','回复训练值','恢复训练值',
                '购买宝石','购买萝卜','支付','其他设备登录','别处登录')):return False
        navigation='返回' in text and any(term in text for term in ('参赛计划','预测'))
        race_card=(any(term in text for term in ('草地','泥地')) and
                   re.search(r'[0-9]{3,4}(?:米|m)',text,re.IGNORECASE) is not None)
        return bool(navigation and race_card)

    @staticmethod
    def _visible_g1_race_candidate(img: np.ndarray) -> dict[str, Any] | None:
        """Return a unique visible G1 row badge without authorizing race entry."""

        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        x1 = int(width * 0.34)
        x2 = int(width * 0.50)
        y1 = int(height * 0.52)
        y2 = int(height * 0.82)
        roi = hsv[y1:y2, x1:x2]
        if roi.size == 0:
            return None
        blue = (
            (roi[:, :, 0] >= 85)
            & (roi[:, :, 0] <= 125)
            & (roi[:, :, 1] > 60)
            & (roi[:, :, 2] > 90)
        ).astype(np.uint8)
        count, _, stats, centers = cv2.connectedComponentsWithStats(
            blue,
            connectivity=8,
        )
        candidates: list[dict[str, Any]] = []
        scale_x = width / 720.0
        scale_y = height / 1280.0
        for index in range(1, count):
            local_x, local_y, candidate_width, candidate_height, area = (
                int(value) for value in stats[index]
            )
            if not (
                candidate_width >= int(round(40 * scale_x))
                and candidate_height >= int(round(16 * scale_y))
                and area >= int(round(500 * scale_x * scale_y))
            ):
                continue
            center_x = int(round(float(centers[index][0]) + x1))
            center_y = int(round(float(centers[index][1]) + y1))
            candidates.append(
                {
                    "bbox": [
                        local_x + x1,
                        local_y + y1,
                        candidate_width,
                        candidate_height,
                    ],
                    "center": [center_x, center_y],
                    "blue_area": area,
                }
            )
        if len(candidates) != 1:
            return None
        return {
            "source": "race_list.visible_grade_badge",
            "grade": "G1",
            "candidate_count": 1,
            **candidates[0],
        }

    @staticmethod
    def _race_list_viewport_sha256(img: np.ndarray) -> str:
        height, width = img.shape[:2]
        viewport = img[
            int(height * 0.52) : int(height * 0.82),
            int(width * 0.18) : int(width * 0.95),
        ]
        return hashlib.sha256(viewport.tobytes()).hexdigest()

    def _fan_goal_shortfall_candidate_evidence(
        self,
        img: np.ndarray,
        selected_race: dict[str, Any] | None,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Return one card candidate from the ordinary fan-shortfall list.

        This is intentionally separate from the old, game-rendered 3000-fan
        recommendation card.  The page header first opts into the narrow path;
        all candidate facts then come from one positioned local OCR pass.
        """

        height, width = img.shape[:2]
        if width > height:
            return False, None
        try:
            regions = self._fan_shortfall_regions_reader.read_regions(img)
        except Exception:
            return True, None
        parsed: list[tuple[tuple[int, int, int, int], str, float]] = []
        low_confidence_surface_star_boxes: list[tuple[int, int, int, int]] = []
        for item in regions:
            try:
                box, text, confidence = item[0], str(item[1]).strip(), float(item[2])
                xs = [float(point[0]) for point in box]
                ys = [float(point[1]) for point in box]
            except (IndexError, TypeError, ValueError):
                continue
            if text == "★草地" and 0.0 < confidence < 0.85:
                low_confidence_surface_star_boxes.append(
                    (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
                )
            if not text or confidence < 0.85:
                continue
            parsed.append(
                ((int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))), text, confidence)
            )

        header = [
            item for item in parsed
            if item[0][1] < int(height * 0.16)
            and (
                "参赛条件剩余" in item[1]
                or "达成条件剩余" in item[1]
            )
        ]
        if len(header) != 1:
            return True, None
        header_box, _, _ = header[0]
        shortfall_matches = [
            (item, re.fullmatch(r"([1-9][0-9,]*)人", item[1]))
            for item in parsed
            if abs(item[0][1] - header_box[1]) <= int(height * 0.04)
            and item[0][0] > header_box[2]
        ]
        shortfalls = [
            (item, int(match.group(1).replace(",", "")))
            for item, match in shortfall_matches
            if match is not None
        ]
        if len(shortfalls) != 1:
            return True, None
        shortfall_item, shortfall = shortfalls[0]

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        green = (
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )
        candidates: list[dict[str, Any]] = []
        for row_index, (row_top, row_bottom) in enumerate(
            ((0.54, 0.67), (0.675, 0.79))
        ):
            card_y0, card_y1 = int(height * row_top), int(height * row_bottom)
            card_bbox = [
                int(width * 0.03),
                card_y0,
                int(width * 0.97),
                card_y1,
            ]
            card_items = [
                item
                for item in parsed
                if (
                    card_bbox[0] <= item[0][0]
                    and item[0][2] <= card_bbox[2]
                    and card_y0 <= item[0][1]
                    and item[0][3] <= card_y1
                )
            ]
            grade = self._fan_shortfall_card_grade(hsv, row_top)
            if grade not in {"G1", "G2", "G3"}:
                continue
            names = [
                item
                for item in card_items
                if item[0][0] < int(width * 0.35)
                and not item[1].startswith(("G", "★", "+"))
                and "人" not in item[1]
            ]
            rewards = [
                (item, re.fullmatch(r"\+([1-9][0-9,]*)人", item[1]))
                for item in card_items
                if int(width * 0.40) <= item[0][0] <= int(width * 0.70)
            ]
            rewards = [
                (item, int(match.group(1).replace(",", "")))
                for item, match in rewards
                if match is not None
            ]
            distance_rows = [
                item
                for item in card_items
                if item[0][0] >= int(width * 0.35)
                and item[0][3] <= int(height * (row_top + 0.065))
                and not item[1].startswith("★")
                and re.search(r"(短距离|英里|中距离|长距离)", item[1]) is not None
            ]
            grade_texts: set[str] = set()
            for item in card_items:
                if not (
                    int(width * 0.35) <= item[0][0]
                    and item[0][2] <= int(width * 0.48)
                    and item[0][3] <= int(height * (row_top + 0.065))
                ):
                    continue
                normalized_grade = (
                    item[1].upper()
                    .replace("Ⅰ", "I")
                    .replace("Ⅱ", "II")
                    .replace("Ⅲ", "III")
                    .replace("1", "I")
                )
                normalized_grade = re.sub(r"[^A-Z]", "", normalized_grade)
                parsed_grade = {
                    "GI": "G1",
                    "GII": "G2",
                    "GIII": "G3",
                    "OP": "OPEN",
                    "OPEN": "OPEN",
                }.get(normalized_grade)
                if parsed_grade is not None:
                    grade_texts.add(parsed_grade)
            if grade_texts and grade_texts != {grade}:
                continue
            if grade == "G1" and grade_texts != {"G1"}:
                continue
            if not (1 <= len(names) <= 2 and len(rewards) == 1 and len(distance_rows) == 1):
                continue
            distance_item = distance_rows[0]
            distance_match = re.search(
                r"(短距离|英里|中距离|长距离)", distance_item[1]
            )
            assert distance_match is not None
            distance = distance_match.group(1)
            star_text = {
                item[1]
                for item in card_items
                if item[1].startswith("★")
                and item[0][0] >= int(width * 0.75)
            }
            for star_item in card_items:
                if star_item[1] != "★" or star_item[0][0] < int(width * 0.75):
                    continue
                for label_item in card_items:
                    if (
                        label_item[1] in {"草地", "短距离", "英里", "中距离", "长距离"}
                        and label_item[0][0] >= star_item[0][2] - int(width * 0.02)
                        and abs(label_item[0][1] - star_item[0][1])
                        <= int(height * 0.015)
                    ):
                        star_text.add(f"★{label_item[1]}")
            surface_star_icon = None
            if "★草地" not in star_text and "草地" in distance_item[1]:
                icon_evidence = [
                    evidence
                    for box in low_confidence_surface_star_boxes
                    if card_y0 <= box[1]
                    and box[3] <= card_y1
                    and (evidence := self._surface_star_icon_evidence(img, box))
                    is not None
                ]
                if len(icon_evidence) == 1:
                    surface_star_icon = icon_evidence[0]
            if (
                ("★草地" not in star_text and surface_star_icon is None)
                or f"★{distance}" not in star_text
            ):
                continue
            reward_item, reward = rewards[0]
            if reward < shortfall:
                continue
            names.sort(key=lambda item: (item[0][1], item[0][0]))
            race_name = "".join(item[1] for item in names)
            corners = [
                float(
                    np.mean(
                        green[
                            int(height * y0) : int(height * y1),
                            int(width * x0) : int(width * x1),
                        ]
                    )
                )
                for x0, x1, y0, y1 in (
                    (0.014, 0.090, row_top, row_top + 0.05),
                    (0.903, 0.993, row_top, row_top + 0.05),
                    (0.014, 0.090, row_bottom - 0.04, row_bottom + 0.01),
                    (0.903, 0.993, row_bottom - 0.04, row_bottom + 0.01),
                )
            ]
            selected = all(value > 0.07 for value in corners)
            name_bbox = [
                min(item[0][0] for item in names),
                min(item[0][1] for item in names),
                max(item[0][2] for item in names),
                max(item[0][3] for item in names),
            ]
            selected_detail_names = [
                item
                for item in parsed
                if (
                    int(width * (0.50 if grade == "G1" else 0.38)) <= item[0][0]
                    and item[0][2] <= int(width * 0.90)
                    and int(height * 0.235) <= item[0][1]
                    and item[0][3] <= int(height * (0.34 if grade == "G1" else 0.305))
                    and not item[1].startswith(("G", "+", "★"))
                    and re.fullmatch(r"[A-Za-z]+", item[1]) is None
                    and "人" not in item[1]
                )
            ]
            selected_detail_name = (
                "".join(item[1] for item in sorted(
                    selected_detail_names,
                    key=lambda item: (item[0][1], item[0][0]),
                ))
                if selected_detail_names
                else None
            )
            if selected and selected_detail_name != race_name:
                if selected_detail_name and race_name in selected_detail_name:
                    continue  # A partial logo does not invalidate another complete card.
                return True, None
            candidates.append(
                {
                    "race_name": race_name,
                    "grade": grade,
                    "fan_shortfall": shortfall,
                    "fan_reward": reward,
                    "gold_star_surface_distance_evidence": ["草地", distance],
                    "card_bbox": card_bbox,
                    "card_center": [
                        int((card_bbox[0] + card_bbox[2]) / 2),
                        int((card_bbox[1] + card_bbox[3]) / 2),
                    ],
                    "row_index": row_index,
                    "selected": selected,
                    "selected_corner_green": [round(value, 4) for value in corners],
                    "shortfall_bbox": list(shortfall_item[0]),
                    "reward_bbox": list(reward_item[0]),
                    "name_bbox": name_bbox,
                    "distance_bbox": list(distance_item[0]),
                    **(
                        {
                            "selected_detail_name": selected_detail_name,
                            "selected_detail_name_bboxes": [
                                list(item[0]) for item in selected_detail_names
                            ],
                        }
                        if selected_detail_name is not None
                        else {}
                    ),
                    **(
                        {"surface_star_icon_evidence": surface_star_icon}
                        if surface_star_icon is not None
                        else {}
                    ),
                }
            )
        selected_candidates = [item for item in candidates if item["selected"]]
        if len(selected_candidates) == 1:
            return True, selected_candidates[0]
        if len(candidates) == 1:
            return True, candidates[0]
        return True, None

    @staticmethod
    def _fan_shortfall_card_grade(hsv: np.ndarray, row_top: float) -> str:
        height, width = hsv.shape[:2]
        y0 = int(height * (row_top + 0.012))
        y1 = int(height * (row_top + 0.055))
        badge = hsv[y0:y1, int(width * 0.35) : int(width * 0.48)]
        if badge.size == 0:
            return "unknown"
        pink = float(
            np.mean(
                ((badge[:, :, 0] <= 8) | (badge[:, :, 0] >= 165))
                & (badge[:, :, 1] > 55)
                & (badge[:, :, 2] > 130)
            )
        )
        green = float(
            np.mean(
                (badge[:, :, 0] >= 38)
                & (badge[:, :, 0] <= 92)
                & (badge[:, :, 1] > 70)
                & (badge[:, :, 2] > 80)
            )
        )
        if pink > 0.10:
            return "G2"
        if green > 0.10:
            return "G3"
        blue = float(np.mean(
            (badge[:, :, 0] >= 85) & (badge[:, :, 0] <= 125)
            & (badge[:, :, 1] > 60) & (badge[:, :, 2] > 90)
        ))
        if blue > 0.10:
            return "G1"
        return "unknown"

    @staticmethod
    def _surface_star_icon_evidence(
        img: np.ndarray,
        star_box: tuple[int, int, int, int],
    ) -> dict[str, Any] | None:
        """Verify only a low-confidence first-card ``★草地`` icon."""

        height, width = img.shape[:2]
        if width > height:
            return None
        scale_x = width / 720.0
        scale_y = height / 1280.0

        def scaled_box(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
            return tuple(
                int(round(value * (scale_x if index % 2 == 0 else scale_y)))
                for index, value in enumerate(box)
            )

        label_box = scaled_box((568, 761, 662, 806))
        if not (
            label_box[0] <= star_box[0]
            and star_box[2] <= label_box[2]
            and label_box[1] <= star_box[1]
            and star_box[3] <= label_box[3]
        ):
            return None
        icon_box = scaled_box((572, 764, 603, 800))
        x0, y0, x1, y1 = icon_box
        icon = img[y0:y1, x0:x1]
        if icon.size == 0:
            return None
        roi = cv2.cvtColor(icon, cv2.COLOR_BGR2HSV)
        gold = (
            (roi[:, :, 0] >= 12)
            & (roi[:, :, 0] <= 40)
            & (roi[:, :, 1] >= 90)
            & (roi[:, :, 2] >= 100)
        ).astype(np.uint8)
        contours, _ = cv2.findContours(
            gold,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_NONE,
        )
        min_width = max(12, int(round(18 * scale_x)))
        max_width = max(min_width, int(round(30 * scale_x)))
        min_height = max(12, int(round(18 * scale_y)))
        max_height = max(min_height, int(round(30 * scale_y)))
        for contour in contours:
            x, y, contour_width, contour_height = cv2.boundingRect(contour)
            if not (
                min_width <= contour_width <= max_width
                and min_height <= contour_height <= max_height
            ):
                continue
            hull = cv2.convexHull(contour)
            hull_area = cv2.contourArea(hull)
            contour_area = cv2.contourArea(contour)
            if hull_area <= 0:
                continue
            solidity = contour_area / hull_area
            if not 0.50 <= solidity <= 0.70:
                continue
            hull_perimeter = cv2.arcLength(hull, True)
            vertices = cv2.approxPolyDP(hull, 0.03 * hull_perimeter, True)
            if len(vertices) not in {5, 6}:
                continue
            points = vertices.reshape(-1, 2).astype(np.float32)
            points[:, 0] -= x
            points[:, 1] -= y
            if not (
                np.min(points[:, 1]) <= contour_height * 0.20
                and contour_width * 0.25
                <= points[np.argmin(points[:, 1]), 0]
                <= contour_width * 0.75
                and np.any(
                    (points[:, 0] <= contour_width * 0.15)
                    & (points[:, 1] <= contour_height * 0.65)
                )
                and np.any(
                    (points[:, 0] >= contour_width * 0.85)
                    & (points[:, 1] <= contour_height * 0.65)
                )
                and np.count_nonzero(points[:, 1] >= contour_height * 0.80) >= 2
            ):
                continue
            hull_indices = cv2.convexHull(contour, returnPoints=False)
            defects = cv2.convexityDefects(contour, hull_indices)
            deep_defects = (
                0
                if defects is None
                else sum(
                    defect[0][3] / 256.0 >= 3.0 * min(scale_x, scale_y)
                    for defect in defects
                )
            )
            if deep_defects < 4:
                continue
            return {
                "source": "race_list.surface_star_icon_shape",
                "icon_bbox": [x0, y0, x1, y1],
                "contour_bbox": [x + x0, y + y0, contour_width, contour_height],
                "solidity": round(float(solidity), 4),
                "hull_vertices": len(vertices),
                "deep_defects": int(deep_defects),
            }
        return None

    def _fan_goal_recommended_race_evidence(
        self,
        evidence: dict[str, Any],
        img: np.ndarray,
    ) -> dict[str, Any] | None:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return None

        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def ratio(
            x0: float,
            x1: float,
            y0: float,
            y1: float,
            mask: np.ndarray,
        ) -> float:
            crop = mask[
                int(height * y0) : int(height * y1),
                int(width * x0) : int(width * x1),
            ]
            return float(np.mean(crop)) if crop.size else 0.0

        green = (
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )
        orange = (
            (hsv[:, :, 0] >= 5)
            & (hsv[:, :, 0] <= 25)
            & (hsv[:, :, 1] > 80)
            & (hsv[:, :, 2] > 120)
        )
        selected_corner_green = [
            ratio(0.014, 0.090, 0.539, 0.590, green),
            ratio(0.903, 0.993, 0.539, 0.590, green),
            ratio(0.014, 0.090, 0.637, 0.680, green),
            ratio(0.903, 0.993, 0.637, 0.680, green),
        ]
        recommendation_orange = ratio(0.785, 0.993, 0.531, 0.594, orange)
        entry_green = ratio(0.292, 0.701, 0.809, 0.879, green)
        if (
            any(value <= 0.07 for value in selected_corner_green)
            or recommendation_orange <= 0.04
            or entry_green <= 0.65
        ):
            return None

        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        rewards = self._positive_rewards(normalized)
        positive_fan_reward = rewards[0] if rewards else 0
        text_source = "full_frame_tesseract"
        if "获得3000名粉丝" not in normalized or positive_fan_reward <= 0:
            localized_goal, localized_reward = self.fan_goal_text_reader(img)
            normalized_goal = "".join(str(localized_goal).split())
            if "获得3000名粉丝" not in normalized_goal:
                return None
            if positive_fan_reward > 0:
                text_source = "localized_goal_full_frame_reward"
            else:
                localized_rewards = self._positive_rewards(
                    "".join(str(localized_reward).split())
                )
                if not localized_rewards:
                    return None
                positive_fan_reward = localized_rewards[0]
                text_source = "localized_targeted_tesseract"
        return {
            "source": "race_list.fan_goal_recommended_card",
            "goal_kind": "fans",
            "goal_target": 3000,
            "recommendation_rank": 1,
            "positive_fan_reward": positive_fan_reward,
            "text_source": text_source,
            "selected_corner_green": [round(value, 4) for value in selected_corner_green],
            "recommendation_orange": round(recommendation_orange, 4),
            "entry_green": round(entry_green, 4),
        }

    @staticmethod
    def _positive_rewards(text: str) -> list[int]:
        return [
            int(match.replace(",", "").replace(".", ""))
            for match in re.findall(r"\+([0-9][0-9,.]*)", text)
            if match.replace(",", "").replace(".", "").isdigit()
        ]

    @staticmethod
    def _localized_fan_goal_texts(img: np.ndarray) -> tuple[str, str]:
        normalized = cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        crops = (
            normalized[65:140, 150:715],
            normalized[700:860, 20:710],
        )
        texts: list[str] = []
        try:
            with tempfile.TemporaryDirectory(prefix="uma-fan-goal-ocr-") as directory:
                for index, crop in enumerate(crops):
                    enlarged = cv2.resize(
                        crop,
                        None,
                        fx=4,
                        fy=4,
                        interpolation=cv2.INTER_CUBIC,
                    )
                    path = Path(directory) / f"roi_{index}.png"
                    if not cv2.imwrite(str(path), enlarged):
                        return "", ""
                    packet = FrameSafetyAnalyzer().analyze(path)
                    if packet.text_status != "ok":
                        return "", ""
                    texts.append(packet.visible_text)
        except (OSError, RuntimeError, ValueError, cv2.error):
            return "", ""
        return texts[0], texts[1]

    @staticmethod
    def _has_pending_target_race(
        session: RaceRouteSession | None,
        evidence: dict[str, Any],
    ) -> bool:
        verification = session.target_race_verification() if session is not None else None
        locked_verification = (
            session.locked_target_race_verification()
            if session is not None
            else None
        )
        if verification is not None and locked_verification is not None:
            evidence["ambiguous_target_race_verification"] = True
            return False
        if verification is None:
            verification = locked_verification
        if verification is None:
            return False
        evidence["prior_target_race_verification"] = verification
        return True

    @staticmethod
    def _pending_scenario_final_race(
        session: RaceRouteSession | None,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        verification = (
            session.scenario_final_race_verification()
            if session is not None
            else None
        )
        if verification is not None:
            evidence["prior_scenario_final_verification"] = verification
        return verification

    @staticmethod
    def _pending_fan_goal_recommendation(
        session: RaceRouteSession | None,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        verification = (
            session.fan_goal_recommendation_verification()
            if session is not None
            else None
        )
        if verification is not None:
            evidence["prior_fan_goal_recommendation_verification"] = verification
        return verification

    @staticmethod
    def _pending_fan_goal_shortfall_selected(
        session: RaceRouteSession | None,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        verification = (
            session.fan_goal_shortfall_selected_verification()
            if session is not None
            else None
        )
        if verification is not None:
            evidence["prior_fan_goal_shortfall_selected_verification"] = verification
        return verification

    def _fan_goal_shortfall_confirmation(
        self,
        evidence: dict[str, Any],
        pending: dict[str, Any] | None,
        img: np.ndarray | None,
    ) -> dict[str, Any] | None:
        if img is None or pending is None or img.shape[:2] not in {(1280, 720), (2560, 1440)}:
            return None
        race_name = pending.get("race_name")
        expected_grade = pending.get("grade")
        if not isinstance(race_name, str) or expected_grade not in {"G1", "G2", "G3"}:
            return None
        height, width = img.shape[:2]
        try:
            regions = self._fan_shortfall_regions_reader.read_regions(img)
        except Exception:
            return None
        parsed: list[tuple[tuple[int, int, int, int], str, float]] = []
        for item in regions:
            try:
                box, text, confidence = item[0], str(item[1]).strip(), float(item[2])
                xs = [float(point[0]) for point in box]
                ys = [float(point[1]) for point in box]
            except (IndexError, TypeError, ValueError):
                continue
            if text:
                parsed.append(
                    ((int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))), text, confidence)
                )

        def inside(
            item: tuple[tuple[int, int, int, int], str, float],
            x0: float,
            x1: float,
            y0: float,
            y1: float,
        ) -> bool:
            box = item[0]
            return (
                int(width * x0) <= box[0]
                and box[2] <= int(width * x1)
                and int(height * y0) <= box[1]
                and box[3] <= int(height * y1)
            )

        page_titles = [
            item
            for item in parsed
            if item[1] == "赛事详情"
            and item[2] >= 0.85
            and inside(item, 0.25, 0.75, 0.22, 0.29)
        ]
        prompts = [
            item
            for item in parsed
            if "要参加比赛吗" in item[1]
            and item[2] >= 0.85
            and inside(item, 0.25, 0.75, 0.62, 0.70)
        ]
        name_items = [
            item
            for item in parsed
            if item[2] >= 0.85 and inside(item, 0.18, 0.82, 0.36, 0.47)
        ]
        name_items.sort(key=lambda item: (item[0][1], item[0][0]))
        confirmed_name = "".join("".join(item[1].split()) for item in name_items)

        grades: list[tuple[tuple[int, int, int, int], str, float]] = []
        for item in parsed:
            if item[2] < 0.60 or not inside(item, 0.02, 0.22, 0.28, 0.35):
                continue
            roman = re.sub(r"[^GI1!|L]", "", item[1].upper())
            roman = roman.replace("1", "I").replace("!", "I").replace("|", "I").replace("L", "I")
            grade = {"GI": "G1", "GII": "G2", "GIII": "G3"}.get(roman)
            if grade is not None:
                grades.append((item[0], grade, item[2]))

        matching_grades = [item for item in grades if item[1] == expected_grade]
        if (
            len(page_titles) != 1
            or len(prompts) != 1
            or not name_items
            or confirmed_name != "".join(race_name.split())
            or len(matching_grades) != 1
        ):
            return None
        return {
            "source": "race_detail_confirm.foreground_positioned_ocr",
            "race_name": race_name,
            "grade": expected_grade,
            "title_bboxes": [list(item[0]) for item in name_items],
            "grade_bbox": list(matching_grades[0][0]),
            "prompt_bbox": list(prompts[0][0]),
            "image": evidence.get("image"),
        }

    @staticmethod
    def _pending_wbc_hub_race(
        session: RaceRouteSession | None,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        verification = (
            session.wbc_hub_race_verification()
            if session is not None
            else None
        )
        if verification is not None:
            evidence["prior_wbc_hub_verification"] = verification
        return verification

    @staticmethod
    def _pending_gur_race(
        session: RaceRouteSession | None,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        verification = (
            session.gur_race_verification()
            if session is not None
            else None
        )
        if verification is not None:
            evidence["prior_gur_race_verification"] = verification
        return verification

    @staticmethod
    def _wbc_race_confirmation(
        evidence: dict[str, Any],
        img: np.ndarray | None = None,
    ) -> dict[str, Any] | None:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        upper = normalized.upper()
        participation_prompt = (
            "是否参加比赛" in normalized
            or "参加比赛" in normalized
            or "瑕佸弬鍔犳瘮璧涘悧" in normalized
        )
        if "WBC" not in upper:
            return None
        if participation_prompt:
            return {
                "source": "race_detail_confirm.wbc_structured_text",
                "scenario": "grand_masters",
            }
        perception = evidence.get("perception")
        if (
            img is None
            or not isinstance(perception, dict)
            or perception.get("screen") != "race_detail_confirm"
            or "MILE" not in upper
            or not SafeAdvancePlanner._looks_like_unreadable_resource_modal(img)
        ):
            return None
        return {
            "source": "race_detail_confirm.wbc_bound_visual",
            "scenario": "grand_masters",
        }

    @staticmethod
    def _race_confirm_cancel_point(
        evidence: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int]:
        height, width = img.shape[:2]
        perception = evidence.get("perception")
        if isinstance(perception, dict):
            modal_buttons: list[tuple[float, int]] = []
            for button in perception.get("buttons", []):
                cx, cy = button.get("center", [0, 0])
                if not (width * 0.45 <= cx <= width * 0.93):
                    continue
                if not (height * 0.58 <= cy < height * 0.84):
                    continue
                modal_buttons.append((float(button.get("area", 0)), int(cy)))
            if modal_buttons:
                _, confirm_y = max(modal_buttons)
                return int(width * 0.28), confirm_y
        screen_text = evidence.get("screen_text")
        normalized = (
            str(screen_text.get("normalized_text", ""))
            if isinstance(screen_text, dict)
            else ""
        )
        compact_upper = "".join(normalized.upper().split())
        bottom_modal_action = (
            "WBC" in compact_upper
            or "GROWUP" in compact_upper
            or ("海外比赛难关" in compact_upper and "已攻克的难关" in compact_upper)
        )
        y_ratio = 0.922 if bottom_modal_action else 0.72
        return int(width * 0.28), int(height * y_ratio)

    @staticmethod
    def _grand_masters_final_selected_list(evidence: dict[str, Any], img: np.ndarray) -> dict[str, Any] | None:
        if img.shape[:2] != (1280, 720):
            return None
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return None
        lines = {"".join(line.split()) for line in str(screen_text.get("normalized_text", "")).splitlines()}
        if not {"赛事一览", "比赛日", "目标", "特殊", "荣耀女神杯举办中",
                "荣耀女神杯中获得第1名", "参赛"}.issubset(lines):
            return None
        selected = RaceRoutePlanner._selected_race_card_evidence(img)
        if selected is None or selected.get("grade") != "OPEN":
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        green = ((hsv[:, :, 0] >= 38) & (hsv[:, :, 0] <= 92)
                 & (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 80))
        # Existing selected-card corner regions and threshold, not another card's badge.
        corners = [float(np.mean(green[int(1280*y0):int(1280*y1), int(720*x0):int(720*x1)]))
                   for x0, x1, y0, y1 in ((.014, .090, .539, .590), (.903, .993, .539, .590),
                                         (.014, .090, .637, .680), (.903, .993, .637, .680))]
        if any(value <= .07 for value in corners):
            return None
        return {"source": "grand_masters_final.selected_special_card", "scenario": "grand_masters",
                "cup": "glory_goddess",
                "selected_corner_green": corners}

    @staticmethod
    def _grand_masters_final_confirmation(
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        upper = normalized.upper()
        participant_evidence = (
            "满员16人" in normalized
            or ("1800" in normalized and "米" in normalized)
        )
        legacy_final = (
            "GRAND" in upper
            and "MASTERS" in upper
            and participant_evidence
            and "要参加比赛吗" in normalized
        )
        mile_final = (
            "GRAND" in upper
            and "ASTERS" in upper
            and "MILE" in upper
            and "要参加比赛吗" in normalized
        )
        if not (legacy_final or mile_final):
            return None
        return {
            "source": "race_detail_confirm.structured_text",
            "scenario": "grand_masters",
            "participant_count": 18 if mile_final else 16,
        }

    @staticmethod
    def _target_race_confirm_tap(
        state: str,
        button: tuple[int, int],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        decision = RaceRoutePlanner._tap(
            "confirm_race_entry",
            state,
            "race_confirm.primary",
            button,
            evidence,
        )
        decision["consume_target_race_verification"] = True
        return decision
