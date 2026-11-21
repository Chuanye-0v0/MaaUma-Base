"""Offline, per-run event observations and conditional future reward summaries."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.event_knowledge_lookup import REVIEWED_PATHS, chain_stage, load_reviewed_events, normalize_text
from core.event_text_match import option_key, owner_matches, visible_event_options


VERSION = "uma.event_progress.v1"
_HINT_GAIN = re.compile(r"[“「『\"]([^”」』\"]+)[”」』\"]的启发等级提升了(\d+)")


def new_progress(run_id: str) -> dict[str, Any]:
    if not run_id:
        raise ValueError("run_id required")
    return {"schema_version": VERSION, "run_id": run_id, "current_turn_index": None,
            "latest_turn_id": None, "seen_observation_ids": [],
            "occurrences": [], "skill_hints": {}, "hint_increase_observations": [],
            "purchasable_skills": {}, "learned_skills": {}}


def load_progress(path: Path, run_id: str) -> dict[str, Any]:
    path = Path(path)
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else new_progress(run_id)
    if state.get("schema_version") != VERSION or state.get("run_id") != run_id:
        raise ValueError("progress file belongs to another run or version")
    return state


def save_progress(path: Path, state: Mapping[str, Any]) -> None:
    path = Path(path)
    if path.exists():
        load_progress(path, state["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _title_matches(event: Mapping[str, Any], title: str) -> bool:
    titles = event["titles"]
    values = [value for key, value in titles.items() if key != "aliases" and isinstance(value, str)]
    values.extend(alias["text"] for alias in titles.get("aliases", []))
    return normalize_text(title) in {normalize_text(value) for value in values}


def _visible_options(lines: list[str], event: Mapping[str, Any]) -> list[str]:
    matched = visible_event_options(lines, event)
    if matched is not None:
        return matched
    title_index = next((index for index, line in enumerate(lines) if _title_matches(event, line)), None)
    if title_index is None:
        return []
    end_index = next((index for index in range(title_index + 1, len(lines))
                      if lines[index] in {"选项效果", "選項效果"}), None)
    if end_index is None:
        return []
    choices = lines[title_index + 1:end_index]
    return choices


def adapt_event_observation(
    raw: Mapping[str, Any], *, reviewed_paths: Iterable[Path] = REVIEWED_PATHS,
) -> dict[str, Any]:
    """Resolve exact library titles from structured fields or existing OCR lines.

    Visible option text never proves a selection. A receipt or result must supply
    selected_option_index/text; results and acquired skills must be explicit.
    """
    run_id = raw.get("run_id") or raw.get("run_session_id")
    turn_index = raw.get("turn_index", raw.get("global_action_turn"))
    turn_id = raw.get("turn_id") or (f"turn_{turn_index:03d}" if type(turn_index) is int else None)
    observation_id = (raw.get("observation_id") or raw.get("source_artifact_path") or
                      raw.get("frame_path") or raw.get("source_frame_path"))
    if observation_id is None and raw.get("observation_index") is not None:
        observation_id = f"{turn_id}:observation_{raw['observation_index']}"
    for field, value in (("run_id", run_id), ("turn_id", turn_id), ("observation_id", observation_id)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} required")
    if turn_index is None:
        match = re.fullmatch(r"turn_0*(\d+)", turn_id)
        turn_index = int(match[1]) if match else None
    if turn_index is not None and (type(turn_index) is not int or turn_index < 0):
        raise ValueError("turn_index invalid")
    events = load_reviewed_events(reviewed_paths)
    explicit_id = raw.get("event_id")
    title = raw.get("title") or raw.get("event_title")
    lines = [line.strip() for line in str(raw.get("visible_text") or "").splitlines() if line.strip()]
    hint_increases = list(raw.get("observed_skill_hint_increments") or [
        {"name": match[1], "increment": int(match[2])}
        for line in lines for match in _HINT_GAIN.finditer(line)
    ])
    if explicit_id:
        matches = [event for event in events if event["event_id"] == explicit_id]
    elif title:
        matches = [event for event in events if _title_matches(event, title)]
    else:
        matches = [event for event in events if any(_title_matches(event, line) for line in lines)]
    if raw.get("owner_id"):
        matches = [event for event in matches if event["owner"]["owner_id"] == raw["owner_id"]]
    matches = [event for event in matches if owner_matches(event, raw.get("trainee_id"))]
    # Same-name trainee events are not interchangeable. Full localized options
    # can disambiguate when the run has no explicit trainee ID recorded yet.
    if len(matches) > 1:
        option_matches = [event for event in matches if event["event_kind"] in {"choice", "outgoing"}
                          and visible_event_options(lines, event)]
        if len(option_matches) == 1:
            matches = option_matches
    event = matches[0] if len(matches) == 1 else None
    if event is not None and title and not _title_matches(event, title):
        event = None
    option = None
    option_match_status = "not_selected"
    observed_options: list[str] = []
    selected_index = raw.get("selected_option_index", raw.get("chosen_option_index"))
    if selected_index is not None and (type(selected_index) is not int or selected_index < 0):
        raise ValueError("selected option index invalid")
    selected_text = raw.get("selected_option_text")
    matched_title_line = next((line for line in lines if event and _title_matches(event, line)), None)
    choice_seen = (any(line in {"选项效果", "選項效果"} for line in lines)
                   or bool(raw.get("visible_option_texts"))
                   or (selected_index is not None and selected_index > 0)
                   or raw.get("selection_action_id") in {
                       "event_choice.top", "event_choice.bottom", "event_choice.second",
                       "event_choice.third", "event_choice.fourth"})
    if event is not None and event["event_kind"] == "no_choice" and choice_seen:
        event = None
        option_match_status = "mismatch"
    if event is not None:
        observed_options = list(raw.get("visible_option_texts") or _visible_options(lines, event))
        catalog_options = sorted(event["options"], key=lambda item: item["index"])
        if selected_index is None and raw.get("selection_action_id") == "event_choice.bottom" and observed_options:
            selected_index = len(observed_options)
        if selected_index is not None and 1 <= selected_index <= len(observed_options):
            visible_selection = observed_options[selected_index - 1]
            if selected_text and normalize_text(selected_text) != normalize_text(visible_selection):
                raise ValueError("selected option text conflicts with visible option")
            selected_text = selected_text or visible_selection
        if observed_options and (len(observed_options) != len(catalog_options) or any(
            option_key(text) not in {option_key(value) for value in item["texts"].values()}
            for text, item in zip(observed_options, catalog_options)
        )):
            option_match_status = "mismatch"
            if not explicit_id:
                event = None
        elif event["event_kind"] == "no_choice" and len(event["options"]) == 1:
            option = event["options"][0]
            option_match_status = "automatic"
        elif selected_index is not None:
            candidate = next((item for item in event["options"] if item["index"] == selected_index), None)
            if candidate is None:
                raise ValueError("selected option index not in event")
            if selected_text is None:
                option_match_status = "unverified"
            elif option_key(selected_text) in {option_key(value) for value in candidate["texts"].values()}:
                option, option_match_status = candidate, "matched"
            else:
                option_match_status = "mismatch"
                if not explicit_id:
                    event = None
        elif selected_text:
            choices = [item for item in event["options"] if option_key(selected_text) in
                       {option_key(value) for value in item["texts"].values()}]
            option = choices[0] if len(choices) == 1 else None
            option_match_status = "matched" if option else "unverified"
    event_id = event["event_id"] if event else None
    resolved_title = title or matched_title_line or (event["titles"].get("zh_cn") or event["titles"].get("ja") if event else None)
    occurrence_id = (raw.get("occurrence_id") or raw.get("event_occurrence_id") or
                     f"{turn_id}:{event_id or resolved_title or observation_id}")
    return {
        "run_id": run_id, "turn_id": turn_id, "turn_index": turn_index,
        "turn_basis": raw.get("turn_basis") or "provided_turn",
        "observation_id": observation_id, "occurrence_id": occurrence_id,
        "source": raw.get("source") or raw.get("source_artifact_path") or "existing_observation",
        "title": resolved_title, "event_id": event_id,
        "story_id":raw.get('story_id'), "option_count":raw.get('option_count'),
        "match_status": "option_mismatch" if option_match_status == "mismatch" else "matched" if event else "ambiguous" if len(matches) > 1 else "unknown",
        "owner": event["owner"] if event else None,
        "event_kind": event["event_kind"] if event else None,
        "chain_stage": chain_stage(event) if event else None,
        "option_id": option["option_id"] if option else None,
        "option_match_status": option_match_status,
        "visible_option_texts": observed_options,
        "selected_option_text": selected_text,
        "selected_option_index": selected_index,
        "selection_action_id": raw.get("selection_action_id"),
        "followup_state": option.get("followup_state") if option else None,
        "outcome": raw.get("outcome"),
        "result_observed": bool(raw.get("result_observed") or raw.get("outcome") or
                                raw.get("observed_effects") or raw.get("observed_skill_hints") or
                                raw.get("chain_ended")),
        "result_text": raw.get("result_text"),
        "observed_effects": list(raw.get("observed_effects") or []),
        "observed_skill_hints": list(raw.get("observed_skill_hints") or []),
        "observed_skill_hint_increments": hint_increases,
        "hint_popup_id": raw.get("hint_popup_id") or observation_id,
        "purchasable_skills": list(raw.get("purchasable_skills") or []),
        "learned_skills": list(raw.get("learned_skills") or []),
        "chain_ended": raw.get("chain_ended") is True,
    }


def adapt_report_observations(
    report: Mapping[str, Any], report_path: str | Path, *,
    run_session_id: str | None = None, action_turn: int | None = None,
    trainee_id: str | None = None,
    reviewed_paths: Iterable[Path] = REVIEWED_PATHS,
) -> list[dict[str, Any]]:
    """Use already saved route rows; do not open screenshots or run OCR."""
    report_path = str(report_path)
    if action_turn is None:
        match = re.search(r"(?:^|[\\/])turn_(\d+)(?:[\\/]|$)", report_path)
        action_turn = int(match[1]) if match else None
    observations = []
    hint_popup_id = None
    for row in report.get("step_reports", []):
        result = row.get("result") or {}
        transition = result.get("route_transition") or report.get("route_transition") or {}
        run_id = run_session_id or transition.get("run_session_id")
        frame_safety = result.get("frame_safety") or {}
        visible_text = frame_safety.get("visible_text") if frame_safety.get("text_status") == "ok" else ""
        hints_seen = bool(_HINT_GAIN.search(visible_text or ""))
        if hints_seen and hint_popup_id is None:
            hint_popup_id = f"{report_path}:hint_popup_{row['step']}"
        elif not hints_seen:
            hint_popup_id = None
        decision = result.get("decision") or {}
        tap = decision.get("tap") or {}
        action_id = tap.get("coordinate_name") if result.get("performed") and decision.get("action") == "tap" else None
        selected_index = {"event_choice.top": 1, "event_choice.second": 2,
                          "event_choice.third": 3, "event_choice.fourth": 4}.get(action_id)
        selection=(decision.get('evidence') or {}).get('event_selection') or {}
        reference=(selection if action_id and action_id.startswith('event_choice.')
            and selection.get('status')=='selected' and selection.get('event_policy_version') in {'larc_events_v2','larc_events_v3','larc_events_v4','larc_events_v5'} else {})
        expected=reference.get('expected_event') or ()
        if expected and type(reference.get('choice_index')) is int and 1<=reference['choice_index']<len(expected):
            selected_index=reference['choice_index']
        lines = [line.strip() for line in (visible_text or "").splitlines() if line.strip()]
        unknown_title = next((lines[index + 1] for index, line in enumerate(lines[:-1])
                              if line in {"养成优俊少女事件", "协助卡事件", "支援卡事件"}), None)
        image_path = (row.get("screenshot_path") or
                      (decision.get("evidence") or {}).get("image") or
                      (result.get("route_action_receipt_source") or {}).get("source_path"))
        raw = {"run_session_id": run_id, "turn_index": action_turn,
               "trainee_id": trainee_id,
               "observation_id": f"{report_path}:step_{row['step']}",
               "source_artifact_path": image_path, "source": report_path,
               "visible_text": visible_text, "selected_option_index": selected_index,
               "selection_action_id": action_id if action_id and action_id.startswith("event_choice.") else None,
               "hint_popup_id": hint_popup_id, "turn_basis": "action_turn"}
        if expected:
            raw.update(title=expected[0],story_id=reference.get('story_id'),option_count=len(expected)-1)
        observation = adapt_event_observation(raw, reviewed_paths=reviewed_paths)
        if observation["event_id"] is None and observation["match_status"] == "unknown" and unknown_title:
            raw["title"] = unknown_title
            observation = adapt_event_observation(raw, reviewed_paths=reviewed_paths)
        if (observation["event_id"] or observation["match_status"] == "option_mismatch" or
                observation["observed_skill_hint_increments"] or observation["selection_action_id"] or unknown_title):
            observations.append(observation)
    return observations


def _skill_key(skill: Mapping[str, Any]) -> str:
    key = skill.get("skill_id") or skill.get("name")
    if not isinstance(key, str) or not key:
        raise ValueError("skill_id or name required")
    return key


def record_event_observation(state: Mapping[str, Any], observation: Mapping[str, Any]) -> dict[str, Any]:
    """Return updated state; repeated observation IDs are no-ops."""
    if state.get("schema_version") != VERSION or state.get("run_id") != observation.get("run_id"):
        raise ValueError("observation belongs to another run")
    updated = copy.deepcopy(state)
    if observation["observation_id"] in updated["seen_observation_ids"]:
        return updated
    updated["seen_observation_ids"].append(observation["observation_id"])
    turn_index = observation.get("turn_index")
    if turn_index is not None and (updated.get("current_turn_index") is None or
                                   turn_index >= updated["current_turn_index"]):
        updated["current_turn_index"] = turn_index
        updated["latest_turn_id"] = observation["turn_id"]
        updated["current_turn_basis"] = observation.get("turn_basis")
    elif turn_index is None:
        updated["latest_turn_id"] = observation["turn_id"]
        updated["current_turn_basis"] = observation.get("turn_basis")
    if (observation.get("event_id") or observation.get("title") or
            any(item["occurrence_id"] == observation["occurrence_id"] for item in updated["occurrences"])):
        occurrence = next((item for item in updated["occurrences"] if item["occurrence_id"] == observation["occurrence_id"]), None)
        if occurrence is None:
            occurrence = {"occurrence_id": observation["occurrence_id"], "turn_id": observation["turn_id"],
                          "turn_index": observation.get("turn_index"),
                          "turn_basis": observation.get("turn_basis"),
                          "phase": observation.get("phase"),
                          "title": observation.get("title"), "event_id": observation.get("event_id"),
                          "story_id":observation.get('story_id'), "option_count":observation.get('option_count'),
                          "match_status": observation.get("match_status"), "owner": observation.get("owner"),
                          "event_kind": observation.get("event_kind"), "chain_stage": observation.get("chain_stage"),
                          "observation_ids": [], "sources": [], "option_id": None,
                          "option_match_status": None, "visible_option_texts": [], "selected_option_text": None,
                          "selection_action_id": None,
                          "selected_option_index": None, "followup_state": None, "outcome": None,
                          "result_observed": False, "result_text": None, "observed_effects": [], "chain_ended": False}
            updated["occurrences"].append(occurrence)
        elif occurrence["turn_id"] != observation["turn_id"] or (
            occurrence["event_id"] and observation.get("event_id") and occurrence["event_id"] != observation["event_id"]
        ):
            raise ValueError("occurrence identity conflict")
        if occurrence["event_id"] is None and observation.get("event_id"):
            for field in ("title", "event_id", "match_status", "owner", "event_kind", "chain_stage"):
                occurrence[field] = observation.get(field)
        if occurrence["option_id"] and observation.get("option_id") and occurrence["option_id"] != observation["option_id"]:
            raise ValueError("occurrence option conflict; use another occurrence_id for a real repeat")
        if occurrence["outcome"] and observation.get("outcome") and occurrence["outcome"] != observation["outcome"]:
            raise ValueError("occurrence outcome conflict")
        occurrence["observation_ids"].append(observation["observation_id"])
        if observation.get("source") not in occurrence["sources"]:
            occurrence["sources"].append(observation.get("source"))
        for field in ("option_id", "selected_option_index", "option_match_status", "visible_option_texts", "story_id", "option_count",
                      "selected_option_text", "selection_action_id", "followup_state", "outcome", "result_text"):
            if observation.get(field) is not None:
                occurrence[field] = observation[field]
        occurrence["result_observed"] |= bool(observation.get("result_observed"))
        occurrence["chain_ended"] |= bool(observation.get("chain_ended"))
        for effect in observation.get("observed_effects", []):
            if effect not in occurrence["observed_effects"]:
                occurrence["observed_effects"].append(effect)
    for skill in observation.get("observed_skill_hints", []):
        key = _skill_key(skill)
        level = skill.get("level")
        if type(level) is not int or level < 1:
            raise ValueError("observed skill hint level required")
        previous = updated["skill_hints"].get(key, {})
        updated["skill_hints"][key] = {"skill_id": skill.get("skill_id"), "name": skill.get("name"),
                                       "level": max(level, previous.get("level", 0)), "status": "observed"}
    for increase in observation.get("observed_skill_hint_increments", []):
        name, amount = increase.get("name"), increase.get("increment")
        if not isinstance(name, str) or not name or type(amount) is not int or amount < 1:
            raise ValueError("skill hint increase invalid")
        existing = next((item for item in updated["hint_increase_observations"]
                         if item["hint_popup_id"] == observation["hint_popup_id"]
                         and item["name"] == name and item["increment"] == amount), None)
        if existing is None:
            updated["hint_increase_observations"].append({"hint_popup_id": observation["hint_popup_id"],
                                                           "name": name, "increment": amount,
                                                           "final_level": None, "source_event_id": None,
                                                           "observation_ids": [observation["observation_id"]]})
        elif observation["observation_id"] not in existing["observation_ids"]:
            existing["observation_ids"].append(observation["observation_id"])
    for skill in observation.get("purchasable_skills", []):
        key = _skill_key(skill)
        if key not in updated["learned_skills"]:
            updated["purchasable_skills"][key] = {"skill_id": skill.get("skill_id"),
                                                   "name": skill.get("name"), "status": "observed"}
    for skill in observation.get("learned_skills", []):
        key = _skill_key(skill)
        updated["learned_skills"][key] = {"skill_id": skill.get("skill_id"),
                                           "name": skill.get("name"), "status": "observed"}
        updated["purchasable_skills"].pop(key, None)
    return updated


def _future_options(event: Mapping[str, Any], followup_states: set[str]) -> list[dict[str, Any]]:
    alternatives = []
    for option in event["options"]:
        if option["effects"]:
            alternatives.append({"option_id": option["option_id"], "outcome": None,
                                 "conditions": [], "branch_basis": "fixed_choice_effect",
                                 "effects": option["effects"]})
        for branch in option["conditional_branches"]:
            conditions = branch.get("conditions", [])
            if followup_states and conditions and not any(item in followup_states for item in conditions):
                continue
            alternatives.append({"option_id": option["option_id"], "outcome": branch["outcome"],
                                 "conditions": conditions, "probability": branch["probability"],
                                 "branch_basis": "prior_state_condition" if conditions else "outcome_unknown",
                                 "effects": branch["effects"]})
    return alternatives


def summarize_event_progress(
    state: Mapping[str, Any], *, reviewed_paths: Iterable[Path] = REVIEWED_PATHS,
) -> dict[str, Any]:
    """Report observed history separately from conditional, unearned references."""
    if state.get("schema_version") != VERSION:
        raise ValueError("progress version mismatch")
    events = load_reviewed_events(reviewed_paths)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for occurrence in state["occurrences"]:
        stage, owner = occurrence.get("chain_stage"), occurrence.get("owner")
        if stage and owner:
            key = (owner["owner_id_namespace"], owner["owner_id"], stage["kind"])
            groups.setdefault(key, []).append(occurrence)
    chains = []
    remaining = []
    for (namespace, owner_id, kind), occurrences in groups.items():
        observed_steps = sorted({item["chain_stage"]["step"] for item in occurrences})
        ended = any(item["chain_ended"] for item in occurrences)
        followup_states = {item["followup_state"] for item in occurrences if item["followup_state"]}
        chains.append({"owner_id_namespace": namespace, "owner_id": owner_id, "kind": kind,
                       "observed_steps": observed_steps,
                       "missing_steps": [step for step in range(1, max(observed_steps)) if step not in observed_steps],
                       "selected_followup_states": sorted(followup_states), "ended": ended,
                       "unverified_settlement_occurrences": [item["occurrence_id"] for item in occurrences
                                                          if not item["result_observed"]]})
        if ended:
            continue
        for event in events:
            stage = chain_stage(event)
            owner = event["owner"]
            if not stage or stage["kind"] != kind or stage["step"] <= max(observed_steps):
                continue
            if owner["owner_id_namespace"] != namespace or owner["owner_id"] != owner_id:
                continue
            if any(item["event_id"] == event["event_id"] for item in occurrences):
                continue
            cn_status = next((item["release_status"] for item in event["region_availability"]
                              if item["region"] == "cn"), "unknown")
            alternatives = _future_options(event, followup_states)
            remaining.append({"event_id": event["event_id"], "title": event["titles"].get("zh_cn") or event["titles"].get("ja"),
                              "owner_id": owner_id, "chain_stage": stage,
                              "trigger_conditions": event["trigger_conditions"], "trigger_status": "unknown",
                              "trigger_probability": None,
                              "effect_status": "reference_estimate" if alternatives else "unknown",
                              "effect_determinism": ("unknown" if not alternatives else "outcome_unknown"
                                                     if any(item["branch_basis"] == "outcome_unknown" for item in alternatives)
                                                     else "conditional_on_prior_state"
                                                     if any(item["branch_basis"] == "prior_state_condition" for item in alternatives)
                                                     else "fixed_if_triggered_and_selected"),
                              "cn_release_status": cn_status,
                              "source_urls": [source["source_url"] for source in event["sources"]],
                              "alternatives": alternatives})
    return {"run_id": state["run_id"], "current_turn_index": state.get("current_turn_index"),
            "current_turn_basis": state.get("current_turn_basis"),
            "latest_turn_id": state.get("latest_turn_id"),
            "confirmed_occurrences": copy.deepcopy(state["occurrences"]),
            "chains": chains, "skill_hints": list(state["skill_hints"].values()),
            "hint_increase_observations": copy.deepcopy(state["hint_increase_observations"]),
            "purchasable_skills": list(state["purchasable_skills"].values()),
            "learned_skills": list(state["learned_skills"].values()),
            "remaining_rewards": remaining, "remaining_rewards_are_guaranteed": False}
