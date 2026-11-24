"""L'Arc aptitude perception and the observed race-essential upgrade workflow.

Supports race essentials and selected training/global upgrade bundles through
the same purchase session. Coordinates come from the user's demonstration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import Counter
from itertools import combinations
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

import cv2
import numpy as np

from core.auto_trainer.career_start_safety import TesseractTsvTokenReader
from core.frame_io import io_path
from core.main_menu_state import _load_assets, _normalize_binary_glyph, _rank_glyph_digits


SLOTS = (
    (146, 56, 347, 175), (372, 56, 572, 175),
    (34, 188, 234, 309), (258, 188, 459, 309), (484, 188, 684, 309),
    (34, 321, 234, 441), (258, 321, 459, 441), (484, 321, 684, 441),
    (146, 454, 347, 575), (372, 454, 572, 575),
)
ESSENTIAL_NAMES = ("海外草地适应性", "隆尚适应性", "生活节奏", "营养管理", "法语能力", "海外远征", "胆魄", "意志力", "凯旋门的希望", "凯旋门奖连霸之梦")
LEVEL_TWO_COSTS = (50, 50, 100, 100, 100, 100, 200, 200, 200, 150)
TRAINING_UPGRADES = {"guts": ("guts_expedition", 0), "stamina": ("stamina_expedition", 1),
                     "power": ("power_expedition", 2), "speed": ("speed_expedition", 3),
                     "wisdom": ("french_three", 4)}
TRAINING_PHASE_SLOTS = {phase: slot for phase, slot in TRAINING_UPGRADES.values()}
PHASE_SLOT_COUNTS = {"first_exchange": 2, "first_expedition": 3, "speed_expedition": 4,
                     "guts_expedition": 1, "stamina_expedition": 2, "power_expedition": 3,
                     "french_three": 5, "expedition_skill_pt": 6, "willpower_three": 8,
                     "courage_three": 7,
                     "first_overseas_race": 5, "first_arc": 8, "foy_prelude": 8, "final_arc": 10,
                     "champion_dream_two": 10}

# An explicitly selected first-expedition plan may reserve the language cost
# for growth upgrades. Only slot4 is optional in these separate phases.
LANGUAGE_DEFERRED_PHASES = {
    "first_overseas_race_defer_language": "first_overseas_race",
    "first_arc_defer_language": "first_arc",
}
PHASE_SLOT_COUNTS.update({name: PHASE_SLOT_COUNTS[base]
                         for name, base in LANGUAGE_DEFERRED_PHASES.items()})

# 1.9.8 can keep the power-related Lv2 cost through Niel. The first Arc
# still uses its original targets; this changes timing, not total liability.
POWER_DEFERRED_PHASES = {
    'first_overseas_race_defer_power': 'first_overseas_race',
    'first_overseas_race_defer_power_language': 'first_overseas_race',
}
PHASE_SLOT_COUNTS.update({name: PHASE_SLOT_COUNTS[base]
                         for name, base in POWER_DEFERRED_PHASES.items()})

# One chosen training upgrade plus any applicable global upgrades. The same
# bounded phase names are understood by planning, persistence and execution.
GROWTH_PHASE_SLOTS = {**TRAINING_PHASE_SLOTS, "expedition_skill_pt": 5,
                     "courage_three": 6, "willpower_three": 7}
BUNDLE_PHASES = {}
for _size in range(2, 5):
    for _phases in combinations(GROWTH_PHASE_SLOTS, _size):
        if sum(p in TRAINING_PHASE_SLOTS for p in _phases) > 1:
            continue
        _phases = tuple(sorted(_phases, key=GROWTH_PHASE_SLOTS.get))
        _name = "bundle_" + "_".join(str(GROWTH_PHASE_SLOTS[p]) for p in _phases)
        BUNDLE_PHASES[_name] = _phases
        PHASE_SLOT_COUNTS[_name] = max(GROWTH_PHASE_SLOTS[p] for p in _phases) + 1

# Mixed target levels must not alias the existing all-Lv3 bundle_4_5.
# Register the single targeted phase on the existing generic bundle CLI too;
# its explicit plan branch below prevents recursive singleton expansion.
BUNDLE_PHASES.update({'french_two': ('french_two',),
                     'bundle_french_two_pt': ('french_two', 'expedition_skill_pt')})
PHASE_SLOT_COUNTS.update({'french_two': 5, 'bundle_french_two_pt': 6})

# Growth phases name one target, unlike the cumulative race-essential phases.
# A cache or interrupted visit can request a target that the current grid has
# already acquired; observing it again must not require buying it a second time.
SINGLE_UPGRADE_TARGETS = {
    **{phase: {slot: 3} for phase, slot in TRAINING_PHASE_SLOTS.items()},
    'french_two': {4: 2}, 'expedition_skill_pt': {5: 3},
    'courage_three': {6: 3}, 'willpower_three': {7: 3},
    'champion_dream_two': {9: 2},
}


def phase_required_levels(phase):
    """Actual targets, not the prefix used to size/read a phase's grid."""
    if phase in SINGLE_UPGRADE_TARGETS:
        return dict(SINGLE_UPGRADE_TARGETS[phase])
    if phase in BUNDLE_PHASES:
        return {slot: level for member in BUNDLE_PHASES[phase]
                for slot, level in phase_required_levels(member).items()}
    required = {slot: 2 for slot in range(9 if phase == 'final_arc' else PHASE_SLOT_COUNTS[phase])}
    if phase in LANGUAGE_DEFERRED_PHASES:
        required.pop(4, None)
    if phase in POWER_DEFERRED_PHASES:
        required.pop(2, None)
        if phase.endswith('_language'):
            required.pop(4, None)
    return required


def growth_bundle_phase(phases):
    if 'french_two' in phases:
        phases = tuple(phases)
        if phases == ('french_two',):
            return 'french_two'
        if len(phases) == 2 and set(phases) == {'french_two', 'expedition_skill_pt'}:
            return 'bundle_french_two_pt'
        raise ValueError('adaptation_bundle_invalid')
    phases = tuple(sorted(phases, key=GROWTH_PHASE_SLOTS.get))
    if len(phases) == 1:
        return phases[0]
    name = "bundle_" + "_".join(str(GROWTH_PHASE_SLOTS[p]) for p in phases)
    if BUNDLE_PHASES.get(name) != phases:
        raise ValueError("adaptation_bundle_invalid")
    return name


def adaptation_target_levels_ready(levels, plan):
    if not isinstance(levels, list) or len(levels) != len(plan["targets"]):
        return False
    changed = [i for i,(start,target) in enumerate(zip(plan["baseline_levels"],plan["targets"])) if target>start]
    return (all(levels[i] == plan["targets"][i] for i in changed) if changed
            else levels == plan["targets"])


def adaptation_relevant_errors(errors, plan):
    changed = {i+1 for i,(start,target) in enumerate(zip(plan["baseline_levels"],plan["targets"])) if target>start}
    result = []
    for error in errors:
        if error in {"points_unreadable", "point_reader_unavailable"}:
            continue
        slot = re.fullmatch(r"slot_(\d+)_unreadable", error)
        if changed and slot and int(slot[1]) not in changed:
            continue
        result.append(error)
    return result


def selected_upgrade_cost(baseline, levels):
    total = 0
    for slot, (start, level) in enumerate(zip(baseline, levels)):
        if start == level:
            continue
        if not 1 <= start < level <= (2 if slot >= 8 else 3):
            return -1
        if start == 1:
            total += LEVEL_TWO_COSTS[slot]
        if level == 3:
            total += 300 if slot in (6, 7) else 200
    return total


def _green(hsv: np.ndarray) -> np.ndarray:
    return (hsv[:, :, 0] > 35) & (hsv[:, :, 0] < 75) & (hsv[:, :, 1] > 130) & (hsv[:, :, 2] > 160)


def _white(hsv: np.ndarray) -> np.ndarray:
    return (hsv[:, :, 1] < 50) & (hsv[:, :, 2] > 225)


def _number_mask(hsv: np.ndarray) -> np.ndarray:
    # Pending levels are orange; committed levels and points are brown.
    return ((hsv[:, :, 0] >= 5) & (hsv[:, :, 0] <= 30) & (hsv[:, :, 1] > 80)).astype(np.uint8) * 255


def _accepted_level_glyph(index: int, best: dict, second: dict) -> bool:
    margin = second["score"] - best["score"]
    ordinary = (best["digit"] in (1, 2, 3) and best["score"] <= 0.28
                and margin >= 0.06 and best["coverage"] >= 0.78)
    # All slots use the same pending orange level glyph, not only speed.
    pending_three = (best["digit"] == 3 and second["digit"] == 8
                            and best["score"] <= 0.22 and best["coverage"] >= 0.80
                            and margin >= 0.05)
    return ordinary or pending_three


class LarcAdaptationReader:
    def __init__(self, token_reader=None, *, numeric_reader=None):
        self.token_reader = token_reader or TesseractTsvTokenReader()
        self.numeric_reader = numeric_reader

    def _numeric_value(self, crop, primary, field, *, minimum, maximum, eligible, suffixes=("",)):
        from core import numeric_ocr_crosscheck as crosscheck
        from core.main_menu_state import secondary_number_accepted
        threshold = crosscheck.second_opinion_min_score()
        record = {"field": field, "primary": {"value": primary,
            "status": "accepted" if type(primary) is int else "abstained",
            "engine": "template_or_tesseract"}, "second": None,
            "min_score": threshold, "minimum": minimum, "maximum": maximum,
            "source_frame_sha256": self._numeric_frame_sha256}
        value = primary
        if type(primary) is int:
            decision, reason = "primary_accepted", "primary_accepted"
        elif not crosscheck.crosscheck_enabled():
            decision, reason = "primary_retained", "crosscheck_disabled"
        elif not eligible:
            decision, reason = "primary_retained", "numeric_slot_or_unit_unconfirmed"
        else:
            try:
                second = crosscheck.read_number(crop, reader=self.numeric_reader,
                    minimum=minimum, maximum=maximum, prefixes=("",), suffixes=suffixes)
            except Exception:
                second = {"status": "abstained", "value": None, "confidence": None,
                    "engine": "local_rapidocr_numeric", "reason": "secondary_reader_unavailable"}
            record["second"] = second
            decision, reason = "secondary_abstained", second.get("reason", "secondary_invalid")
            if secondary_number_accepted(second, minimum=minimum, maximum=maximum):
                value, decision = second["value"], "secondary_accepted"
            self._numeric_evidence[field] = {**record, "decision": decision}
        try:
            crosscheck.archive("larc_adaptation", decision=decision, reason=reason, **record)
        except Exception:
            pass
        return value

    def read_grid(self, image: np.ndarray) -> dict[str, Any]:
        canonical = cv2.resize(image, (720, 1280))
        self._numeric_evidence = {}
        self._numeric_frame_sha256 = hashlib.sha256(canonical.tobytes()).hexdigest()
        hsv = cv2.cvtColor(canonical, cv2.COLOR_BGR2HSV)
        green = _green(hsv)
        levels, selected, errors = [], [], []
        manifest, assets = _load_assets()
        for index, (x1, y1, x2, y2) in enumerate(SLOTS):
            strip = hsv[y2 - 28:y2 - 5, x1 + 5:x1 + 25]
            gray = (strip[:, :, 1] < 100) & (strip[:, :, 2] < 225) & (strip[:, :, 2] > 90)
            if float(gray.mean()) > 0.85 and float(_white(strip).mean()) < 0.05:
                levels.append(0)
                self._numeric_value(canonical[y2-31:y2-3, x1:x2], 0, f"slot_{index+1}",
                    minimum=1, maximum=3, eligible=False)
            else:
                cx = (x1 + x2) // 2
                mask = _number_mask(hsv[y2 - 31:y2 - 3, cx + 18:cx + 43])
                _, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
                glyphs = [row for row in stats[1:] if row[cv2.CC_STAT_AREA] >= 15 and row[cv2.CC_STAT_HEIGHT] >= 12]
                value = None
                if len(glyphs) == 1 and float(_white(strip).mean()) > 0.35:
                    x, y, w, h, _ = glyphs[0]
                    ranked = _rank_glyph_digits(
                        _normalize_binary_glyph(mask[y:y+h, x:x+w], tuple(manifest["glyph_sizes"]["small"])),
                        assets["glyphs"]["small"],
                    )
                    best, second = ranked[:2]
                    if _accepted_level_glyph(index, best, second):
                        value = best["digit"]
                value = self._numeric_value(canonical[y2-31:y2-3, cx+18:cx+43], value,
                    f"slot_{index+1}", minimum=1, maximum=3,
                    eligible=float(_white(strip).mean()) > 0.35 and bool(np.any(mask)))
                levels.append(value)
                if value is None:
                    errors.append(f"slot_{index + 1}_unreadable")
            corners = [green[y-8:y+12, x-8:x+12].mean()
                       for x, y in ((x1, y1), (x2, y1), (x1, y2), (x2, y2))]
            if min(corners) > 0.20:
                selected.append(index)

        # Read the two labelled point boxes in spatial order, in ONE OCR call.
        # Full-screen OCR can reverse these columns even when both numbers are right.
        canvas = np.full((240, 460, 3), 255, dtype=np.uint8)
        digit_counts = []
        point_glyphs = []
        point_masks = []
        for row, (x1, x2) in enumerate(((155, 355), (497, 699))):
            mask = _number_mask(hsv[982:1016, x1:x2])
            point_masks.append(mask)
            _, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
            glyphs = sorted((item for item in stats[1:] if item[cv2.CC_STAT_AREA] > 15
                             and item[cv2.CC_STAT_HEIGHT] >= 10), key=lambda item: item[0])
            # Keep the unit as an OCR anchor and verify the numeric glyph count.
            if len(glyphs) < 3:
                errors.append("point_unit_unreadable")
            digit_counts.append(len(glyphs) - 2)
            point_glyphs.append([mask[y:y+h, x:x+w].copy() for x, y, w, h, _ in glyphs[:-2]])
            crop = cv2.cvtColor(cv2.resize(255-mask, None, fx=2, fy=2,
                                          interpolation=cv2.INTER_NEAREST), cv2.COLOR_GRAY2BGR)
            canvas[20+120*row:88+120*row, 20:20+crop.shape[1]] = crop
        values = [None, None]
        point_glyph_fallback_rows = []
        point_roi_rescan_rows = []
        try:
            _, png = cv2.imencode(".png", canvas)
            tokens = self.token_reader.read(png.tobytes())
            for row in range(2):
                line = sorted((token for token in tokens if token.bbox is not None
                               and int((token.bbox[1] + token.bbox[3] / 2) // 120) == row),
                              key=lambda token: token.bbox[0])
                text = "".join(token.text for token in line).replace(" ", "")
                match = re.fullmatch(r"([0-9Oo]{1,5})[pP][tT]", text)
                # Tesseract's word confidence drops on '5Opt', although the
                # fixed numeric field unambiguously means 50. Check geometry
                # and the unit instead of treating word confidence as a probability.
                if match and len(match.group(1)) == digit_counts[row]:
                    values[row] = int(match.group(1).replace("O", "0").replace("o", "0"))
                elif re.fullmatch(r"[0-9A-Za-z]{1,5}[pP][tT]", text) and len(text) - 2 == digit_counts[row]:
                    # A word OCR can read 990pt as 9S0pt. Keep its unit and
                    # glyph-count anchors; recover digits from their pixels.
                    digits = []
                    thresholds = manifest["glyph_thresholds"]["small"]
                    for character, glyph in zip(text[:-2], point_glyphs[row]):
                        # Preserve digits already read by the same anchored OCR
                        # path above; only repair its non-digit confusions.
                        if character in "0123456789":
                            digits.append(character)
                            continue
                        ranked = _rank_glyph_digits(
                            _normalize_binary_glyph(glyph, tuple(manifest["glyph_sizes"]["small"])),
                            assets["glyphs"]["small"],
                        )
                        best, second = ranked[:2]
                        if (best["score"] > thresholds["max_best_score"]
                                or second["score"] - best["score"] < thresholds["min_margin"]
                                or best["coverage"] < thresholds["min_coverage"]):
                            break
                        digits.append(str(best["digit"]))
                    if len(digits) == digit_counts[row] and digits:
                        values[row] = int("".join(digits))
                        point_glyph_fallback_rows.append(row)
                elif match and 1 <= digit_counts[row] <= 5:
                    # Sparse two-row OCR can split one printed digit (590pt
                    # becomes 5390pt). Re-read only that same labelled field
                    # at another scale; never drop an OCR digit or infer a
                    # balance. The pt unit and pixel glyph count still apply.
                    crop = cv2.resize(255-point_masks[row], None, fx=3, fy=3,
                                      interpolation=cv2.INTER_NEAREST)
                    crop = cv2.copyMakeBorder(crop, 20, 20, 20, 20,
                                              cv2.BORDER_CONSTANT, value=255)
                    _, retry_png = cv2.imencode(".png", crop)
                    retry_tokens = self.token_reader.read(retry_png.tobytes())
                    retry_text = "".join(token.text for token in sorted(
                        retry_tokens, key=lambda token: token.bbox[0] if token.bbox else 0,
                    )).replace(" ", "")
                    retry_match = re.fullmatch(r"([0-9Oo]{1,5})[pP][tT]", retry_text)
                    if retry_match and len(retry_match.group(1)) == digit_counts[row]:
                        values[row] = int(retry_match.group(1).replace("O", "0").replace("o", "0"))
                        point_roi_rescan_rows.append(row)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            errors.append("point_reader_unavailable")
        # The same two fixed point boxes retain their glyph/unit presence gate;
        # the independent reader must itself read the pt suffix, never an
        # unrelated number or an inferred balance. Known primary zero stays zero.
        point_suffixes = tuple(space+unit for space in ("", " ") for unit in ("pt", "pT", "Pt", "PT"))
        for row, (x1, x2) in enumerate(((155, 355), (497, 699))):
            dc = digit_counts[row]
            row_min = (0 if dc == 1 else 10 ** (dc - 1)) if 1 <= dc <= 5 else 0
            row_max = ((10 ** dc) - 1) if 1 <= dc <= 5 else 99999
            values[row] = self._numeric_value(canonical[982:1016, x1:x2], values[row],
                "pending_cost" if row == 0 else "points", minimum=row_min, maximum=row_max,
                eligible=1 <= dc <= 5, suffixes=point_suffixes)
        if all(type(value) is int for value in values):
            errors = [error for error in errors if error != "point_reader_unavailable"]
        if None in values:
            errors.append("points_unreadable")
        if len(selected) != 1:
            errors.append("selected_slot_unreadable")
        # The grid owner verifies its page text; the ten levels, selected slot
        # and labelled point fields above establish the actionable state.
        # A selected item's blue heading adds no further identity or value.
        result = {"kind": "grid", "levels": levels, "selected_slot": selected[0] if len(selected) == 1 else None,
                "pending_cost": values[0], "points": values[1], "errors": errors,
                "point_glyph_fallback_rows": point_glyph_fallback_rows,
                "point_roi_rescan_rows": point_roi_rescan_rows}
        if self._numeric_evidence:
            result["numeric_crosscheck"] = self._numeric_evidence
        return result


def build_first_exchange_plan(observation: dict[str, Any]) -> dict[str, Any]:
    return build_adaptation_plan(observation, phase="first_exchange")


def build_adaptation_plan(observation: dict[str, Any], *, phase: str) -> dict[str, Any]:
    if phase not in PHASE_SLOT_COUNTS:
        raise ValueError("adaptation_phase_invalid")
    levels, points = observation.get("levels"), observation.get("points")
    if (observation.get("errors") or not isinstance(levels, list) or len(levels) != 10
            or any(type(level) is not int or not 0 <= level <= 3 for level in levels)
            or type(points) is not int or points < 0 or observation.get("pending_cost") != 0):
        raise ValueError("first_exchange_baseline_invalid")
    if phase in SINGLE_UPGRADE_TARGETS and all(
            levels[slot] >= target for slot, target in SINGLE_UPGRADE_TARGETS[phase].items()):
        return {"phase": phase, "baseline_levels": list(levels), "baseline_points": points,
                "targets": list(levels), "cost": 0, "deferred_slots": []}
    if phase in POWER_DEFERRED_PHASES:
        skipped = [2, 4] if phase.endswith('_language') else [2]
        if any(levels[slot] < 1 for slot in skipped):
            raise ValueError('power_defer_baseline_invalid')
        required = list(levels)
        for slot in skipped:
            required[slot] = max(2, required[slot])
        plan = build_adaptation_plan({'levels': required, 'points': points, 'pending_cost': 0},
                                     phase=POWER_DEFERRED_PHASES[phase])
        for slot in skipped:
            plan['targets'][slot] = levels[slot]
        plan.update(phase=phase, baseline_levels=list(levels),
                    power_debuff_deferred=levels[2] == 1, power_due_ordinal=42,
                    language_debuff_deferred=4 in skipped and levels[4] == 1,
                    skipped_slots=[slot for slot in skipped if levels[slot] == 1])
        return plan
    if phase in LANGUAGE_DEFERRED_PHASES:
        if levels[4] < 1:
            raise ValueError("language_defer_baseline_invalid")
        # Build the same required plan with language already cleared, then
        # preserve the actual language level and its unspent cost verbatim.
        required = list(levels)
        required[4] = max(2, required[4])
        plan = build_adaptation_plan({"levels": required, "points": points,
                                      "pending_cost": 0}, phase=LANGUAGE_DEFERRED_PHASES[phase])
        plan.update(phase=phase, baseline_levels=list(levels),
                    language_debuff_deferred=True, skipped_slots=[4])
        plan["targets"][4] = levels[4]
        return plan
    if phase == 'french_two':
        if levels[4] != 1 or points < LEVEL_TWO_COSTS[4]:
            raise ValueError('french_two_baseline_invalid')
        targets = list(levels)
        targets[4] = 2
        return {'phase': phase, 'baseline_levels': list(levels), 'baseline_points': points,
                'targets': targets, 'cost': LEVEL_TWO_COSTS[4], 'deferred_slots': []}
    if phase in BUNDLE_PHASES:
        targets, remaining = list(levels), points
        for member in BUNDLE_PHASES[phase]:
            part = build_adaptation_plan({"levels": targets, "points": remaining,
                                          "pending_cost": 0}, phase=member)
            targets, remaining = part["targets"], remaining - part["cost"]
        return {"phase": phase, "baseline_levels": list(levels), "baseline_points": points,
                "targets": targets, "cost": points - remaining, "deferred_slots": []}
    if phase in TRAINING_PHASE_SLOTS:
        slot = TRAINING_PHASE_SLOTS[phase]
        cost = 200 + (LEVEL_TWO_COSTS[slot] if levels[slot] == 1 else 0)
        if levels[slot] not in {1, 2} or points < cost:
            raise ValueError(f"{phase}_baseline_invalid")
        targets = list(levels)
        targets[slot] = 3
        return {"phase": phase, "baseline_levels": list(levels), "baseline_points": points,
                "targets": targets, "cost": cost, "deferred_slots": []}
    if phase == "champion_dream_two":
        if levels[9] != 1 or points < 150:
            raise ValueError("champion_dream_two_baseline_invalid")
        targets = list(levels)
        targets[9] = 2
        return {"phase": phase, "baseline_levels": list(levels), "baseline_points": points,
                "targets": targets, "cost": 150, "deferred_slots": []}
    if phase == "expedition_skill_pt":
        cost = {1: 300, 2: 200}.get(levels[5])
        if cost is None or points < cost:
            raise ValueError("expedition_skill_pt_baseline_invalid")
        targets = list(levels)
        targets[5] = 3
        return {"phase": phase, "baseline_levels": list(levels), "baseline_points": points,
                "targets": targets, "cost": cost, "deferred_slots": []}
    if phase == "willpower_three":
        cost = {1: 500, 2: 300}.get(levels[7])
        if cost is None or points < cost:
            raise ValueError("willpower_three_baseline_invalid")
        targets = list(levels)
        targets[7] = 3
        return {"phase": phase, "baseline_levels": list(levels), "baseline_points": points,
                "targets": targets, "cost": cost, "deferred_slots": []}
    if phase == "courage_three":
        if levels[6] != 2 or points < 300:
            raise ValueError("courage_three_baseline_invalid")
        targets = list(levels)
        targets[6] = 3
        return {"phase": phase, "baseline_levels": list(levels), "baseline_points": points,
                "targets": targets, "cost": 300, "deferred_slots": []}
    targets, deferred, budget = list(levels), [], points
    for slot in range(PHASE_SLOT_COUNTS[phase]):
        if levels[slot] >= 2:
            continue
        cost = LEVEL_TWO_COSTS[slot]
        # The final bonus is bought together with race essentials when unlocked
        # and affordable. Its absence must not block an otherwise legal race.
        if phase == "final_arc" and slot == 9 and (levels[slot] != 1 or budget < cost or deferred):
            continue
        if levels[slot] == 1 and budget >= cost:
            targets[slot], budget = 2, budget - cost
        else:
            deferred.append(slot)
    return {"phase": phase, "baseline_levels": list(levels), "baseline_points": points,
            "targets": targets, "cost": points - budget, "deferred_slots": deferred}


@dataclass
class LarcAdaptationSession:
    run_session_id: str
    path: Path | None = None
    plan: dict[str, Any] | None = None
    source_sha256: str | None = None
    stage: str = "selecting"
    submitted_source_sha256: str | None = None
    receipt: dict[str, Any] | None = None
    inspect_only: bool = False
    inspected_slots: dict[str, Any] = field(default_factory=dict)
    returned: bool = False
    phase: str = "first_exchange"
    inspect_grid_only: bool = False
    stay_on_grid: bool = False
    pending_retry_used: bool = False
    pending_preview_frames: int = 0
    control_waits: dict[str, int] = field(default_factory=dict)
    selection_ready: bool = False

    def to_dict(self):
        return {"schema_version": "uma.larc_adaptation.v1", "run_session_id": self.run_session_id,
                "plan": self.plan, "source_sha256": self.source_sha256, "stage": self.stage,
                "submitted_source_sha256": self.submitted_source_sha256, "receipt": self.receipt,
                "inspect_only": self.inspect_only, "inspected_slots": self.inspected_slots,
                "returned": self.returned, "phase": self.phase,
                **({"inspect_grid_only": True} if self.inspect_grid_only else {}),
                **({"stay_on_grid": True} if self.stay_on_grid else {}),
                **({"pending_retry_used": True} if self.pending_retry_used else {}),
                **({"pending_preview_frames": self.pending_preview_frames} if self.pending_preview_frames else {}),
                **({"control_waits": dict(self.control_waits)} if self.control_waits else {}),
                **({"selection_ready": True} if self.selection_ready else {})}

    def save(self):
        if self.path is None:
            raise ValueError("adaptation_session_path_required")
        target = io_path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         suffix=".tmp", delete=False) as stream:
            json.dump(self.to_dict(), stream, ensure_ascii=False, indent=2)
            temporary = stream.name
        os.replace(temporary, target)

    @classmethod
    def load(cls, path: Path, run_session_id: str):
        value = json.loads(io_path(path).read_text(encoding="utf-8"))
        if (value.get("schema_version") != "uma.larc_adaptation.v1"
                or value.get("run_session_id") != run_session_id
                or value.get("stage") not in {"selecting", "submission_pending", "awaiting_result", "complete"}):
            raise ValueError("adaptation_session_invalid")
        if (type(value.get("inspect_only", False)) is not bool or type(value.get("returned", False)) is not bool
                or not isinstance(value.get("inspected_slots", {}), dict)
                or type(value.get("inspect_grid_only", False)) is not bool
                or type(value.get("stay_on_grid", False)) is not bool
                or type(value.get("pending_retry_used", False)) is not bool
                or type(value.get("selection_ready", False)) is not bool
                or type(value.get("pending_preview_frames", 0)) is not int
                or not 0 <= value.get("pending_preview_frames", 0) <= 2
                or not isinstance(value.get('control_waits', {}), dict)
                or any(key not in {'review', 'upgrade', 'level_up', 'result_close', 'selection_cost', 'untracked_change'} or type(count) is not int
                       or not 0 <= count <= 3 for key, count in value.get('control_waits', {}).items())
                or value.get("stay_on_grid", False) and not value.get("inspect_grid_only", False)
                or value.get("inspect_grid_only", False) and not value.get("inspect_only", False)):
            raise ValueError("adaptation_session_invalid")
        phase = value.get("phase", "first_exchange")
        if phase not in PHASE_SLOT_COUNTS:
            raise ValueError("adaptation_phase_invalid")
        plan = value.get("plan")
        if plan is not None:
            expected = build_adaptation_plan({"levels": plan.get("baseline_levels"),
                "points": plan.get("baseline_points"), "pending_cost": 0}, phase=phase)
            if plan != expected or re.fullmatch(r"[0-9a-f]{64}", value.get("source_sha256") or "") is None:
                raise ValueError("adaptation_plan_invalid")
        if value["stage"] != "selecting" and (plan is None or re.fullmatch(
                r"[0-9a-f]{64}", value.get("submitted_source_sha256") or "") is None):
            raise ValueError("adaptation_submission_invalid")
        return cls(run_session_id, path, plan, value.get("source_sha256"), value["stage"],
                   value.get("submitted_source_sha256"), value.get("receipt"),
                   value.get("inspect_only", False), value.get("inspected_slots", {}), value.get("returned", False), phase,
                   value.get("inspect_grid_only", False), value.get("stay_on_grid", False),
                   value.get("pending_retry_used", False), value.get("pending_preview_frames", 0),
                   dict(value.get('control_waits', {})), value.get('selection_ready', False))

    def mark_submitted(self, source_sha256: str):
        self.stage = "awaiting_result"
        self.submitted_source_sha256 = source_sha256
        self.pending_preview_frames = 0


def _navigation_tap(image, evidence, reason, point, route_state):
    return {"action": "tap", "reason": "larc_adaptation_" + reason, "route_state": route_state,
            "tap": {"coordinate_name": "safe_advance.larc_adaptation_" + reason,
                    "x": round(point[0] * image.shape[1] / 720),
                    "y": round(point[1] * image.shape[0] / 1280), "scaled": False}, "evidence": evidence}


def plan_larc_adaptation_entry(image, evidence, session, *, race_day=False):
    """Called only after the shared career main-menu controls are verified."""
    if session is None or session.returned:
        return None
    text = "".join(str(evidence.get("screen_text", {}).get("normalized_text", "")).split())
    hsv = cv2.cvtColor(cv2.resize(image, (720, 1280)), cv2.COLOR_BGR2HSV)
    button = hsv[1070:1130, 550:664] if race_day else hsv[1092:1168, 384:502]
    blue = (button[:, :, 0] > 90) & (button[:, :, 0] < 130) & (button[:, :, 1] > 70)
    if "海外适应性" not in text or float(blue.mean()) < 0.15:
        return None
    return _navigation_tap(image, evidence, "open", (607, 1100) if race_day else (445, 1133), "main_menu")


def plan_larc_adaptation_return(image, decision):
    """Return only from a fully read grid with no uncommitted selections."""
    if decision.get("reason") not in {"larc_adaptation_verified", "larc_adaptation_already_ready",
                                     "larc_adaptation_deferred", "larc_adaptation_inspection_complete"}:
        return decision
    detail = decision["evidence"]["larc_adaptation"]
    hsv = cv2.cvtColor(cv2.resize(image, (720, 1280)), cv2.COLOR_BGR2HSV)
    # The demonstration's mouse/click effect covers the label centre; the
    # left inset still verifies the white return button independently.
    if detail.get("pending_cost") != 0:
        return decision
    if (np.mean(_white(hsv[1212:1265, 43:112])) < 0.35
            or np.mean(_white(hsv[1215:1245, 44:61])) < 0.60):
        # The grid values settle before the return button fades back in.
        return {**decision, "action": "wait", "reason": "larc_adaptation_modal_loading"}
    detail["completion_reason"] = decision["reason"]
    return _navigation_tap(image, decision["evidence"], "return", (86, 1238), "modal")


def _confirmation_items(text, session):
    """Compatibility for sessions saved before fixed-slot progress existed."""
    if (session is None or not session.plan or not session.plan.get('cost')
            or '海外适应性升级确认' not in text):
        return False
    plan = session.plan
    slots = [i for i in range(PHASE_SLOT_COUNTS[session.phase])
             if plan['targets'][i] > plan['baseline_levels'][i]]
    expected = [ESSENTIAL_NAMES[i] + '等级' + str(plan['targets'][i]) for i in slots]
    names = '|'.join(re.escape(name) for name in ESSENTIAL_NAMES)
    observed = [name + '等级' + value for name, value in re.findall(rf'({names})等级([123])', text)]
    return bool(expected and Counter(observed) == Counter(expected))


_CONTROL_REGIONS = {
    'review': ((210, 1037, 510, 1120), (279, 1046, 440, 1110), (360, 1078), '确定'),
    'upgrade': ((370, 1136, 668, 1220), (446, 1150, 600, 1207), (519, 1182), '升级'),
    'level_up': ((480, 696, 538, 753), (490, 704, 526, 744), (508, 724), None),
    'result_close': ((205, 853, 520, 973), (285, 888, 440, 946), (360, 919), '关闭'),
}


def _adaptation_control(image, kind, *, text_reader=None):
    """Observe a current control; hue is neither identity nor enable authority.

    A dim control with a rendered outline is explicitly disabled. Incomplete
    outlines/labels remain unknown, with one regional text fallback when the
    label is visible. The caller owns the transaction and bounds re-observation.
    """
    normalized = cv2.resize(image, (720, 1280))
    outer, label, point, expected_label = _CONTROL_REGIONS[kind]
    x1, y1, x2, y2 = outer
    gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
    crop = gray[y1:y2, x1:x2]
    a, b, c, d = label
    letters = gray[b:d, a:c]
    background = float(np.median(crop))
    low, high = np.percentile(letters, [5, 98])
    mask = ((letters < background - 35) if kind == 'result_close'
            else (letters > max(210, background + 35))).astype(np.uint8)
    _, _, components, _ = cv2.connectedComponentsWithStats(mask, 8)
    foreground = any(20 <= row[cv2.CC_STAT_AREA] <= letters.size*.55 and row[cv2.CC_STAT_HEIGHT] >= 7
                     for row in components[1:])
    edges = cv2.Canny(crop, 30, 80)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = {cv2.boundingRect(contour) for contour in contours}
    outline = next(([x+x1, y+y1, width, height]
        for x, y, width, height in sorted(boxes, key=lambda box: box[2]*box[3], reverse=True)
        if width >= crop.shape[1]*.50 and height >= crop.shape[0]*.45
        and x+x1 < point[0] < x+x1+width and y+y1 < point[1] < y+y1+height), None)
    proof = {'status': 'unknown', 'kind': kind, 'point': list(point),
             'box': outline, 'foreground_visible': bool(foreground)}
    if outline is not None and kind != 'result_close' and high < 195 and np.percentile(crop, 90) < 185:
        return dict(proof, status='disabled', basis='rendered_dim_control_and_label')
    if outline is not None and foreground:
        return dict(proof, status='enabled', basis='control_outline_and_foreground_contrast')
    if expected_label is not None and high-low >= 40:
        from core.larc_growth_reader import _line
        observed, confidence = _line(normalized, label, text_reader)
        proof.update(observed_label=observed, label_confidence=confidence)
        if observed == expected_label and confidence >= .90 and foreground:
            return dict(proof, status='enabled', basis='current_control_label_and_contrast')
    return proof


def _result_close_control(image):
    proof = _adaptation_control(image, 'result_close')
    return proof if proof['status'] == 'enabled' else None


def plan_larc_adaptation(image, evidence, *, session=None, reader=None):
    text = "".join(str(evidence.get("screen_text", {}).get("normalized_text", "")).split())
    selected_plan_ready = bool(session is not None and session.selection_ready)
    confirmation_items_match = ((selected_plan_ready and '海外适应性升级确认' in text)
        or _confirmation_items(text, session))
    if (session is not None and session.stage == "selecting" and session.plan
            and not session.inspect_only):
        from .larc_control_wait import adaptation_modal_content_pending as modal_content_pending
        if not confirmation_items_match and modal_content_pending(image):
            # The owned confirmation shell renders before its title/items.
            # Waiting does not need exact OCR of that still-fading text and
            # never authorizes an upgrade until the selected plan check passes.
            return {"action": "wait", "reason": "larc_adaptation_modal_loading",
                    "route_state": "modal", "evidence": evidence}
    grid = "海外适应性" in text and any(
        term in text for term in ("获得条件一览", "效果一览", "消耗积分"))
    confirmation = "海外适应性升级确认" in text
    fading_confirmation = (
        not confirmation
        and "是否升级以上海外适应性" in text
        and "取消" in text
        and "升级" in text
    )
    if fading_confirmation and not evidence.get("screen_text", {}).get("dangerous"):
        evidence.setdefault("larc_adaptation", {})["kind"] = "fading_confirmation"
        return {"action": "wait", "reason": "larc_adaptation_modal_loading",
                "route_state": "modal", "evidence": evidence}
    result = "攻克难关" in text and "通过升级海外适应性" in text
    animation = (
        ("海外适应性等级提升" in text and ("获得海外适应性效果" in text or "等级提升了" in text))
        or ("获得海外适应性效果" in text and "海外远征的等级提升了" in text)
    )
    if not (grid or confirmation or result or animation):
        if (session is not None and session.stage == "awaiting_result"
                and session.submitted_source_sha256 is not None
                and not evidence.get("screen_text", {}).get("dangerous")):
            # Keep an already submitted transaction with its owner while its
            # return page renders. A missing OCR title is not a new event page.
            evidence.setdefault("larc_adaptation", {})["kind"] = "settling"
            return {"action": "wait", "reason": "larc_adaptation_awaiting_result",
                    "route_state": "modal", "evidence": evidence}
        return None
    detail = evidence.setdefault("larc_adaptation", {})
    def decision(reason, point=None, *, wait=False):
        action = "tap" if point is not None else "wait" if wait else "stop"
        response = {"action": action, "reason": f"larc_adaptation_{reason}",
                    "route_state": "modal", "evidence": evidence}
        if point is not None:
            response["tap"] = {"coordinate_name": f"safe_advance.larc_adaptation_{reason}",
                "x": round(point[0] * image.shape[1] / 720),
                "y": round(point[1] * image.shape[0] / 1280), "scaled": False}
        return response

    def control(kind, unreadable, *, wait_reason='modal_loading'):
        observed = _adaptation_control(image, kind)
        detail.setdefault('controls', {})[kind] = observed
        if observed['status'] == 'enabled':
            if session is not None:
                session.control_waits.pop(kind, None)
            return None
        if session is None:
            return decision(wait_reason, wait=True)
        count = min(3, session.control_waits.get(kind, 0) + 1)
        session.control_waits[kind] = count
        observed['read_only_observations'] = count
        return (decision(wait_reason, wait=True) if count <= 2 else
                decision(kind + '_disabled' if observed['status'] == 'disabled' else unreadable))

    # An informational/result page is navigation, even after a restart or in
    # inspection mode. Its close control never supplies spending authority.
    if result:
        blocked = control('result_close', 'result_unverified', wait_reason='awaiting_result')
        if blocked is not None:
            return blocked
        detail['result_close_control'] = detail['controls']['result_close']
        detail['kind'] = 'notice'
        if (session is not None and session.stage == 'submission_pending'
                and session.plan and session.submitted_source_sha256):
            # The actual result page resolves an interrupted transport attempt.
            # Retain its original source and read the following grid; this is
            # never authority to send the purchase again.
            session.mark_submitted(session.submitted_source_sha256)
            detail['submission_resolved_by'] = 'current_result_notice'
        # Observe the following grid/home afresh; closing does not settle or
        # recreate any purchase, and does not manufacture a receipt.
        return decision('result_close', (360, 919))
    if animation:
        if session is not None and session.inspect_only:
            return decision("inspection_no_spending")
        detail['kind'] = 'animation'
        return decision("awaiting_result", wait=True)
    if session is None:
        return decision("session_required")
    if session.stage == "submission_pending":
        return decision("submission_outcome_unknown")
    plan = session.plan
    if session.inspect_only and (confirmation or result):
        return decision("inspection_no_spending")
    if confirmation:
        from .larc_control_wait import adaptation_modal_content_pending as modal_content_pending
        if not confirmation_items_match and modal_content_pending(image):
            return decision("modal_loading", wait=True)
        if plan is None or not plan["cost"]:
            return decision("plan_required")
        if session.stage != "selecting":
            return decision("awaiting_result", wait=True)
        # Review was reached from this plan's fixed slots, target levels and
        # selected cost. Re-reading the scrollable item names adds no purchase
        # identity and must not stall on omitted, duplicated or faded OCR.
        if not confirmation_items_match:
            return decision('confirmation_mismatch')
        blocked = control('upgrade', 'upgrade_control_unreadable')
        if blocked is not None:
            return blocked
        detail["expected_cost"] = plan["cost"]
        detail['selection_basis'] = ('fixed_slots_target_levels_pending_cost' if selected_plan_ready
            else 'legacy_confirmation_items')
        return decision("upgrade", (519, 1182))

    # The semantic grid title plus the reader's slot/point fields determine
    # readiness; a decorative blue heading must not veto those observations.
    state = (reader or LarcAdaptationReader()).read_grid(image)
    detail.update(state)
    if (grid and state.get('pending_cost') is None and state.get('points') is None
            and 'point_unit_unreadable' in state.get('errors', ())):
        # The labelled point fields themselves are still absent. Wait for the
        # grid content, rather than requiring the decoration to finish fading.
        return {"action": "wait", "reason": "larc_adaptation_grid_loading",
                "route_state": "loading", "evidence": evidence}
    if (session.inspect_only and session.inspect_grid_only and not session.stay_on_grid
            and state.get('pending_cost') == 0 and state.get('errors') == ['points_unreadable']):
        detail['inspection_exit_only'] = True
        detail['balance_not_required_for_return'] = True
        return decision('inspection_complete')
    actionable_errors = list(state["errors"])
    if plan is not None and not session.inspect_only:
        # The balance is needed to make a NEW plan, not to audit clicks or a
        # completed purchase. Tiny balance glyphs often read 30 as 20.
        actionable_errors = adaptation_relevant_errors(actionable_errors, plan)
        changed = [i for i,(start,target) in enumerate(zip(plan["baseline_levels"],plan["targets"])) if target>start]
        if session.stage == "awaiting_result" and any(type(state["levels"][i]) is not int for i in changed):
            return decision("awaiting_result", wait=True)
    if actionable_errors:
        if session.stage == "awaiting_result" and plan and session.submitted_source_sha256:
            # A submitted upgrade can return a blurred/partially painted grid.
            # Keep observing in this bounded route; never re-submit or invent
            # the unreadable values. The next readable grid settles the session.
            return decision("awaiting_result", wait=True)
        selected = state.get("selected_slot")
        if (session.stage == "selecting" and not session.inspect_only
                and isinstance(selected, int)
                and actionable_errors == [f"slot_{selected + 1}_unreadable"]):
            # The game's tap sparkle can briefly cover the selected level.
            # Wait without clicking or changing the purchase plan; the route's
            # existing step budget also bounds a genuinely unreadable level.
            return decision("selected_level_loading", wait=True)
        return decision("unreadable")
    if session.inspect_only:
        if state["pending_cost"] != 0:
            return decision("inspection_pending_selection")
        if session.inspect_grid_only:
            return decision("inspection_complete")
        slot = state["selected_slot"]
        session.inspected_slots[str(slot)] = {
            "slot": slot, "level": state["levels"][slot], "points": state["points"],
            "image": evidence["image"], "source_sha256": hashlib.sha256(io_path(evidence["image"]).read_bytes()).hexdigest(),
            "visible_text": str(evidence.get("screen_text", {}).get("normalized_text", "")),
        }
        detail["inspected_slots"] = sorted(int(key) for key in session.inspected_slots)
        for slot, level in enumerate(state["levels"]):
            if level > 0 and str(slot) not in session.inspected_slots:
                detail["next_slot"] = slot
                x1, y1, x2, y2 = SLOTS[slot]
                return decision("select", ((x1+x2)/2, (y1+y2)/2))
        return decision("inspection_complete")
    if plan is None:
        try:
            plan = build_adaptation_plan(state, phase=session.phase)
        except ValueError:
            return decision("baseline_invalid")
        session.plan = plan
        session.source_sha256 = hashlib.sha256(io_path(evidence["image"]).read_bytes()).hexdigest()
    detail["plan"] = plan
    if plan["cost"] == 0:
        return decision("deferred" if plan["deferred_slots"] else "already_ready")
    levels, pending, points = state["levels"], state["pending_cost"], state["points"]
    if type(pending) is not int:
        count = min(3, session.control_waits.get('selection_cost', 0) + 1)
        session.control_waits['selection_cost'] = count
        detail['selection_cost'] = {'status': 'unknown', 'read_only_observations': count}
        return decision('grid_loading', wait=True) if count <= 2 else decision('selection_cost_unreadable')
    session.control_waits.pop('selection_cost', None)
    target_ready = adaptation_target_levels_ready(levels, plan)
    if session.stage == "awaiting_result" and target_ready and type(pending) is int and pending > 0:
        if pending != plan['cost']:
            return decision('settlement_mismatch')
        blocked = control('review', 'review_unverified', wait_reason='awaiting_result')
        if blocked is not None:
            return blocked
        # Orange preview levels + a pending selection and enabled Review are
        # the game's positive unapplied state, not an unknown transaction.
        # Require a second fresh observation, and allow only one recovery.
        if session.pending_retry_used:
            return decision("submission_not_applied_again")
        session.pending_preview_frames = min(2, session.pending_preview_frames + 1)
        if session.pending_preview_frames < 2:
            return decision("awaiting_result", wait=True)
        session.pending_retry_used = True
        session.stage = "selecting"
        session.selection_ready = True
        detail["submission_recovery"] = {"basis":"current_pending_selection_visible",
            "prior_submitted_source_sha256":session.submitted_source_sha256,"retry_limit":1}
        return decision("review", (360,1078))
    if session.stage in {"awaiting_result", "complete"} or (session.pending_retry_used and session.submitted_source_sha256):
        if target_ready and pending == 0:
            session.stage = "complete"
            session.receipt = {"run_session_id": session.run_session_id, "spent_points": plan["cost"],
                "before_points": plan["baseline_points"], "after_points": points,
                "levels": levels, "pending_cost": 0,
                "first_exchange_ready": all(type(level) is int and level >= 2 for level in levels[:2]),
                "phase": session.phase, "phase_targets_ready": not plan["deferred_slots"],
                "completion_basis": "current_target_levels_pending_zero",
                "spent_points_basis": "selected_upgrade_plan",
                "expected_after_points": plan["baseline_points"] - plan["cost"],
                "point_reconciliation": "diagnostic_only",
                "submitted_source_sha256": session.submitted_source_sha256,
                "result_source_sha256": hashlib.sha256(io_path(evidence["image"]).read_bytes()).hexdigest()}
            detail["receipt"] = session.receipt
            return decision("verified")
        if target_ready and pending == plan["cost"]:
            return decision("awaiting_result", wait=True)
        return decision("settlement_mismatch")
    changed = [i for i,(start,target) in enumerate(zip(plan["baseline_levels"],plan["targets"])) if target>start]
    if ((pending == 0 and any(levels[i] != plan["baseline_levels"][i] for i in changed))
            or any(type(levels[i]) is not int or not plan["baseline_levels"][i] <= levels[i] <= plan["targets"][i]
                for i in changed)):
        count = min(3, session.control_waits.get("untracked_change", 0) + 1)
        session.control_waits["untracked_change"] = count
        detail["untracked_change_wait"] = {"read_only_observations": count}
        return decision("grid_loading", wait=True) if count <= 2 else decision("untracked_change")
    session.control_waits.pop("untracked_change", None)
    if type(pending) is int:
        # The visible selected levels and pending total must describe this
        # plan. Ignore unrelated unreadable slots, never their unknown values.
        selected_levels = [levels[i] if i in changed else start
                           for i, start in enumerate(plan['baseline_levels'])]
        if pending != selected_upgrade_cost(plan['baseline_levels'], selected_levels):
            return decision('selection_cost_mismatch')
    for slot, (level, target) in enumerate(zip(levels, plan["targets"])):
        if slot not in changed:
            continue
        if level >= target:
            continue
        detail["next_slot"] = slot
        if state["selected_slot"] != slot:
            x1, y1, x2, y2 = SLOTS[slot]
            return decision("select", ((x1+x2)/2, (y1+y2)/2))
        blocked = control('level_up', 'level_up_control_unreadable', wait_reason='selected_level_loading')
        if blocked is not None:
            return blocked
        return decision("level_up", (508, 724))
    blocked = control('review', 'review_unverified')
    if blocked is not None:
        return blocked
    session.selection_ready = True
    return decision("review", (360, 1078))
