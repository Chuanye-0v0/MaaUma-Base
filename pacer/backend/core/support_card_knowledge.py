"""Offline card/event/skill graph and per-run consequences for pacer decisions."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import unicodedata

from core.skill_knowledge import SkillKnowledgeLibrary

DEFAULT_LIBRARY = Path(__file__).resolve().parents[1] / "data/support_knowledge/cn_larc_end/support_library.json"


def normalized(value):
    return "".join(unicodedata.normalize("NFKC", str(value)).split())


def offered_skills(event):
    result = []
    for option in event["options"]:
        for branch in option["branches"]:
            for skill in branch["skills"]:
                if skill["operation"] not in {"hint", "grant_skill", "random_hint", "random_hint_related"}:
                    continue
                result.append({**deepcopy(skill), "event_id": event["event_id"],
                    "story_id": event["story_id"], "support_card_id": event["support_card_id"],
                    "stage": event["stage"], "option_index": option["index"],
                    "branch_id": branch["branch_id"], "branch_probability": branch["probability"],
                    "event_occurrence_probability": None, "guaranteed": False})
    return result


class SupportCardKnowledgeLibrary:
    def __init__(self, payload, *, skills=None):
        if payload.get("schema_version") != "uma.support_knowledge.v1":
            raise ValueError("support_library_schema_invalid")
        self.metadata = {k: v for k, v in payload.items() if k not in {"cards", "events", "chains"}}
        self.cards = {r["card_id"]: r for r in payload["cards"]}
        self.events = {r["event_id"]: r for r in payload["events"]}
        self.chains = {r["chain_id"]: r for r in payload["chains"]}
        if (len(self.cards) != len(payload["cards"]) or len(self.events) != len(payload["events"])
                or len(self.chains) != len(payload["chains"])):
            raise ValueError("support_library_duplicate_id")
        self.skills = skills or SkillKnowledgeLibrary.load()
        self.by_story = defaultdict(list)
        self.by_title = defaultdict(list)
        self.by_skill = defaultdict(list)
        for event in self.events.values():
            if event["support_card_id"] not in self.cards:
                raise ValueError("support_event_owner_missing")
            self.by_story[event["story_id"]].append(event)
            for title in {v for v in event["titles"].values() if isinstance(v, str) and v}:
                self.by_title[normalized(title)].append(event)
            for opportunity in offered_skills(event):
                if opportunity["skill_id"]:
                    if opportunity["skill_id"] not in self.skills.rows:
                        raise ValueError("support_skill_binding_missing")
                    self.by_skill[opportunity["skill_id"]].append(opportunity)
            if any(key not in self.events for key in event["next_event_ids"]):
                raise ValueError("support_next_event_missing")
        for chain in self.chains.values():
            if any(key not in self.events for key in chain["event_ids"]):
                raise ValueError("support_chain_event_missing")

    @classmethod
    def load(cls, path=DEFAULT_LIBRARY, *, skills=None):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), skills=skills)

    def get_card(self, identity):
        key = str(identity)
        if key in self.cards:
            return self.cards[key]
        matches = [card for card in self.cards.values() if normalized(key) in
            {normalized(v) for v in [card["name_zh_cn"], card["name_ja"], *card["aliases"]] if v}]
        if len(matches) != 1:
            raise ValueError("support_card_ambiguous" if matches else "support_card_unknown")
        return matches[0]

    def search_cards(self, text=""):
        query = normalized(text)
        return [card for card in self.cards.values() if not query or any(query in normalized(v)
            for v in [card["card_id"], card["name_zh_cn"], card["name_ja"], *card["aliases"]] if v)]

    def resolve_event(self, request, *, deck_card_ids=None):
        if not isinstance(request, dict):
            raise ValueError("support_event_request_invalid")
        event_id = request.get("event_id")
        if event_id in self.events:
            matches = [self.events[event_id]]
        elif request.get("story_id") is not None:
            matches = list(self.by_story.get(str(request["story_id"]), []))
        elif request.get("title"):
            matches = list(self.by_title.get(normalized(request["title"]), []))
        else:
            return {"status": "unknown", "events": []}
        if deck_card_ids is not None:
            allowed = set(deck_card_ids)
            matches = [e for e in matches if e["support_card_id"] in allowed]
        if request.get("card_id"):
            matches = [e for e in matches if e["support_card_id"] == str(request["card_id"])]
        count = request.get("option_count")
        if count is None and request.get("visible_option_texts"):
            count = len(request["visible_option_texts"])
        if count is not None:
            matches = [e for e in matches if len(e["options"]) == count]
        return {"status": "matched" if len(matches) == 1 else "ambiguous" if matches else "unknown", "events": matches}

    def _future_events(self, event, option):
        if option["chain_transition"] == "end":
            return []
        result, seen = [], set()
        queue = list(event["next_event_ids"])
        for chain_id in option.get("opens_chain_ids", []):
            queue.extend(self.chains[chain_id]["event_ids"][:1])
        while queue:
            key = queue.pop(0)
            if key in seen:
                continue
            seen.add(key)
            row = self.events[key]
            result.append(row)
            # A later event can still advance only through an appropriate choice.
            for later_option in row["options"]:
                if later_option["chain_transition"] != "end":
                    queue.extend(row["next_event_ids"])
                    for cid in later_option.get("opens_chain_ids", []):
                        queue.extend(self.chains[cid]["event_ids"][:1])
        return result

    def option_consequences(self, event_identity, *, targets=(), learned=()):
        if isinstance(event_identity, dict):
            event = event_identity
        elif event_identity in self.events:
            event = self.events[event_identity]
        else:
            match = self.resolve_event({"story_id": str(event_identity)})
            if match["status"] != "matched":
                raise ValueError("support_event_identity_ambiguous")
            event = match["events"][0]
        target_ids = {self.skills.get(i)["skill_id"] for i in targets}
        learned_ids = {self.skills.get(i)["skill_id"] for i in learned}
        satisfied = set(learned_ids)
        for identity in learned_ids:
            row = self.skills.rows[identity]
            satisfied.update(row["relations"].get("supersedes", []))
            for parent in row["relations"].get("evolves_from", []):
                satisfied.add(parent)
                satisfied.update(self.skills.rows[parent]["relations"].get("supersedes", []))
        unsatisfied = target_ids - satisfied
        # Reachability before the present choice includes all continuable paths.
        potential = {e["event_id"]: e for o in event["options"] for e in self._future_events(event, o)}
        potential_skills = [s for e in potential.values() for s in offered_skills(e)]
        future_ids = {s["skill_id"] for s in potential_skills if s["skill_id"]}
        all_current_ids = {s["skill_id"] for s in offered_skills(event) if s["skill_id"]}
        result = []
        for option in event["options"]:
            future_events = self._future_events(event, option)
            future_hints = [s for e in future_events for s in offered_skills(e)]
            retained = {s["skill_id"] for s in future_hints if s["skill_id"]}
            current = [s for s in offered_skills(event) if s["option_index"] == option["index"]]
            current_ids = {s["skill_id"] for s in current if s["skill_id"]}
            lost = future_ids - retained
            uncertain = option["chain_transition"] == "outcome_dependent"
            result.append({"option_index": option["index"], "option_id": option["option_id"],
                "texts": deepcopy(option["texts"]), "chain_transition": option["chain_transition"],
                "immediate_branches": deepcopy(option["branches"]),
                "current_skill_hints": current, "future_possible_skill_hints": future_hints,
                "future_event_ids": [e["event_id"] for e in future_events],
                "lost_future_skill_ids": sorted(lost),
                "lost_future_gold_skill_ids": sorted(s for s in lost if self.skills.rows[s]["rarity"] == "gold"),
                "lost_target_skill_routes": sorted(((future_ids | all_current_ids) - (retained | current_ids)) & unsatisfied),
                "foregone_current_target_skill_ids": sorted((all_current_ids - current_ids - retained) & unsatisfied),
                "target_paths_retained": sorted((retained | current_ids) & unsatisfied),
                "target_skill_hints_now": sorted(current_ids & unsatisfied),
                "termination_probability": None if uncertain else 1 if option["chain_transition"] == "end" else 0,
                "target_path_uncertain": uncertain, "future_rewards_are_guaranteed": False})
        retained_sets = [set(row["target_paths_retained"]) for row in result]
        best = max((len(v) for v in retained_sets), default=0)
        preferred = [row["option_index"] for row in result if best > 0 and len(row["target_paths_retained"]) == best
                     and not row["target_path_uncertain"]]
        # Advisory target paths are distinct from a final vitality/risk/utility decision.
        return {"event_id": event["event_id"], "story_id": event["story_id"],
            "support_card_id": event["support_card_id"], "title_zh_cn": event["titles"]["zh_cn"],
            "stage": event["stage"], "chain_id": event["chain_id"],
            "unsatisfied_target_skills": sorted(unsatisfied), "options": result,
            "preferred_for_target_paths": preferred, "final_choice_index": None,
            "basis": "reference_card_event_graph; target-path advisory, not an executed action",
            "purchase_or_tap_authorized": False}

    def build_run_context(self, profile, *, run_id, progress=None, through_turn=None,
                          target_skill_ids=(), learned_skill_ids=(), current_event=None):
        if not isinstance(profile, dict) or profile.get("schema_version") not in {
            "uma_run_support_profile.v1", "uma_current_support_deck.v1"}:
            raise ValueError("run_support_profile_schema_invalid")
        if not run_id or profile.get("run_session_id") not in {None, run_id}:
            raise ValueError("run_support_profile_run_mismatch")
        raw_cards = profile.get("cards")
        if not isinstance(raw_cards, list) or len(raw_cards) > 6:
            raise ValueError("run_support_profile_cards_invalid")
        if through_turn is not None and (type(through_turn) is not int or through_turn < 0):
            raise ValueError("support_context_turn_invalid")
        deck, slots, known = [], set(), set()
        for row in raw_cards:
            slot = row.get("slot")
            if type(slot) is not int or not 0 <= slot <= 5 or slot in slots:
                raise ValueError("run_support_profile_slot_invalid")
            slots.add(slot)
            identity = str(row["card_id"]) if row.get("card_id") else None
            if identity in known:
                raise ValueError("run_support_profile_duplicate_card")
            card = self.cards.get(identity)
            if card:
                known.add(identity)
                if row.get("rarity") not in {None, card["rarity"]}:
                    raise ValueError("run_support_profile_rarity_mismatch")
            deck.append({"slot": slot, "card_id": identity,
                "identity_status": "known" if card else "unknown",
                "name_zh_cn": card["name_zh_cn"] if card else None,
                "rarity": card["rarity"] if card else row.get("rarity"),
                "type": card["type"] if card else row.get("type"),
                "level": row.get("level"), "limit_breaks": row.get("limit_breaks"),
                "actual_effects": deepcopy(row.get("actual_effects", {})),
                "actual_effects_source": deepcopy(row.get("actual_effects_source", row.get("source"))),
                "source": deepcopy(row.get("source", profile.get("source"))),
                "chain_ids": list(card["chain_ids"]) if card else []})
        progress = progress or {"run_id": run_id, "occurrences": []}
        if progress.get("run_id", progress.get("run_session_id")) != run_id:
            raise ValueError("support_progress_run_mismatch")
        last = progress.get("current_turn_index", progress.get("through_turn"))
        if through_turn is not None and type(last) is int and last > through_turn:
            raise ValueError("support_progress_from_future")
        targets = [self.skills.get(i)["skill_id"] for i in target_skill_ids]
        learned = [self.skills.get(i)["skill_id"] for i in learned_skill_ids]
        unresolved_learned = []
        learned_rows = progress.get("learned_skills", {})
        learned_rows = learned_rows.values() if isinstance(learned_rows, dict) else learned_rows
        for row in learned_rows:
            try:
                identity = row.get("skill_id") or row.get("db_id") or row.get("name")
                learned.append(self.skills.get(identity)["skill_id"])
            except (ValueError, TypeError):
                unresolved_learned.append(deepcopy(row))
        states = {cid: {"chain_id": cid, "support_card_id": chain["card_id"], "kind": chain["kind"],
            "status": "not_started", "seen_stages": [], "completed_stages": [], "selected_options": [],
            "ended_at_stage": None, "history": [], "future_rewards_are_guaranteed": False}
            for cid, chain in self.chains.items() if chain["card_id"] in known}
        observations = progress.get("occurrences", progress.get("confirmed_occurrences", []))
        unique = {}
        for index, row in enumerate(observations):
            key = row.get("occurrence_id") or f"row:{index}"
            if key in unique and unique[key] != row:
                raise ValueError("support_occurrence_conflict")
            unique[key] = row
        notes = []
        for receipt in sorted(unique.values(), key=lambda r: r.get("turn_index") if type(r.get("turn_index")) is int else -1):
            at = receipt.get("turn_index")
            if through_turn is not None and type(at) is int and at > through_turn:
                raise ValueError("support_receipt_from_future")
            owner = receipt.get("owner") or {}
            owner_id = owner.get("owner_id")
            request = {"event_id": receipt.get("event_id"), "story_id": receipt.get("story_id"),
                "title": receipt.get("title"), "visible_option_texts": receipt.get("visible_option_texts"),
                "option_count":receipt.get('option_count')}
            if str(owner_id) in self.cards:
                request["card_id"] = str(owner_id)
            match = self.resolve_event(request, deck_card_ids=known)
            if match["status"] != "matched":
                notes.append({"occurrence_id": receipt.get("occurrence_id"), "status": match["status"]})
                continue
            event = match["events"][0]
            state = states.get(event["chain_id"])
            selected = receipt.get("selected_option_index")
            if selected is None and receipt.get("option_id"):
                selected = next((o["index"] for o in event["options"] if o["option_id"] == receipt["option_id"]), None)
            option = next((o for o in event["options"] if o["index"] == selected), None)
            settled = receipt.get("result_observed") is True
            actual_end = receipt.get("chain_ended") is True
            branch_id = receipt.get("branch_id")
            branch = next((b for b in (option or {}).get("branches", []) if b["branch_id"] == branch_id), None)
            if state is not None:
                stage = event["stage"]
                if state["ended_at_stage"] is not None and stage > state["ended_at_stage"]:
                    state["status"] = "conflicting_history"
                state["seen_stages"].append(stage)
                state["history"].append({"occurrence_id": receipt.get("occurrence_id"), "event_id": event["event_id"],
                    "stage": stage, "option_index": selected, "result_observed": settled,
                    "branch_id": branch_id, "turn_index": at})
                if state["status"] == "not_started":
                    state["status"] = "in_progress"
                if settled:
                    state["completed_stages"].append(stage)
                if actual_end:
                    state["ended_at_stage"] = stage
                    if state["status"] != "conflicting_history":
                        state["status"] = "terminated"
                if option:
                    state["selected_options"].append({"stage": stage, "index": selected, "settled": settled})
                    if actual_end or (settled and option["chain_transition"] == "end") or (settled and branch and branch["chain_end"]):
                        state["ended_at_stage"] = stage
                        if state["status"] != "conflicting_history":
                            state["status"] = "terminated"
                    elif option["chain_transition"] in {"end", "outcome_dependent"} and not (settled and branch and not branch["chain_end"]):
                        if state["status"] not in {"terminated", "conflicting_history"}:
                            state["status"] = "termination_unconfirmed"
                if settled and stage == self.chains[state["chain_id"]]["stage_count"] and state["status"] == "in_progress":
                    state["status"] = "completed"
            if option and settled:
                if option["chain_transition"] == "end" or actual_end or branch and branch["chain_end"]:
                    for key in option.get("closes_chain_ids", []) + option.get("conditional_closes_chain_ids", []):
                        if key in states:
                            if states[key]["status"] != "conflicting_history":
                                states[key]["status"] = "terminated"
                            states[key]["closed_by_event_id"] = event["event_id"]
                for key in option.get("opens_chain_ids", []):
                    if key in states and (len(option["branches"]) == 1 or branch and branch["outing_unlock"]):
                        states[key]["unlock_observed"] = True
            elif option and option["chain_transition"] in {"end", "outcome_dependent"}:
                for key in option.get("closes_chain_ids", []) + option.get("conditional_closes_chain_ids", []):
                    if key in states and states[key]["status"] not in {"terminated", "conflicting_history"}:
                        states[key]["status"] = "termination_unconfirmed"
        active_match = self.resolve_event(current_event, deck_card_ids=known) if current_event else None
        if active_match and active_match["status"] == "matched":
            active = active_match["events"][0]
            if active["chain_id"] in states:
                states[active["chain_id"]]["current_visible_stage"] = active["stage"]
                if states[active["chain_id"]]["status"] == "not_started":
                    states[active["chain_id"]]["status"] = "in_progress"
        future = []
        for key, state in states.items():
            state["seen_stages"] = sorted(set(state["seen_stages"]))
            state["completed_stages"] = sorted(set(state["completed_stages"]))
            if (state["status"] == "in_progress" and max(state["seen_stages"], default=0) == self.chains[key]["stage_count"]):
                state["status"] = "final_stage_seen_unsettled"
            highest = max([*state["seen_stages"], state.get("current_visible_stage", 0)], default=0)
            chain = self.chains[key]
            remaining = [eid for eid in chain["event_ids"] if self.events[eid]["stage"] > highest]
            state["remaining_event_ids"] = [] if state["status"] in {"terminated", "completed"} else remaining
            for eid in state["remaining_event_ids"]:
                for hint in offered_skills(self.events[eid]):
                    if hint["skill_id"] in learned:
                        continue
                    future.append({**hint, "chain_id": key, "chain_status": state["status"],
                        "targeted": hint["skill_id"] in targets,
                        "availability": "uncertain" if state["status"] in {"termination_unconfirmed", "conflicting_history"} else "possible"})
        current = None
        if current_event:
            match = active_match
            current = {"status": match["status"], "candidates": [e["event_id"] for e in match["events"]]}
            if match["status"] == "matched":
                event = match["events"][0]
                current["consequences"] = self.option_consequences(event, targets=targets, learned=learned)
                state = states.get(event["chain_id"])
                if state and state["status"] in {"terminated", "completed", "conflicting_history"}:
                    current["status"] = "history_conflict_or_already_finished"
                    current["consequences"]["preferred_for_target_paths"] = []
                # A lost path through this card is not a claim that every other source is gone.
                for option in current["consequences"]["options"]:
                    visible = current_event.get("visible_option_texts") or []
                    option["observed_option_text"] = visible[option["option_index"] - 1] if len(visible) == len(event["options"]) else None
                    option["other_known_support_routes"] = {sid: sorted({h["support_card_id"] for h in future
                        if h["skill_id"] == sid and h["support_card_id"] != event["support_card_id"]})
                        for sid in option["lost_target_skill_routes"]}
        return {"schema_version": "uma.support_chain_context.v1", "run_session_id": run_id,
            "through_turn": through_turn, "turn_basis": progress.get("current_turn_basis", "pacer_action_turn"),
            "dataset_version": self.metadata["dataset_version"],
            "skill_library_version": self.metadata["skill_library_version"], "deck": deck,
            "chain_progress": list(states.values()), "future_skill_opportunities": future,
            "target_skill_ids": sorted(set(targets)), "learned_skill_ids": sorted(set(learned)),
            "current_event": current, "unmatched_occurrences": notes, "unresolved_learned_skills": unresolved_learned,
            "observed_skill_hints": deepcopy(progress.get("skill_hints", {})),
            "future_rewards_are_guaranteed": False, "reference_effects_are_observed": False,
            "tap_or_purchase_authorized": False}


@lru_cache(maxsize=1)
def default_library():
    return SupportCardKnowledgeLibrary.load()


def prepare_support_chain_context(profile, **kwargs):
    """Pacer entry: reuse the in-process library, without capture/OCR/network calls."""
    return default_library().build_run_context(profile, **kwargs)


def attach_support_chain_context(packet, profile, **kwargs):
    if not isinstance(packet, dict):
        raise ValueError("support_context_packet_required")
    run_id = kwargs.get("run_id")
    bound = packet.get("source", {}).get("run_session_id", packet.get("run_session_id"))
    if bound != run_id:
        raise ValueError("support_context_packet_run_mismatch")
    result = deepcopy(packet)
    result["support_chain_context"] = prepare_support_chain_context(profile, **kwargs)
    return result


def prepare_pacer_support_context(session_dir, *, run_id, through_turn,
                                  target_skill_ids=(), learned_skill_ids=(), current_event=None):
    """Read only this run's existing profile/history; never open images or a global deck."""
    root = Path(session_dir)
    profile = json.loads((root / "support_card_profile.json").read_text(encoding="utf-8-sig"))
    progress_path = root / "event_progress.json"
    if not progress_path.exists():
        progress_path = root / "event_progress_summary.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8-sig")) if progress_path.exists() else None
    target_path = root / "support_skill_targets.json"
    if not target_skill_ids and target_path.exists():
        requested = json.loads(target_path.read_text(encoding="utf-8-sig"))
        if requested.get("run_session_id") != run_id:
            raise ValueError("support_skill_targets_run_mismatch")
        target_skill_ids = requested.get("target_skill_ids", ())
    result = prepare_support_chain_context(profile, run_id=run_id, progress=progress, through_turn=through_turn,
        target_skill_ids=target_skill_ids, learned_skill_ids=learned_skill_ids, current_event=current_event)
    result["input_sources"] = {"profile": str(root / "support_card_profile.json"),
                               "progress": str(progress_path) if progress is not None else None}
    return result
