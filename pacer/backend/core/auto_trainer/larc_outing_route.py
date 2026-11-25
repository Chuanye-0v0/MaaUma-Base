"""Bounded, guarded navigation and execution of one observed Mei outing."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import math
from pathlib import Path
import time

import cv2

from core.auto_trainer.executor import validate_training_execution_gate
from core.auto_trainer.frame_safety import FrameSafetyAnalyzer, FormalActionSafetyGate
from core.auto_trainer.larc_friend_outing import CHARACTER, domestic_period, verified_source
from core.auto_trainer.larc_outing_session import MeiOutingSession
from core.auto_trainer.main_menu_state_provider import provide_main_menu_state
from core.auto_trainer.route_transition_session import RouteTransitionSession, POST_ACTION_ROUTE_SUCCESSORS
from core.coordinates import CoordinateConfig
from core.larc_outing_reader import MeiOutingReader
from core.training_menu_precheck import TrainingMenuPrechecker
from core.execution_frames import ExecutionFrames


MEI_OUTING_LOADING_EXTRA_WAITS = 20
MEI_OUTING_LOADING_WAIT_SECONDS = 0.6


def menu_identity(state):
    return {key: state.get(key) for key in ("current_stats", "remaining_turns", "target_text", "vitality")} | {
        "period": state.get("scenario_state", {}).get("larc_period", {}).get("value"),
        "mood": state.get("scenario_state", {}).get("mood"),
        **{key: state[key] for key in ('run_session_id','device','character_id','trainee_id',
            'career_id','career_epoch','consumption_epoch') if state.get(key) is not None}}


def menu_identity_conflicts(expected, observed):
    """A stale optional quote cannot override a known career/device conflict."""
    return (isinstance(expected,dict) and isinstance(observed,dict) and any(
        expected.get(key) is not None and observed.get(key) is not None
        and expected[key] != observed[key] for key in ('run_session_id','device','character_id',
            'trainee_id','career_id','career_epoch','consumption_epoch')))


def menu_information_matches(expected, observed):
    """Keep known facts exact; tolerate one percentage point of vitality-bar jitter."""
    if expected is None:return True
    if isinstance(expected,dict):
        if not isinstance(observed,dict):return False
        for key,value in expected.items():
            actual=observed.get(key)
            if key=='vitality' and type(value) in {int,float}:
                if (type(actual) not in {int,float} or not 0<=value<=100
                        or not 0<=actual<=100 or abs(value-actual)>1.0):
                    return False
            elif not menu_information_matches(value,actual):
                return False
        return True
    return expected==observed


def same_career_position(expected, observed):
    """Cancel-only navigation must return to the observed original turn."""
    return (isinstance(expected,dict) and isinstance(observed,dict)
        and type(expected.get('remaining_turns')) is int
        and expected['remaining_turns']==observed.get('remaining_turns')
        and isinstance(expected.get('period'),dict) and bool(expected['period'])
        and expected['period']==observed.get('period'))


def friend_badge_available(hint):
    """The current red/pink friend marker proves chooser entry, not identity."""
    return (isinstance(hint,dict) and hint.get('status')=='accepted'
        and hint.get('available') is True)


def menu_information_diff(expected, observed):
    """Small game-state diagnostics; no raw target text, paths or user settings."""
    def groups(menu):
        menu = menu if isinstance(menu, dict) else {}
        stats = menu.get('current_stats') or {}
        stats = stats if isinstance(stats, dict) else {}
        return {'stats': {key: stats.get(key) for key in ('speed','stamina','power','guts','wisdom')},
            'skill_pt': stats.get('skill_pt'), **{key: menu.get(key) for key in
                ('remaining_turns','target_text','vitality','period','mood')}}
    def safe(value):
        if value is None or type(value) is bool or type(value) is int:
            return value
        if type(value) is float:
            return value if math.isfinite(value) else None
        if isinstance(value, dict):
            return {key: safe(value.get(key)) for key in (
                'speed','stamina','power','guts','wisdom','year_index','month','half','stage','pre_debut') if key in value}
        if isinstance(value,str) and value in {'first','second','junior','classic','senior','great','excellent','good','normal','bad','very_bad','poor','awful'}:
            return value
        if isinstance(value, str):
            return {'known': bool(value), 'length': len(value),
                'text_sha256': hashlib.sha256(value.encode('utf8')).hexdigest()[:16]}
        return {'status': 'invalid'}
    before, after = groups(expected), groups(observed)
    fields = {}
    for key, value in before.items():
        # Retain the same asymmetric unknown-value and vitality-jitter rules.
        matched = menu_information_matches({key:value}, {key:after[key]})
        def projected(item):
            if key == 'target_text' and isinstance(item,str):
                return {'known': bool(item), 'length': len(item),
                    'text_sha256': hashlib.sha256(item.encode('utf8')).hexdigest()[:16]}
            return safe(item)
        fields[key] = {'same': matched, 'expected': projected(value), 'observed': projected(after[key])}
    return {'changed_fields': [key for key, row in fields.items() if not row['same']], 'fields': fields}


def menu_recollection_allowed(report, session, *, pending_at_start=False, friend_navigation=False):
    """Only confirmed non-consuming navigation before any selection/pending."""
    if (pending_at_start or session.data['pending'] is not None or any(report.get(key) for key in
            ('performed','tap_performed','turn_consumed','input_may_have_occurred','input_receipts',
             'outing_receipt','selection_reserved','input_attempted','has_pending_operation','pending_operation'))
            or report.get('tap_outcome') in {'failed','unknown','pending'}):
        return False
    turn=(report.get('returned_source') or report.get('last_source') or {}).get('turn_index')
    if type(turn) is int and any(isinstance(row,dict)
            and row.get('run_session_id')==session.data['run_session_id']
            and type(row.get('turn_index')) is int and row['turn_index']>=turn
            for row in session.data.get('receipts',[])):
        return False  # An already recorded current action belongs to its recovery owner.
    allowed = {'mei_outing.back_to_menu', 'mei_outing.close_ss_info'}
    if friend_navigation:
        if not friend_badge_available(report.get('friend_badge_observation')):
            return False
        allowed.update({'mei_outing.open','mei_outing.cancel'})
    for step in report.get('steps', []):
        if (any(step.get(key) for key in ('input_may_have_occurred','input_receipts'))
                or step.get('tap_outcome') in {'failed','unknown','pending'}):
            return False
        if step.get('input_attempted') or step.get('tap_performed'):
            if (step.get('input_attempted') is not True
                    or step.get('decision', {}).get('reason') not in allowed
                    or step.get('tap_performed') is not True):
                return False
    return True


class MeiOutingRoute:
    def __init__(self, *, adb, reader=None, safety=None, gate=None, menu_reader=None,
                 training_prechecker=None, sleep_fn=time.sleep, control_driver=None):
        self.adb = adb
        self.reader = reader or MeiOutingReader()
        self.safety = safety or FrameSafetyAnalyzer()
        self.gate = gate or FormalActionSafetyGate()
        self.menu_reader = menu_reader or provide_main_menu_state
        self.training = training_prechecker or TrainingMenuPrechecker(scenario="larc")
        self.sleep = sleep_fn
        self.control = control_driver

    def run(self, *, run_id, turn, session_path, transition_path, output_dir,
            mode, execute_training_confirm, confirm_token, max_turns,
            candidate=None, expected_menu=None):
        report = {"schema_version": "larc_outing_route.v1", "performed": False,
            "tap_performed": False, "turn_consumed": False, "is_valid": False,
            "reason": "outing_not_started", "target": None, "steps": [], "tap_plan": [],
            "stage": "preflight", "last_source": None, "has_pending_operation": False}
        allowed, why = validate_training_execution_gate(mode=mode,
            execute_training_confirm=execute_training_confirm, confirm_token=confirm_token, max_turns=max_turns)
        if not allowed or mode != "live":
            return dict(report, reason=why)
        session = MeiOutingSession.load(session_path, run_id)
        pending_at_start = session.data["pending"] is not None
        report["has_pending_operation"] = pending_at_start
        report["inspection_only"] = candidate is None
        if candidate is not None and not session.data["unlocked"]:
            return dict(report, reason="mei_outing_unlock_unknown")
        if candidate is not None:
            evidence=session.data.get('source') or session.data.get('unlock_source')
            if not (verified_source(evidence,run_id=run_id,turn=turn)
                    or session.verified_historical_source(evidence,
                        run_root=Path(session_path).resolve().parent.parent,current_turn=turn)):
                return dict(report, reason="mei_outing_unlock_source_unverified")
        if candidate is not None and (not verified_source(candidate.get("source"), run_id=run_id, turn=turn)
                or candidate.get("completed") != session.data["completed"] or session.data["pending"] is not None):
            return dict(report, reason="mei_outing_candidate_stale")
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=False)
        route = RouteTransitionSession.load(transition_path, expected_run_session_id=run_id)
        sequence = 0
        frames = ExecutionFrames(self.adb)

        def capture():
            nonlocal sequence
            report["last_source"] = None
            if self.control is not None:
                self.control.reach("before_capture")
            path = root / f"frame_{sequence:02d}.png"
            sequence += 1
            image = frames.screenshot(str(path))
            if image is None or not path.is_file():
                raise ValueError("mei_outing_capture_failed")
            source = {"run_session_id": run_id, "turn_index": turn, "path": str(path.resolve()),
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            report["last_source"] = source
            if image is None or (image.shape[1], image.shape[0]) not in {(720, 1280), (1440, 2560)}:
                raise ValueError("mei_outing_capture_size")
            return path, image, source

        def observe(screen, source, *, resync=False):
            try:
                route.observe(screen, source_sha256=source["sha256"], resync=resync)
            finally:
                route.save(transition_path)

        def handoff_event(stage, image, source):
            # Inspection has not selected an outing. A new character event
            # belongs to the normal event owner, never to this chooser reader.
            if candidate is not None or session.data["pending"] is not None:
                return False
            from scripts.larc_transition_probe import actionable_surface
            if actionable_surface(image) != "event_choice":
                return False
            if stage == "before_open" and route.current_state == "main_menu" and not route.expected_next_states:
                route.commit_action("wait:mei_outing_event", expected_next_states=("event_choice",),
                                    source_sha256=route.observation_sha256)
            observe("event_choice", source)
            report.update(reason="mei_outing_event_handoff", page_handoff="event_choice",
                          handoff_stage=stage, handoff_source=source)
            return True

        def tap(action_id, point, screen, successors, path, source, *, reserve=False, consume=False, resync=False, supplied_text=None):
            if point is None:
                raise ValueError("mei_outing_control_unverified")
            decision = {"action": "tap", "reason": action_id, "route_state": screen,
                        "tap": {"coordinate_name": action_id, "x": point[0], "y": point[1], "scaled": False}}
            safety = self.safety.analyze(path, supplied_text=supplied_text) if supplied_text is not None else self.safety.analyze(path)
            verdict = self.gate.evaluate(safety, decision)
            step = {"source": source, "decision": decision, "safety": verdict.to_dict(), "tap_performed": False}
            report["steps"].append(step)
            if not verdict.allowed or safety.source_image_sha256 != source["sha256"]:
                raise ValueError("mei_outing_safety:" + verdict.reason)
            if not verified_source(source, run_id=run_id, turn=turn):
                raise ValueError("mei_outing_source_changed")
            if self.control is not None:
                self.control.reach("before_tap")
            observe(screen, source, resync=resync)
            if reserve:
                report["selection_reserved"] = True
                session.reserve(candidate["completed"] + 1, source, menu_snapshot=menu)
                session.save(session_path)  # Reserve before the possibly consuming input.
            step["input_attempted"] = True
            ok = frames.tap(*point, scaled=False)
            if consume:
                receipt = session.record_tap(ok is True)
                if receipt is not None:
                    report.update(performed=True, tap_performed=True, turn_consumed=True,
                        reason="executed_mei_outing", target=f"satake_mei:{receipt['stage']}",
                        outing_receipt=receipt, required_event_choice="top" if receipt["stage"] == 3 else None)
                session.save(session_path)
            if ok is not True:
                raise ValueError("mei_outing_input_unconfirmed")
            step["tap_performed"] = True
            report["tap_plan"].append(decision["tap"])
            route.commit_action(action_id, expected_next_states=successors, source_sha256=source["sha256"])
            route.save(transition_path)
            if getattr(self.adb, "supports_in_memory_capture", False) is True and action_id != "mei_outing.close_ss_info":
                if not consume:
                    from core.auto_trainer.larc_control_wait import wait_outing_controls
                    frames.pending_frame = wait_outing_controls(self.adb, frames.last_frame,
                        home="main_menu" in successors, confirm=action_id == "mei_outing.select", sleep=self.sleep,
                        event_handoff=action_id == "mei_outing.open" and candidate is None and session.data["pending"] is None)
            else:
                self.sleep(.35)

        def read_menu(path, *, badge_recheck=False):
            context = self.menu_reader(path, path.with_suffix(".state.json"), turn_index=turn, scenario="larc")
            state = context.get("state_context") if isinstance(context, dict) else None
            if isinstance(state, dict) and context.get("valid_for_training") is True:
                period = state.get("scenario_state", {}).get("larc_period", {})
                if period.get("status") == "unverified" and period.get("reason") == "anchor_date_unverified":
                    # The wide calendar crop can append a low-confidence extra
                    # glyph after the short pre-debut label. Read its existing
                    # tight ROI; never fill the date from the earlier snapshot.
                    from core.auto_trainer.main_menu_state_provider import _CALENDAR_REGION_READER
                    image = cv2.imread(str(path))
                    if image is not None and image.shape[:2] == (1280, 720):
                        text, confidence = _CALENDAR_REGION_READER.read_line(image[36:72, 155:280])
                        if "".join(str(text).split()) == "初级出道前" and confidence >= .95:
                            state["scenario_state"]["larc_period"] = {
                                "status": "verified", "value": {"pre_debut": True, "stage": "junior", "year_index": 1},
                                "source_frame_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                            report.setdefault("date_rechecks", []).append({"path": str(path),
                                "text": text, "confidence": confidence, "roi": [155, 36, 280, 72]})
            if not isinstance(state, dict) or context.get("valid_for_training") is not True:
                raise ValueError("mei_outing_main_menu_unverified_or_overseas")
            period = state.get("scenario_state", {}).get("larc_period")
            if not domestic_period(period):
                if badge_recheck and isinstance(period, dict) and period.get("status") == "verified":
                    raise ValueError("mei_outing_main_menu_changed")
                raise ValueError("mei_outing_main_menu_unverified_or_overseas")
            return menu_identity(state)

        def read_nav(image):
            nav = self.reader.read(image)
            if not isinstance(nav, dict) or nav.get('screen') != 'main_menu':
                raise ValueError('mei_outing_main_menu_unverified')
            return nav

        def recollect(reason, menu, path, source, *, unavailable=False):
            opened=any(s.get('decision',{}).get('reason')=='mei_outing.open' for s in report['steps'])
            if (not menu_recollection_allowed(report,session,pending_at_start=pending_at_start,
                    friend_navigation=opened)
                    or MeiOutingSession.load(session_path,run_id).data != session.data):
                raise ValueError('mei_outing_replan_input_unverified')
            observe('main_menu',source)
            report.update(reason=reason, execution_reason=reason, optional_outing_replan=True,
                candidate_unavailable=unavailable, returned_main_menu_confirmed=True,
                returned_main_menu_identity=menu, stage='main_menu_recollect',
                menu_recollect_required=True, returned_main_menu_path=str(path),
                returned_source=source, inspection_status='unknown')
            if candidate is None:
                report.update(is_valid=True,packet=None)
            else:
                report['page_handoff']={'phase':'execution_precheck','before_consuming_input':True,
                    'consuming_input_attempted':False,'input_attempted':bool(report['steps']),
                    'surface':'main_menu','source_path':str(path)}
            return report

        try:
            report["stage"] = "connecting"
            if not frames.connect():
                raise ValueError("mei_outing_adb_connect_failed")
            report["stage"] = "initial_capture"
            path, image, source = capture()
            if candidate is not None:
                checked = self.training.check(path)
                report["training_precheck"] = asdict(checked)
                if not checked.accepted:
                    from core.auto_trainer.larc_ss_information_probe import _close_only_navigation
                    from core.auto_trainer.larc_preview_text import default_local_reader
                    ss_close = _close_only_navigation(image)
                    if ss_close is not None and ss_close.get("page_verified") and ss_close.get("close") is not None:
                        need_resync = (
                            route.status.value != "mismatch"
                            and (
                                (route.expected_next_states and "larc_ss_information" not in route.expected_next_states)
                                or (not route.expected_next_states and route.current_state not in (None, "larc_ss_information"))
                            )
                        )
                        tap(
                            "mei_outing.close_ss_info",
                            [int(ss_close["close"][0]), int(ss_close["close"][1])],
                            "larc_ss_information",
                            ("training_menu",),
                            path,
                            source,
                            resync=need_resync,
                            supplied_text=ss_close["verified_text"] if default_local_reader(self.safety) else None,
                        )
                        report["closed_ss_information"] = {"close": list(ss_close["close"])}
                        for retry_idx in range(3):
                            path, image, source = capture()
                            checked = self.training.check(path)
                            report["training_precheck"] = asdict(checked)
                            if checked.accepted:
                                break
                            if retry_idx < 2:
                                self.sleep(.35)
                    if not checked.accepted:
                        raise ValueError("mei_outing_training_entry_unverified")
                x, y = CoordinateConfig().point("training_menu", "back")
                tap("mei_outing.back_to_menu", [round(x*image.shape[1]/1440), round(y*image.shape[0]/2560)],
                    "training_menu", ("main_menu",), path, source)
                path, image, source = capture()
            for attempt in range(4):
                report["stage"] = "main_menu"
                if handoff_event("before_open", image, source):
                    return report
                try:
                    menu = read_menu(path)
                    nav = read_nav(image)
                    break
                except ValueError:
                    if attempt == 3:
                        raise
                    self.sleep(.35)
                    path, image, source = capture()
            expected = candidate.get("menu_snapshot") if candidate is not None else expected_menu
            if menu_identity_conflicts(expected,menu):
                raise ValueError('mei_outing_identity_changed')
            if expected is not None and not menu_information_matches(expected,menu):
                comparison = {'schema_version': 'larc.outing_menu_comparison.v1',
                    'run_session_id': run_id, 'turn_index': turn, 'observations': []}
                report['menu_identity_comparison'] = comparison
                def record_menu():
                    comparison['observations'].append({'source_sha256': source['sha256'],
                        **menu_information_diff(expected, menu)})
                record_menu()
                safe_recollect = menu_recollection_allowed(report, session, pending_at_start=pending_at_start)
                # A possibly consuming/uncertain input never becomes another read
                # or a retry. Back-to-menu has already happened on candidate
                # execution; do not replay it or add another capture there.
                if safe_recollect and not any(step.get('input_attempted') for step in report['steps']):
                    comparison['reobserved'] = True
                    self.sleep(.35)
                    path, image, source = capture()
                    menu = read_menu(path)
                    nav = read_nav(image)
                    record_menu()
                unchanged_session = MeiOutingSession.load(session_path, run_id).data == session.data
                if not menu_information_matches(expected,menu) or not unchanged_session:
                    same_position = same_career_position(expected, menu)
                    comparison.update(same_career_position=same_position, session_unchanged=unchanged_session)
                    if safe_recollect and unchanged_session and not menu_identity_conflicts(expected,menu):
                        # Discard this candidate. The existing caller/outer page
                        # handoff builds a fresh plan; this route does not Open,
                        # select, consume a turn, acknowledge or edit its ledger.
                        return recollect('mei_outing_menu_recollect',menu,path,source)
                    raise ValueError("mei_outing_main_menu_changed")
                comparison['recovered'] = True
            # With the game's second confirmation disabled, opening an ordinary
            # outing may consume the turn immediately. Only the current frame's
            # friend marker proves that this button leads to a chooser. Historical
            # unlocks, old inspections and a strategy candidate cannot supply it.
            for badge_read in range(2):
                hint = nav.get('friend_outing_hint')
                report['friend_badge_observation'] = {
                    'status':'accepted' if isinstance(hint,dict) and hint.get('status')=='accepted' else 'unknown',
                    'available':hint.get('available') if isinstance(hint,dict) and type(hint.get('available')) is bool else None,
                    'source_sha256':source['sha256']}
                if (friend_badge_available(hint) or badge_read or candidate is None
                        or not menu_recollection_allowed(report, session, pending_at_start=pending_at_start)):
                    break
                # A selected outing can reach the main menu before its badge
                # renders. Read one fresh frame without pressing Open.
                report['friend_badge_recheck'] = {
                    'first_observation': report.pop('friend_badge_observation'), 'wait_seconds': .15}
                previous_menu, previous_path, previous_source = menu, path, source
                self.sleep(.15)
                observation_error = None
                try:
                    path, image, source = capture()
                    menu = read_menu(path, badge_recheck=True)
                    if menu_identity_conflicts(previous_menu, menu) or menu_identity_conflicts(expected, menu):
                        raise ValueError('mei_outing_identity_changed')
                    if not same_career_position(previous_menu, menu):
                        raise ValueError('mei_outing_main_menu_changed')
                    nav = read_nav(image)
                except (OSError, ValueError) as exc:
                    if isinstance(exc, ValueError) and str(exc) not in {
                        'mei_outing_capture_failed', 'mei_outing_capture_size',
                        'mei_outing_main_menu_unverified', 'mei_outing_main_menu_unverified_or_overseas',
                    }:
                        raise
                    observation_error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                current_session = MeiOutingSession.load(session_path, run_id)
                if current_session.data != session.data:
                    session = current_session
                    raise ValueError('mei_outing_replan_input_unverified')
                if observation_error is not None:
                    # Hand back the last verified menu for provenance. The
                    # caller reads the current page again before any input.
                    report['friend_badge_recheck']['observation_error'] = observation_error
                    report['returned_source'] = previous_source
                    return recollect('mei_outing_friend_entry_not_observed',
                                     previous_menu, previous_path, previous_source, unavailable=True)
                if not verified_source(candidate.get('source'), run_id=run_id, turn=turn):
                    raise ValueError('mei_outing_candidate_stale')
                if not menu_information_matches(previous_menu, menu) or not menu_information_matches(expected, menu):
                    return recollect('mei_outing_menu_recollect', menu, path, source)
            if not friend_badge_available(hint):
                report.update(reason='mei_outing_friend_entry_not_observed',
                    returned_main_menu_path=str(path),returned_source=source,inspection_status='unknown')
                if menu_recollection_allowed(report,session,pending_at_start=pending_at_start):
                    return recollect('mei_outing_friend_entry_not_observed',menu,path,source,unavailable=True)
                return report
            successors = ("mei_outing_chooser", "modal", "event_choice") if candidate is None and session.data["pending"] is None else ("mei_outing_chooser", "modal")
            report["stage"] = "opening_chooser"
            tap("mei_outing.open", nav.get("open"), "main_menu", successors, path, source)
            path, image, source = capture()
            chooser_waits = (.15, .15, .25, .35, .5, .7, .9)
            report["chooser_read_budget"] = len(chooser_waits) + 1
            for attempt in range(report["chooser_read_budget"]):
                report["stage"] = "reading_chooser"
                if handoff_event("after_open", image, source):
                    return report
                choice = self.reader.read(image) or {}
                other_character = (choice.get("status") == "accepted"
                    and choice.get("screen") in {"outing_chooser", "mei_outing_chooser"}
                    and isinstance(choice.get("character"), str)
                    and bool(choice["character"]) and choice["character"] != CHARACTER)
                observation = {
                    'source': source, **{key: choice.get(key) for key in
                        ('status', 'screen', 'reason', 'character', 'completed', 'evidence')},
                    'identity_state': ('other_character' if other_character else
                        'mei' if choice.get("character") == CHARACTER else 'unresolved')}
                report.setdefault('chooser_observations', []).append(observation)
                safe_to_cancel = (candidate is None and not pending_at_start
                    and session.data["pending"] is None and not report.get("selection_reserved"))
                inspect_only = (safe_to_cancel
                    and choice.get("screen") in {"outing_chooser", "mei_outing_confirm"}
                    and choice.get("cancel") is not None)
                # A cancel-only list proves navigation, not that its fading
                # name/progress is finished. Give the successful Open time to
                # render Mei before retaining the existing cancel-only exit.
                cancel_ready = (inspect_only and (other_character
                    or choice.get("screen") == "mei_outing_confirm"
                    or attempt == report["chooser_read_budget"] - 1))
                if (choice.get("screen") == "mei_outing_chooser" and not other_character) or cancel_ready:
                    break
                if other_character or attempt == report["chooser_read_budget"] - 1:
                    raise ValueError("mei_outing_chooser_unverified")
                # One successful Open already owns this transition. A faded
                # name or progress row needs a fresh read, never another Open.
                observation['wait_seconds'] = chooser_waits[attempt]
                self.sleep(chooser_waits[attempt])
                path, image, source = capture()
            choice_screen = "modal" if choice.get("screen") == "mei_outing_confirm" else "mei_outing_chooser"
            unresolved_exit = (inspect_only and not other_character
                and choice.get("screen") == "outing_chooser" and choice.get("status") != "accepted")
            unresolved_source = source if unresolved_exit else None
            observe(choice_screen, source)
            if not inspect_only:
                try:
                    session.observe_chooser(choice, source, menu_snapshot=menu,
                        recover_interrupted_selection=candidate is None)
                except ValueError as exc:
                    if (str(exc) != 'mei_outing_progress_disagrees'
                            or not menu_recollection_allowed(report,session,
                                pending_at_start=pending_at_start,friend_navigation=True)
                            or MeiOutingSession.load(session_path,run_id).data != session.data):
                        raise
                    # This is a fresh identified chooser, before any selection.
                    # Cancel it; keep the conflicting ledger unchanged and omit
                    # the optional candidate instead of inventing its progress.
                    report['progress_conflict']={'recorded_completed':session.data['completed'],
                        'observed_completed':choice.get('completed'),'ledger_changed':False}
                    tap('mei_outing.cancel',choice.get('cancel'),choice_screen,('main_menu',),path,source)
                    for attempt in range(4):
                        path, image, source = capture()
                        try:
                            read_nav(image)
                            returned = read_menu(path)
                            break
                        except ValueError as fade_exc:
                            if attempt == 3 or str(fade_exc) not in {
                                "mei_outing_main_menu_unverified",
                                "mei_outing_main_menu_unverified_or_overseas",
                            }:
                                raise
                            self.sleep(.35)
                    if (not same_career_position(menu,returned) or menu_identity_conflicts(menu,returned)
                            or MeiOutingSession.load(session_path,run_id).data != session.data):
                        raise ValueError('mei_outing_inspection_changed_turn')
                    return recollect('mei_outing_progress_recollect',returned,path,source,unavailable=True)
            if not unresolved_exit:
                session.data["last_inspection_turn"] = turn
                session.data["last_inspection_result"] = choice.get("reason", choice.get("screen"))
                session.save(session_path)
            if candidate is None:
                packet = None if inspect_only else session.packet(turn=turn, session_path=session_path, transition_path=transition_path)
                if packet is not None:
                    packet["menu_snapshot"] = menu
                report["stage"] = "cancelling_inspection"
                tap("mei_outing.cancel", choice.get("cancel"), choice_screen, ("main_menu",), path, source)
                path, image, source = capture()
                report["stage"] = "verifying_return"
                for attempt in range(4):
                    observed = self.reader.read(image)
                    try:
                        returned_menu = read_menu(path) if isinstance(observed, dict) and observed.get("screen") == "main_menu" else None
                    except ValueError:
                        returned_menu = None
                    if menu_identity_conflicts(menu,returned_menu):
                        raise ValueError('mei_outing_identity_changed')
                    same_turn = (same_career_position(menu,returned_menu) if inspect_only
                                 else menu_information_matches(menu,returned_menu))
                    if same_turn:
                        break
                    if attempt == 3:
                        raise ValueError("mei_outing_inspection_changed_turn")
                    self.sleep(.35)
                    path, image, source = capture()
                observe("main_menu", source)
                report.update(is_valid=True, reason="mei_outing_not_available" if inspect_only else "mei_outing_inspected", packet=packet,
                              returned_main_menu_path=str(path), returned_source=source, stage="returned_main_menu",
                              inspection_skipped=inspect_only,
                              skip_reason=choice.get("reason") if inspect_only else None)
                if unresolved_exit:
                    # Returning safely does not make the unreadable name or
                    # arrows a negative friend observation. The existing
                    # packet=None return rebuilds the menu provider and lets
                    # ordinary actions continue without an outing candidate.
                    report.update(is_valid=True, reason="mei_outing_inspection_deferred",
                        inspection_status="unknown", unresolved_chooser_source=unresolved_source,
                        failure_source=unresolved_source, cleanup_stage="returned_main_menu")
                    return recollect('mei_outing_inspection_deferred',returned_menu,path,source)
            else:
                report["stage"] = "selecting_outing"
                if choice.get("completed") != candidate["completed"] or choice.get("completed") == 5:
                    raise ValueError("mei_outing_progress_changed")
                tap("mei_outing.select", choice.get("select"), "mei_outing_chooser",
                    POST_ACTION_ROUTE_SUCCESSORS, path, source, reserve=True)
                path, image, source = capture()
                from core.auto_trainer.transition_wait import (
                    career_comic_loading_visible,
                    connecting_overlay_visible,
                )
                confirmation_waits = (.15, .15, .25, .35, .5, .7, .9)
                report["confirmation_read_budget"] = len(confirmation_waits) + 1
                report["confirmation_loading_extra_waits"] = 0
                normal_used = 0
                extra_loading_used = 0
                while True:
                    report["stage"] = "reading_confirmation"
                    confirmation = self.reader.read(image) or {}
                    if confirmation.get("screen") not in {"mei_outing_confirm", "mei_outing_chooser"}:
                        selected_reader = getattr(self.reader, "read_selected_confirmation", None)
                        if selected_reader is not None:
                            selected = selected_reader(image) or {}
                            if selected.get("screen") == "mei_outing_confirm":
                                confirmation = selected
                    observation = {
                        'source': source, **{key: confirmation.get(key) for key in
                            ('status', 'screen', 'reason', 'evidence')}}
                    report.setdefault('confirmation_observations', []).append(observation)
                    if confirmation.get("screen") == "mei_outing_confirm":
                        break
                    if confirmation.get('screen') not in {'mei_outing_chooser', 'outing_chooser'}:
                        from core.auto_trainer.optional_confirmation import direct_action_started
                        started = direct_action_started(path, before_menu=menu)
                        observation['direct_start_observation'] = started
                        if started is not None:
                            # With the game's second confirmation disabled,
                            # selecting Mei is itself the consuming input.
                            receipt = session.record_tap(True)
                            session.save(session_path)
                            route.commit_action(receipt['action_id'],
                                expected_next_states=POST_ACTION_ROUTE_SUCCESSORS,
                                source_sha256=receipt['source']['sha256'])
                            observe(started['screen'], source)
                            route.save(transition_path)
                            report.update(performed=True, tap_performed=True, turn_consumed=True,
                                is_valid=True, reason='executed_mei_outing',
                                target=f"satake_mei:{receipt['stage']}", outing_receipt=receipt,
                                required_event_choice='top' if receipt['stage']==3 else None,
                                confirmation_mode='disabled', direct_start_source=source,
                                direct_start_observation=started, page_handoff=started['screen'],
                                handoff_stage='after_selection', handoff_source=source)
                            return report
                    is_loading = image is not None and (
                        connecting_overlay_visible(image) or career_comic_loading_visible(image)
                    )
                    if is_loading and extra_loading_used < MEI_OUTING_LOADING_EXTRA_WAITS:
                        extra_loading_used += 1
                        observation['loading_overlay'] = True
                        observation['loading_extra_attempt'] = extra_loading_used
                        observation['wait_seconds'] = MEI_OUTING_LOADING_WAIT_SECONDS
                        report['confirmation_loading_extra_waits'] = extra_loading_used
                        self.sleep(MEI_OUTING_LOADING_WAIT_SECONDS)
                        path, image, source = capture()
                        continue
                    normal_used += 1
                    report['confirmation_loading_extra_waits'] = extra_loading_used
                    if normal_used >= report["confirmation_read_budget"]:
                        raise ValueError("mei_outing_confirmation_unverified")
                    # A fast renderer can return multiple frames before the
                    # confirmation body fades in. Wait only on an unreadable
                    # frame; never replay the selection or confirmation tap.
                    wait_seconds = confirmation_waits[normal_used - 1]
                    observation['wait_seconds'] = wait_seconds
                    self.sleep(wait_seconds)
                    path, image, source = capture()
                tap(f"outing.satake_mei.{candidate['completed']+1}", confirmation.get("confirm"),
                    "modal", POST_ACTION_ROUTE_SUCCESSORS, path, source, consume=True)
                report["confirmation_source"] = source
                report["is_valid"] = True
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}:{exc}"
            if str(exc) in {'mei_outing_identity_changed','mei_outing_main_menu_changed',
                    'mei_outing_progress_disagrees','mei_outing_input_unconfirmed',
                    'mei_outing_inspection_changed_turn','mei_outing_interrupted_selection_unverified',
                    'mei_outing_replan_input_unverified'}:
                report['execution_reason']=str(exc)
            if not report["performed"]:
                report["reason"] = str(exc)
            # A successful tap is still counted if later persistence/routing fails.
            report["is_valid"] = False
            report["failure_source"] = report["last_source"]
        finally:
            report["has_pending_operation"] = session.data["pending"] is not None
            if session.data["pending"] is not None:
                report["pending_operation"] = {key:session.data["pending"].get(key)
                    for key in ("action_id","stage","turn_index","tap_performed")}
        return report
