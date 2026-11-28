"""Fixed races and current-game first-win/fan rescue through the same executor."""
from dataclasses import asdict, replace
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from time import monotonic

from core.adb import ADB
from core.frame_io import io_path
from core.auto_trainer.runtime_control import guarded_input
from core.auto_trainer.config import load_goal_config
from core.auto_trainer.larc_adaptation import LarcAdaptationSession
from core.auto_trainer.race_route import (
    RACE_ROUTE_CONFIRM_TOKEN, RaceRouteExecutor, RaceRoutePlanner, RaceRoutePolicy, RaceRouteSession,
    larc_exchange_entry_released,
)
from core.auto_trainer.route_transition_session import RouteTransitionSession
from core.auto_trainer.skip_session import RunSkipSession
from core.auto_trainer.transition_wait import TransitionBudget, technical_wait_stop, transient_observation
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json
from scripts.ug_parent_race_route import MAX_CONSECUTIVE_IDENTICAL_TAPS
from scripts.ug_parent_safe_advance import parse_args as advance_args, run as advance
from scripts.larc_autonomy_adaptation import _verified_ready_reason
from scripts.ug_parent_supervised_run import (
    _canonical_sha256, _fan_shortfall_entry_progress, _load_race_owner_session, _record_race_owner_handoff,
    _settle_race_day_owner_preflight,
)


from core.auto_trainer.route_handoff import is_route_handoff, is_route_handoff_result


def _file_sha256(path: Path | str) -> str:
    return hashlib.sha256(io_path(path).read_bytes()).hexdigest()


def _race_tactic_state_path(run_root: Path) -> Path:
    return Path(run_root) / "session" / "larc_race_tactic_state.json"


def _load_race_tactic_state(run_root: Path, run_id: str) -> dict:
    path = _race_tactic_state_path(run_root)
    default = {
        "schema_version": "uma.larc_race_tactic_state.v1",
        "run_session_id": run_id,
        "solo_leader_restore_style": None,
        "front_aptitude_rejected": False,
        "observed_front_aptitude": None,
    }
    if not path.is_file():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default
    if not isinstance(data, dict) or data.get("run_session_id") != run_id:
        return default
    return {
        **default,
        "solo_leader_restore_style": data.get("solo_leader_restore_style")
        if data.get("solo_leader_restore_style") in {"leader"}
        else None,
        "front_aptitude_rejected": bool(data.get("front_aptitude_rejected")),
        "observed_front_aptitude": data.get("observed_front_aptitude"),
    }


def _save_race_tactic_state(run_root: Path, state: dict) -> None:
    _atomic_write_json(_race_tactic_state_path(run_root), state)


HANDOFF_STOPS = {"post_race_dialogue", "event_choice", "ready_main_menu", "ready_training_menu",
                 "goal_achievement_loading", "goal_achievement_result"}


def _debut_retry_scope(rows):
    for row in rows:
        text=''.join(str(row.get('result',{}).get('frame_safety',{}).get('visible_text','')).split())
        if '初级新人战' in text and '出道前' in text:return True
    return False


def _clock_usage(steps):
    paid=free=0
    for row in steps:
        result=row.get('result',{});decision=result.get('decision',{})
        if result.get('performed') is not True or decision.get('reason')!='retry_lost_race':continue
        proof=decision.get('evidence',{}).get('clock_retry',{})
        if type(proof.get('before')) is int and type(proof.get('after')) is int:
            delta=proof['before']-proof['after']
            paid+=max(0,delta);free+=int(delta==0 and proof.get('free') is True)
    return {'clock_items_committed':paid,'clock_free_retries':free}


def _live_clock_session(source):
    state = source.get('clock_retry_state')
    if state is None:
        return RaceRouteSession(result_transition_pending=source.get('entered') is True), _clock_usage([])
    scope_cut = (source.get('cut_debut_race') is True and isinstance(state, dict)
        and state.get('cut_debut_race') is True)
    scope_ordinary = (source.get('ordinary_clock_retry') is True and isinstance(state, dict)
        and state.get('ordinary_clock_retry') is True)
    if source.get('entered') is not True or not (scope_cut or scope_ordinary):
        raise ValueError('live_debut_clock_state_unverified')
    paid, free = state.get('clock_items_committed'), state.get('clock_free_retries')
    retries = state.get('clock_retries_used')
    if (any(type(v) is not int or v < 0 for v in (paid, free, retries))
            or paid + free != retries):
        raise ValueError('live_debut_clock_usage_invalid')
    session = RaceRouteSession(result_transition_pending=True, clock_retries_used=retries,
        last_clock_retry_remaining=state.get('last_clock_retry_remaining'),
        clock_retry_entry_pending=state.get('clock_retry_entry_pending', False),
        clock_retry_declined=state.get('clock_retry_declined', False))
    return session, {'clock_items_committed': paid, 'clock_free_retries': free}


def _cumulative_clock_usage(steps, baseline):
    current = _clock_usage(steps)
    return {key: current[key] + baseline.get(key, 0) for key in current}


def _confirmed_result_repeat_resume(saved):
    rows = saved.get('steps', [])
    last = rows[-1].get('result', {}) if rows else {}
    receipts = saved.get('current_input_receipts', [])
    return bool(saved.get('status') == 'blocked'
        and saved.get('stop_reason') == 'repeated_tap_limit_reached'
        and _fixed_race_entry_confirmed(rows)
        and last.get('reason') == 'race_result_continue' and last.get('performed') is True
        and receipts and all(item.get('outcome') == 'confirmed' for item in receipts))



def _fan_shortfall_list_resume(saved, last):
    """Old closed-entry stops are safe to reobserve, never replay an input."""
    if (saved.get("status") != "stopped" or saved.get("fan_goal") is not None
            or saved.get("fan_race_receipt") or saved.get("current_input_receipts")
            or saved.get("stop_reason") != "fan_goal_shortfall:entry_not_allowed"
            or last.get("reason") != saved.get("stop_reason") or last.get("performed") is not False
            or last.get("decision", {}).get("action") != "stop"
            or last.get("decision", {}).get("tap")
            or any(last.get(k) for k in ("tap_performed", "input_may_have_occurred", "input_receipts"))):
        return False
    return all(row.get("result", {}).get("tap_outcome") not in {"failed", "unknown", "pending"}
        and (not row.get("result", {}).get("input_may_have_occurred")
             or bool(row.get("result", {}).get("input_receipts")))
        and all(r.get("outcome") == "confirmed" for r in row.get("result", {}).get("input_receipts", []))
        for row in saved.get("steps", []))



def _record_fan_list_home_observation(args, report, before, run_root):
    """The player may already have left an old denied list before updating."""
    last = report["step_reports"][-1]["result"]
    decision, packet, verdict = (last.get(k, {}) for k in ("decision", "frame_safety", "safety_verdict"))
    source = Path(str(decision.get("evidence", {}).get("image", ""))).resolve(strict=True)
    sha = _file_sha256(source)
    if (report.get("performed_steps") != 0 or report.get("stop_reason") != "ready_main_menu"
            or not source.is_relative_to(run_root) or last.get("performed") is not False
            or not is_route_handoff(decision, reason="ready_main_menu")
            or decision.get("tap") or any(last.get(k) for k in ("tap_performed", "input_may_have_occurred", "input_receipts"))
            or packet.get("source_image_sha256") != sha or packet.get("dangerous") is True
            or verdict.get("allowed") is not True or verdict.get("reason") != "non_executing_decision"
            or verdict.get("action_id") is not None or verdict.get("source_image_sha256") != sha
            or _load_race_owner_session(args).to_dict() != before.to_dict()):
        raise ValueError("fan_list_home_observation_invalid")
    from copy import deepcopy
    after = deepcopy(before)
    after.observe("main_menu", source_sha256=sha, resync=True)
    path = run_root / "session/transition_session.json"
    after.save(path)
    persisted = RouteTransitionSession.load(path, expected_run_session_id=run_root.name)
    return {"before": before.to_dict(), "after": persisted.to_dict(),
        "report_sha256": _canonical_sha256(report), "terminal_source_sha256": sha,
        "observation_handoff": {"kind": "unsubmitted_fan_list_already_returned", "new_tap": False}}


def _catalog_read_error_resume(saved, last):
    """This exact planner read error precedes input; finish the entered race."""
    if (saved.get("status") != "blocked" or saved.get("stop_reason") != "race_executor_error"
            or last.get("reason") != "race_executor_error" or last.get("performed") is not False
            or last.get("error") != "CareerGoalPerceptionError:goal_catalog_invalid"
            or saved.get("current_input_receipts") or last.get("decision", {}).get("tap")
            or any(last.get(k) for k in ("tap_performed", "input_may_have_occurred", "input_receipts"))
            or not _fixed_race_entry_confirmed(saved.get("steps", []))):
        return False
    # A catalog repair is not proof that any uncertain earlier input committed.
    return all(row.get("result", {}).get("tap_outcome") not in {"failed", "unknown", "pending"}
               and all(receipt.get("outcome") == "confirmed"
                       for receipt in row.get("result", {}).get("input_receipts", []))
               for row in saved.get("steps", []))


def _date_change_safety_resume(run_root,saved,last):
    """Recover only the recorded exact date notice denied before any input."""
    if (saved.get('status')!='stopped' or saved.get('stop_reason')!='action_safety_gate'
            or last.get('reason')!='action_safety_gate' or last.get('performed') is not False
            or saved.get('current_input_receipts')
            or any(last.get(key) for key in ('tap_performed','input_may_have_occurred','input_receipts'))
            or not _fixed_race_entry_confirmed(saved.get('steps',[]))):return False
    packet=last.get('frame_safety',{});verdict=last.get('safety_verdict',{});decision=last.get('decision',{})
    text=str(packet.get('visible_text',''));source=decision.get('evidence',{}).get('image')
    if (''.join(text.split())!='日期变化日期发生了变化确定' or packet.get('text_status')!='ok'
            or packet.get('dangerous') or packet.get('resource_terms')
            or verdict.get('allowed') is not False or verdict.get('reason')!='generic_action_semantics_required'
            or decision.get('action')!='tap' or decision.get('reason')!='confirm_safe_modal'
            or decision.get('tap',{}).get('coordinate_name')!='safe_advance.safe_confirm'
            or verdict.get('action_id')!='safe_advance.safe_confirm' or not source):return False
    path=Path(source).resolve()
    if not path.is_relative_to(Path(run_root).resolve()) or not path.is_file():return False
    sha=_file_sha256(path)
    if packet.get('source_image_sha256')!=sha or verdict.get('source_image_sha256')!=sha:return False
    replay=RaceRoutePlanner().plan(path,policy=RaceRoutePolicy(scenario='larc',allow_confirm_race=True,visible_text=text))
    return (replay.get('reason')=='larc_date_change_confirm'
        and replay.get('tap',{}).get('coordinate_name')=='safe_advance.date_change_confirm')


class _FanReceiptPlanner(RaceRoutePlanner):
    """Keep the original planner decision for the input receipt, even on later errors."""
    def __init__(self, goal):
        super().__init__()
        self.goal, self.last_decision = goal, None

    def plan(self, *args, **kwargs):
        decision = super().plan(*args, **kwargs)
        evidence = decision.get("evidence", {})
        proof = (evidence.get("fan_goal_shortfall_candidate")
                 or evidence.get("fan_goal_shortfall_selected_verification"))
        if (decision.get("reason") == "enter_race_from_race_day"
                or isinstance(proof, dict) and proof.get("fan_shortfall") != self.goal["remaining_fans"]):
            decision = self._stop("fan_goal_current_candidate_mismatch", evidence=evidence)
        self.last_decision = decision
        return decision


def _fan_receipt_from_input(run_root, device, goal, step, decision, receipt):
    """Only an actual successful selected-fan-race confirmation consumes a turn."""
    try:
        proof = decision["evidence"]["fan_goal_shortfall_selected_verification"]
        tap = decision["tap"]
        path = Path(receipt["source_path"]).resolve(strict=True)
        sha = _file_sha256(path)
        if (receipt.get("outcome") != "confirmed" or not path.is_relative_to(run_root)
                or Path(decision["evidence"]["image"]).resolve() != path
                or receipt.get("source_sha256") != sha
                or decision.get("action") != "tap"
                or decision.get("reason") != "confirm_fan_goal_shortfall_selected_race"
                or decision.get("consume_fan_goal_shortfall_selected_verification") is not True
                or tap.get("coordinate_name") != "race_confirm.fan_goal_shortfall_selected"
                or any(receipt.get(k) != tap.get(k) for k in ("x", "y", "scaled"))
                or proof.get("source") != "race_list.fan_goal_shortfall_selected_card"
                or proof.get("action_id") != "race_list.fan_goal_shortfall_selected_entry"
                or proof.get("single_use") is not True
                or proof.get("confirmation_source") != "race_detail_confirm.foreground_positioned_ocr"
                or proof.get("grade") not in {"G1", "G2", "G3"}
                or proof.get("fan_shortfall") != goal["remaining_fans"]
                or type(proof.get("fan_reward")) is not int or proof["fan_reward"] < goal["remaining_fans"]
                or proof.get("gold_star_surface_distance_evidence") not in
                    [["草地", d] for d in ("短距离", "英里", "中距离", "长距离")]):
            return None
        return {"schema_version": "larc_fan_race_receipt.v1", "run_session_id": run_root.name,
                "device": device, "turn_index": goal["source"]["turn_index"], "performed": True,
                "reason": "fan_shortfall_entry_consumed", "source_path": str(path), "source_sha256": sha,
                "confirmation_step": step, "decision": decision, "input_receipt": dict(receipt)}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _post_result_unreadable_recheck(result, previous):
    """Unreadable result fade eligible for a bounded read-only settling window."""
    if (not isinstance(previous, dict) or previous.get('performed') is not True
            or previous.get('reason') != 'race_result_continue'
            or result.get('reason') != 'resource_prompt_unreadable'
            or result.get('decision', {}).get('action') != 'stop'
            or result.get('decision', {}).get('tap')):
        return False
    frame = result.get('frame_safety', {})
    text = str(frame.get('visible_text', ''))
    # The observed failure contained only a backslash while the next event
    # faded in. Readable labels or amounts belong to the ordinary prompt route.
    if any(char.isalnum() for char in text) or frame.get('resource_terms'):
        return False
    return transient_observation(result, reason='common_career_page_unrecognized')


def _clock_retry_unavailable_recheck(result, policy, *, unresolved):
    """Only an allowed post-entry, zero-input diagnostic may acquire a new frame."""
    decision = result.get('decision', {})
    return bool(result.get('performed') is False
        and result.get('reason') == 'clock_retry_unavailable'
        and decision.get('action') == 'stop' and decision.get('reason') == 'clock_retry_unavailable'
        and not decision.get('tap') and not decision.get('gesture')
        and policy.post_entry_only is True and policy.allow_clock_retry is True
        and not policy.clock_retry_unresolved and not unresolved
        and (policy.clock_retry_remaining is None or policy.clock_retry_remaining > 0)
        and not any(result.get(key) for key in ('tap_performed', 'input_may_have_occurred', 'input_receipts'))
        and all(result.get(key) is None for key in ('tap_outcome', 'input_outcome', 'outcome')))


def _race_text_transition(result, *, result_transition_pending=False):
    """Unreadable post-entry animation: reobserve only, never authorize a tap."""
    if result.get('reason') == 'clock_retry_outcome_pending':
        decision=result.get('decision',{})
        return decision.get('action')=='wait' and not decision.get('tap')
    if result.get("reason") == "unsupported_screen:loading":
        # The generic reader can still see loading after a race-specific visual
        # template stops matching a fading frame. Callers retain the confirmed
        # race owner and bounded wait budget; this grants no retry of an input.
        decision = result.get("decision", {})
        return (result.get("performed") is False and decision.get("action") == "stop"
            and not decision.get("tap")
            and transient_observation(result, reason="common_career_page_unrecognized"))
    if result.get("reason") == "modal_confirm:unsupported":
        # A fading loading/trivia layer over the race introduction resembles
        # a modal. This does NOT grant a confirm tap: the caller only waits
        # after an already-confirmed entry, with the normal bounded budget.
        return transient_observation(result, reason="common_career_page_unrecognized")
    if result.get("reason") == "race_preview:unknown_action_layout":
        # A fading loading/trivia layer over green racecourse turf can trip the
        # HSV preview-action check before race_participant_list renders. Only
        # re-observe read-only when zero race-preview controls are visible; this
        # never taps or bypasses real preview/WBC guards.
        decision = result.get("decision", {})
        visible = str(result.get("frame_safety", {}).get("visible_text", ""))
        if any(term in visible for term in ("查看结果", "前往赛事", "基础能力", "更改战术")):
            return False
        return (result.get("performed") is False and decision.get("action") == "stop"
            and not decision.get("tap")
            and transient_observation(result, reason="common_career_page_unrecognized"))
    if result.get('reason') in {'race_day:enter_not_allowed', 'race_list:enter_not_allowed', 'wbc_hub:enter_not_allowed'}:
        # Confirmation may briefly uncover the old race-day background.
        # Callers require a confirmed entry before accepting this as a wait.
        return (result.get('performed') is False and not result.get('decision',{}).get('tap')
            and not any(result.get(k) for k in ('tap_performed','input_may_have_occurred','input_receipts'))
            and result.get('tap_outcome') not in {'failed','unknown','pending'})
    if result.get("reason") == "unsupported_screen:training_menu" and not result_transition_pending:
        return False
    return transient_observation(result)


def _fixed_race_entry_confirmed(steps):
    """Reuse successful executor receipts; entering a race ends candidate selection."""
    for row in steps:
        result = row.get("result", {})
        decision = result.get("decision", {})
        tap = decision.get("tap", {})
        receipts = result.get("input_receipts", [])
        if (result.get("performed") is not True or result.get("tap_performed") is not True
                or decision.get("action") != "tap"
                or decision.get("reason") not in {"confirm_race_entry", "confirm_larc_unupgraded_entry", "confirm_custom_scheduled_race"}
                or tap.get("coordinate_name") != "race_confirm.primary" or len(receipts) != 1):
            continue
        receipt = receipts[0]
        sha = result.get("frame_safety", {}).get("source_image_sha256")
        if (sha and receipt.get("outcome") == "confirmed" and receipt.get("source_sha256") == sha
                and receipt.get("source_path") == decision.get("evidence", {}).get("image")
                and all(receipt.get(k) == tap.get(k) for k in ("x", "y", "scaled"))):
            return True
    return False


def _confirmed_race_warning_pending(steps, run_root):
    """Continue this entry's warning, never an earlier race or uncertain input.

    The executor already confirmed this input. Read-only loading and the same
    denied warning may follow it, including when resuming that checkpoint.
    A later action/page ends this narrow confirmation scope. Expired working
    pixels are not required again; an existing changed source is rejected.
    """
    root = Path(run_root).resolve()
    for row in reversed(steps):
        result = row.get("result", {})
        decision = result.get("decision", {})
        if result.get("tap_outcome") in {"failed", "unknown", "pending"}:
            return False
        if _fixed_race_entry_confirmed([row]):
            if decision.get("reason") != "confirm_race_entry":
                return False  # This warning was already confirmed.
            try:
                source = Path(decision["evidence"]["image"]).resolve()
                sha = result["frame_safety"]["source_image_sha256"]
                if (not source.is_relative_to(root) or source == root
                        or not isinstance(sha, str) or len(sha) != 64
                        or any(c not in "0123456789abcdefABCDEF" for c in sha)):
                    return False
                return not source.exists() or (source.is_file()
                    and _file_sha256(source) == sha.lower())
            except (OSError, ValueError, KeyError, TypeError):
                return False
        if any(result.get(key) for key in ("tap_performed", "input_may_have_occurred", "input_receipts")):
            return False
        loading = (decision.get("action") == "wait"
                   and decision.get("route_state") in {"loading", "race_detail_loading"}
                   and transient_observation(result))
        warning = (result.get("performed") is False and decision.get("action") == "stop"
                   and result.get("reason") == "larc_adaptation_warning:confirmation_not_allowed"
                   and decision.get("evidence", {}).get("larc_adaptation_warning", {}).get("verified") is True)
        if not (loading or warning):
            return False
    return False


def _is_niel_warning_context(steps, *, require_named=False):
    """Recognize this race from its confirmed detail or preceding race-day entry."""
    detail_seen = False
    for row in reversed(steps):
        result = row.get("result", {})
        decision = result.get("decision", {})
        reason = decision.get("reason")
        if not detail_seen:
            if reason != "confirm_race_entry":
                continue
            detail_seen = True
        elif reason != "enter_race_from_race_day":
            continue
        visible = result.get("frame_safety", {}).get("visible_text", "")
        normalized = decision.get("evidence", {}).get("screen_text", {}).get("normalized_text", "")
        text = "".join(f"{visible} {normalized}".split())
        if "尼尔锦标" in text or "尼埃尔锦标" in text:
            return True
        if require_named or reason == "enter_race_from_race_day":
            return False
        if any(f"已攻克的难关{n}/5" in text for n in (3, 4, 5)):
            return True
    return False


def _restore_larc_exchange_pending(session, steps, run_root):
    """Recover an old stopped exchange's owner from its confirmed detail input."""
    for row in reversed(steps):
        result = row.get("result", {})
        decision = result.get("decision", {})
        if (larc_exchange_entry_released(decision)
                or decision.get("reason") in {"cancel_unverified_race_confirm", "leave_race_detail"}):
            session.pending_larc_exchange = None
            return None
        if result.get("input_may_have_occurred") or result.get("tap_outcome") in {"unknown", "pending", "failed"}:
            return None
        if not _fixed_race_entry_confirmed([row]):
            if result.get("performed") is True and decision.get("action") == "tap":
                return None
            continue
        text = "".join(str(result.get("frame_safety", {}).get("visible_text", "")).split())
        if not all(label in text for label in ("赛事详情", "凯旋门代表交流赛", "取消", "参赛")):
            continue
        try:
            source = Path(decision["evidence"]["image"]).resolve()
            if not source.is_relative_to(Path(run_root).resolve()):
                return None
            # The confirmed input above already binds this saved frame SHA.
            # Screenshot retention may have expired; do not demand its pixels again.
            sha = result["frame_safety"]["source_image_sha256"]
            proof = {"source": "larc_exchange.current_detail", "race_kind": "larc_exchange",
                     "source_image_sha256": sha}
            session.consume_target_race_entry()
            session.pending_larc_exchange = proof
            return proof
        except (OSError, ValueError, KeyError, TypeError):
            return None
    return dict(session.pending_larc_exchange) if session.pending_larc_exchange is not None else None


def _stopped_exchange_confirmation_resume(saved, run_root):
    last = (saved.get("steps") or [{}])[-1].get("result", {})
    if (saved.get("status") != "stopped" or saved.get("fan_goal") is not None
            or saved.get("current_input_receipts")
            or saved.get("stop_reason") != last.get("reason")
            or last.get("reason") not in {"race_confirm:target_verification_required",
                "larc_adaptation_warning:confirmation_not_allowed"}
            or last.get("performed") is not False or last.get("decision", {}).get("action") != "stop"
            or any(last.get(key) for key in ("tap_performed", "input_may_have_occurred", "input_receipts"))):
        return False
    return _restore_larc_exchange_pending(RaceRouteSession(), saved.get("steps", []), run_root) is not None


def _goal_achievement_terminal_text(visible_text: str) -> bool:
    compact = "".join(str(visible_text).split())
    if "目标达成" in compact:
        return True
    # RapidOCR occasionally misreads stylized gold '目标达成' as '自标达成' / '日标达成'.
    return ("标达成" in compact or "目标达" in compact) and any(
        anchor in compact for anchor in ("继续", "协助者积分", "粉丝")
    )


def _completed_live_result_handoff(run_root, saved):
    """A cold result can finish without its earlier race entry in this log."""
    stop_reason = saved.get('stop_reason')
    if (saved.get('status') != 'handed_off'
            or stop_reason not in HANDOFF_STOPS
            or saved.get('current_input_receipts')
            or any(value for key, value in saved.get('session', {}).items()
                   if (key.startswith('pending_') or key.endswith('_pending'))
                   and key != 'result_transition_pending')):
        return None
    result_pending = bool(saved.get('session', {}).get('result_transition_pending'))
    rows = saved.get('steps') or []
    if not rows:
        return None
    confirmed_result = False
    for row in rows:
        result = row.get('result', {}); decision = result.get('decision', {}); receipts = result.get('input_receipts') or []
        if (result.get('tap_outcome') in {'failed', 'unknown', 'pending'}
                or result.get('frame_safety', {}).get('dangerous')
                or result.get('frame_safety', {}).get('resource_terms')):
            return None
        if decision.get('action') == 'tap':
            tap = decision.get('tap', {}); sha = result.get('frame_safety', {}).get('source_image_sha256')
            expected = {
                'race_result_continue': 'race_result.continue',
                'larc_explanation_continue': 'safe_advance.larc_explanation_continue',
                'close_story_record_modal': 'safe_advance.announcement_close',
                'race_scene_start_or_continue': 'race_scene.start_or_continue',
                'skip_or_advance_race_scene': 'race_scene.skip_or_advance',
            }.get(decision.get('reason'))
            if (not expected or tap.get('coordinate_name') != expected or not sha
                    or result.get('performed') is not True or result.get('tap_performed') is not True
                    or not receipts or any(receipt.get('outcome') != 'confirmed'
                        or receipt.get('source_sha256') != sha
                        or receipt.get('source_path') != decision.get('evidence', {}).get('image')
                        or any(receipt.get(key) != tap.get(key) for key in ('x', 'y', 'scaled'))
                        for receipt in receipts)):
                return None
            confirmed_result |= decision['reason'] == 'race_result_continue'
        elif (decision.get('action') not in {'wait', 'stop', 'handoff'}
                or any(result.get(key) for key in ('tap_performed', 'input_may_have_occurred', 'input_receipts'))):
            return None
    try:
        last = rows[-1]['result']; source = Path(last['decision']['evidence']['image']).resolve(strict=True)
        is_goal_stop = stop_reason in {'goal_achievement_loading', 'goal_achievement_result'}
        last_decision = last.get('decision', {})
        terminal_ok = (
            last_decision.get('action') == 'stop'
            and _goal_achievement_terminal_text(last.get('frame_safety', {}).get('visible_text', ''))
            if is_goal_stop
            else (last_decision.get('action') == 'stop' or is_route_handoff(last_decision, reason=stop_reason))
        )
        if (not source.is_relative_to(run_root)
                or last.get('performed') is not False or last.get('reason') != stop_reason
                or not terminal_ok):
            return None
        report = json.loads(io_path(source.parent / 'report.json').read_text(encoding='utf-8'))
        handoff = json.loads(io_path(source.parent / 'owner_handoff.json').read_text(encoding='utf-8'))
        live_src = report.get('live_source') or {}
        live_overlay_proof = (
            live_src.get('owner') == 'race'
            and live_src.get('state') == 'race_result'
            and live_src.get('entered') is True
            and live_src.get('race_reason') == 'race_result_continue'
            and (live_src.get('current_result_proof') or {}).get('kind') == 'larc_rank_points_overlay'
        )
        live_scene_proof = (
            live_src.get('owner') == 'race'
            and live_src.get('state') == 'race_scene'
            and live_src.get('entered') is True
            and live_src.get('race_reason') in {
                'race_scene_start_or_continue',
                'skip_or_advance_race_scene',
                'race_scene_running',
            }
            and confirmed_result
            and not result_pending
        )
        if not live_overlay_proof and not live_scene_proof:
            if not is_goal_stop or not confirmed_result or result_pending:
                return None
        if (report.get('is_valid') is not True or report.get('live_takeover') is not True
                or live_src.get('entered') is not True
                or any(report.get(key) != saved.get(key) for key in ('run_session_id', 'device', 'turn_index', 'stop_reason'))
                or report.get('step_reports') != rows or handoff.get('before') != saved.get('owner_before')
                or handoff.get('report_sha256') != _canonical_sha256(report)
                or handoff.get('terminal_source_sha256') != last.get('frame_safety', {}).get('source_image_sha256')):
            return None
        return {
            'basis': 'completed_live_overlay_result' if live_overlay_proof else 'completed_scene_first_result',
            'report_path': str(source.parent / 'report.json'),
            'owner_handoff_path': str(source.parent / 'owner_handoff.json'),
        }
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def _fresh_race_day_preflight(preflight, *, required_text=None):
    rows = preflight.get('step_reports') or []
    if not rows or any(any(row.get('result', {}).get(key) for key in
            ('tap_performed', 'input_may_have_occurred', 'input_receipts')) for row in rows):
        return False
    last = rows[-1].get('result', {}); decision = last.get('decision', {}); packet = last.get('frame_safety', {})
    visible = ''.join(packet.get('visible_text', '').split())
    return (last.get('performed') is False and last.get('reason') == 'race_day_main_menu'
        and is_route_handoff(decision, reason='race_day_main_menu')
        and decision.get('evidence', {}).get('larc_race_day', {}).get('verified') is True
        and packet.get('text_status') == 'ok' and not packet.get('dangerous') and not packet.get('resource_terms')
        and (required_text is None or required_text in visible))


def _fresh_exchange_preflight(preflight):
    return _fresh_race_day_preflight(preflight, required_text='凯旋门代表交流赛')


def _fresh_entry_preflight_safe(preflight, expected_stops):
    rows = preflight.get('step_reports') or []
    if not rows or any(any(row.get('result', {}).get(key) for key in
            ('tap_performed', 'input_may_have_occurred', 'input_receipts')) for row in rows):
        return False
    last = rows[-1].get('result', {}); packet = last.get('frame_safety', {})
    return (last.get('performed') is False and last.get('reason') in expected_stops
        and packet.get('text_status') == 'ok' and not packet.get('dangerous') and not packet.get('resource_terms'))


def _dialogue_handoff_owner(run_root, saved):
    """Resume the same entered race after its source-bound dialogue handoff."""
    if (saved.get("status") != "handed_off" or saved.get("stop_reason") != "post_race_dialogue"
            or not _fixed_race_entry_confirmed(saved.get("steps", []))):
        return None

def _dialogue_handoff_owner(run_root, saved):
    """Resume the same entered race after its source-bound dialogue handoff."""
    if (saved.get("status") != "handed_off" or saved.get("stop_reason") != "post_race_dialogue"
            or not _fixed_race_entry_confirmed(saved.get("steps", []))):
        return None
    try:
        last = saved["steps"][-1]["result"]
        source = Path(last["decision"]["evidence"]["image"]).resolve(strict=True)
        if not source.is_relative_to(run_root):
            return None
        handoff = json.loads((source.parent / "owner_handoff.json").read_text(encoding="utf-8"))
        report = json.loads((source.parent / "report.json").read_text(encoding="utf-8"))
        if (report.get("run_session_id") != saved["run_session_id"]
                or report.get("device") != saved["device"]
                or report.get("turn_index") != saved["turn_index"]
                or report.get("stop_reason") != "post_race_dialogue"
                or report.get("step_reports") != saved["steps"]
                or handoff.get("before") != saved["owner_before"]
                or handoff.get("report_sha256") != _canonical_sha256(report)
                or handoff.get("terminal_source_sha256") != _file_sha256(source)
                or last.get("performed") is not False
                or any(last.get(k) for k in ("tap_performed", "input_may_have_occurred", "input_receipts"))
                or saved.get("current_input_receipts")):
            return None
        owner = handoff["after"]
        current = RouteTransitionSession.load(run_root / "session/transition_session.json",
            expected_run_session_id=saved["run_session_id"]).to_dict()
        story_actions = {"event_choice.top", "event_choice.second", "event_choice.third",
            "event_choice.fourth", "event_choice.bottom", "event_choice.dialogue_next",
            "safe_advance.dialogue_box", "safe_advance.dialogue_next",
            "wait:loading", "wait:bottom_skip_double_enabled"}
        # The common route legitimately advances the pre-race story after our
        # handoff. Resume that same entered race from its CURRENT owner, not the
        # frozen pre-story snapshot. No entry input or ledger resync is needed.
        progressed = (current != owner and current["status"] == "tracking"
            and current["mismatch_state"] is None
            and current["current_state"] in {"dialogue", "event_choice", "loading"}
            and current["last_action_id"] in story_actions
            and current["action_source_sha256"] is not None
            and current["transition_version"] > owner["transition_version"])
        return {"owner": current if progressed else owner,
                "story_progressed": progressed,
                "handoff_path": str(source.parent / "owner_handoff.json"),
                "report_path": str(source.parent / "report.json"), "report_sha256": handoff["report_sha256"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _niel_hazard_count_from_ready(text, session, reason):
    """Use verified returned levels. Readable contradictory details still veto."""
    from core.auto_trainer.larc_adaptation import phase_required_levels
    from core.auto_trainer.larc_language_deferral import unresolved_hazards_within

    names = ("不习惯的草地", "未知赛道", "时差疲劳", "海外饮食", "语言不通")
    if reason == "larc_adaptation_already_ready":
        # The existing return verifier compares the full observed level list
        # to the plan targets when the zero-cost plan changed no levels.
        levels = session.plan["targets"]
        if levels != session.plan["baseline_levels"]:
            return None
    else:
        levels = session.receipt["levels"]
    if len(levels) < len(names) or any(type(level) is not int for level in levels[:len(names)]):
        return None
    required = phase_required_levels(session.phase)
    unresolved = {name for slot, name in enumerate(names) if levels[slot] < 2}
    if any(names[slot] in unresolved for slot in required):
        return None
    count = len(names) - len(unresolved)
    headers = re.findall(r"已攻克的难关(\d+)/(\d+)", "".join(text.split()))
    if any((int(cleared), int(total)) != (count, len(names)) for cleared, total in headers):
        return None
    for line in text.splitlines():
        line = "".join(line.split())
        named = [name for name in names if name in line]
        if len(named) == 1 and ("未克服" in line) != ("已克服" in line):
            if ("未克服" in line) != (named[0] in unresolved):
                return None
    if (unresolved_hazards_within(text, names)
            and not unresolved_hazards_within(text, unresolved)):
        return None
    return count


def _prepared_race_optional_upgrade(run_root, device, steps, *, readiness_report=None):
    """One-step permission after this race's confirmed, fully prepared detail."""
    def bound_source(result, *, input_required=False):
        decision = result["decision"]
        path = Path(decision["evidence"]["image"]).resolve(strict=True)
        if not path.is_relative_to(run_root):
            raise ValueError("outside_run")
        sha = _file_sha256(path)
        if (result["frame_safety"]["source_image_sha256"] != sha
                or result["safety_verdict"].get("source_image_sha256") != sha
                or result["safety_verdict"].get("allowed") is not True):
            raise ValueError("source_unverified")
        if input_required:
            receipts = result.get("input_receipts", [])
            tap = decision["tap"]
            if (result.get("tap_performed") is not True or len(receipts) != 1
                    or receipts[0].get("outcome") != "confirmed"
                    or receipts[0].get("source_sha256") != sha
                    or Path(receipts[0]["source_path"]).resolve() != path
                    or result["safety_verdict"].get("action_id") != tap.get("coordinate_name")
                    or any(receipts[0].get(k) != tap.get(k) for k in ("x", "y", "scaled"))):
                raise ValueError("input_unverified")
        return sha

    try:
        index = len(steps) - 1
        while index >= 0:
            row = steps[index]["result"]
            decision = row["decision"]
            no_input = (not row.get("input_receipts") and row.get("tap_performed") is not True
                        and row.get("input_may_have_occurred") is not True)
            loading = (decision.get("action") == "wait" and decision.get("reason") == "loading"
                       and decision.get("route_state") in {"loading", "race_detail_loading"})
            warning = (row.get("performed") is False and decision.get("action") == "stop"
                       and row.get("reason") == "larc_adaptation_warning:confirmation_not_allowed"
                       and decision.get("evidence", {}).get("larc_adaptation_warning", {}).get("verified") is True)
            if not (no_input and (loading or warning)):
                break
            bound_source(row)
            index -= 1
        if index < 1:
            return None
        detail = steps[index]["result"]
        decision = detail["decision"]
        if (detail.get("performed") is not True or decision.get("reason") != "confirm_race_entry"
                or decision.get("consume_target_race_verification") is not True):
            return None
        sha = bound_source(detail, input_required=True)
        safety = detail["frame_safety"]
        visible_text = str(safety.get("visible_text", ""))
        text = "".join(visible_text.split())
        language_deferred = False
        verified_niel_deferral = False
        language_turn = None
        legacy_niel_phase = None
        niel_receipt_phase = None
        if _is_niel_warning_context(steps, require_named=True):
            from core.auto_trainer.larc_adaptation import POWER_DEFERRED_PHASES
            from core.auto_trainer.larc_language_deferral import (
                language_only_unresolved, selected_language_deferred_phase,
            )
            progress = json.loads((run_root / "progress.json").read_text(encoding="utf-8"))
            if (progress.get("run_session_id") != run_root.name or progress.get("device") != device
                    or type(progress.get("next_turn")) is not int):
                return None
            language_turn = progress["next_turn"]
            selected = selected_language_deferred_phase(
                run_root, "first_overseas_race", language_turn, load_goal_config(run_root / "goal.json"))
            if selected in POWER_DEFERRED_PHASES:
                niel_receipt_phase = selected
        if niel_receipt_phase:
            phase = niel_receipt_phase
            if (phase == "first_overseas_race_defer_power_language"
                    and "已攻克的难关4/5" in text and language_only_unresolved(visible_text)):
                legacy_niel_phase = "first_overseas_race_defer_language"
        elif "未克服" in text:
            from core.auto_trainer.larc_language_deferral import (
                selected_language_deferred_phase, language_only_unresolved, unresolved_hazards_within,
            )
            if any(f"已攻克的难关{n}/5" in text for n in (3, 4)) and _is_niel_warning_context(steps):
                base_phase, total_count = "first_overseas_race", 5
                count = 3 if "已攻克的难关3/5" in text else 4
                labels = ("未知赛道", "不习惯的草地", "时差疲劳", "海外饮食", "语言不通")
            elif "凯旋门奖" in text and "已攻克的难关7/9" in text:
                base_phase, count, total_count = "first_arc", 7, 9
                labels = ("凯旋门奖", "不习惯的草地", "未知赛道", "时差疲劳", "海外饮食", "长途旅行", "语言不通")
            else:
                return None
            progress = json.loads((run_root / "progress.json").read_text(encoding="utf-8"))
            if (progress.get("run_session_id") != run_root.name or progress.get("device") != device
                    or type(progress.get("next_turn")) is not int):
                return None
            language_turn = progress["next_turn"]
            phase = selected_language_deferred_phase(run_root, base_phase, language_turn,
                                                     load_goal_config(run_root / "goal.json"))
            allowed = {"first_overseas_race_defer_power": {"时差疲劳"},
                       "first_overseas_race_defer_power_language": {"时差疲劳", "语言不通"}}.get(phase)
            if allowed is not None:
                # The detail header counts every Niel hazard, including rows OCR can miss.
                if (text.count("未克服") != total_count - count
                        or not unresolved_hazards_within(safety.get("visible_text", ""), allowed)):
                    return None
                verified_niel_deferral = True
                if (phase == "first_overseas_race_defer_power_language" and count == 4
                        and language_only_unresolved(safety.get("visible_text", ""))):
                    legacy_niel_phase = "first_overseas_race_defer_language"
            elif (phase == base_phase or (base_phase == "first_overseas_race" and count != 4)
                    or not language_only_unresolved(safety.get("visible_text", ""))):
                return None
            language_deferred = True
        elif "凯旋门奖" in text and "已攻克的难关9/9" in text:
            phase, count = "final_arc", 9
            labels = ("凯旋门奖", "不习惯的草地", "未知赛道", "时差疲劳",
                      "海外饮食", "长途旅行", "语言不通")
        elif "凯旋门奖" in text and "已攻克的难关8/9" in text:
            # The existing first-Arc plan prepares eight adaptations. Require
            # that exact phase's completed receipt, not a final-Arc shortcut.
            phase, count = "first_arc", 8
            labels = ("凯旋门奖", "不习惯的草地", "未知赛道", "时差疲劳",
                      "海外饮食", "长途旅行", "语言不通")
        elif "福伊锦标" in text:
            phase, count = "foy_prelude", 8
            labels = ("福伊锦标", "不习惯的草地", "未知赛道", "时差疲劳",
                      "海外饮食", "长途旅行", "语言不通")
        elif "已攻克的难关5/5" in text and _is_niel_warning_context(steps):
            phase, count = "first_overseas_race", 5
            progress = json.loads((run_root / "progress.json").read_text(encoding="utf-8"))
            if (progress.get("run_session_id") != run_root.name or progress.get("device") != device
                    or type(progress.get("next_turn")) is not int):
                return None
            from core.auto_trainer.larc_strategy_versions import strategy_revision
            niel_goal = load_goal_config(run_root / "goal.json")
            if strategy_revision(niel_goal) in ("1.9.8", "1.9.9", "1.9.10", "1.9.11", "1.9.12", "2.0", "2.1", "2.2"):
                from core.auto_trainer.larc_language_deferral import selected_language_deferred_phase
                phase = selected_language_deferred_phase(run_root, phase, progress["next_turn"], niel_goal)
                language_turn = progress["next_turn"]
            labels = ("未知赛道", "不习惯的草地", "时差疲劳", "海外饮食", "语言不通")
        elif "已攻克的难关6/6" in text:
            phase, count = "second_exchange", 6
            labels = ("凯旋门代表交流赛", "未知赛道", "不习惯的草地", "时差疲劳",
                      "海外饮食", "语言不通", "长途旅行")
        elif "已攻克的难关3/3" in text:
            phase, count = "first_exchange", 3
            labels = ("凯旋门代表交流赛", "未知赛道", "不习惯的草地", "时差疲劳")
        else:
            phase, count = "first_exchange", 2
            labels = ("凯旋门代表交流赛", "未知赛道", "不习惯的草地")
        if safety.get("text_status") != "ok":
            return None
        if not niel_receipt_phase:
            visible_count = (5 if base_phase == "first_arc" else count) if language_deferred else (
                6 if phase in {"foy_prelude", "first_arc", "final_arc"} else count)
            if not language_deferred:
                total_count = 9 if phase == "first_arc" else count
            if (("未克服" in text and not language_deferred)
                    or f"已攻克的难关{count}/{total_count}" not in text):
                return None
            # The legacy count-only Niel identity still requires each
            # unresolved row. Missing already-cleared rows remain harmless.
            if not verified_niel_deferral and (
                    text.count("已克服") != visible_count or any(label not in text for label in labels)):
                return None
        proof = decision["evidence"]["prior_target_race_verification"]
        entries = [r["result"] for r in steps[:index] if r["result"].get("decision", {}).get("action") != "wait"]
        entry = entries.pop() if entries else None
        if entry and entry["decision"].get("reason") == "enter_selected_target_race":
            if (entry.get("performed") is not True
                    or entry["decision"].get("target_race_verification") != proof):
                return None
            bound_source(entry, input_required=True)
            proof = entry["decision"].get("evidence", {}).get("prior_locked_target_verification")
            entry = entries.pop() if entries else None
        if entry and entry["decision"].get("reason") == "enter_locked_target_race_from_list":
            if (entry.get("performed") is not True
                    or entry["decision"].get("evidence", {}).get("prior_locked_target_verification") != proof):
                return None
            bound_source(entry, input_required=True)
            entry = entries.pop() if entries else None
        if (not entry or entry["decision"].get("reason") != "enter_race_from_race_day"
                or entry["decision"].get("locked_target_verification") != proof
                or proof.get("source_image_sha256") != bound_source(entry, input_required=True)):
            return None
        RaceRouteSession().stage_locked_target_race_entry(entry["decision"])
        if phase in {"second_exchange", "foy_prelude"}:
            return {"phase": phase, "detail_source_sha256": sha, "detail_step": steps[index]["step"],
                    "reason": f"{phase}_race_hazards_{count}_of_{count}_cleared"}
        if readiness_report is None:
            ready_index = json.loads((run_root / "session/larc_adaptation_ready.json").read_text(encoding="utf-8"))
            if ready_index.get("run_session_id") != run_root.name or ready_index.get("device") != device:
                return None
            phases = ready_index["phases"]
            reference = phases.get(phase) or (phases.get(legacy_niel_phase) if legacy_niel_phase else None)
            if not reference:
                return None
            report_path = Path(reference).resolve(strict=True)
        else:
            report_path = Path(readiness_report).resolve(strict=True)
        if not report_path.is_relative_to(run_root):
            return None
        ready = json.loads(report_path.read_text(encoding="utf-8"))
        ready_phase = ready.get("phase")
        if legacy_niel_phase and ready_phase == legacy_niel_phase:
            phase = legacy_niel_phase
        elif ready_phase != phase and not (readiness_report is not None
                and phase == "first_exchange" and ready_phase == "first_expedition"):
            return None
        session_path = run_root / "session" / f"larc_adaptation_{ready_phase}.json"
        session = LarcAdaptationSession.load(session_path, run_root.name)
        if (ready.get("run_session_id") != run_root.name or ready.get("device") != device
                or (niel_receipt_phase or language_deferred or "defer_power" in phase)
                and ready.get("turn_index") != language_turn
                or session.phase != ready_phase
                or ready.get("phase_session_path") != str(session_path) or ready.get("session") != session.to_dict()
                or ready.get("is_valid") is not True or ready.get("status") != "ready"
                or ready.get("phase_complete") is not True or not session.returned or session.inspect_only
                or ready.get("safe_advance_exit_code") != 0):
            return None
        child_path = Path(ready["safe_advance_report_path"]).resolve(strict=True)
        if not child_path.is_relative_to(run_root):
            return None
        child = json.loads(child_path.read_text(encoding="utf-8"))
        if (child.get("is_valid") is not True or any(
                not Path(r.get("screenshot_path", "")).resolve().is_relative_to(run_root)
                for r in child.get("step_reports", []))):
            return None
        reason = _verified_ready_reason(session, child)
        if reason is None or reason != ready.get("stop_reason"):
            return None
        if niel_receipt_phase:
            count = _niel_hazard_count_from_ready(visible_text, session, reason)
            if count is None:
                return None
            total_count = 5
            language_deferred = count < total_count
        return {"phase": phase, "ready_report_path": str(report_path),
                **({"language_debuff_deferred": True, "skipped_slots": [4]} if language_deferred and phase in {"first_overseas_race_defer_language", "first_arc_defer_language"} else {}),
                **({"allowed_unresolved_phase": phase} if language_deferred and "defer_power" in phase else {}),
                "detail_source_sha256": sha, "detail_step": steps[index]["step"],
                "reason": f"{phase}_required_adaptations_ready_and_race_hazards_{count}_of_{total_count}"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _adaptation_reentry_evidence(run_root, saved, report_path, device, turn):
    """Bind an explicit new entry segment to the completed purchase, retaining history."""
    try:
        last = saved["steps"][-1]["result"]
        text = "".join(str(last.get("frame_safety", {}).get("visible_text", "")).split())
        idle_disconnect = (saved.get("stop_reason") == "connection_conflict"
            and saved.get("adaptation_reentry", {}).get("readiness_report_path") == str(Path(report_path).resolve())
            and len(saved["steps"]) == 2 and saved["steps"][0]["result"].get("reason") == "enter_race_from_race_day"
            and all(term in text for term in ("连接已断开", "长时间内未进行操作", "与服务器的连接发生中断", "返回标题页面"))
            and not any(term in text for term in ("账号", "其他设备", "别的设备", "其他终端")))
        if (saved.get("turn_index") != turn or saved.get("fan_goal") is not None
                or saved.get("status") != "stopped" or saved.get("current_input_receipts")
                or (saved.get("stop_reason") != "larc_adaptation_warning:confirmation_not_allowed" and not idle_disconnect)
                or last.get("reason") != saved["stop_reason"] or last.get("performed") is not False
                or last.get("decision", {}).get("action") != "stop"
                or any(last.get(k) for k in ("tap_performed", "input_may_have_occurred", "input_receipts"))
                or any(r.get("outcome") != "confirmed" for row in saved["steps"]
                       for r in row["result"].get("input_receipts", []))
                or any(row["result"].get("reason") not in {"enter_race_from_race_day", "confirm_race_entry",
                    "loading", "enter_locked_target_race_from_list", "enter_selected_target_race",
                    "larc_adaptation_warning:confirmation_not_allowed",
                    *( {"connection_conflict"} if idle_disconnect else set())} for row in saved["steps"])):
            return None
        path = Path(report_path).resolve(strict=True)
        if not path.is_relative_to(run_root):
            return None
        ready = json.loads(path.read_text(encoding="utf-8"))
        phase = ready.get("phase")
        if phase not in {"first_exchange", "first_expedition", "foy_prelude"}:
            return None
        session_path = run_root / "session" / f"larc_adaptation_{phase}.json"
        session = LarcAdaptationSession.load(session_path, run_root.name)
        child_path = Path(ready["safe_advance_report_path"]).resolve(strict=True)
        if (not child_path.is_relative_to(run_root) or ready.get("run_session_id") != run_root.name
                or ready.get("device") != device or ready.get("turn_index") != turn
                or ready.get("phase_session_path") != str(session_path) or ready.get("session") != session.to_dict()
                or ready.get("is_valid") is not True or ready.get("status") != "ready"
                or ready.get("phase_complete") is not True or ready.get("safe_advance_exit_code") != 0
                or session.inspect_only or not session.returned):
            return None
        child = json.loads(child_path.read_text(encoding="utf-8"))
        reason = _verified_ready_reason(session, child)
        if child.get("is_valid") is not True or not reason or reason != ready.get("stop_reason"):
            return None
        return {"readiness_report_path": str(path), "readiness_report_sha256": _file_sha256(path),
                "race_title": "福伊锦标" if phase == "foy_prelude" else "凯旋门代表交流赛",
                "reason": "adaptation_completed_returned_to_race_home"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _revalidate_exchange_entry(run_root, saved, session):
    """Read-only exception for one confirmed exchange entry's rejected postcondition."""
    try:
        if len(saved["steps"]) != 1:
            return None
        result = saved["steps"][0]["result"]
        receipts = result.get("input_receipts", [])
        if (result.get("performed") is not False or result.get("tap_performed") is not True
                or result.get("reason") != "race_day_entry_postcondition_failed"
                or len(receipts) != 1 or receipts != saved.get("current_input_receipts")
                or receipts[0].get("outcome") != "confirmed"):
            return None
        decision, receipt = result["decision"], receipts[0]
        source = Path(decision["evidence"]["image"]).resolve()
        post = Path(result["post_tap_frame"]["path"]).resolve()
        if (not source.is_relative_to(run_root) or post.parent != source.parent
                or Path(receipt["source_path"]).resolve() != source):
            return None
        source_sha = _file_sha256(source)
        post_sha = _file_sha256(post)
        proof = decision["locked_target_verification"]
        verdict = result["safety_verdict"]
        if (source_sha != receipt["source_sha256"]
                or source_sha != result["frame_safety"]["source_image_sha256"]
                or source_sha != verdict.get("source_image_sha256")
                or verdict.get("allowed") is not True or verdict.get("action_id") != "race_day.practice"
                or post_sha != result["post_tap_frame"]["sha256"]
                or proof != session.pending_locked_target_race
                or proof.get("source_image_sha256") != source_sha
                or any(receipt.get(k) != decision["tap"].get(k) for k in ("x", "y", "scaled"))):
            return None
        old_checkpoint, old_report = source.parent / "checkpoint.json", source.parent / "report.json"
        if json.loads(old_checkpoint.read_text(encoding="utf-8")) != saved:
            return None
        report = json.loads(old_report.read_text(encoding="utf-8"))
        if (report.get("is_valid") is not False or report.get("step_reports") != saved["steps"]
                or any(report.get(k) != saved.get(k) for k in ("run_session_id", "device", "turn_index", "stop_reason"))):
            return None
        verified = RaceRoutePlanner().larc_exchange_entry_postcondition(
            post, entry_decision=decision, session=session)
        if verified is None:
            return None
        return {"checkpoint_path": str(old_checkpoint), "report_path": str(old_report),
                "entry_source_sha256": source_sha, "post_tap_path": str(post),
                "post_tap_sha256": post_sha, "decision": verified}
    except (OSError, ValueError, KeyError, TypeError):
        return None


class _ReceiptADB:
    """Preserve actual input outcomes even when the executor later raises."""
    def __init__(self, adb, record):
        self.adb, self.record, self.source = adb, record, None

    def __getattr__(self, name):
        return getattr(self.adb, name)

    def screenshot(self, path):
        result = self.adb.screenshot(path)
        self.source = Path(path)
        return result

    @guarded_input
    def tap(self, x, y, *, scaled=False):
        receipt = {"source_path": str(self.source.resolve()),
                   "source_sha256": _file_sha256(self.source),
                   "x": x, "y": y, "scaled": scaled, "outcome": "pending"}
        self.record(receipt)
        try:
            result = self.adb.tap(x, y, scaled=scaled)
        except Exception:
            receipt["outcome"] = "unknown"
            self.record(receipt)
            raise
        # Reaching the native adapter without a confirmed result does not
        # prove zero input. The pre-input SafetyGate never reaches this hook.
        receipt["outcome"] = "confirmed" if result is True else "unknown"
        self.record(receipt)
        if result is not True:
            raise RuntimeError("race_tap_outcome_" + receipt["outcome"])
        return True


def run_larc_race_route(*, run_root: Path, output_dir: Path, adb_port: int,
                        max_steps: int = 24, resume: bool = False, fan_goal=None,
                        incomplete_first_arc_turn=None, resume_after_adaptation=None,
                        optional_upgrade_readiness_report=None, new_exchange_after_race=False,
                        safety_analyzer=None,debut_clock_retry=None,first_win_rescue=False,live_source=None,
                        custom_race: dict | None = None):
    from contextlib import ExitStack
    with ExitStack() as resources:
        return _run_larc_race_route(run_root=run_root, output_dir=output_dir, adb_port=adb_port,
            max_steps=max_steps, resume=resume, fan_goal=fan_goal,
            incomplete_first_arc_turn=incomplete_first_arc_turn, resume_after_adaptation=resume_after_adaptation,
            optional_upgrade_readiness_report=optional_upgrade_readiness_report,
            new_exchange_after_race=new_exchange_after_race, resources=resources,
            safety_analyzer=safety_analyzer,debut_clock_retry=debut_clock_retry,
            first_win_rescue=first_win_rescue, live_source=live_source,
            custom_race=custom_race)


def _run_larc_race_route(*, run_root: Path, output_dir: Path, adb_port: int,
                        max_steps: int, resume: bool, fan_goal,
                        incomplete_first_arc_turn, resume_after_adaptation,
                        optional_upgrade_readiness_report, new_exchange_after_race, resources,
                        safety_analyzer=None,debut_clock_retry=None,first_win_rescue=False,live_source=None,
                        custom_race: dict | None = None):
    run_root, output_dir = Path(run_root).resolve(), Path(output_dir).resolve()
    if debut_clock_retry is not None and type(debut_clock_retry) is not bool:
        raise ValueError('debut_clock_retry_invalid')
    if (not output_dir.is_relative_to(run_root) or output_dir == run_root
            or type(max_steps) is not int or not 1 <= max_steps <= 24):
        raise ValueError("larc_race_output_or_budget_invalid")
    progress = json.loads((run_root / "progress.json").read_text(encoding="utf-8"))
    run_id = progress.get("run_session_id")
    if (run_id != run_root.name or type(adb_port) is not int
            or progress.get("device") != f"127.0.0.1:{adb_port}"
            or type(progress.get("next_turn")) is not int or progress["next_turn"] < 1):
        raise ValueError("larc_race_run_or_device_mismatch")
    goal = load_goal_config(run_root / "goal.json")
    from core.auto_trainer.race_retry_budget import ClockBudget
    from core.auto_trainer.larc_strategy_versions import strategy_revision
    clock_budget = ClockBudget(run_root, goal.race_retry)
    if goal.scenario != "larc" or not goal.execution.allow_race:
        raise ValueError("larc_race_execution_not_authorized")
    turn = progress["next_turn"]
    if incomplete_first_arc_turn is not None and (type(incomplete_first_arc_turn) is not int
            or incomplete_first_arc_turn != turn or fan_goal is not None):
        raise ValueError("first_arc_exception_turn_or_mode_mismatch")
    if fan_goal is not None:
        try:
            source = fan_goal["source"]
            path = Path(source["path"]).resolve(strict=True)
            if (fan_goal.get("schema_version") != "larc_fan_goal.v1" or fan_goal.get("status") != "accepted"
                    or fan_goal.get("target_id") != "larc_japan_derby_top3"
                    or type(fan_goal.get("remaining_fans")) is not int or fan_goal["remaining_fans"] <= 0
                    or type(fan_goal.get("remaining_turns")) is not int or not 1 <= fan_goal["remaining_turns"] <= 6
                    or source.get("run_session_id") != run_id or type(source.get("turn_index")) is not int
                    or source["turn_index"] not in ({turn, turn-1} if resume else {turn})
                    or not path.is_relative_to(run_root) or _file_sha256(path) != source.get("sha256")):
                raise ValueError("fan_goal_binding_invalid")
            turn = source["turn_index"]
        except (OSError, KeyError, TypeError) as exc:
            raise ValueError("fan_goal_binding_invalid") from exc
    route_path, skip_path = (run_root / "session" / name for name in ("transition_session.json", "skip_session.json"))
    RouteTransitionSession.load(route_path, expected_run_session_id=run_id)
    RunSkipSession.load(skip_path, expected_run_session_id=run_id)
    owner_args = SimpleNamespace(route_transition_json=str(route_path), run_session_id=run_id)
    output_dir.mkdir(parents=True, exist_ok=False)
    checkpoint = run_root / "session/larc_race_checkpoint.json"
    report = {"is_valid": False, "status": "stopped", "run_session_id": run_id,
              "device": progress["device"], "turn_index": turn,
              "performed_steps": 0, "step_reports": []}
    preflight={}
    fan_list_resume = False
    clock_usage_baseline = _clock_usage([])
    if fan_goal is not None:
        report["fan_goal"] = fan_goal
    if custom_race is not None:
        if not isinstance(custom_race, dict) or not isinstance(custom_race.get("race_id") or custom_race.get("id"), str):
            raise ValueError("custom_race_spec_invalid")
        report["custom_race"] = dict(custom_race)
    if incomplete_first_arc_turn is not None:
        report["incomplete_first_arc_turn"] = incomplete_first_arc_turn

    def finish(reason, code=2):
        report["stop_reason"] = reason
        _atomic_write_json(output_dir / "report.json", report)
        return report, code

    if live_source is not None:
        source_path = Path(live_source.get('source', '')).resolve(strict=True)
        if (live_source.get('owner') != 'race' or live_source.get('state') not in
                {'race_scene','race_detail','race_result','race_list','modal'}
                or not source_path.is_relative_to(run_root)
                or _file_sha256(source_path) != live_source.get('source_sha256')):
            return finish('live_race_source_invalid')
        report['live_takeover'] = True
        report['live_source'] = live_source
        if live_source.get('ordinary_clock_retry') is True:
            report['ordinary_clock_retry'] = True
    saved = (None if live_source is not None else
        json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else None)
    if resume and saved:
        first_win_rescue = bool(saved.get("first_win_rescue"))
    if first_win_rescue:
        report["first_win_rescue"] = True
    if saved and (saved["run_session_id"] != run_id or saved["device"] != progress["device"]):
        return finish("race_checkpoint_context_mismatch")
    superseded = None
    if resume_after_adaptation is not None:
        evidence = (_adaptation_reentry_evidence(run_root, saved, resume_after_adaptation, progress["device"], turn)
                    if saved and not resume and fan_goal is None and incomplete_first_arc_turn is None else None)
        if evidence is None:
            return finish("race_adaptation_reentry_unverified")
        superseded, saved = saved, None
        report["adaptation_reentry"] = evidence
    if saved and saved["status"] == "handed_off" and saved["turn_index"] != turn:
        saved = None
    cold_result_handoff = (_completed_live_result_handoff(run_root, saved)
        if saved and not resume and saved.get('turn_index') == turn
        and not _fixed_race_entry_confirmed(saved.get('steps', [])) else None)
    if (saved and new_exchange_after_race and not resume and fan_goal is None
            and saved["status"] == "handed_off" and saved["turn_index"] == turn
            and (_fixed_race_entry_confirmed(saved.get("steps", [])) or cold_result_handoff)):
        superseded, saved = saved, None
        report["consecutive_exchange"] = {"prior_turn_index": turn}
        if cold_result_handoff:
            report['consecutive_exchange']['cold_result_handoff'] = cold_result_handoff
    elif (saved and not resume and saved["status"] == "handed_off" and saved["turn_index"] == turn
            and cold_result_handoff and cold_result_handoff.get("basis") == "completed_live_overlay_result"):
        superseded, saved = saved, None
        report["prior_live_result_handoff"] = {
            "prior_turn_index": turn,
            "cold_result_handoff": cold_result_handoff,
        }
    if saved:
        if saved.get("consecutive_exchange"):
            report["consecutive_exchange"] = saved["consecutive_exchange"]
        if saved.get("adaptation_reentry"):
            report["adaptation_reentry"] = saved["adaptation_reentry"]
        if saved.get("prior_live_result_handoff"):
            report["prior_live_result_handoff"] = saved["prior_live_result_handoff"]
        if resume and saved.get("incomplete_first_arc_turn") != incomplete_first_arc_turn:
            # A first-Arc warning may be observed before the caller opts into
            # the single-turn exception. Bind that late choice to this exact
            # stopped race; never change an already recorded exception.
            late_first_arc = (saved.get("incomplete_first_arc_turn") is None
                and incomplete_first_arc_turn == turn
                and saved.get("status") == "stopped"
                and saved.get("stop_reason") == "larc_adaptation_warning:confirmation_not_allowed"
                and _fixed_race_entry_confirmed(saved.get("steps", []))
                and any(step.get("result", {}).get("decision", {}).get("reason") == "confirm_race_entry"
                        and "凯旋门奖" in str(step.get("result", {}).get("frame_safety", {}).get("visible_text", ""))
                        for step in saved.get("steps", [])))
            if not late_first_arc:
                return finish("first_arc_exception_resume_mismatch")
        if saved["turn_index"] != turn or saved.get("fan_goal") != fan_goal:
            return finish("race_checkpoint_context_mismatch")
        if saved.get("fan_race_receipt"):
            prior = saved["fan_race_receipt"]
            verified = _fan_receipt_from_input(run_root, progress["device"], fan_goal,
                prior.get("confirmation_step"), prior.get("decision"), prior.get("input_receipt"))
            if verified != prior:
                return finish("fan_race_receipt_unverified")
            report["fan_race_receipt"] = prior
        if progress["next_turn"] != turn and not report.get("fan_race_receipt"):
            return finish("race_checkpoint_context_mismatch")
        entry_recovery = (saved["status"] == "blocked"
                          and saved["stop_reason"] == "race_day_entry_postcondition_failed")
        last = saved["steps"][-1]["result"] if saved.get("steps") else {}
        loading_resource_stop = False
        last_text = str(last.get("frame_safety", {}).get("visible_text", ""))
        if (saved["status"] == "stopped" and saved["stop_reason"] == last.get("reason") == "resource_prompt"
                and "加载中" in last_text and last.get("performed") is False
                and last.get("decision", {}).get("action") == "stop"
                and (_fixed_race_entry_confirmed(saved.get("steps", [])) or report.get("fan_race_receipt"))
                and not any(last.get(key) for key in ("tap_performed", "input_may_have_occurred", "input_receipts"))
                and not saved.get("current_input_receipts")):
            old_frame = last.get("decision", {}).get("evidence", {}).get("image")
            if old_frame and Path(old_frame).is_file():
                replay = RaceRoutePlanner().plan(old_frame, policy=RaceRoutePolicy(
                    scenario="larc", post_entry_only=True, visible_text=last_text))
                loading_resource_stop = replay.get("action") == "wait" and replay.get("reason") == "loading"
        trophy_reward_stop = (saved["stop_reason"] == "resource_prompt" and all(
            label in str(last.get("decision", {}).get("evidence", {}).get("screen_text", {}).get("normalized_text", ""))
            for label in ("奖杯陈列室", "关闭")))
        fan_route_resume = (fan_goal is not None
            and saved["stop_reason"] == last.get("reason")
            and ((not saved.get("fan_race_receipt") and saved["stop_reason"] == "fan_goal_shortfall:candidate_unverified")
                 or (report.get("fan_race_receipt") and (
                     saved["stop_reason"] == "fan_goal_shortfall:entry_not_allowed"
                     or trophy_reward_stop
                     or (saved["stop_reason"] == "race_confirm:confirm_not_allowed"
                         and last.get("decision", {}).get("route_state") == "race_playback_confirm"))))
            and last.get("performed") is False and last.get("decision", {}).get("action") == "stop"
            and not any(last.get(key) for key in ("tap_performed", "input_may_have_occurred", "input_receipts"))
            and not saved.get("current_input_receipts"))
        fixed_route_resume = (fan_goal is None and _fixed_race_entry_confirmed(saved.get("steps", []))
            and saved["stop_reason"] == last.get("reason")
            and saved["stop_reason"] in {"race_list:selected_grade_not_g1", "race_list:enter_not_allowed",
                                         "goal_achievement_unverified"}
            and last.get("performed") is False and last.get("decision", {}).get("action") == "stop"
            and not any(last.get(key) for key in ("tap_performed", "input_may_have_occurred", "input_receipts"))
            and not saved.get("current_input_receipts"))
        text_transition_resume = (_fixed_race_entry_confirmed(saved.get("steps", []))
            and _race_text_transition(last,
                result_transition_pending=saved.get("session", {}).get("result_transition_pending") is True)
            and not saved.get("current_input_receipts"))
        date_change_resume=_date_change_safety_resume(run_root,saved,last)
        confirmed_result_resume = _confirmed_result_repeat_resume(saved)
        catalog_read_error_resume = _catalog_read_error_resume(saved, last)
        # A denied warning precedes input. A separate current receipt means an
        # unmerged input attempt, so retain that checkpoint for existing review
        # instead of clearing it and borrowing an older entry confirmation.
        normal_resume = (saved["status"] == "stopped" and (date_change_resume or text_transition_resume or fan_route_resume or fixed_route_resume or loading_resource_stop or saved["stop_reason"] in {
            "max_steps_reached", "race_transition_no_progress_timeout", "no_progress_timeout", "gur_race:enter_not_allowed"}
            or (saved["stop_reason"] == "larc_adaptation_warning:confirmation_not_allowed"
                and not saved.get("current_input_receipts"))
            or (saved["stop_reason"] in {"race_confirm:confirm_not_allowed", "race_confirm:target_verification_required"}
                and last.get("performed") is False
                and not any(last.get(key) for key in ("tap_performed", "input_may_have_occurred", "input_receipts"))
                and not saved.get("current_input_receipts"))))
        fan_list_resume = _fan_shortfall_list_resume(saved, last)
        exchange_confirmation_resume = _stopped_exchange_confirmation_resume(saved, run_root)
        normal_resume = normal_resume or confirmed_result_resume or catalog_read_error_resume or fan_list_resume or exchange_confirmation_resume
        dialogue_owner = _dialogue_handoff_owner(run_root, saved) if fan_goal is None else None
        if not resume or not (normal_resume or entry_recovery or dialogue_owner is not None):
            return finish("race_checkpoint_requires_review")
        if dialogue_owner is not None:
            owner_args.verified_race_dialogue_handoff_owner = dialogue_owner["owner"]
        elif normal_resume and _fixed_race_entry_confirmed(saved.get("steps", [])):
            # A resumed post-race segment may itself exhaust its step budget.
            # Keep its existing owner; the exact session comparison below still
            # rejects any intervening page ownership change.
            owner_args.verified_race_dialogue_handoff_owner = saved["owner_before"]
        owner = _load_race_owner_session(owner_args)
        if owner.to_dict() != (dialogue_owner["owner"] if dialogue_owner is not None else saved["owner_before"]):
            return finish("race_checkpoint_owner_changed")
        if dialogue_owner is not None:
            report["dialogue_handoff_resume"] = dialogue_owner
        if date_change_resume:report['date_change_safety_resume']=True
        if catalog_read_error_resume:report['goal_catalog_read_error_resume']=True
        state = dict(saved["session"])
        for field in ("planned_race_ids", "race_list_scan_viewports", "count_resume_preview_title", "count_resume_restart_title"):
            if state.get(field) is not None:
                state[field] = tuple(state[field])
        session = RaceRouteSession(**state)
        exchange_owner = _restore_larc_exchange_pending(session, saved.get("steps", []), run_root)
        if exchange_owner is not None:
            report["exchange_confirmation_resume"] = exchange_owner
        clock_usage_baseline = saved.get("clock_usage_baseline", _clock_usage([]))
        if entry_recovery:
            verified = _revalidate_exchange_entry(run_root, saved, session)
            if verified is None:
                return finish("race_entry_postcondition_revalidation_failed")
            report["entry_postcondition_revalidation"] = verified
        elif saved.get("entry_postcondition_revalidation"):
            report["entry_postcondition_revalidation"] = saved["entry_postcondition_revalidation"]
        report["step_reports"] = saved["steps"]
    elif live_source is not None:
        owner = _load_race_owner_session(owner_args)
        owner_args.verified_race_dialogue_handoff_owner = owner.to_dict()
        try:
            session, clock_usage_baseline = _live_clock_session(live_source)
        except ValueError as exc:
            return finish(str(exc))
    else:
        if resume:
            return finish("race_checkpoint_missing")
        preflight_args = advance_args([
            "--mode", "live", "--scenario", "larc", "--max-steps", "1",
            "--screenshot-dir", str(output_dir / "preflight"), "--adb-port", str(adb_port),
            "--run-session-id", run_id, "--transition-session-json", str(route_path),
            "--skip-session-json", str(skip_path),
        ])
        if safety_analyzer is not None:
            preflight_args._safety_analyzer = safety_analyzer
        preflight, code = advance(preflight_args)
        _atomic_write_json(output_dir / "preflight_report.json", preflight)
        expected_stops = ({"race_list"} if first_win_rescue else
            {"ready_main_menu"} if custom_race is not None else
            {"ready_main_menu", "fan_goal_warning_handoff"} if fan_goal is not None else {"race_day_main_menu"})
        if code or preflight.get("is_valid") is not True or preflight.get("stop_reason") not in expected_stops:
            return finish("race_day_preflight_unverified")
        if cold_result_handoff:
            if report.get("consecutive_exchange") and not _fresh_exchange_preflight(preflight):
                return finish('consecutive_exchange_preflight_unverified')
            if report.get("prior_live_result_handoff"):
                preflight_ok = (
                    _fresh_race_day_preflight(preflight)
                    if (fan_goal is None and custom_race is None and not first_win_rescue)
                    else _fresh_entry_preflight_safe(preflight, expected_stops)
                )
                if not preflight_ok:
                    return finish('prior_live_result_preflight_unverified')
        if superseded is not None:
            if report.get("consecutive_exchange") or report.get("adaptation_reentry"):
                last = preflight.get("step_reports", [{}])[-1].get("result", {})
                text = str(last.get("frame_safety", {}).get("visible_text", ""))
                expected_race = report.get("adaptation_reentry", {}).get("race_title", "凯旋门代表交流赛")
                if expected_race not in "".join(text.split()):
                    return finish("consecutive_exchange_wrong_race" if report.get("consecutive_exchange")
                                  else "race_adaptation_reentry_wrong_race")
            archive = output_dir / "superseded_checkpoint.json"
            _atomic_write_json(archive, superseded)
            history = (
                report.get("consecutive_exchange")
                or report.get("adaptation_reentry")
                or report.get("prior_live_result_handoff")
            )
            history.update(superseded_checkpoint_path=str(archive),
                fresh_preflight_report_path=str(output_dir / "preflight_report.json"))
        if first_win_rescue:
            from core.auto_trainer.larc_first_win import first_win_required
            last = (preflight.get("step_reports") or [{}])[-1].get("result", {})
            if not first_win_required(last.get("frame_safety", {}).get("visible_text")):
                return finish("first_win_current_requirement_missing")
        elif fan_goal is None and custom_race is None:
            report["owner_preflight"] = _settle_race_day_owner_preflight(owner_args, preflight)
        owner = _load_race_owner_session(owner_args)
        session = RaceRouteSession()
    steps = report["step_reports"]
    receipts = []
    # Preserve prior stops in the checkpoint, but this segment's report only
    # emits newly exhausted waits so summary aggregation cannot count a resume
    # as another occurrence of the old failure.
    technical_wait_history = list((saved or {}).get("technical_wait_stops", []))

    def save(status, reason):
        value = {"schema_version": "larc_race_checkpoint.v1", "run_session_id": run_id,
                 "device": progress["device"], "turn_index": turn,
                 "status": status, "stop_reason": reason, "owner_before": owner.to_dict(),
                 "session": asdict(session), "steps": steps, "current_input_receipts": receipts}
        value["career_clock_budget"]=clock_budget.snapshot()
        value.update(clock_usage_baseline=clock_usage_baseline,
                     **_cumulative_clock_usage(steps, clock_usage_baseline))
        if 'cut_debut_race' in report:value['cut_debut_race']=report['cut_debut_race']
        if 'ordinary_clock_retry' in report:value['ordinary_clock_retry']=report['ordinary_clock_retry']
        if first_win_rescue:value['first_win_rescue']=True
        if report.get("entry_postcondition_revalidation"):
            value["entry_postcondition_revalidation"] = report["entry_postcondition_revalidation"]
        if report.get("adaptation_reentry"):
            value["adaptation_reentry"] = report["adaptation_reentry"]
        if report.get("consecutive_exchange"):
            value["consecutive_exchange"] = report["consecutive_exchange"]
        if report.get("prior_live_result_handoff"):
            value["prior_live_result_handoff"] = report["prior_live_result_handoff"]
        if fan_goal is not None:
            value["fan_goal"] = fan_goal
        if custom_race is not None:
            value["custom_race"] = dict(custom_race)
        if report.get("fan_race_receipt"):
            value["fan_race_receipt"] = report["fan_race_receipt"]
        if incomplete_first_arc_turn is not None:
            value["incomplete_first_arc_turn"] = incomplete_first_arc_turn
        if technical_wait_history or report.get("technical_wait_stops"):
            value["technical_wait_stops"] = technical_wait_history + report.get("technical_wait_stops", [])
        _atomic_write_json(checkpoint, value)
        _atomic_write_json(output_dir / "checkpoint.json", value)

    def record_input(receipt):
        clock_budget.observe(receipt, executor.planner.last_decision)
        if receipts and receipts[-1]["outcome"] == "pending":
            receipts[-1] = dict(receipt)
        else:
            receipts.append(dict(receipt))
        if fan_goal is not None and receipt["outcome"] == "confirmed":
            consumed = _fan_receipt_from_input(run_root, progress["device"], fan_goal,
                len(steps)+1, executor.planner.last_decision, receipt)
            if consumed is not None:
                if report.get("fan_race_receipt") is not None:
                    raise ValueError("fan_race_duplicate_confirmation")
                report["fan_race_receipt"] = consumed
        save("in_flight", "input_outcome_pending")

    from core.native_capture_adb import native_instance_id
    from core.cut_portable_runtime import portable_root
    native_capture = native_instance_id(adb_port) is not None
    managed_capture = native_capture or portable_root() is not None
    if managed_capture:
        from core.native_capture_adb import adb_for_capture
        from scripts.larc_race_transport import RaceNativeTransport
        transport = RaceNativeTransport(adb_for_capture(adb_port))
        resources.callback(transport.close)
    else:
        transport = ADB(port=adb_port)
    receipt_adb = _ReceiptADB(transport, record_input)
    executor = RaceRouteExecutor(adb_factory=lambda: receipt_adb,
        planner=_FanReceiptPlanner(fan_goal) if fan_goal is not None else None,
        safety_analyzer=safety_analyzer)
    skill_context_path = run_root / "skill_policy/race_build_context.json"
    from core.auto_trainer.larc_race_tactics import STYLE_LABELS
    from core.auto_trainer.larc_custom_race_catalog import resolve_configured_running_style
    cut_debut=bool((saved or {}).get('cut_debut_race') or (live_source or {}).get('cut_debut_race') is True or _debut_retry_scope((preflight.get('step_reports',[]) if 'preflight' in locals() and isinstance(preflight, dict) else [])+steps))
    visible_chunks = []
    for row in ((preflight.get('step_reports', []) if 'preflight' in locals() and isinstance(preflight, dict) else []) + steps):
        vt = str(row.get('result', {}).get('frame_safety', {}).get('visible_text', '') or '')
        if vt:
            visible_chunks.append(vt)
    configured_style, style_source = resolve_configured_running_style(
        goal,
        custom_race=custom_race,
        visible_text="\n".join(visible_chunks),
        cut_debut=cut_debut,
    )
    desired_style = None
    if configured_style == "keep":
        desired_style = None
    elif isinstance(configured_style, str) and configured_style in STYLE_LABELS:
        desired_style = configured_style
    elif skill_context_path.is_file():
        desired_style = json.loads(skill_context_path.read_text(encoding="utf-8")).get("race_profile", {}).get("style")
        if isinstance(desired_style, str):
            desired_style = {'betweener': 'middle', 'chaser': 'back'}.get(desired_style, desired_style)
        if desired_style in (None, "any", "keep"):
            desired_style = None
        elif not isinstance(desired_style, str) or desired_style not in STYLE_LABELS:
            return finish("race_tactic_style_invalid")
    report["desired_run_style"] = desired_style
    report["desired_run_style_source"] = style_source
    tactic_state = _load_race_tactic_state(run_root, run_id)
    solo_leader_enabled = bool(
        not (isinstance(goal.larc_policy, dict) and goal.larc_policy.get("solo_leader_to_front") is False)
        and not cut_debut
        and configured_style not in {"front", "middle", "back"}
        and not str(style_source or "").startswith(("race_running_styles:", "custom_race_inline:"))
    )
    report["solo_leader_to_front"] = solo_leader_enabled
    if debut_clock_retry is not None:
        report.update(cut_debut_race=cut_debut,debut_clock_retry=debut_clock_retry)
    else:
        report['ordinary_clock_retry'] = True
    policy = RaceRoutePolicy(scenario="larc", desired_run_style=desired_style,
                             solo_leader_to_front=solo_leader_enabled,
                             solo_leader_restore_style=tactic_state.get("solo_leader_restore_style"),
                             solo_leader_front_aptitude_rejected=bool(tactic_state.get("front_aptitude_rejected")),
                             is_debut_race=cut_debut,
                             allow_enter_race=not bool(report.get("entry_postcondition_revalidation")), allow_locked_race_entry=(fan_goal is None and custom_race is None),
                             allow_enter_race_from_main_menu=(fan_goal is not None or custom_race is not None),
                             allow_fan_goal_recommended_race=fan_goal is not None,
                             allow_larc_custom_scheduled_race=custom_race is not None,
                             custom_target_race=dict(custom_race) if custom_race is not None else None,
                             allow_leave_race_list=custom_race is not None or (
                                 live_source is not None
                                 and not live_source.get("entered")
                                 and live_source.get("state") == "race_list"
                             ),
                             allow_leave_race_detail=custom_race is not None or (
                                 live_source is not None
                                 and not live_source.get("entered")
                                 and live_source.get("state") in {"modal", "race_detail"}
                             ),
                             return_fan_shortfall_to_home=(fan_goal is None and custom_race is None),
                             allow_first_win_rescue=first_win_rescue,
                             allow_confirm_race=True, allow_quick_result=True,
                             allow_skip_race_scene=True, allow_result_continue=True,
                             allow_clock_retry=True if debut_clock_retry is None else debut_clock_retry and cut_debut,
                             allow_debut_retry_entry=debut_clock_retry is True and cut_debut,
                             allow_result_retry_entry=debut_clock_retry is None)
    if live_source is not None and live_source.get('entered') is True:
        # The player has already entered this race. Only finish its current UI;
        # never select another race, confirm another entry, or change tactics.
        policy = replace(policy, post_entry_only=True, allow_enter_race=False,
            allow_locked_race_entry=True, allow_confirm_race=False,
            desired_run_style=None, solo_leader_to_front=False)
    if report.get("goal_catalog_read_error_resume"):
        # Observe the real current page and hand off the existing completion;
        # this read-only failure cannot grant entry or clock-retry authority.
        policy = replace(policy, post_entry_only=True, allow_enter_race=False,
            allow_enter_race_from_main_menu=False, allow_confirm_race=False,
            allow_clock_retry=False, allow_debut_retry_entry=False,
            desired_run_style=None, solo_leader_to_front=False)
    if goal.race_retry is not None:
        retry_enabled=goal.race_retry["enabled"]
        policy=replace(policy,allow_clock_retry=retry_enabled,
            allow_debut_retry_entry=retry_enabled and cut_debut,allow_result_retry_entry=retry_enabled)
    reason, status = "max_steps_reached", "stopped"
    wait_budget = TransitionBudget(clock=lambda: monotonic())
    result_prompt_wait_started = None
    # Segment-local observation limit; loading never renews it and saved steps
    # cannot replay an old retry proof. Each follow-up still runs the executor.
    clock_retry_reobserve_count = 0
    clock_retry_reobserve_pending = False
    last_tap, repeated = None, 0
    for row in steps:
        tap = row["result"].get("decision", {}).get("tap")
        signature = (row["result"].get("reason"), tap) if tap else None
        repeated = repeated + 1 if signature and signature == last_tap else int(bool(signature))
        last_tap = signature
    initial_retries = session.clock_retries_used
    iteration = 0
    while iteration < max_steps * (1 + session.clock_retries_used - initial_retries):
        # A confirmed clock starts another race attempt, not another career turn.
        # Give that attempt the same bounded route budget; mere waits add none.
        iteration += 1
        if _load_race_owner_session(owner_args).to_dict() != owner.to_dict():
            reason, status = "race_owner_session_changed", "blocked"
            break
        receipts = []
        save("in_flight", "step_outcome_pending")
        optional_upgrade = _prepared_race_optional_upgrade(run_root, progress["device"], steps,
            readiness_report=(optional_upgrade_readiness_report or
                report.get("adaptation_reentry", {}).get("readiness_report_path"))) if fan_goal is None else None
        approved_first_arc = (incomplete_first_arc_turn is not None and any(
            _fixed_race_entry_confirmed([row]) and "凯旋门奖" in str(
                row["result"].get("frame_safety", {}).get("visible_text", "")) for row in steps))
        # Manual takeover may finish the player's current entered race. An
        # ordinary confirmed entry alone must NOT grant an adaptation bypass
        # to every later page/race; keep its readiness/first-Arc rules intact.
        continuing_entered = (fan_goal is None and live_source is not None
            and live_source.get('entered') is True and not any(
                row['result'].get('performed') is True
                and row['result'].get('decision', {}).get('reason') == 'confirm_larc_unupgraded_entry'
                for row in steps))
        step_policy = replace(policy, clock_retry_remaining=clock_budget.remaining,
            clock_retry_unresolved=clock_budget.retry_pending, allow_larc_unupgraded_entry=
            optional_upgrade is not None or approved_first_arc or continuing_entered
            or (fan_goal is None and _confirmed_race_warning_pending(steps, run_root)
                and (strategy_revision(goal) not in ("1.9.8", "1.9.9", "1.9.10", "1.9.11", "1.9.12", "2.0", "2.1", "2.2") or not _is_niel_warning_context(steps))))
        if report.get("fan_race_receipt"):
            step_policy = replace(step_policy, allow_enter_race=False, allow_enter_race_from_main_menu=False,
                                  allow_confirm_race=False, allow_fan_goal_recommended_race=False,
                                  allow_locked_race_entry=True, post_entry_only=True)
        elif report.get("custom_race_returned_to_home"):
            step_policy = replace(step_policy, allow_enter_race=False, allow_enter_race_from_main_menu=False,
                                  allow_larc_custom_scheduled_race=False, allow_confirm_race=False)
        elif fan_goal is None and _fixed_race_entry_confirmed(steps):
            step_policy = replace(step_policy, allow_enter_race=False,
                                  allow_enter_race_from_main_menu=False,
                                  allow_larc_custom_scheduled_race=False, post_entry_only=True)
        step_started=monotonic()
        fast_frames=native_capture or managed_capture and getattr(transport,'supports_in_memory_capture',False) is True
        try:
            result = executor.run_once(mode="live", execute=True, confirm_token=RACE_ROUTE_CONFIRM_TOKEN,
                policy=step_policy, session=session, screenshot_path=output_dir / f"race_{len(steps)+1:03d}.png",
                allow_gentle_transition_wait=True,
                # Only the connected managed fast path has the outer bounded
                # wait_transition poll below; let its first observation start now.
                wait_seconds=0 if fast_frames and managed_capture and transport.connected else .15 if fast_frames else 1,
                settle_seconds=0 if managed_capture else 1.2)
        except Exception as exc:
            result = {"performed": False, "reason": "race_executor_error", "error": f"{type(exc).__name__}:{exc}"}
            status = "blocked"
        if clock_retry_reobserve_pending:
            clock_retry_reobserve_count += 1
            result['clock_retry_reobserve'] = clock_retry_reobserve_count
            report['clock_retry_reobserve_count'] = clock_retry_reobserve_count
            clock_retry_reobserve_pending = False
        clock_budget.observe_page(result.get('decision',{}),result.get('frame_safety',{}))
        timing=result.setdefault('timing',{})
        timing['total_seconds']=round(monotonic()-step_started,4)
        channel=getattr(transport,'capture_backend_name',None)
        timing['capture_backend']=channel if isinstance(channel,str) else None
        timing['connection_scope']='race' if managed_capture else 'step'
        if optional_upgrade:
            result["prepared_race_optional_upgrade"] = optional_upgrade
        if managed_capture and transport.last_wait is not None:
            result["native_settle"] = transport.last_wait
            transport.last_wait = None
        if receipts:
            result["input_receipts"] = list(receipts)
            result["tap_performed"] = any(r["outcome"] == "confirmed" for r in receipts)
            result["input_may_have_occurred"] = any(r["outcome"] in {"confirmed", "unknown", "pending"} for r in receipts)
        previous_result = None
        for previous_step in reversed(steps):
            previous_result = previous_step['result']
            # Connecting/loading observations do not end the result-page
            # transition. Keep its last input across those read-only frames.
            if ((previous_result.get('decision',{}).get('action')=='wait'
                    or previous_result.get('result_prompt_reobserve') is True
                    or previous_result.get('clock_retry_reobserve_next') is not None)
                    and not previous_result.get('decision',{}).get('tap')
                    and not any(previous_result.get(key) for key in
                        ('tap_performed','input_may_have_occurred','input_receipts'))):
                previous_result = None
                continue
            break
        steps.append({"step": len(steps)+1, "result": result})
        if result.get("performed") is True and result.get("reason") == "leave_fan_goal_shortfall_race_list":
            report["fan_shortfall_returned_to_home"] = {"entry_submitted": False, "step": len(steps)}
        if result.get("performed") is True and result.get("reason") == "leave_unavailable_custom_scheduled_race_list":
            report["custom_race_returned_to_home"] = {"entry_submitted": False, "step": len(steps)}
        if (
            live_source is not None
            and result.get("performed") is True
            and result.get("reason") in {"leave_selected_non_g1_race_list", "leave_unverified_g1_race_list"}
        ):
            report["live_race_list_returned_to_home"] = {"entry_submitted": False, "step": len(steps)}
        clock_retry_wait = (session.clock_retries_used < 5 and _clock_retry_unavailable_recheck(result, step_policy,
            unresolved=clock_budget.unresolved) and clock_retry_reobserve_count < 2)
        if clock_retry_wait:
            result['clock_retry_reobserve_next'] = clock_retry_reobserve_count + 1
        elif result.get('reason') == 'clock_retry_unavailable' and clock_retry_reobserve_count >= 2:
            result['clock_retry_reobserve_exhausted'] = True
        result_prompt_wait = _post_result_unreadable_recheck(result, previous_result)
        if result_prompt_wait:
            now = monotonic()
            if result_prompt_wait_started is None:
                result_prompt_wait_started = now
            result['result_prompt_wait_seconds'] = round(now-result_prompt_wait_started,3)
            result_prompt_wait = now-result_prompt_wait_started < 5.
            if result_prompt_wait:
                result['result_prompt_reobserve'] = True
        transition = (clock_retry_wait
            or _race_text_transition(result, result_transition_pending=session.result_transition_pending)
            or result_prompt_wait)
        if transition and (_fixed_race_entry_confirmed(steps) or step_policy.post_entry_only
                           or result.get("decision", {}).get("action") == "wait"):
            result["transition_reobserve"] = True
            wait_budget.observe(image=getattr(transport, "last_frame", None),
                key=result.get("decision", {}).get("route_state"),
                progress_token=result.get("acknowledged_inputs"))
            if wait_budget.expired:
                reason = ('clock_retry_unavailable' if clock_retry_wait
                          else "clock_retry_entry_unverified" if result.get('reason') == 'clock_retry_entry_unverified'
                          else "race_transition_no_progress_timeout")
                status = "stopped"
                report.setdefault("technical_wait_stops", []).append(technical_wait_stop(
                    wait_budget, reason=reason, owner="race",
                    surface=result.get("decision", {}).get("route_state") or result.get("reason"),
                    run_session_id=run_id, turn_index=turn))
                save(status, reason)
                break
            save("stopped", result.get("reason", "loading"))
            if result_prompt_wait or result.get("reason") in {
                "race_tactic_preview_loading",
                "race_tactic_counts_loading",
                "race_tactic_control_loading",
                "race_tactic_selection_loading",
                "race_tactic_aptitude_loading",
                "race_tactic_modal_closing",
                "race_result_controls_loading",
                "larc_exchange_rank_table_loading",
                "larc_race_day_controls_loading",
            }:
                # Outer controls already match on sub-element fade-in; pace
                # re-observation instead of spinning full OCR at 16ms intervals.
                executor.sleep_fn(.25)
            if managed_capture and transport.connected and (native_capture or getattr(transport,'supports_in_memory_capture',False) is True):
                result["transition_poll"] = transport.wait_transition()
                wait_budget.observe(image=transport.last_frame,
                    key=result["transition_poll"].get("controls"))
                if result["transition_poll"].get("error"):
                    executor.sleep_fn(.15)
            else:
                executor.sleep_fn(.15)
            if clock_retry_wait:
                clock_retry_reobserve_pending = True
            iteration -= 1  # Observations do not use up the action-step budget.
            continue  # Fresh screenshot; no repeat of the previous input.
        if not transition:
            if result.get("tap_performed") or (result.get("performed") is True
                    and result.get("decision", {}).get("action") == "tap"):
                wait_budget.reset()
                result_prompt_wait_started = None
        if result.get("performed") is not True:
            reason = result.get("reason", "race_step_unverified")
            if reason == "no_progress_timeout":
                reason = "race_transition_no_progress_timeout"
                gentle_budget = getattr(executor.planner, "_default_gentle_budget", None)
                stop_budget = (gentle_budget if gentle_budget is not None and getattr(gentle_budget, "expired", False)
                               else wait_budget)
                decision = result.get("decision") or executor.planner.last_decision or {}
                report.setdefault("technical_wait_stops", []).append(technical_wait_stop(
                    stop_budget,
                    reason=reason,
                    owner="race",
                    surface=decision.get("route_state") or result.get("screen"),
                    run_session_id=run_id,
                    turn_index=turn,
                ))
            if result.get("tap_outcome") in {"failed", "unknown"} or result.get("tap_performed") is True:
                status = "blocked"
            save(status, reason)
            break
        tap = result.get("decision", {}).get("tap")
        signature = (result.get("reason"), tap) if tap else None
        repeated = repeated + 1 if signature and signature == last_tap else int(bool(signature))
        last_tap = signature
        if repeated >= MAX_CONSECUTIVE_IDENTICAL_TAPS:
            reason, status = "repeated_tap_limit_reached", "blocked"
            save(status, reason)
            break
        if result.get("performed") is True:
            dec = result.get("decision") or {}
            tactic_ev = (dec.get("evidence") or {}).get("race_tactic") if isinstance(dec.get("evidence"), dict) else None
            if dec.get("reason") == "race_tactic_confirm" and isinstance(tactic_ev, dict):
                if tactic_ev.get("solo_leader_fallback") is True and tactic_ev.get("target_style") == "front":
                    tactic_state["solo_leader_restore_style"] = "leader"
                    _save_race_tactic_state(run_root, tactic_state)
                    policy = replace(policy, solo_leader_restore_style="leader")
                elif tactic_ev.get("target_style") == "leader" and tactic_state.get("solo_leader_restore_style") is not None:
                    tactic_state["solo_leader_restore_style"] = None
                    _save_race_tactic_state(run_root, tactic_state)
                    policy = replace(policy, solo_leader_restore_style=None)
            elif (dec.get("reason") == "race_tactic_cancel" and isinstance(tactic_ev, dict)
                    and tactic_ev.get("reason") == "solo_leader_front_aptitude_blocked"):
                tactic_state["front_aptitude_rejected"] = True
                tactic_state["observed_front_aptitude"] = tactic_ev.get("front_aptitude")
                _save_race_tactic_state(run_root, tactic_state)
                policy = replace(policy, solo_leader_front_aptitude_rejected=True)
        save("stopped", reason)
    report["career_clock_budget"]=clock_budget.snapshot()
    report.update(is_valid=status != "blocked", stop_reason=reason,
                  clock_retries_used=session.clock_retries_used,
                  **_cumulative_clock_usage(steps, clock_usage_baseline),
                  performed_steps=sum(row["result"].get("performed") is True for row in steps))
    if fan_goal is not None:
        report["fan_shortfall_progress"] = _fan_shortfall_entry_progress(report, allowed_grades=("G1", "G2", "G3"))
    if custom_race is not None:
        report["custom_race_outcome"] = (
            "completed" if _fixed_race_entry_confirmed(steps)
            else "skipped_unavailable" if report.get("custom_race_returned_to_home") and reason == "ready_main_menu"
            else "stopped"
        )
    if reason in HANDOFF_STOPS:
        last = steps[-1]["result"] if steps else {}
        if last.get("reason") == reason and is_route_handoff_result(last):
            report.update(status="handoff", handoff_target=last["decision"]["target"])
        try:
            from core.auto_trainer.larc_classic_arc_result import (
                extract_classic_arc_result,
                save_classic_arc_result,
            )

            arc_result = extract_classic_arc_result(report, read_roi=True)
            if arc_result is not None:
                report["classic_arc_result"] = arc_result
                save_classic_arc_result(
                    run_root,
                    run_id,
                    arc_result,
                    turn_index=turn,
                    report_path=str(output_dir / "report.json"),
                )
        except Exception:
            pass
        handoff_path = output_dir / "owner_handoff.json"
        report["owner_handoff_path"] = str(handoff_path)
        _atomic_write_json(output_dir / "report.json", report)
        try:
            handoff = (_record_fan_list_home_observation(owner_args, report, owner, run_root)
                if fan_list_resume and report.get("performed_steps") == 0 and reason == "ready_main_menu"
                else _record_race_owner_handoff(owner_args, report, owner))
            _atomic_write_json(handoff_path, handoff)
        except (ValueError, OSError) as exc:
            report.update(is_valid=False, status="stopped")
            report.pop("handoff_target", None)
            reason, status = f"race_owner_handoff:{exc}", "blocked"
        else:
            if reason in {"goal_achievement_loading", "goal_achievement_result"}:
                session.result_transition_pending = False
            save("handed_off", reason)
            return finish(reason, 0)
    save(status, reason)
    return finish(reason)
