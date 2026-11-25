"""Bounded L'Arc integration with opt-in fixed races and aptitude phases."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid
from types import SimpleNamespace
from time import monotonic, time_ns

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.auto_trainer.config import load_goal_config
from core.auto_trainer.larc_ai_runtime import LarcAIPause
from core.auto_trainer.runtime_control import (
    RunStopped, ReplanTurn, ResumeFromCurrentPage, bind, current, controlled_sleep as sleep,
)
from core.auto_trainer.career_goal_ledger import CareerGoalLedger, CareerGoalLedgerStore
from core.auto_trainer.main_menu_state_provider import provide_main_menu_state
from core.auto_trainer.larc_adaptation import LarcAdaptationSession, PHASE_SLOT_COUNTS
from core.auto_trainer.larc_race_day_context import read_larc_race_day_context
from core.auto_trainer.pipeline_timing import timed_call
from core.auto_trainer.larc_supporter_goal import read_larc_supporter_goal
from core.auto_trainer.larc_policy import policy_version, policy_branch
from core.auto_trainer.larc_fan_goal import read_larc_fan_goal, fan_goal_due, plan_ss_before_fan_race
from core.auto_trainer.route_transition_session import RouteTransitionSession, POST_ACTION_ROUTE_SUCCESSORS
from core.auto_trainer.skip_session import RunSkipSession
from core.auto_trainer.route_handoff import is_route_handoff
from core.auto_trainer.transition_wait import (
    TransitionBudget, transient_observation, connecting_overlay_visible, pipeline_page_handoff,
    technical_wait_stop,
)
from core.auto_trainer.run_support_profile import load_profile
from core.training_parser import TrainingParser
from scripts.ug_parent_one_turn_pipeline import parse_args as pipeline_args, run_pipeline, _atomic_write_json
from scripts.ug_parent_safe_advance import parse_args as advance_args, run as advance
from scripts.ug_parent_event_choice_route import parse_args as event_args, run as event
from scripts.ug_parent_supervised_run import _record_training_route_receipt, _record_rest_route_receipt, _canonical_sha256
from scripts.larc_autonomy_race import (
    _fixed_race_entry_confirmed, _confirmed_result_repeat_resume, _catalog_read_error_resume,
    _fan_shortfall_list_resume, run_larc_race_route,
)
from scripts.larc_autonomy_adaptation import run_larc_adaptation, select_larc_adaptation_phase
from scripts.larc_autonomy_skills import run_larc_terminal_skills, run_larc_terminal_settlement
from scripts.larc_autonomy_deck import prepare_deck_inspection
from scripts.larc_autonomy_support_skills import prepare_support_skills, resume_support_skill_completion
from scripts.larc_autonomy_arc_skills import prepare_arc_skills, resume_arc_skill_learning
from scripts.larc_autonomy_outing import (observe_mei_unlock, prepare_mei_outing,
    MeiOutingEventHandoff, MeiOutingMenuHandoff, MeiOutingInspectionError,
    outing_replan_handoff, accept_outing_replan)
from scripts.larc_autonomy_growth import (growth_check_key, inspect_growth_adaptation,
    cached_growth_recommendation, return_from_growth_inspection, restore_training_menu)
from scripts.larc_autonomy_events import observe_event_report

CONFIRM_TOKEN = "LARC_AUTONOMY_TEST"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--adb-port", required=True, type=int)
    parser.add_argument("--max-actions", required=True, type=int, choices=range(1, 73))
    parser.add_argument("--training-records", choices=("keep", "discard"), default=None,
                        help="Optional per-turn review archive, or discard completed working training images when stopped")
    parser.add_argument("--stop-after-year", type=int, choices=(1, 2),
                        help="Pause on the next year's observed home before taking another action")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-fixed-races", action="store_true")
    parser.add_argument("--allow-adaptations", action="store_true")
    parser.add_argument("--ordinary-career-opening", action="store_true",
        help="Normal career page ownership, configured opening and existing-career recovery (not cut farming)")
    parser.add_argument("--same-account-reconnect", action="store_true",
        help="Allow one cached same-account reconnect; never switch accounts or start a replacement career")
    parser.add_argument("--allow-incomplete-first-arc-turn", type=int)
    parser.add_argument("--d1-skill-root", type=Path)
    parser.add_argument("--startup-skill-catalog-dir",type=Path)
    parser.add_argument("--d1-startup-only",action="store_true",help="Use configured D1 driver only for mid-career support activation, not terminal learning")
    parser.add_argument("--external-skill-db-path", type=Path)
    parser.add_argument("--resume-recorded-ss", action="store_true")
    parser.add_argument("--confirm-token")
    parser.add_argument("--expected-main-menu-state-json", type=Path)
    return parser.parse_args(argv)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _context(args):
    if bool(args.d1_skill_root) != bool(args.external_skill_db_path):
        raise ValueError("d1_skill_root_and_external_db_required_together")
    root = args.run_root.resolve(strict=True)
    goal_config = load_goal_config(root / "goal.json")
    from core.auto_trainer.larc_skill_driver import load_driver
    driver=load_driver(root)
    if getattr(args,"startup_skill_catalog_dir",None) is None and driver and driver.get("startup_skill_catalog_dir"):
        args.startup_skill_catalog_dir=Path(driver["startup_skill_catalog_dir"])
    if args.d1_skill_root is None and args.external_skill_db_path is None:
        if driver:
            args.d1_skill_root=Path(driver["d1_root"])
            args.external_skill_db_path=Path(driver["external_skill_db_path"])
            args.d1_startup_only = getattr(args, "d1_startup_only", False) or not goal_config.skill_timing.arc_policy.get("enabled")
    progress = _read(root / "progress.json")
    run_id = progress.get("run_session_id")
    if (root / "session/joint_trial_owner.json").exists():
        raise ValueError("joint_trial_owns_run_no_automatic_actions")
    if run_id != root.name or progress.get("device") != f"127.0.0.1:{args.adb_port}":
        raise ValueError("run_or_device_mismatch")
    if (type(progress.get("actions_completed")) is not int or progress["actions_completed"] < 0
            or type(progress.get("next_turn")) is not int
            or progress["next_turn"] != progress["actions_completed"] + 1):
        raise ValueError("progress_invalid")
    if goal_config.scenario != "larc":
        raise ValueError("goal_scenario_not_larc")
    route, skip, roster = (root / "session" / name for name in (
        "transition_session.json", "skip_session.json", "larc_member_roster.json"))
    if not all(path.is_file() for path in (route, skip, roster)):
        raise ValueError("required_session_missing")
    RouteTransitionSession.load(route, expected_run_session_id=run_id)
    RunSkipSession.load(skip, expected_run_session_id=run_id)
    ledger = root / "session/career_goal_ledger.json"
    if ledger.exists():
        CareerGoalLedgerStore().load(ledger, expected_run_session_id=run_id)
    roster_data = _read(roster)
    if (roster_data.get("run_session_id") != run_id
            or roster_data.get("schema_version") != "larc_member_roster.v1"):
        raise ValueError("roster_run_or_schema_mismatch")
    pending = root / "session/larc_autonomous_pending.json"
    fan_pending = _read(pending) if pending.exists() else None
    if fan_pending is not None and fan_pending.get("kind") != "fan_race":
        fan_pending = None
    if pending.exists() and not args.resume_recorded_ss and not (
            fan_pending is not None and args.execute):
        raise ValueError(f"pending_action_requires_review:{pending}")
    if args.execute and args.confirm_token != CONFIRM_TOKEN:
        raise ValueError("confirm_token_required")
    if args.allow_incomplete_first_arc_turn is not None and (
            args.allow_incomplete_first_arc_turn != progress["next_turn"]
            or not args.allow_fixed_races or not args.allow_adaptations):
        raise ValueError("first_arc_exception_turn_mismatch_or_permissions_missing")
    if args.resume_recorded_ss and (not args.execute or not pending.is_file()):
        raise ValueError("resume_recorded_ss_requires_execute_and_pending")
    expected = _read(args.expected_main_menu_state_json) if args.expected_main_menu_state_json else None
    if expected is not None and (expected.get("valid_for_training") is not True
            or expected.get("state_context", {}).get("turn") != progress["next_turn"]):
        raise ValueError("expected_main_menu_state_invalid")
    ctx = SimpleNamespace(root=root, run_id=run_id, goal=goal_config, progress=progress, route=route, skip=skip,
                          roster=roster, ledger=ledger, pending=pending, expected=expected,
                          port=args.adb_port, allow_fixed_races=args.allow_fixed_races,
                          allow_adaptations=args.allow_adaptations, ready_adaptations={},
                          incomplete_first_arc_turn=args.allow_incomplete_first_arc_turn,
                          incomplete_first_arc_accepted=False,
                          growth_checks=set(), growth_recollections={},
                          d1_skill_root=args.d1_skill_root, external_skill_db_path=args.external_skill_db_path,
                          startup_skill_catalog_dir=getattr(args,"startup_skill_catalog_dir",None),
                          terminal_skill_enabled=args.d1_skill_root is not None and not getattr(args,"d1_startup_only",False),
                          ordinary_career_opening=getattr(args,"ordinary_career_opening",False) is True,
                          same_account_reconnect=getattr(args,"same_account_reconnect",False) is True,
                          reconnect_attempted=False)
    ctx.recovery = _recorded_ss_recovery(ctx) if args.resume_recorded_ss else None
    ctx.fan_pending = fan_pending
    ctx.support_profile = load_profile(root, run_id)
    ctx.cut_farming_hook = getattr(args, '_cut_farming_hook', None)
    if ctx.cut_farming_hook is not None:
        ctx.terminal_skill_enabled = False
        # Ordinary cut farming ends before this preparation is useful.
        # Explicit continuation restores the already-authorized normal
        # adaptation route; it does not grant a new purchase permission.
        config = getattr(getattr(ctx.cut_farming_hook, 'session', None), 'config', None)
        continuation = getattr(config, 'continuation', None)
        ctx.allow_adaptations = bool(ctx.allow_adaptations and isinstance(continuation, dict)
                                     and continuation.get('enabled') is True)
    return ctx


def _cut_screening(ctx):
    hook = getattr(ctx, 'cut_farming_hook', None)
    return hook is not None and not getattr(hook, 'continuation_flow', False)


def _continuation_enabled(ctx):
    hook = getattr(ctx, 'cut_farming_hook', None)
    return hook is not None and bool(getattr(hook, 'continuation_flow', False))


def _ss_sieve_config(ctx, checkpoint):
    runtime = getattr(ctx, 'continuation_runtime', None)
    due = getattr(runtime, 'sieve_checkpoint_due', None)
    return due(checkpoint) if callable(due) else None


def _attach_continuation_runtime(ctx, args):
    session = ctx.root / 'session'
    if not ((session / 'larc_continuation_options.json').is_file()
            or (session / 'career_workflow_context.json').is_file()):
        return
    from scripts.larc_continuation_runtime import load_runtime, acknowledge_pending
    if getattr(args, '_continuation_user_continue', False):
        acknowledge_pending(ctx.root)
    from scripts.larc_continuation_collect import send_notice
    ctx.continuation_runtime = load_runtime(ctx, notify=lambda node, entry: send_notice(ctx, node, entry))


def _ss_sieve_dispatch(ctx, evidence, summary):
    checkpoint = evidence['checkpoint']
    result = ctx.continuation_runtime.before_ss_sieve(checkpoint, evidence)
    summary.setdefault('ss_sieve_checks', []).append({'evidence': evidence, **result})
    if result.get('stop_reason') and isinstance(result.get('ss_sieve'), dict):
        summary['ss_sieve'] = result['ss_sieve']
    return result.get('stop_reason')


def _ss_sieve_before_debut(ctx, race_context, summary):
    if (_ss_sieve_config(ctx, 'pre_debut_end') is None or race_context.get('status') != 'verified'
            or race_context.get('target_id') != 'participate_in_debut_race'):
        return None
    period = race_context['period']
    source = race_context['source']
    if (period.get('status') != 'verified' or (period.get('value') or {}).get('pre_debut') is not True
            or not source.get('absolute_path') or not source.get('sha256')
            or period.get('source_frame_sha256') != source['sha256']):
        return None
    from core.auto_trainer.larc_ss_sieve import checkpoint_evidence
    evidence = checkpoint_evidence(ctx, 'pre_debut_end', page_kind='race_day',
        path=source['absolute_path'], sha256=source['sha256'], period=period)
    evidence['target_id'] = race_context['target_id']
    return _ss_sieve_dispatch(ctx, evidence, summary)


def _ss_sieve_year1_home(ctx, source, provider):
    period = provider.get('larc_period') or {}
    date = period.get('value') or {}
    binding = provider.get('source') or {}
    state = provider.get('state_context') or {}
    if (provider.get('valid_for_training') is not True or period.get('status') != 'verified'
            or (date.get('year_index'), date.get('month'), date.get('half')) != (2, 1, 'first')
            or state.get('turn') != ctx.progress['next_turn'] or not binding.get('path')
            or (state.get('scenario_state') or {}).get('scenario') != 'larc'
            or Path(binding['path']).resolve() != Path(source).resolve() or not binding.get('sha256')
            or period.get('source_frame_sha256') != binding['sha256']):
        return None
    from core.auto_trainer.larc_ss_sieve import checkpoint_evidence
    return checkpoint_evidence(ctx, 'year1_end', page_kind='training_home', path=source,
        sha256=binding['sha256'], period=period)


def _record_missed_sieve_checkpoints(ctx, source, provider, summary):
    # The caller already verified this ordinary home and its game date.
    period = provider['larc_period']
    date = period['value']
    year = date.get('year_index')
    if type(year) is not int or not date.get('month'):
        return
    missed = [('pre_debut_end', 'debut_boundary_not_observed')]
    if year > 2 or year == 2 and (date.get('month'), date.get('half')) != (1, 'first'):
        missed.append(('year1_end', 'year1_boundary_not_observed'))
    for checkpoint, reason in missed:
        if _ss_sieve_config(ctx, checkpoint) is not None:
            from core.auto_trainer.larc_ss_sieve import checkpoint_evidence
            _ss_sieve_dispatch(ctx, checkpoint_evidence(ctx, checkpoint, page_kind='training_home',
                path=source, sha256=provider['source']['sha256'], period=period, unavailable=reason), summary)


def _ss_sieve_from_capture(ctx, args, capture, config, evidence, summary):
    from core.auto_trainer.capture_adapter import load_capture_group_raws
    from core.auto_trainer.larc_ss_sieve import decide, read_ready_count
    try:
        raw = load_capture_group_raws(manifest_path=Path(capture['manifest_path']),
            execution_root=Path(capture['output_root']), actual_group_id=args.actual_group_id,
            formal_capture=True)['wisdom']
        count, binding = read_ready_count(raw, run_id=ctx.run_id, turn=evidence['turn'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        count, binding = None, None
    evidence = {**evidence, 'ready_count': count, 'ready_source': binding}
    if decide(checkpoint='year1_end', full5_ss=evidence['full5_ss'], ready_count=count,
              config=config)['decision'] == 'screen_out':
        return evidence  # The ordinary owner verifies a fresh home before requesting restart.
    _ss_sieve_dispatch(ctx, evidence, summary)
    return None


def _ss_sieve_return_home(ctx, turn_root, old_evidence, summary, save_summary):
    # This pipeline outcome is explicitly pre-action. Release only its intent.
    ctx.pending.unlink(missing_ok=True)
    source, reason = _route_to_menu(ctx, turn_root / 'ss_sieve_return_home', summary, save_summary)
    if source is None:
        return source, reason
    provider = provide_main_menu_state(source, turn_root / 'ss_sieve_home.json',
        turn_index=old_evidence['turn'], scenario='larc')
    evidence = _ss_sieve_year1_home(ctx, source, provider)
    if (evidence is None or evidence['turn'] != old_evidence['turn']
            or evidence['full5_ss'] != old_evidence['full5_ss']):
        evidence = {**old_evidence, 'full5_ss': None, 'ready_count': None, 'ready_source': None,
                    'evidence_reason': 'sieve_return_home_unverified'}
    else:
        evidence.update(ready_count=old_evidence['ready_count'], ready_source=old_evidence['ready_source'])
    stop = _ss_sieve_dispatch(ctx, evidence, summary)
    if stop:
        summary.update(status='continuation_checkpoint', stop_reason=stop)
        return None, stop
    return source, reason


def _manual_home_checkpoint(ctx, provider):
    """Use the current provider's game calendar, never the script turn count."""
    if not _cut_screening(ctx):
        from core.auto_trainer.larc_manual_handoff import checkpoint
        checkpoint(ctx, period=provider.get('larc_period'), source=provider.get('source'))


def _manual_race_checkpoint(ctx, context, report):
    if _cut_screening(ctx):
        return context
    from core.auto_trainer.larc_manual_handoff import snapshot, checkpoint
    config = snapshot(ctx.root)['config']
    configured = config['first_arc'] or config['final_arc'] or any(
        row.get('enabled', True) for row in config.get('dates', []))
    if not context and (ctx.allow_adaptations or configured):
        rows = report.get('step_reports') or []
        if rows:
            row = rows[-1]; result = row.get('result', {})
            context = read_larc_race_day_context(row.get('screenshot_path', ''),
                source_sha256=result.get('frame_safety', {}).get('source_image_sha256'),
                visible_text=result.get('decision', {}).get('evidence', {}).get('screen_text', {}).get('normalized_text', ''))
    checkpoint(ctx, context)
    return context


def _notify_manual_handoff(payload):
    from desktop.notifications import send_event
    value = payload.get('current_date') or {}
    grade = {1: '初级', 2: '经典', 3: '高级'}.get(value.get('year_index'))
    half = {'first': '上半月', 'second': '下半月'}.get(value.get('half'))
    label = (f"{grade}年{value['month']}月{half}" if grade and half and value.get('month') else '指定节点')
    return send_event(event_id='manual_takeover', title='MaaUma 需要人工处理',
        content=f'已到育成停点（{label}），请查看面板并确认安全暂停后接管。',
        dedupe_key=f"career:{payload['run_session_id']}:{payload['pause_id']}:manual")


def _notify_career_outcome(ctx, summary):
    """Report confirmed outcomes once; notifications never change game results."""
    if (summary.get('career_complete') is True and summary.get('status') == 'completed'
            and summary.get('completion_scope') == 'normal_settlement'):
        event, title, content = 'career_completed', 'MaaUma 育成完成', '本局已正常结算，请查看面板记录。'
    elif summary.get('status') == 'stopped' and not summary.get('stopped_by_control'):
        event, title, content = 'career_stopped', 'MaaUma 育成异常停止', '育成遇到技术停点，已保留现场，请查看面板。'
    else:
        return
    path = ctx.root/'session/larc_career_outcome_notifications.json'
    key = event + ':' + Path(summary['output']).name
    try:
        record = _read(path) if path.is_file() else {'run_session_id': ctx.run_id, 'events': {}}
        if record.get('run_session_id') != ctx.run_id:
            raise ValueError('notification_run_mismatch')
        if key in record['events']:
            return
        record['events'][key] = {'status': 'attempted'}
        _atomic_write_json(path, record)  # Mark before I/O, including unsuccessful attempts.
        from desktop.notifications import send_event
        result = send_event(event_id=event, title=title, content=content,
            dedupe_key=f'career:{ctx.run_id}:{key}')
        result = {field: result[field] for field in ('ok', 'status') if field in result}
        record['events'][key] = result
        _atomic_write_json(path, record)
        summary.setdefault('notifications', {})[event] = result
    except Exception:
        summary.setdefault('notifications', {})[event] = {'ok': False, 'status': 'failed'}


def _stop(summary, reason):
    summary.update(status="stopped", stop_reason=reason)
    if reason.startswith('terminal_settlement_stopped:'):
        return  # A stopped closer is not a successful terminal handoff.
    if reason in {"race_day_main_menu", "race_list", "race_detail", "race_scene", "fan_goal_warning_handoff",
                  "fan_goal_race_requires_permission"}:
        summary.update(status="handoff", handoff="race")
    elif reason in {"skill_acquisition_handoff", "career_complete", "team_formation", "ready_career_end",
                    "career_end_confirmation", "terminal_rating_verified"}:
        summary.update(status="handoff", handoff="terminal")


def _record_outing_inspection_failure(summary, error):
    """Keep this failed inspection's frame, never a previous pipeline image."""
    _stop(summary, "execution_exception")
    summary["error"] = f"{type(error).__name__}:{error}"
    report = error.report
    summary["recognition_failure_source"] = {
        "kind": "mei_outing_inspection", "report_path": error.report_path,
        "source": deepcopy(report.get("failure_source") or report.get("last_source")),
        "reason": report.get("reason"), "stage": report.get("stage"),
        "has_pending_operation": report.get("has_pending_operation"),
        "pending_operation": deepcopy(report.get("pending_operation")),
        "chooser_observations": deepcopy(report.get("chooser_observations", [])),
    }


def _wait_stop_counts(records):
    """Count exhausted waits, not routine unknown probe frames or game losses."""
    stops = [row for row in records if isinstance(row, dict)
        and row.get('schema_version') == 'uma.technical_wait_stop.v1']
    phases = Counter(row.get('wait', {}).get('phase') or 'unclassified' for row in stops)
    unknown = phases.get('unknown', 0)
    return {'scope': 'recorded_wait_stops_only', 'total': len(stops),
        'by_phase': dict(phases), 'unknown_count': unknown,
        'unknown_fraction': unknown / len(stops) if stops else None,
        'by_owner': dict(Counter(row.get('owner') for row in stops)),
        'by_surface': dict(Counter(row.get('surface') or 'unavailable' for row in stops)),
        'by_reason': dict(Counter(row.get('reason') for row in stops)),
        'by_expired_limit': dict(Counter(limit for row in stops
            for limit in row.get('wait', {}).get('expired_by', []))),
        'by_recovery_status': dict(Counter(row.get('recovery', {}).get('status', 'not_attempted')
            for row in stops))}


def _adopt_wait_stops(summary, records, source_report):
    """A nested/resumed report retains the same source/index identity."""
    target = summary.setdefault('technical_wait_stops', [])
    known = {row.get('event_id') for row in target}
    for index, row in enumerate(records):
        if not isinstance(row, dict) or row.get('schema_version') != 'uma.technical_wait_stop.v1':
            continue
        value = deepcopy(row)
        value.setdefault('source_report_path', str(source_report))
        value.setdefault('event_id', f"{value['source_report_path']}#wait_stop_{index}")
        if value['event_id'] not in known:
            target.append(value)
            known.add(value['event_id'])
    summary['technical_wait_stop_counts'] = _wait_stop_counts(target)


def _record_wait_stop(ctx, summary, budget, reason, source_report, *, surface=None, owner='career', **extra):
    row = technical_wait_stop(budget, reason=reason, owner=owner, surface=surface,
        run_session_id=ctx.run_id, turn_index=ctx.progress['next_turn'])
    row.update(extra)
    row['source_report_path'] = str(source_report)
    row['event_id'] = f"{source_report}#wait_stop_{len(summary.get('technical_wait_stops', []))}"
    _adopt_wait_stops(summary, [row], source_report)
    return row


def _await_late_career_page(ctx, frame_path):
    """A read-only wait after a proved page change, retaining this career owner."""
    path = Path(frame_path).resolve(strict=True)
    if not path.is_relative_to(ctx.root):
        raise ValueError("page_handoff_source_outside_run")
    route = RouteTransitionSession.load(ctx.route, expected_run_session_id=ctx.run_id)
    route.commit_action("wait:late_career_page", expected_next_states=(*POST_ACTION_ROUTE_SUCCESSORS, "training_menu"),
                        source_sha256=route.observation_sha256)
    route.save(ctx.route)


def _resumable_adaptation_selection(session):
    return (session.stage == "selecting" and not session.inspect_only
        and session.receipt is None
        and (session.submitted_source_sha256 is None or session.pending_retry_used))


def _adaptation_resume_candidates(ctx):
    """Read this run's purchase owners, excluding transaction-free inspections."""
    candidates, ignored = [], []
    for phase in PHASE_SLOT_COUNTS:
        path = ctx.root / "session" / f"larc_adaptation_{phase}.json"
        if not path.exists():
            continue
        session = LarcAdaptationSession.load(path, ctx.run_id)
        row = {"phase": session.phase, "stage": session.stage,
            "inspect_only": session.inspect_only, "returned": session.returned,
            "has_plan": session.plan is not None,
            "has_submission": session.submitted_source_sha256 is not None,
            "has_receipt": session.receipt is not None}
        transaction_free = (session.stage == "selecting"
            and (session.inspect_only or session.plan is None)
            and session.submitted_source_sha256 is None and session.receipt is None)
        if session.returned or transaction_free:
            # Keep the file unchanged. An inspection/empty preparation is not
            # a second purchase owner and cannot authorize any purchase.
            ignored.append(row)
        else:
            candidates.append(session)
    return candidates, ignored


def _return_unowned_adaptation_grid(ctx, output):
    """Only inspect and return from a currently proved zero-pending grid."""
    route = RouteTransitionSession.load(ctx.route, expected_run_session_id=ctx.run_id)
    if route.last_action_id in {
            "safe_advance.larc_adaptation_review", "safe_advance.larc_adaptation_upgrade",
            "safe_advance.larc_adaptation_level_up"}:
        # No matching ledger is not evidence that a preceding input failed.
        return {"ok": False, "reason": "larc_adaptation_resume_submission_not_safe"}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    session_path = output / "inspection_session.json"
    LarcAdaptationSession(ctx.run_id, path=session_path, inspect_only=True,
                          inspect_grid_only=True).save()
    # The existing inspection planner cannot select, review or buy. It only
    # returns after the current grid proves pending_cost == 0; a confirmation,
    # selected-cost grid or unreadable return stays with its original reason.
    result = return_from_growth_inspection(ctx, session_path, output / "return")
    return {**result, "inspection_only": True, "purchase_phase_adopted": False}


def _record_growth_failure(summary, result, turn, output, save_summary):
    value = {**result, "turn_index": turn, "path": str(output)}
    summary.setdefault("growth_adaptations", []).append(value)
    save_summary()
    return result


def _record_pipeline_growth_failure(summary, row, report, turn):
    if report.get("status") != "larc_growth_stopped":
        return
    preparation = report.get("growth_adaptation")
    code = preparation.get("reason") if isinstance(preparation, dict) else None
    if not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,119}", code) is None:
        return
    failure = {"reason": code, "wrapper_status": "larc_growth_stopped", "turn_index": turn}
    row["growth_failure"] = dict(failure)
    summary["growth_failure"] = dict(failure)
    summary["error_type"] = "GrowthPreparationError"
    summary["error"] = code


def _pre_action_reading_retry_reason(report, retries):
    status = report.get("status")
    if status in {"capture_failed", "sidecar_failed", "sidecar_quality_failed",
                  "training_page_observation_failed"} and retries < 2:
        return status
    if status == "live_loop_failed" and retries == 0:
        loop = (report.get("stages") or {}).get("live_loop") or {}
        if loop.get("status") == "pre_action_capture_artifact_missing":
            return loop["reason"]
    execution = ((report.get("stages") or {}).get("live_loop") or {}).get("execution") or {}
    if (execution.get("performed") is False and not execution.get("tap_plan")
            and not execution.get("tap_performed")
            and execution.get("reason") == "selected_primary_gain_unreliable"
            and retries < 2):
        return "selected_primary_gain_unreliable"
    return None


def _record_terminal_pipeline_failure(summary, row, report, code):
    """Keep short child-stage diagnostics when that pipeline really stops.

    Preserve the original stop/resume decision. Raw exception strings may
    contain paths or credentials, so only exception classes and code-like
    values are copied into the automatically submitted stop summary.
    """
    stage_name = {"live_loop_failed": "live_loop",
                  "larc_ss_information_failed": "larc_ss_information"}.get(report.get("status"))
    if stage_name is None:
        return
    stage = (report.get("stages") or {}).get(stage_name)
    if not isinstance(stage, dict):
        return
    detail = {"stage": stage_name, "pipeline_status": report["status"]}
    if type(code) is int:
        detail["pipeline_code"] = code
    def short_code(value):
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,199}", value) else None
    for key in ("status", "reason", "error_type"):
        value = short_code(stage.get(key))
        if value:
            detail[key] = value
    error = stage.get("error")
    if isinstance(error, str):
        match = re.match(r"^([A-Za-z][A-Za-z0-9_]*(?:Error|Exception)):\s*(.*)$", error)
        if match:
            detail["error_type"] = match[1]
        detail["error_fingerprint"] = hashlib.sha256(error.encode("utf-8")).hexdigest()[:16]
    summary["pipeline_failure"] = dict(detail)
    row["pipeline_failure"] = dict(detail)
    summary.setdefault("error_type", detail.get("error_type", "PipelineStageFailure"))
    leaf = detail.get("reason") or detail.get("status") or report["status"]
    summary.setdefault("error", detail["error_type"] + ":" + leaf if detail.get("error_type") else leaf)


def _resume_submitted_adaptation(ctx, output, summary, save_summary):
    """Restore an owned selection or unfinished purchase before generic routing."""
    if not ctx.allow_adaptations or ctx.expected is not None:
        return None
    pending = []
    selection_owner = False
    route_path = getattr(ctx, "route", None)
    if route_path is not None and Path(route_path).is_file():
        route = RouteTransitionSession.load(route_path, expected_run_session_id=ctx.run_id)
        if (tuple(route.expected_next_states) == ("modal",)
                and isinstance(route.last_action_id, str)
                and route.last_action_id.startswith("safe_advance.larc_adaptation_")):
            from core.auto_trainer.safe_advance_transition import expected_next_states_for_decision
            successors = expected_next_states_for_decision({"action":"tap",
                "reason":route.last_action_id.removeprefix("safe_advance."),
                "tap":{"coordinate_name":route.last_action_id}})
            if set(successors) == {"modal", "loading"}:
                # Upgrade only the old observation contract, not the recorded
                # action, source, position, purchase receipt or game progress.
                route.expected_next_states = tuple(successors)
                route.transition_version += 1
                route.save(route_path)
        selection_owner = (route.current_state in {"modal", "loading"}
            and route.last_action_id in {
                "safe_advance.larc_adaptation_open", "safe_advance.larc_adaptation_select",
                "safe_advance.larc_adaptation_level_up", "safe_advance.larc_adaptation_review",
                "wait:larc_adaptation_selected_level_loading", "wait:larc_adaptation_modal_loading",
                "wait:larc_adaptation_grid_loading"})
    for phase in PHASE_SLOT_COUNTS:
        path = ctx.root / "session" / f"larc_adaptation_{phase}.json"
        if not path.exists():
            continue
        session = LarcAdaptationSession.load(path, ctx.run_id)
        if (not session.returned and not session.inspect_only
                and (session.stage in {"submission_pending", "awaiting_result", "complete"}
                     or (_resumable_adaptation_selection(session) and session.plan is not None
                         and (selection_owner or (session.pending_retry_used
                             and session.submitted_source_sha256 is not None))))):
            pending.append(session)
    if not pending:
        return None
    if len(pending) != 1:
        return False, "larc_adaptation_resume_requires_unique_session"
    session = pending[0]
    if session.stage == "selecting":
        ready, _, reason = _ensure_adaptation(ctx, session.phase, output, summary, save_summary)
        return ready, reason
    # A saved pending attempt may now show a harmless result notice. Let its
    # existing owner observe/close it; the planner still forbids re-submission.
    if session.submitted_source_sha256 is None:
        return False, "larc_adaptation_submission_outcome_unknown"
    if session.stage == "complete" and session.receipt is None:
        return False, "larc_adaptation_resume_receipt_missing"
    ready, _, reason = _ensure_adaptation(ctx, session.phase, output, summary, save_summary)
    return ready, reason


def _partial_first_arc_race_ready(report):
    """A settled Pt shortfall is a weaker race state, not a broken transaction."""
    session = report.get("session") or {}
    receipt = session.get("receipt") or {}
    plan = session.get("plan") or {}
    levels = receipt.get("levels") or []
    deferred = plan.get("deferred_slots") or []
    after_points = receipt.get("after_points")
    return bool(
        report.get("stop_reason") == "larc_adaptation_phase_incomplete"
        and report.get("phase") == session.get("phase") == "first_arc"
        and session.get("stage") == "complete" and session.get("returned") is True
        and receipt.get("pending_cost") == 0
        and len(levels) == 10 and all(type(v) is int for v in levels)
        and min(levels[:6]) >= 2 and min(levels[6:8]) >= 1
        and levels == plan.get("targets")
        and deferred and set(deferred).issubset({6, 7})
        and type(after_points) is int and 0 <= after_points < 200
    )


def _locked_hope_final_race_ready(report):
    """A locked game bonus with no possible purchase is not a broken workflow."""
    session = report.get('session') or {}
    plan = session.get('plan') or {}
    levels = plan.get('targets') or []
    return bool(report.get('stop_reason') == 'larc_adaptation_phase_incomplete'
        and report.get('phase') == session.get('phase') == 'final_arc'
        and session.get('stage') == 'selecting' and session.get('returned') is True
        and session.get('submitted_source_sha256') is None and session.get('receipt') is None
        and plan.get('cost') == 0 and plan.get('baseline_levels') == levels
        and len(levels) == 10 and all(type(value) is int for value in levels)
        and min(levels[:8]) >= 2 and levels[8] == 0
        and plan.get('deferred_slots') == [8])


def _is_larc_exchange_hub(evidence):
    if not isinstance(evidence, dict) or evidence.get("larc_race_day", {}).get("verified") is not True:
        return False
    compact = "".join(str(evidence.get("screen_text", {}).get("normalized_text", "")).split())
    return "凯旋门代表交流赛" in compact and "比赛日" not in compact


_PHASE_MIN_ORDINALS = {
    "first_exchange": 22,
    "first_expedition": 35,
    "first_overseas_race": 36,
    "first_overseas_race_defer_language": 36,
    "first_overseas_race_defer_power": 36,
    "first_overseas_race_defer_power_language": 36,
    "speed_expedition": 36,
    "power_expedition": 36,
    "stamina_expedition": 36,
    "guts_expedition": 36,
    "french_three": 36,
    "expedition_skill_pt": 36,
    "courage_three": 36,
    "willpower_three": 36,
    "first_arc": 42,
    "first_arc_defer_language": 42,
    "foy_prelude": 59,
    "final_arc": 60,
    "champion_dream_two": 60,
}


def _phase_min_ordinal(phase: str) -> int:
    return max((_PHASE_MIN_ORDINALS.get(part, 0) for part in str(phase).split("+")), default=0)


def _valid_stats_snapshot(stats) -> dict[str, int] | None:
    if not isinstance(stats, dict):
        return None
    keys = ("speed", "stamina", "power", "guts", "wisdom")
    out = {}
    for key in keys:
        val = stats.get(key)
        if type(val) is not int or val < 0:
            return None
        out[key] = val
    return out


def _clear_stale_adaptations_for_year1_takeover(ctx, summary, save_summary, *, phases=None):
    """Invalidate stale cached adaptation completions once after a live takeover."""
    session_dir = ctx.root / "session"
    index_path = session_dir / "larc_adaptation_ready.json"
    index = {}
    if index_path.is_file():
        try:
            loaded = _read(index_path)
            if isinstance(loaded, dict):
                index = loaded
        except (OSError, ValueError, TypeError):
            index = {}
    index_valid = (
        isinstance(index, dict)
        and index.get("run_session_id") == ctx.run_id
        and index.get("device") == ctx.progress["device"]
    )
    if phases is None:
        extra = (
            list((index.get("phases") or {}).keys()) + list((index.get("partial_phases") or {}).keys())
            if isinstance(index, dict)
            else []
        )
        target_phases = tuple(dict.fromkeys([*PHASE_SLOT_COUNTS, *extra]))
    else:
        target_phases = tuple(phases)
    index_changed = False
    cleared = []
    archived_files = False
    archive_dir = session_dir / "live_takeover_history" / f"year1_recheck_{time_ns()}"
    ready_cache = getattr(ctx, "ready_adaptations", None)
    for phase in target_phases:
        session_path = session_dir / f"larc_adaptation_{phase}.json"
        if session_path.is_file():
            try:
                loaded_session = LarcAdaptationSession.load(session_path, ctx.run_id)
                if loaded_session.stage in {"submission_pending", "awaiting_result"}:
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                pass
            archive_dir.mkdir(parents=True, exist_ok=True)
            session_path.replace(archive_dir / session_path.name)
            archived_files = True
            cleared.append(phase)
        if isinstance(ready_cache, dict) and phase in ready_cache:
            ready_cache.pop(phase, None)
            cleared.append(phase)
        if index_valid:
            phases_map = index.get("phases")
            if isinstance(phases_map, dict) and phase in phases_map:
                phases_map.pop(phase, None)
                index_changed = True
                cleared.append(phase)
            partial_map = index.get("partial_phases")
            if isinstance(partial_map, dict) and phase in partial_map:
                partial_map.pop(phase, None)
                index_changed = True
                cleared.append(phase)
    if index_valid and index_changed:
        _atomic_write_json(index_path, index)
    ctx.live_takeover_year1_adapt_recheck = False
    unique_cleared = sorted(set(cleared))
    if unique_cleared:
        summary["live_takeover_year1_adaptation_recheck"] = {
            "cleared_phases": unique_cleared,
            "archive": str(archive_dir) if archived_files else None,
        }
        save_summary()
    return unique_cleared


def _load_prior_career_fingerprint(ctx):
    from core.auto_trainer.larc_calendar import period_ordinal

    fp_path = ctx.root / "session/larc_career_fingerprint.json"
    if fp_path.is_file():
        try:
            loaded = _read(fp_path)
            if isinstance(loaded, dict) and loaded.get("run_session_id") == ctx.run_id:
                return loaded
        except (OSError, ValueError, TypeError):
            pass

    current_segment = getattr(ctx, "segment_root", None)
    max_ordinal = None
    last_ordinal = None
    last_period = None
    last_stats = None
    last_turn = None
    try:
        candidates = sorted(ctx.root.glob("autonomous_*/summary.json"), key=lambda p: p.parent.name)
    except OSError:
        candidates = []
    for summary_path in candidates:
        if current_segment is not None and summary_path.parent == current_segment:
            continue
        try:
            doc = _read(summary_path)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(doc, dict) or doc.get("run_session_id") != ctx.run_id:
            continue
        for row in doc.get("turns") or []:
            if not isinstance(row, dict):
                continue
            before = row.get("before") or {}
            per = before.get("period")
            ord_val = period_ordinal(per) if isinstance(per, dict) else None
            stats = _valid_stats_snapshot(before.get("current_stats"))
            if ord_val is not None:
                max_ordinal = ord_val if max_ordinal is None else max(max_ordinal, ord_val)
                last_ordinal = ord_val
                last_period = deepcopy(per)
            if stats is not None:
                last_stats = stats
            if type(row.get("turn_index")) is int:
                last_turn = row["turn_index"]
    if max_ordinal is None and last_stats is None:
        return None
    return {
        "schema_version": "larc_career_fingerprint.v1",
        "run_session_id": ctx.run_id,
        "max_ordinal": max_ordinal,
        "last_ordinal": last_ordinal,
        "last_period": last_period,
        "last_stats": last_stats,
        "last_turn": last_turn,
    }


def _record_career_fingerprint(ctx, provider):
    from core.auto_trainer.larc_calendar import period_ordinal

    period_wrap = provider.get("larc_period") or {}
    if period_wrap.get("status") != "verified":
        return
    obs_period = period_wrap.get("value") or {}
    obs_ord = period_ordinal(obs_period)
    obs_stats = _valid_stats_snapshot((provider.get("state_context") or {}).get("current_stats"))
    fp_path = ctx.root / "session/larc_career_fingerprint.json"
    existing_max = None
    if fp_path.is_file():
        try:
            prev = _read(fp_path)
            if isinstance(prev, dict) and prev.get("run_session_id") == ctx.run_id:
                existing_max = prev.get("max_ordinal")
        except (OSError, ValueError, TypeError):
            existing_max = None
    if obs_ord is not None and type(existing_max) is int:
        max_ordinal = max(existing_max, obs_ord)
    elif obs_ord is not None:
        max_ordinal = obs_ord
    else:
        max_ordinal = existing_max
    payload = {
        "schema_version": "larc_career_fingerprint.v1",
        "run_session_id": ctx.run_id,
        "device": ctx.progress["device"],
        "max_ordinal": max_ordinal,
        "last_ordinal": obs_ord,
        "last_period": deepcopy(obs_period),
        "last_stats": obs_stats,
        "last_turn": ctx.progress.get("next_turn"),
    }
    _atomic_write_json(fp_path, payload)


def _detect_career_discontinuity(ctx, provider, prior):
    from core.auto_trainer.larc_calendar import period_ordinal

    reasons = []
    period_wrap = provider.get("larc_period") or {}
    obs_period = (period_wrap.get("value") or {}) if period_wrap.get("status") == "verified" else {}
    obs_ord = period_ordinal(obs_period) if obs_period else None
    obs_stats = _valid_stats_snapshot((provider.get("state_context") or {}).get("current_stats"))

    if isinstance(prior, dict):
        prior_max = prior.get("max_ordinal")
        prior_last_period = prior.get("last_period") or {}
        if (type(prior_max) is int and obs_ord is not None and obs_ord < prior_max) or (
            prior_last_period.get("pre_debut") is False and obs_period.get("pre_debut") is True
        ):
            reasons.append("date_regressed")
        prev_stats = _valid_stats_snapshot(prior.get("last_stats"))
        if prev_stats is not None and obs_stats is not None:
            any_drop = any(obs_stats[k] <= prev_stats[k] - 60 for k in prev_stats)
            sum_drop = sum(obs_stats.values()) <= sum(prev_stats.values()) - 80
            same_day_jump = (
                type(prior.get("last_ordinal")) is int
                and obs_ord == prior["last_ordinal"]
                and (
                    abs(sum(obs_stats.values()) - sum(prev_stats.values())) >= 120
                    or any(abs(obs_stats[k] - prev_stats[k]) >= 80 for k in prev_stats)
                )
            )
            if any_drop or sum_drop or same_day_jump:
                reasons.append("stats_discontinuity")

    index_path = ctx.root / "session/larc_adaptation_ready.json"
    if obs_ord is not None and index_path.is_file():
        try:
            index = _read(index_path)
            if (
                isinstance(index, dict)
                and index.get("run_session_id") == ctx.run_id
                and index.get("device") == ctx.progress["device"]
            ):
                cached_phases = list((index.get("phases") or {}).keys()) + list((index.get("partial_phases") or {}).keys())
                if any(_phase_min_ordinal(phase) > obs_ord for phase in cached_phases):
                    reasons.append("cached_adaptation_ahead_of_date")
        except (OSError, ValueError, TypeError):
            pass

    if obs_period.get("year_index") == 1 or obs_period.get("pre_debut") is True:
        reasons.append("year1_takeover")
    return reasons


def _reconcile_live_takeover_career_continuity(ctx, provider, summary, save_summary):
    """Invalidate stale adaptation and outing ledgers when live-takeover detects another career or Year-1 state."""
    prior = _load_prior_career_fingerprint(ctx)
    reasons = _detect_career_discontinuity(ctx, provider, prior)
    if not reasons:
        ctx.live_takeover_year1_adapt_recheck = False
        return None
    cross_career = any(
        r in {"date_regressed", "cached_adaptation_ahead_of_date", "stats_discontinuity"} for r in reasons
    )
    cleared_adaptations = _clear_stale_adaptations_for_year1_takeover(ctx, summary, save_summary)
    growth_cache = getattr(ctx, "growth_checks", None)
    if isinstance(growth_cache, set):
        growth_cache.clear()
    ctx.incomplete_first_arc_accepted = False

    session_dir = ctx.root / "session"
    archive_dir = session_dir / "live_takeover_history" / f"career_reset_{time_ns()}"
    archived_other = []

    def archive_file(path: Path) -> bool:
        if not path.is_file():
            return False
        archive_dir.mkdir(parents=True, exist_ok=True)
        path.replace(archive_dir / path.name)
        archived_other.append(path.name)
        return True

    cleared_mei_outing = False
    outing_path = session_dir / "larc_friend_outing.json"
    if outing_path.is_file():
        should_reset_outing = cross_career
        if not should_reset_outing and "year1_takeover" in reasons:
            try:
                outing_doc = _read(outing_path)
                should_reset_outing = bool(
                    isinstance(outing_doc, dict)
                    and ((outing_doc.get("completed") or 0) > 0 or outing_doc.get("unlocked") is True)
                )
            except (OSError, ValueError, TypeError):
                should_reset_outing = True
        if should_reset_outing:
            cleared_mei_outing = archive_file(outing_path)

    if cross_career:
        archive_file(session_dir / "larc_action_history.json")
        archive_file(session_dir / "larc_career_fingerprint.json")
        support_path = session_dir / "larc_support_skill_startup.json"
        if support_path.is_file():
            try:
                sup_doc = _read(support_path)
                if not isinstance(sup_doc, dict) or sup_doc.get("status") != "pending_purchase":
                    archive_file(support_path)
            except (OSError, ValueError, TypeError):
                archive_file(support_path)

    result = {
        "reasons": reasons,
        "cleared_adaptations": cleared_adaptations,
        "cleared_mei_outing": cleared_mei_outing,
        "archived_files": archived_other,
        "archive": str(archive_dir) if archived_other else None,
    }
    summary["live_takeover_career_reset"] = result
    save_summary()
    return result


def _ensure_adaptation(ctx, phase, output, summary, save_summary):
    """Reuse a proven report, or delegate one bounded phase without replacing its session."""
    if phase in {"first_overseas_race", "first_arc"}:
        from core.auto_trainer.larc_language_deferral import selected_language_deferred_phase
        selected_phase = selected_language_deferred_phase(ctx.root, phase, ctx.progress["next_turn"],
                                                          load_goal_config(ctx.root / "goal.json"))
        if selected_phase != phase:
            if Path(output).name == f"adaptation_{phase}":
                output = Path(output).with_name(f"adaptation_{selected_phase}")
            phase = selected_phase
    if phase == "first_arc" and ctx.incomplete_first_arc_turn == ctx.progress["next_turn"]:
        path = ctx.root / "session/larc_adaptation_first_arc.json"
        session = LarcAdaptationSession.load(path, ctx.run_id)
        plan = session.plan or {}
        targets = plan.get("targets", [])
        settled = session.stage == "complete" or (
            session.stage == "selecting" and plan.get("cost") == 0
            and plan.get("baseline_levels") == targets)
        if (session.phase != phase or session.inspect_only or not session.returned
                or not settled or len(targets) < 8
                or any(level < 2 for level in targets[:6]) or targets[6:8] != [0, 0]
                or plan.get("deferred_slots") != [6, 7]):
            raise ValueError("first_arc_exception_context_mismatch")
        ctx.incomplete_first_arc_accepted = True
        summary.setdefault("adaptation_exceptions", []).append({
            "run_session_id": ctx.run_id, "device": ctx.progress["device"],
            "turn_index": ctx.progress["next_turn"], "phase": phase, "phase_complete": False,
            "reason": "user_approved_locked_adaptations_entry", "session_path": str(path),
            "deferred_slots": list(session.plan["deferred_slots"])})
        save_summary()
        return True, False, "user_approved_incomplete_first_arc"
    if phase in ctx.ready_adaptations:
        return True, False, ctx.ready_adaptations[phase]
    if phase in {'first_overseas_race','first_arc','foy_prelude','final_arc'}:
        from core.auto_trainer.larc_ai_nodes import checkpoint_choice
        choice=checkpoint_choice(ctx,'adaptation_checkpoint',{'phase':phase},reason='expedition_or_required_adaptation')
        if choice is not None and not choice.get('cached'):
            summary.setdefault('ai_checkpoints',[]).append(choice);save_summary()
    index_path = ctx.root / "session/larc_adaptation_ready.json"
    index = _read(index_path) if index_path.exists() else {
        "run_session_id": ctx.run_id, "device": ctx.progress["device"], "phases": {},
    }
    if index.get("run_session_id") != ctx.run_id or index.get("device") != ctx.progress["device"]:
        raise ValueError("larc_adaptation_ready_index_context_mismatch")
    reference = index["phases"].get(phase) or index.get("partial_phases", {}).get(phase)
    reused = reference is not None
    if reused:
        report_path = Path(reference).resolve(strict=True)
        if not report_path.is_relative_to(ctx.root):
            raise ValueError("larc_adaptation_ready_report_outside_run")
        report, code = _read(report_path), 0
        session_path = ctx.root / "session" / f"larc_adaptation_{phase}.json"
        session = LarcAdaptationSession.load(session_path, ctx.run_id)
        if (report.get("run_session_id") != ctx.run_id or report.get("device") != ctx.progress["device"]
                or report.get("phase") != phase or report.get("phase_session_path") != str(session_path)
                or session.phase != phase or not session.returned or session.inspect_only
                or report.get("session") != session.to_dict()):
            raise ValueError("larc_adaptation_ready_report_session_mismatch")
    else:
        report_path = output / "report.json"
        report, code = run_larc_adaptation(run_root=ctx.root, output_dir=output, adb_port=ctx.port,
            phase=phase, max_steps=48, safety_analyzer=getattr(ctx, "screen_reader", None))
    reason = str(report.get("stop_reason", "larc_adaptation_stopped"))
    summary["adaptation_reports"].append({"phase": phase, "path": str(report_path),
        "code": code, "reason": reason, "reused": reused})
    save_summary()
    locked_hope = _locked_hope_final_race_ready(report)
    if code in {0, 2} and (_partial_first_arc_race_ready(report) or locked_hope):
        # Keep the policy's desired levels and original partial report intact.
        # The forced race can use the actual committed levels, without another
        # shop visit or claiming that every planned bonus was purchased.
        reason = "larc_adaptation_partial_race_ready"
        summary["adaptation_reports"][-1].update(partial_phase_accepted=True,
            deferred_slots=list(report["session"]["plan"]["deferred_slots"]))
        if locked_hope:
            summary['adaptation_reports'][-1].update(locked_condition='凯旋门的希望未获得',
                phase_complete=False, additional_purchase_submitted=False)
        if not reused:
            index.setdefault("partial_phases", {})[phase] = str(report_path)
            _atomic_write_json(index_path, index)
        ctx.ready_adaptations[phase] = reason
        save_summary()
        return True, not reused, reason
    if (code or report.get("is_valid") is not True or report.get("phase_complete") is not True
            or report.get("status") != "ready"
            or reason not in {"larc_adaptation_verified", "larc_adaptation_already_ready"}):
        return False, False, reason if not reused else "larc_adaptation_ready_report_unverified"
    if not reused:
        index["phases"][phase] = str(report_path)
        _atomic_write_json(index_path, index)
    ctx.ready_adaptations[phase] = reason
    return True, not reused, reason


def _prepare_growth(ctx, period, text, output, summary, save_summary,
                    *, preferred_training=None, race_day=False, from_training=False, state=None, goal=None):
    turn = ctx.progress["next_turn"]
    if from_training and getattr(ctx, "growth_purchase_turn", None) == turn:
        return None  # Refreshed previews go straight to action comparison.
    key = growth_check_key(ctx.root, period, text,
        preferred_training=preferred_training, race_day=race_day, state=state, goal=goal)
    kwargs = dict(preferred_training=preferred_training, race_day=race_day, state=state, goal=goal)
    cached_key=key is not None and key in ctx.growth_checks
    if key is None or cached_key:
        # Only explicit193 can quote known current facts below the old shop
        # floor or reattach after an old cache hit. Never inspect unknowns.
        from core.auto_trainer.larc_joint_career.dispatch import enabled as career_enabled
        from core.auto_trainer.larc_joint_career.binding import source as career_source
        from core.auto_trainer.larc_calendar import period_ordinal
        bound=career_source(state) if state is not None else None
        growth=(state.scenario_state or{}).get('larc_growth_observation')or{} if state is not None else{}
        observed_source=growth.get('source')or{} if isinstance(growth,dict)else{}
        frame=growth.get('source_frame_sha256')if isinstance(growth,dict)else None
        points=growth.get('adaptation_points')if isinstance(growth,dict)else None
        cached=getattr(ctx,'growth_grid',None)
        cached_grid=cached.get('grid')if isinstance(cached,dict)else None
        day=period_ordinal(period.get('value')or{})if isinstance(period,dict)and period.get('status')=='verified'else None
        actual_period=(state.scenario_state or{}).get('larc_period')or{} if state is not None else{}
        actual_day=period_ordinal(actual_period.get('value')or{})if isinstance(actual_period,dict)and actual_period.get('status')=='verified'and isinstance(actual_period.get('value'),dict)else None
        if (not from_training or race_day or not career_enabled(goal)or day not in(36,37,38,39,41)
                or actual_day!=day or bound is None or bound['run_session_id']!=ctx.run_id or state.turn!=turn
                or getattr(getattr(ctx,'args',None),'allow_adaptations',True)is not True
                or not isinstance(cached_grid,dict)or cached_grid.get('pending_cost')!=0 or cached_grid.get('errors')
                or not isinstance(cached.get('period'),dict)or not isinstance(growth,dict)
                or growth.get('status')!='accepted'or growth.get('turn_index')!=turn
                or not isinstance(observed_source,dict)or observed_source.get('run_session_id')!=ctx.run_id
                or observed_source.get('turn_index')!=turn or not isinstance(frame,str)or not frame
                or observed_source.get('sha256')!=frame or type(points)is not int or points<0
                or(not cached_key and points>=200)):
            return None
        result=timed_call("growth_quote_cached", cached_growth_recommendation, ctx, period, text, **kwargs)
        if (not isinstance(result,dict)or not result.get('ok')
                or(result.get('recommendation',{}).get('career_joint')or{}).get('applied')is not True):
            return None
        # A pure NoBuy quote must be reattached even after an earlier check
        # of this capture. The existing purchased-turn guard still runs first.
        key=('career193_known_low_wallet',ctx.run_id,turn,day,points,
             tuple(sorted(bound['page_sha256'].items())))
    else:
        if key in ctx.growth_checks:return None
        result = timed_call("growth_quote_cached", cached_growth_recommendation, ctx, period, text, **kwargs)
    if result is not None:
        from core.auto_trainer.larc_ai_nodes import growth_choice
        result=growth_choice(ctx,period,result,state=state,goal=goal,race_day=race_day)
        from core.auto_trainer.larc_language_deferral import record_purchase_plan
        record_purchase_plan(ctx.root, turn, result["recommendation"],
            goal or load_goal_config(ctx.root / "goal.json"), grid=result.get("grid"))
    if result is not None and not result["recommendation"].get("phase"):
        ctx.growth_checks.add(key)
        result.update(requires_recollection=False, visited=False)
        summary.setdefault("growth_adaptations", []).append({"turn_index": turn, **result})
        save_summary()
        return result
    source = None
    if from_training:
        source, reason = timed_call("growth_exit_training", _route_to_menu,
                                    ctx, output / "to_menu", summary, save_summary)
        if source is None:
            return _record_growth_failure(summary, {"ok": False, "reason": reason},
                                          turn, output, save_summary)
    if result is None:
        result = timed_call("growth_grid_inspect_and_quote", inspect_growth_adaptation,
                            ctx, output / "grid", period, stay_on_grid=True, **kwargs)
    if not result.get("ok"):
        return _record_growth_failure(summary, result, turn, output, save_summary)
    from core.auto_trainer.larc_ai_nodes import growth_choice
    result=growth_choice(ctx,period,result,state=state,goal=goal,race_day=race_day)
    from core.auto_trainer.larc_language_deferral import record_purchase_plan
    record_purchase_plan(ctx.root, turn, result["recommendation"],
        goal or load_goal_config(ctx.root / "goal.json"), grid=result.get("grid"))
    result["visited"] = True
    if result.get("grid"):
        ctx.growth_grid = {"period": period.get("value"), "grid": result["grid"]}
    phase = result["recommendation"].get("phase")
    if phase:
        ready, executed, reason = timed_call("growth_purchase_and_verify", _ensure_adaptation,
                                             ctx, phase, output / phase, summary, save_summary)
        purchased = ready and executed and reason == "larc_adaptation_verified"
        result.update(ok=ready, upgraded=purchased, reason=reason,
                      requires_recollection=purchased and from_training)
        if ready:
            phase_path = ctx.root / "session" / f"larc_adaptation_{phase}.json"
            if phase_path.exists():
                purchased_session = LarcAdaptationSession.load(phase_path, ctx.run_id)
                receipt = purchased_session.receipt or {}
                from core.auto_trainer.larc_joint_career.session import remember_verified
                remember_verified(ctx, result["recommendation"], receipt, phase_path,
                    verified=purchased)
                if receipt.get("levels") is not None:
                    ctx.growth_grid = {"period": period.get("value"), "grid": {
                        "levels": receipt["levels"], "points": receipt["after_points"],
                        "pending_cost": 0, "errors": []}}
        if purchased and from_training:
            ctx.growth_purchase_turn = turn
            before_levels = (result.get("grid") or {}).get("levels")
            after_levels = (getattr(ctx, "growth_grid", {}) or {}).get("grid", {}).get("levels")
            if (isinstance(before_levels, list) and isinstance(after_levels, list)
                    and len(before_levels) == len(after_levels) == 10):
                slots = ("guts", "stamina", "power", "speed", "wisdom")
                changed = [name for index, name in enumerate(slots)
                    if type(before_levels[index]) is int and type(after_levels[index]) is int
                    and after_levels[index] > before_levels[index]]
                if changed:
                    ctx.last_training_purchase = {"turn": turn, "training_type": changed[0],
                        "training_types": changed, "basis": "verified_same_turn_level_receipt"}
    else:
        returned = timed_call("growth_return_home", return_from_growth_inspection,
                              ctx, result["inspection_session_path"], output / "return")
        result.update(ok=returned["ok"], reason=returned["reason"], requires_recollection=False)
        source = returned.get("menu_source")
    if result.get("ok") and from_training and not result.get("requires_recollection"):
        if phase:  # A live visit can find the requested upgrade already acquired.
            source, reason = timed_call("growth_return_home", _route_to_menu,
                                        ctx, output / "return_home", summary, save_summary)
        restored = timed_call("growth_restore_training", restore_training_menu,
                              ctx, source, output / "restore_training")
        result.update(ok=restored["ok"], reason=restored["reason"])
    if result.get("ok"):
        ctx.growth_checks.add(key)
    summary.setdefault("growth_adaptations", []).append({"turn_index": turn,
        "path": str(output), **result})
    save_summary()
    return result


def _training_growth(ctx, state, text, output, summary, save_summary):
    return _prepare_growth(ctx, state.scenario_state.get("larc_period", {}), text,
        output, summary, save_summary,
        from_training=True, state=state, goal=load_goal_config(ctx.root / "goal.json"))


def _terminal_finish_verified(ctx):
    path = ctx.root / "session/larc_terminal_finish.json"
    if not path.is_file():
        return False
    state = _read(path)
    if (state.get("run_session_id") != ctx.run_id or state.get("device") != ctx.progress["device"]
            or state.get("turn_index") != ctx.progress["next_turn"] or state.get("status") != "submitted"):
        return False
    report = _read(state["confirmation_report"])
    rows = report.get("step_reports", [])
    result = rows[-1].get("result", {}) if rows else {}
    return (result.get("performed") is True and result.get("tap_performed") is True
        and result.get("decision", {}).get("tap", {}).get("coordinate_name") == "safe_advance.finish_training"
        and bool(result.get("frame_safety", {}).get("source_image_sha256")))


def _finish_terminal(ctx, output, summary, save_summary):
    state_path = ctx.root / "session/larc_terminal_finish.json"
    if state_path.exists():
        return False, "terminal_finish_pending_or_submitted_requires_review"
    skills, code = run_larc_terminal_skills(run_root=ctx.root, output_dir=output / "skills",
        d1_root=ctx.d1_skill_root, external_skill_db_path=ctx.external_skill_db_path, adb_port=ctx.port)
    summary["terminal_skills"] = skills
    save_summary()
    if code or skills.get("ready_to_finish") is not True:
        return False, skills.get("stop_reason", "terminal_skills_stopped")
    points = skills["remaining_skill_points"]
    state = {"run_session_id": ctx.run_id, "device": ctx.progress["device"],
             "turn_index": ctx.progress["next_turn"], "status": "pending",
             "skills_report": skills["report_path"], "remaining_skill_points": points}
    for stage, action in (("entry", "career_end_finish"), ("confirmation", "finish_training")):
        report_path = output / stage / "report.json"
        state.update(stage=stage, pending_report=str(report_path))
        _atomic_write_json(state_path, state)
        argv = ["--mode", "live", "--scenario", "larc", "--max-steps", "1",
            "--adb-port", str(ctx.port), "--run-session-id", ctx.run_id,
            "--transition-session-json", str(ctx.route), "--skip-session-json", str(ctx.skip),
            "--career-goal-ledger-json", str(ctx.ledger), "--career-goal-observed-turn", str(ctx.progress["next_turn"]),
            "--screenshot-dir", str(report_path.parent), "--expected-action-id", "safe_advance." + action,
            "--execute", "--confirm-token", "UG_PARENT_SAFE_ADVANCE_SANDBOX"]
        if stage == "entry":
            argv += ["--allow-career-end-finish-after-skill-handoff"]
            # D1 has verified its return to the finish-ready page. Reattach
            # the common route to that current frame instead of retaining
            # the old skill-list state across the isolated child worker.
            argv += ["--transition-resync", "--route-bootstrap-state", "live-career"]
        elif points > 0:
            argv += ["--allow-finish-training-with-unused-skill-points"]
        report, code = advance(advance_args(argv))
        _atomic_write_json(report_path, report)
        summary["route_reports"].append({"path": str(report_path), "reason": report.get("stop_reason"), "code": code})
        save_summary()
        rows = report.get("step_reports", [])
        result = rows[-1].get("result", {}) if rows else {}
        if (code or result.get("performed") is not True or result.get("tap_performed") is not True
                or result.get("decision", {}).get("tap", {}).get("coordinate_name") != "safe_advance." + action
                or not result.get("frame_safety", {}).get("source_image_sha256")):
            return False, "terminal_finish_unverified:" + str(report.get("stop_reason"))
        state[stage + "_report"] = str(report_path)
    state.update(status="submitted")
    _atomic_write_json(state_path, state)
    return True, "terminal_finish_submitted"


def _record_race_executor_error(summary, report, code):
    rows = report.get("step_reports") or []
    last = rows[-1].get("result", {}) if rows else {}
    error = last.get("error")
    if (report.get("stop_reason") == last.get("reason") == "race_executor_error"
            and type(error) is str and error):
        summary["race_reports"][-1]["error"] = error
        if code or report.get("is_valid") is not True:
            summary["error"] = error


def _run_fixed_race(ctx, race_root, summary, save_summary, *, resume=False,
                    new_exchange_after_race=False, first_win_rescue=False, live_source=None):
    checkpoint_path = ctx.root / "session/larc_race_checkpoint.json"
    checkpoint = _read(checkpoint_path) if checkpoint_path.is_file() else {}
    previously_entered = (
        checkpoint.get("run_session_id") == ctx.run_id
        and checkpoint.get("turn_index") == ctx.progress["next_turn"]
        and _fixed_race_entry_confirmed(checkpoint.get("steps") or [])
    )
    kwargs = {"resume": True} if resume else {}
    if live_source is not None:
        kwargs['live_source'] = live_source
        previously_entered = live_source.get('entered') is True
    if first_win_rescue:
        kwargs["first_win_rescue"] = True
    cut_hook=getattr(ctx,'cut_farming_hook',None)
    if cut_hook is not None and getattr(cut_hook,'session',None) is not None:
        kwargs['debut_clock_retry']=cut_hook.session.config.debut_clock_retry
    if new_exchange_after_race:
        kwargs["new_exchange_after_race"] = True
        previously_entered = False
    if ctx.incomplete_first_arc_turn == ctx.progress["next_turn"] and (
            ctx.incomplete_first_arc_accepted or resume):
        kwargs["incomplete_first_arc_turn"] = ctx.incomplete_first_arc_turn
    report, code = run_larc_race_route(
        run_root=ctx.root, output_dir=race_root, adb_port=ctx.port, max_steps=24,
        safety_analyzer=getattr(ctx, "screen_reader", None), **kwargs)
    reason = str(report.get("stop_reason", "race_stopped"))
    race_steps = report.get("step_reports") or []
    entered_here = not previously_entered and _fixed_race_entry_confirmed(race_steps)
    first_text = str(race_steps[0].get("result", {}).get("frame_safety", {}).get(
        "visible_text", "")) if race_steps else ""
    # The representative exchange is inserted after the last December action;
    # unlike a dated race day, it does not consume another calendar slot.
    automatic_exchange = "".join(first_text.split()).startswith("凯旋门代表交流赛")
    calendar_consumed = entered_here and not automatic_exchange
    from scripts.larc_race_transport import race_timing_summary
    summary["race_reports"].append({"path": str(race_root / "report.json"), "code": code,
                                    "reason": reason, "entry_confirmed_here": entered_here,
                                    "calendar_turn_consumed": calendar_consumed,'race_turn':ctx.progress['next_turn'],
                                    'cut_debut_race':report.get('cut_debut_race',False),
                                    'clock_retries_used':report.get('clock_retries_used',0),
                                    'clock_items_committed':report.get('clock_items_committed',0),
                                    'clock_free_retries':report.get('clock_free_retries',0),
                                    'timing':race_timing_summary(report),
                                    'fan_shortfall_returned_to_home':report.get('fan_shortfall_returned_to_home')})
    _adopt_wait_stops(summary, report.get('technical_wait_stops', []), race_root / 'report.json')
    _record_race_executor_error(summary, report, code)
    if report.get("first_win_rescue"):
        summary["race_reports"][-1]["first_win_rescue"] = True
    if calendar_consumed:
        summary["fixed_races_consumed_this_run"] += 1
        summary["consumed_turns_this_run"] += 1
    save_summary()
    if code or report.get("is_valid") is not True:
        return False, reason
    handoff_path = race_root / "owner_handoff.json"
    if report.get("owner_handoff_path") != str(handoff_path) or not handoff_path.is_file():
        return False, "race_owner_handoff_unverified"
    handoff = _read(handoff_path)
    if (handoff.get("before", {}).get("run_session_id") != ctx.run_id
            or handoff.get("report_sha256") != _canonical_sha256(report)
            or handoff.get("after") != RouteTransitionSession.load(
                ctx.route, expected_run_session_id=ctx.run_id).to_dict()):
        return False, "race_owner_handoff_unverified"
    ctx.incomplete_first_arc_accepted = False
    return True, reason


def _observe_events(ctx, report, path, summary, *, post_action):
    continuation = getattr(ctx, 'continuation_runtime', None)
    if continuation is not None:
        continuation.observe_report(report)
    hook = getattr(ctx, 'cut_farming_hook', None)
    if hook is not None:
        hook.observe_event(report)
    # Consumption has already incremented next_turn on the after-action path.
    match = re.search(r"(?:^|[\\/])turn_(\d+)(?:[_\\/]|$)", str(path))
    action_turn = int(match[1]) if match else max(0, ctx.progress["next_turn"] - int(post_action))
    result = observe_event_report(ctx, report, path, action_turn, "after" if post_action else "before")
    if result.get("note"):
        summary.setdefault("event_progress_notes", []).append(result["note"])
    if result.get("new_observations"):
        summary["event_observations_recorded"] = summary.get("event_observations_recorded", 0) + result["new_observations"]
    return result


def _make_transition_probe(ctx):
    from core.cut_portable_runtime import portable_root, device_ports
    from scripts.larc_transition_probe import CareerTransitionProbe
    if portable_root() is not None:
        if ctx.port not in device_ports():
            raise ValueError("portable_transition_device_mismatch")
        from core.adb import ADB
        # The portable ADB owns native LD/MuMu selection and same-device
        # fallback. UI readiness does not depend on the local MuMu port map.
        # Keep passive title sampling unchanged in this portable speed fix.
        return CareerTransitionProbe(ADB(port=ctx.port))
    from core.native_capture_adb import native_instance_id
    if native_instance_id(ctx.port) is None:
        return None
    from core.native_capture_adb import NativeCaptureADB
    from scripts.larc_passive_events import PassiveEventJournal
    ctx.passive_events = PassiveEventJournal(ctx.root)
    return CareerTransitionProbe(NativeCaptureADB(port=ctx.port), journal=ctx.passive_events,
        cut_sampling=_cut_screening(ctx))


def _drain_passive_events(ctx, summary, *, settle_cut=False):
    journal = getattr(ctx, "passive_events", None)
    if journal is None:
        return
    hook=getattr(ctx,'cut_farming_hook',None)
    def fault(key):
        if hook is not None and callable(getattr(hook,'sampling_fault',None)):hook.sampling_fault(key)
    try:
        if settle_cut and hook is not None and not journal.settle(timeout=1.0):
            # Only already-captured small titles; never wait for perfect OCR.
            fault(('pending_titles',journal.sequence))
            summary['passive_event_pending_at_cut_check']=len(journal.pending)
        rows=journal.drain(include_health=True) if hook is not None else journal.drain()
        for report, path, turn in rows:
            if hook is not None:
                hook.observe_event(report)
            if report.get('status') not in (None,'observed'):
                continue # Cut health/duplicates do not alter ordinary event progress.
            note = observe_event_report(ctx, report, path, turn, "passive_title")
            summary["passive_event_observations"] = summary.get("passive_event_observations", 0) + note.get("new_observations", 0)
        summary["passive_event_samples_dropped"] = journal.dropped
        if hook is not None and (journal.dropped or journal.last_error):
            fault(('journal_health',journal.dropped,journal.last_error))
        if journal.last_error:
            summary["passive_event_last_error"] = journal.last_error
    except Exception as exc:
        fault(('passive_drain',type(exc).__name__))
        summary.setdefault("event_progress_notes", []).append(f"passive_events:{type(exc).__name__}")


def _record_career_outcome(ctx, report, report_path, summary):
    """Reuse existing page evidence for reporting; never authorizes an input."""
    path = ctx.root / "session/larc_game_outcome.json"
    outcome = _read(path) if path.is_file() else {
        "run_session_id": ctx.run_id, "goal_failure_observed": False, "all_goals_verified": False}
    if outcome.get("run_session_id") != ctx.run_id:
        raise ValueError("career_outcome_run_mismatch")
    changed = False
    for row in report.get("step_reports", []):
        result = row.get("result", {})
        evidence = result.get("decision", {}).get("evidence", {})
        if evidence.get("career_outcome", {}).get("goal_failed") is True:
            outcome.update(goal_failure_observed=True, failure_report=str(report_path))
            changed = True
        confirmation = result.get("career_all_goals_confirmation") or result.get("all_goals_confirmation") or {}
        if confirmation.get("status") in {"verified", "builtin_verified", "game_ui_verified"}:
            outcome.update(all_goals_verified=True, all_goals_report=str(report_path))
            changed = True
    if changed:
        _atomic_write_json(path, outcome)
    summary.update(goal_failure_observed=outcome["goal_failure_observed"],
        all_goals_verified=outcome["all_goals_verified"],
        career_success=outcome["all_goals_verified"] and not outcome["goal_failure_observed"])


def _handoff_ready_home(ctx, frame, root):
    """Observe a ready home without dispatching the generic action planner.

    Read only the goal/expectation area; numerical state is read by the caller
    from this same saved frame. This observation never authorizes a tap.
    """
    from core.auto_trainer.speed_toggle import SpeedTogglePlanner
    from core.auto_trainer.career_goal_perception import _GOAL_REGION_READER
    from core.auto_trainer.frame_safety import _join_high_confidence_rapidocr_text
    from core.auto_trainer.larc_home_text import read_ready_home_text
    from scripts.larc_transition_probe import normalized_frame
    from core.auto_trainer.safe_advance import SafeAdvanceExecutor

    image = normalized_frame(frame)
    if (image is None or not SafeAdvanceExecutor._career_home_controls_ready(image)
            or SpeedTogglePlanner._bottom_skip_level(image) != "double"):
        return None
    if connecting_overlay_visible(image):
        return None
    skip = RunSkipSession.load(ctx.skip, expected_run_session_id=ctx.run_id)
    if skip.to_dict()["state"] == "conflict":
        return None
    try:
        text = read_ready_home_text(image, reader=_GOAL_REGION_READER.read_line)
        text_reader = "home_goal_fixed_lines" if text is not None else "home_goal_roi"
        if text is None:
            text = _join_high_confidence_rapidocr_text(_GOAL_REGION_READER.read_regions(image[:480]))
    except Exception:
        return None  # Existing reader remains the fallback, not a new retry loop.
    if not text.strip():
        return None
    if "连接中" in text or "CONNECTING" in text.upper():
        return None
    path = root / "home.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    ctx.transition_probe.adb.save_frame(str(path), image)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    route = RouteTransitionSession.load(ctx.route, expected_run_session_id=ctx.run_id)
    route.observe("main_menu", source_sha256=digest, resync=True)
    route.save(ctx.route)
    skip.observe("double", source_sha256=digest)
    skip.save(ctx.skip)
    ctx.menu_observation = {"screenshot_path": str(path), "result": {
        "performed": False, "reason": "ready_main_menu",
        "frame_safety": {"source_image_sha256": digest},
        "decision": {"action": "stop", "reason": "ready_main_menu", "evidence": {
            "screen_text": {"normalized_text": text}, "reader": text_reader}}}}
    return str(path)


def _settle_finished_career(ctx, output, summary, save_summary):
    probe = getattr(ctx, "transition_probe", None)
    if probe is not None:
        probe.close()
        ctx.transition_probe = None
    settlement, code = run_larc_terminal_settlement(run_root=ctx.root,
        output_dir=output / "settlement", d1_root=ctx.d1_skill_root, adb_port=ctx.port)
    summary["terminal_settlement"] = settlement
    _adopt_wait_stops(summary, settlement.get('technical_wait_stops', []),
        settlement.get('report_path', output / 'settlement/report.json'))
    _record_career_outcome(ctx, {}, Path(settlement.get("report_path", output / "settlement/report.json")), summary)
    if code:
        save_summary()
        return None, "terminal_settlement_stopped:" + str(settlement.get("reason"))
    summary.update(status="completed", career_complete=True, stop_reason="normal_settlement_complete",
        completion_scope="normal_settlement")
    save_summary()
    return None, "normal_settlement_complete"


def _current_free_race_preview(ctx, scene):
    """A current same-run free preview may wake its existing race owner."""
    if (not isinstance(scene, dict) or scene.get('owner') != 'race'
            or scene.get('state') != 'race_scene' or scene.get('entered') is not True
            or scene.get('race_reason') != 'view_race_result'
            or scene.get('run_session_id', ctx.run_id) != ctx.run_id
            or scene.get('turn_index', ctx.progress['next_turn']) != ctx.progress['next_turn']):
        return False
    try:
        source = Path(scene.get('source', '')).resolve(strict=True)
        return (source.is_file() and source.is_relative_to(ctx.root.resolve())
            and hashlib.sha256(source.read_bytes()).hexdigest() == scene.get('source_sha256'))
    except (OSError, ValueError, TypeError):
        return False


def _current_owned_result_overlay(ctx, result):
    """Recognize a current free result page; this never proves a race outcome."""
    packet = result.get('frame_safety', {})
    decision = result.get('decision', {})
    if (packet.get('dangerous') or packet.get('resource_terms')
            or decision.get('action') not in {'stop', 'wait'}
            or decision.get('tap') or decision.get('gesture')
            or any(result.get(key) for key in ('tap_performed', 'input_may_have_occurred', 'input_receipts'))):
        return False
    try:
        source = Path(decision.get('evidence', {}).get('image', '')).resolve(strict=True)
        if (not source.is_file() or not source.is_relative_to(ctx.root.resolve())
                or hashlib.sha256(source.read_bytes()).hexdigest() != packet.get('source_image_sha256')):
            return False
        import cv2
        from core.auto_trainer.race_route import RaceRoutePlanner
        # The rank and Pt captions are optional page metadata. Only the
        # foreground layout wakes the already entered race's normal reader.
        return RaceRoutePlanner._larc_rank_points_overlay(
            cv2.imread(str(source)), packet.get('visible_text', '')) is not None
    except (OSError, ValueError, TypeError):
        return False


def _career_transition_budget():
    from core.auto_trainer.thresholds import UNKNOWN_TRANSITION_IDLE_SECONDS, UNKNOWN_TRANSITION_TOTAL_SECONDS
    return TransitionBudget(clock=lambda: monotonic(), idle_seconds=UNKNOWN_TRANSITION_IDLE_SECONDS,
                            unknown_total_seconds=UNKNOWN_TRANSITION_TOTAL_SECONDS)


def _route_to_menu(ctx, output, summary, save_summary, *, observe_only=False, post_action=False):
    from core.auto_trainer.frame_safety import FrameSafetyAnalyzer
    if not observe_only and _terminal_finish_verified(ctx):
        # A confirmed finish hands off to the result-only owner, including
        # its first call before a settlement checkpoint has been created.
        return _settle_finished_career(ctx, output, summary, save_summary)
    if getattr(ctx, "screen_reader", None) is None:
        ctx.screen_reader = FrameSafetyAnalyzer()
    last_event = None
    resync_main_menu = False
    probe = getattr(ctx, "transition_probe", None) if not observe_only else None
    wait_budget = _career_transition_budget()
    last_unknown_read = None
    preview_handoff_seen = False
    skip_stall_nudges = 0
    last_skip_nudge_at = None
    race_list_return_attempts = 0
    for index in range(240 if probe is not None else 60):
        root = output / f"route_{index:02d}"
        entry_route = RouteTransitionSession.load(ctx.route, expected_run_session_id=ctx.run_id)
        bootstrap = entry_route.current_state is None
        initial_frame = None
        progress_frame = None
        direct_event = False
        preview_wakeup = False
        if probe is not None:
            skip_enabled = RunSkipSession.load(ctx.skip, expected_run_session_id=ctx.run_id).to_dict()["state"] == "enabled"
            observation = probe.observe(action_turn=max(0, ctx.progress["next_turn"] - int(post_action)),
                                        skip_enabled=skip_enabled)
            initial_frame = observation.pop("frame", None)
            progress_frame = observation.pop("progress_frame", None)
            summary.setdefault("transition_probes", []).append(observation)
            if not observation.get('surface') and progress_frame is not None:
                from core.auto_trainer.race_route import RaceRoutePlanner
                controls = RaceRoutePlanner._race_preview_action_points(progress_frame)
                if controls is not None and controls.get('view_result') is not None and not controls.get('view_result_locked'):
                    # Readiness only. Give this already captured candidate to
                    # the normal reader before an old unknown-page deadline;
                    # no button or race ownership is authorized by this hint.
                    initial_frame = progress_frame
                    preview_wakeup = True
                    observation['race_preview_wakeup'] = 'free_view_result_control_candidate'
            if observation.get('cut_sampling_error') and callable(getattr(getattr(ctx,'cut_farming_hook',None),'sampling_fault',None)):
                ctx.cut_farming_hook.sampling_fault(('capture',ctx.progress['next_turn'],observation['cut_sampling_error']))
            _drain_passive_events(ctx, summary)
            if wait_budget.started is not None:
                wait_budget.observe(image=initial_frame if initial_frame is not None else progress_frame,
                    key=observation.get("surface"),
                    progressed=bool(observation.get("progressed") and observation.get("surface")))
            if observation.get("event_selection_pending") and not observation.get("surface"):
                wait_budget.observe(key="event_selection_pending")
                if wait_budget.expired:
                    _record_wait_stop(ctx, summary, wait_budget, 'event_selection_transition_timeout',
                        root / 'report.json', surface='event_selection_pending')
                    save_summary()
                    return None, "event_selection_transition_timeout"
                continue  # Do not let the generic fallback tap the unchanged options.
            if not observation.get("surface") and not observation.get("fallback") and not preview_wakeup and wait_budget.started is not None:
                now = monotonic()
                if wait_budget.expired:
                    _record_wait_stop(ctx, summary, wait_budget, 'career_transition_no_progress_timeout',
                        root / 'report.json', surface=observation.get('surface'))
                    save_summary()
                    return None, "career_transition_no_progress_timeout"
                if last_unknown_read is not None and now - last_unknown_read < 6:
                    # Keep polling controls at native-frame speed. Only the
                    # expensive fallback reader is throttled, never a ready UI.
                    observation["full_reader_deferred"] = True
                    continue
            if observation.get("surface") == "main_menu":
                home_started = monotonic()
                source = _handoff_ready_home(ctx, initial_frame, root)
                if source is not None:
                    summary["route_reports"].append({"path": str(root / "report.json"),
                        "reason": "ready_main_menu", "code": 0,
                        "handoff": ctx.menu_observation["result"]["decision"]["evidence"]["reader"],
                        "reader_seconds": monotonic() - home_started})
                    _atomic_write_json(root / "report.json", {"is_valid": True,
                        "stop_reason": "ready_main_menu", "source_screenshot_path": source,
                        "step_reports": [ctx.menu_observation]})
                    save_summary()
                    return source, "ready_main_menu"
                route = RouteTransitionSession.load(ctx.route, expected_run_session_id=ctx.run_id)
                if not bootstrap and (route.current_state != "main_menu" or route.expected_next_states):
                    resync_main_menu = True
            direct_event = (observation.get("surface") == "event_choice" and initial_frame is not None
                and entry_route.status != "mismatch"
                and ("event_choice" in entry_route.expected_next_states
                     or not entry_route.expected_next_states and entry_route.current_state in {None,"event_choice"}))
            if observation.get("surface") == "event_choice" and initial_frame is not None and not direct_event:
                # The native probe recognizes controls, but does not persist a
                # page handoff. Let the ordinary current-page reader establish
                # it before the event executor's preflight; never bypass that
                # preflight or replay the preceding training input.
                observation['event_handoff']='route_observation_required'
        argv = ["--mode", "live", "--scenario", "larc", "--max-steps", "1" if observe_only or resync_main_menu or probe is not None else "6",
                "--adb-port", str(ctx.port), "--run-session-id", ctx.run_id,
                "--transition-session-json", str(ctx.route), "--skip-session-json", str(ctx.skip),
                "--career-goal-ledger-json", str(ctx.ledger),
                "--career-goal-observed-turn", str(ctx.progress["next_turn"]),
                "--screenshot-dir", str(root), "--allow-return-main-menu-from-training"]
        if getattr(ctx, 'live_takeover', False):
            live_scope = 'live-career-opening' if (ctx.progress['next_turn']==1
                and getattr(ctx,'ordinary_career_opening',False)) else 'live-career'
            argv += ["--transition-resync", "--route-bootstrap-state", live_scope]
            if not observe_only:
                argv += ["--execute", "--confirm-token", "UG_PARENT_SAFE_ADVANCE_SANDBOX"]
        elif resync_main_menu:
            argv += ["--transition-resync", "--route-bootstrap-state", "main-menu"]
            resync_main_menu = False
        elif not observe_only:
            argv += ["--execute", "--confirm-token", "UG_PARENT_SAFE_ADVANCE_SANDBOX"]
        opening_bootstrap=(ctx.progress['next_turn']==1 and (
            getattr(ctx,'cut_farming_hook',None) is not None or getattr(ctx,'ordinary_career_opening',False)))
        if (bootstrap or opening_bootstrap) and '--transition-resync' not in argv:
            argv += ["--route-bootstrap-state", "career-opening" if opening_bootstrap else "main-menu"]
        route_args = advance_args(argv)
        route_args._safety_analyzer = ctx.screen_reader
        if probe is not None:
            route_args._capture_adb = probe.adb
        if initial_frame is not None:
            route_args._initial_frame = initial_frame
        reader_started = monotonic()
        if direct_event:
            # No input and no general OCR between probe and event executor.
            report, code = {"is_valid": True, "stop_reason": "event_choice", "step_reports": []}, 0
        else:
            report, code = advance(route_args)
        observed_at = monotonic()
        reader_seconds = monotonic() - reader_started
        _atomic_write_json(root / "report.json", report)
        _record_career_outcome(ctx, report, root / "report.json", summary)
        _observe_events(ctx, report, root / "report.json", summary, post_action=post_action)
        observe_mei_unlock(ctx, report)
        if getattr(ctx, 'cut_farming_hook', None) is None:
            from core.auto_trainer.larc_mei_unlock_policy import confound_mei_gamble
            confound_mei_gamble(report, ctx.root, ctx.run_id)
        reason = str(report.get("stop_reason", "route_stopped"))
        if reason == "no_progress_timeout":
            reason = "career_transition_no_progress_timeout"
            _step_res = ((report.get("step_reports") or [{}])[-1].get("result") or {})
            _gentle_dec = (_step_res.get("decision") or {})
            _record_wait_stop(
                ctx,
                summary,
                wait_budget,
                reason,
                root / "report.json",
                surface=_gentle_dec.get("route_state") or _step_res.get("screen"),
                gentle_transition_wait=(_gentle_dec.get("evidence") or {}).get("gentle_transition_wait"),
            )
        summary["route_reports"].append({"path": str(root / "report.json"), "reason": reason, "code": code,
                                         "reader_seconds": reader_seconds})
        save_summary()
        rows = report.get("step_reports", [])
        last_result = rows[-1].get("result", {}) if rows else {}
        # A fresh Start intentionally archives old navigation intentions. The
        # current title tap, not a historical date-notice checkpoint, owns this
        # cached-account -> existing-career tail (never a new career).
        if (last_result.get("performed") is True
                and last_result.get("decision", {}).get("reason") == "tap_title_start"):
            from scripts.larc_same_account_reconnect import reconnect_existing_career
            recovery = reconnect_existing_career(ctx, output / f"date_change_resume_{index:02d}",
                title_start_submitted=True, require_existing_career=True)
            summary.setdefault("date_change_recoveries", []).append(recovery)
            save_summary()
            if not recovery.get("ok"):
                return None, recovery.get("reason", "date_change_resume_failed")
            old_skip = RunSkipSession.load(ctx.skip, expected_run_session_id=ctx.run_id)
            refreshed_skip = RunSkipSession.new(ctx.run_id)
            refreshed_skip.lifetime_toggle_tap_count = old_skip.lifetime_toggle_tap_count
            refreshed_skip.save(ctx.skip)
            post_action = True
            wait_budget.reset()
            continue
        if reason == "connection_conflict" and getattr(ctx,"same_account_reconnect",False):
            if ctx.reconnect_attempted:
                return None, "reconnect_repeated_account_conflict"
            ctx.reconnect_attempted = True
            from scripts.larc_same_account_reconnect import reconnect_existing_career
            recovery = reconnect_existing_career(ctx, output / f"reconnect_{index:02d}")
            summary.setdefault("connection_recoveries", []).append(recovery)
            save_summary()
            if not recovery.get("ok"):
                return None, recovery.get("reason", "reconnect_failed")
            # A new client login may reset the UI skip option. Its fresh page,
            # not an old enabled bit, drives setup; game progress is untouched.
            old_skip = RunSkipSession.load(ctx.skip, expected_run_session_id=ctx.run_id)
            refreshed_skip = RunSkipSession.new(ctx.run_id)
            refreshed_skip.lifetime_toggle_tap_count = old_skip.lifetime_toggle_tap_count
            refreshed_skip.save(ctx.skip)
            post_action = True
            wait_budget.reset()
            continue
        rows = report.get("step_reports", [])
        last_result = rows[-1].get("result", {}) if rows else {}
        from core.auto_trainer.safe_advance import SafeAdvancePlanner
        observed_frame = initial_frame if initial_frame is not None else progress_frame
        visible_text = str(last_result.get("decision", {}).get("evidence", {}).get(
            "screen_text", {}).get("normalized_text", ""))
        if observed_frame is None and transient_observation(last_result, reason=reason, allow_home_guess=post_action):
            import cv2
            source = last_result.get("decision", {}).get("evidence", {}).get("image")
            if source and Path(source).is_file():
                observed_frame = cv2.imread(str(source))
        has_frame = getattr(observed_frame, "shape", None) is not None
        home_guess = (post_action and (has_frame or "友情训练" in visible_text)
                      and not (has_frame and SafeAdvancePlanner.is_post_career_global_home(observed_frame)))
        if (code or report.get("is_valid") is not True) and not transient_observation(
                last_result, reason=reason, allow_home_guess=home_guess):
            return None, reason
        if any(row.get("result", {}).get("tap_performed") or (
                row.get("result", {}).get("performed")
                and row.get("result", {}).get("decision", {}).get("action") == "tap") for row in rows):
            wait_budget.reset()
            last_unknown_read = None
            preview_handoff_seen = False
            skip_stall_nudges = 0
            last_skip_nudge_at = None
        if reason == "terminal_rating_verified":
            rows = report.get("step_reports", [])
            evidence = rows[-1].get("result", {}).get("decision", {}).get("evidence", {}) if rows else {}
            if (evidence.get("terminal_rating", {}).get("verified") is True
                    and evidence["terminal_rating"].get("completion_scope") == "training_rating"
                    and _terminal_finish_verified(ctx)):
                settlement, settlement_code = run_larc_terminal_settlement(run_root=ctx.root,
                    output_dir=output / "settlement", d1_root=ctx.d1_skill_root, adb_port=ctx.port)
                summary["terminal_settlement"] = settlement
                _adopt_wait_stops(summary, settlement.get('technical_wait_stops', []),
                    settlement.get('report_path', root / 'settlement/report.json'))
                save_summary()
                if settlement_code:
                    return None, "terminal_settlement_stopped:" + str(settlement.get("reason"))
                summary.update(status="completed", career_complete=True, stop_reason=reason,
                               completion_scope="normal_settlement", terminal_rating_report=str(root / "report.json"))
                save_summary()
                return None, reason
            return None, "terminal_rating_without_finish_receipt"
        if reason == "ready_main_menu":
            # The generic reader can also see the home silhouette during a
            # training result or the remaining-turn bounce. Reuse the shared
            # UI-only readiness check before handing that frame to the provider.
            # This observation never taps, parses rewards, or reconciles state.
            import cv2
            from core.auto_trainer.safe_advance import SafeAdvanceExecutor
            menu_source = report.get("source_screenshot_path")
            menu_frame = cv2.imread(str(menu_source)) if menu_source and Path(menu_source).is_file() else None
            if menu_frame is not None and not SafeAdvanceExecutor._career_home_controls_ready(menu_frame):
                summary["route_reports"][-1]["main_menu_handoff_pending"] = True
                wait_budget.observe(image=menu_frame, key="main_menu_transition")
                save_summary()
                if wait_budget.expired:
                    _record_wait_stop(ctx, summary, wait_budget, 'career_transition_no_progress_timeout',
                        root / 'report.json', surface='main_menu_transition')
                    save_summary()
                    return None, "career_transition_no_progress_timeout"
                sleep(.15)
                continue
            rows = report.get("step_reports", [])
            ctx.menu_observation = rows[-1] if rows else {}
            return report.get("source_screenshot_path"), reason
        if observe_only and not transient_observation(
                last_result, reason=reason, allow_home_guess=home_guess):
            return None, reason
        rows = report.get("step_reports", [])
        decision = rows[-1].get("result", {}).get("decision", {}) if rows else {}
        if (reason == "route_state_out_of_scope"
                and is_route_handoff(decision, reason="ready_main_menu")
                and decision.get("route_state") == "main_menu"
                and last_result.get("performed") is False
                and not last_result.get("tap_performed")
                and not last_result.get("input_receipts")
                and RouteTransitionSession.load(ctx.route, expected_run_session_id=ctx.run_id).can_resume_skipped_event_to_home()):
            # Older sessions dropped a training/rest transition at an apparent
            # event that auto-skip removed. The verified home only needs a
            # non-executing resync, never another event or training input.
            resync_main_menu = True
            summary["route_reports"][-1]["skipped_event_home_reobserve"] = True
            continue
        if (reason == "route_state_out_of_scope"
                and is_route_handoff(decision, reason="race_day_main_menu")
                and decision.get("evidence", {}).get("larc_race_day", {}).get("verified") is True
                and not rows[-1].get("result", {}).get("tap_performed")):
            # A skipped story may have been provisionally classified as an
            # event. The verified race home is a new observation, not a tap.
            resync_main_menu = True
            summary["route_reports"][-1]["race_home_reobserve"] = True
            continue
        current_preview = False
        if (ctx.allow_fixed_races and (reason in {"max_steps_reached", "common_career_page_unrecognized"}
                or reason == "route_state_out_of_scope"
                and decision.get("reason") == "common_career_page_unrecognized")):
            if (not observe_only and getattr(ctx, 'ordinary_career_opening', False)
                    and getattr(ctx, 'cut_farming_hook', None) is None
                    and (last_result.get('performed') is False or decision.get('action') == 'wait')
                    and not any(row.get('result', {}).get(key) for row in rows for key in
                        ('tap_performed', 'input_may_have_occurred', 'input_receipts'))
                    and not any(row.get('result', {}).get('decision', {}).get(key)
                        for row in rows for key in ('tap', 'gesture'))
                    and not any(row.get('result', {}).get('performed') is True
                        and row.get('result', {}).get('decision', {}).get('action') != 'wait' for row in rows)
                    and not any(row.get('result', {}).get(key) not in (None, False, 'not_attempted', 'not_performed')
                        for row in rows for key in ('tap_outcome', 'input_outcome', 'outcome'))):
                # A free modal may uncover a current race result after a cold
                # attachment. Establish that owner from the current page only.
                from scripts.larc_live_takeover import observe_current
                scene = observe_current(ctx, output / f'race_result_observe_{index:02d}',
                    adb=probe.adb if probe is not None else None, persist_route=False)
                current_preview = _current_free_race_preview(ctx, scene)
                if current_preview and preview_handoff_seen:
                    # No intervening common input established a new phase.
                    # Preserve the current case instead of cold-attaching it
                    # over and over when the owner has not resolved this page.
                    return None, 'race_preview_handoff_unresolved'
                preview_without_checkpoint = (current_preview
                    and not (ctx.root / 'session/larc_race_checkpoint.json').exists())
                if (scene.get('owner') == 'race' and scene.get('entered') is True
                        and (scene.get('race_reason') in {'open_lost_race_retry', 'retry_lost_race',
                            'cancel_clock_retry', 'clock_retry_outcome_pending'}
                            or scene.get('current_result_proof', {}).get('kind') == 'larc_rank_points_overlay'
                            or preview_without_checkpoint)):
                    # A speculative read must not erase the race owner's valid
                    # dialogue/modal successors when its generic race guess is
                    # rejected. Adopt only the current result proof used below.
                    route = RouteTransitionSession.load(ctx.route, expected_run_session_id=ctx.run_id)
                    route.observe(scene['state'], source_sha256=scene['source_sha256'], resync=True)
                    route.save(ctx.route)
                    summary['route_reports'][-1]['current_result_handoff'] = scene
                    if preview_without_checkpoint:
                        preview_handoff_seen = True
                    save_summary()
                    ready, reason = _run_fixed_race(ctx, output / f'race_result_{index:02d}',
                        summary, save_summary, live_source=scene)
                    if not ready:
                        return None, reason
                    post_action = True
                    wait_budget.reset()
                    last_unknown_read = None
                    skip_stall_nudges = 0
                    last_skip_nudge_at = None
                    continue
            visible = "".join(str(decision.get("evidence", {}).get("screen_text", {}).get(
                "normalized_text", "")).split())
            checkpoint_path = ctx.root / "session/larc_race_checkpoint.json"
            checkpoint = _read(checkpoint_path) if checkpoint_path.is_file() else {}
            result_surface = (checkpoint.get("session", {}).get("result_transition_pending") is True
                              and ("第" in visible and "名" in visible
                                   or _current_owned_result_overlay(ctx, last_result)))
            if (("查看结果" in visible and "前往赛事" in visible or result_surface)
                    and checkpoint.get("run_session_id") == ctx.run_id
                    and checkpoint.get("turn_index") == ctx.progress["next_turn"]
                    and checkpoint.get("status") == "handed_off"
                    and checkpoint.get("stop_reason") == "post_race_dialogue"
                    and _fixed_race_entry_confirmed(checkpoint.get("steps") or [])):
                if current_preview:
                    preview_handoff_seen = True
                ready, reason = _run_fixed_race(ctx, output / f"race_resume_{index:02d}",
                    summary, save_summary, resume=True)
                if not ready:
                    return None, reason
                post_action = True
                # The race owner made progress; its execution time must not
                # exhaust the old common-route unknown-page timer.
                wait_budget.reset()
                last_unknown_read = None
                skip_stall_nudges = 0
                last_skip_nudge_at = None
                continue
        if transient_observation(last_result, reason=reason, allow_home_guess=home_guess):
            # Unknown animation is not a new actionable page. Keep the existing
            # action receipt and let fresh frames establish where it landed.
            wait_key = "training_action_animation" if decision.get("reason") == "training_action_animation_wait" else decision.get("route_state")
            wait_budget.observe(image=observed_frame, key=wait_key,
                progress_token=last_result.get("acknowledged_inputs"))
            last_unknown_read = monotonic()
            from core.auto_trainer.event_options import event_option_rows_ready
            snap = wait_budget.snapshot()
            nudge_now = monotonic()
            nudge_text = "".join(str((((decision.get("evidence") or {}).get("screen_text") or {}).get("normalized_text") or "")).split())
            if (not observe_only
                    and probe is not None
                    and has_frame
                    and decision.get("reason") == "bottom_skip_double_enabled"
                    and snap["idle_elapsed_seconds"] >= 6.0
                    and skip_stall_nudges < 3
                    and (last_skip_nudge_at is None or nudge_now - last_skip_nudge_at >= 4.0)
                    and not last_result.get("frame_safety", {}).get("dangerous")
                    and not decision.get("evidence", {}).get("screen_text", {}).get("dangerous")
                    and not any(marker in nudge_text for marker in ("休息", "海外适应性", "能力详情", "参赛条件", "距离代表交流赛还剩"))
                    and not SafeAdvancePlanner._looks_like_confirm_modal_panel(observed_frame)
                    and not event_option_rows_ready(observed_frame)
                    and not SafeAdvancePlanner._looks_like_training_home_controls(observed_frame)
                    and SafeAdvancePlanner._looks_like_large_bottom_dialogue(observed_frame)):
                nudge_frame = observed_frame
                if callable(getattr(probe.adb, "read_frame", None)):
                    try:
                        fresh = probe.adb.read_frame(deadline=monotonic() + 0.25)
                        if getattr(fresh, "ndim", None) == 3 and getattr(fresh, "size", 0):
                            nudge_frame = fresh
                    except Exception:
                        pass
                still_dialogue = True
                if nudge_frame is not observed_frame:
                    from core.auto_trainer.safe_advance import SafeAdvanceExecutor
                    still_dialogue = (
                        not SafeAdvancePlanner._looks_like_confirm_modal_panel(nudge_frame)
                        and not event_option_rows_ready(nudge_frame)
                        and not SafeAdvancePlanner._looks_like_training_home_controls(nudge_frame)
                        and not SafeAdvanceExecutor._career_home_controls_ready(nudge_frame)
                        and SafeAdvancePlanner._looks_like_large_bottom_dialogue(nudge_frame)
                    )
                if still_dialogue:
                    h, w = nudge_frame.shape[:2]
                    probe.adb.tap(int(w * 0.14), int(h * 0.45), scaled=False)
                    skip_stall_nudges += 1
                    last_skip_nudge_at = monotonic()
                    last_unknown_read = None
                    summary["route_reports"][-1]["skip_stall_dialogue_nudge"] = skip_stall_nudges
                    wait_budget.observe(image=nudge_frame, key=wait_key, progressed=True)
                    sleep(.35)
                    continue
            if wait_budget.expired:
                _record_wait_stop(ctx, summary, wait_budget, 'career_transition_no_progress_timeout',
                    root / 'report.json', surface=decision.get('route_state'))
                save_summary()
                return None, "career_transition_no_progress_timeout"
            summary["route_reports"][-1]["transient_wait"] = True
            sleep(.15)
            continue
        last_unknown_read = None
        if reason == "larc_adaptation_session_required" and ctx.allow_adaptations:
            unfinished, ignored = _adaptation_resume_candidates(ctx)
            summary["adaptation_resume_context"] = {
                "candidate_count": len(unfinished),
                "candidate_phases": [session.phase for session in unfinished],
                "ignored_transaction_free_or_returned": ignored,
                "turn_index": getattr(ctx, "progress", {}).get("next_turn")}
            save_summary()
            if not unfinished and not observe_only:
                returned = _return_unowned_adaptation_grid(ctx, root / "unowned_grid")
                summary["adaptation_resume_context"].update(
                    inspection_only_return=returned.get("ok") is True,
                    return_reason=returned.get("reason"))
                save_summary()
                if returned.get("ok") is True:
                    resync_main_menu = True
                    wait_budget.reset()
                    continue
                return None, str(returned.get("reason") or "larc_adaptation_resume_requires_unique_session")
            if len(unfinished) != 1:
                summary["error_type"] = "AdaptationResumeContext"
                summary["error"] = "larc_adaptation_resume_requires_unique_session:count=" + str(len(unfinished))
                save_summary()
                return None, "larc_adaptation_resume_requires_unique_session"
            session = unfinished[0]
            resumable_selection = _resumable_adaptation_selection(session) and session.plan is not None
            resumable_result = (session.stage == "awaiting_result"
                and session.submitted_source_sha256 is not None and session.receipt is None)
            if session.inspect_only or not (resumable_selection or resumable_result):
                return None, "larc_adaptation_resume_submission_not_safe"
            ready, _, reason = _ensure_adaptation(ctx, session.phase,
                output / f"adaptation_{session.phase}_resume", summary, save_summary)
            if not ready:
                return None, reason
            continue
        evidence = decision.get("evidence", {})
        compact_race_text = "".join(str(evidence.get("screen_text", {}).get("normalized_text", "")).split())
        exchange_hub = _is_larc_exchange_hub(evidence)
        race_context = {}  # Never reuse a previous page's identity in AI context.
        if reason == 'race_day_main_menu' and getattr(ctx,'cut_farming_hook',None) is not None:
            rows=report.get('step_reports',[])
            if not rows:
                return None,'cut_race_date_source_missing'
            row=rows[-1];result=row.get('result',{})
            text=result.get('decision',{}).get('evidence',{}).get('screen_text',{}).get('normalized_text','')
            race_context=read_larc_race_day_context(row.get('screenshot_path',''),
                source_sha256=result.get('frame_safety',{}).get('source_image_sha256'),visible_text=text)
            _drain_passive_events(ctx,summary,settle_cut=True)
            cut_report=ctx.cut_farming_hook.before_race(ctx,row.get('screenshot_path',''),
                race_context.get('period',{}),output/f'cut_race_status_{index:02d}')
            summary.setdefault('cut_farming_checks',[]).append(cut_report)
            save_summary()
            if cut_report.get('stop_reason'):
                summary.update(status='cut_farming_checkpoint',stop_reason=cut_report['stop_reason'])
                return None,cut_report['stop_reason']
            if cut_report.get('status')=='probed':
                continue
        if reason == "race_day_main_menu" and summary["consumed_turns_this_run"] >= ctx.batch_limit:
            return None, "batch_limit_reached"
        if reason == 'race_day_main_menu' and not exchange_hub and not _cut_screening(ctx):
            race_context = _manual_race_checkpoint(ctx, race_context, report)
        if (reason == 'race_day_main_menu' and getattr(ctx, 'cut_farming_hook', None) is None
                and _ss_sieve_config(ctx, 'pre_debut_end') is not None):
            sieve_race = race_context
            rows = report.get('step_reports') or []
            if not sieve_race and rows:
                row = rows[-1]
                result = row.get('result', {})
                text = result.get('decision', {}).get('evidence', {}).get('screen_text', {}).get('normalized_text', '')
                sieve_race = read_larc_race_day_context(row.get('screenshot_path', ''),
                    source_sha256=result.get('frame_safety', {}).get('source_image_sha256'), visible_text=text)
            sieve_stop = _ss_sieve_before_debut(ctx, sieve_race, summary)
            if sieve_stop:
                summary.update(status='continuation_checkpoint', stop_reason=sieve_stop)
                save_summary()
                return None, sieve_stop
            if race_context:
                summary['route_reports'][-1]['race_day_context'] = race_context
                save_summary()
        if reason == "race_day_main_menu" and ctx.allow_adaptations:
            if exchange_hub:
                if getattr(ctx, "live_takeover_year1_adapt_recheck", False):
                    if "白银级" not in compact_race_text and "黄金级" not in compact_race_text:
                        _clear_stale_adaptations_for_year1_takeover(
                            ctx, summary, save_summary, phases=("first_exchange",))
                    else:
                        ctx.live_takeover_year1_adapt_recheck = False
                # Keep the normal two-essential preparation. A later exchange
                # may list extra hazards; those are advice, not an entry ban.
                phase = "first_exchange"
                summary.setdefault("race_preparation_advice", []).append({
                    "turn_index": ctx.progress["next_turn"], "race_kind": "larc_exchange",
                    "phase": phase, "perfect_adaptation_required": False,
                    "reason": "普通代表交流赛保留适应性准备，额外未克服难关不阻断参赛。"})
                save_summary()
            else:
                rows = report.get("step_reports", [])
                if not rows:
                    return None, "larc_adaptation_race_day_source_missing"
                row = rows[-1]
                result = row.get("result", {})
                text = result.get("decision", {}).get("evidence", {}).get("screen_text", {}).get("normalized_text", "")
                if not race_context:
                    race_context = read_larc_race_day_context(row.get("screenshot_path", ""),
                        source_sha256=result.get("frame_safety", {}).get("source_image_sha256"), visible_text=text)
                    summary["route_reports"][-1]["race_day_context"] = race_context
                    save_summary()
                period = race_context.get("period", {})
                date = period.get("value", {}) or {}
                ordinary_date = (period.get("status") == "verified"
                    and (date.get("pre_debut") is True or date.get("year_index") == 1
                         or date.get("month") not in {9, 10}))
                if race_context.get("status") != "verified" and not ordinary_date:
                    return None, "larc_adaptation_race_day_context:" + str(race_context.get("reason", "unverified"))
                phase = (None if ordinary_date else select_larc_adaptation_phase(period,
                    race_target_id=race_context["target_id"], race_day=True))
            if phase in {"first_overseas_race", "first_arc"}:
                from core.auto_trainer.larc_policy import overseas_purchase_v3_enabled
                if overseas_purchase_v3_enabled(load_goal_config(ctx.root / "goal.json")):
                    # Decide the explicit language reserve from the existing
                    # current-grid purchase planner before fixed Lv2 prep.
                    growth = _prepare_growth(ctx, race_context["period"], text,
                        output / f"growth_race_{index:02d}", summary, save_summary, race_day=True,
                        goal=load_goal_config(ctx.root / "goal.json"))
                    if growth is not None:
                        if not growth.get("ok"):
                            return None, growth.get("reason", "growth_adaptation_stopped")
                        if growth.get("visited", True):
                            continue
            if phase:
                ready, executed, reason = _ensure_adaptation(ctx, phase, output / f"adaptation_{phase}", summary, save_summary)
                if not ready:
                    return None, reason
                if executed:
                    continue  # Re-read the actual race-day frame after upgrading.
            if not exchange_hub:
                if not _cut_screening(ctx):
                    from core.auto_trainer.larc_manual_handoff import checkpoint as manual_checkpoint
                    manual_checkpoint(ctx, race_context)
                growth = _prepare_growth(ctx, race_context["period"], text,
                    output / f"growth_race_{index:02d}", summary, save_summary, race_day=True)
                if growth is not None:
                    if not growth.get("ok"):
                        return None, growth.get("reason", "growth_adaptation_stopped")
                    if growth.get("visited", True):
                        continue
            reason = "race_day_main_menu"
        if reason == "race_day_main_menu" and ctx.allow_fixed_races:
            if summary["consumed_turns_this_run"] >= ctx.batch_limit:
                return None, "batch_limit_reached"
            from core.auto_trainer.larc_ai_nodes import checkpoint_choice,current_larc_ai
            ai_runtime=current_larc_ai()
            if ai_runtime is not None and ai_runtime.mode!='local':
                ai_check=checkpoint_choice(ctx,'prerace',{
                    'race':race_context,
                    'configured_skill_policy':load_goal_config(ctx.root/'goal.json').skill_timing.arc_policy,
                    'exchange_race':exchange_hub},reason='important_race_preparation')
                if ai_check is not None and not ai_check.get('cached'):
                    summary.setdefault('ai_checkpoints',[]).append(ai_check);save_summary()
            if (not exchange_hub and not _cut_screening(ctx)
                    and load_goal_config(ctx.root / "goal.json").skill_timing.arc_policy.get("enabled")):
                row = (report.get("step_reports") or [{}])[-1]
                result = row.get("result", {})
                race_source = row.get("screenshot_path") or report.get("source_screenshot_path", "")
                skill_race_context = read_larc_race_day_context(race_source,
                    source_sha256=result.get("frame_safety", {}).get("source_image_sha256"),
                    visible_text=result.get("decision", {}).get("evidence", {}).get("screen_text", {}).get("normalized_text", ""))
                from core.auto_trainer.larc_manual_handoff import snapshot as manual_handoff_snapshot
                from core.auto_trainer.larc_manual_handoff import checkpoint as manual_checkpoint
                manual_checkpoint(ctx, skill_race_context)
                race_context = skill_race_context
                manual = manual_handoff_snapshot(ctx.root)
                # Future plan edits cannot rewrite an acknowledged handoff's purpose.
                manual_first_done = (manual.get('acknowledged_intents', {}).get('first_arc_manual_skills') is True
                    and skill_race_context.get('target_id') == 'larc_arc_participation')
                skills = (None if manual_first_done else prepare_arc_skills(ctx, skill_race_context, race_source,
                    output / f"arc_skills_{index:02d}"))
                if skills is not None:
                    summary.setdefault("prerace_skills", []).append(skills)
                    save_summary()
                    if not skills.get("ok"):
                        return None, skills.get("reason", skills.get("status", "arc_skills_stopped"))
            race_root = output / f"race_{index:02d}"
            if not exchange_hub and not _cut_screening(ctx):
                race_context = _manual_race_checkpoint(ctx, race_context, report)
            ready, reason = _run_fixed_race(ctx, race_root, summary, save_summary,
                new_exchange_after_race=exchange_hub)
            if not ready:
                return None, reason
            # The race handoff can first expose a transient loading frame.
            wait_budget.reset()
            post_action = True
            continue
        if reason == "race_list" and ctx.allow_fixed_races:
            from core.auto_trainer.larc_first_win import first_win_required
            if first_win_required(visible_text):
                ready, reason = _run_fixed_race(ctx, output / f"first_win_{index:02d}",
                    summary, save_summary, first_win_rescue=True)
                if not ready:
                    return None, reason
                wait_budget.reset()
                post_action = True
                continue
            if not observe_only and race_list_return_attempts < 2:
                last_source = (
                    (rows[-1].get("screenshot_path") if rows else None)
                    or decision.get("evidence", {}).get("image")
                    or report.get("source_screenshot_path")
                    or ""
                )
                last_sha = str(last_result.get("frame_safety", {}).get("source_image_sha256", "") or "")
                if last_source and re.fullmatch(r"[0-9a-fA-F]{64}", last_sha):
                    race_list_return_attempts += 1
                    route = RouteTransitionSession.load(ctx.route, expected_run_session_id=ctx.run_id)
                    route.observe("race_list", source_sha256=last_sha, resync=True)
                    route.save(ctx.route)
                    ready, reason = _run_fixed_race(
                        ctx,
                        output / f"race_list_return_{index:02d}",
                        summary,
                        save_summary,
                        live_source={
                            "owner": "race",
                            "state": "race_list",
                            "entered": False,
                            "source": str(last_source),
                            "source_sha256": last_sha,
                        },
                    )
                    if not ready:
                        return None, reason
                    wait_budget.reset()
                    post_action = True
                    resync_main_menu = True
                    continue
        if reason == "max_steps_reached":
            continue
        terminal_skill_state = ctx.root / "session/larc_terminal_skills.json"
        terminal_return_handoff = (reason == "skill_acquisition_handoff" and terminal_skill_state.is_file())
        if (reason == "career_end_confirmation" or terminal_return_handoff) and getattr(ctx,"terminal_skill_enabled",ctx.d1_skill_root is not None):
            submitted, reason = _finish_terminal(ctx, output / "terminal", summary, save_summary)
            if submitted:
                return _settle_finished_career(ctx, output / "terminal", summary, save_summary)
            return None, reason
        if reason != "event_choice":
            return None, reason
        rows = report.get("step_reports", [])
        evidence = rows[-1].get("result", {}).get("decision", {}).get("evidence", {}) if rows else {}
        text = evidence.get("screen_text", {}).get("normalized_text")
        if text and text == last_event:
            return None, "repeated_event_screen"
        event_root = output / f"event_{index:02d}"
        choice_args = event_args([
            "--mode", "live", "--max-steps", "1", "--screenshot-dir", str(event_root),
            "--adb-port", str(ctx.port), "--transition-session-json", str(ctx.route),
            "--transition-run-session-id", ctx.run_id, "--allow-choose-event", "--execute",
            "--confirm-token", "UG_PARENT_EVENT_CHOICE_SANDBOX",
        ])
        choice_args._safety_analyzer = ctx.screen_reader
        if direct_event:
            choice_args._capture_adb = probe.adb
            choice_args._initial_frame = initial_frame
        from scripts.larc_event_selection import event_selector
        choice_args._choice_selector = event_selector(ctx)
        if rows:
            previous = rows[-1].get("result", {})
            choice_args._prior_observation = {"observed_at": observed_at,
                "path": evidence.get("image"), "frame_safety": previous.get("frame_safety", {})}
        event_started = monotonic()
        result, code = event(choice_args)
        event_seconds = monotonic() - event_started
        _atomic_write_json(event_root / "report.json", result)
        _observe_events(ctx, result, event_root / "report.json", summary, post_action=post_action)
        observe_mei_unlock(ctx, result)
        if getattr(ctx, 'cut_farming_hook', None) is None:
            from core.auto_trainer.larc_mei_unlock_policy import confound_mei_gamble
            confound_mei_gamble(result, ctx.root, ctx.run_id)
        reason = str(result.get("stop_reason", "event_stopped"))
        summary["route_reports"].append({"path": str(event_root / "report.json"), "reason": reason, "code": code,
                                         "reader_seconds": event_seconds})
        save_summary()
        if (transient_observation(
                (result.get("step_reports") or [{}])[-1].get("result", {}), reason=reason)
                and not result.get("performed_steps")
                and not any(row.get("result", {}).get("performed") for row in result.get("step_reports", []))):
            # Auto-skip can remove the apparent event before its handler reads.
            # No input happened: observe the new surface, never click the
            # old option coordinates or count another consumed action.
            if wait_budget.expired:
                _record_wait_stop(ctx, summary, wait_budget, 'career_transition_no_progress_timeout',
                    event_root / 'report.json', surface='event_choice', owner='event_handoff')
                save_summary()
                return None, "career_transition_no_progress_timeout"
            summary["route_reports"][-1]["transient_event_handoff"] = True
            post_action = True
            sleep(.15)
            continue
        if code or result.get("is_valid") is not True:
            return None, reason
        if result.get("performed_steps"):
            last_event = text
            wait_budget.reset()
        post_action = True
        if not result.get("performed_steps") and reason not in {"ready_main_menu", "ready_training_menu"}:
            return None, reason
        if not result.get("performed_steps") and reason == "ready_main_menu":
            event_rows = result.get("step_reports", [])
            event_decision = event_rows[-1].get("result", {}).get("decision", {}) if event_rows else {}
            if is_route_handoff(event_decision, reason="ready_main_menu"):
                resync_main_menu = True
        for row in result.get("step_reports", []):
            execution = row.get("result", {})
            decision = execution.get("decision", {})
            if (execution.get("performed") is True and decision.get("action") == "tap"
                    and str(decision.get("tap", {}).get("coordinate_name", "")).startswith("event_choice.")):
                if probe is not None:
                    import cv2
                    selected_frame = initial_frame if direct_event else cv2.imread(
                        str(decision.get("evidence", {}).get("image", "")))
                    probe.mark_event_selected(selected_frame)
                # A confirmed event selection starts a new transition, separate
                # from the already-consumed training animation observation.
                post_action = True
                break
    return None, "route_iteration_limit"


def _menu_identity(state):
    return {key: state.get(key) for key in ("current_stats", "remaining_turns", "target_text", "vitality")} | {
        "period": state.get("scenario_state", {}).get("larc_period", {}).get("value")}


def _fan_race_receipt(ctx, value, turn):
    receipt = value.get("fan_race_receipt")
    if receipt is None:
        return None
    if (receipt.get("schema_version") != "larc_fan_race_receipt.v1"
            or receipt.get("run_session_id") != ctx.run_id
            or receipt.get("device") != ctx.progress["device"] or receipt.get("turn_index") != turn
            or receipt.get("performed") is not True or receipt.get("reason") != "fan_shortfall_entry_consumed"
            or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("source_sha256", "")))
            or receipt.get("input_receipt", {}).get("outcome") != "confirmed"
            or receipt["input_receipt"].get("source_sha256") != receipt["source_sha256"]):
        raise ValueError("fan_race_receipt_unverified")
    return receipt


def _run_fan_race(ctx, pending, race_output, summary, save_summary, commit_consumption, *, resume=False):
    if not resume:return False,'supplemental_fan_race_disabled'
    turn, goal = pending.get("turn_index"), pending.get("fan_goal")
    turn_root = Path(pending["output"]).resolve()
    if (pending.get("run_session_id") != ctx.run_id or pending.get("device") != ctx.progress["device"]
            or not fan_goal_due(goal) or goal["source"]["run_session_id"] != ctx.run_id
            or goal["source"]["turn_index"] != turn or turn not in {ctx.progress["next_turn"], ctx.progress["next_turn"] - 1}
            or turn_root == ctx.root or not turn_root.is_relative_to(ctx.root)):
        raise ValueError("fan_race_pending_context_mismatch")
    row = {"turn_index": turn, "output": str(turn_root), "kind": "fan_race", "fan_goal": goal,
           "turn_consumed": False, "resumed": resume}
    summary["turns"].append(row)
    checkpoint_path = ctx.root / "session/larc_race_checkpoint.json"

    def account(value):
        receipt = _fan_race_receipt(ctx, value, turn)
        if receipt is None:
            return False
        row["execution"] = receipt
        if ctx.progress["next_turn"] == turn:
            commit_consumption(row, receipt)
        elif _read(turn_root / "action_receipt.json") != receipt:
            raise ValueError("fan_race_recorded_receipt_mismatch")
        else:
            row["turn_consumed"] = True
        return True

    def checkpoint():
        saved = _read(checkpoint_path) if checkpoint_path.is_file() else {}
        if (saved.get("run_session_id") != ctx.run_id or saved.get("device") != ctx.progress["device"]
                or saved.get("turn_index") != turn or saved.get("fan_goal") != goal):
            return {}
        return saved

    saved = checkpoint()
    if not account(saved):
        return False,'fan_race_unsubmitted_not_resumed'
    if saved.get("status") != "stopped":
        return False, "fan_race_checkpoint_requires_review"
    save_summary()
    try:
        report, code = run_larc_race_route(run_root=ctx.root, output_dir=race_output,
            adb_port=ctx.port, max_steps=24, fan_goal=goal, resume=resume,
            safety_analyzer=getattr(ctx, "screen_reader", None))
    except Exception:
        account(checkpoint())  # The adapter persists confirmation before later route work.
        raise
    consumed = account(report)
    reason = str(report.get("stop_reason", "fan_race_stopped"))
    from scripts.larc_race_transport import race_timing_summary
    summary["race_reports"].append({"path": str(race_output / "report.json"), "code": code,
        "reason": reason, "fan_goal": goal,'timing':race_timing_summary(report)})
    _record_race_executor_error(summary, report, code)
    save_summary()
    if code or report.get("is_valid") is not True:
        return False, reason
    if not consumed:
        return False, "fan_race_confirmation_unverified"
    handoff_path = race_output / "owner_handoff.json"
    if report.get("owner_handoff_path") != str(handoff_path) or not handoff_path.is_file():
        return False, "race_owner_handoff_unverified"
    handoff = _read(handoff_path)
    if (handoff.get("before", {}).get("run_session_id") != ctx.run_id
            or handoff.get("report_sha256") != _canonical_sha256(report)
            or handoff.get("after") != RouteTransitionSession.load(ctx.route, expected_run_session_id=ctx.run_id).to_dict()):
        return False, "race_owner_handoff_unverified"
    ctx.pending.unlink()
    return True, reason


def _valid_ss_receipt(receipt, ctx, turn):
    return (receipt.get("run_session_id") == ctx.run_id and receipt.get("turn_index") == turn
            and receipt.get("action_id") == "safe_advance.larc_ss_confirm"
            and receipt.get("tap_performed") is True
            and re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("source_sha256", ""))) is not None)


def _valid_outing_receipt(receipt, ctx, turn):
    from core.auto_trainer.larc_friend_outing import verified_source
    return (receipt.get("run_session_id") == ctx.run_id and receipt.get("turn_index") == turn
            and receipt.get("tap_performed") is True and type(receipt.get("stage")) is int
            and 1 <= receipt["stage"] <= 5
            and receipt.get("action_id") == f"outing.satake_mei.{receipt['stage']}"
            and verified_source(receipt.get("source"), run_id=ctx.run_id, turn=turn))


def _new_persisted_outing(ctx, turn, previous):
    path = ctx.root / "session/larc_friend_outing.json"
    if not path.is_file():
        return None
    from core.auto_trainer.larc_outing_session import MeiOutingSession
    session = MeiOutingSession.load(path, ctx.run_id)
    receipts = [receipt for receipt in session.data["receipts"] if receipt not in previous]
    if not receipts:
        return None
    if len(receipts) != 1 or not _valid_outing_receipt(receipts[0], ctx, turn):
        raise ValueError("mei_outing_recovery_receipt_unverified")
    receipt = receipts[0]
    if session.data["pending"] != {key: value for key, value in receipt.items() if key != "run_session_id"}:
        raise ValueError("mei_outing_recovery_pending_mismatch")
    return {"performed": True, "tap_performed": True, "is_valid": False,
            "reason": "executed_mei_outing", "outing_receipt": receipt,
            "recovered_from_persisted_receipt": True}


def _persisted_ss_freeze_budget_exhausted(step):
    status=(step.get('post_wait_freeze') or {}).get('status')
    reason=step.get('reason','')
    return (step.get("performed") is False and step.get("tap_performed") is True
            and status in {'insufficient_budget','timeout','poll_limit','frame_unavailable','capture_error'}
            and (reason=='post_wait_freeze_'+status or status=='capture_error' and reason.startswith('post_wait_freeze_capture:'))
            and step.get("larc_ss_execution_receipt", {}).get("status") == "persisted")


def _action_result(report, ctx, turn):
    rows = report.get("stages", {}).get("larc_ss", {}).get("step_reports", [])
    ss = [row["result"] for row in rows if row.get("result", {}).get("tap_performed") is True
          and row["result"].get("decision", {}).get("reason") == "larc_ss_confirm"]
    if ss:
        if len(ss) != 1:
            raise ValueError("multiple_ss_execution_receipts")
        step = ss[0]
        stored = step.get("larc_ss_execution_receipt", {})
        receipt = stored.get("receipt", {})
        if (not _valid_ss_receipt(receipt, ctx, turn)
                or receipt["source_sha256"] != step.get("frame_safety", {}).get("source_image_sha256")
                or stored.get("status") not in {"persisted", "persistence_failed"}):
            raise ValueError("ss_execution_receipt_unverified")
        failure = ("larc_ss_receipt_persistence_failed" if stored["status"] == "persistence_failed"
                   else "ss_post_action_failed" if step.get("performed") is False
                   and not _persisted_ss_freeze_budget_exhausted(step) else None)
        return "ss", receipt, failure
    execution = report.get("stages", {}).get("live_loop", {}).get("execution", {})
    if execution.get("performed") is True:
        if execution.get("reason") == "executed":
            return "train", execution, None
        if execution.get("reason") == "executed_rest_confirmed":
            return "rest", execution, None
        if execution.get("reason") == "executed_normal_outing":
            return "normal_outing", execution, None
        if execution.get("reason") == "executed_mei_outing":
            receipt = execution.get("outing_receipt", {})
            if not _valid_outing_receipt(receipt, ctx, turn):
                raise ValueError("mei_outing_execution_receipt_unverified")
            return "outing", execution, None if execution.get("is_valid") is True else "mei_outing_post_action_failed"
        raise ValueError("unrecognized_execution_receipt")
    if execution.get("tap_performed") is True:
        raise ValueError("partial_execution_requires_review")
    if execution.get("input_may_have_occurred") is True:
        raise ValueError("partial_execution_requires_review")
    return None, execution, None


def _recorded_ss_recovery(ctx):
    """Validate an already-accounted SS whose following observation was delayed."""
    pending = _read(ctx.pending)
    turn = pending.get("turn_index")
    if (pending.get("run_session_id") != ctx.run_id or type(turn) is not int
            or turn != ctx.progress["next_turn"] - 1 or turn != ctx.progress["actions_completed"]):
        raise ValueError("recorded_ss_pending_progress_mismatch")
    old_output = Path(pending["output"]).resolve(strict=True)
    if old_output == ctx.root or not old_output.is_relative_to(ctx.root):
        raise ValueError("recorded_ss_output_outside_run")
    saved = _read(old_output / "pipeline_report.json")
    report = saved.get("report", {})
    if (saved.get("code") != 0 or report.get("is_valid") is not True
            or report.get("status") != "larc_ss_executed"):
        raise ValueError("recorded_ss_pipeline_not_completed")
    rows = [row for row in report.get("stages", {}).get("larc_ss", {}).get("step_reports", [])
            if row.get("result", {}).get("decision", {}).get("reason") == "larc_ss_confirm"]
    if len(rows) != 1 or not _persisted_ss_freeze_budget_exhausted(rows[0]["result"]):
        raise ValueError("recorded_ss_not_exact_freeze_interruption")
    kind, receipt, failure = _action_result(report, ctx, turn)
    if kind != "ss" or failure or _read(old_output / "action_receipt.json") != receipt:
        raise ValueError("recorded_ss_action_receipt_mismatch")
    frame = Path(rows[0]["screenshot_path"]).resolve(strict=True)
    if (not frame.is_relative_to(old_output)
            or hashlib.sha256(frame.read_bytes()).hexdigest() != receipt["source_sha256"]):
        raise ValueError("recorded_ss_frame_mismatch")
    roster = _read(ctx.roster)
    matches = [entry for entry in roster.get("ss_execution_participants", [])
               if entry.get("source_sha256") == receipt["source_sha256"] and entry.get("receipt") == receipt]
    if len(matches) != 1 or receipt["source_sha256"] not in roster.get("ss_executions", []):
        raise ValueError("recorded_ss_roster_receipt_missing")
    return {"reason": "recorded_ss_post_wait_freeze_insufficient_budget", "pending": pending,
            "pipeline_report": saved, "progress": deepcopy(ctx.progress), "action_receipt": receipt}


def run(args):
    from core.auto_trainer.larc_ai_nodes import run_ai_scope
    with run_ai_scope(args):
        return _run_with_control(args)


def _run_with_control(args):
    control = getattr(args, "_run_control", None)
    if control is None:
        return _run(args)
    with bind(control, f"127.0.0.1:{args.adb_port}"):
        try:
            return _run(args)
        finally:
            if not getattr(args, '_keep_run_control', False):
                control.finish()


def _reobserved_turn_root(turn_root, observation_restart):
    """Keep an interrupted attempt's captures when the control epoch is unchanged."""
    turn_root = Path(turn_root)
    return (turn_root.with_name(turn_root.name + f"_reobserve_{observation_restart}")
            if observation_restart else turn_root)


def _run(args):
    started_at = monotonic()
    from scripts.larc_live_takeover import enabled as live_takeover_enabled, reset_live_contexts
    live_takeover = live_takeover_enabled(args)
    attachment = (reset_live_contexts(args.run_root, f'127.0.0.1:{args.adb_port}')
                  if live_takeover else None)
    ctx = _context(args)
    if args.execute and getattr(args, "training_records", None) in {"keep", "discard"}:
        try:
            from core.auto_trainer.pacer_storage import record_retention
            record_retention(ctx.root, ctx.progress["device"], args.training_records)
        except Exception as error:
            import logging
            logging.getLogger(__name__).warning("截图保留设置未记录：%s", type(error).__name__)
    ctx.notify_manual_handoff = _notify_manual_handoff
    _attach_continuation_runtime(ctx, args)
    ctx.live_takeover = live_takeover
    ctx.live_takeover_year1_adapt_recheck = bool(live_takeover)
    ctx.ss_sieve_player_takeover = getattr(args, '_player_live_takeover', False) is True
    output = ctx.root / ("autonomous_" + datetime.now().strftime("%y%m%d%H%M%S_") + uuid.uuid4().hex[:4])
    output.mkdir()
    ctx.segment_root = output
    summary = {"run_session_id": ctx.run_id, "device": ctx.progress["device"], "output": str(output),
               "policy_version": policy_version(load_goal_config(ctx.root / "goal.json")),
                   "policy_branch": policy_branch(load_goal_config(ctx.root / "goal.json")),
               "status": "running" if args.execute else "dry_run", "career_complete": False,
               "max_actions": args.max_actions, "actions_completed_this_run": 0,
               "consumed_turns_this_run": 0, "fixed_races_consumed_this_run": 0,
               "start_progress": deepcopy(ctx.progress), "turns": [], "route_reports": [], "race_reports": [],
               "adaptation_reports": []}
    if getattr(args, '_panel_player_resume_history', None):
        summary['player_resume_history'] = deepcopy(args._panel_player_resume_history)

    pause_baseline = current().snapshot()['paused_seconds'] if current() is not None else 0.

    def save_summary(*, final=False):
        summary["elapsed_seconds"] = monotonic() - started_at
        if current() is not None:
            summary["control_timing"] = current().snapshot()
            if getattr(ctx, 'continuation_runtime', None) is not None:
                summary['wall_elapsed_seconds'] = summary['elapsed_seconds']
                summary['human_wait_seconds'] = max(0., summary['control_timing']['paused_seconds'] - pause_baseline)
                summary['elapsed_seconds'] = max(0., summary['elapsed_seconds'] - summary['human_wait_seconds'])
        summary["final_progress"] = deepcopy(ctx.progress)
        from core.auto_trainer.larc_manual_handoff import snapshot as manual_handoff_snapshot
        summary['manual_handoff'] = manual_handoff_snapshot(ctx.root)
        summary['technical_wait_stop_counts'] = _wait_stop_counts(summary.get('technical_wait_stops', []))
        try:
            _atomic_write_json(output / "summary.json", summary, buffered_diagnostic=True)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33}:
                raise
            # This is display telemetry, not an action receipt or progress
            # ledger. A busy reader must not terminate or replay game actions.
            summary["summary_write_failures"] = summary.get("summary_write_failures", 0) + 1
            summary["summary_write_warning"] = f"{type(exc).__name__}:{exc}"
            if summary["summary_write_failures"] == 1:
                print("[pacer] summary file busy; continuing with persisted action progress", file=sys.stderr)
            if final:
                _atomic_write_json(output / "summary.final.json", summary, buffered_diagnostic=True)

    def commit_consumption(row, receipt):
        _atomic_write_json(Path(row["output"]) / "action_receipt.json", receipt)
        updated = dict(ctx.progress, next_turn=ctx.progress["next_turn"] + 1,
                       actions_completed=ctx.progress["actions_completed"] + 1)
        _atomic_write_json(ctx.root / "progress.json", updated)
        ctx.progress = updated
        row["turn_consumed"] = True
        try:
            from core.auto_trainer.larc_strategy_params import unified_larc_enabled
            if (unified_larc_enabled(load_goal_config(ctx.root/'goal.json'))
                    or _ss_sieve_config(ctx, 'year1_end') is not None
                    or _ss_sieve_config(ctx, 'pre_debut_end') is not None):
                from core.auto_trainer.larc_action_history import record_confirmed_action
                record_confirmed_action(ctx.root/'session', ctx.run_id, row['turn_index'],
                                        row.get('kind'), Path(row['output'])/'action_receipt.json')
        except (OSError, ValueError, TypeError, KeyError) as error:
            row['strategy_history_note'] = type(error).__name__
        target = receipt.get('target')
        ctx.event_training_action = ({
            'status':'accepted', 'run_session_id':ctx.run_id,
            'turn_index':row['turn_index'], 'training_type':target,
            'basis':'executed_training_action',
        } if row.get('kind') == 'train' and target in {'speed','stamina','power','guts','wisdom'} else None)
        if ctx.event_training_action is not None:
            from core.auto_trainer.larc_mei_contact import record_contact
            try:
                contact=record_contact(ctx.root/'session',ctx.run_id,row['turn_index'],
                    Path(row['output'])/'decision/turns/turn_000/state.json',receipt)
                if contact is not None:row['mei_contact']=contact
            except (OSError,ValueError,TypeError) as error:
                row['mei_contact_note']=type(error).__name__  # Optional strategy history, not an action retry.
        summary["actions_completed_this_run"] += 1
        summary["consumed_turns_this_run"] += 1
        save_summary()

    save_summary()
    ctx.batch_limit = args.max_actions
    if not args.execute:
        return summary, 0
    continuation = getattr(ctx, 'continuation_runtime', None)
    if continuation is not None:
        resumed = continuation._resume_pending()
        if resumed and resumed.get('stop_reason') == 'continuation_handoff':
            summary.update(status='handoff', stop_reason='continuation_handoff',
                           within_scope_complete=True, normal_settlement_complete=False)
            save_summary(final=True)
            return summary, 0
    from core.auto_trainer.stop_frame_observer import StopFrameObserver
    stop_frames = StopFrameObserver(ctx).bind() if getattr(ctx,'cut_farming_hook',None) is None else None
    try:
        if current() is not None:
            current().checkpoint("starting")
        from core.auto_trainer.larc_manual_handoff import checkpoint as manual_handoff_checkpoint
        if not _cut_screening(ctx):
            manual_handoff_checkpoint(ctx)
        ctx.transition_probe = _make_transition_probe(ctx)
        if live_takeover and not _terminal_finish_verified(ctx):
            from scripts.larc_live_takeover import observe_current
            if getattr(args, '_player_live_takeover', False):
                from core.auto_trainer.cut_pause_resume import return_from_inspection
                inspection = return_from_inspection(ctx, None, output/'player_inspection',
                    allow_handoff=True)
                summary['player_inspection'] = inspection
                if not inspection.get('ok'):
                    _stop(summary, inspection['reason'])
                    return summary, 2
            scene = observe_current(ctx, output/'live_takeover',
                adb=ctx.transition_probe.adb if ctx.transition_probe is not None else None)
            if (ctx.root/'session/ordinary_setup.json').is_file() and scene.get('source'):
                from scripts.larc_ordinary_entry import prepare_ordinary_entry
                entry=prepare_ordinary_entry(ctx,scene,output/'ordinary_entry')
                summary['ordinary_entry']=entry
                save_summary()
                if not entry.get('ok'):
                    if entry.get('normal_stop') is True:
                        summary.update(normal_stop=True,resource_status=entry.get('resource_status'),stop_message=entry.get('message'))
                        _stop(summary,entry['reason']);return summary,0
                    _stop(summary,entry['reason']);return summary,2
                if entry.get('changed'):
                    ctx.ordinary_career_opening = (ctx.ordinary_career_opening
                        or entry.get('new_career_started') is True)
                    scene=observe_current(ctx,output/'ordinary_entry_takeover',
                        adb=ctx.transition_probe.adb if ctx.transition_probe is not None else None)
            if (scene.get('cut_debut_race') or scene.get('ordinary_clock_retry')) and attachment.get('clock_retry_state'):
                scene['clock_retry_state'] = attachment['clock_retry_state']
            summary['live_takeover'] = dict(attachment, current_scene=scene)
            save_summary()
            if getattr(args,'_new_cycle_live_takeover',False) and scene.get('owner')=='unknown':
                summary['entry_message']='当前页面不能安全接手。请先进入与配置角色一致的凯旋门育成；资源确认页面请自行处理后再开始新一轮。'
                _stop(summary,'new_cycle_current_scene_unverified')
                save_summary()
                return summary,2
            if scene['owner'] == 'race' and ctx.allow_fixed_races:
                ready, reason = _run_fixed_race(ctx, output/'live_race', summary, save_summary,
                                               live_source=scene)
                if not ready:
                    _stop(summary, reason)
                    return summary, 2
        ledger_store = CareerGoalLedgerStore()
        if not ctx.ledger.exists():
            ledger_store.write_atomic(ctx.ledger, CareerGoalLedger.empty(ctx.run_id))
        ledger_store.load(ctx.ledger, expected_run_session_id=ctx.run_id)
        if ctx.recovery is not None:
            _atomic_write_json(output / "recovery.json", ctx.recovery)
            ctx.pending.unlink()
            summary["recovery_path"] = str(output / "recovery.json")
            save_summary()
        if ctx.fan_pending is not None:
            checkpoint_path=ctx.root/'session/larc_race_checkpoint.json'
            old_checkpoint=_read(checkpoint_path) if checkpoint_path.is_file() else {}
            receipt=_fan_race_receipt(ctx,old_checkpoint,ctx.fan_pending.get('turn_index'))
            if receipt is not None:
                ready, reason = _run_fan_race(ctx, ctx.fan_pending, output / "fan_race_resume",
                    summary, save_summary, commit_consumption, resume=True)
                if not ready:
                    _stop(summary, reason)
                    return summary, 2
            else:
                if old_checkpoint.get('entry_consumed') is True:
                    _stop(summary,'fan_race_confirmed_receipt_missing');return summary,2
                if (ctx.fan_pending.get('run_session_id')!=ctx.run_id or ctx.fan_pending.get('device')!=ctx.progress['device']
                        or ctx.fan_pending.get('turn_index')!=ctx.progress['next_turn']):
                    _stop(summary,'fan_race_pending_context_mismatch');return summary,2
                source,reason=_route_to_menu(ctx,output/'unsubmitted_fan_observation',summary,save_summary,observe_only=True)
                if reason!='ready_main_menu' or source is None:
                    _stop(summary,'fan_race_unsubmitted_handoff');return summary,2
                # Fresh main menu proves no supplemental race owns the page.
                # Retain the old checkpoint/report as evidence; remove only the intent.
                summary['unsubmitted_fan_pending']={'status':'cancelled_at_main_menu','pending':ctx.fan_pending,'source':source}
                ctx.pending.unlink();ctx.fan_pending=None;save_summary()
        checkpoint_path = ctx.root / "session/larc_race_checkpoint.json"
        checkpoint = _read(checkpoint_path) if checkpoint_path.exists() else None
        dialogue_race_resume=False
        if (checkpoint is not None and checkpoint.get('status')=='handed_off'
                and checkpoint.get('stop_reason')=='post_race_dialogue' and ctx.transition_probe is not None):
            from core.auto_trainer.race_route import RaceRoutePlanner
            try:
                image=ctx.transition_probe.adb.read_frame(deadline=monotonic()+1.)
                dialogue_race_resume=image is not None and RaceRoutePlanner._race_preview_action_points(image) is not None
            except (OSError,RuntimeError,AttributeError,TypeError):
                pass
        if checkpoint and checkpoint.get("fan_goal") is None and (checkpoint.get("status") != "handed_off" or dialogue_race_resume):
            exchange_entry_recovery=(checkpoint.get('status')=='blocked'
                and checkpoint.get('stop_reason')=='race_day_entry_postcondition_failed')
            checkpoint_steps = checkpoint.get("steps") or []
            catalog_error_resume = _catalog_read_error_resume(checkpoint,
                checkpoint_steps[-1].get("result", {}) if checkpoint_steps else {})
            fan_list_resume = _fan_shortfall_list_resume(checkpoint,
                checkpoint_steps[-1].get("result", {}) if checkpoint_steps else {})
            if (not ctx.allow_fixed_races or ctx.expected is not None
                    or not (fan_list_resume or catalog_error_resume or exchange_entry_recovery or dialogue_race_resume or _confirmed_result_repeat_resume(checkpoint) or (checkpoint.get("status") == "stopped"
                    and checkpoint.get("stop_reason") in {"max_steps_reached", "race_transition_no_progress_timeout", "no_progress_timeout", "action_safety_gate", "modal_confirm:unsupported",
                        "larc_adaptation_warning:confirmation_not_allowed", "race_list:selected_grade_not_g1",
                        "resource_prompt", "unsupported_screen:training_menu", "unsupported_screen:portrait_ui", "unsupported_screen:game_home", "race_list:enter_not_allowed", "race_day:enter_not_allowed", "wbc_hub:enter_not_allowed", "gur_race:enter_not_allowed", "goal_achievement_unverified"}))):
                _stop(summary, "race_checkpoint_requires_review")
                return summary, 2
            ready, reason = _run_fixed_race(ctx, output / "fixed_race_resume", summary, save_summary, resume=True)
            if not ready:
                _stop(summary, reason)
                return summary, 2
        adaptation_resume = _resume_submitted_adaptation(
            ctx, output / "submitted_adaptation_resume", summary, save_summary)
        if adaptation_resume is not None:
            ready, reason = adaptation_resume
            if not ready:
                _stop(summary, reason)
                return summary, 2
        parser = TrainingParser(use_mimo=False, recognition_mode="lightweight", larc_roster_path=ctx.roster)
        page_retries, handoff_budgets = {}, {}
        control_reobservations = {}  # Naming only; never an action or retry budget.
        recovered_arc = resume_arc_skill_learning(ctx, output / "arc_skill_completion_resume")
        if recovered_arc is not None:
            summary.setdefault("prerace_skills", []).append(recovered_arc)
            save_summary()
            if not recovered_arc.get("ok"):
                _stop(summary, recovered_arc.get("reason", recovered_arc.get("status", "arc_skills_stopped")))
                return summary, 2
        # An unsettled purchase remains owned even if a future preset disables
        # learning. Recover it before entering either career or cut navigation.
        completed_skills=resume_support_skill_completion(ctx,output/"support_skill_completion_resume")
        if completed_skills is not None:
            summary['support_skill_completion_recovery'],source,_=completed_skills
            reason='ready_main_menu'
            save_summary()
        else:
            source, reason = _route_to_menu(ctx, output / "entry", summary, save_summary,
                                            observe_only=ctx.expected is not None)
        home_preparation_pending = {}
        while source is not None:
            turn = ctx.progress["next_turn"]
            if current() is not None:
                current().turn = turn
                epoch = current().epoch
                current().checkpoint("turn_start")
                if current().epoch != epoch:
                    ctx.ss_sieve_player_takeover = True
                    source, reason = _route_to_menu(ctx, output / f"resume_{current().epoch}", summary, save_summary)
                    if source is None:
                        break
            home_preparation_started = monotonic()
            home_timing = home_preparation_pending.pop(turn, {
                "provider_seconds": 0., "cut_check_seconds": 0., "outing_prepare_seconds": 0.})
            home_preparation_carried = home_timing.pop("total_seconds", 0.)
            for attempt in range(2):
                suffix = (f"_page{page_retries[turn]}" if page_retries.get(turn) else "") + ("_retry" if attempt else "")
                provider_started = monotonic()
                provider = provide_main_menu_state(source, output / f"menu_{turn:03d}{suffix}.json",
                                                   turn_index=turn, scenario="larc")
                home_timing["provider_seconds"] += monotonic() - provider_started
                state = provider.get("state_context", {})
                if (provider.get("valid_for_training") is True
                        and state.get("scenario_state", {}).get("scenario") == "larc"
                        and provider.get("larc_period", {}).get("status") == "verified"):
                    break
                if attempt == 0:
                    # A newly entered home can still be fading in. Preserve the
                    # failed provider report and re-read one fresh frame, no tap.
                    sleep(1.0)
                    from core.auto_trainer.transition_wait import entry_page_handoff
                    import cv2
                    image = cv2.imread(str(source)) if Path(source).is_file() else None
                    changed_page = image is not None and entry_page_handoff(image, source) is not None
                    if changed_page and ctx.expected is None:
                        _await_late_career_page(ctx, source)
                    source, reason = _route_to_menu(ctx, output / f"menu_{turn:03d}{suffix}_retry",
                        summary, save_summary, observe_only=ctx.expected is not None or not changed_page, post_action=True)
                    if (source is None and reason == "race_day_main_menu"
                            and ctx.expected is None and ctx.allow_fixed_races):
                        # A fresh read-only retry can reveal the next forced
                        # race day. Resume its existing authorized owner rather
                        # than treating that ordinary page handoff as terminal.
                        source, reason = _route_to_menu(ctx,
                            output / f"menu_{turn:03d}{suffix}_race_handoff",
                            summary, save_summary, post_action=True)
                    if source is None:
                        break
            if source is None:
                break
            if (provider.get("valid_for_training") is not True
                    or state.get("scenario_state", {}).get("scenario") != "larc"
                    or provider.get("larc_period", {}).get("status") != "verified"):
                reason = "main_menu_state_unverified"
                summary['recognition_failure_source'] = {
                    'report_path': str(output / f"menu_{turn:03d}{suffix}.json"),
                    'turn_index': turn}
                break
            if ctx.expected is not None:
                if _menu_identity(state) != _menu_identity(ctx.expected["state_context"]):
                    reason = "expected_main_menu_state_mismatch"
                    break
                ctx.expected = None
            ctx.event_state = deepcopy(state)
            if getattr(ctx, "live_takeover_year1_adapt_recheck", False):
                _reconcile_live_takeover_career_continuity(ctx, provider, summary, save_summary)
            _record_career_fingerprint(ctx, provider)
            _manual_home_checkpoint(ctx, provider)
            if getattr(ctx,'cut_farming_hook',None) is not None:
                cut_check_started = monotonic()
                _drain_passive_events(ctx, summary,settle_cut=True)
                cut_report = ctx.cut_farming_hook.before_training(ctx, source, provider,
                    output / f'cut_status_{turn:03d}')
                home_timing["cut_check_seconds"] += monotonic() - cut_check_started
                summary.setdefault('cut_farming_checks', []).append(cut_report)
                if cut_report.get('stop_reason'):
                    summary.update(status='cut_farming_checkpoint', stop_reason=cut_report['stop_reason'])
                    reason = cut_report['stop_reason']
                    break
                if cut_report.get('status') == 'probed':
                    source = cut_report['source']
                    home_timing["total_seconds"] = home_preparation_carried + monotonic() - home_preparation_started
                    home_preparation_pending[turn] = home_timing
                    save_summary()
                    continue
                if cut_report.get('status') == 'continuation_cutoff_ready':
                    source = cut_report['source']
            else:
                from core.auto_trainer.larc_mei_unlock_policy import failed_mei_gamble, observe_mei_gamble_menu
                menu_check = observe_mei_gamble_menu(ctx, source, provider)
                if menu_check is not None:
                    summary['mei_unlock_result_check'] = menu_check
                    save_summary()
                mei_failure = failed_mei_gamble(ctx)
                if mei_failure is not None:
                    summary.update(
                        status='mei_unlock_failed',
                        stop_reason='mei_unlock_failed',
                        mei_unlock_failure=mei_failure,
                    )
                    reason = 'mei_unlock_failed'
                    save_summary()
                    break
                binding_cfg = _read(ctx.root / 'session/ace_cycle_binding.json')
                tf_cfg = binding_cfg.get('training_failure_restart') if isinstance(binding_cfg, dict) else None
                if (isinstance(binding_cfg, dict)
                        and binding_cfg.get('run_session_id') == ctx.run_id
                        and isinstance(tf_cfg, dict)
                        and tf_cfg.get('enabled') is True):
                    from core.auto_trainer.cut_training_failure import completed_training_failure, training_failure_should_restart
                    observed_tf = None
                    for nav in reversed(summary.get('route_reports', [])):
                        nav_path = nav.get('path') if isinstance(nav, dict) else None
                        if nav_path and Path(nav_path).is_file():
                            cand_tf = completed_training_failure(_read(nav_path), ctx.run_id)
                            if cand_tf is not None:
                                observed_tf = cand_tf
                                break
                    current_period = provider.get('larc_period', {}).get('value')
                    if observed_tf is not None and training_failure_should_restart(
                        observed_tf,
                        ignore_wisdom=bool(tf_cfg.get('ignore_wisdom', True)),
                        stop_at_senior_expedition=bool(tf_cfg.get('stop_at_senior_expedition', True)),
                        period=current_period,
                    ):
                        _write_json(ctx.root / 'session/ace_training_failure.json', {
                            'schema_version': 'larc.ace_training_failure.v1',
                            'run_id': ctx.run_id,
                            'endpoint': ctx.progress.get('device'),
                            'turn': turn,
                            'period': current_period,
                            'failure': observed_tf,
                        })
                        summary.update(
                            status='training_failure_restart',
                            stop_reason='training_failure_restart',
                            training_failure_event=observed_tf,
                        )
                        reason = 'training_failure_restart'
                        save_summary()
                        break
            continuation = getattr(ctx, 'continuation_runtime', None)
            if continuation is not None:
                ctx.continuation_return_menu = lambda folder: _route_to_menu(ctx, folder, summary, save_summary)
                for navigation in summary['route_reports']:
                    path = navigation.get('path')
                    if path and Path(path).is_file():
                        continuation.observe_report(_read(path))
                continuation_result = continuation.before_home(source, provider,
                    output / f'continuation_{turn:03d}')
                summary.setdefault('continuation_checks', []).append(continuation_result)
                save_summary()
                if continuation_result.get('stop_reason'):
                    summary.update(status='handoff' if continuation_result['stop_reason']=='continuation_handoff' else 'continuation_checkpoint',
                        stop_reason=continuation_result['stop_reason'])
                    reason = continuation_result['stop_reason']
                    break
                if continuation_result.get('status') in {'recollected', 'reobserve'}:
                    if continuation_result.get('source'):
                        source = continuation_result['source']
                    else:
                        source, reason = _route_to_menu(ctx, output / f'continuation_return_{turn}', summary, save_summary)
                    continue
            stop_year = getattr(args, "stop_after_year", None)
            observed_year = provider["larc_period"].get("value", {}).get("year_index")
            if stop_year is not None and type(observed_year) is int and observed_year > stop_year:
                summary.update(status="year_checkpoint", stop_reason="year_checkpoint",
                               checkpoint_period=provider["larc_period"]["value"],
                               checkpoint_state_path=str(output / f"menu_{turn:03d}{suffix}.json"))
                break
            if summary["consumed_turns_this_run"] >= args.max_actions:
                summary.update(status="batch_limit_reached", stop_reason="batch_limit_reached")
                break
            _record_missed_sieve_checkpoints(ctx, source, provider, summary)
            sieve_config = _ss_sieve_config(ctx, 'year1_end')
            sieve_ready_evidence = None
            if sieve_config is not None:
                sieve_evidence = _ss_sieve_year1_home(ctx, source, provider)
                if sieve_evidence is not None:
                    from core.auto_trainer.larc_ss_sieve import needs_ready_count
                    if needs_ready_count('year1_end', sieve_evidence['full5_ss'], sieve_config):
                        sieve_ready_evidence = sieve_evidence
                    else:
                        sieve_stop = _ss_sieve_dispatch(ctx, sieve_evidence, summary)
                        if sieve_stop:
                            summary.update(status='continuation_checkpoint', stop_reason=sieve_stop)
                            reason = sieve_stop
                            break
            from core.auto_trainer.larc_ai_nodes import expedition_checkpoint
            ai_checkpoint=expedition_checkpoint(ctx,provider)
            if ai_checkpoint is not None and not ai_checkpoint.get('cached'):
                summary.setdefault('ai_checkpoints',[]).append(ai_checkpoint);save_summary()
            _manual_home_checkpoint(ctx, provider)
            deck_started = monotonic()
            deck_report, source, provider = prepare_deck_inspection(ctx, source, provider,
                output / f"deck_inspect_{turn:03d}", cut_mode=(_cut_screening(ctx) and not _continuation_enabled(ctx)))
            if deck_report is not None:
                home_timing["deck_inspect_seconds"] = home_timing.get("deck_inspect_seconds", 0.) + (monotonic() - deck_started)
                summary.setdefault("deck_inspections", []).append({"turn_index": turn, **deck_report})
                save_summary()
                if source is None:
                    source, reason = _route_to_menu(ctx, output / f"deck_inspect_{turn:03d}_recover", summary, save_summary)
                    continue
                if deck_report.get("visited"):
                    state = provider["state_context"]
                    ctx.event_state = deepcopy(state)
            startup, source, provider = prepare_support_skills(ctx,source,provider,
                output/f"support_skills_{turn:03d}",cut_mode=_cut_screening(ctx))
            if startup is not None:
                summary.setdefault("support_skill_startup",[]).append({"turn_index":turn,**startup})
                save_summary()
                if startup.get("visited"):
                    continue # New skill Pt and actual training previews; no turn consumed.
            if ctx.allow_adaptations:
                if getattr(ctx, "live_takeover_year1_adapt_recheck", False):
                    observed_period = provider.get("larc_period") or {}
                    observed_value = observed_period.get("value") or {}
                    if observed_period.get("status") == "verified":
                        if observed_value.get("year_index") == 1:
                            _clear_stale_adaptations_for_year1_takeover(ctx, summary, save_summary)
                        else:
                            ctx.live_takeover_year1_adapt_recheck = False
                phase = select_larc_adaptation_phase(provider["larc_period"],
                    goal=load_goal_config(ctx.root / "goal.json"))
                if phase:
                    _manual_home_checkpoint(ctx, provider)
                    ready, executed, reason = _ensure_adaptation(
                        ctx, phase, output / f"adaptation_{phase}", summary, save_summary)
                    if not ready:
                        break
                    if executed:
                        source, reason = _route_to_menu(ctx, output / f"adaptation_{phase}_after", summary, save_summary)
                        continue  # The pre-upgrade provider state is no longer current.
            if ctx.allow_fixed_races and not _cut_screening(ctx):
                from core.auto_trainer.larc_custom_race import due_custom_race, record_custom_race_outcome
                current_goal = load_goal_config(ctx.root / "goal.json")
                custom_spec = due_custom_race(ctx.root, current_goal, provider["larc_period"])
                if custom_spec is not None:
                    _manual_home_checkpoint(ctx, provider)
                    custom_race_id = str(custom_spec.get("race_id") or custom_spec.get("id"))
                    safe_race_tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", custom_race_id)
                    custom_idx = len(summary.setdefault("custom_race_attempts", []))
                    custom_out = output / f"custom_race_{turn:03d}_{safe_race_tag}_{custom_idx}"
                    race_report, race_code = run_larc_race_route(
                        run_root=ctx.root, output_dir=custom_out, adb_port=ctx.port, max_steps=24,
                        custom_race=custom_spec, safety_analyzer=getattr(ctx, "screen_reader", None))
                    from scripts.larc_race_transport import race_timing_summary
                    summary["race_reports"].append({
                        "path": str(custom_out / "report.json"), "code": race_code,
                        "reason": str(race_report.get("stop_reason", "custom_race_stopped")),
                        "custom_race": custom_spec, "custom_race_outcome": race_report.get("custom_race_outcome"),
                        "timing": race_timing_summary(race_report),
                    })
                    summary["custom_race_attempts"].append({
                        "turn_index": turn, "race_id": custom_race_id,
                        "larc_ordinal": custom_spec["larc_ordinal"],
                        "outcome": race_report.get("custom_race_outcome"),
                        "code": race_code,
                    })
                    _record_race_executor_error(summary, race_report, race_code)
                    save_summary()
                    if race_code == 0 and race_report.get("custom_race_outcome") == "completed":
                        record_custom_race_outcome(
                            ctx.root, ordinal=custom_spec["larc_ordinal"], race_id=custom_race_id,
                            status="completed", reason=str(race_report.get("stop_reason")), turn_index=turn,
                            extra={"report_path": str(custom_out / "report.json")})
                        row = {
                            "turn_index": turn, "output": str(custom_out), "kind": "custom_race",
                            "custom_race": custom_spec, "policy_version": policy_version(current_goal),
                            "policy_branch": policy_branch(current_goal), "before": _menu_identity(state),
                            "turn_consumed": False,
                        }
                        summary["turns"].append(row)
                        commit_consumption(row, {
                            "kind": "custom_race", "race_id": custom_race_id,
                            "larc_ordinal": custom_spec["larc_ordinal"],
                            "report_path": str(custom_out / "report.json"),
                        })
                        source, reason = _route_to_menu(
                            ctx, output / f"custom_race_{turn:03d}_after", summary, save_summary, post_action=True)
                        continue
                    if race_code == 0 and race_report.get("custom_race_outcome") == "skipped_unavailable":
                        record_custom_race_outcome(
                            ctx.root, ordinal=custom_spec["larc_ordinal"], race_id=custom_race_id,
                            status="skipped_unavailable", reason="race_unavailable_or_locked_on_list", turn_index=turn,
                            extra={"report_path": str(custom_out / "report.json")})
                        source, reason = _route_to_menu(
                            ctx, output / f"custom_race_{turn:03d}_reobserve", summary, save_summary, observe_only=True)
                        continue
                    reason = str(race_report.get("stop_reason", "custom_race_stopped"))
                    _stop(summary, reason)
                    break
            retry = ctx.growth_recollections.get(turn, 0)
            resume_suffix = f"_resume_{current().epoch}" if current() is not None and current().epoch else ""
            page_suffix = f"_page{page_retries[turn]}" if page_retries.get(turn) else ""
            turn_root = output / (f"turn_{turn:03d}" + (f"_growth_{retry}" if retry else "") + resume_suffix + page_suffix)
            turn_root = _reobserved_turn_root(turn_root, control_reobservations.get(turn, 0))
            observation = ctx.menu_observation
            observed = observation.get("result", {})
            source_sha = observed.get("frame_safety", {}).get("source_image_sha256")
            fan_goal = None
            fan_ss_plan = None
            menu_text = observed.get("decision", {}).get("evidence", {}).get("screen_text", {}).get("normalized_text", "")
            if (observation.get("screenshot_path") == source and source_sha
                    and source_sha == provider.get("source", {}).get("sha256")
                    and provider.get("source", {}).get("path") == source):
                fan_goal = read_larc_fan_goal(
                    menu_text,
                    remaining_turns=state.get("remaining_turns"), source_path=source,
                    source_sha256=source_sha, run_session_id=ctx.run_id, turn_index=turn)
            if fan_goal is not None:
                fan_ss_plan = plan_ss_before_fan_race(fan_goal, menu_text, provider["larc_period"],
                    growth_observation=state.get('scenario_state',{}).get('larc_growth_observation'))
            outing_started = monotonic()
            _manual_home_checkpoint(ctx, provider)
            try:
                outing, source, provider = prepare_mei_outing(ctx, source, provider, turn_root / "outing_inspection")
            except MeiOutingMenuHandoff as handoff:
                home_timing['outing_prepare_seconds']+=monotonic()-outing_started
                home_timing['total_seconds']=home_preparation_carried+monotonic()-home_preparation_started
                home_preparation_pending[turn]=home_timing
                summary.setdefault('outing_inspection_handoffs',[]).append({
                    'turn_index':turn,'report_path':handoff.report_path,
                    'execution_reason':handoff.report['execution_reason'],'screen':'main_menu'})
                save_summary()
                source,reason=_route_to_menu(ctx,turn_root/'outing_inspection_replan',summary,save_summary)
                control_reobservations[turn]=control_reobservations.get(turn,0)+1
                continue
            except MeiOutingEventHandoff as handoff:
                home_timing["outing_prepare_seconds"] += monotonic() - outing_started
                home_timing["total_seconds"] = home_preparation_carried + monotonic() - home_preparation_started
                home_preparation_pending[turn] = home_timing
                summary.setdefault("outing_inspection_handoffs", []).append({
                    "turn_index": turn, "report_path": handoff.report_path,
                    "stage": handoff.report.get("handoff_stage"), "screen": "event_choice"})
                save_summary()
                # The ordinary route/event owner chooses the event. Re-enter
                # this same turn from its returned home, without reusing the
                # inspection directory or a pre-event training decision.
                source, reason = _route_to_menu(ctx, turn_root / "outing_event_after",
                    summary, save_summary, post_action=True)
                control_reobservations[turn] = control_reobservations.get(turn, 0) + 1
                continue
            home_timing["outing_prepare_seconds"] += monotonic() - outing_started
            state = provider["state_context"]
            # Outing inspection can return a new menu frame. Rebuild this
            # same-turn plan from that existing observation, without recapture.
            if fan_ss_plan is not None and fan_ss_plan["source"]["path"] != source:
                fresh = ctx.menu_observation.get("result", {})
                menu_text = fresh.get("decision", {}).get("evidence", {}).get("screen_text", {}).get("normalized_text", "")
                fresh_sha = fresh.get("frame_safety", {}).get("source_image_sha256")
                fan_goal = (read_larc_fan_goal(menu_text, remaining_turns=state.get("remaining_turns"),
                    source_path=source, source_sha256=fresh_sha, run_session_id=ctx.run_id, turn_index=turn)
                    if ctx.menu_observation.get("screenshot_path") == source
                    and fresh_sha == provider.get("source", {}).get("sha256") else None)
                fan_ss_plan = plan_ss_before_fan_race(fan_goal, menu_text, provider["larc_period"],
                    growth_observation=state.get('scenario_state',{}).get('larc_growth_observation'))
            row = {"turn_index": turn, "output": str(turn_root),
                   "policy_version": policy_version(load_goal_config(ctx.root / "goal.json")),
                   "policy_branch": policy_branch(load_goal_config(ctx.root / "goal.json")), "before": _menu_identity(state),
                   "turn_consumed": False}
            row["home_preparation"] = {
                **{key: round(value, 4) for key, value in home_timing.items()},
                "total_seconds": round(home_preparation_carried + monotonic() - home_preparation_started, 4),
                "scope": "after_home_handoff_before_training_pipeline; includes_retries_and_navigation",
            }
            if fan_ss_plan is not None:
                row.update(fan_goal=fan_goal, fan_ss_plan=fan_ss_plan)
            observation = ctx.menu_observation
            result = observation.get("result", {})
            source_sha = result.get("frame_safety", {}).get("source_image_sha256")
            supporter_goal = None
            if (observation.get("screenshot_path") == source and source_sha
                    and source_sha == provider.get("source", {}).get("sha256")):
                supporter_goal = read_larc_supporter_goal(
                    result.get("decision", {}).get("evidence", {}).get("screen_text", {}).get("normalized_text", ""),
                    remaining_turns=state.get("remaining_turns"), source_path=source,
                    source_sha256=source_sha, run_session_id=ctx.run_id, turn_index=turn)
            if supporter_goal is not None:
                row["supporter_goal"] = supporter_goal
            summary["turns"].append(row)
            before_ss = set(_read(ctx.roster).get("ss_executions", []))
            outing_session_path = ctx.root / "session/larc_friend_outing.json"
            before_outing = _read(outing_session_path).get("receipts", []) if outing_session_path.is_file() else []
            _manual_home_checkpoint(ctx, provider)
            _atomic_write_json(ctx.pending, {"run_session_id": ctx.run_id, "turn_index": turn,
                                             "output": str(turn_root)})
            save_summary()
            argv = ["--goal-config", str(ctx.root / "goal.json"), "--output-root", str(turn_root / "capture"),
                    "--actual-group-id", f"t{turn:03d}_{uuid.uuid4().hex[:6]}", "--run-session-id", ctx.run_id,
                    "--turn-index", str(turn), "--source-state-key", f"{ctx.run_id}_turn{turn:03d}",
                    "--operator-id", "larc-autonomy", "--state-context-provider", "main-menu-png",
                    "--main-menu-screenshot-path", str(source), "--start-screen", "main-menu",
                    "--settle-seconds", "0.25",
                    "--adb-port", str(ctx.port), "--transition-session-json", str(ctx.route),
                    "--skip-session-json", str(ctx.skip), "--capture-live", "--capture-execute",
                    "--capture-confirm-token", "CAPTURE_UG_PARENT_LIVE_TABS",
                    "--prefer-larc-ss", "--allow-partial-live-sidecar",
                    "--initialize-larc-reward-cycles",
                    "--run-dir", str(turn_root / "decision"), "--mode", "live",
                    "--max-turns", "1", "--execute-training-confirm", "--training-confirm-token", "UG_PARENT_AUTOTRAINER_SANDBOX"]
            try:
                turn_args = pipeline_args(argv)
                turn_args._main_menu_provider_report = provider
                turn_args._larc_history_owner = 'autonomous'
                turn_args._safety_analyzer = ctx.screen_reader
                last_purchase = getattr(ctx, "last_training_purchase", None)
                if isinstance(last_purchase, dict) and last_purchase.get("turn") == turn:
                    turn_args._larc_last_training_purchase = dict(last_purchase)
                from core.auto_trainer.larc_joint_career.session import current_marker
                career_main = current_marker(ctx, turn)
                if career_main is not None:
                    turn_args._larc_career_joint_main = career_main
                sieve_capture = {}
                if sieve_ready_evidence is not None:
                    sieve_capture['larc_ss_sieve_pre_action'] = lambda capture: _ss_sieve_from_capture(
                        ctx, turn_args, capture, sieve_config, sieve_ready_evidence, summary)
                report, code = run_pipeline(turn_args, training_parser=parser,
                    **sieve_capture,
                    **({"larc_fan_plan": fan_ss_plan} if fan_ss_plan is not None else {}),
                    **({"larc_friend_outing": outing} if outing is not None else {}),
                    **({"larc_supporter_goal": supporter_goal} if supporter_goal is not None else {}),
                    **({"larc_pre_action": lambda captured: _training_growth(ctx, captured, menu_text,
                        turn_root / "growth", summary, save_summary)} if ctx.allow_adaptations else {}))
            except ReplanTurn:
                # The action executor has sent no consuming input. Discard its
                # old choice and re-enter normal capture/decision from the UI.
                row["control_interrupted_before_action"] = True
                control_reobservations[turn] = control_reobservations.get(turn, 0) + 1
                ctx.pending.unlink(missing_ok=True)
                source, reason = _route_to_menu(ctx, turn_root / "control_resume", summary, save_summary)
                continue
            except Exception as exc:
                _atomic_write_json(turn_root / "pipeline_exception.json", {
                    "error": f"{type(exc).__name__}:{exc}", "turn_index": turn,
                    "run_session_id": ctx.run_id,
                })
                # SS is persisted immediately by its executor, before later route work.
                # Recover only that explicit receipt; never infer a training action.
                new_ss = [entry["receipt"] for entry in _read(ctx.roster).get("ss_execution_participants", [])
                          if entry.get("source_sha256") not in before_ss
                          and _valid_ss_receipt(entry.get("receipt", {}), ctx, turn)]
                recovered_outing = _new_persisted_outing(ctx, turn, before_outing)
                if new_ss and recovered_outing is not None:
                    raise ValueError("multiple_consuming_action_receipts") from exc
                if recovered_outing is not None:
                    row.update(kind="outing", execution=recovered_outing)
                    commit_consumption(row, recovered_outing)
                elif len(new_ss) == 1:
                    row["kind"] = "ss"
                    commit_consumption(row, new_ss[0])
                if recovered_outing is not None or len(new_ss) == 1:
                    row["post_action_exception_warning"] = f"{type(exc).__name__}:{exc}"
                    ctx.pending.unlink(missing_ok=True)
                    save_summary()
                    source, reason = _route_to_menu(ctx, turn_root / "after_observation_error",
                        summary, save_summary, post_action=True)
                    continue
                raise
            _atomic_write_json(turn_root / "pipeline_report.json", {"code": code, "report": report}, buffered_diagnostic=True)
            row.update(pipeline_status=report.get("status"), pipeline_code=code)
            _record_pipeline_growth_failure(summary, row, report, turn)
            if code == 0 and report.get('status') == 'ss_sieve_return_home' and report.get('turn_consumed') is False:
                source, reason = _ss_sieve_return_home(ctx, turn_root, report['ss_sieve_evidence'],
                    summary, save_summary)
                if source is None:
                    break
                continue
            if (report.get('control_boundary')=='larc_ai_pause'
                    or (report.get('stages',{}).get('live_loop') or {}).get('control_boundary')=='larc_ai_pause'):
                raise LarcAIPause({'reason':'training_ai_unavailable'})
            if code == 0 and report.get("status") in {"larc_growth_recollect", "larc_ss_information_recollect"}:
                ctx.pending.unlink()
                if report.get("status") == "larc_growth_recollect":
                    ctx.growth_recollections[turn] = retry + 1
                    recollect_dir = "growth_after"
                else:
                    row["ss_information_recollect"] = True
                    recollect_dir = "ss_information_after"
                source, reason = _route_to_menu(ctx, turn_root / recollect_dir, summary, save_summary)
                continue  # Collect fresh previews after purchases or page recovery.
            kind, receipt, failure = _action_result(report, ctx, turn)
            if kind is None:
                recovered_outing = _new_persisted_outing(ctx, turn, before_outing)
                if recovered_outing is not None:
                    kind, receipt, failure = "outing", recovered_outing, "mei_outing_post_action_failed"
            if kind is None:
                cut_hook=getattr(ctx,'cut_farming_hook',None)
                if (cut_hook is not None and code==0
                        and callable(getattr(cut_hook,'observe_supporter_deadline',None))
                        and cut_hook.observe_supporter_deadline(ctx,report)):
                    # No consuming action was selected. Return through the
                    # normal page owner, then check cut before authorizing the
                    # existing discard route; never abandon on the SS preview.
                    row['supporter_deadline']=report['goal_deadline']
                    ctx.pending.unlink(missing_ok=True)
                    save_summary()
                    source,reason=_route_to_menu(ctx,turn_root/'supporter_deadline_home',summary,save_summary)
                    continue
                handoff = pipeline_page_handoff(report)
                if handoff is not None:
                    if outing_replan_handoff(report):
                        replan=accept_outing_replan(ctx,report)
                        if replan is None:
                            reason='mei_outing_replan_unverified'
                            _record_terminal_pipeline_failure(summary,row,report,code)
                            break
                        row['execution_reason']=replan
                        ctx.pending.unlink(missing_ok=True)
                        save_summary()
                        # Re-enter the original home/identity owner. This is not
                        # a training-entry attempt and spends no 30s page budget.
                        source,reason=_route_to_menu(ctx,turn_root/'outing_replan',summary,save_summary)
                        control_reobservations[turn]=control_reobservations.get(turn,0)+1
                        continue
                    import cv2
                    row["page_handoff"] = handoff
                    ctx.pending.unlink(missing_ok=True)
                    budget = handoff_budgets.setdefault(turn, _career_transition_budget())
                    frame_path = handoff.get("source_path")
                    frame = cv2.imread(str(frame_path)) if frame_path and Path(frame_path).is_file() else None
                    budget.observe(image=frame)
                    budget_exhausted = budget.expired
                    if budget_exhausted or page_retries.get(turn, 0) >= 30:
                        reason = "training_entry_no_progress_timeout"
                        _record_wait_stop(ctx, summary, budget, reason, turn_root / 'pipeline.json',
                            surface=handoff.get('surface'), owner='training_entry',
                            trigger='budget_expired' if budget_exhausted else 'retry_limit',
                            retry_count=page_retries.get(turn, 0))
                        break
                    page_retries[turn] = page_retries.get(turn, 0) + 1
                    if frame_path and Path(frame_path).is_file():
                        _await_late_career_page(ctx, frame_path)
                    source, reason = _route_to_menu(ctx, turn_root / "page_changed", summary, save_summary, post_action=True)
                    continue  # Discard the old state and training choice; read the actual home again.
                ctx.pending.unlink()
                execution = report.get("stages", {}).get("live_loop", {}).get("execution", {})
                retry_reason = _pre_action_reading_retry_reason(report, page_retries.get(turn, 0))
                if retry_reason is not None:
                    # These stages precede every consuming action. A single
                    # bad capture/reading does not terminate the whole career;
                    # return through the normal UI owner and try a fresh read.
                    page_retries[turn] = page_retries.get(turn, 0) + 1
                    row["pre_action_reading_retry"] = retry_reason
                    source, reason = _route_to_menu(ctx, turn_root / "reading_retry",
                        summary, save_summary)
                    continue
                reason = str(execution.get("reason") or report.get("status", "pipeline_did_not_execute"))
                _record_terminal_pipeline_failure(summary, row, report, code)
                break
            row.update(kind=kind, execution=receipt)
            # Keep the raw execution even if persisting its route receipt fails.
            _atomic_write_json(turn_root / "execution.json", receipt)
            if kind in {"train", "rest", "normal_outing"}:
                recorder = _record_training_route_receipt if kind == "train" else _record_rest_route_receipt
                route_receipt = recorder(args=SimpleNamespace(route_transition_json=str(ctx.route),
                                                              run_session_id=ctx.run_id), execution=receipt)
                _atomic_write_json(turn_root / "route_receipt.json", route_receipt)
            commit_consumption(row, receipt)
            if getattr(args, "training_records", None) == "keep":
                from core.auto_trainer.training_review import archive_training_turn
                try:
                    row["training_review"] = archive_training_turn(ctx.root, row, report)
                except (OSError, ValueError, KeyError) as exc:
                    row["training_review_warning"] = str(exc)
            if failure and failure not in {"ss_post_action_failed", "mei_outing_post_action_failed"}:
                reason = failure
                break
            ctx.pending.unlink()
            if failure or code or report.get("is_valid") is not True:
                # The confirmed consuming receipt is already persisted above.
                # A failed post-animation observation is not permission to
                # replay it, nor a reason to kill the whole run. Observe the
                # next actual page through the existing bounded route owner.
                row["post_action_observation_warning"] = failure or str(report.get("status"))
                save_summary()
            source, reason = _route_to_menu(ctx, turn_root / "after", summary, save_summary,
                                            post_action=kind in {"train", "rest", "ss", "outing", "normal_outing"})
            if args.training_records == "discard" and source is not None and reason == "ready_main_menu":
                try:
                    from core.auto_trainer.training_review import discard_consumed_training_images
                    row["training_image_cleanup"]=discard_consumed_training_images(ctx.root,row)
                    if row["training_image_cleanup"].get("removed",0)>0:
                        parser.refresh_larc_roster(ctx.run_id)
                except Exception as exc:
                    row["training_image_cleanup_warning"]=type(exc).__name__
                save_summary()
            if (summary["status"] == "running" and summary["consumed_turns_this_run"] >= args.max_actions
                    and reason in {"race_day_main_menu", "batch_limit_reached"}):
                summary.update(status="batch_limit_reached", stop_reason="batch_limit_reached")
                break
        if summary["status"] == "running":
            _stop(summary, reason)
    except LarcAIPause as exc:
        from core.auto_trainer.larc_ai_nodes import pause_summary
        ctx.pending.unlink(missing_ok=True)
        pause_summary(current(),summary,exc,save_summary)
    except ResumeFromCurrentPage:
        summary.update(status="reattach", stop_reason="player_resume_current_page")
    except RunStopped as exc:
        summary.update(status="stopped", stop_reason=exc.reason, stopped_by_control=True)
        if exc.reason == "manual_final_arc_finish_handoff":
            from core.auto_trainer.larc_manual_handoff import snapshot as manual_handoff_snapshot
            handoff = manual_handoff_snapshot(ctx.root)
            if (handoff.get("status") == "handed_off" and handoff.get("terminal") is True
                    and handoff.get("reason") == exc.reason):
                # The configured final-Arc handoff ends script ownership, not
                # the career itself. Keep actual control stops as failures.
                summary.update(status="handoff", handoff="manual", stopped_by_control=False)
    except MeiOutingInspectionError as exc:
        _record_outing_inspection_failure(summary, exc)
    except Exception as exc:
        from core.auto_trainer.stop_screenshot_evidence import safe_capture_failure
        failure=safe_capture_failure(getattr(exc,'capture_failure',None)or getattr(ctx,'capture_failure',None))
        stop_reason = (
            "capture_failed"
            if failure and str(exc) in {"live_takeover_capture_failed", "live_takeover_adb_connect_failed"}
            else "execution_exception"
        )
        _stop(summary, stop_reason)
        summary["error"] = f"{type(exc).__name__}:{exc}"
        if failure:summary['capture_failure']=failure
    finally:
        # Freeze the already observed frame before closing probes/journals.
        # Submission never captures the user's later game state.
        stopped_frame = stop_frames.freeze() if stop_frames is not None else None
        if stop_frames is not None:
            stop_frames.close()
        probe = getattr(ctx, "transition_probe", None)
        if probe is not None:
            probe.close()
        journal = getattr(ctx, "passive_events", None)
        if journal is not None:
            journal.close()
            _drain_passive_events(ctx, summary)
        # Keep the failed reader's bound frame before disposable images go.
        # Reporting never alters this segment's stop/resume/abandon decision.
        if (args.execute and getattr(ctx, 'cut_farming_hook', None) is None
                and summary.get('stop_reason') != 'ss_sieve_confirmed_failure'
                and not (summary.get('stop_reason') == 'continuation_gate_failed'
                         and (ctx.root / 'session/ace_continuation_failure.json').is_file())):
            summary['stop_diagnostic']={'status':'unavailable','run_id':ctx.run_id,
                'segment_id':output.name,'reason':summary.get('stop_reason'),
                'stop_recorded_at':datetime.now(timezone.utc).isoformat()}
            try:
                from core.auto_trainer.cut_stop_diagnostic import save_ordinary_stop_diagnostic
                from core.auto_trainer.recognition_failure_archive import archive_abnormal_stop
                summary['stop_diagnostic']=save_ordinary_stop_diagnostic(ctx.root,ctx.port,summary,
                    last_game_frame=stopped_frame or getattr(ctx,'last_game_frame',None),
                    capture_failure=summary.get('capture_failure')or getattr(ctx,'capture_failure',None))
                if summary['stop_diagnostic'].get('status')in {'ready','unavailable'}:
                    summary['abnormal_stop_archive']=archive_abnormal_stop(ctx.root,
                        {'status':'route_failed','stop':{'reason':summary.get('stop_reason'),'run_id':ctx.run_id}},summary=summary)
            except Exception as error:
                summary['stop_diagnostic'].update(status='unavailable',error=type(error).__name__,
                    capture_error_reason='diagnostic_save_failed')
        try:
            from core.auto_trainer.recognition_failure_archive import archive_segment_failure, complete_run_feedback
            summary['final_progress'] = deepcopy(ctx.progress)
            summary['recognition_failure_archive'] = archive_segment_failure(ctx.root, summary)
            if (summary.get('status') == 'completed' and summary.get('career_complete') is True
                    and summary.get('completion_scope') == 'normal_settlement'):
                complete_run_feedback(ctx.root, 'completed')
            elif summary.get('stopped_by_control') is True:
                complete_run_feedback(ctx.root, 'operator_stop')
        except Exception as error:
            summary['recognition_failure_archive'] = {'status': 'archive_unavailable', 'error': type(error).__name__}
        if getattr(args, "training_records", "keep") == "discard":
            from core.auto_trainer.training_review import discard_completed_training_images
            try:
                summary["working_training_images_removed"] = discard_completed_training_images(ctx.root, summary["turns"])
            except (OSError, ValueError, KeyError) as exc:
                summary["training_cleanup_warning"] = str(exc)
        if (getattr(args,"training_records",None)=="discard"
                and summary.get("status")=="completed" and summary.get("career_complete") is True
                and summary.get("completion_scope")=="normal_settlement"):
            try:
                from core.auto_trainer.pacer_storage import maintain
                summary["completed_image_retention"]=maintain(
                    Path(__file__).resolve().parents[1],_completed_run=ctx.root)
            except Exception as error:
                summary["completed_image_retention"]={"errors":1,"warning":type(error).__name__}
        if current() is not None and not getattr(args,'_keep_run_control',False):
            current().finish()
        _notify_career_outcome(ctx, summary)
        save_summary(final=True)
    return summary, 0 if summary["status"] in {"batch_limit_reached", "completed", "year_checkpoint", "handoff"} else 2


def main(argv=None):
    summary, code = run(parse_args(argv))
    print(json.dumps(summary, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
