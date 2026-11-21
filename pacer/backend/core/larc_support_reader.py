"""Read L'Arc training participants; observations never authorize an action."""

from pathlib import Path
from typing import Any
import json

import cv2
import numpy as np

from core.training_menu_precheck import TrainingMenuPrechecker


class LarcSupportReader:
    """Fixed right-lane geometry, including NPCs without support-card badges.

    The default deliberately does not load current_support_deck.json: that
    file may describe another run/scenario. An explicitly bound identity
    matcher can be supplied by a caller with a verified deck.
    """

    def __init__(self, *, identity_matcher: Any | None = None):
        from core.larc_reward_reader import LarcRewardReader
        from core.larc_member_roster import LarcMemberRoster

        self._identity_matcher = identity_matcher
        self._portrait_reader = LarcMemberRoster
        root = Path(__file__).resolve().parents[1] / "assets/larc_support"
        aliases = json.loads((root / "portrait_aliases.json").read_text(encoding="utf-8"))
        self._key_characters = aliases.get("key_characters", {})
        self._portrait_samples = []
        for entry in aliases["examples"]:
            if entry["layout"] == "training":
                sample = cv2.imread(str(root / entry["file"]))
                self._portrait_samples.append((entry["appearance_id"],
                    LarcMemberRoster._portrait(sample, [0, 0, sample.shape[1], sample.shape[0]])))
        self._reward_reader = LarcRewardReader()
        self._prechecker = TrainingMenuPrechecker(scenario="larc")
        maximum = cv2.imread(
            str(Path(__file__).resolve().parents[1] / "assets/larc_support/bond_max.png"),
        )
        self._max_template = None if maximum is None else self._max_text_mask(cv2.cvtColor(maximum, cv2.COLOR_BGR2HSV))
        role_path = Path(__file__).resolve().parents[1] / "assets/larc_support/role_badges.json"
        self._role_badges = []
        for item in json.loads(role_path.read_text(encoding="utf-8")):
            mask = np.array([[int(value) for value in row] for row in item["mask"]], np.uint8)
            self._role_badges.append((item, mask))

    def read(self, image: np.ndarray) -> dict[str, Any]:
        result = {
            "schema_version": "larc_training_participants.v1",
            "status": "abstained",
            "reason": "unsupported_frame",
            "visible_portrait_count": None,
            "participants": [],
            "decision_safe": False,
        }
        if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
            return result
        height, width = image.shape[:2]
        if (width, height) not in {(720, 1280), (1440, 2560)}:
            return result
        scale = width // 720
        canonical = image if scale == 1 else cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(canonical, cv2.COLOR_BGR2HSV)
        # Reuse the existing local layout check on the already loaded image.
        layout_image = image if scale == 2 else cv2.resize(image, (1440, 2560))
        precheck = self._prechecker._check_larc(layout_image, hsv, expected_tab=None, evidence={})
        if not precheck.accepted or precheck.selected_tab == "ss":
            result["reason"] = "not_a_verified_training_page"
            return result

        gray = cv2.cvtColor(canonical, cv2.COLOR_BGR2GRAY)
        lane = cv2.medianBlur(gray[180:760, 590:710], 5)
        circles = cv2.HoughCircles(
            lane, cv2.HOUGH_GRADIENT, dp=1.2, minDist=80,
            param1=80, param2=20, minRadius=30, maxRadius=44,
        )
        candidates = [] if circles is None else circles[0]
        participants = []
        occupied = []
        uncertain_slots = []
        for slot in range(5):
            cy = 242 + 115 * slot
            near = [c for c in candidates if abs(c[0] + 590 - 644) <= 20 and abs(c[1] + 180 - cy) <= 22]
            portrait = gray[cy - 30:cy + 28, 616:672]
            bond = self._read_bond(hsv, slot, scale)
            charge = self._read_charge(hsv, slot, scale)
            patch = hsv[cy - 48:cy + 49, 596:693]
            yy, xx = np.ogrid[-48:49, -48:49]
            radius_squared = xx * xx + yy * yy
            # The white portrait disc, unlike a background lamp, continues
            # around the side/bottom arcs. Exclude the hair/ears at the top.
            arc = (radius_squared >= 41 ** 2) & (radius_squared <= 47 ** 2) & (yy >= -20)
            white = (patch[:, :, 1] < 60) & (patch[:, :, 2] > 200)
            # Friendship glow can replace the white rim; a verified bar/panel
            # anchors that portrait independently. A lamp has neither.
            has_ui_anchor = bond["status"] == "accepted" or charge["status"] == "accepted"
            rim_visible = float(white[arc].mean()) >= .20
            # A nearby background sign can steal Hough's circle peak. The
            # fixed portrait rim plus its charge/bond UI is an alternate anchor.
            anchored_rim = rim_visible and (has_ui_anchor or charge["status"] == "abstained")
            if (not near and not anchored_rim) or float(np.std(portrait)) < 20 or (not rim_visible and not has_ui_anchor):
                if bond["status"] == "accepted" or charge["status"] != "not_visible":
                    uncertain_slots.append(slot)
                continue
            occupied.append(slot)
            bbox = [598, cy - 48, 691, cy + 48]
            source_bbox = [value * scale for value in bbox]
            identity = {
                "status": "unknown", "matched_card_id": None,
                "character_id": "unknown", "character_name": "unknown",
                "reason": "no_verified_larc_identity_matcher",
            }
            if self._identity_matcher is not None:
                identity = self._identity_matcher.match_slot(
                    image=image, bbox=source_bbox, slot_index=slot,
                ).to_dict()
            if identity.get("status") != "known":
                identity = self._read_key_character(image, source_bbox) or identity
            reward = self._reward_reader.read_training(
                canonical, slot, panel_visible=charge["status"] != "not_visible")
            key_character = self._key_characters.get(identity.get("appearance_id"), {})
            if (identity.get("status") == "known" and reward.get("status") == "accepted"
                    and reward.get("kind") == "positive_condition" and not reward.get("earned")
                    and key_character.get("positive_condition_kind")):
                reward = dict(reward, icon_kind="positive_condition",
                              kind=key_character["positive_condition_kind"],
                              reason="current_portrait_and_condition_icon")
            participants.append({
                "slot": slot, "bbox": source_bbox, "identity": identity,
                "role": self._read_role(hsv, slot, scale),
                "bond": bond, "charge": charge,
                "training_hint": self._read_training_hint(hsv, slot, scale),
                "ss_reward": reward,
            })
        # A hole cannot be silently collapsed into a shorter, renumbered list.
        if uncertain_slots or occupied != list(range(len(occupied))):
            result.update(reason="unresolved_portrait_slots", participants=participants,
                          training_type=precheck.selected_tab,
                          uncertain_slots=uncertain_slots)
            return result
        result.update(
            status="accepted", reason="fixed_training_lane",
            visible_portrait_count=len(participants), participants=participants,
            training_type=precheck.selected_tab,
            summary=summarize_participants(participants),
        )
        return result

    def _read_key_character(self, image, bbox):
        # Match the current face, not the roster's previously associated name.
        # Rivals keep a similar face from being forced into the one named class.
        portrait = self._portrait_reader._portrait(image, bbox)
        if portrait is None:
            return None
        ranked = sorted((self._portrait_reader._similarity(portrait, sample), alias)
                        for alias, sample in self._portrait_samples)
        score, alias = ranked[-1]
        if (score < .86 or score - ranked[-2][0] < .06
                or alias not in self._key_characters):
            return None
        character = self._key_characters[alias]
        return {"status": "known", "matched_card_id": None, "entity_kind": "character",
                "character_id": character["character_id"], "character_name": character["character_name"],
                "appearance_id": alias, "confidence": round(score, 4),
                "reason": "current_frame_portrait_template"}

    def _read_role(self, hsv, slot, scale):
        # A type badge is run-independent evidence of card role, not identity.
        # Bond bars alone also occur on the chairman/reporter and prove no role.
        x1, y1, x2, y2 = 596, 193 + slot * 115, 626, 225 + slot * 115
        patch = hsv[y1:y2, x1:x2]
        white = ((patch[:, :, 1] < 70) & (patch[:, :, 2] > 205)).astype(np.uint8)
        result = {"status": "unknown", "kind": None, "icon_type": None,
                  "reason": "no_verified_card_type_badge",
                  "source": {"bbox": [v * scale for v in (x1, y1, x2, y2)]}}
        matches = []
        for item, template in self._role_badges:
            _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(white, template, cv2.TM_CCOEFF_NORMED))
            if score < 0.80:
                continue
            h, w = template.shape
            badge = patch[y:y+h, x:x+w]
            saturated = (badge[:, :, 1] > 100) & (badge[:, :, 2] > 170)
            hue_delta = np.abs(badge[:, :, 0].astype(int) - item["hue"])
            same_hue = np.minimum(hue_delta, 180 - hue_delta) <= 9
            if saturated.mean() < 0.25 or float(same_hue[saturated].mean()) < 0.90:
                continue
            matches.append((score, item, [v * scale for v in (x1+x, y1+y, x1+x+w, y1+y+h)]))
        if not matches:
            return result
        score, item, bbox = max(matches, key=lambda match: match[0])
        return dict(result, status="accepted", kind=item["role"], icon_type=item["icon"],
                    reason="visible_card_type_badge", confidence=round(float(score), 4),
                    source={"bbox": bbox, "template": item["icon"]})

    @staticmethod
    def _read_training_hint(hsv, slot, scale):
        # Only the badge above this portrait. SS reward bubbles and the large
        # red badge on the bottom training button are different observations.
        x1, y1, x2, y2 = 657, 185 + slot*115, 710, 237 + slot*115
        patch = hsv[y1:y2, x1:x2]
        pink = ((patch[:, :, 0] >= 145) & (patch[:, :, 0] <= 178)
                & (patch[:, :, 1] > 100) & (patch[:, :, 2] > 170)).astype(np.uint8)
        result = {"status": "accepted", "present": False, "reason": "badge_absent",
                  "bbox": [v*scale for v in (x1, y1, x2, y2)]}
        count, _, components, _ = cv2.connectedComponentsWithStats(pink, 8)
        for x, y, w, h, area in components[1:count]:
            if area < 200 or not 24 <= w <= 49 or not 24 <= h <= 49:
                continue
            badge = patch[y:y+h, x:x+w]
            white = ((badge[:, :, 1] < 65) & (badge[:, :, 2] > 205)).astype(np.uint8)
            n, _, parts, _ = cv2.connectedComponentsWithStats(white, 8)
            stems, dots = [], []
            for a, b, c, d, size in parts[1:n]:
                if 3 <= c <= 16 and 10 <= d <= 29 and d >= c*1.2 and size >= 30:
                    stems.append((a, b, c, d))
                elif 2 <= c <= 12 and 2 <= d <= 11 and size >= 5:
                    dots.append((a, b, c, d))
            if any(abs((a+c/2)-(e+g/2)) <= 8 and b+d-2 <= f <= b+d+9
                   for a, b, c, d in stems for e, f, g, _ in dots):
                return dict(result, present=True, reason="pink_badge_with_white_exclamation",
                            bbox=[v*scale for v in (x1+int(x), y1+int(y), x1+int(x+w), y1+int(y+h))])
            result.update(status="unknown", present=None, reason="pink_badge_glyph_uncertain")
        return result

    @staticmethod
    def _max_text_mask(hsv: np.ndarray) -> np.ndarray:
        # Brown/gold glyph strokes, excluding the portrait and pale bar fill.
        return ((hsv[:, :, 0] >= 12) & (hsv[:, :, 0] <= 35)
                & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] < 235)).astype(np.uint8)

    def _read_bond(self, hsv: np.ndarray, slot: int, scale: int) -> dict[str, Any]:
        offset = slot * 115
        result = {
            "status": "not_visible", "reason": "bond_bar_absent",
            "fill_ratio_estimate": None, "color": None, "is_max": None,
            "bbox": [v * scale for v in (599, 274 + offset, 684, 295 + offset)],
        }
        # Both dark rounded end caps are independent of fill colour or MAX text.
        caps = hsv[281 + offset:286 + offset, [601, 681]]
        if not bool(np.all(np.median(caps[:, :, 2], axis=0) < 155)):
            return result
        if not bool(np.all(np.median(caps[:, :, 1], axis=0) < 65)):
            return result
        if self._max_template is not None:
            patch = self._max_text_mask(hsv[268 + offset:299 + offset, 615:672])
            score = float(cv2.matchTemplate(patch, self._max_template, cv2.TM_CCOEFF_NORMED).max())
            if score >= 0.70:
                return dict(result, status="accepted", reason="max_label", fill_ratio_estimate=1.0,
                            color="yellow", is_max=True)
        # The 115px pitch rounds alternating outlines one pixel up/down.
        for y in (276, 290):
            edge = hsv[y - 1 + offset:y + 2 + offset, 605:678]
            white_edge = (edge[:, :, 1] < 60) & (edge[:, :, 2] > 200)
            if float(white_edge.mean(axis=1).max()) < 0.60:
                return result
        inner = hsv[280 + offset:287 + offset, 604:679]
        columns = np.median(inner, axis=0)
        colored = (columns[:, 1] > 65) & (columns[:, 2] > 160)
        # One/two-pixel tick marks divide a continuous fill; they are not empty bond.
        filled = cv2.morphologyEx(colored.astype(np.uint8)[None, :], cv2.MORPH_CLOSE, np.ones((1, 3), np.uint8))[0] > 0
        if not filled.any():
            empty = (columns[:, 1] < 65) & (columns[:, 2] < 160)
            if float(empty.mean()) < 0.85:
                return dict(result, status="abstained", reason="bond_pixels_unknown")
            return dict(result, status="accepted", reason="empty_bar", fill_ratio_estimate=0.0,
                        color="empty", is_max=False)
        end = int(np.flatnonzero(filled)[-1]) + 1
        if not filled[:3].any() or float(filled[:end].mean()) < 0.90:
            return dict(result, status="abstained", reason="non_prefix_bond_fill")
        hue = float(np.median(columns[colored, 0]))
        color = "cyan" if 80 <= hue <= 110 else "green" if 32 <= hue < 80 else "orange" if 8 <= hue < 32 else None
        if color is None:
            return dict(result, status="abstained", reason="bond_colour_unknown")
        return dict(result, status="accepted", reason="visible_fill_length", color=color,
                    fill_ratio_estimate=round(end / len(filled), 2), is_max=False)

    @staticmethod
    def _read_charge(hsv: np.ndarray, slot: int, scale: int) -> dict[str, Any]:
        offset = slot * 115
        result = {
            "status": "not_visible", "reason": "charge_panel_absent",
            "capacity": None, "filled_cells": None, "preview_cells": None,
            "is_full": None, "cell_states_bottom_to_top": [],
            "bbox": [v * scale for v in (545, 222 + offset, 597, 284 + offset)],
        }
        border = hsv[227 + offset:260 + offset, 545:549]
        footer = hsv[265 + offset:282 + offset, 550:591]
        def blue_ratio(patch):
            return float(np.mean((patch[:, :, 0] >= 108) & (patch[:, :, 0] <= 125)
                                 & (patch[:, :, 1] > 120) & (patch[:, :, 2] > 170)))
        if blue_ratio(border) < 0.55 or blue_ratio(footer) < 0.40:
            return result
        states = []
        for y1, y2 in ((250, 259), (238, 247), (226, 235)):
            patch = hsv[y1 + offset:y2 + offset, 555:586]
            h, s, v = np.median(patch, axis=(0, 1))
            if 8 <= h <= 40 and s >= 140 and v >= 235:
                state = "filled"
            elif 20 <= h <= 42 and 25 <= s < 140 and 110 <= v < 235:
                state = "preview"
            elif s < 55 and 40 <= v < 110:
                state = "empty"
            else:
                state = "unknown"
            states.append(state)
        result.update(capacity=3, cell_states_bottom_to_top=states)
        order = {"filled": 0, "preview": 1, "empty": 2}
        if "unknown" in states or [order[s] for s in states] != sorted(order[s] for s in states):
            return dict(result, status="abstained", reason="charge_cells_uncertain")
        return dict(result, status="accepted", reason="visible_cells",
                    filled_cells=states.count("filled"), preview_cells=states.count("preview"),
                    is_full=states.count("filled") == 3)


def summarize_participants(participants):
    """Measured counts, not a weighted bond value or inferred card identity."""
    hints = [p["training_hint"] for p in participants]
    bonds = [p["bond"] for p in participants]
    charges = [p["charge"] for p in participants if p["charge"]["status"] != "not_visible"]
    hints_known = all(h["status"] == "accepted" for h in hints)
    bonds_known = all(b["status"] in {"accepted", "not_visible"} for b in bonds)
    charges_known = all(c["status"] == "accepted" for c in charges)
    return {
        "head_count": len(participants),
        "training_hint_count": sum(h["present"] for h in hints) if hints_known else None,
        "bond_visible_count": sum(b["status"] == "accepted" for b in bonds) if bonds_known else None,
        "bond_not_max_count": sum(b["is_max"] is False for b in bonds) if bonds_known else None,
        "bond_below_friendship_color_count": sum(b["color"] in {"empty", "cyan", "green"} for b in bonds) if bonds_known else None,
        "hint_with_bond_count": sum(h["present"] and b["status"] == "accepted" for h, b in zip(hints, bonds)) if hints_known and bonds_known else None,
        "hint_with_not_max_bond_count": sum(h["present"] and b["is_max"] is False for h, b in zip(hints, bonds)) if hints_known and bonds_known else None,
        "effective_bond_count": None,
        "effective_bond_reason": "requires_policy_target_and_member_role",
        "charge_participant_count": len(charges),
        "charge_filled_cells": sum(c["filled_cells"] for c in charges) if charges_known else None,
        "charge_preview_cells": sum(c["preview_cells"] for c in charges) if charges_known else None,
        "charge_remaining_capacity": sum(c["capacity"]-c["filled_cells"] for c in charges) if charges_known else None,
        "full_charge_count": sum(c["is_full"] for c in charges) if charges_known else None,
    }
