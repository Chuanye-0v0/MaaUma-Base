"""Attach to the live career, not to a previously recorded action plan."""
from pathlib import Path
import json
import time

from core.auto_trainer.route_transition_session import RouteTransitionSession
from core.auto_trainer.skip_session import RunSkipSession
from core.auto_trainer.larc_adaptation import PHASE_SLOT_COUNTS, LarcAdaptationSession
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json


def enabled(args):
    return bool(args.execute and getattr(args, 'live_takeover', True)
        and (not getattr(args, '_cut_farming_hook', None)
             or getattr(args, '_player_live_takeover', False)
             or getattr(args, '_new_cycle_live_takeover', False))
        and not getattr(args, 'resume_recorded_ss', False)
        and getattr(args, 'expected_main_menu_state_json', None) is None)


def reset_live_contexts(root, endpoint):
    """Keep old evidence, but do not replay old intentions on a new Start."""
    root = Path(root).resolve(strict=True)
    progress = json.loads((root/'progress.json').read_text(encoding='utf8'))
    if progress.get('run_session_id') != root.name or progress.get('device') != endpoint:
        raise ValueError('live_takeover_run_or_device_mismatch')
    if (root/'session/joint_trial_owner.json').exists():
        raise ValueError('joint_trial_owns_run_no_automatic_actions')
    archive = root/'session/live_takeover_history'/str(time.time_ns())
    names = ['larc_autonomous_pending.json', 'larc_race_checkpoint.json',
        'larc_adaptation_ready.json',
        'larc_terminal_finish.json', 'larc_terminal_skills.json', 'larc_terminal_settlement.json',
        'larc_support_skill_startup.json', 'transition_session.json', 'skip_session.json']
    names += [f'larc_adaptation_{phase}.json' for phase in PHASE_SLOT_COUNTS]
    confirmed_phases = []
    clock_retry_state = None
    checkpoint_path = root/'session/larc_race_checkpoint.json'
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding='utf8')) if checkpoint_path.is_file() else {}
    except (OSError, ValueError):
        checkpoint = {}
    ordinary_clock=(checkpoint.get('ordinary_clock_retry') is True
        and checkpoint.get('status')=='blocked' and checkpoint.get('turn_index')==progress.get('next_turn'))
    if (checkpoint.get('run_session_id') == root.name and checkpoint.get('device') == endpoint
            and (checkpoint.get('cut_debut_race') is True or ordinary_clock)):
        from scripts.larc_autonomy_race import _clock_usage
        usage = _clock_usage(checkpoint.get('steps', []))
        race_session = checkpoint.get('session') or {}
        clock_retry_state = {key:race_session.get(key, default) for key,default in (
            ('clock_retries_used',0),('last_clock_retry_remaining',None))}
        # The player may have closed/reopened the retry dialog. The entry flag
        # is an old navigation intention; only confirmed clock usage survives.
        clock_retry_state['clock_retry_entry_pending'] = False
        clock_retry_state.update({key:checkpoint.get(key, value) for key,value in usage.items()})
        clock_retry_state['cut_debut_race'] = checkpoint.get('cut_debut_race') is True
        if ordinary_clock:clock_retry_state['ordinary_clock_retry']=True
    for phase in PHASE_SLOT_COUNTS:
        path = root/'session'/f'larc_adaptation_{phase}.json'
        if not path.is_file():
            continue
        try:
            session = LarcAdaptationSession.load(path, root.name)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        receipt = session.receipt or {}
        if (session.phase == phase and session.stage == 'complete' and session.returned
                and not session.inspect_only and receipt.get('run_session_id') == root.name
                and receipt.get('phase') == phase and receipt.get('pending_cost') == 0):
            names.remove(path.name)
            confirmed_phases.append(phase)
    index_path = root/'session/larc_adaptation_ready.json'
    try:
        index = json.loads(index_path.read_text(encoding='utf8')) if index_path.is_file() else {}
    except (OSError, ValueError):
        index = {}
    finish_path = root/'session/larc_terminal_finish.json'
    try:
        finish = json.loads(finish_path.read_text(encoding='utf8')) if finish_path.is_file() else {}
    except (OSError, ValueError):
        finish = {}
    completed_finish = (finish.get('status') == 'submitted' and finish.get('run_session_id') == root.name
        and finish.get('device') == endpoint and finish.get('turn_index') == progress.get('next_turn'))
    if completed_finish:
        # Preserve a confirmed transaction's settlement owner, never its input.
        names = [name for name in names if name not in {'larc_terminal_finish.json',
            'larc_terminal_skills.json', 'larc_terminal_settlement.json'}]
    skill_path=root/'session/larc_terminal_skills.json'
    skill_state=json.loads(skill_path.read_text(encoding='utf8')) if skill_path.is_file() else {}
    retained_skills=bool(skill_state and skill_state.get('run_session_id')==root.name
        and skill_state.get('device')==endpoint and skill_state.get('turn_index')==progress.get('next_turn'))
    if retained_skills:
        # This owns real skill transactions, including unresolved outcomes.
        # Scene-first attachment discards navigation intentions, not this
        # spending/return checkpoint. Its owner decides what can resume.
        names=[name for name in names if name!='larc_terminal_skills.json']
    support_path=root/'session/larc_support_skill_startup.json'
    try:
        support_state=json.loads(support_path.read_text(encoding='utf8')) if support_path.is_file() else {}
    except (OSError,ValueError,TypeError):
        support_state={}
    retained_support=support_state.get('status')=='pending_purchase'
    if retained_support:
        # Pending spending is not a stale navigation intention. Its owner
        # validates the bound run/device/turn and confirmation proof.
        names=[name for name in names if name!='larc_support_skill_startup.json']
    moved = []
    for name in names:
        source = (root/'session'/name).resolve()
        target = (archive/name).resolve()
        if not source.is_relative_to(root) or not target.is_relative_to(root):
            raise ValueError('live_takeover_archive_outside_run')
        if source.is_file():
            archive.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            moved.append(name)
    RouteTransitionSession.new(root.name).save(root/'session/transition_session.json')
    RunSkipSession.new(root.name).save(root/'session/skip_session.json')
    if confirmed_phases and index.get('run_session_id') == root.name and index.get('device') == endpoint:
        references = {phase:reference for phase,reference in index.get('phases', {}).items()
                      if phase in confirmed_phases}
        _atomic_write_json(index_path, {'run_session_id':root.name, 'device':endpoint, 'phases':references})
    report = {'mode':'current_screen', 'run_session_id':root.name, 'device':endpoint,
        'archived_contexts':moved, 'archive':str(archive), 'old_actions_replayed':False,
        'progress_is_log_sequence_not_game_position':True, 'new_career_started':False,
        'confirmed_finish_retained':completed_finish, 'confirmed_adaptations_retained':confirmed_phases,
        'terminal_skill_transaction_retained':retained_skills,
        'support_skill_transaction_retained':retained_support}
    if clock_retry_state is not None:
        report['clock_retry_state'] = clock_retry_state
    _atomic_write_json(root/'session/live_takeover.json', report)
    return report


def scene_from_decisions(safe, race):
    """Only current-frame facts choose the initial owner."""
    if safe.get('reason') in {'career_end_confirmation','continue_goal_failure_summary'}:
        # Known end/failed-goal controls outrank a generic race-result footer.
        return {'owner':'common','state':'modal','entered':False}
    if safe.get('reason')=='skill_acquisition_handoff':
        # A skill list can resemble the generic race-result green footer.
        # Its recognized skill owner takes precedence over that broad match.
        return {'owner':'common','state':'skill_acquisition','entered':False}
    if (safe.get('action')=='stop' and safe.get('route_state')=='modal'
            and safe.get('reason')=='larc_adaptation_session_required'):
        # The typed current adaptation page belongs to its existing session,
        # not a generic portrait race-animation match. This is ownership only;
        # the adaptation executor still verifies the plan and every purchase.
        return {'owner':'common','state':'modal','entered':False}
    evidence = safe.get('evidence', {})
    if (safe.get('action') == 'tap' and safe.get('route_state') == 'modal'
            and safe.get('reason') == 'larc_adaptation_result_close'
            and safe.get('tap', {}).get('coordinate_name') == 'safe_advance.larc_adaptation_result_close'
            and evidence.get('larc_adaptation', {}).get('result_close_control', {}).get('status') == 'enabled'
            and evidence.get('screen_text', {}).get('dangerous') is not True):
        # A current result notice may have no old purchase session. Ownership
        # here is read-only; only the ordinary executor can close its control.
        return {'owner':'common','state':'modal','entered':False}
    page = evidence.get('larc_page', {})
    if (safe.get('action') == 'tap' and safe.get('reason') == 'larc_explanation_continue'
            and safe.get('route_state') == 'modal'
            and page.get('schema_version') == 'uma.larc_page.v1'
            and page.get('owner') == 'safe_advance' and page.get('route_state') == 'modal'
            and page.get('page_id') in {'exchange_schedule', 'scenario_explanation',
                'supporter_reward', 'supporter_points_shortage'}
            and evidence.get('screen_text', {}).get('dangerous') is not True):
        # Typed free explanation/award navigation outranks a generic green
        # footer or portrait race guess. This selects an owner only; the
        # ordinary executor still checks the current frame and action gates.
        return {'owner':'common','state':'modal','entered':False}
    if safe.get('reason') in {'skip_larc_opening_movie', 'larc_opening_movie_pending'}:
        return {'owner':'common', 'state':'dialogue', 'entered':False}
    if (race.get('reason') == 'larc_adaptation_warning:confirmation_not_allowed'
            and race.get('evidence', {}).get('larc_adaptation_warning', {}).get('verified') is True):
        return {'owner':'race', 'state':'race_scene', 'entered':True}
    if (safe.get('route_state') == 'main_menu'
            and safe.get('reason') in {'ready_main_menu', 'enable_bottom_skip_toggle'}):
        # Verified current home controls outrank a generic portrait race-scene
        # match. This does not turn an unknown page or race preview into home.
        return {'owner':'common', 'state':'main_menu', 'entered':False}
    reason = race.get('reason', '')
    state = race.get('route_state', '')
    text = str(race.get('evidence', {}).get('screen_text', {}).get('normalized_text', ''))
    narration = '实况' in text and '人气' in text
    if (reason in {'view_race_result', 'go_to_race_from_detail', 'race_preview:view_result_locked',
                   'race_scene_running', 'race_result_continue',
                   'landscape_race_skip_enabled', 'landscape_race_result'}
            or state in {'race_scene','race_result','race_detail_view_result'}
            or reason == 'race_list:no_goal_race_candidate' and narration):
        return {'owner':'race', 'state':'race_scene', 'entered':True}
    reason = safe.get('reason', '')
    if race.get('route_state') == 'race_detail_confirm':
        return {'owner':'race', 'state':'modal', 'entered':False}
    if reason == 'race_day_main_menu':
        return {'owner':'common', 'state':'main_menu', 'entered':False}
    if reason == 'race_list':
        return {'owner':'race', 'state':'race_list', 'entered':False}
    from core.auto_trainer.safe_advance_transition import observed_route_state
    state = observed_route_state(safe)
    if state in {'main_menu','training_menu','dialogue','event_choice','loading','modal','skill_acquisition'}:
        return {'owner':'common', 'state':state, 'entered':False}
    return {'owner':'unknown', 'state':None, 'entered':False}


def _capture_error(ctx, device, error, *, stage='capture'):
    """Preserve the existing exception while exposing a safe capture leaf."""
    from core.adb import safe_capture_failure, capture_failure
    diagnostic = (safe_capture_failure(getattr(error, 'capture_failure', None))
                  or safe_capture_failure(getattr(device, 'last_capture_failure', None)))
    if diagnostic is None:
        try:
            backend = getattr(device, 'capture_backend_name', 'unknown')
        except Exception:
            backend = 'unknown'
        diagnostic = capture_failure(backend, stage, 'connection_failed' if stage == 'connect' else 'capture_failed', error=error)
    try:
        ctx.capture_failure = diagnostic
    except (AttributeError, TypeError):
        pass
    try:
        error.capture_failure = diagnostic
    except (AttributeError, TypeError):
        pass
    return error


def _remember_game_frame(ctx, source, image):
    """Replace one in-memory last frame; this never writes another PNG."""
    segment = getattr(ctx, 'segment_root', None)
    value = {'path': str(source), 'captured_at_unix': time.time(),
             'run_session_id': getattr(ctx, 'run_id', None),
             'segment_id': Path(segment).name if isinstance(segment, (str, Path)) else None}
    if (getattr(image, 'ndim', None) == 3 and str(getattr(image, 'dtype', None)) == 'uint8'
            and image.shape[2] == 3):
        try:
            value['frame'] = image.copy()
        except Exception:
            pass  # Keeping diagnostic pixels must not fail a successful capture.
    try:
        ctx.last_game_frame = value
        ctx.capture_failure = None
    except (AttributeError, TypeError):
        pass


def observe_current(ctx, output, *, adb=None, persist_route=True):
    """Read one frame without game input; speculative callers may skip route adoption."""
    from contextlib import nullcontext
    from core.native_capture_adb import adb_for_capture
    from core.auto_trainer.frame_safety import FrameSafetyAnalyzer
    from core.auto_trainer.safe_advance import SafeAdvancePlanner
    from core.auto_trainer.safe_advance_transition import SafeAdvanceRoutePolicy
    from core.auto_trainer.race_route import RaceRoutePlanner, RaceRoutePolicy, RaceRouteSession
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    source = output/'current.png'
    try:
        created = adb_for_capture(ctx.port) if adb is None else None
        if created is not None and not getattr(created,'supports_in_memory_capture',False) and not created.connect():
            raise _capture_error(ctx, created, RuntimeError('live_takeover_adb_connect_failed'), stage='connect')
    except Exception as error:
        _capture_error(ctx, None, error, stage='connect')
        raise
    scope = created if created is not None and hasattr(created,'__enter__') else nullcontext(adb or created)
    device = adb or created
    try:
        with scope as device:
            image = device.screenshot(str(source))
            if image is None:
                raise _capture_error(ctx, device, RuntimeError('live_takeover_capture_failed'))
            _remember_game_frame(ctx, source, image)
    except Exception as error:
        _capture_error(ctx, device, error)
        raise
    packet = FrameSafetyAnalyzer().analyze(source)
    opening = bool(getattr(ctx, 'ordinary_career_opening', False)
        and getattr(ctx, 'progress', {}).get('next_turn') == 1)
    policy = SafeAdvanceRoutePolicy.resolve(current_state=None, expected_next_states=(),
        bootstrap_state='live-career-opening' if opening else 'live-career')
    safe = SafeAdvancePlanner().plan(source, scenario='larc', visible_text=packet.visible_text,
        route_policy=policy)
    debut = any(word in packet.visible_text for word in ('初级新人战','初级出道前'))
    hook = getattr(ctx, 'cut_farming_hook', None)
    ordinary_retry=hook is None and getattr(ctx,'ordinary_career_opening',False) is True
    allow_retry = ordinary_retry or bool(debut and hook is not None and hook.session.config.debut_clock_retry)
    from core.auto_trainer.race_retry_budget import ClockBudget
    retry_config=getattr(ctx.goal,'race_retry',None)
    budget=ClockBudget(ctx.root,retry_config)
    if retry_config is not None:
        allow_retry=retry_config['enabled'];ordinary_retry=allow_retry
    race = RaceRoutePlanner().plan(source, policy=RaceRoutePolicy(scenario='larc',
        post_entry_only=True, allow_locked_race_entry=True,
        allow_quick_result=True, allow_skip_race_scene=True,
        allow_result_continue=True, allow_clock_retry=allow_retry,clock_retry_remaining=budget.remaining,clock_retry_unresolved=budget.retry_pending,
        allow_debut_retry_entry=allow_retry and debut,allow_result_retry_entry=ordinary_retry,
        visible_text=packet.visible_text),
        session=RaceRouteSession())
    budget.observe_page(safe,packet.to_dict())
    budget.observe_page(race,packet.to_dict())
    scene = scene_from_decisions(safe, race)
    if (ordinary_retry and not packet.dangerous
            and (scene['owner'] == 'unknown' or scene.get('state') in {'race_result', 'race_scene'})
            and safe.get('reason') != 'connection_conflict'):
        # A cold attachment has no previous result-transition intention. Only
        # this fresh, strictly recognized rank/Pt award can establish its
        # result owner; do not restore a prior race entry or spending action.
        overlay = RaceRoutePlanner._larc_rank_points_overlay(image, packet.visible_text)
        if overlay is not None:
            scene = {'owner':'race', 'state':'race_result', 'entered':True,
                'current_result_proof':{'kind':'larc_rank_points_overlay',
                    'provenance':'current_frame_not_checkpoint', **overlay}}
    verified_retry = (allow_retry and race.get('reason') in {'retry_lost_race','open_lost_race_retry'}
        or race.get('reason') in {'cancel_clock_retry','clock_retry_outcome_pending'})
    if (packet.dangerous and not verified_retry) or safe.get('reason') == 'connection_conflict':
        scene = {'owner':'unknown', 'state':None, 'entered':False}
    scene.update(source=str(source), source_sha256=packet.source_image_sha256,
                 safe_reason=safe.get('reason'), race_reason=race.get('reason'),
                 cut_debut_race=debut)
    if ordinary_retry or retry_config is not None:scene['ordinary_clock_retry']=True
    if scene['state'] and persist_route:
        route = RouteTransitionSession.load(ctx.route, expected_run_session_id=ctx.run_id)
        route.observe(scene['state'], source_sha256=packet.source_image_sha256, resync=True)
        route.save(ctx.route)
    _atomic_write_json(output/'observation.json', scene)
    return scene
