"""Fail-closed training menu precheck for bounded live tab capture.

This detector is intentionally narrower than general screen perception.  It
accepts a page only when multiple training-menu structures agree: fixed
portrait layout, five whitelisted training tab ROIs, one selected tab marker,
attribute/failure-rate structure, and absence of known blocking screens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from core.grand_masters_overlay import detect_grand_masters_crystal_reward_overlay
from core.main_menu_state import analyze_main_menu_state
from core.perception import Perception


EXPECTED_SIZE = (1440, 2560)
NORMALIZABLE_SIZES = {(720, 1280)}
TRAINING_TABS = ("speed", "stamina", "power", "guts", "wisdom")


@dataclass(frozen=True)
class TrainingMenuPrecheckResult:
    accepted: bool
    reason: str
    selected_tab: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


class TrainingMenuPrechecker:
    """Validate training-menu structure without broadening general perception."""

    def __init__(
        self,
        *,
        extra_forbidden_regions: list[tuple[str, tuple[int, int, int, int]]] | None = None,
        scenario: str = "grand_masters",
    ) -> None:
        if scenario not in {"grand_masters", "larc"}:
            raise ValueError(f"unsupported_training_layout:{scenario}")
        self.scenario = scenario
        self.extra_forbidden_regions = extra_forbidden_regions or []

    def check(self, image_path: str | Path, *, expected_tab: str | None = None) -> TrainingMenuPrecheckResult:
        path = Path(image_path)
        img = cv2.imread(str(path))
        if img is None:
            return self._reject(["image_read"], {"image": str(path)}, "cannot read image")

        original_height, original_width = img.shape[:2]
        original_size = (original_width, original_height)
        is_normalizable = original_size in NORMALIZABLE_SIZES
        normalized_from = [original_width, original_height] if is_normalizable else None
        effective_size = EXPECTED_SIZE if (original_size == EXPECTED_SIZE or is_normalizable) else original_size
        height, width = effective_size[1], effective_size[0]
        failures: list[str] = []
        evidence: dict[str, Any] = {
            "image": str(path),
            "layout": {"size": [width, height], "expected_size": list(EXPECTED_SIZE)},
        }
        if normalized_from is not None:
            evidence["layout"]["normalized_from"] = normalized_from

        if effective_size != EXPECTED_SIZE:
            return self._reject(["layout"], evidence, "unsupported training layout size")

        canonical = img if original_size == (720, 1280) else cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(canonical, cv2.COLOR_BGR2HSV)
        header = hsv[42:126, 15:149]
        blue_header = float(np.mean(
            (header[:, :, 0] > 90) & (header[:, :, 0] < 125)
            & (header[:, :, 1] > 100) & (header[:, :, 2] > 120)
        )) >= 0.22 and float(np.mean(
            (header[:, :, 1] < 65) & (header[:, :, 2] > 175)
        )) >= 0.30
        evidence["scenario_layout"] = {"scenario": self.scenario, "blue_header": blue_header}
        if blue_header != (self.scenario == "larc"):
            return self._reject(["scenario_layout_mismatch"], evidence, "scenario layout mismatch")
        if self.scenario == "larc":
            return self._check_larc(canonical, hsv, expected_tab=expected_tab, evidence=evidence)

        if is_normalizable:
            img = cv2.resize(img, EXPECTED_SIZE)

        perception = Perception().analyze_image(img)
        evidence["perception"] = {
            "screen": perception.get("screen"),
            "confidence": perception.get("confidence"),
        }
        if perception.get("screen") in {"event_choice", "modal_confirm", "landscape_action"}:
            failures.append("blocking_screen")
        if width > height:
            failures.append("layout")

        main_menu_state = analyze_main_menu_state(path.read_bytes())
        screen_field = main_menu_state.get("fields", {}).get("screen", {})
        evidence["atomic_main_menu_state"] = {
            "value": screen_field.get("value"),
            "confidence": screen_field.get("confidence", 0.0),
            "abstained": screen_field.get("abstained", True),
            "valid_for_training": main_menu_state.get("valid_for_training", False),
        }
        if (
            screen_field.get("value") == "main_menu"
            and float(screen_field.get("confidence", 0.0)) >= 0.8
            and not bool(screen_field.get("abstained", True))
        ):
            failures.append("main_menu")

        option_evidence = self._check_training_option_rois(img)
        evidence["training_option_rois"] = option_evidence

        selected_detection = self._detect_selected_tab(img)
        selected = selected_detection["selected_tab"]
        ribbon_best_tab = selected_detection.get("decision_ribbon_best_tab")
        ribbon_best_score = float(
            selected_detection.get("decision_ribbon_best_score", 0.0)
        )
        ribbon_margin = float(selected_detection.get("decision_ribbon_margin", 0.0))
        color_best_tab = selected_detection.get("best_tab")
        title_best_tab = selected_detection.get("title_icon_best_tab")
        title_best_score = float(selected_detection.get("title_icon_best_score", 0.0))
        title_margin = float(selected_detection.get("title_icon_margin", 0.0))
        marker_scores = selected_detection.get("marker_scores", {})
        marker_best_score = float(selected_detection.get("marker_best_score", 0.0))
        ribbon_marker_score = float(marker_scores.get(ribbon_best_tab, 0.0))
        ribbon_title_marker_tie_consensus = (
            ribbon_best_tab in {"speed", "stamina", "power", "guts", "wisdom"}
            and ribbon_best_tab == title_best_tab
            and ribbon_best_score >= 0.15
            and ribbon_margin >= 0.035
            and title_best_score >= 0.65
            and title_margin >= 0.25
            and (marker_best_score - ribbon_marker_score) <= 0.01
        )
        decisive_ribbon = (
            (
                ribbon_best_tab in {"speed", "stamina", "power", "guts", "wisdom"}
                and ribbon_best_score >= 0.15
                and ribbon_margin >= 0.045
            )
            or ribbon_title_marker_tie_consensus
        )
        decisive_ribbon_agrees_with_color = decisive_ribbon and ribbon_best_tab == color_best_tab
        if decisive_ribbon:
            selected_detection = dict(selected_detection)
            selected_detection["selected_tab_before_ribbon_consensus"] = selected
            selected_detection["selected_tab"] = ribbon_best_tab
            selected_detection["selected_tab_mode"] = (
                "decision_ribbon_title_consensus"
                if ribbon_title_marker_tie_consensus
                else (
                    "decision_ribbon_color_consensus"
                    if decisive_ribbon_agrees_with_color
                    else "decision_ribbon_primary"
                )
            )
            selected = ribbon_best_tab
        absent_tabs = [
            tab
            for tab, check in option_evidence.get("checks", {}).items()
            if not bool(check.get("present"))
        ]
        selected_check = option_evidence.get("checks", {}).get(str(selected), {})
        selected_layout_override = bool(
            len(absent_tabs) == 1
            and absent_tabs[0] == selected
            and decisive_ribbon_agrees_with_color
            and selected_detection.get("title_icon_best_tab") == selected
            and float(selected_detection.get("title_icon_best_score", 0.0)) >= 0.75
            and float(selected_detection.get("best_score", 0.0)) >= 0.45
            and float(selected_check.get("white_ratio", 0.0)) >= 0.20
            and float(selected_check.get("saturated_bright_ratio", 0.0)) >= 0.40
        )
        if selected_layout_override:
            option_evidence["all_present"] = True
            option_evidence["selected_tab_layout_override"] = {
                "tab": selected,
                "mode": "selected_button_shifted_above_static_roi",
                "decision_ribbon_score": round(ribbon_best_score, 6),
                "decision_ribbon_margin": round(ribbon_margin, 6),
                "title_icon_score": round(
                    float(selected_detection.get("title_icon_best_score", 0.0)),
                    6,
                ),
            }
        if not option_evidence["all_present"]:
            failures.append("tab_presence")
        if expected_tab is not None and selected != expected_tab:
            expected_score = float(selected_detection["scores"].get(expected_tab, 0.0))
            expected_marker = float(selected_detection["marker_scores"].get(expected_tab, 0.0))
            best_tab = str(selected_detection.get("best_tab") or "")
            best_score = float(selected_detection.get("best_score", 0.0))
            selected_score = float(selected_detection["scores"].get(selected, 0.0)) if selected is not None else 0.0
            best_marker = float(selected_detection["marker_scores"].get(best_tab, 0.0))
            marker_advantage = expected_marker - best_marker
            score_gap = best_score - expected_score
            color_margin = float(selected_detection.get("margin", 0.0))
            marker_primary_gap = float(selected_detection.get("marker_primary_gap", 0.0))
            decision_ribbon_scores = selected_detection.get("decision_ribbon_scores", {})
            decision_ribbon_best_tab = selected_detection.get("decision_ribbon_best_tab")
            decision_ribbon_best_score = float(
                selected_detection.get("decision_ribbon_best_score", 0.0)
            )
            decision_ribbon_margin = float(
                selected_detection.get("decision_ribbon_margin", 0.0)
            )
            conflicting_tab = selected if selected is not None else best_tab
            competing_ribbon_score = float(
                decision_ribbon_scores.get(conflicting_tab, 0.0)
            )
            selected_before_override = selected
            override = self._expected_tab_evidence_override(
                selected_detection,
                expected_tab=expected_tab,
                selected_before_override=selected_before_override,
                selected_score=selected_score,
                option_rois_all_present=bool(option_evidence["all_present"]),
            )
            if (
                decisive_ribbon
                and decision_ribbon_best_tab != expected_tab
            ):
                selected_detection = dict(selected_detection)
                selected_detection["expected_tab_override_rejected"] = {
                    "expected_tab": expected_tab,
                    "decision_ribbon_best_tab": decision_ribbon_best_tab,
                    "decision_ribbon_best_score": round(decision_ribbon_best_score, 6),
                    "decision_ribbon_margin": round(decision_ribbon_margin, 6),
                    "mode": "decisive_ribbon_conflicts_with_expected",
                }
            elif (
                decision_ribbon_best_tab == expected_tab
                and decision_ribbon_best_score >= 0.15
                and decision_ribbon_margin >= 0.06
                and selected != expected_tab
                and (selected is None or conflicting_tab != expected_tab)
                and not (
                    expected_tab == best_tab
                    and selected_detection.get("marker_primary_mode") is not None
                    and expected_score >= 1.0
                    and (expected_score - selected_score) >= 0.12
                    and score_gap <= 0.02
                    and color_margin <= 0.03
                    and marker_primary_gap <= 0.08
                )
            ):
                selected = expected_tab
                selected_detection = dict(selected_detection)
                selected_detection["selected_tab"] = expected_tab
                selected_detection["expected_tab_override"] = {
                    "expected_tab": expected_tab,
                    "selected_tab_before_override": selected_before_override,
                    "best_tab": best_tab,
                    "conflicting_tab": conflicting_tab,
                    "decision_ribbon_best_score": round(decision_ribbon_best_score, 6),
                    "decision_ribbon_margin": round(decision_ribbon_margin, 6),
                    "competing_ribbon_score": round(competing_ribbon_score, 6),
                    "mode": "decision_ribbon_over_friendship_glow",
                }
            elif (
                expected_score >= 0.70
                and expected_marker >= 0.25
                and marker_advantage >= 0.30
                and score_gap <= 0.18
            ):
                selected = expected_tab
                selected_detection = dict(selected_detection)
                selected_detection["selected_tab"] = expected_tab
                selected_detection["expected_tab_override"] = {
                    "expected_tab": expected_tab,
                    "expected_score": round(expected_score, 6),
                    "expected_marker_score": round(expected_marker, 6),
                    "best_tab": best_tab,
                    "best_score": round(best_score, 6),
                    "best_marker_score": round(best_marker, 6),
                    "marker_advantage": round(marker_advantage, 6),
                    "score_gap": round(score_gap, 6),
                    "mode": "strong_marker_override",
                }
            elif (
                expected_score >= 0.68
                and expected_marker >= 0.50
                and marker_advantage >= 0.15
                and score_gap <= 0.18
            ):
                selected = expected_tab
                selected_detection = dict(selected_detection)
                selected_detection["selected_tab"] = expected_tab
                selected_detection["expected_tab_override"] = {
                    "expected_tab": expected_tab,
                    "expected_score": round(expected_score, 6),
                    "expected_marker_score": round(expected_marker, 6),
                    "best_tab": best_tab,
                    "best_score": round(best_score, 6),
                    "best_marker_score": round(best_marker, 6),
                    "marker_advantage": round(marker_advantage, 6),
                    "score_gap": round(score_gap, 6),
                    "mode": "near_match_marker_override",
                }
            elif (
                expected_score >= 0.72
                and expected_marker >= 0.49
                and -0.08 <= marker_advantage
                and score_gap <= 0.15
            ):
                selected = expected_tab
                selected_detection = dict(selected_detection)
                selected_detection["selected_tab"] = expected_tab
                selected_detection["expected_tab_override"] = {
                    "expected_tab": expected_tab,
                    "expected_score": round(expected_score, 6),
                    "expected_marker_score": round(expected_marker, 6),
                    "best_tab": best_tab,
                    "best_score": round(best_score, 6),
                    "best_marker_score": round(best_marker, 6),
                    "marker_advantage": round(marker_advantage, 6),
                    "score_gap": round(score_gap, 6),
                    "mode": "borderline_expected_marker_override",
                }
            elif (
                expected_tab == best_tab
                and selected_detection.get("marker_primary_mode") is not None
                and expected_score >= 1.0
                and (expected_score - selected_score) >= 0.12
                and score_gap <= 0.02
                and color_margin <= 0.03
                and marker_primary_gap <= 0.08
            ):
                selected = expected_tab
                selected_detection = dict(selected_detection)
                selected_detection["selected_tab"] = expected_tab
                selected_detection["expected_tab_override"] = {
                    "expected_tab": expected_tab,
                    "expected_score": round(expected_score, 6),
                    "expected_marker_score": round(expected_marker, 6),
                    "selected_tab_before_override": selected_before_override,
                    "selected_score": round(selected_score, 6),
                    "best_tab": best_tab,
                    "best_score": round(best_score, 6),
                    "best_marker_score": round(best_marker, 6),
                    "marker_advantage": round(marker_advantage, 6),
                    "score_gap": round(score_gap, 6),
                    "mode": "expected_color_winner_over_marker_drift",
                }
            elif override is not None:
                selected = expected_tab
                selected_detection = dict(selected_detection)
                selected_detection["selected_tab"] = expected_tab
                selected_detection["expected_tab_override"] = override
        evidence["selected_tab"] = selected
        evidence["selected_tab_detection"] = selected_detection
        if selected is None:
            failures.append("selected_tab")
        elif expected_tab is not None and selected != expected_tab:
            failures.append("selected_tab")

        attribute_evidence = self._check_attribute_panel(img)
        evidence["attribute_panel"] = attribute_evidence
        if not attribute_evidence["present"]:
            failures.append("attribute_panel")

        failure_evidence = self._check_failure_region(img)
        evidence["failure_rate_region"] = failure_evidence
        if not failure_evidence["present"]:
            failures.append("failure_rate_region")

        race_entry_evidence = self._check_race_entry_overlay(img)
        evidence["race_entry_overlay"] = race_entry_evidence
        if race_entry_evidence["present"]:
            failures.append("race_entry")

        race_hub_evidence = self._check_race_hub_overlay(img)
        evidence["race_hub_overlay"] = race_hub_evidence
        if race_hub_evidence["present"]:
            failures.append("race_hub")

        settings_evidence = self._check_settings_overlay(img)
        evidence["settings_overlay"] = settings_evidence
        if settings_evidence["present"]:
            failures.append("settings_overlay")

        crystal_reward_evidence = detect_grand_masters_crystal_reward_overlay(img)
        evidence["grand_masters_crystal_reward"] = crystal_reward_evidence
        if crystal_reward_evidence["present"] and failures:
            failures.append("grand_masters_crystal_reward")

        intersection_evidence = self._check_forbidden_intersections(option_evidence["rois"])
        evidence["forbidden_regions"] = intersection_evidence
        if intersection_evidence["intersections"]:
            failures.append("forbidden_intersection")

        blocking_override = self._blocking_screen_override_if_training_menu(
            perception_screen=str(perception.get("screen") or ""),
            option_evidence=option_evidence,
            selected=selected,
            expected_tab=expected_tab,
            attribute_evidence=attribute_evidence,
            failure_evidence=failure_evidence,
            race_entry_evidence=race_entry_evidence,
            race_hub_evidence=race_hub_evidence,
            settings_evidence=settings_evidence,
            crystal_reward_evidence=crystal_reward_evidence,
            intersection_evidence=intersection_evidence,
        )
        if blocking_override is not None and "blocking_screen" in failures:
            failures = [failure for failure in failures if failure != "blocking_screen"]
            evidence["blocking_screen_override"] = blocking_override

        failures = list(dict.fromkeys(failures))
        if failures:
            return TrainingMenuPrecheckResult(
                accepted=False,
                reason="; ".join(failures),
                selected_tab=selected,
                evidence=evidence,
                failures=failures,
            )

        return TrainingMenuPrecheckResult(
            accepted=True,
            reason="training_menu_precheck_passed",
            selected_tab=selected,
            evidence=evidence,
            failures=[],
        )

    def _check_larc(
        self, img: np.ndarray, hsv: np.ndarray, *,
        expected_tab: str | None, evidence: dict[str, Any],
    ) -> TrainingMenuPrecheckResult:
        # Own L'Arc frame: five training controls plus a sixth locked/SS slot.
        # Only a unique two-chevron Decide ribbon establishes the selected tab.
        # Never infer the selection from background colour or the requested tab.
        checks: dict[str, Any] = {}
        rois: dict[str, list[int]] = {}
        selected_tabs: list[str] = []
        for tab, center in zip((*TRAINING_TABS, "ss"), (67, 184, 302, 420, 538, 654)):
            control = hsv[995:1145, center - 48:center + 48]
            # Training level changes the button colour (level 2 is blue).
            saturated = float(np.mean((control[:, :, 1] > 60) & (control[:, :, 2] > 100)))
            white = float(np.mean((control[:, :, 1] < 65) & (control[:, :, 2] > 175)))
            ribbon = hsv[1090:1175, center - 48:center + 48]
            gold = (
                (ribbon[:, :, 0] >= 15) & (ribbon[:, :, 0] <= 38)
                & (ribbon[:, :, 1] > 100) & (ribbon[:, :, 2] > 150)
            ).astype(np.uint8)
            _, labels, components, _ = cv2.connectedComponentsWithStats(gold, connectivity=8)
            chevrons = []
            for label, row in enumerate(components[1:], start=1):
                x, y, width, height, area = row
                if width < 48 or not 12 <= height <= 35 or area < 250:
                    continue
                component = labels[y:y+height, x:x+width] == label
                columns = component[:, [width//10, width//2, width*9//10]]
                tops = np.argmax(columns, axis=0)
                # Both wings descend from the central apex; a sloping wooden
                # background can have the same colour and size, but not this shape.
                if columns.any(axis=0).all() and tops[1] + 3 <= min(tops[0], tops[2]):
                    chevrons.append(row)
            decided = (
                any(row[cv2.CC_STAT_TOP] < 28 for row in chevrons)
                and any(row[cv2.CC_STAT_TOP] > 40 for row in chevrons)
            )
            if tab == "ss":
                # A retained SS preview is a valid navigation origin, not a
                # sixth training candidate. Check the shared blue star icon,
                # excluding the label (blue for SS, pink for SSS) and ribbon.
                # Locked sixth slots cannot qualify.
                star_icon = hsv[995:1060, center - 48:center + 48]
                decided = decided and float(np.mean(
                    (star_icon[:, :, 0] >= 90) & (star_icon[:, :, 0] <= 125)
                    & (star_icon[:, :, 1] > 70) & (star_icon[:, :, 2] > 130)
                )) > 0.35
            if decided:
                selected_tabs.append(tab)
            checks[tab] = {"present": saturated > 0.10 and white > 0.07,
                           "saturated_bright_ratio": saturated, "white_ratio": white,
                           "decide_ribbon": decided}
            if tab in TRAINING_TABS:
                rois[tab] = [2 * (center - 48), 1990, 2 * (center + 48), 2350]
        failures = []
        if not all(checks[tab]["present"] for tab in TRAINING_TABS):
            failures.append("tab_presence")
        selected = selected_tabs[0] if len(selected_tabs) == 1 else None
        if selected is None or (expected_tab is not None and selected != expected_tab):
            failures.append("selected_tab")
        evidence["training_option_rois"] = {"rois": rois, "checks": checks}
        evidence["selected_tab"] = selected
        evidence["selected_tab_detection"] = {"selected_tab": selected, "mode": "larc_decide_ribbon"}
        for name, check in (
            ("attribute_panel", self._check_attribute_panel),
            ("failure_rate_region", self._check_failure_region),
        ):
            evidence[name] = check(img, hsv=hsv)
            if not evidence[name]["present"]:
                failures.append(name)
        for name, check in (
            ("race_entry", self._check_race_entry_overlay),
            ("race_hub", self._check_race_hub_overlay),
            ("settings_overlay", self._check_settings_overlay),
        ):
            evidence[name] = check(img, hsv=hsv)
            if evidence[name]["present"]:
                failures.append(name)
        evidence["forbidden_regions"] = self._check_forbidden_intersections(rois)
        if evidence["forbidden_regions"]["intersections"]:
            failures.append("forbidden_intersection")
        return TrainingMenuPrecheckResult(
            accepted=not failures,
            reason="; ".join(failures) if failures else "training_menu_precheck_passed",
            selected_tab=selected, evidence=evidence, failures=failures,
        )

    def _check_training_option_rois(self, img: np.ndarray) -> dict[str, Any]:
        from scripts.extract_training_option_rois import compute_training_option_rois

        # Build from dimensions using the existing read-only ROI extractor, but
        # classify actual image content locally so fixed coordinates alone are
        # not sufficient for acceptance.
        roi_result = compute_training_option_rois(
            img.shape[1],
            img.shape[0],
            "umamusume_1440x2560_v1",
        )

        rois: dict[str, list[int]] = {}
        checks: dict[str, dict[str, Any]] = {}
        all_present = not roi_result.get("errors") and len(roi_result.get("training_options", [])) == 5
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        for opt in roi_result.get("training_options", []):
            option_id = opt.get("option_id")
            bbox = opt.get("bbox_xyxy")
            if option_id not in TRAINING_TABS or not isinstance(bbox, list) or len(bbox) != 4:
                all_present = False
                continue
            x1, y1, x2, y2 = [int(v) for v in bbox]
            rois[option_id] = [x1, y1, x2, y2]
            roi = hsv[y1:y2, x1:x2]
            if roi.size == 0:
                present = False
                green_ratio = 0.0
                white_ratio = 0.0
                saturated_bright_ratio = 0.0
            else:
                green_ratio = float(
                    np.mean(
                        (roi[:, :, 0] >= 38)
                        & (roi[:, :, 0] <= 92)
                        & (roi[:, :, 1] > 70)
                        & (roi[:, :, 2] > 80)
                    )
                )
                white_ratio = float(np.mean((roi[:, :, 1] < 70) & (roi[:, :, 2] > 180)))
                saturated_bright_ratio = float(np.mean((roi[:, :, 1] > 70) & (roi[:, :, 2] > 80)))
                # Some late-game training buttons render with a denser colored
                # icon stack and slightly less white fill (turn061 power:
                # 0.0727).  Keep the saturated-button structure mandatory,
                # but allow that observed white-fill floor.
                present = green_ratio > 0.08 or (
                    white_ratio >= 0.07
                    and saturated_bright_ratio >= 0.45
                ) or (
                    # Dense late-game icons can replace much of the white
                    # button fill, while still retaining white glyph strokes
                    # over a strongly saturated circular control.
                    white_ratio >= 0.04
                    and saturated_bright_ratio >= 0.80
                )
            checks[option_id] = {
                "bbox_xyxy": [x1, y1, x2, y2],
                "green_ratio": round(green_ratio, 4),
                "white_ratio": round(white_ratio, 4),
                "saturated_bright_ratio": round(saturated_bright_ratio, 4),
                "present": present,
            }
            if not present:
                all_present = False

        missing = [tab for tab in TRAINING_TABS if tab not in rois]
        if missing:
            all_present = False

        strong_green_count = sum(
            1 for check in checks.values() if float(check.get("green_ratio", 0.0)) > 0.80
        )
        if strong_green_count >= 3:
            all_present = False

        return {
            "schema_errors": roi_result.get("errors", []),
            "schema_warnings": roi_result.get("warnings", []),
            "rois": rois,
            "checks": checks,
            "missing": missing,
            "strong_green_count": strong_green_count,
            "all_present": bool(all_present),
        }

    def _detect_selected_tab(self, img: np.ndarray) -> dict[str, Any]:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        scores: dict[str, float] = {}
        for tab, (cx, _) in _tab_centers().items():
            x1, x2 = max(0, cx - 70), min(img.shape[1], cx + 70)
            roi = hsv[2200:2280, x1:x2]
            if roi.size == 0:
                scores[tab] = 0.0
                continue
            red = np.mean(
                (((roi[:, :, 0] <= 8) | (roi[:, :, 0] >= 170))
                & (roi[:, :, 1] > 80)
                & (roi[:, :, 2] > 100))
            )
            yellow = np.mean(
                (roi[:, :, 0] >= 20)
                & (roi[:, :, 0] <= 38)
                & (roi[:, :, 1] > 80)
                & (roi[:, :, 2] > 120)
            )
            orange = np.mean(
                (roi[:, :, 0] >= 5)
                & (roi[:, :, 0] < 24)
                & (roi[:, :, 1] > 80)
                & (roi[:, :, 2] > 100)
            )
            green = np.mean(
                (roi[:, :, 0] >= 38)
                & (roi[:, :, 0] <= 92)
                & (roi[:, :, 1] > 70)
                & (roi[:, :, 2] > 80)
            )
            white = np.mean((roi[:, :, 1] < 70) & (roi[:, :, 2] > 180))
            scores[tab] = float(
                (red * 2.0)
                + (orange * 1.3)
                + (yellow * 1.2)
                + (white * 0.2)
                - (green * 0.5)
            )

        best_tab, best_score = max(scores.items(), key=lambda item: item[1])
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        second_tab = ranked[1][0] if len(ranked) > 1 else None
        margin = best_score - second_score
        best_score_threshold = 0.15
        margin_threshold = 0.05
        marker_scores = self._detect_selected_tab_marker_scores(hsv)
        marker_best_tab, marker_best_score = max(marker_scores.items(), key=lambda item: item[1])
        marker_ranked = sorted(marker_scores.items(), key=lambda item: item[1], reverse=True)
        marker_second_tab = marker_ranked[1][0] if len(marker_ranked) > 1 else None
        marker_second_score = marker_ranked[1][1] if len(marker_ranked) > 1 else 0.0
        marker_primary_gap = marker_best_score - marker_second_score
        marker_best_color_score = scores.get(marker_best_tab, 0.0)
        marker_margin = 0.0
        if second_tab is not None:
            marker_margin = marker_scores.get(best_tab, 0.0) - marker_scores.get(second_tab, 0.0)
        title_icon_scores = self._detect_title_icon_tab_scores(img)
        title_icon_best_tab, title_icon_best_score = max(title_icon_scores.items(), key=lambda item: item[1])
        title_icon_ranked = sorted(title_icon_scores.items(), key=lambda item: item[1], reverse=True)
        title_icon_second_tab = title_icon_ranked[1][0] if len(title_icon_ranked) > 1 else None
        title_icon_second_score = title_icon_ranked[1][1] if len(title_icon_ranked) > 1 else 0.0
        title_icon_margin = title_icon_best_score - title_icon_second_score
        decision_ribbon_scores = self._detect_decision_ribbon_scores(hsv)
        decision_ribbon_best_tab, decision_ribbon_best_score = max(
            decision_ribbon_scores.items(), key=lambda item: item[1]
        )
        decision_ribbon_ranked = sorted(
            decision_ribbon_scores.items(), key=lambda item: item[1], reverse=True
        )
        decision_ribbon_second_score = (
            decision_ribbon_ranked[1][1] if len(decision_ribbon_ranked) > 1 else 0.0
        )
        decision_ribbon_margin = decision_ribbon_best_score - decision_ribbon_second_score
        marker_title_reject_color = (
            best_tab != marker_best_tab
            and marker_best_score >= 0.40
            and marker_primary_gap >= 0.04
            and marker_primary_gap <= 0.12
            and title_icon_margin <= 0.12
            and title_icon_scores.get(best_tab, 0.0) <= 0.65
            and title_icon_scores.get(marker_best_tab, 0.0) >= 0.84
            and (title_icon_scores.get(marker_best_tab, 0.0) - title_icon_scores.get(best_tab, 0.0)) >= 0.25
        )
        selected_tab = (
            best_tab
            if best_score >= best_score_threshold and margin >= margin_threshold
            else None
        )
        conflict_fail_closed_mode: str | None = None
        if marker_title_reject_color:
            selected_tab = None
            conflict_fail_closed_mode = "marker_title_reject_color_fail_closed"
        marker_primary_mode: str | None = None
        title_marker_agree = title_icon_best_tab == marker_best_tab
        marker_conflicts_with_color_title = (
            title_icon_best_tab == best_tab
            and title_icon_best_tab != marker_best_tab
            and title_icon_best_score >= 0.90
            and best_score >= 1.30
        )
        strong_color_selected_tab = (
            selected_tab == best_tab
            and best_score >= 1.20
            and margin >= 0.12
            and marker_primary_gap < 0.12
        )
        if (
            title_marker_agree
            and selected_tab != marker_best_tab
            and not marker_title_reject_color
            and marker_best_score >= 0.38
            and marker_primary_gap >= 0.20
            and marker_best_color_score >= 0.65
            and title_icon_best_score >= 0.86
            and title_icon_margin >= 0.35
        ):
            selected_tab = marker_best_tab
            marker_primary_mode = "title_icon_marker_primary_override"
        if selected_tab is None and best_score >= best_score_threshold and margin >= 0.01:
            if (
                not marker_title_reject_color
                and marker_scores.get(best_tab, 0.0) >= 0.6
                and marker_margin >= 0.5
            ):
                selected_tab = best_tab
        if (
            marker_best_score >= 0.38
            and marker_primary_gap >= 0.04
            and marker_best_color_score >= 0.60
            and (best_score - marker_best_color_score) <= 0.40
            and not strong_color_selected_tab
            and not marker_conflicts_with_color_title
            and not marker_title_reject_color
            and selected_tab != marker_best_tab
        ):
            selected_tab = marker_best_tab
            marker_primary_mode = "marker_primary_override"
        if (
            marker_best_score >= 0.42
            and marker_primary_gap >= 0.16
            and marker_scores.get(best_tab, 0.0) <= 0.20
            and marker_best_color_score >= 0.55
            and not marker_conflicts_with_color_title
            and not marker_title_reject_color
            and selected_tab != marker_best_tab
        ):
            selected_tab = marker_best_tab
            marker_primary_mode = "strong_marker_beats_background_color"
        if (
            marker_best_score >= 0.49
            and marker_primary_gap >= 0.16
            and (marker_best_score - marker_scores.get(best_tab, 0.0)) >= 0.15
            and marker_best_color_score >= 0.66
            and not marker_conflicts_with_color_title
            and not marker_title_reject_color
            and selected_tab != marker_best_tab
        ):
            selected_tab = marker_best_tab
            marker_primary_mode = "dominant_marker_beats_speed_color_bleed"
        if (
            selected_tab is None
            and not marker_title_reject_color
            and marker_best_tab == best_tab
            and best_score >= 0.65
            and margin >= 0.025
            and marker_best_score >= 0.50
            and marker_primary_gap >= 0.025
        ):
            selected_tab = best_tab
            marker_primary_mode = "aligned_low_margin_marker"
        return {
            "selected_tab": selected_tab,
            "best_tab": best_tab,
            "best_score": round(best_score, 6),
            "second_tab": second_tab,
            "second_score": round(second_score, 6),
            "margin": round(margin, 6),
            "scores": {key: round(value, 6) for key, value in scores.items()},
            "marker_scores": {key: round(value, 6) for key, value in marker_scores.items()},
            "marker_best_tab": marker_best_tab,
            "marker_best_score": round(marker_best_score, 6),
            "marker_second_tab": marker_second_tab,
            "marker_second_score": round(marker_second_score, 6),
            "marker_primary_gap": round(marker_primary_gap, 6),
            "marker_best_color_score": round(marker_best_color_score, 6),
            "marker_primary_mode": marker_primary_mode,
            "strong_color_selected_tab": strong_color_selected_tab,
            "marker_conflicts_with_color_title": marker_conflicts_with_color_title,
            "marker_title_reject_color": marker_title_reject_color,
            "conflict_fail_closed_mode": conflict_fail_closed_mode,
            "marker_margin": round(marker_margin, 6),
            "title_icon_scores": {key: round(value, 6) for key, value in title_icon_scores.items()},
            "title_icon_best_tab": title_icon_best_tab,
            "title_icon_best_score": round(title_icon_best_score, 6),
            "title_icon_second_tab": title_icon_second_tab,
            "title_icon_second_score": round(title_icon_second_score, 6),
            "title_icon_margin": round(title_icon_margin, 6),
            "decision_ribbon_scores": {
                key: round(value, 6) for key, value in decision_ribbon_scores.items()
            },
            "decision_ribbon_best_tab": decision_ribbon_best_tab,
            "decision_ribbon_best_score": round(decision_ribbon_best_score, 6),
            "decision_ribbon_margin": round(decision_ribbon_margin, 6),
            "best_score_threshold": best_score_threshold,
            "margin_threshold": margin_threshold,
            "roi": [max(0, _tab_centers()[best_tab][0] - 70), 2200,
                    min(img.shape[1], _tab_centers()[best_tab][0] + 70), 2280],
        }

    def _detect_title_icon_tab_scores(self, img: np.ndarray) -> dict[str, float]:
        title_icon = img[400:545, 20:180]
        title_feature = _icon_feature(title_icon)
        scores: dict[str, float] = {}
        for tab, (cx, cy) in _tab_centers().items():
            best = 0.0
            for y_offset in (-80, -40, 0, 40):
                y1 = max(0, cy - 110 + y_offset)
                y2 = min(img.shape[0], cy + 65 + y_offset)
                x1 = max(0, cx - 95)
                x2 = min(img.shape[1], cx + 95)
                candidate_feature = _icon_feature(img[y1:y2, x1:x2])
                best = max(best, float(np.dot(title_feature, candidate_feature)))
            scores[tab] = best
        return scores

    def _detect_selected_tab_marker_scores(self, hsv: np.ndarray) -> dict[str, float]:
        scores: dict[str, float] = {}
        for tab, (cx, _) in _tab_centers().items():
            x1, x2 = max(0, cx - 95), min(hsv.shape[1], cx + 95)
            roi = hsv[2250:2425, x1:x2]
            if roi.size == 0:
                scores[tab] = 0.0
                continue
            red = np.mean(
                (((roi[:, :, 0] <= 8) | (roi[:, :, 0] >= 170))
                & (roi[:, :, 1] > 80)
                & (roi[:, :, 2] > 100))
            )
            orange = np.mean(
                (roi[:, :, 0] >= 5)
                & (roi[:, :, 0] < 24)
                & (roi[:, :, 1] > 80)
                & (roi[:, :, 2] > 100)
            )
            yellow = np.mean(
                (roi[:, :, 0] >= 20)
                & (roi[:, :, 0] <= 38)
                & (roi[:, :, 1] > 80)
                & (roi[:, :, 2] > 120)
            )
            green = np.mean(
                (roi[:, :, 0] >= 38)
                & (roi[:, :, 0] <= 92)
                & (roi[:, :, 1] > 70)
                & (roi[:, :, 2] > 80)
            )
            blue = np.mean(
                (roi[:, :, 0] >= 85)
                & (roi[:, :, 0] <= 125)
                & (roi[:, :, 1] > 60)
                & (roi[:, :, 2] > 90)
            )
            white = np.mean((roi[:, :, 1] < 70) & (roi[:, :, 2] > 180))
            scores[tab] = float(
                (green * 1.5)
                + (yellow * 2.0)
                + (orange * 1.0)
                + (white * 0.2)
                - (blue * 0.7)
                - (red * 0.2)
            )
        return scores

    def _detect_decision_ribbon_scores(self, hsv: np.ndarray) -> dict[str, float]:
        scores: dict[str, float] = {}
        for tab, (cx, _) in _tab_centers().items():
            x1, x2 = max(0, cx - 85), min(hsv.shape[1], cx + 115)
            roi = hsv[2290:2345, x1:x2]
            if roi.size == 0:
                scores[tab] = 0.0
                continue
            yellow = (
                (roi[:, :, 0] >= 20)
                & (roi[:, :, 0] <= 38)
                & (roi[:, :, 1] > 100)
                & (roi[:, :, 2] > 150)
            )
            # A shallow friendship glow is not the vertically extended decide ribbon.
            _, labels, components, _ = cv2.connectedComponentsWithStats(yellow.astype(np.uint8), 8)
            for label, component in enumerate(components[1:], start=1):
                if component[cv2.CC_STAT_HEIGHT] < roi.shape[0] * 0.60:
                    yellow[labels == label] = False
            scores[tab] = float(np.mean(yellow))
        return scores

    def _expected_tab_evidence_override(
        self,
        detection: dict[str, Any],
        *,
        expected_tab: str,
        selected_before_override: str | None,
        selected_score: float,
        option_rois_all_present: bool,
    ) -> dict[str, Any] | None:
        if expected_tab not in TRAINING_TABS or not option_rois_all_present:
            return None

        expected_score = float(detection["scores"].get(expected_tab, 0.0))
        expected_marker = float(detection["marker_scores"].get(expected_tab, 0.0))
        best_tab = str(detection.get("best_tab") or "")
        best_score = float(detection.get("best_score", 0.0))
        best_marker = float(detection["marker_scores"].get(best_tab, 0.0))
        score_gap = best_score - expected_score
        marker_advantage = expected_marker - best_marker
        marker_primary_gap = float(detection.get("marker_primary_gap", 0.0))
        title_icon_best_score = float(detection.get("title_icon_best_score", 0.0))
        title_icon_margin = float(detection.get("title_icon_margin", 0.0))
        title_icon_scores = detection.get("title_icon_scores", {})
        expected_title_score = float(title_icon_scores.get(expected_tab, 0.0))
        best_title_score = float(title_icon_scores.get(best_tab, 0.0))

        base = {
            "expected_tab": expected_tab,
            "expected_score": round(expected_score, 6),
            "expected_marker_score": round(expected_marker, 6),
            "selected_tab_before_override": selected_before_override,
            "selected_score": round(selected_score, 6),
            "best_tab": best_tab,
            "best_score": round(best_score, 6),
            "best_marker_score": round(best_marker, 6),
            "marker_advantage": round(marker_advantage, 6),
            "marker_primary_gap": round(marker_primary_gap, 6),
            "score_gap": round(score_gap, 6),
            "title_icon_best_score": round(title_icon_best_score, 6),
            "title_icon_margin": round(title_icon_margin, 6),
            "expected_title_icon_score": round(expected_title_score, 6),
            "best_title_icon_score": round(best_title_score, 6),
        }

        title_and_marker_agree = (
            detection.get("marker_best_tab") == expected_tab
            and detection.get("title_icon_best_tab") == expected_tab
        )
        dominant_marker_title = (
            title_and_marker_agree
            and expected_score >= 0.70
            and expected_marker >= 0.40
            and marker_advantage >= 0.45
            and marker_primary_gap >= 0.30
            and title_icon_best_score >= 0.90
            and title_icon_margin >= 0.60
        )
        balanced_title_marker = (
            title_and_marker_agree
            and expected_score >= 1.30
            and expected_marker >= 0.10
            and marker_primary_gap >= 0.07
            and (marker_advantage >= 0.05 or best_tab == expected_tab)
            and score_gap <= 0.30
            and title_icon_best_score >= 0.89
        )
        if dominant_marker_title or balanced_title_marker:
            return {
                **base,
                "marker_best_tab": detection.get("marker_best_tab"),
                "title_icon_best_tab": detection.get("title_icon_best_tab"),
                "mode": "title_icon_marker_expected_override",
            }

        title_only_high_confidence = (
            detection.get("title_icon_best_tab") == expected_tab
            and title_icon_best_score >= 0.86
            and title_icon_margin >= 0.35
            and expected_score >= 1.25
            and score_gap <= 0.30
        )
        if title_only_high_confidence:
            return {
                **base,
                "title_icon_best_tab": detection.get("title_icon_best_tab"),
                "mode": "title_icon_expected_override",
            }

        strong_color_expected = (
            best_tab == expected_tab
            and expected_score >= 1.25
            and float(detection.get("margin", 0.0)) >= 0.12
            and detection.get("marker_primary_mode") == "marker_primary_override"
            and marker_primary_gap <= 0.14
        )
        if strong_color_expected:
            return {
                **base,
                "color_margin": round(float(detection.get("margin", 0.0)), 6),
                "marker_best_tab": detection.get("marker_best_tab"),
                "title_icon_best_tab": detection.get("title_icon_best_tab"),
                "mode": "strong_color_expected_override",
            }

        color_title_low_margin_expected = (
            best_tab == expected_tab
            and detection.get("title_icon_best_tab") == expected_tab
            and expected_score >= 1.35
            and float(detection.get("margin", 0.0)) >= 0.03
            and title_icon_best_score >= 0.90
            and (
                selected_before_override is None
                or (expected_score - selected_score) >= 0.20
            )
        )
        if color_title_low_margin_expected:
            return {
                **base,
                "color_margin": round(float(detection.get("margin", 0.0)), 6),
                "marker_best_tab": detection.get("marker_best_tab"),
                "title_icon_best_tab": detection.get("title_icon_best_tab"),
                "mode": "color_title_low_margin_expected_override",
            }

        marker_title_rejects_color_for_expected = (
            detection.get("marker_best_tab") == expected_tab
            and detection.get("best_tab") != expected_tab
            and expected_score >= 0.60
            and expected_marker >= 0.40
            and marker_primary_gap >= 0.04
            and marker_primary_gap <= 0.12
            and expected_title_score >= 0.84
            and best_title_score <= 0.65
            and (expected_title_score - best_title_score) >= 0.25
        )
        if marker_title_rejects_color_for_expected:
            return {
                **base,
                "marker_best_tab": detection.get("marker_best_tab"),
                "title_icon_best_tab": detection.get("title_icon_best_tab"),
                "conflict_fail_closed_mode": detection.get("conflict_fail_closed_mode"),
                "mode": "expected_marker_title_reject_color_override",
            }

        return None

    def _check_attribute_panel(
        self, img: np.ndarray, *, hsv: np.ndarray | None = None
    ) -> dict[str, Any]:
        is_720p = (
            (hsv is not None and hsv.shape[:2] == (1280, 720))
            or img.shape[:2] == (1280, 720)
        )
        if is_720p:
            band_hsv = (
                hsv[740:890, :703]
                if (hsv is not None and hsv.shape[:2] == (1280, 720))
                else cv2.cvtColor(img[740:890, :703], cv2.COLOR_BGR2HSV)
            )
            legacy_roi = band_hsv[0:90, 22:703]
            header = band_hsv[80:110, 22:703]
            values = band_hsv[110:150, 22:703]
            edges = (22, 135, 253, 371, 490, 598, 703)
            label_band = band_hsv[90:110]
            cell_top, cell_bottom = 110, 150
            rank_pad, cell_right_pad = 35, 2
        else:
            if img.shape[1] < 1405 or img.shape[0] < 1780:
                return {"present": False, "reason": "image_too_small"}
            band_hsv = cv2.cvtColor(img[1480:1780, :1405], cv2.COLOR_BGR2HSV)
            legacy_roi = band_hsv[0:180, 45:1405]
            header = band_hsv[160:220, 45:1405]
            values = band_hsv[220:300, 45:1405]
            edges = (45, 270, 506, 742, 979, 1196, 1405)
            label_band = band_hsv[180:220]
            cell_top, cell_bottom = 220, 300
            rank_pad, cell_right_pad = 70, 5
        legacy_blue = float(
            np.mean(
                (legacy_roi[:, :, 0] >= 85)
                & (legacy_roi[:, :, 0] <= 130)
                & (legacy_roi[:, :, 1] > 40)
                & (legacy_roi[:, :, 2] > 80)
            )
        )
        legacy_white = float(
            np.mean((legacy_roi[:, :, 1] < 70) & (legacy_roi[:, :, 2] > 170))
        )
        legacy_orange = float(
            np.mean(
                (legacy_roi[:, :, 0] >= 5)
                & (legacy_roi[:, :, 0] <= 32)
                & (legacy_roi[:, :, 1] > 60)
                & (legacy_roi[:, :, 2] > 100)
            )
        )
        legacy_signal_present = (
            (
                (legacy_blue > 0.03 and legacy_white > 0.12)
                or (legacy_orange > 0.04 and legacy_white > 0.12)
            )
            and not (legacy_blue > 0.75 and legacy_orange < 0.05)
        )

        header_pink = float(
            np.mean(
                (header[:, :, 0] >= 145)
                & (header[:, :, 0] <= 175)
                & (header[:, :, 1] > 60)
                & (header[:, :, 2] > 100)
            )
        )
        header_white = float(
            np.mean((header[:, :, 1] < 70) & (header[:, :, 2] > 170))
        )
        label_cells: list[dict[str, float]] = []
        label_cells_present = False
        if self.scenario == "larc":
            # Floating training gains can overlap the upper header strip.
            # Read each actual label cell so a blank cell cannot borrow its
            # neighbours' labels, and pair this fallback with numeric readiness.
            for left, right in zip(edges, edges[1:]):
                cell = label_band[:, left:right]
                label_cells.append({
                    "white_ratio": float(np.mean((cell[:, :, 1] < 70) & (cell[:, :, 2] > 170))),
                    "saturated_ratio": float(np.mean((cell[:, :, 1] > 60) & (cell[:, :, 2] > 100))),
                })
            label_cells_present = all(cell["white_ratio"] >= .12 and cell["saturated_ratio"] >= .45
                                      for cell in label_cells)
        header_blue = float(
            np.mean(
                (header[:, :, 0] >= 85)
                & (header[:, :, 0] <= 130)
                & (header[:, :, 1] > 40)
                & (header[:, :, 2] > 80)
            )
        )
        header_yellow = float(
            np.mean(
                (header[:, :, 0] >= 15)
                & (header[:, :, 0] <= 38)
                & (header[:, :, 1] > 60)
                & (header[:, :, 2] > 100)
            )
        )
        header_saturated = float(np.mean(
            (header[:, :, 1] > 60) & (header[:, :, 2] > 100)
        ))
        values_blue = float(
            np.mean(
                (values[:, :, 0] >= 85)
                & (values[:, :, 0] <= 130)
                & (values[:, :, 1] > 40)
                & (values[:, :, 2] > 80)
            )
        )
        values_white = float(
            np.mean((values[:, :, 1] < 70) & (values[:, :, 2] > 170))
        )
        numeric_cells_white = values_white
        numeric_cells_loaded = False
        if self.scenario == "larc":
            # Rank badges (SS/A/etc.) may be large and gold. Locate the six
            # numeric cells without counting their left-hand rank artwork as
            # missing white panel background.
            cells = [band_hsv[cell_top:cell_bottom, edges[index] + (rank_pad if index < 5 else 0):edges[index + 1] - cell_right_pad]
                     for index in range(6)]
            numeric_cells_white = float(np.mean([np.mean((cell[:, :, 1] < 70) &
                (cell[:, :, 2] > 170)) for cell in cells]))
            numeric_cells_loaded = all(
                float(np.mean((cell[:, :, 1] < 70) & (cell[:, :, 2] > 170))) >= .50
                and float(np.mean((cell[:, :, 0] <= 35) & (cell[:, :, 1] > 50)
                    & (cell[:, :, 2] > 45) & (cell[:, :, 2] < 180))) >= .025
                for cell in cells)
        # L'Arc's label strip can be orange as well as blue/pink/yellow.
        # Its hue does not determine whether the training page is ready.
        # Keep the white labels, numeric ink/background and the enclosing
        # training-menu controls as the structural checks.
        header_color_present = (
            header_saturated >= 0.45 if self.scenario == "larc"
            else header_pink >= 0.45 or header_blue >= 0.45
        )
        value_ink = float(np.mean(
            (values[:, :, 0] <= 35) & (values[:, :, 1] > 50)
            & (values[:, :, 2] > 45) & (values[:, :, 2] < 180)
        ))
        # L'Arc ranks change colour as attributes improve; check the brown
        # numeric ink instead of requiring a quota of blue rank glyphs.
        values_present = value_ink >= 0.025 if self.scenario == "larc" else values_blue >= 0.075
        present = (
            header_color_present
            and (header_white >= 0.18 or label_cells_present and numeric_cells_loaded)
            and values_present
            and (values_white >= (0.57 if self.scenario == "larc" else 0.575)
                 or self.scenario == "larc" and numeric_cells_white >= 0.57)
        )
        return {
            "blue_ratio": round(legacy_blue, 4),
            "white_ratio": round(legacy_white, 4),
            "orange_ratio": round(legacy_orange, 4),
            "legacy_roi": [45, 1480, 1405, 1660],
            "legacy_signal_present": bool(legacy_signal_present),
            "live_header": {
                "roi": [45, 1640, 1405, 1700],
                "pink_ratio": round(header_pink, 4),
                "blue_ratio": round(header_blue, 4),
                "yellow_ratio": round(header_yellow, 4),
                "saturated_ratio": round(header_saturated, 4),
                "white_ratio": round(header_white, 4),
                "label_cell_roi": [45, 1660, 1405, 1700] if label_cells else None,
                "label_cells": [{key: round(value, 4) for key, value in cell.items()} for cell in label_cells],
                "label_cells_present": bool(label_cells_present),
                "label_row_fallback_present": bool(label_cells_present and numeric_cells_loaded),
            },
            "live_values": {
                "roi": [45, 1700, 1405, 1780],
                "number_ink_ratio": round(value_ink, 4),
                "blue_ratio": round(values_blue, 4),
                "white_ratio": round(values_white, 4),
                "numeric_cells_white_ratio": round(numeric_cells_white, 4),
                "all_numeric_cells_loaded": bool(numeric_cells_loaded),
            },
            "present": bool(present),
        }

    def _check_failure_region(
        self, img: np.ndarray, *, hsv: np.ndarray | None = None
    ) -> dict[str, Any]:
        if hsv is not None and hsv.shape[:2] == (1280, 720):
            roi = hsv[830:930, 0:180]
        elif img.shape[:2] == (1280, 720):
            roi = cv2.cvtColor(img[830:930, 0:180], cv2.COLOR_BGR2HSV)
        else:
            if img.shape[1] < 360 or img.shape[0] < 1860:
                return {"present": False, "reason": "image_too_small"}
            roi = cv2.cvtColor(img[1660:1860, 0:360], cv2.COLOR_BGR2HSV)
        orange = float(np.mean((roi[:, :, 0] >= 5) & (roi[:, :, 0] <= 32) & (roi[:, :, 1] > 70) & (roi[:, :, 2] > 100)))
        blue = float(np.mean((roi[:, :, 0] >= 88) & (roi[:, :, 0] <= 118) & (roi[:, :, 1] > 70) & (roi[:, :, 2] > 100)))
        white = float(np.mean((roi[:, :, 1] < 70) & (roi[:, :, 2] > 170)))
        present = (orange + blue) > 0.04 and white > 0.08
        return {
            "orange_ratio": round(orange, 4),
            "blue_ratio": round(blue, 4),
            "white_ratio": round(white, 4),
            "present": bool(present),
        }

    def _check_race_entry_overlay(
        self, img: np.ndarray, *, hsv: np.ndarray | None = None
    ) -> dict[str, Any]:
        src_hsv = hsv if (hsv is not None and hsv.ndim == 3 and hsv.shape[2] == 3) else None
        height, width = (src_hsv.shape[:2] if src_hsv is not None else img.shape[:2])
        coord_scale = 2 if (width, height) == (720, 1280) else 1
        x1, x2 = int(width * 0.45), int(width * 0.90)
        y1, y2 = int(height * 0.72), int(height * 0.92)
        if src_hsv is not None:
            roi = src_hsv[y1:y2, x1:x2]
            if roi.size == 0:
                return {
                    "bbox_xyxy": [x1 * coord_scale, y1 * coord_scale, x2 * coord_scale, y2 * coord_scale],
                    "pink_ratio": 0.0,
                    "red_ratio": 0.0,
                    "white_ratio": 0.0,
                    "present": False,
                }
        else:
            bgr_roi = img[y1:y2, x1:x2]
            if bgr_roi.size == 0:
                return {
                    "bbox_xyxy": [x1 * coord_scale, y1 * coord_scale, x2 * coord_scale, y2 * coord_scale],
                    "pink_ratio": 0.0,
                    "red_ratio": 0.0,
                    "white_ratio": 0.0,
                    "present": False,
                }
            roi = cv2.cvtColor(bgr_roi, cv2.COLOR_BGR2HSV)
        pink = float(
            np.mean(
                (roi[:, :, 0] >= 145)
                & (roi[:, :, 0] <= 175)
                & (roi[:, :, 1] > 70)
                & (roi[:, :, 2] > 100)
            )
        )
        red = float(
            np.mean(
                (((roi[:, :, 0] <= 8) | (roi[:, :, 0] >= 170)))
                & (roi[:, :, 1] > 80)
                & (roi[:, :, 2] > 100)
            )
        )
        white = float(np.mean((roi[:, :, 1] < 70) & (roi[:, :, 2] > 180)))
        if src_hsv is not None:
            title_slice = src_hsv[int(height * 0.07):int(height * 0.28), int(width * 0.15):int(width * 0.85)]
            title = title_slice if title_slice.size else np.zeros((1, 1, 3), dtype=np.uint8)
        else:
            title_bgr = img[int(height * 0.07):int(height * 0.28), int(width * 0.15):int(width * 0.85)]
            title = cv2.cvtColor(title_bgr, cv2.COLOR_BGR2HSV) if title_bgr.size else np.zeros((1, 1, 3), dtype=np.uint8)
        title_pale_green = float(
            np.mean(
                (title[:, :, 0] >= 35)
                & (title[:, :, 0] <= 85)
                & (title[:, :, 1] > 20)
                & (title[:, :, 1] < 110)
                & (title[:, :, 2] > 150)
            )
        )
        title_dark = float(np.mean(title[:, :, 2] < 80))
        present = (
            pink >= 0.08
            and red >= 0.12
            and white >= 0.30
            and title_pale_green >= 0.05
            and title_dark >= 0.05
        )
        return {
            "bbox_xyxy": [x1 * coord_scale, y1 * coord_scale, x2 * coord_scale, y2 * coord_scale],
            "pink_ratio": round(pink, 4),
            "red_ratio": round(red, 4),
            "white_ratio": round(white, 4),
            "title_pale_green_ratio": round(title_pale_green, 4),
            "title_dark_ratio": round(title_dark, 4),
            "present": bool(present),
        }

    def _check_race_hub_overlay(
        self, img: np.ndarray, *, hsv: np.ndarray | None = None
    ) -> dict[str, Any]:
        src_hsv = hsv if (hsv is not None and hsv.ndim == 3 and hsv.shape[2] == 3) else None
        height, width = (src_hsv.shape[:2] if src_hsv is not None else img.shape[:2])

        def crop_hsv(region: tuple[float, float, float, float]) -> np.ndarray | None:
            x0, x1, y0, y1 = region
            if src_hsv is not None:
                sub = src_hsv[int(height * y0): int(height * y1), int(width * x0): int(width * x1)]
                return sub if sub.size else None
            sub = img[int(height * y0): int(height * y1), int(width * x0): int(width * x1)]
            if sub.size == 0:
                return None
            return cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)

        title_hsv = crop_hsv((0.15, 0.90, 0.08, 0.30))
        left_hsv = crop_hsv((0.04, 0.48, 0.78, 0.90))
        right_hsv = crop_hsv((0.48, 0.96, 0.76, 0.91))

        title_blue = 0.0 if title_hsv is None else float(np.mean((title_hsv[:, :, 0] >= 85) & (title_hsv[:, :, 0] <= 130) & (title_hsv[:, :, 1] > 40) & (title_hsv[:, :, 2] > 80)))
        title_gold = 0.0 if title_hsv is None else float(np.mean((title_hsv[:, :, 0] >= 15) & (title_hsv[:, :, 0] <= 35) & (title_hsv[:, :, 1] > 60) & (title_hsv[:, :, 2] > 120)))
        title_white = 0.0 if title_hsv is None else float(np.mean((title_hsv[:, :, 1] < 60) & (title_hsv[:, :, 2] > 175)))
        left_white = 0.0 if left_hsv is None else float(np.mean((left_hsv[:, :, 1] < 60) & (left_hsv[:, :, 2] > 175)))
        left_green = 0.0 if left_hsv is None else float(np.mean((left_hsv[:, :, 0] >= 38) & (left_hsv[:, :, 0] <= 92) & (left_hsv[:, :, 1] > 70) & (left_hsv[:, :, 2] > 80)))
        right_pink = 0.0 if right_hsv is None else float(np.mean((right_hsv[:, :, 0] >= 140) & (right_hsv[:, :, 0] <= 175) & (right_hsv[:, :, 1] > 60) & (right_hsv[:, :, 2] > 100)))
        right_gold = 0.0 if right_hsv is None else float(np.mean((right_hsv[:, :, 0] >= 15) & (right_hsv[:, :, 0] <= 35) & (right_hsv[:, :, 1] > 60) & (right_hsv[:, :, 2] > 120)))

        present = (
            title_blue > 0.45
            and title_gold > 0.04
            and title_white < 0.18
            and left_white > 0.35
            and left_green > 0.08
        )
        return {
            "title_blue_ratio": round(title_blue, 4),
            "title_gold_ratio": round(title_gold, 4),
            "title_white_ratio": round(title_white, 4),
            "left_white_ratio": round(left_white, 4),
            "left_green_ratio": round(left_green, 4),
            "right_pink_ratio": round(right_pink, 4),
            "right_gold_ratio": round(right_gold, 4),
            "present": bool(present),
        }

    def _check_settings_overlay(
        self, img: np.ndarray, *, hsv: np.ndarray | None = None
    ) -> dict[str, Any]:
        src_hsv = hsv if (hsv is not None and hsv.ndim == 3 and hsv.shape[2] == 3) else cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        height, width = src_hsv.shape[:2]

        def ratio(
            region: tuple[float, float, float, float],
            kind: str,
        ) -> float:
            x0, x1, y0, y1 = region
            roi = src_hsv[int(height * y0): int(height * y1), int(width * x0): int(width * x1)]
            if roi.size == 0:
                return 0.0
            if kind == "green":
                return float(np.mean((roi[:, :, 0] >= 38) & (roi[:, :, 0] <= 92) & (roi[:, :, 1] > 70) & (roi[:, :, 2] > 80)))
            if kind == "white":
                return float(np.mean((roi[:, :, 1] < 70) & (roi[:, :, 2] > 175)))
            return float(np.mean((roi[:, :, 0] >= 125) & (roi[:, :, 0] <= 155) & (roi[:, :, 1] > 20) & (roi[:, :, 2] > 80)))

        top_header = (0.02, 0.98, 0.02, 0.095)
        left_bottom = (0.08, 0.48, 0.895, 0.965)
        right_bottom = (0.52, 0.92, 0.895, 0.965)
        center_panel = (0.05, 0.95, 0.10, 0.88)
        right_scrollbar = (0.965, 0.985, 0.10, 0.88)

        top_green = ratio(top_header, "green")
        top_white = ratio(top_header, "white")
        left_white = ratio(left_bottom, "white")
        left_green = ratio(left_bottom, "green")
        right_green = ratio(right_bottom, "green")
        right_white = ratio(right_bottom, "white")
        center_white = ratio(center_panel, "white")
        scrollbar_purple = ratio(right_scrollbar, "purple")

        present = (
            top_green > 0.45
            and top_white < 0.40
            and left_white > 0.65
            and left_green < 0.15
            and right_green > 0.45
            and right_white > 0.08
            and center_white > 0.65
            and scrollbar_purple > 0.04
        )
        return {
            "top_green_ratio": round(top_green, 4),
            "top_white_ratio": round(top_white, 4),
            "left_white_ratio": round(left_white, 4),
            "left_green_ratio": round(left_green, 4),
            "right_green_ratio": round(right_green, 4),
            "right_white_ratio": round(right_white, 4),
            "center_white_ratio": round(center_white, 4),
            "scrollbar_purple_ratio": round(scrollbar_purple, 4),
            "present": bool(present),
        }

    def _check_forbidden_intersections(self, rois: dict[str, list[int]]) -> dict[str, Any]:
        forbidden = list(self.extra_forbidden_regions)
        intersections: list[dict[str, Any]] = []
        for tab, bbox in rois.items():
            for name, forbidden_bbox in forbidden:
                if _intersects(tuple(bbox), forbidden_bbox):
                    intersections.append({"tab": tab, "forbidden": name})
        return {"intersections": intersections}

    @staticmethod
    def _blocking_screen_override_if_training_menu(
        *,
        perception_screen: str,
        option_evidence: dict[str, Any],
        selected: str | None,
        expected_tab: str | None,
        attribute_evidence: dict[str, Any],
        failure_evidence: dict[str, Any],
        race_entry_evidence: dict[str, Any],
        race_hub_evidence: dict[str, Any],
        settings_evidence: dict[str, Any],
        crystal_reward_evidence: dict[str, Any],
        intersection_evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        if perception_screen not in {"event_choice"}:
            return None
        if not option_evidence.get("all_present"):
            return None
        if selected not in TRAINING_TABS:
            return None
        if expected_tab is not None and selected != expected_tab:
            return None
        if not attribute_evidence.get("present"):
            return None
        if not failure_evidence.get("present"):
            return None
        if race_entry_evidence.get("present"):
            return None
        if race_hub_evidence.get("present"):
            return None
        if settings_evidence.get("present"):
            return None
        if crystal_reward_evidence.get("present"):
            return None
        if intersection_evidence.get("intersections"):
            return None
        return {
            "mode": "strong_training_menu_structure_overrides_event_choice_perception",
            "perception_screen": perception_screen,
            "selected_tab": selected,
            "expected_tab": expected_tab,
        }

    @staticmethod
    def _reject(failures: list[str], evidence: dict[str, Any], reason: str) -> TrainingMenuPrecheckResult:
        return TrainingMenuPrecheckResult(
            accepted=False,
            reason=reason,
            selected_tab=None,
            evidence=evidence,
            failures=failures,
        )


def _tab_centers() -> dict[str, tuple[int, int]]:
    return {
        "speed": (205, 2138),
        "stamina": (432, 2178),
        "power": (668, 2178),
        "guts": (912, 2178),
        "wisdom": (1192, 2178),
    }


def _icon_feature(bgr: np.ndarray) -> np.ndarray:
    if bgr.size == 0:
        return np.zeros(34, dtype=np.float32)

    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    saturated = (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 100)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 120) > 0
    edge_neighborhood = cv2.dilate(
        edges.astype(np.uint8),
        np.ones((3, 3), np.uint8),
    ).astype(bool)
    mask = saturated & edge_neighborhood
    if int(np.sum(mask)) < 20:
        mask = saturated

    mask_u8 = mask.astype(np.uint8)
    hue_hist = cv2.calcHist([hsv], [0], mask_u8, [18], [0, 180]).ravel()
    sat_hist = cv2.calcHist([hsv], [1], mask_u8, [8], [0, 256]).ravel()
    val_hist = cv2.calcHist([hsv], [2], mask_u8, [8], [0, 256]).ravel()
    feature = np.concatenate([hue_hist, sat_hist * 0.25, val_hist * 0.15]).astype(np.float32)
    norm = float(np.linalg.norm(feature))
    if norm == 0.0:
        return feature
    return feature / norm


def _intersects(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    return ax1 < bx2 and ax2 > bx1 and ay1 < by2 and ay2 > by1
