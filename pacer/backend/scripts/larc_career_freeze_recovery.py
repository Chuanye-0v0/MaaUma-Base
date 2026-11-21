"""Opt-in ordinary pre-debut training freeze recovery, never an action replay."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import time
import uuid

import numpy as np

from core.auto_trainer.cut_training_transition_recovery import (
    ordinary_period_scope, recovered_header, same_or_next_ordinary_period,
    training_transition, turn_identity, valid_ordinary_before_period,
)
from core.auto_trainer.frame_safety import FrameSafetyAnalyzer
from core.auto_trainer.main_menu_state_provider import provide_main_menu_state
from core.auto_trainer.route_transition_session import RouteTransitionSession
from core.auto_trainer.runtime_control import input_guard
from core.native_capture_adb import adb_for_capture
from scripts.larc_same_account_reconnect import reconnect_existing_career
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json


PACKAGE = "com.bilibili.umamusu"
MAX_RECOVERIES = 2


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _resolved(record):
    continuations = record.get('continuations', [])
    return (record.get('status') == 'recovered' or bool(continuations
            and continuations[-1].get('status') == 'recovered' and continuations[-1].get('ok') is True))


def recover_career_training_freeze(root, report, *, adb_port, authorized=False,
        ordinary_career_opening=False, control=None, stopped_control_state_path=None,
        resume_restarted=False,
        adb_factory=None, safety=None, menu_reader=provide_main_menu_state,
        transition_check=training_transition, reconnect=reconnect_existing_career,
        header_reader=recovered_header, sleep_fn=time.sleep, clock_fn=time.monotonic):
    """Restart one proven frozen training transition and reconcile its game turn.

    The panel calls this while it still holds its lease and binds ``control``.
    A one-off stopped-run caller must hold that same lease and supply the saved
    stopped control-state path. Neither mode executes the old training again.
    All inputs are default-off; this does not grant account or device authority.
    """
    root = Path(root).resolve(strict=True)
    endpoint = f"127.0.0.1:{adb_port}"
    ignored = lambda reason: {"handled": False, "ok": False, "reason": reason}
    if authorized is not True or ordinary_career_opening is not True:
        return ignored("ordinary_training_recovery_not_authorized")

    def control_ready():
        if control is not None:
            state = control.snapshot()
            if control.stopping or state.get("pause_requested") or state.get("state") == "paused":
                return False
            return state.get("input_in_flight") is False and state.get("state") in {"starting", "running"}
        if stopped_control_state_path is None:
            return False
        path = Path(stopped_control_state_path).resolve(strict=True)
        if not path.is_relative_to(root):
            return False
        state = _read(path)
        return (state.get("state") == "stopped" and state.get("tap_in_flight") is False
                and state.get("active_boundary") is None
                and state.get("last_result", {}).get("reason") == report.get("stop_reason"))

    def checkpoint(step):
        if not control_ready():
            raise ValueError("ordinary_training_recovery_control_not_ready")
        if control is not None:
            # License/user stop is checked again immediately before each input.
            control.checkpoint(step)
            if not control_ready():
                raise ValueError("ordinary_training_recovery_control_not_ready")

    if not control_ready():
        return ignored("ordinary_training_recovery_control_not_ready")
    if (report.get("status") != "stopped"
            or report.get("stop_reason") != "career_transition_no_progress_timeout"
            or report.get("run_session_id") != root.name or report.get("device") != endpoint
            or not report.get("turns")):
        return ignored("not_owned_ordinary_training_timeout")
    output = Path(report.get("output", "")).resolve()
    turn = report["turns"][-1]
    turn_output = Path(turn.get("output", "")).resolve()
    if (not output.is_relative_to(root) or not turn_output.is_relative_to(output)
            or turn.get("kind") != "train" or turn.get("turn_consumed") is not True
            or turn.get("pipeline_code") != 0):
        return ignored("not_confirmed_ordinary_training_input")
    receipt_path = turn_output / "action_receipt.json"
    if not receipt_path.is_file() or _read(receipt_path) != turn.get("execution"):
        return ignored("ordinary_training_receipt_not_owned")
    receipt = turn["execution"]
    source = receipt.get("route_action_receipt_source") or {}
    source_path = Path(source.get("source_path", "")).resolve()
    if (receipt.get("performed") is not True
            or receipt.get("target") not in {"speed", "stamina", "power", "guts", "wisdom"}
            or source.get("action_id") != "training.execute"
            or not source_path.is_relative_to(turn_output) or not source_path.is_file()
            or hashlib.sha256(source_path.read_bytes()).hexdigest() != source.get("source_sha256")):
        return ignored("ordinary_training_receipt_source_unverified")
    progress = _read(root / "progress.json")
    index = turn.get("turn_index")
    if (progress.get("run_session_id") != root.name or progress.get("device") != endpoint
            or type(index) is not int or progress.get("next_turn") != index + 1
            or progress.get("actions_completed") != index):
        return ignored("ordinary_training_progress_not_owned")
    session = root / "session"
    if (session / "joint_trial_owner.json").exists() or (session / "larc_autonomous_pending.json").exists():
        return ignored("ordinary_training_pending_input")
    for name in ("larc_friend_outing.json", "larc_race_checkpoint.json", "larc_support_skill_startup.json"):
        path = session / name
        if path.is_file():
            pending = _read(path)
            if pending.get("pending") or pending.get("status") in {"in_flight", "blocked", "pending_purchase"}:
                return ignored("ordinary_training_other_input_unresolved")
    for path in session.glob("larc_adaptation_*.json"):
        if _read(path).get("stage") in {"submission_pending", "awaiting_result"}:
            return ignored("ordinary_training_resource_unresolved")
    route_path = session / "transition_session.json"
    route = RouteTransitionSession.load(route_path, expected_run_session_id=root.name)
    if (route.last_action_id != "training.execute" or not route.expected_next_states
            or route.action_source_sha256 != source["source_sha256"]):
        return ignored("ordinary_training_transition_owner_missing")
    before = turn_identity(turn.get("before", {}))
    if (type(before["remaining_turns"]) is not int or before["remaining_turns"] < 2
            or not valid_ordinary_before_period(before["period"])
            or not before["target_text"]):
        return ignored("ordinary_training_before_turn_unverified")

    history_path = session / "ordinary_training_freeze_recovery.json"
    history = (_read(history_path) if history_path.is_file() else
               {"schema_version": "uma.ordinary_training_freeze_recovery.v1",
                "run_session_id": root.name, "device": endpoint, "records": []})
    if history.get("run_session_id") != root.name or history.get("device") != endpoint:
        return ignored("ordinary_training_recovery_history_mismatch")
    records = history["records"]
    prior = None
    if resume_restarted is True:
        if not records or len(records) > MAX_RECOVERIES:
            return ignored('ordinary_training_restart_tail_not_owned')
        prior = records[-1]
        nav = prior.get('resume_report') or {}
        commands = prior.get('commands') or []
        expected_commands = [("shell", "am", "force-stop", PACKAGE),
            ("shell", "am", "start", "-n", PACKAGE+"/com.uo.sdk.SplashActivity")]
        if (any(not _resolved(item) for item in records[:-1])
                or prior.get('status') != 'unknown' or prior.get('ok') is not False
                or prior.get('reason') != 'ordinary_training_same_career_resume_unverified'
                or prior.get('run_session_id') != root.name or prior.get('device') != endpoint
                or prior.get('package') != PACKAGE or prior.get('scope') not in {'pre_debut', 'ordinary_career'}
                or prior.get('restart_performed') is not True or prior.get('continuations')
                or prior.get('original_receipt') != str(receipt_path)
                or prior.get('progress_before') != progress or prior.get('before_identity') != before
                or len(commands) != 2
                or any(item.get('args') != list(expected) or item.get('status') != 'ok'
                       or item.get('returncode') != 0 for item, expected in zip(commands, expected_commands))
                or nav.get('run_session_id') != root.name or nav.get('device') != endpoint
                or nav.get('ok') is not False or nav.get('reason') != 'reconnect_existing_career_not_visible'
                or not nav.get('records')
                or any(item.get('performed') is not False or item.get('tap_performed')
                       or item.get('decision', {}).get('action') not in {'wait', 'stop'} for item in nav['records'])):
            return ignored('ordinary_training_restart_tail_not_owned')
        prior_folder = Path(prior.get('evidence', '')).resolve(strict=True)
        if (not prior_folder.is_relative_to(root/'automatic_recovery')
                or _read(prior_folder/'report.json') != prior):
            return ignored('ordinary_training_restart_tail_evidence_changed')
        folder = prior_folder / f'continuation_{uuid.uuid4().hex[:6]}'
    else:
        if len(records) >= MAX_RECOVERIES:
            return ignored("ordinary_training_freeze_recovery_limit")
        if any(not _resolved(item) for item in records):
            return ignored("ordinary_training_previous_recovery_unknown")
        if any(item.get("original_receipt") == str(receipt_path) for item in records):
            return ignored("ordinary_training_receipt_already_recovered")
        folder = root / "automatic_recovery" / f"career_training_{len(records)+1:02d}_{uuid.uuid4().hex[:6]}"
    folder.mkdir(parents=True, exist_ok=False)
    adb = (adb_factory or (lambda: adb_for_capture(adb_port)))()
    safety = safety or FrameSafetyAnalyzer()
    started = clock_fn()
    record = None

    def save():
        if record is not None:
            _atomic_write_json(folder / "report.json", record)
            _atomic_write_json(history_path, history)

    try:
        if getattr(adb, "address", endpoint) != endpoint:
            return ignored("ordinary_training_capture_device_mismatch")
        checkpoint("career_freeze_capture")
        if not adb.connect():
            return ignored("ordinary_training_capture_connection_failed")
        images, sources = [], []
        if prior is None:
            for number in range(3):
                checkpoint("career_freeze_observe")
                path = folder / f"freeze_{number}.png"
                image = adb.screenshot(str(path))
                if image is None:
                    return ignored("ordinary_training_capture_failed")
                if not transition_check(path, image, safety.analyze(path), route):
                    return ignored("current_ordinary_training_transition_unverified")
                images.append(image)
                sources.append(str(path))
                if number < 2:
                    sleep_fn(.5)
            if not all(np.array_equal(images[0], image) for image in images[1:]):
                return ignored("ordinary_training_transition_moving")
        checkpoint("career_freeze_before_restart")
        if _read(root / "progress.json") != progress or _read(receipt_path) != receipt:
            return ignored("ordinary_training_owner_changed_before_restart")
        record = {"handled": True, "automatic": True, "kind": "ordinary_training_transition_freeze",
            "status": "started", "ok": False, "run_session_id": root.name, "device": endpoint,
            "package": PACKAGE, "scope": ordinary_period_scope(before["period"]), "started_at_unix_ms": int(time.time()*1000),
            "source_output": str(output), "original_receipt": str(receipt_path),
            "original_receipt_preserved": True, "freeze_frames": sources,
            "before_identity": before, "progress_before": progress, "commands": [],
            "restart_performed": False, "evidence": str(folder),
            "source_segment_elapsed_seconds": report.get("elapsed_seconds")}
        if prior is None:
            records.append(record)
        else:
            record.update(kind='ordinary_training_restart_continuation', restart_already_performed=True,
                prior_recovery_evidence=prior['evidence'], prior_failure_reason=prior['reason'],
                prior_elapsed_seconds=prior['elapsed_seconds'])
            prior['continuations'] = [record]
        save()
        for command in (() if prior is not None else (("shell", "am", "force-stop", PACKAGE),
                        ("shell", "am", "start", "-n", PACKAGE+"/com.uo.sdk.SplashActivity"))):
            checkpoint("career_freeze_restart_input")
            record["commands"].append({"args": list(command), "status": "pending"})
            save()
            with input_guard(adb):
                result, ok = adb._run(*command, timeout=20)
            record["commands"][-1].update(status="ok" if ok else "failed",
                                            returncode=getattr(result, "returncode", None))
            save()
            if not ok:
                raise ValueError("ordinary_training_restart_command_failed")
            record["restart_performed"] = True
        checkpoint("career_freeze_same_account_reconnect")
        nav = reconnect(SimpleNamespace(root=root, run_id=root.name, port=adb_port,
            progress=progress, screen_reader=safety), folder / "resume", require_existing_career=True,
            adb=adb, sleep_fn=sleep_fn, clock_fn=clock_fn, app_startup_wait_seconds=20)
        record["resume_report"] = nav
        if nav.get("ok") is not True:
            raise ValueError("ordinary_training_same_career_resume_unverified")
        source_path = Path(nav["current_source"]).resolve(strict=True)
        if not source_path.is_relative_to(folder):
            raise ValueError("ordinary_training_resume_source_outside_recovery")
        checkpoint("career_freeze_reconcile_observation")
        observed = menu_reader(source_path, source_path.with_suffix(".state.json"),
                               turn_index=progress["next_turn"], scenario="larc")
        menu = observed.get("valid_for_training") is True
        after = turn_identity(observed.get("state_context", {})) if menu else header_reader(source_path)
        record.update(after_identity=after, recovered_source=str(source_path),
                      recovered_source_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest())
        if (after is None or after.get("target_text") != before["target_text"]
                or not same_or_next_ordinary_period(before["period"], after.get("period"))):
            raise ValueError("ordinary_training_restored_career_or_date_changed")
        if _read(root / "progress.json") != progress or _read(receipt_path) != receipt:
            raise ValueError("ordinary_training_owner_changed_during_recovery")
        if after.get("remaining_turns") == before["remaining_turns"]-1:
            record["training_outcome"] = "applied"
        elif after.get("remaining_turns") == before["remaining_turns"]:
            if after.get("period") != before["period"]:
                raise ValueError("ordinary_training_restored_career_or_date_changed")
            if not menu:
                raise ValueError("ordinary_training_event_outcome_not_final")
            corrected = {**progress, "actions_completed": index-1, "next_turn": index}
            _atomic_write_json(root / "progress.json", corrected)
            record.update(training_outcome="unapplied", progress_correction={"before": progress,
                "after": corrected, "original_receipt": str(receipt_path), "original_receipt_preserved": True})
        else:
            raise ValueError("ordinary_training_restored_remaining_turns_unverified")
        if menu:
            route.observe("main_menu", source_sha256=record["recovered_source_sha256"], resync=True)
            route.save(route_path)
        record.update(status="recovered", ok=True, reason="same_career_training_outcome_observed",
                      progress_after=_read(root / "progress.json"))
        return record
    except Exception as error:
        if record is None:
            return ignored(str(error))
        record.update(status="unknown", reason=str(error))
        return record
    except BaseException:
        if record is not None:
            record.update(status="unknown", reason="ordinary_training_recovery_interrupted")
        raise
    finally:
        try:
            if record is not None:
                record.update(ended_at_unix_ms=int(time.time()*1000), elapsed_seconds=max(0., clock_fn()-started))
                save()
        finally:
            adb.close()
