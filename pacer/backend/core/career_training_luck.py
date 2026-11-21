"""Retrospective training rolls, separate from offers and policy evaluation."""

from collections import Counter, defaultdict
from math import fsum
import re

from .career_review_evidence import ATTRIBUTES, normalize_review


SCALE = 30
_STATS = (*ATTRIBUTES, "skill_pt")
_OBS_ID = re.compile(r"(autonomous_[A-Za-z0-9_-]+)/turn_([0-9]{1,9})(?:_[A-Za-z0-9_-]+)?\Z")
_AFTER_EVENT = re.compile(r"/after/event_[0-9]+/step[1-9][0-9]*\Z")
_INJURY_TITLES = {"保重身体", "保重身體", "お大事に", "不许逞强", "不許逞強", "無茶は厳禁"}
_TRAINEE_HEADERS = {"养成优俊少女事件", "養成優駿少女事件", "养成赛马娘事件", "養成賽馬娘事件",
                    "育成赛马娘事件", "育成賽馬娘事件", "育成ウマ娘イベント"}
_RISK_UNKNOWN = {"parse_failed", "training_type", "failure_rate", "failure_rate_unreliable", "confidence_invalid"}
_GAIN_UNKNOWN = {"stats_gain", "stats_gain_partial", "stats_gain_unreliable", "stats_gain_unverified_provenance"}


def _object(value):
    return value if isinstance(value, dict) else {}


def _items(value):
    return [(i, item) for i, item in enumerate(value) if isinstance(item, dict)] if isinstance(value, list) else []


def _rate(value):
    return type(value) in (int, float) and 0 <= value <= 100


def _unknown(tile, fields):
    unknown = tile.get("unknown_fields")
    return isinstance(unknown, list) and any(isinstance(key, str) and key in fields for key in unknown)


def _observation_source(value, turn):
    source = _OBS_ID.fullmatch(value) if isinstance(value, str) else None
    return source if source and int(source[2]) == turn else None


def _selected_tile(raw, tab):
    tiles = [tile for _, tile in _items(raw.get("trainings")) if tile.get("training_type") == tab]
    if len(tiles) != 1:
        return None
    tile = tiles[0]
    risk = tile.get("failure_rate")
    basis = raw.get("training_basis")
    if (not _rate(risk) or _unknown(tile, _RISK_UNKNOWN)
            or basis not in ("decision_state", "capture_sidecar")
            or (basis == "capture_sidecar" and "failure_rate_ocr" not in tile)):
        return None
    if "numeric_evidence" in tile and _object(tile["numeric_evidence"]).get("failure_rate_verified") is not True:
        return None
    if "failure_rate_ocr" in tile:
        proof = _object(tile["failure_rate_ocr"])
        if (proof.get("status") != "accepted" or proof.get("selected_tab") != tab
                or not _rate(proof.get("value")) or proof["value"] != risk):
            return None
    return tile


def _evidence(row, pointer):
    return {**row["evidence"], "pointer": pointer}


def _explicit(document, raw, row, events, run_id):
    turn, tab, obs_id = row["turn_index"], row["target"], raw.get("observation_id")
    source = _observation_source(obs_id, turn)
    results = []
    result = _object(raw.get("training_outcome"))
    if (result.get("schema_version") == "uma.training_outcome.v1" and result.get("status") == "confirmed"
            and result.get("basis") == "observed_training_result" and result.get("run_id") == run_id
            and type(result.get("turn_index")) is int and result["turn_index"] == turn and result.get("target") == tab
            and result.get("observation_id") == obs_id and source
            and result.get("outcome") in ("success", "failure")):
        results.append((result["outcome"], "direct_result",
                        _evidence(row, row["evidence"]["pointer"] + "/training_outcome")))
    failure = _object(document.get("training_failure_event"))
    if (failure.get("run_id") == run_id and type(failure.get("action_turn")) is int and failure["action_turn"] == turn
            and failure.get("family") in ("minor", "major") and type(failure.get("selected_option")) is int
            and failure["selected_option"] in (1, 2)
            and failure.get("basis") == "executed_choice_on_known_training_failure_event"):
        results.append(("failure", "injury_event", _evidence(row, "/training_failure_event")))
    if source:
        for index, event in events:
            event_id = event.get("observation_id")
            if (event.get("performed") is not True or event.get("turn_basis") != "action_turn_from_report_folder"
                    or type(event.get("turn_index")) is not int or event["turn_index"] != turn
                    or not isinstance(event_id, str) or not event_id.startswith(obs_id + "/")
                    or not _AFTER_EVENT.fullmatch(event_id[len(obs_id):])):
                continue
            text = event.get("observed_text")
            if not isinstance(text, str):
                continue
            lines = [re.sub(r"\s+", "", line).rstrip("!！") for line in text.splitlines() if line.strip()]
            titles = {right for left, right in zip(lines, lines[1:]) if left in _TRAINEE_HEADERS and right in _INJURY_TITLES}
            if len(titles) == 1:
                results.append(("failure", "injury_event", {
                    **_evidence(row, f"/event_choices/{index}"), "observation_id": event_id}))
    return results


def _date_ordinal(date):
    return (date["year"] * 12 + date["month"]) * 2 + (date["half"] == "second") if date else None


def _positive_gains(tile, tab, *, exact):
    gains = _object(tile.get("stats_gain"))
    if (not all(type(gains.get(k, 0)) is int and 0 <= gains.get(k, 0) <= 10_000 for k in _STATS)
            or not gains.get(tab) or not gains.get("skill_pt") or _unknown(tile, _GAIN_UNKNOWN)
            or tile.get("gain_row_diagnostics")):
        return None
    if "numeric_evidence" in tile:
        proof = _object(tile["numeric_evidence"])
        verified = _object(proof.get("verified_gains"))
        required = _STATS if exact else (tab, "skill_pt")
        if (proof.get("usable") is not True
                or any(type(verified.get(k)) is not int or verified[k] != gains.get(k, 0) for k in required)):
            return None
    return tuple(gains.get(k, 0) for k in _STATS)


def _adjacent(raw, row, tile, next_rows, blocked_turns):
    turn = row["turn_index"]
    source = _observation_source(raw.get("observation_id"), turn)
    # Match the immediate next calendar observation in the same uninterrupted
    # segment; never compare pre-debut dates, reselection, or cross-segment state.
    if not source or turn in blocked_turns or len(next_rows) != 1:
        return None
    after_raw, after = next_rows[0]
    destination = _observation_source(after_raw.get("observation_id"), turn + 1)
    current_day, next_day = _date_ordinal(row["date"]), _date_ordinal(after["date"])
    if not destination or destination[1] != source[1] or current_day is None or next_day != current_day + 1:
        return None
    if any(_object(entry.get("state_before")).get("turn") not in (None, expected)
           for entry, expected in ((raw, turn), (after_raw, turn + 1))):
        return None
    before, later = (tuple(_object(_object(entry.get("state_before")).get("stats")).get(k) for k in _STATS)
                     for entry in (raw, after_raw))
    if not all(type(value) is int and value >= 0 for value in (*before, *later)):
        return None
    delta = tuple(y - x for x, y in zip(before, later))
    if delta == _positive_gains(tile, row["target"], exact=True):
        return "success", "next_turn_exact_gain", after["evidence"]
    if (row["target"] == "wisdom" and tile["failure_rate"] > 0 and all(value == 0 for value in delta)
            and _positive_gains(tile, "wisdom", exact=False) is not None):
        return "failure", "wisdom_next_turn_no_gain", after["evidence"]
    return None


def _blocked_turns(document, choices):
    if not all(isinstance(document.get(field), list) for field in ("event_choices", "event_history", "run_segments")):
        return None  # No bounded adjacent inference without all three channels.
    if any(issue.get("file") in ("report.json", "pipeline_report.json", "event_progress.json",
                                 "summary.json", "summary.final.json")
           for _, issue in _items(_object(document.get("coverage")).get("issues"))):
        return None
    blocked = set()
    for _, event in choices:
        turn = event.get("turn_index")
        event_id = event.get("observation_id")
        source_id = event_id.rsplit("/", 3)[0] if isinstance(event_id, str) else None
        source = _OBS_ID.fullmatch(source_id) if source_id is not None else None
        if source and (type(turn) is not int or int(source[2]) != turn):
            source_turn = int(source[2])
            blocked.update((source_turn, source_turn - 1))
        elif not source and type(turn) is not int:
            return None
        if type(turn) is int:
            if not isinstance(event_id, str) or "/before/" not in event_id:
                blocked.add(turn)
            if not isinstance(event_id, str) or "/after/" not in event_id:
                blocked.add(turn - 1)
    for _, event in _items(document["event_history"]):
        turn, phase = event.get("turn_index"), event.get("phase")
        if type(turn) is not int or event.get("turn_basis", "action_turn") != "action_turn":
            return None
        if phase != "before":
            blocked.add(turn)
        if phase != "after":
            blocked.add(turn - 1)
    for _, segment in _items(document["run_segments"]):
        if segment.get("purchase_decisions_truncated") is True:
            return None
        for field in ("race_reports", "adaptation_reports", "skill_reports", "purchase_decisions"):
            for _, report in _items(segment.get(field)):
                if field == "purchase_decisions" and report.get("upgraded") is not True:
                    continue
                turn = report.get("turn_index", report.get("race_turn", report.get("turn")))
                if type(turn) is int:
                    blocked.update((turn, turn - 1))
                elif field in ("skill_reports", "purchase_decisions"):
                    return None
    return blocked


def summarize_training_luck(document, side, normalized=None):
    normalized = normalized if normalized is not None else normalize_review(document, side)
    run_id = normalized["run"]["id"]
    raw_rows = dict(_items(document.get("observations")))
    observations, attempts, consumed = defaultdict(list), defaultdict(list), Counter()
    missing_turn = 0
    for row in normalized["rows"]:
        pointer = row["evidence"]["pointer"]
        if row["state_basis"] != "before_action" or not pointer.startswith("/observations/"):
            continue
        raw = raw_rows[int(pointer.rsplit("/", 1)[-1])]
        turn = row["turn_index"]
        if turn is not None:
            observations[turn].append((raw, row))
            if row["consumed"] is True:
                consumed[turn] += 1
        if (row["execution_state"] == "executed" and row["action"] == "train" and row["consumed"] is True
                and raw.get("kind") == "train"):
            if turn is None:
                missing_turn += 1
            else:
                attempts[turn].append((raw, row))

    choices = _items(document.get("event_choices"))
    blocked = _blocked_turns(document, choices)
    events, reasons = [], Counter()
    if missing_turn:
        reasons["missing_turn"] = missing_turn
    for turn, rows in sorted(attempts.items()):
        if len(rows) != 1 or consumed[turn] != 1:
            reasons["ambiguous_action"] += 1
            continue
        raw, row = rows[0]
        tile = _selected_tile(raw, row["target"]) if row["target"] in ATTRIBUTES else None
        if tile is None:
            reasons["risk_unverified"] += 1
            continue
        explicit = _explicit(document, raw, row, choices, run_id)
        if len({outcome for outcome, _, _ in explicit}) > 1:
            reasons["conflicting_outcome"] += 1
            continue
        outcome = explicit[0] if explicit else (
            _adjacent(raw, row, tile, observations.get(turn + 1, []), blocked) if blocked is not None else None)
        if outcome is None:
            reasons["outcome_unverified"] += 1
            continue
        status, basis, evidence = outcome
        risk = tile["failure_rate"]
        points = round(SCALE * (risk - (100 if status == "failure" else 0)) / 100, 2)
        events.append(dict(turn_index=turn, date_label=row["date"]["label"] if row["date"] else None,
                           tab=row["target"], failure_rate_percent=risk, outcome=status, basis=basis,
                           points=points, training_evidence=row["evidence"], outcome_evidence=evidence))
    count = len(attempts) + missing_turn
    successes = sum(item["outcome"] == "success" for item in events)
    return dict(status="recorded" if events else "insufficient_evidence",
                score=round(fsum(item["points"] for item in events), 2) if events else None,
                recorded_trainings=count, scored_trainings=len(events),
                successes=successes, failures=len(events) - successes, unscored=count - len(events),
                unscored_reasons=dict(reasons), events=events)
