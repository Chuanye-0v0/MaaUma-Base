"""Project one exported career into an offline, source-addressable evidence view."""

import math
import re


SCHEMA = "uma.career_review.v1"
ATTRIBUTES = ("speed", "stamina", "power", "guts", "wisdom")
STAT_FIELDS = (*ATTRIBUTES, "skill_pt", "vitality")
_ACTIONS = {
    "train": "train", "rest": "rest", "outing": "outing", "normal_outing": "outing",
    "ss": "ss", "sss": "sss", "race": "race", "fan_race": "race", "clinic": "medical",
    "adaptation": "adaptation",
}
_RECEIPT_REASONS = {
    "executed": "train", "executed_rest_confirmed": "rest", "executed_normal_outing": "outing",
    "executed_mei_outing": "outing", "fan_shortfall_entry_consumed": "race",
}
_MANUAL_REASONS = {
    "manual_first_arc_skill_handoff": "首次凯旋门赛前手动接管",
    "manual_final_arc_finish_handoff": "最终凯旋门手动接管",
    "manual_career_date_handoff": "预设日期手动接管",
    "user_stop": "用户手动停止",
}
_TECHNICAL_REASONS = {
    "execution_exception", "runner_heartbeat_failed", "capture_error", "capture_failed",
    "capture_timeout", "career_transition_no_progress_timeout", "event_selection_transition_timeout",
    "training_entry_no_progress_timeout", "partial_execution_requires_review",
    "larc_ss_receipt_persistence_failed", "race_owner_handoff_unverified",
}
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
_REASON = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_REVISION = re.compile(r"[0-9]+(?:\.[0-9]+){1,4}\Z")
_CREDENTIAL = re.compile(r"(?i)(?:gh[pousr]_|github_pat_|sk-|(?:api[_ -]?key|password|secret|token|authorization)\s*[:=])")
_OBSERVATION_ID = re.compile(r"autonomous_[A-Za-z0-9_-]{1,130}/turn_[0-9]+(?:_[A-Za-z0-9_-]+)?\Z")


def _object(value):
    return value if isinstance(value, dict) else {}


def _objects(value):
    if isinstance(value, list):
        yield from ((index, item) for index, item in enumerate(value) if isinstance(item, dict))


def _text(value, pattern=_TOKEN):
    if (isinstance(value, str) and pattern.fullmatch(value) and not _CREDENTIAL.search(value)
            and not re.match(r"[A-Za-z]:", value)):
        return value
    return None


def _name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 100 or _CREDENTIAL.search(value):
        return None
    return value if all(c.isalnum() or c in " _-.·・！!？?（）()【】[]『』「」〜～☆★'’" for c in value) else None


def _integer(value, lower=0, upper=2**53 - 1):
    return value if type(value) is int and lower <= value <= upper else None


def _boolean(value):
    return value if type(value) is bool else None


def _id(value):
    return str(value) if _integer(value, 1) is not None else _text(value)


def _number(value):
    if type(value) is float and math.isfinite(value) and 0 <= value <= 2**53 - 1:
        return value
    return _integer(value)


def _date(value):
    # .5.33 exports a flattened period value. A wrapped reading is only usable
    # if it does not explicitly deny verification; no dates are computed from turns.
    value = _object(value)
    if ("status" in value and value["status"] != "verified") or value.get("verified") is False:
        return None
    if "value" in value:
        return _date(value["value"]) if isinstance(value["value"], dict) and "value" not in value["value"] else None
    if value.get("pre_debut") is True:
        return None
    year, month, half = _integer(value.get("year_index"), 1, 3), _integer(value.get("month"), 1, 12), value.get("half")
    if year is None or month is None or half not in ("first", "second"):
        return None
    return {"key": f"{year}-{month:02}-{half}", "label": f"第{year}年{month}月{'上' if half == 'first' else '下'}半",
            "year": year, "month": month, "half": half}


def _state_date(state):
    if any(key in state and state[key] != "verified" for key in ("period_status", "date_status")):
        return None
    return _date(state.get("period"))


def _route_date(report):
    if any(key in report and report[key] != "verified" for key in ("period_status", "date_status")):
        return None
    return _date(report.get("period", report.get("date", report)))


def _state(state):
    stats = _object(state.get("stats"))
    result = {key: _number(stats.get(key)) for key in STAT_FIELDS}
    result["vitality"] = _number(state.get("vitality"))
    return result


def _evidence(side, pointer, source):
    raw = source.get("observation_id")
    observation_id = raw if isinstance(raw, str) and _OBSERVATION_ID.fullmatch(raw) else _text(raw)
    return {"side": side, "pointer": pointer, "observation_id": observation_id}


def _row(evidence, date, turn, kind, *, action=None, target=None, execution_state="unknown",
         consumed=None, state=None, state_basis=None, **fields):
    return {"date": date, "turn_index": turn, "kind": kind, "action": action, "target": target,
            "execution_state": execution_state, "consumed": consumed, "state": _state(state or {}),
            "state_basis": state_basis, "evidence": evidence, **fields}


def _action(value):
    return _ACTIONS.get(value.lower()) if isinstance(value, str) else None


def _target(action, value):
    if action == "train":
        return value if value in ATTRIBUTES else None
    if action == "outing":
        return value if isinstance(value, str) and re.fullmatch(r"normal|satake_mei:[1-5]", value) else None
    if action in ("race", "adaptation"):
        return _text(value)
    return None


def _observation(source, evidence):
    state = _object(source.get("state_before"))
    selected, execution = _object(source.get("selected")), _object(source.get("execution"))
    picked, kind = _action(selected.get("action")), _action(source.get("kind"))
    reason = _text(execution.get("reason"), _REASON)
    performed, tapped = _boolean(execution.get("performed")), _boolean(execution.get("tap_performed"))
    consumed = _boolean(source.get("consumed"))
    actual = _RECEIPT_REASONS.get(reason) if performed is True else None
    if kind in ("ss", "sss") and tapped is True and consumed is True and performed is not False and actual is None:
        # The exported SS receipt only retains tap_performed from the bound
        # SS action receipt; kind + persisted consumption disambiguates it.
        actual = kind
    if actual is not None and ((kind is not None and kind != actual) or tapped is False):
        actual = None
    if actual is not None:
        target = _target(actual, execution.get("target"))
        if actual == "outing" and reason == "executed_normal_outing":
            target = "normal"
        action, execution_state = actual, "executed"
    else:
        action = picked or kind
        target = _target(action, selected.get("target")) if picked else None
        execution_state = ("planned" if picked and consumed is not True and performed is not True
                           and tapped is not True and kind is None else "unknown")
    return _row(evidence, _state_date(state), _integer(source.get("turn_index"), 1), kind or action or "observation",
                action=action, target=target, execution_state=execution_state, consumed=consumed,
                state=state, state_basis="before_action", selected_action=picked,
                selected_target=_target(picked, selected.get("target")), execution_reason=reason)


def _merge_ss_receipts(rows, matches, side, run_id):
    observed = {}
    for row in rows:
        if row["execution_state"] == "executed" and row["action"] in ("ss", "sss"):
            row["ss_subtype"] = "sss" if row["action"] == "sss" else None
            if row["turn_index"] is not None:
                observed.setdefault(row["turn_index"], []).append(row)

    related = {}
    for index, match in _objects(matches):
        if match.get("tap_performed") is not True or any(
                match.get(key) is not None and _id(match[key]) != run_id for key in ("run_id", "run_session_id")):
            continue
        turn = _integer(match.get("turn_index"), 1)
        kind = match.get("kind")
        subtype = kind.lower() if kind in ("SS", "SSS") else None
        evidence = _evidence(side, f"/ss_matches/{index}", match)
        if turn not in observed:
            # Without a confirmed observation, the receipt owns its own
            # date (normally null); a matching action index is not a date.
            rows.append(_row(evidence, _route_date(match), turn, subtype or "ss", action=subtype or "ss",
                             execution_state="executed", ss_subtype=subtype))
        elif subtype is not None:
            related.setdefault(turn, []).append((subtype, evidence))

    for turn, receipts in related.items():
        if len(observed[turn]) != 1:
            continue
        row = observed[turn][0]
        subtypes = {subtype for subtype, _ in receipts}
        if row["ss_subtype"] is not None:
            subtypes.add(row["ss_subtype"])
        if len(subtypes) == 1:
            subtype = receipts[0][0]
            row.update(action=subtype, ss_subtype=subtype, related_evidence=[evidence for _, evidence in receipts])
        else:
            row.update(action="ss", ss_subtype=None)


def _note(kind, title, text, evidence, date=None):
    return {"kind": kind, "title": title, "text": text, "date": date, "evidence": evidence}


def _stop_note(record, evidence, diagnostic=None):
    status = record.get("status")
    reason = _text(record.get("stop_reason"), _REASON)
    if status == "completed" and record.get("career_complete") is True:
        return _note("game_outcome", "育成已结算", "记录确认育成结束；这不表示任何一场比赛的名次。", evidence)
    if status not in ("stopped", "handoff", "failed", "error", "aborted"):
        return None
    if reason in _MANUAL_REASONS:
        return _note("manual_handoff", _MANUAL_REASONS[reason], f"记录原因：{reason}。", evidence)
    if status != "handoff" and diagnostic and reason and diagnostic.get("reason") == reason:
        wait = _object(diagnostic.get("technical_wait"))
        if wait.get("schema_version") == "uma.technical_wait_stop.v1" and wait.get("reason") == reason:
            return _note("technical_stop", "技术等待中断", f"记录原因：{reason}。",
                         {**evidence, "pointer": "/stop_diagnostic/technical_wait"})
        capture = _object(diagnostic.get("capture_failure"))
        capture_reason = _text(capture.get("reason"), _REASON)
        if capture.get("schema_version") == "uma.capture_failure.v1" and capture_reason:
            return _note("technical_stop", "画面采集故障", f"采集诊断：{capture_reason}。",
                         {**evidence, "pointer": "/stop_diagnostic/capture_failure"})
    if status != "handoff" and (reason in _TECHNICAL_REASONS or (reason or "").startswith("terminal_settlement_stopped:")):
        return _note("technical_stop", "技术中断", f"记录原因：{reason}。", evidence)
    text = f"记录原因为 {reason}；未确认停机类别或游戏结果。" if reason else "未确认停机类别或游戏结果。"
    return _note("missing_evidence", "中止或移交原因未确认", text, evidence)


def _outcome_notes(document, side, run_id):
    failure = _object(document.get("training_failure_event"))
    family = failure.get("family")
    if (failure.get("basis") == "executed_choice_on_known_training_failure_event" and family in ("minor", "major")
            and failure.get("run_id") in (None, run_id)
            and _integer(failure.get("selected_option"), 1, 2) is not None):
        yield _note("game_outcome", "已确认训练失败事件", "有与已执行事件选项绑定的训练失败记录。",
                    _evidence(side, "/training_failure_event", failure), _route_date(failure))
    for index, event in _objects(document.get("event_history")):
        if event.get("result_observed") is True:
            title = _name(event.get("title"))
            yield _note("game_outcome", "已记录事件结果", f"事件：{title}。" if title else "记录确认观察到事件结果。",
                        _evidence(side, f"/event_history/{index}", event), _route_date(event))
    unlock = _object(_object(document.get("mei_outing")).get("unlock_event"))
    result = _object(unlock.get("result_observation"))
    if result.get("status") == "accepted" and result.get("result_observed") is True:
        yield _note("game_outcome", "已记录黄帽解锁事件结果", "解锁事件含已接受的游戏结果观测。",
                    _evidence(side, "/mei_outing/unlock_event/result_observation", result), _route_date(result))
    resolution = _object(_object(document.get("supporter_deadline_failure")).get("resolution"))
    if resolution.get("status") == "unreachable":
        yield _note("missing_evidence", "支持力目标预测未能达到", "记录的是路线可达性判定，不能当作游戏已结算失败。",
                    _evidence(side, "/supporter_deadline_failure/resolution", resolution))


def normalize_review(document, side):
    """Normalize the v1 document already in memory without interpreting source paths."""
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA:
        raise ValueError("仅支持 uma.career_review.v1 育成复盘。")
    if side not in ("left", "right"):
        raise ValueError("复盘来源必须是左侧或右侧。")
    raw_run = document.get("run")
    if not isinstance(raw_run, dict) or not (run_id := _id(raw_run.get("run_id"))):
        raise ValueError("育成复盘缺少有效的 run.run_id。")
    character = _object(raw_run.get("character"))
    revision = _object(document.get("strategy_identity")).get("revision")
    policy_values = raw_run.get("observed_policy_versions")
    policies = sorted({version for value in policy_values if (version := _text(value))}) if isinstance(policy_values, list) else []
    profile_status = _text(document.get("support_profile_status"))
    run = {"id": run_id, "mode": _text(raw_run.get("mode")), "character_id": _id(character.get("character_id")),
           "character_name": _name(character.get("character_name")), "scenario": _text(_object(document.get("goal")).get("scenario")),
           "strategy_revision": _text(revision, _REVISION), "policy_versions": policies,
           "status": _text(raw_run.get("status")), "career_complete": _boolean(raw_run.get("career_complete")),
           "partial_snapshot": _boolean(raw_run.get("partial_snapshot")), "support_profile_status": profile_status}
    supports = [{"slot": _integer(card.get("slot"), 0, 5), "card_id": _id(card.get("card_id")),
                 "level": _integer(card.get("level"), 1, 50), "limit_breaks": _integer(card.get("limit_breaks"), 0, 4)}
                for _, card in _objects(document.get("supports"))] if profile_status in (None, "recorded") else []

    rows = [_observation(row, _evidence(side, f"/observations/{index}", row))
            for index, row in _objects(document.get("observations"))]
    observation_count = len(rows)
    dated_observations = sum(row["date"] is not None for row in rows)
    _merge_ss_receipts(rows, document.get("ss_matches"), side, run_id)

    notes = list(_outcome_notes(document, side, run_id))
    diagnostic = _object(document.get("stop_diagnostic"))
    current_stop = _stop_note(raw_run, _evidence(side, "/run", raw_run), diagnostic)
    if current_stop:
        notes.append(current_stop)
    segments = list(_objects(document.get("run_segments")))
    for index, segment in segments:
        segment_pointer = f"/run_segments/{index}"
        # The export copies the final segment's status into run. Older
        # segments with the same reason are separate observations.
        if index != segments[-1][0] or (segment.get("status"), segment.get("stop_reason")) != (raw_run.get("status"), raw_run.get("stop_reason")):
            segment_stop = _stop_note(segment, _evidence(side, segment_pointer, segment))
            if segment_stop:
                notes.append(segment_stop)
        for report_index, race in _objects(segment.get("race_reports")):
            evidence = _evidence(side, f"{segment_pointer}/race_reports/{report_index}", race)
            rows.append(_row(evidence, _route_date(race), _integer(race.get("race_turn"), 1), "race", action="race",
                             target=_target("race", race.get("target")), consumed=_boolean(race.get("calendar_turn_consumed")),
                             execution_state="executed" if race.get("entry_confirmed_here") is True else "unknown",
                             execution_reason=_text(race.get("reason"), _REASON)))

    latest = _object(document.get("latest_observed_state"))
    latest_date = _state_date(latest)
    if latest_date is not None or any(value is not None for value in _state(latest).values()):
        rows.append(_row(_evidence(side, "/latest_observed_state", latest), latest_date,
                         _integer(latest.get("turn_index", latest.get("turn")), 1), "latest_state",
                         state=latest, state_basis="latest_observed_state"))

    raw_coverage = _object(document.get("coverage"))
    issues = []
    for index, issue in _objects(raw_coverage.get("issues")):
        reason = _text(issue.get("reason"), _REASON)
        evidence = _evidence(side, f"/coverage/issues/{index}", issue)
        issues.append({"reason": reason, "evidence": evidence})
        notes.append(_note("missing_evidence", "导出记录不完整",
                           f"导出记录原因：{reason}。" if reason else "导出标记了未说明的记录缺口。", evidence))
    coverage = {key: _integer(raw_coverage.get(key)) for key in ("training_observations", "five_tab_observations",
                "recorded_consumed_actions", "event_choices", "ss_receipts")}
    coverage.update(rows=len(rows), dated_rows=sum(row["date"] is not None for row in rows),
                    undated_rows=sum(row["date"] is None for row in rows),
                    executed_rows=sum(row["execution_state"] == "executed" for row in rows),
                    planned_rows=sum(row["execution_state"] == "planned" for row in rows),
                    unknown_execution_rows=sum(row["execution_state"] == "unknown" for row in rows),
                    observations=observation_count, dated_observations=dated_observations, issues=issues)
    return {"run": run, "supports": supports, "rows": rows, "notes": notes, "coverage": coverage}
