"""Source-bound selected count-race cards; no device actions."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

import cv2
import numpy as np

from core.auto_trainer.condition_context import _NEGATIVE_CONDITION_IDS, load_explicit_condition_context
from core.auto_trainer.frame_safety import LocalRapidOCRScreenTextReader
from core.auto_trainer.race_calendar import RaceCalendar
from core.auto_trainer.race_scheduler import CountRaceEligibilityEvidence
from core.auto_trainer.route_handoff import is_route_handoff


class CountRaceCardEvidenceError(ValueError):
    """Missing or conflicting evidence, never a negative aptitude inference."""


def _file(path, digest=None):
    try:
        source = Path(path).resolve(strict=True)
        raw = source.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        raise CountRaceCardEvidenceError("card_source_unreadable") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if digest is not None and actual != digest:
        raise CountRaceCardEvidenceError("card_source_sha_mismatch")
    return source, raw, actual


def _json(path):
    source, raw, digest = _file(path)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise CountRaceCardEvidenceError("card_context_json_invalid") from exc
    if type(value) is not dict:
        raise CountRaceCardEvidenceError("card_context_json_invalid")
    return value, {"path": str(source), "sha256": digest}


def _one(rows, predicate, roi, *, minimum=.85):
    found = [row for row in rows if predicate(row[1]) and row[2] >= minimum
             and roi[0] <= row[3][0] and roi[1] <= row[3][1]
             and row[3][2] <= roi[2] and row[3][3] <= roi[3]]
    if len(found) != 1:
        raise CountRaceCardEvidenceError("card_field_missing_or_ambiguous")
    return found[0]


def _header_turn(text):
    matches = re.findall(r"高级\s*([1-9]|1[0-2])月(上|下)半月", "\n".join(text.splitlines()[:8]))
    if len(matches) != 1:
        raise CountRaceCardEvidenceError("card_history_date_unverified")
    month, half = matches[0]
    return 49 + (int(month) - 1) * 2 + int(half == "下")


def _name_rows(rows, roi):
    found = sorted((row for row in rows if row[2] >= .85
                    and roi[0] <= row[3][0] and roi[1] <= row[3][1]
                    and row[3][2] <= roi[2] and row[3][3] <= roi[3]), key=lambda row: row[3][1])
    if len(found) not in {1, 2} or any(len(row[1].splitlines()) != 1 for row in found):
        raise CountRaceCardEvidenceError("card_name_unverified")
    if len(found) == 2:
        upper, lower = (row[3] for row in found)
        if (lower[1] <= upper[1] or lower[3] <= upper[3]
            or abs((upper[0] + upper[2]) - (lower[0] + lower[2])) > max(upper[2] - upper[0], lower[2] - lower[0])
            or lower[1] - upper[3] > max(upper[3] - upper[1], lower[3] - lower[1])):
            raise CountRaceCardEvidenceError("card_name_unverified")
    return found


def parse_count_race_card(image_path, *, source_frame_sha256, calendar: RaceCalendar, regions_reader=None):
    """Read only the selected first-card identity, two stars and entry conditions.

    This is not full eligibility: condition and prior-action evidence are separate.
    Maintained localized names must match a unique current calendar course.
    """
    from core.auto_trainer.race_route import RaceRoutePlanner

    if not isinstance(source_frame_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", source_frame_sha256) is None:
        raise CountRaceCardEvidenceError("card_source_sha_required")
    source, _, digest = _file(image_path, source_frame_sha256)
    image = cv2.imread(str(source))
    if image is None or image.shape[:2] != (1280, 720) or not isinstance(calendar, RaceCalendar):
        raise CountRaceCardEvidenceError("card_layout_unsupported")
    reader = regions_reader if regions_reader is not None else LocalRapidOCRScreenTextReader()
    try:
        raw = reader.read_regions(image)
    except Exception as exc:
        raise CountRaceCardEvidenceError("card_regions_unavailable") from exc
    rows = []
    for item in raw:
        try:
            box, text, confidence = item
            if type(text) is not str or type(confidence) is bool:
                continue
            points = [[float(x), float(y)] for x, y in box]
            confidence = float(confidence)
            if len(points) != 4 or not all(math.isfinite(v) for point in points for v in point) or not 0 <= confidence <= 1:
                continue
            bounds = (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))
            rows.append(([points, text, confidence], text, confidence, bounds))
        except (ValueError, TypeError):
            continue
    exact = lambda text: lambda value: value == text
    _one(rows, exact("赛事一览"), (0, 0, 160, 40))
    date = _one(rows, lambda value: re.fullmatch(r"高级[1-9][0-2]?月[上下]半月", value) is not None, (150, 35, 400, 80))
    _one(rows, exact(date[1]), (200, 620, 520, 680))
    calendar_turn = _header_turn(date[1])
    names = _name_rows(rows, (30, 720, 250, 840))
    name_text = "".join(row[1] for row in names)
    # Osaka's name is below its separate GI badge and right of HANSHIN signage.
    banner = _name_rows(rows, (350, 310, 710, 430) if name_text == "大阪杯" else (320, 250, 710, 430))
    # Maintained UI name and inner/outer suffix; direction still comes from the calendar.
    aliases = {"川崎纪念": ("川崎記念", ""), "二月锦标赛": ("フェブラリーステークス", ""),
               "大阪杯": ("大阪杯", "·内"), "天王奖春": ("天皇賞（春）", "·外")}
    if name_text not in aliases or "".join(row[1] for row in banner) != name_text:
        raise CountRaceCardEvidenceError("card_name_unverified")
    course = _one(rows, lambda text: re.fullmatch(r".+(?:泥地|草地)[0-9]+米（.+）.+", text) is not None, (315, 715, 695, 765))
    grade = _one(rows, exact("GI"), (255, 715, 320, 765))
    badge = RaceRoutePlanner._selected_race_card_evidence(image)
    if not badge or badge["grade"] != "G1":
        raise CountRaceCardEvidenceError("card_grade_conflict")
    venues = {"kawasaki": "川崎", "tokyo": "东京", "hanshin": "阪神", "kyoto": "京都"}
    surfaces = {"dirt": "泥地", "turf": "草地"}
    distances = {"medium": "中距离", "mile": "英里", "long": "长距离"}
    directions = {"left": "逆", "right": "顺"}
    entries = [entry for entry in calendar.query_turn(calendar_turn, formal_only=False)
               if entry.grade == "G1" and entry.names.get("ja_jp") == aliases[name_text][0]
               and entry.venue in venues and entry.surface in surfaces and entry.distance_category in distances
               and entry.direction in directions
               and course[1] == f"{venues[entry.venue]}{surfaces[entry.surface]}{entry.distance_m}米（{distances[entry.distance_category]}）{directions[entry.direction]}{aliases[name_text][1]}"]
    if len(entries) != 1 or entries[0].schedule_status != "verified":
        raise CountRaceCardEvidenceError("card_calendar_identity_unverified")
    entry = entries[0]
    surface_text = surfaces[entry.surface]
    surface = _one(rows, lambda text: text in {f"★{surface_text}", f"☆{surface_text}", surface_text}, (568, 761, 662, 806), minimum=0.0)
    icon = surface
    if surface[1] == surface_text:
        icon = _one(rows, lambda text: text in {"★", "☆"}, (568, 761, 606, 806), minimum=0.0)
        if (surface[2] < .85 or icon[2] <= 0 or icon[3][2] > surface[3][0]
            or abs((icon[3][1] + icon[3][3]) - (surface[3][1] + surface[3][3])) > 12):
            raise CountRaceCardEvidenceError("card_surface_star_unverified")
    shape = RaceRoutePlanner._surface_star_icon_evidence(image, tuple(int(v) for v in icon[3]))
    distance = _one(rows, exact(f"★{distances[entry.distance_category]}"), (568, 795, 680, 838))
    if surface[2] <= 0 or shape is None:
        raise CountRaceCardEvidenceError("card_surface_star_unverified")
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    green = (hsv[:, :, 0] >= 38) & (hsv[:, :, 0] <= 92) & (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 80)
    corners = [float(np.mean(green[int(1280*y0):int(1280*y1), int(720*x0):int(720*x1)]))
               for x0, x1, y0, y1 in ((.014,.090,.539,.590),(.903,.993,.539,.590),(.014,.090,.637,.680),(.903,.993,.637,.680))]
    if not all(value > .07 for value in corners):
        raise CountRaceCardEvidenceError("card_not_selected")
    _one(rows, exact("粉丝数"), (20, 520, 110, 558))
    fans = _one(rows, lambda value: re.fullmatch(r"[1-9][0-9,]*人", value) is not None, (40, 555, 210, 595))
    fan_count = int(fans[1][:-1].replace(",", ""))
    minimum_fans = entry.entry_requirements.get("fan_count")
    _one(rows, exact("参赛"), (300, 1040, 420, 1120))
    enabled_green = float(np.mean(green[1045:1121, 219:501]))
    if type(minimum_fans) is not int or fan_count < minimum_fans or enabled_green <= .50:
        raise CountRaceCardEvidenceError("card_entry_conditions_unverified")
    return {"source_path": str(source), "source_frame_sha256": digest, "calendar_turn": calendar_turn,
        "race_id": entry.race_id, "race_name": name_text, "grade": "G1", "fan_count": fan_count,
        "aptitude_ok": True, "entry_conditions_met": True,
        "selected_card": {"source": "count_race_card.local_spatial", "race_id": entry.race_id, "grade": "G1",
            "card_bbox": [10, 689, 710, 871], "selected": True, "selected_corner_green": corners},
        "field_evidence": {"date": date[0], "name": names[0][0], "course": course[0], "grade": grade[0],
            **({"name_regions": [row[0] for row in names], "banner_regions": [row[0] for row in banner]} if len(names) > 1 else {}),
            **({"surface_star_icon_region": icon[0]} if icon is not surface else {}),
            "grade_shape": badge, "surface_star": surface[0], "surface_star_shape": shape,
            "distance_star": distance[0], "fans": fans[0], "minimum_fans": minimum_fans, "enabled_green": enabled_green}}


def _condition(context, run_session_id, observed_runner_turn):
    # The caller must first use formal_live=True with its in-memory binding key.
    # Recheck the returned context against its original files; never persist the key.
    try:
        metadata = context["metadata"]
        binding = metadata["formal_binding"]
        if binding["turn_id"] != f"{run_session_id}_turn_{observed_runner_turn:03d}" or binding["turn_index"] != observed_runner_turn:
            raise ValueError("condition_turn_mismatch")
        raw, provenance = _json(metadata["source"]["report_path"])
        original = load_explicit_condition_context(provenance["path"])
        if (original["conditions"] != context["conditions"]
            or any(original["metadata"][key] != metadata[key] for key in ("source", "provenance"))
            or any(raw["formal_binding"][key] != binding[key] for key in raw["formal_binding"])
            or any(item["condition_id"] in _NEGATIVE_CONDITION_IDS for item in context["conditions"])):
            raise ValueError("condition_context_mismatch")
        return metadata
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise CountRaceCardEvidenceError("card_condition_unverified") from exc


def _history_frame(result, action_id=None):
    plan, packet, gate = result["decision"], result["frame_safety"], result["safety_verdict"]
    _, _, digest = _file(plan["evidence"]["image"], packet["source_image_sha256"])
    if (gate["allowed"] is not True or gate["source_image_sha256"] != digest or gate["action_id"] != action_id
        or plan["reason"] != result["reason"] or plan["action"] != ("tap" if action_id is not None else "stop")
        or action_id is not None and plan["tap"]["coordinate_name"] != action_id):
        raise ValueError("history_frame_gate_mismatch")
    return digest


def validate_previous_count_race_history(ledger, *, calendar, observed_runner_turn, current_calendar_turn, paths):
    """Verify a recorded prior race and its return to main; grant no eligibility."""
    from core.auto_trainer.career_goal_ledger import CareerGoalLedgerStore, _normalize_race_result
    from core.auto_trainer.race_result_observation import build_count_race_result_receipt

    try:
        store = CareerGoalLedgerStore()
        store.verify_hash_chain(ledger)
        loaded = {key: _json(paths[key]) for key in ("race", "schedule", "result", "handoff", "event", "safe", "zero_action", "inspection")}
        data = {key: value[0] for key, value in loaded.items()}
        snapshot = data["result"]
        result = snapshot["step_reports"][-1]["result"]
        receipt = build_count_race_result_receipt(result["decision"]["evidence"]["image"],
            source_frame_sha256=result["frame_safety"]["source_image_sha256"], schedule_path=paths["schedule"],
            race_report_path=paths["result"], calendar=calendar)
        normalized = _normalize_race_result(receipt.observation)
        reservation = receipt.schedule["count_reservation"]
        if (receipt.observation.run_session_id != ledger.run_session_id
            or receipt.observation.observed_turn != observed_runner_turn - 1
            or reservation["calendar_turn"] != current_calendar_turn - 1
            or not any(r["event_kind"] == "count_race_result" and r["source_sha256"] == receipt.receipt_sha256 for r in ledger.revisions)
            or sum(dict(r) == normalized for r in ledger.race_results) != 1):
            raise ValueError("history_recorded_race_mismatch")
        turn = data["race"]["turn_reports"][-1]
        race = turn["race_route"]["report"]
        recorded = turn["count_race_result"]
        if (data["race"]["run_session_id"] != ledger.run_session_id or turn["turn_index"] != observed_runner_turn - 1
            or recorded["receipt_sha256"] != receipt.receipt_sha256 or recorded["status"] != "updated"
            or recorded["reason"] != "count_race_result_recorded"
            or race["step_reports"][:len(snapshot["step_reports"])] != snapshot["step_reports"]):
            raise ValueError("history_race_report_mismatch")
        handoff = data["handoff"]
        canonical = hashlib.sha256(json.dumps(race, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        terminal = race["step_reports"][-1]["result"]
        if (handoff["report_sha256"] != canonical or terminal["reason"] != "event_choice"
            or terminal["performed"] is not False or handoff["terminal_source_sha256"] != _history_frame(terminal)
            or handoff["before"] != turn["count_resume"]["owner_baseline"]
            or handoff["after"]["run_session_id"] != ledger.run_session_id
            or handoff["after"]["status"] != "tracking"
            or handoff["after"]["current_state"] != "event_choice" or handoff["after"]["expected_next_states"]
            or handoff["after"]["observation_sha256"] != handoff["terminal_source_sha256"]
            or handoff["after"]["transition_version"] != handoff["before"]["transition_version"] + 1):
            raise ValueError("history_handoff_mismatch")
        event_wrapper = data["event"]
        event = event_wrapper["turn_report"]["event_choice"]["report"]
        event_result, = [row["result"] for row in event["step_reports"]]
        action_id = event_result["decision"]["tap"]["coordinate_name"]
        event_sha = _history_frame(event_result, action_id)
        formal = event["formal_capture"]
        route = event["route_transition"]
        if (event_wrapper["outcome"] != [True, "max_steps_reached", 0]
            or event_wrapper["turn_report"]["turn_index"] != observed_runner_turn - 1
            or action_id not in {"event_choice.top", "event_choice.bottom"} or event_result["performed"] is not True
            or event_result["route_action_receipt_source"]["source_sha256"] != event_sha
            or formal["run_session_id"] != ledger.run_session_id
            or formal["global_action_turn"] != observed_runner_turn - 1
            or formal["source_frame_sha256"] != event_sha
            or route["run_session_id"] != ledger.run_session_id or route["status"] != "tracking"
            or route["last_action_id"] != action_id or route["current_state"] != "event_choice"
            or route["action_source_sha256"] != event_sha or route["observation_sha256"] != event_sha
            or route["transition_version"] != handoff["after"]["transition_version"] + 2):
            raise ValueError("history_event_mismatch")
        safe = data["safe"]["report"]
        zero = data["zero_action"]
        if (data["safe"]["code"] != 0 or zero["run_session_id"] != ledger.run_session_id
            or any(zero[key] != 0 for key in ("completed_action_turns", "completed_training_turns", "completed_race_turns"))
            or len(zero["turn_reports"]) != 1 or zero["turn_reports"][0]["turn_index"] != observed_runner_turn):
            raise ValueError("history_intervening_action")
        previous_version = event["route_transition"]["transition_version"]
        for report in (safe, zero["pre_route"]["advance"]["report"]):
            last, = [row["result"] for row in report["step_reports"]]
            sha = _history_frame(last)
            route = report["route_transition"]
            if (report["is_valid"] is not True or report["performed_steps"] != 0 or last["performed"] is not False
                or last["reason"] != "ready_main_menu" or _header_turn(last["frame_safety"]["visible_text"]) != current_calendar_turn
                or route["run_session_id"] != ledger.run_session_id or route["current_state"] != "main_menu"
                or route["expected_next_states"] or route["action_source_sha256"] is not None
                or route["last_action_id"] != action_id or route["observation_sha256"] != sha
                or route["transition_version"] != previous_version + 1):
                raise ValueError("history_return_main_mismatch")
            previous_version = route["transition_version"]
        inspected = data["inspection"]
        inspection = inspected["inspection"]
        if (inspected["run_session_id"] != ledger.run_session_id or inspection["inspection_only"] is not True
            or inspection["is_valid"] is not True or inspection["ledger_unchanged"] is not True
            or inspection["calendar_turn"] != current_calendar_turn or inspection["stop_reason"] != "race_inspection_complete"):
            raise ValueError("history_inspection_mismatch")
        expected = [
            ("enter_race_from_main_menu", "main_menu.race", 2, "main_menu", ["loading", "modal", "race_list"]),
            ("leave_inspected_race_list", "race_list.back", 2, "race_list", ["loading", "main_menu"]),
            ("race_inspection_complete", None, 1, "main_menu", []),
        ]
        if len(inspection["step_reports"]) != len(expected):
            raise ValueError("history_inspection_actions_unverified")
        last_action = action_id
        for step, (reason, identifier, version_increment, state, next_states) in zip(inspection["step_reports"], expected):
            value = step["result"]
            sha = _history_frame(value, identifier)
            route = value["route_transition"]
            if (value["reason"] != reason or value["performed"] is not (identifier is not None)
                or identifier is not None and (value.get("tap_performed") is not True or value["decision"]["tap"]["coordinate_name"] != identifier)
                or route["run_session_id"] != ledger.run_session_id or route["status"] != "tracking"
                or route["transition_version"] != previous_version + version_increment
                or route["current_state"] != state or route["last_action_id"] != (identifier or last_action)
                or route["observation_sha256"] != sha
                or route["action_source_sha256"] != (sha if identifier is not None else None)
                or route["expected_next_states"] != next_states):
                raise ValueError("history_inspection_actions_unverified")
            previous_version = route["transition_version"]
            if identifier is not None:
                last_action = identifier
        returned = inspection["step_reports"][-1]["result"]["route_transition"]
        if returned["run_session_id"] != ledger.run_session_id or returned["current_state"] != "main_menu" or returned["expected_next_states"]:
            raise ValueError("history_inspection_return_unverified")
        return {"previous_action_kind": "race", "previous_runner_turn": observed_runner_turn - 1,
            "previous_calendar_turn": reservation["calendar_turn"], "race_result": normalized,
            "receipt_sha256": receipt.receipt_sha256, "sources": {key: value[1] for key, value in loaded.items()},
            "returned_main": returned}
    except (KeyError, TypeError, ValueError, OSError, IndexError) as exc:
        raise CountRaceCardEvidenceError("card_previous_race_unverified") from exc


def _previous_non_race_turn_root(run_session_id, runner, calendar_turn, turn_root):
    """Verify the immediately preceding supervisor turn was a training action."""
    try:
        root = Path(turn_root).resolve(strict=True)
        if root.name != f"{run_session_id}_turn_{runner - 1:03d}":
            raise ValueError("history_turn_root_mismatch")
        decision_roots = [root / "decision", *root.glob("route_steps/*_one_turn_attempt/decision")]
        decision_roots = [path for path in decision_roots if (path / "turns/turn_000/execution.json").is_file()]
        if len(decision_roots) != 1:
            raise ValueError("history_decision_root_ambiguous")
        decision_root = decision_roots[0]
        context, context_source = _json(root / "race_schedule_state_context.json")
        state, state_source = _json(decision_root / "turns/turn_000/state.json")
        decision, decision_source = _json(decision_root / "turns/turn_000/decision.json")
        execution, execution_source = _json(decision_root / "turns/turn_000/execution.json")

        main = context["state_context"]
        anchor = main["scenario_state"]["career_calendar_anchor"]
        source = context["source"]
        _file(source["path"], source["sha256"])
        receipt = execution["route_action_receipt_source"]
        _file(receipt["source_path"], receipt["source_sha256"])
        target = decision["target"]
        checks = execution["safety_checks"]
        if (
            main["turn"] != runner - 1
            or context.get("valid_for_training") is not True
            or anchor.get("status") != "verified"
            or anchor.get("basis") != "same_frame_main_menu_header"
            or anchor.get("source_frame_sha256") != source["sha256"]
            or anchor["career_time"].get("global_turn") != calendar_turn - 1
            or state.get("turn") != runner - 1
            or decision.get("action") != "train"
            or target not in {"speed", "stamina", "power", "guts", "wisdom"}
            or execution.get("performed") is not True
            or execution.get("reason") != "executed"
            or execution.get("target") != target
            or receipt.get("action_id") != "training.execute"
            or receipt.get("action_state") != "training_menu"
            or receipt.get("coordinate_name") != f"training_menu.{target}_decide"
            or not isinstance(checks, list)
            or not checks
            or any(check.get("accepted") is not True for check in checks)
        ):
            raise ValueError("history_training_artifact_mismatch")
        return {
            "previous_action_id": "training.execute",
            "previous_runner_turn": runner - 1,
            "previous_calendar_turn": calendar_turn - 1,
            "training_turn_root": {
                "path": str(root),
                "context": context_source,
                "state": state_source,
                "decision": decision_source,
                "execution": execution_source,
            },
            "training_receipt": dict(receipt),
        }
    except (KeyError, TypeError, ValueError, OSError, IndexError) as exc:
        raise CountRaceCardEvidenceError("card_consecutive_race_unverified") from exc


def _post_training_route(transition, path, run_session_id, *, rest_pending=False):
    from core.auto_trainer.route_transition_session import RouteTransitionSession, RouteTransitionStatus
    from core.auto_trainer.safe_advance_transition import (
        SafeAdvanceRoutePolicy, action_allowed_from_state, observed_route_state, expected_next_states_for_decision)

    payload = {key: value for key, value in transition.items() if key != "schema_version"}
    payload["status"] = RouteTransitionStatus(payload["status"])
    payload["expected_next_states"] = tuple(payload["expected_next_states"])
    candidate = RouteTransitionSession(**payload)
    candidate._validate_invariants()
    if candidate.run_session_id != run_session_id or candidate.to_dict() != transition:
        raise ValueError("history_training_route_mismatch")
    wrapper, source = _json(path)
    report = wrapper["report"]
    if wrapper["code"] != 0 or report["is_valid"] is not True or report["stop_reason"] != "ready_main_menu":
        raise ValueError("history_post_training_incomplete")
    if rest_pending and (report["performed_steps"] != 0 or len(report["step_reports"]) != 1):
        raise ValueError("history_post_rest_action_unverified")
    non_turn_taps = {("confirm_wisdom_learning_prompt", "safe_advance.wisdom_learning_prompt_confirm"),
                    ("learn_wisdom_modal", "safe_advance.learn_wisdom"),
                    ("close_knowledge_table", "safe_advance.knowledge_close")}
    performed = 0
    for step in report["step_reports"]:
        result = step["result"]
        decision, packet, gate = result["decision"], result["frame_safety"], result["safety_verdict"]
        _, _, sha = _file(decision["evidence"]["image"], packet["source_image_sha256"])
        action = decision["action"]
        identifier = decision["tap"]["coordinate_name"] if action == "tap" else None
        state = observed_route_state(decision)
        policy = SafeAdvanceRoutePolicy.resolve(current_state=candidate.current_state,
            expected_next_states=candidate.expected_next_states, bootstrap_state=None,
            last_action_id=candidate.last_action_id, action_source_sha256=candidate.action_source_sha256)
        if (decision["reason"] != result["reason"] or gate["allowed"] is not True
            or gate["source_image_sha256"] != sha or gate["action_id"] != identifier
            or not policy.allows_observed_state(state) or not action_allowed_from_state(state, decision)
            or result["route_policy"] != {"allowed_observed_states": list(policy.allowed_observed_states),
                "source": policy.source, "observed_state": state, "source_image_sha256": sha,
                "pending_action_id": policy.pending_action_id, "action_source_sha256": policy.action_source_sha256}):
            raise ValueError("history_post_training_policy_mismatch")
        candidate.observe(state, source_sha256=sha)
        if action == "tap":
            if ((decision["reason"], identifier) not in non_turn_taps
                or result["performed"] is not True or result.get("tap_performed") is not True):
                raise ValueError("history_post_training_action_unverified")
        elif action == "wait":
            if (decision["reason"] != "bottom_skip_double_enabled" or decision.get("tap") is not None
                or result["performed"] is not True or result.get("tap_performed") is True):
                raise ValueError("history_post_training_wait_unverified")
            identifier = "wait:bottom_skip_double_enabled"
        elif not is_route_handoff(decision, reason="ready_main_menu") or result["performed"] is not False:
            raise ValueError("history_post_training_terminal_unverified")
        if action in {"tap", "wait"}:
            candidate.commit_action(identifier, expected_next_states=expected_next_states_for_decision(decision), source_sha256=sha)
            performed += 1
        if candidate.to_dict() != result["route_transition"]:
            raise ValueError("history_post_training_receipt_mismatch")
    pending_ok = (rest_pending and candidate.last_action_id == "safe_advance.rest_confirm"
        and list(candidate.expected_next_states) == transition["expected_next_states"]
        and candidate.action_source_sha256 == transition["action_source_sha256"])
    if (performed != report["performed_steps"] or candidate.to_dict() != report["route_transition"]
        or candidate.current_state != "main_menu" or candidate.expected_next_states and not pending_ok):
        raise ValueError("history_post_training_return_mismatch")
    return candidate, source


def _previous_non_race(run_session_id, runner, calendar_turn, training_path, invocation_path, zero_path, inspection_path,
                       post_training_route_report_path=None):
    try:
        prior, prior_source = _json(training_path)
        invocation, invocation_source = _json(invocation_path)
        zero, zero_source = _json(zero_path)
        inspected, inspection_source = _json(inspection_path)
        if any(report["run_session_id"] != run_session_id for report in (prior, zero, inspected)):
            raise ValueError("history_run_mismatch")
        if (prior["is_valid"] is not True or prior["completed_action_turns"] != 1 or prior["completed_training_turns"] != 1
            or prior["completed_race_turns"] != 0 or len(prior["turn_reports"]) != 1
            or any(zero[key] != 0 for key in ("completed_action_turns", "completed_training_turns", "completed_race_turns"))):
            raise ValueError("history_action_mismatch")
        turn = prior["turn_reports"][0]
        argv = invocation["argv"]
        arg = lambda flag: argv[argv.index(flag) + 1]
        if (turn["turn_index"] != runner - 1 or arg("--run-session-id") != run_session_id
            or int(arg("--start-turn-index")) != runner - 1 or int(arg("--max-training-turns")) != 1
            or len(zero["turn_reports"]) != 1 or zero["turn_reports"][0]["turn_index"] != runner):
            raise ValueError("history_turn_mismatch")
        loop = turn["report"]["stages"]["live_loop"]
        execution = loop["execution"]
        receipt = execution["route_action_receipt_source"]
        is_rest = loop["decision"]["action"] == "rest"
        action_id = "safe_advance.rest_confirm" if is_rest else "training.execute"
        transition = turn["rest_route_receipt" if is_rest else "training_route_receipt"]
        if (loop["decision"]["action"] not in {"train", "rest"} or execution["performed"] is not True
            or execution["reason"] != ("executed_rest_confirmed" if is_rest else "executed")
            or receipt["action_id"] != action_id or receipt["action_state"] != ("modal" if is_rest else "training_menu")
            or transition["last_action_id"] != action_id or transition["action_source_sha256"] != receipt["source_sha256"]
            or not execution["safety_checks"] or not all(check["accepted"] is True for check in execution["safety_checks"])):
            raise ValueError("history_training_unverified")
        _file(receipt["source_path"], receipt["source_sha256"])
        if is_rest:
            from core.auto_trainer.route_transition_session import POST_ACTION_ROUTE_SUCCESSORS

            decision, packet, gate = execution["decision"], execution["frame_safety"], execution["action_safety"]
            if (post_training_route_report_path is None or receipt["coordinate_name"] != action_id
                or decision["action"] != "tap" or decision["reason"] != "confirm_rest"
                or decision["tap"]["coordinate_name"] != action_id or decision["evidence"]["image"] != receipt["source_path"]
                or packet["source_image_sha256"] != receipt["source_sha256"] or gate["allowed"] is not True
                or gate["action_id"] != action_id or gate["source_image_sha256"] != receipt["source_sha256"]
                or transition["run_session_id"] != run_session_id or transition["status"] != "tracking"
                or transition["current_state"] != "modal" or transition["mismatch_state"] is not None
                or transition["observation_sha256"] != receipt["source_sha256"]
                or transition["expected_next_states"] != list(POST_ACTION_ROUTE_SUCCESSORS)):
                raise ValueError("history_rest_unverified")
        for report, expected in ((prior, calendar_turn - 1), (zero, calendar_turn)):
            result = report["pre_route"]["advance"]["report"]["step_reports"][-1]["result"]
            packet = result["frame_safety"]
            _file(result["decision"]["evidence"]["image"], packet["source_image_sha256"])
            if _header_turn(packet["visible_text"]) != expected:
                raise ValueError("history_date_mismatch")
        session = zero["pre_route"]["advance"]["report"]["route_transition"]
        replay, post_source = None, None
        if post_training_route_report_path is not None:
            replay, post_source = _post_training_route(transition, post_training_route_report_path, run_session_id,
                rest_pending=is_rest)
            zero_report = zero["pre_route"]["advance"]["report"]
            last, = [step["result"] for step in zero_report["step_reports"]]
            if zero_report["performed_steps"] != 0 or last["performed"] is not False or last["reason"] != "ready_main_menu":
                raise ValueError("history_zero_action_mismatch")
            sha = _history_frame(last)
            if is_rest:
                # The existing supervisor starts each invocation with this
                # bootstrap lifecycle; replay its recorded zero-tap receipt only.
                if (zero_report["is_valid"] is not True or zero_report["stop_reason"] != "ready_main_menu"
                    or sha == replay.observation_sha256
                    or last["route_policy"] != {"allowed_observed_states": ["main_menu"], "source": "bootstrap",
                        "observed_state": "main_menu", "source_image_sha256": sha,
                        "pending_action_id": None, "action_source_sha256": None}):
                    raise ValueError("history_rest_bootstrap_unverified")
            replay.observe("main_menu", source_sha256=sha, resync=is_rest)
            if replay.to_dict() != session or is_rest and session != last["route_transition"]:
                raise ValueError("history_zero_route_mismatch")
        if ((post_source is None and session["last_action_id"] != "training.execute")
            or session["expected_next_states"] or session["current_state"] != "main_menu"):
            raise ValueError("history_last_action_mismatch")
        inspection = inspected["inspection"]
        if (inspection["inspection_only"] is not True or inspection["is_valid"] is not True
            or inspection["ledger_unchanged"] is not True or inspection["calendar_turn"] != calendar_turn
            or inspection["stop_reason"] != "race_inspection_complete"):
            raise ValueError("history_inspection_unverified")
        allowed = {("confirm_race_recommendation_top", "safe_advance.race_recommendation_top_confirm"),
                   ("enter_race_from_main_menu", "main_menu.race"),
                   ("leave_inspected_race_list", "race_list.back")}
        taps = 0
        for step in inspection["step_reports"]:
            result = step["result"]
            decision, packet, gate = result["decision"], result["frame_safety"], result["safety_verdict"]
            _file(decision["evidence"]["image"], packet["source_image_sha256"])
            if gate["allowed"] is not True or gate["source_image_sha256"] != packet["source_image_sha256"]:
                raise ValueError("history_gate_mismatch")
            if result["performed"] is True:
                action_id = decision["tap"]["coordinate_name"]
                if (decision["action"] != "tap" or (decision["reason"], action_id) not in allowed
                    or result.get("tap_performed") is not True or gate["action_id"] != action_id):
                    raise ValueError("history_intervening_action")
                taps += 1
            elif decision["action"] != "stop" or decision["reason"] != "race_inspection_complete":
                raise ValueError("history_terminal_unverified")
            if replay is not None:
                state, successors = {"enter_race_from_main_menu": ("main_menu", ("loading", "modal", "race_list")),
                    "confirm_race_recommendation_top": ("modal", ("loading", "race_list")),
                    "leave_inspected_race_list": ("race_list", ("loading", "main_menu")),
                    "race_inspection_complete": ("main_menu", ())}[decision["reason"]]
                replay.observe(state, source_sha256=packet["source_image_sha256"])
                if decision["action"] == "tap":
                    replay.commit_action(action_id, expected_next_states=successors, source_sha256=packet["source_image_sha256"])
                if replay.to_dict() != result["route_transition"]:
                    raise ValueError("history_inspection_route_mismatch")
        if taps != inspection["tap_count"] or taps != inspection["performed_steps"]:
            raise ValueError("history_count_mismatch")
        if replay is not None:
            if not inspection["step_reports"]:
                raise ValueError("history_inspection_return_unverified")
            final = inspection["step_reports"][-1]["result"]
            if (final["reason"] != "race_inspection_complete" or final["performed"] is not False
                or replay.current_state != "main_menu" or replay.expected_next_states
                or replay.observation_sha256 != _history_frame(final)):
                raise ValueError("history_inspection_return_unverified")
        return {"previous_action_id": receipt["action_id"], "previous_calendar_turn": calendar_turn - 1,
            "previous_runner_turn": runner - 1, ("rest" if is_rest else "training"): prior_source, "invocation": invocation_source,
            "zero_action": zero_source, "inspection": inspection_source, ("rest_receipt" if is_rest else "training_receipt"): receipt,
            **({"post_training_route": post_source} if post_source is not None else {})}
    except (KeyError, TypeError, ValueError, OSError, IndexError) as exc:
        raise CountRaceCardEvidenceError("card_consecutive_race_unverified") from exc


def build_count_race_card_evidence(image_path, *, source_frame_sha256, run_session_id, observed_runner_turn,
    expected_calendar_turn, calendar, validated_condition_context, previous_training_report_path=None,
    previous_invocation_path=None, zero_action_report_path=None, inspection_report_path=None, regions_reader=None,
    post_training_route_report_path=None, previous_turn_root=None):
    card = parse_count_race_card(image_path, source_frame_sha256=source_frame_sha256,
        calendar=calendar, regions_reader=regions_reader)
    if card["calendar_turn"] != expected_calendar_turn:
        raise CountRaceCardEvidenceError("card_expected_date_mismatch")
    condition = _condition(validated_condition_context, run_session_id, observed_runner_turn)
    previous = (
        _previous_non_race_turn_root(
            run_session_id, observed_runner_turn, expected_calendar_turn, previous_turn_root,
        )
        if previous_turn_root is not None
        else _previous_non_race(
            run_session_id, observed_runner_turn, expected_calendar_turn,
            previous_training_report_path, previous_invocation_path,
            zero_action_report_path, inspection_report_path,
            post_training_route_report_path=post_training_route_report_path,
        )
    )
    return CountRaceEligibilityEvidence(run_session_id=run_session_id, calendar_turn=card["calendar_turn"],
        race_id=card["race_id"], source_path=card["source_path"], source_frame_sha256=source_frame_sha256,
        aptitude_ok=True, condition_ok=True, consecutive_race_ok=True, entry_conditions_met=True,
        evidence={"card": card, "condition": condition, "previous_non_race": previous})
