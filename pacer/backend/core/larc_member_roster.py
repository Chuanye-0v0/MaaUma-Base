"""Run-local portrait associations. This is observation state, not a policy."""
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from core.larc_reward_cycle import advance_cycle, align_cycle, cycle_window, observe_cycle


class LarcMemberRoster:
    def __init__(self, run_session_id: str):
        if not isinstance(run_session_id, str) or not run_session_id.strip():
            raise ValueError("larc_roster_run_session_missing")
        self.run_session_id = run_session_id
        self._members: list[dict[str, Any]] = []
        self._portraits: dict[tuple[str, str], np.ndarray] = {}
        self._last_turn = -1
        self._information_scan = {"status": "never_scanned"}
        self._ss_executions: list[str] = []
        self._last_ss_preview = None
        self._ss_execution_participants: list[dict[str, Any]] = []
        root = Path(__file__).resolve().parents[1] / "assets/larc_support"
        aliases = json.loads((root / "portrait_aliases.json").read_text(encoding="utf-8"))
        self._aliases = []
        for entry in aliases["examples"]:
            image = cv2.imread(str(root / entry["file"]))
            self._aliases.append((entry["appearance_id"], entry["layout"],
                                  self._portrait(image,[0,0,image.shape[1],image.shape[0]])))

    @staticmethod
    def _portrait(image, bbox):
        x1, y1, x2, y2 = bbox
        crop = image[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        crop = cv2.resize(crop, (64, 64), interpolation=cv2.INTER_AREA)
        # Keep the face, not support-type badges, bond bars or friendship rings.
        return crop[16:52, 16:48].copy()

    @staticmethod
    def _similarity(left, right):
        # Small translations tolerate one-pixel frame rounding. Colour retains
        # distinctions between similar hairstyles without loading a model.
        template = right[2:-2, 2:-2]
        scores = cv2.matchTemplate(left, template, cv2.TM_CCOEFF_NORMED)
        grey_left = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
        grey_right = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)
        shape = cv2.matchTemplate(grey_left, grey_right[2:-2, 2:-2], cv2.TM_CCOEFF_NORMED)
        return float((scores * np.maximum(shape, 0)).max())

    def _check_source(self, source):
        if source.get("run_session_id") != self.run_session_id:
            raise ValueError("larc_roster_run_session_mismatch")
        turn = source.get("turn_index")
        if type(turn) is not int or turn < self._last_turn:
            raise ValueError("larc_roster_stale_or_missing_turn")
        return turn

    @property
    def needs_ss_information(self):
        return (self._information_scan["status"] != "completed"
                or self._information_scan.get("association_schema_version") != "training_group.v1")

    @property
    def reward_cycles_initialized(self):
        return (self._information_scan.get("cycle_initialization_attempted") is True
                or any(member.get("reward_cycle") is not None for member in self._members))

    def mark_cycle_initialization_attempted(self):
        self._information_scan["cycle_initialization_attempted"] = True

    def mark_information_scan_complete(self, *, source):
        turn = self._check_source(source)
        self._information_scan = {"status": "completed", "source": deepcopy(source),
                                  "association_schema_version": "training_group.v1"}
        self._last_turn = turn

    def record_ss_execution(self, receipt):
        if receipt.get("run_session_id") != self.run_session_id:
            raise ValueError("larc_roster_run_session_mismatch")
        sha = receipt.get("source_sha256")
        if (receipt.get("action_id") != "safe_advance.larc_ss_confirm"
                or receipt.get("tap_performed") is not True
                or type(receipt.get("turn_index")) is not int or receipt["turn_index"] < 0
                or not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None):
            raise ValueError("larc_ss_execution_receipt_invalid")
        if sha in self._ss_executions:
            linked = next((entry for entry in self._ss_execution_participants
                           if entry["source_sha256"] == sha), None)
            if linked is not None and linked["receipt"]["turn_index"] != receipt["turn_index"]:
                raise ValueError("larc_ss_preview_receipt_mismatch")
            return False
        turn = self._check_source(receipt)
        initialized = self.reward_cycles_initialized
        preview = self._last_ss_preview
        selected = None
        if preview is not None and preview["source"]["source_sha256"] not in self._ss_executions:
            if (preview["source"]["source_sha256"] != sha
                    or preview["source"]["turn_index"] != turn):
                raise ValueError("larc_ss_preview_receipt_mismatch")
            self._ss_execution_participants.append({
                "source_sha256": sha, "receipt": deepcopy(receipt),
                "participants_observation": deepcopy(preview),
            })
            rows = preview.get("participants", [])
            if (preview.get("status") == "accepted" and rows and all(
                    row.get("member_ref", {}).get("status") == "accepted" for row in rows)):
                selected = {row["member_ref"]["member_id"]: row for row in rows}
        self._ss_executions.append(sha)
        tracked = set()
        for member in self._members:
            if selected is not None and member["member_id"] not in selected:
                continue  # Full but unselected members retain their reward position.
            cycle = member.get("reward_cycle")
            row = (selected or {}).get(member["member_id"], {})
            if cycle is not None:
                member["reward_cycle"] = advance_cycle(
                    cycle, row.get("current_reward", {}), receipt,
                    guaranteed=row.get("win_guaranteed") is True)
                if cycle_window(member["reward_cycle"]) is not None:
                    tracked.add(member["member_id"])
            if member["reward_preview"].get("upcoming") is not None:
                member["reward_preview"]["freshness"] = "stale_after_ss_execution"
        if selected is None or tracked != set(selected):
            self._information_scan = {"status": "refresh_required", "reason": "ss_execution_observed",
                                      "source": deepcopy(receipt), "cycle_initialization_attempted": initialized}
        self._last_turn = turn
        return True

    def _visual_alias(self, portrait, layout):
        scores = {}
        for alias, example_layout, sample in self._aliases:
            if layout == example_layout:
                scores[alias] = max(scores.get(alias,0), self._similarity(portrait,sample))
        ranked = sorted(((score,alias) for alias,score in scores.items()),reverse=True)
        if ranked and ranked[0][0] >= 0.86 and (len(ranked)==1 or ranked[0][0]-ranked[1][0] >= 0.06):
            return ranked[0][1]
        return None

    def _remember_portrait(self, member, portrait, bbox, source, layout):
        key = (member["member_id"], layout)
        if key not in self._portraits:
            self._portraits[key] = portrait
            primary = member["portrait_source"]
            member.setdefault("portrait_sources", {primary["layout"]: deepcopy(primary)})[layout] = {
                "absolute_path": source["absolute_path"], "bbox": list(bbox), "layout": layout,
            }

    def _associate(self, image, bbox, source, used, *, layout, position=None):
        portrait = self._portrait(image, bbox)
        if portrait is None or float(portrait.std()) < 12:
            return None, "portrait_unreadable"
        alias = self._visual_alias(portrait,layout)
        # The information list is explicitly grouped by the current training
        # page, in portrait order, excluding portraits without an SS gauge.
        # A dated group is only a same-turn bridge between two different arts;
        # later turns must match an observed portrait, never the old position.
        grouped = [m for m in self._members if position is not None
                   and m.get("training_position") == position
                   and m["reward_preview"].get("freshness") != "stale_after_ss_execution"]
        if len(grouped) == 1:
            member = grouped[0]
            if member["member_id"] in used:
                return None, "duplicate_identity_in_one_page"
            if alias is not None and member.get("appearance_id") not in {None, alias}:
                return None, "training_group_alias_conflict"
            sample = self._portraits.get((member["member_id"], layout))
            if sample is not None and self._similarity(portrait, sample) < 0.65:
                return None, "training_group_portrait_conflict"
            if sample is None:
                independently_matched = alias is not None and member.get("appearance_id") == alias
                member["layout_association"] = {
                    "status": "accepted" if independently_matched else "provisional",
                    "basis": "verified_visual_alias" if independently_matched else "same_turn_training_group",
                    "source": deepcopy(source), "position": deepcopy(position),
                }
            self._remember_portrait(member, portrait, bbox, source, layout)
            if alias is not None:
                member["appearance_id"] = alias
            return member, "same_turn_training_group"
        if alias is not None:
            known = next((m for m in self._members if m.get("appearance_id") == alias),None)
            if known is not None:
                if known["member_id"] in used:
                    return None,"duplicate_identity_in_one_page"
                if (known["member_id"], layout) not in self._portraits:
                    known["layout_association"] = {
                        "status": "accepted", "basis": "verified_visual_alias",
                        "source": deepcopy(source), "appearance_id": alias,
                    }
                self._remember_portrait(known, portrait, bbox, source, layout)
                return known,"verified_visual_alias"
        comparable = {m["member_id"] for m in self._members
                      if alias is None or m.get("appearance_id") in {None,alias}}
        ranked = sorted(((self._similarity(portrait, sample), member_id)
                         for (member_id, sample_layout), sample in self._portraits.items()
                         if member_id in comparable and sample_layout == layout), reverse=True)
        best, member_id = ranked[0] if ranked else (0.0, None)
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        if best >= 0.86 and best - runner_up >= 0.06:
            if member_id in used:
                return None, "duplicate_identity_in_one_page"
            member = next(m for m in self._members if m["member_id"] == member_id)
            if alias is not None:
                member["appearance_id"] = alias
            return member, "matched_run_portrait"
        if best >= 0.65:
            return None, "portrait_match_ambiguous"
        member_id = f"member_{len(self._members) + 1:02d}"
        member = {
            "member_id": member_id, "character_id": None, "character_name": None,
            "appearance_id": alias,
            "member_kind": "unknown", "matched_card_id": None,
            "portrait_source": {"absolute_path": source["absolute_path"], "bbox": list(bbox), "layout":layout},
            "reward_preview": {"current": None, "upcoming": None, "status": "unknown"},
        }
        self._members.append(member)
        self._remember_portrait(member, portrait, bbox, source, layout)
        return member, "new_run_portrait"

    def observe_training(self, image, observation, *, source):
        turn = self._check_source(source)
        result = deepcopy(observation)
        if result.get("status") != "accepted":
            if result.get("reason") == "unresolved_portrait_slots":
                result["source"] = deepcopy(source)
            return result
        used = set()
        charge_index = 0
        positions_known = all(p["charge"]["status"] in {"accepted", "not_visible"} for p in result["participants"])
        for participant in result["participants"]:
            participant.pop("ss_reward_cycle", None)
            position = None
            if participant["charge"]["status"] != "not_visible":
                if positions_known and result.get("training_type") == source.get("tab"):
                    position = {"turn_index": turn, "training_type": source["tab"], "member_index": charge_index}
                charge_index += 1
            member, reason = self._associate(image, participant["bbox"], source, used, layout="training", position=position)
            participant["member_ref"] = {
                "status": "accepted" if member is not None else "unknown",
                "run_session_id": self.run_session_id,
                "member_id": member["member_id"] if member else None,
                "identity_scope": "run_visual_alias" if member and member.get("appearance_id") else "run_layout_cluster",
            }
            if member is None:
                participant["member_ref"]["reason"] = reason
                continue
            participant["member_match_reason"] = reason
            used.add(member["member_id"])
            if position is not None:
                member["training_position"] = position
            identity = participant.get("identity", {})
            if identity.get("status") == "known":
                member.update(character_id=identity.get("character_id"),
                              character_name=identity.get("character_name"),
                              matched_card_id=identity.get("matched_card_id"))
                member["member_kind"] = "support" if member["matched_card_id"] else "unknown"
            member["last_seen"] = deepcopy(source)
            if participant.get("role", {}).get("status") == "accepted":
                member["training_role"] = deepcopy(participant["role"])
            if participant.get("bond", {}).get("status") == "accepted":
                member["training_bond"] = deepcopy(participant["bond"])
                member["training_bond_turn"] = turn
            # Preview increments belong to the candidate page, never inventory.
            member["observed_charge"] = {
                "filled_cells": participant["charge"].get("filled_cells"),
                "status": participant["charge"].get("status"), "turn_index": turn,
            }
            current = deepcopy(participant.get("ss_reward"))
            if member.get("reward_cycle") is not None:
                member["reward_cycle"] = align_cycle(member["reward_cycle"], current, source)
                participant["ss_reward_cycle"] = deepcopy(member["reward_cycle"])
                participant["ss_reward_cycle"]["association"] = deepcopy(member.get(
                    "layout_association", {"status": "unknown"}))
            if member["reward_preview"]["upcoming"] is None:
                member["reward_preview"] = {
                    "current": current, "upcoming": None, "status": "current_only",
                    "reason": "future_rewards_require_fresh_ss_information",
                    "source": deepcopy(source),
                }
            else:
                # Keep the dated information-page observation, not a fabricated
                # rolling cycle. A training-page visit cannot advance rewards.
                participant["ss_information_preview"] = deepcopy(member["reward_preview"])
                participant["ss_information_preview"]["freshness"] = (
                    "stale_after_ss_execution" if member["reward_preview"].get("freshness") == "stale_after_ss_execution"
                    else "same_turn" if member["reward_preview"]["source"]["turn_index"] == turn else "historical"
                )
                # Re-seeing a template learned solely through row order is
                # not an independent proof of that initial correspondence.
                participant["ss_information_preview"]["association"] = deepcopy(member.get(
                    "layout_association", {"status": "unknown", "basis": "layout_evidence_missing"},
                ))
            member["training_current_reward"] = current
        self._last_turn = turn
        result["ss_information_linked_count"] = sum("ss_information_preview" in p for p in result["participants"])
        result["ss_information_provisional_count"] = sum(
            p.get("ss_information_preview", {}).get("association", {}).get("status") == "provisional"
            for p in result["participants"]
        )
        return result

    def observe_information(self, image, observation, *, source):
        turn = self._check_source(source)
        result = deepcopy(observation)
        if result.get("status") != "accepted":
            return result
        used = set()
        for row in result["members"]:
            position = None
            if row.get("training_type") and type(row.get("training_member_index")) is int:
                position = {"turn_index": turn, "training_type": row["training_type"],
                            "member_index": row["training_member_index"]}
            member,reason = self._associate(image,row["bbox"],source,used,layout="ss_information",position=position)
            row["member_ref"] = {"status":"accepted" if member else "unknown",
                                 "run_session_id":self.run_session_id,
                                 "member_id":member["member_id"] if member else None,
                                 "identity_scope":"run_visual_alias" if member and member.get("appearance_id") else "run_layout_cluster"}
            if member is None:
                row["member_ref"]["reason"] = reason
                continue
            used.add(member["member_id"])
            if position is not None:
                member["training_position"] = position
            member["last_seen"] = deepcopy(source)
            member["reward_preview"] = {
                "current":deepcopy(row["current"]),"upcoming":deepcopy(row["upcoming"]),
                "status":"observed_information", "source":deepcopy(source),
            }
            cycle = observe_cycle(row["current"], row["upcoming"], source, member.get("reward_cycle"))
            if cycle is not None:
                member["reward_cycle"] = cycle
        self._last_turn = turn
        return result

    def observe_ss_preview(self, image, observation, *, source):
        turn = self._check_source(source)
        sha = source.get("source_sha256")
        path = Path(source.get("absolute_path", ""))
        if (turn < 0 or not path.is_absolute() or not path.is_file()
                or not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None
                or hashlib.sha256(path.read_bytes()).hexdigest() != sha):
            raise ValueError("larc_ss_preview_source_invalid")
        original = cv2.imread(str(path))
        if original is None or not np.array_equal(original, image):
            raise ValueError("larc_ss_preview_source_image_mismatch")
        result = deepcopy(observation)
        result.update(source=deepcopy(source), decision_safe=False,
                      matched_information_count=0, unknown_count=len(result.get("participants", [])))
        if (result.get("schema_version") != "larc_ss_participants.v1"
                or result.get("layout", "ss_preview") != "ss_preview"):
            result.update(status="abstained", reason="unsupported_preview_observation")
        result["layout"] = "ss_preview"
        if result.get("status") == "accepted" and (
                result.get("expected_count") != len(result.get("participants", []))
                or result.get("visible_portrait_count") != len(result.get("participants", []))):
            result.update(status="abstained", reason="preview_participant_count_mismatch")
        accepted = result.get("status") == "accepted"
        # Keep the visual reader's verdict before historical identity matching.
        # Unknown portrait associations must not erase visible current rewards.
        boxes = [tuple(row.get("bbox", [])) for row in result.get("participants", [])]
        result["current_frame"] = {
            "status": "accepted" if accepted and len(set(boxes)) == len(boxes) else "abstained",
            "expected_count": result.get("expected_count"),
            "visible_portrait_count": result.get("visible_portrait_count"),
            "basis": "fresh_preview_reader_before_roster_association",
        }
        used = set()
        for row in result.get("participants", []):
            member, reason = (self._associate(image, row["bbox"], source, used, layout="ss_preview")
                              if accepted else (None, "preview_observation_abstained"))
            row.pop("ss_information_preview", None)
            row.pop("ss_reward_cycle", None)
            row["member_ref"] = {
                "status": "accepted" if member else "unknown", "run_session_id": self.run_session_id,
                "member_id": member["member_id"] if member else None,
                "identity_scope": "run_visual_alias" if member and member.get("appearance_id") else "run_layout_cluster",
                "reason": reason,
            }
            row["information_association"] = {"status": "unknown", "basis": "no_independent_information_alias"}
            if member is None:
                if accepted:
                    result.update(status="abstained", reason=reason)
                continue
            used.add(member["member_id"])
            member["last_seen"] = deepcopy(source)
            if "training_role" in member:
                row["role"] = deepcopy(member["training_role"])
            if (row.get("bond", {}).get("status") != "accepted"
                    and member.get("training_bond_turn") == turn and "training_bond" in member):
                row["bond"] = deepcopy(member["training_bond"])
            # Neither full charge nor row order bridges the preview layout.
            # Both portrait layouts must independently identify the same alias.
            alias = self._visual_alias(self._portraits[(member["member_id"], "ss_preview")], "ss_preview")
            info_portrait = self._portraits.get((member["member_id"], "ss_information"))
            reward = member["reward_preview"]
            if (alias is not None and info_portrait is not None
                    and self._visual_alias(info_portrait, "ss_information") == alias
                    and reward.get("status") == "observed_information"):
                row["information_association"] = {"status": "accepted", "basis": "verified_visual_alias",
                                                  "appearance_id": alias}
                row["ss_information_preview"] = deepcopy(reward)
                row["ss_information_preview"]["freshness"] = (
                    "stale_after_ss_execution" if reward.get("freshness") == "stale_after_ss_execution"
                    else "same_turn" if reward["source"]["turn_index"] == turn else "historical")
                result["matched_information_count"] += 1
            if member.get("reward_cycle") is not None:
                member["reward_cycle"] = align_cycle(member["reward_cycle"], row.get("current_reward"), source)
                row["ss_reward_cycle"] = deepcopy(member["reward_cycle"])
                row["ss_reward_cycle"]["association"] = deepcopy(row["information_association"])
        result["unknown_count"] -= result["matched_information_count"]
        self._last_ss_preview = deepcopy(result)
        self._last_turn = turn
        return result

    def to_dict(self):
        return {"schema_version": "larc_member_roster.v1", "run_session_id": self.run_session_id,
                "last_observed_turn": self._last_turn, "members": deepcopy(self._members),
                "information_scan": deepcopy(self._information_scan), "ss_executions": list(self._ss_executions),
                "last_ss_preview": deepcopy(self._last_ss_preview),
                "ss_execution_participants": deepcopy(self._ss_execution_participants),
                "complete_roster_verified": False,
                "decision_safe": False}

    @classmethod
    def from_dict(cls, payload, *, run_session_id):
        if payload.get("run_session_id") != run_session_id:
            raise ValueError("larc_roster_run_session_mismatch")
        if payload.get("schema_version") != "larc_member_roster.v1":
            raise ValueError("larc_roster_schema_invalid")
        result = cls(run_session_id)
        result._members = deepcopy(payload["members"])
        result._last_turn = payload["last_observed_turn"]
        result._information_scan = deepcopy(payload.get("information_scan", {"status":"never_scanned"}))
        result._ss_executions = list(payload.get("ss_executions", []))
        result._last_ss_preview = deepcopy(payload.get("last_ss_preview"))
        result._ss_execution_participants = deepcopy(payload.get("ss_execution_participants", []))
        for member in result._members:
            sources = member.get("portrait_sources") or {member["portrait_source"]["layout"]: member["portrait_source"]}
            for layout, source in sources.items():
                image = cv2.imread(source["absolute_path"])
                if image is None:
                    raise ValueError("larc_roster_portrait_source_missing")
                result._portraits[(member["member_id"], layout)] = cls._portrait(image, source["bbox"])
        return result

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
