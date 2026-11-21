"""Offline skill knowledge shared by daily-task and career recommendation callers."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
import unicodedata
from pathlib import Path
from typing import Mapping

from core.skill_conditions import evaluate_condition, describe_condition

DEFAULT_LIBRARY = Path(__file__).resolve().parents[1] / "data/skills/cn_larc_end/cn_skill_master.json"
HINT_DISCOUNTS = {0: 0, 1: 10, 2: 20, 3: 30, 4: 35, 5: 40}
STYLE_ENUM = {"leader": 1, "front": 2, "betweener": 3, "chaser": 4,
              "领跑": 1, "跟前": 2, "居中": 3, "后追": 4}
_NAME_PUNCTUATION = frozenset('!?,.。、;:「」『』“”‘’"\'…')


def skill_name_key(value: str, *, ignore_punctuation: bool = True) -> str:
    """Normalize user-entered names without dropping grade/math symbols.

    This is deterministic spelling normalization, not substring or typo
    guessing. In particular ○, ◎, ×, +, -, / and stars keep their meaning.
    """
    text=unicodedata.normalize('NFKC',value).casefold()
    return ''.join(c for c in text if not c.isspace()
                   and not (ignore_punctuation and c in _NAME_PUNCTUATION))
DISTANCE_ENUM = {"short": 1, "mile": 2, "middle": 3, "long": 4}


def race_inputs(target: dict) -> dict:
    """Static course facts only; dynamic race variables must be supplied separately."""
    result = {"always": 1}
    if target.get("style") is not None:
        if target["style"] not in STYLE_ENUM:
            raise ValueError("race_target_style_invalid")
        result["running_style"] = STYLE_ENUM[target["style"]]
    distance = target.get("distance_m")
    if distance is not None:
        if type(distance) is not int or distance <= 0:
            raise ValueError("race_target_distance_invalid")
        result["course_distance"] = distance
        result["distance_type"] = 1 if distance < 1600 else 2 if distance <= 1800 else 3 if distance <= 2400 else 4
    if target.get("distance_type"):
        value = DISTANCE_ENUM.get(target["distance_type"])
        if value is None or ("distance_type" in result and result["distance_type"] != value):
            raise ValueError("race_target_distance_type_invalid")
        result["distance_type"] = value
    if target.get("surface") is not None:
        if target["surface"] not in {"turf", "dirt", "草地", "泥地"}:
            raise ValueError("race_target_surface_invalid")
        result["ground_type"] = 1 if target["surface"] in {"turf", "草地"} else 2
    for key in ("track_id", "ground_condition", "season", "weather", "rotation"):
        if target.get(key) is not None:
            if type(target[key]) is not int:
                raise ValueError(f"race_target_{key}_invalid")
            result[key] = target[key]
    return result


class SkillKnowledgeLibrary:
    def __init__(self, payload: dict):
        if payload.get("schema_version") != "uma.cn_larc_skill_library.v1":
            raise ValueError("skill_library_schema_invalid")
        self.metadata = {k: v for k, v in payload.items() if k != "skills"}
        self.rows = {}
        self.by_db = {}
        self.by_name = defaultdict(list)
        self.by_name_surface = defaultdict(dict)
        self.by_name_punctuation = defaultdict(dict)
        for row in payload["skills"]:
            if row["skill_id"] in self.rows or row["db_id"] in self.by_db:
                raise ValueError("skill_library_duplicate_id")
            self.rows[row["skill_id"]] = row
            self.by_db[row["db_id"]] = row
            for name in {row["name_zh_cn"], row.get("name_ja")} - {None, ""}:
                self.by_name[name].append(row)
                self.by_name_surface[skill_name_key(name,ignore_punctuation=False)][row['skill_id']]=row
                self.by_name_punctuation[skill_name_key(name)][row['skill_id']]=row
        for row in self.rows.values():
            for relation in row["relations"].values():
                if not set(relation) <= self.rows.keys():
                    raise ValueError("skill_library_relation_missing")

    @classmethod
    def load(cls, path: str | Path = DEFAULT_LIBRARY):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def get(self, identity: str | int) -> dict:
        key = str(identity)
        if key in self.rows:
            return self.rows[key]
        if key in self.by_db:
            return self.by_db[key]
        matches = self.by_name.get(key, [])
        if len(matches) == 1:
            return matches[0]
        if matches:raise ValueError('skill_identity_ambiguous')
        # Keep exact spellings ahead of the looser punctuation fallback: if
        # two skills differ only in punctuation, an explicit spelling wins.
        for index,normalized in ((self.by_name_surface,skill_name_key(key,ignore_punctuation=False)),
                                 (self.by_name_punctuation,skill_name_key(key))):
            if not normalized:continue
            matches=list(index.get(normalized,{}).values())
            if len(matches)==1:return matches[0]
            if matches:raise ValueError('skill_identity_ambiguous')
        raise ValueError('skill_identity_unknown')

    def search(self, query: str = "", *, kind: str | None = None, card_id: str | None = None) -> list[dict]:
        return [row for row in self.rows.values()
            if (not query or query in row["name_zh_cn"] or query in (row.get("name_ja") or "") or query == row["db_id"])
            and (not kind or row["skill_kind"] == kind)
            and (not card_id or any(e["card_id"] == str(card_id) for e in row["evolution"] + row["character_sources"]))]

    def quote_cost(self, identity: str | int, *, owned: tuple[str | int, ...] = (),
                   hint_levels: Mapping[str, int] | None = None, extra_discount_percent: int = 0,
                   displayed_final_cost: int | None = None) -> dict:
        row = self.get(identity)
        owned_ids = {self.get(item)["skill_id"] for item in owned}
        if row["skill_id"] in owned_ids:
            return {"cost_pt": 0, "cost_basis": "already_owned", "steps": []}
        if any(row["skill_id"] in self.rows[upper]["relations"].get("supersedes", []) for upper in owned_ids):
            return {"cost_pt": 0, "cost_basis": "covered_by_owned_upper", "steps": []}
        if displayed_final_cost is not None:
            if type(displayed_final_cost) is not int or displayed_final_cost < 0:
                raise ValueError("displayed_skill_cost_invalid")
            return {"cost_pt": displayed_final_cost, "cost_basis": "observed_final", "steps": [],
                    "discount_applied_again": False}
        if row["acquisition"]["kind"] != "pt_purchase":
            return {"cost_pt": None, "cost_basis": row["acquisition"]["kind"], "steps": [],
                    "evolution_pt": row["cost"]["evolution_pt"]}
        if type(extra_discount_percent) is not int or not 0 <= extra_discount_percent <= 60:
            raise ValueError("extra_skill_discount_invalid")
        hints = {self.get(key)["skill_id"]: value for key, value in (hint_levels or {}).items()}
        steps, low_total, high_total = [], 0, 0
        for skill_id in row["cost"]["chain"]:
            if skill_id in owned_ids or any(skill_id in self.rows[upper]["relations"].get("supersedes", []) for upper in owned_ids):
                continue
            step = self.rows[skill_id]
            hint = hints.get(skill_id, 0)
            if type(hint) is not int or hint not in HINT_DISCOUNTS:
                raise ValueError("skill_hint_level_invalid")
            percent = HINT_DISCOUNTS[hint] + extra_discount_percent
            if percent > 100:
                raise ValueError("skill_discount_invalid")
            numerator = step["upgrade_cost"] * (100 - percent)
            lower = numerator // 100
            upper = (numerator + 99) // 100
            low_total += lower
            high_total += upper
            steps.append({"skill_id": skill_id, "name_zh_cn": step["name_zh_cn"],
                "base_step_pt": step["upgrade_cost"], "hint_level": hint,
                "discount_percent": percent, "unrounded_pt": numerator / 100,
                "cost_pt_range": [lower, upper]})
        return {"cost_pt": high_total if low_total == high_total else None,
                "cost_pt_range": [low_total, high_total], "cost_basis": "library_estimate",
                "steps": steps, "rounding_verified": low_total == high_total,
                "rounding_note": "Nonintegral discounted costs retain an interval until a CN displayed price is available"}

    def explain_activation(self, identity: str | int, target: dict | None = None) -> dict:
        row = self.get(identity)
        inputs = race_inputs(target or {})
        branches = []
        for branch in row["activation_branches"]:
            condition = evaluate_condition(branch["condition_ast"], inputs)
            precondition = evaluate_condition(branch["precondition_ast"], inputs)
            status = "excluded" if condition is False or precondition is False else "conditional"
            branches.append({"branch": branch["branch"], "target_status": status,
                "precondition_code": branch["precondition_code"], "condition_code": branch["condition_code"],
                "condition_explanation": describe_condition(branch["condition_ast"], inputs.get("course_distance")),
                "effects": branch["effects"], "duration": branch["duration"],
                "cooldown_seconds": branch["cooldown_seconds"]})
        return {"skill_id": row["skill_id"], "db_id": row["db_id"], "name_zh_cn": row["name_zh_cn"],
                "target_compatible": any(b["target_status"] != "excluded" for b in branches),
                "branches": branches, "activation_probability": None,
                "race_simulation_supported": False}

    def prepare_recommendation_input(self, target: dict, available_skills: list[dict], *,
                                     owned: tuple[str | int, ...] = (), current_skill_pt: int = 0,
                                     stage: str = "training_end", card_id: str | None = None) -> dict:
        race_inputs(target)  # Validate before a partial packet can be emitted.
        if type(current_skill_pt) is not int or current_skill_pt < 0:
            raise ValueError("current_skill_pt_invalid")
        if stage not in {"training_mid", "training_end", "daily_task"}:
            raise ValueError("skill_recommendation_stage_invalid")
        owned_ids = [self.get(identity)["skill_id"] for identity in owned]
        candidates, excluded = [], []
        seen = set()
        for visible in available_skills:
            row = self.get(visible.get("skill_id", visible.get("db_id", "")))
            if row["skill_id"] in seen:
                raise ValueError("available_skill_duplicate")
            seen.add(row["skill_id"])
            reason = None
            quote = self.quote_cost(row["skill_id"], owned=owned,
                hint_levels=visible.get("hint_levels"),
                extra_discount_percent=visible.get("extra_discount_percent", 0),
                displayed_final_cost=visible.get("displayed_final_cost"))
            activation = self.explain_activation(row["skill_id"], target)
            if row["acquisition"]["kind"] != "pt_purchase":
                reason = "not_a_pt_purchase"
            elif quote["cost_basis"] in {"already_owned", "covered_by_owned_upper"}:
                reason = quote["cost_basis"]
            elif not activation["target_compatible"]:
                reason = "target_condition_mismatch"
            if reason:
                excluded.append({"skill_id": row["skill_id"], "reason": reason})
                continue
            candidates.append({"skill_id": row["skill_id"], "db_id": row["db_id"],
                "name_zh_cn": row["name_zh_cn"], "rarity": row["rarity"], "skill_kind": row["skill_kind"],
                "cost": quote, "relations": deepcopy(row["relations"]), "activation": activation,
                "evolution_options": [deepcopy(self.rows[evo]["evolution"][0]) for evo in row["relations"].get("evolves_to", [])
                                      if card_id and self.rows[evo]["evolution"][0]["card_id"] == str(card_id)]})
        return {"schema_version": "uma.skill_recommendation_input.v1",
            "dataset_version": self.metadata["dataset_version"], "scope": self.metadata["scope"],
            "stage": stage, "race_target": deepcopy(target), "card_id": card_id,
            "current_skill_pt": current_skill_pt, "owned_skills": owned_ids,
            "candidates": candidates, "excluded": excluded,
            "recommendation_status": "candidates_prepared_not_ranked",
            "purchase_execution_authorized": False}
