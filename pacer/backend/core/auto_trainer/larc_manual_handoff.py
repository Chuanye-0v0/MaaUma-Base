"""Optional game-calendar pauses using the owning run's existing controller.

Initial settings and mutable current-career plans are separate. Old 0.4.9
options/state files retain their old shapes; date rules use new files. This
module never captures, inputs to a device, or guesses dates from turn numbers.
"""
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

from core.auto_trainer.cut_farming import atomic_json
from core.auto_trainer import runtime_control

OPTIONS = "session/larc_manual_handoff_options.json"
STATE = "session/larc_manual_handoff_state.json"
PLAN = "session/larc_manual_pause_plan.json"
PAUSE_STATE = "session/larc_manual_pause_state.json"
OPTIONS_VERSION = "larc_manual_handoff_options.v1"
STATE_VERSION = "larc_manual_handoff_state.v1"
PLAN_VERSION = "larc_manual_pause_plan.v1"
PAUSE_STATE_VERSION = "larc_manual_pause_state.v1"
REASONS = {"first_arc": "manual_first_arc_skill_handoff",
           "final_arc": "manual_final_arc_finish_handoff",
           "date": "manual_career_date_handoff"}
_LOCKS = {}
_LOCKS_LOCK = threading.Lock()


def _lock(root):
    # Host edits and worker observations share this module in one process.
    key = str(Path(root).resolve()).casefold()
    with _LOCKS_LOCK:
        return _LOCKS.setdefault(key, threading.RLock())


def _date(value):
    if not isinstance(value, dict):
        return None
    year, month, half = value.get("year_index"), value.get("month"), value.get("half")
    if (type(year) is not int or year not in (1, 2, 3)
            or type(month) is not int or not 1 <= month <= 12
            or half not in ("first", "second")):
        return None
    return {"year_index": year, "month": month, "half": half}


def _date_key(value):
    return f'{value["year_index"]}-{value["month"]:02d}-{value["half"]}'


def _date_order(value):
    return (value["year_index"], value["month"], value["half"] == "second")


def normalize(value=None):
    """Normalize the two 0.4.9 switches plus game-calendar rules."""
    result = {"first_arc": False, "final_arc": False, "dates": []}
    if value is None:
        return result
    if not isinstance(value, dict) or set(value) - set(result):
        raise ValueError("manual_handoff_options_invalid")
    for name in ("first_arc", "final_arc"):
        if name in value:
            if type(value[name]) is not bool:
                raise ValueError("manual_handoff_option_must_be_boolean")
            result[name] = value[name]
    rules = value.get("dates", [])
    if not isinstance(rules, list):
        raise ValueError("manual_handoff_dates_invalid")
    ids = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - {"id", "year_index", "month", "half", "enabled"}:
            raise ValueError("manual_handoff_date_rule_invalid")
        date = _date(rule)
        if date is None or type(rule.get("enabled", True)) is not bool:
            raise ValueError("manual_handoff_date_rule_invalid")
        rule_id = rule.get("id", "date-" + _date_key(date))
        if not isinstance(rule_id, str) or not rule_id.strip() or rule_id in ids:
            raise ValueError("manual_handoff_date_rule_id_invalid")
        ids.add(rule_id)
        result["dates"].append({"id": rule_id, **date, "enabled": rule.get("enabled", True)})
    return result


def _read(path):
    target = Path(path)
    delays = (0.05, 0.06, 0.08, 0.10, None)
    for attempt, delay in enumerate(delays):
        try:
            return json.loads(target.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            # Windows atomic os.replace can briefly hide the target path.
            if attempt >= 1 or delay is None:
                raise
            time.sleep(delay)
        except (PermissionError, OSError, json.JSONDecodeError):
            if delay is None:
                raise
            time.sleep(delay)


def _binding(root):
    root = Path(root).resolve()
    progress = _read(root / "progress.json")
    run_id, device = progress.get("run_session_id"), progress.get("device")
    if run_id != root.name or not isinstance(device, str) or not device:
        raise ValueError("manual_handoff_run_or_device_mismatch")
    return root, run_id, device


def _validate_record(record, run_id, device, schema):
    if (not isinstance(record, dict) or record.get("schema_version") != schema
            or record.get("run_session_id") != run_id or record.get("device") != device):
        raise ValueError("manual_handoff_run_or_device_mismatch")


def _legacy_options(root, run_id, device):
    path = root / OPTIONS
    if not path.is_file():
        return normalize()
    saved = _read(path)
    _validate_record(saved, run_id, device, OPTIONS_VERSION)
    return normalize(saved.get("config"))


def _load_plan(root, run_id, device):
    path = root / PLAN
    if not path.is_file():
        config = _legacy_options(root, run_id, device)
        return {"schema_version": PLAN_VERSION, "run_session_id": run_id, "device": device,
                "revision": 1, "config": config, "initial_config": deepcopy(config),
                "observed_date": None, "date_status": "unknown", "receipts": {}}
    plan = _read(path)
    _validate_record(plan, run_id, device, PLAN_VERSION)
    if type(plan.get("revision")) is not int or plan["revision"] < 1:
        raise ValueError("manual_handoff_plan_invalid")
    plan["config"] = normalize(plan.get("config"))
    plan["initial_config"] = normalize(plan.get("initial_config"))
    if (plan.get("date_status") not in ("verified", "unknown")
            or (plan.get("observed_date") is not None and _date(plan["observed_date"]) is None)
            or not isinstance(plan.get("receipts", {}), dict)):
        raise ValueError("manual_handoff_plan_invalid")
    plan.setdefault("receipts", {})
    return plan


def save_options(root, value):
    """Persist initial settings, not runtime edits; keep 0.4.9 files readable."""
    config = normalize(value)
    with _lock(root):
        root, run_id, device = _binding(root)
        path = root / OPTIONS
        if path.is_file():
            if load_options(root) != config:
                raise ValueError("manual_handoff_options_immutable")
            return config
        existing_plan = _load_plan(root, run_id, device)
        if (root / PLAN).is_file() and existing_plan["initial_config"] != config:
            raise ValueError("manual_handoff_options_immutable")
        atomic_json(path, {"schema_version": OPTIONS_VERSION, "run_session_id": run_id,
            "device": device, "config": {name: config[name] for name in ("first_arc", "final_arc")}})
        existing_plan.update(initial_config=deepcopy(config))
        if not (root / PLAN).is_file():
            existing_plan["config"] = deepcopy(config)
        atomic_json(root / PLAN, existing_plan)
        return config


def load_options(root):
    """Read the immutable initial preset, including dates in the new file."""
    with _lock(root):
        if not (Path(root) / OPTIONS).is_file() and not (Path(root) / PLAN).is_file():
            return normalize()
        root, run_id, device = _binding(root)
        initial = _legacy_options(root, run_id, device)
        if (root / PLAN).is_file():
            initial = _load_plan(root, run_id, device)["initial_config"]
        return deepcopy(initial)


def _load_state(root, run_id, device):
    path, version = Path(root) / PAUSE_STATE, PAUSE_STATE_VERSION
    if not path.is_file():
        path, version = Path(root) / STATE, STATE_VERSION
    if not path.is_file():
        return None
    state = _read(path)
    _validate_record(state, run_id, device, version)
    if (state.get("stage"), state.get("status")) not in {
            ("first_arc", "waiting"), ("first_arc", "acknowledged"),
            ("date", "waiting"), ("date", "acknowledged"), ("final_arc", "handed_off")}:
        raise ValueError("manual_handoff_state_invalid")
    if (not isinstance(state.get("acknowledged_stages"), list)
            or any(stage != "first_arc" for stage in state["acknowledged_stages"])):
        raise ValueError("manual_handoff_state_invalid")
    if not isinstance(state.get("acknowledged_dates", []), list):
        raise ValueError("manual_handoff_state_invalid")
    return state


def _save_state(root, state):
    current = {**deepcopy(state), "schema_version": PAUSE_STATE_VERSION}
    atomic_json(root / PAUSE_STATE, current)
    if state["stage"] in ("first_arc", "final_arc"):
        # Old readers ignore date-only pauses, but can still show old shortcuts.
        fields = ("run_session_id", "device", "stage", "status", "reason", "period", "source",
                  "target_id", "observed_at", "acknowledged_at", "acknowledged_stages", "input_performed")
        legacy = {key: deepcopy(state[key]) for key in fields if key in state}
        legacy["schema_version"] = STATE_VERSION
        legacy["stages"] = {
            key: {name: deepcopy(value[name]) for name in fields if name in value}
            for key, value in state.get("stages", {}).items() if key in ("first_arc", "final_arc")}
        atomic_json(root / STATE, legacy)


def _intent(state):
    saved = state.get("intent") if state else None
    value = saved.get("first_arc_manual_skills") if isinstance(saved, dict) else None
    # Legacy first-Arc state itself proves its purpose; a calendar date or a
    # later plan edit does not. Never infer old date-only intent from config.
    if type(value) is not bool:
        value = bool(state and state.get("stage") == "first_arc")
    return {"first_arc_manual_skills": value}


def _acknowledged_intents(state):
    saved = state.get("acknowledged_intents") if state else None
    value = isinstance(saved, dict) and saved.get("first_arc_manual_skills") is True
    legacy_ack = bool(state and "first_arc" in state.get("acknowledged_stages", []))
    return {"first_arc_manual_skills": value or legacy_ack}


def _ack_dates(state):
    values = list(state.get("acknowledged_dates", [])) if state else []
    if state and "first_arc" in state["acknowledged_stages"] and "2-10-first" not in values:
        values.append("2-10-first")
    return values


def _state_date(state):
    if not state:
        return None
    return _date(state.get("current_date")) or _observed_date(state.get("period"))


def _rules(plan, state):
    result = []
    observed = plan.get("observed_date")
    acked = _ack_dates(state)
    waiting_date = _state_date(state) if state and state["status"] == "waiting" else None
    for rule in plan["config"]["dates"]:
        key = _date_key(rule)
        if not rule["enabled"]:
            status = "disabled"
        elif key in acked:
            status = "acknowledged"
        elif waiting_date and key == _date_key(waiting_date):
            status = "waiting"
        elif observed and _date_order(rule) < _date_order(observed):
            status = "missed"
        elif plan["date_status"] == "unknown":
            status = "date_unknown"
        else:
            status = "pending"
        result.append({**deepcopy(rule), "status": status})
    return result


def snapshot(root):
    """Pure read; polling absent optional settings creates no files."""
    with _lock(root):
        initial = load_options(root)
        result = {"config": initial, "initial_config": deepcopy(initial), "status": "inactive",
                  "stage": None, "reason": None, "pause_id": None, "current_date": None,
                  "triggered_rule_ids": [], "acknowledged_stages": [], "acknowledged_dates": [],
                  "intent": _intent(None), "acknowledged_intents": _acknowledged_intents(None),
                  "can_resume": False, "terminal": False}
        if not (Path(root) / "progress.json").is_file():
            result["plan"] = {"revision": 1, "config": deepcopy(initial), "observed_date": None,
                              "date_status": "unknown", "rules": []}
            return result
        root, run_id, device = _binding(root)
        state = _load_state(root, run_id, device)
        plan = _load_plan(root, run_id, device)
        result.update(run_session_id=run_id, device=device)
        result["config"] = deepcopy(plan["config"])
        result["plan"] = {key: deepcopy(plan.get(key)) for key in
                          ("revision", "config", "observed_date", "date_status")}
        result["plan"]["rules"] = _rules(plan, state)
        if state:
            result.update({key: deepcopy(state.get(key)) for key in
                ("status", "stage", "reason", "pause_id", "triggered_rule_ids", "acknowledged_stages")})
            result["triggered_rule_ids"] = result["triggered_rule_ids"] or [state["stage"]]
            result.update(current_date=_state_date(state), acknowledged_dates=_ack_dates(state),
                intent=_intent(state), acknowledged_intents=_acknowledged_intents(state),
                can_resume=state["status"] == "waiting" and state["stage"] in ("first_arc", "date"),
                terminal=state["stage"] == "final_arc")
        return result


def update_plan(root, endpoint, plan, *, request_id, expected_revision=None):
    """Save runtime-only edits without pausing, restarting, or touching input."""
    if not isinstance(plan, dict):
        raise ValueError("manual_handoff_plan_object_required")
    config = normalize(plan)
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("manual_handoff_request_id_required")
    with _lock(root):
        root, run_id, device = _binding(root)
        if endpoint != device:
            raise ValueError("manual_handoff_run_or_device_mismatch")
        saved = _load_plan(root, run_id, device)
        if request_id in saved["receipts"]:
            return deepcopy(saved["receipts"][request_id])
        if expected_revision is not None and (
                type(expected_revision) is not int or expected_revision != saved["revision"]):
            raise ValueError("manual_handoff_plan_revision_conflict")
        saved.update(config=config, revision=saved["revision"] + 1, updated_at=time.time())
        receipt = {"accepted": True, "request_id": request_id, "run_session_id": run_id,
                   "device": device, "revision": saved["revision"],
                   "applies": "next_safe_checkpoint", "pause_unchanged": True}
        saved["receipts"][request_id] = receipt
        atomic_json(root / PLAN, saved)
        return deepcopy(receipt)


def acknowledge(root, endpoint, *, explicit=False, pause_id=None):
    """A human request acknowledges exactly one pause; resume alone never does."""
    if explicit is not True:
        raise ValueError("manual_handoff_explicit_continue_required")
    with _lock(root):
        root, run_id, device = _binding(root)
        if endpoint != device:
            raise ValueError("manual_handoff_run_or_device_mismatch")
        state = _load_state(root, run_id, device)
        if state is None or state["stage"] not in ("first_arc", "date") or state["status"] != "waiting":
            raise ValueError("manual_handoff_not_waiting")
        if ((state["stage"] == "date" and not pause_id)
                or (pause_id is not None and pause_id != state.get("pause_id"))):
            raise ValueError("manual_handoff_pause_id_mismatch")
        state.update(status="acknowledged", acknowledged_at=time.time())
        stages = list(state["acknowledged_stages"])
        if "first_arc" in state.get("triggered_stages", [state["stage"]]) and "first_arc" not in stages:
            stages.append("first_arc")
        dates = _ack_dates(state)
        date = _state_date(state)
        if date and _date_key(date) not in dates:
            dates.append(_date_key(date))
        intent = _intent(state)
        acknowledged = _acknowledged_intents(state)
        acknowledged["first_arc_manual_skills"] |= intent["first_arc_manual_skills"]
        state.update(acknowledged_stages=stages, acknowledged_dates=dates,
                     intent=intent, acknowledged_intents=acknowledged)
        history_key = state.get("history_key", state["stage"])
        state.setdefault("stages", {}).setdefault(history_key, {}).update(
            status="acknowledged", acknowledged_at=state["acknowledged_at"], intent=deepcopy(intent))
        _save_state(root, state)
        return snapshot(root)


def acknowledge_first_arc(root, endpoint, explicit=False):
    """0.4.9 compatibility for the existing first-Arc continue action."""
    with _lock(root):
        root, run_id, device = _binding(root)
        state = _load_state(root, run_id, device)
        if state is None or state["stage"] != "first_arc" or state["status"] != "waiting":
            raise ValueError("manual_handoff_not_waiting_first_arc")
        return acknowledge(root, endpoint, explicit=explicit)


def _observed_date(period):
    if not isinstance(period, dict) or period.get("status") != "verified":
        return None
    evidence = period.get("evidence") or {}
    if isinstance(evidence, dict) and (evidence.get("date_visible") is False
            or evidence.get("date_reader") == "verified_scenario_milestone"):
        return None
    return _date(period.get("value"))


def _observed_stage(context):
    # Shortcuts keep the 0.4.9 verified race target; date rules do not need one.
    if not isinstance(context, dict) or context.get("status") != "verified":
        return None
    date = _observed_date(context.get("period"))
    if not date or date["month"] != 10 or date["half"] != "first":
        return None
    target = {2: "larc_arc_participation", 3: "larc_final_arc_top5"}.get(date["year_index"])
    if target is None or context.get("target_id") != target:
        return None
    return "first_arc" if date["year_index"] == 2 else "final_arc"


def _hold(root, run_id, device, state):
    # Do not hold a file lock while waiting: host edits/ack must remain possible.
    reason = state["reason"]
    control = runtime_control.current()
    if control is None:
        raise runtime_control.RunStopped(reason)
    control.note = {"kind": "manual_handoff", "stage": state["stage"], "reason": reason,
                    "run_session_id": run_id, "device": device, "pause_id": state.get("pause_id")}
    control.request("pause")
    try:
        control.checkpoint(reason)
    except runtime_control.ResumeFromCurrentPage:
        with _lock(root):
            current = _load_state(root, run_id, device)
        if (current and current["status"] == "acknowledged"
                and current.get("pause_id") == state.get("pause_id")):
            raise
        raise runtime_control.RunStopped(reason)
    with _lock(root):
        current = _load_state(root, run_id, device)
    if (current and current["status"] == "acknowledged"
            and current.get("pause_id") == state.get("pause_id")):
        raise runtime_control.ResumeFromCurrentPage()
    raise runtime_control.RunStopped(reason)


def _notify(ctx, root, run_id, device, state):
    hook = getattr(ctx, "notify_manual_handoff", None)
    status = "not_configured"
    ok = None
    if callable(hook):
        payload = {key: deepcopy(state.get(key)) for key in
                   ("pause_id", "stage", "reason", "current_date", "triggered_rule_ids")}
        payload.update(run_session_id=run_id, device=device)
        try:
            result = hook(payload)
            # The shared API reports provider acceptance, not delivery. Keep
            # only known status/bool fields, never raw provider/config data.
            status = "attempted"
            known = {"accepted", "rejected", "failed", "disabled", "muted", "not_configured",
                     "saved_token_unreadable", "token_file_unreadable", "duplicate"}
            if isinstance(result, dict):
                if result.get("status") in known:
                    status = result["status"]
                if type(result.get("ok")) is bool:
                    ok = result["ok"]
        except Exception:
            status = "failed"  # Notification failure cannot bypass a saved pause.
    with _lock(root):
        current = _load_state(root, run_id, device)
        if current and current.get("pause_id") == state["pause_id"]:
            current["notification"]["status"] = status
            if ok is not None:
                current["notification"]["ok"] = ok
            _save_state(root, current)


def checkpoint(ctx, context=None, *, period=None, source=None):
    """Observe a verified game date at the caller's safe pre-input boundary.

    No context on a cold restart means only enforce the saved human pause.
    Unknown dates are reported, not guessed or made into a global stop.
    """
    root = Path(ctx.root).resolve()
    run_id, device = ctx.run_id, ctx.progress.get("device")
    if run_id != root.name or not isinstance(device, str) or not device:
        raise ValueError("manual_handoff_run_or_device_mismatch")
    new_trigger = False
    with _lock(root):
        initial = load_options(root)
        if not any((root / name).is_file() for name in (STATE, PAUSE_STATE, PLAN)) and not (
                initial["first_arc"] or initial["final_arc"] or initial["dates"]):
            return
        if _binding(root) != (root, run_id, device):
            raise ValueError("manual_handoff_run_or_device_mismatch")
        state = _load_state(root, run_id, device)
        if state and state["stage"] == "final_arc":
            raise runtime_control.RunStopped(REASONS["final_arc"])
        if not (state and state["status"] == "waiting"):
            plan = _load_plan(root, run_id, device)
            config = plan["config"]
            supplied_period = period if period is not None else (
                context.get("period") if isinstance(context, dict) else None)
            date = _observed_date(supplied_period)
            if period is not None or context is not None:
                plan["date_status"] = "verified" if date else "unknown"
                if date:
                    plan["observed_date"] = date
                atomic_json(root / PLAN, plan)
            if date is None:
                return
            shortcut = _observed_stage(context)
            if shortcut and _observed_date(context.get("period")) != date:
                shortcut = None
            acknowledged_date = _date_key(date) in _ack_dates(state)
            matched = [rule["id"] for rule in config["dates"]
                       if not acknowledged_date and rule["enabled"] and _date_key(rule) == _date_key(date)]
            shortcut_enabled = (shortcut and config[shortcut]
                and shortcut not in (state or {}).get("acknowledged_stages", [])
                and (shortcut == "final_arc" or not acknowledged_date))
            if shortcut_enabled:
                matched.append(shortcut)
            if not matched:
                return
            # A user-chosen rule ID named "final_arc" is still a date rule,
            # never authorization to create the verified terminal shortcut.
            stage = shortcut if shortcut_enabled else "date"
            history_key = stage if stage != "date" else "date:" + _date_key(date)
            # Freeze the purpose now. Later edits can change future stops but
            # cannot retroactively turn this pause into (or out of) skill work.
            intent = {"first_arc_manual_skills": stage == "first_arc" or (
                stage == "date" and _date_key(date) == "2-10-first" and config["first_arc"])}
            entry = {"stage": stage, "status": "handed_off" if stage == "final_arc" else "waiting",
                     "reason": REASONS[stage], "pause_id": uuid4().hex,
                     "current_date": date, "triggered_rule_ids": matched,
                     "triggered_stages": [shortcut] if shortcut_enabled else [],
                     "intent": intent,
                     "period": deepcopy(supplied_period),
                     "source": deepcopy(source if source is not None else (context or {}).get("source")),
                     "target_id": (context or {}).get("target_id"), "observed_at": time.time(),
                     "history_key": history_key}
            stages = deepcopy((state or {}).get("stages", {}))
            stages[history_key] = deepcopy(entry)
            state = {"schema_version": PAUSE_STATE_VERSION, "run_session_id": run_id, "device": device,
                     **entry, "acknowledged_stages": list((state or {}).get("acknowledged_stages", [])),
                     "acknowledged_dates": _ack_dates(state), "stages": stages,
                     "acknowledged_intents": _acknowledged_intents(state),
                     "input_performed": False,
                     "notification": {"attempted": True, "status": "attempted"}}
            _save_state(root, state)
            new_trigger = True
    if new_trigger:
        _notify(ctx, root, run_id, device, state)
    if state["stage"] == "final_arc":
        raise runtime_control.RunStopped(REASONS["final_arc"])
    return _hold(root, run_id, device, state)



def apply_workflow_options(root, value, *, request_id):
    """Apply a new career step through the existing live-plan controller."""
    root=Path(root);config=normalize(value)
    if not (root/OPTIONS).is_file() and not (root/PLAN).is_file():
        return save_options(root,config)
    current=snapshot(root)
    if current['plan']['config']==config:
        return config
    progress=_read(root/'progress.json')
    update_plan(root,progress['device'],config,request_id=request_id)
    return config
