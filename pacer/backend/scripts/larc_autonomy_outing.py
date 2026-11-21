"""Narrow bridge between the existing L'Arc runner and Mei's outing route."""
from pathlib import Path

import cv2

from core.native_capture_adb import adb_for_capture
from core.larc_outing_reader import read_friend_outing_hint
from core.auto_trainer.larc_friend_outing import CHARACTER, SCHEMA, domestic_period, verified_source
from core.auto_trainer.larc_outing_route import (MeiOutingRoute, menu_identity, friend_badge_available,
    menu_identity_conflicts, menu_recollection_allowed)
from core.auto_trainer.larc_outing_session import MeiOutingSession, observe_unlock_report
from core.auto_trainer.main_menu_state_provider import provide_main_menu_state
from core.auto_trainer.run_support_profile import attach_profile
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json


class MeiOutingEventHandoff(Exception):
    """Return a non-consuming inspection page to the existing event owner."""
    def __init__(self, report, report_path):
        super().__init__("mei_outing_event_handoff")
        self.report = report
        self.report_path = str(report_path)


class MeiOutingMenuHandoff(Exception):
    """Discard optional inspection state; let the original home owner recheck."""
    def __init__(self, report, report_path):
        super().__init__('mei_outing_menu_replan')
        self.report=report
        self.report_path=str(report_path)


class MeiOutingInspectionError(ValueError):
    """Keep the route's actual last frame available to stop-report capture."""
    def __init__(self, report, report_path, *, message=None):
        super().__init__(message or "mei_outing_inspection:" + report.get("reason", "unknown"))
        self.report = report
        self.report_path = str(report_path)


_REPLAN_REASONS = frozenset({'mei_outing_menu_recollect','mei_outing_friend_entry_not_observed',
    'mei_outing_progress_recollect'})


def _replan_scope(ctx, menu):
    period=menu.get('period') or {}
    return (str(ctx.root.resolve()),ctx.run_id,ctx.progress['next_turn'],ctx.progress.get('device'),
        *(menu.get(k) for k in ('character_id','trainee_id','career_id','career_epoch','consumption_epoch')),
        *(period.get(k) for k in ('year_index','month','half','pre_debut')),menu.get('remaining_turns'),
        ctx.progress.get('consumption_epoch'))


def _replan_state(ctx, menu):
    scope=_replan_scope(ctx,menu);value=getattr(ctx,'_mei_optional_replan',None)
    if not isinstance(value,dict) or value.get('scope')!=scope:
        value={'scope':scope,'failures':0};ctx._mei_optional_replan=value
    return value


def _note_replan(ctx, report):
    value=_replan_state(ctx,report['returned_main_menu_identity'])
    # A positively missing badge or a conflicting ledger cannot become a fresh
    # available candidate by repeating the same optional inspection.
    value['failures']=2 if report.get('candidate_unavailable') else min(2,value['failures']+1)
    return {'reason':report['reason'],'temporarily_excluded':value['failures']>=2,
        'message':'本回合暂不选外出，按当前画面重新比较其他合法行动。' if value['failures']>=2
            else '旧外出候选已撤销，按当前画面重新规划。'}


def _unknown_outing(source, reason):
    return {'schema_version':SCHEMA,'status':'unknown','character':CHARACTER,
        'source':source,'reason':reason}


def _excluded_outing(ctx, session, menu_source):
    packet = _unknown_outing(menu_source, 'mei_outing_temporarily_excluded')
    turn = ctx.progress['next_turn']
    if (session.data['run_session_id'] != ctx.run_id
            or not menu_recollection_allowed({'returned_source': menu_source}, session)
            or not verified_source(menu_source, run_id=ctx.run_id, turn=turn)):
        return packet
    evidence = session.data.get('source') or session.data.get('unlock_source')
    if session.verified_historical_source(evidence, run_root=ctx.root, current_turn=turn):
        packet['confirmed_progress'] = {'unlocked': True, 'completed': session.data['completed'],
                                        'progress_source': dict(evidence)}
    profile = getattr(ctx, 'support_profile', None)
    if isinstance(profile, dict) and profile.get('run_session_id') == ctx.run_id:
        try:
            return attach_profile(packet, profile, ctx.run_id)
        except ValueError:
            pass  # Malformed deck evidence cannot stop this non-consuming fallback.
    return packet


def outing_replan_handoff(report):
    execution=report.get('stages',{}).get('live_loop',{}).get('execution',{})
    return (execution.get('reason') in _REPLAN_REASONS
        and execution.get('page_handoff',{}).get('surface')=='main_menu')


def accept_outing_replan(ctx, report):
    """Accept only this run's confirmed non-consuming optional-route return."""
    execution=report.get('stages',{}).get('live_loop',{}).get('execution',{})
    source=execution.get('returned_source');menu=execution.get('returned_main_menu_identity')
    if (not outing_replan_handoff(report) or execution.get('optional_outing_replan') is not True
            or execution.get('returned_main_menu_confirmed') is not True
            or not isinstance(menu,dict) or not verified_source(source,run_id=ctx.run_id,
                turn=ctx.progress['next_turn'])):
        return None
    try:
        if not Path(source['path']).resolve().is_relative_to(ctx.root.resolve()):return None
    except (KeyError,TypeError,OSError,ValueError):return None
    if menu_identity_conflicts(menu_identity(getattr(ctx,'event_state',{}) or {}),menu):return None
    if menu.get('device') is not None and menu['device']!=ctx.progress.get('device'):return None
    session=MeiOutingSession.load(ctx.root/'session/larc_friend_outing.json',ctx.run_id)
    navigation=any(s.get('decision',{}).get('reason')=='mei_outing.open' for s in execution.get('steps',[]))
    if not menu_recollection_allowed(execution,session,friend_navigation=navigation):return None
    return _note_replan(ctx,execution)


def observe_mei_unlock(ctx, report):
    path = ctx.root / "session/larc_friend_outing.json"
    session = MeiOutingSession.load(path, ctx.run_id)
    if observe_unlock_report(session, report, turn=ctx.progress["next_turn"]):
        session.save(path)


def needs_outing_probe(ctx, session, provider, *, friend_hint=None):
    """Probe only a current friend chooser entry, never an ordinary outing."""
    turn = ctx.progress["next_turn"]
    if not friend_badge_available(friend_hint):
        return False
    if (turn != session.data.get("last_inspection_turn")
            and not session.data.get("last_inspection_had_friend_badge")):
        return True  # A newly visible friend badge can supersede an old miss.
    if turn < 3 or turn - session.data.get("last_inspection_turn", -4) < 4:
        return False
    return True


def prepare_mei_outing(ctx, source, provider, output_dir):
    path = ctx.root / "session/larc_friend_outing.json"
    session = MeiOutingSession.load(path, ctx.run_id)
    menu_source = {"run_session_id": ctx.run_id, "turn_index": ctx.progress["next_turn"],
                   "path": str(Path(source).resolve()), "sha256": provider.get("source", {}).get("sha256")}
    # This saved current menu frame already exists; no capture/OCR is added.
    # A ledger unlock or old event does not establish today's chooser entrance.
    friend_hint = read_friend_outing_hint(cv2.imread(str(source)))
    interrupted_return = False
    try:
        if session.confirm_return(menu_identity(provider["state_context"]), menu_source):
            session.save(path)
    except ValueError as exc:
        pending = session.data.get("pending")
        interrupted_selection = (str(exc) == "mei_outing_return_receipt_unverified"
            and isinstance(pending, dict) and pending.get("tap_performed") is not True
            and verified_source(menu_source, run_id=ctx.run_id)
            and menu_source["turn_index"] >= pending["turn_index"])
        if ((str(exc) != "mei_outing_return_did_not_advance" and not interrupted_selection)
                or not friend_badge_available(friend_hint)):
            raise
        # Never repeat the outing from a tap receipt alone. Inspect/cancel the
        # real chooser once to resolve whether the server applied that input.
        interrupted_return = True
    if not domestic_period(provider.get("larc_period")):
        return None, source, provider
    if session.data["completed"] == 5:
        packet = _excluded_outing(ctx, session, menu_source)
        return (packet if 'confirmed_progress' in packet or 'support_card_profile' in packet else None), source, provider
    if _replan_state(ctx,menu_identity(provider['state_context']))['failures']>=2:
        if not menu_recollection_allowed({'returned_source':menu_source},session):
            raise ValueError('mei_outing_replan_state_changed')
        return _excluded_outing(ctx, session, menu_source), source, provider
    if not friend_badge_available(friend_hint):
        packet = _excluded_outing(ctx, session, menu_source)
        return (packet if 'confirmed_progress' in packet or 'support_card_profile' in packet else None), source, provider
    if session.data["unlocked"] and not interrupted_return:
        # Progress comes from the run ledger. The executor rechecks the actual
        # chooser when selected, so ordinary turns need no menu round trip.
        evidence = session.data.get("source") or session.data.get("unlock_source")
        historical=session.verified_historical_source(evidence,run_root=ctx.root,current_turn=ctx.progress['next_turn'])
        if not historical or not verified_source(menu_source, run_id=ctx.run_id):
            raise ValueError("mei_outing_ledger_source_unverified")
        packet = {"schema_version": SCHEMA, "status": "accepted", "character": CHARACTER,
            "unlocked": True, "completed": session.data["completed"], "pending": None,
            "source": menu_source, "progress_source": evidence,
            "session_path": str(path.resolve()), "transition_session_path": str(Path(ctx.route).resolve()),
            "menu_snapshot": menu_identity(provider["state_context"])}
        return attach_profile(packet, getattr(ctx, "support_profile", None), ctx.run_id), source, provider
    # Reuse the already saved current menu image. No extra capture/OCR, and a
    # generic badge never invents Mei's identity or number of used outings.
    if not interrupted_return and not needs_outing_probe(ctx, session, provider, friend_hint=friend_hint):
        return None, source, provider
    report = MeiOutingRoute(adb=adb_for_capture(ctx.port)).run(run_id=ctx.run_id, turn=ctx.progress["next_turn"],
        session_path=path, transition_path=ctx.route, output_dir=output_dir, mode="live",
        execute_training_confirm=True, confirm_token="UG_PARENT_AUTOTRAINER_SANDBOX", max_turns=1,
        expected_menu=menu_identity(provider["state_context"]))
    if report.get("is_valid") is True and report.get("packet") is not None:
        report["packet"] = attach_profile(report["packet"], getattr(ctx, "support_profile", None), ctx.run_id)
    _atomic_write_json(Path(output_dir) / "report.json", report)
    if report.get("page_handoff") == "event_choice" and not report.get("turn_consumed"):
        raise MeiOutingEventHandoff(report, Path(output_dir) / "report.json")
    if report.get("is_valid") is not True:
        raise MeiOutingInspectionError(report, Path(output_dir) / "report.json")
    if report.get('optional_outing_replan') is True:
        _note_replan(ctx,report)
        raise MeiOutingMenuHandoff(report,Path(output_dir)/'report.json')
    session = MeiOutingSession.load(path, ctx.run_id)
    if (report.get("inspection_status") != "unknown"
            and report.get("reason") != "mei_outing_inspection_deferred"):
        # A safely deferred read has not inspected this newly visible friend.
        # Preserve the prior badge marker so the next turn may probe again.
        session.data["last_inspection_had_friend_badge"] = friend_hint.get("available") is True
        session.save(path)
    source = report["returned_main_menu_path"]
    provider = provide_main_menu_state(source, Path(output_dir) / "returned_main_menu.json",
        turn_index=ctx.progress["next_turn"], scenario="larc")
    if provider.get("valid_for_training") is not True:
        report.update(is_valid=False, reason="mei_outing_return_provider_unverified",
            stage="rebuilding_return_provider",
            failure_source=report.get("returned_source") or report.get("last_source"))
        _atomic_write_json(Path(output_dir) / "report.json", report)
        raise MeiOutingInspectionError(report, Path(output_dir) / "report.json",
            message="mei_outing_return_provider_unverified")
    # The caller's old menu observation was before navigation. Rebind it to
    # the returned frame so supporter deadlines keep their original authority.
    from core.auto_trainer.frame_safety import FrameSafetyAnalyzer
    safety = FrameSafetyAnalyzer().analyze(Path(source))
    ctx.menu_observation = {"screenshot_path": source, "result": {
        "frame_safety": safety.to_dict(), "decision": {"evidence": {
            "screen_text": {"normalized_text": safety.visible_text}}}}}
    packet=report['packet']
    return packet, source, provider
