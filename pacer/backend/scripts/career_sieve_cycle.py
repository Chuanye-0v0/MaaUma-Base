"""Ordinary careers sharing one existing setup and resource-budget owner."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import logging
import uuid

from core.auto_trainer.ace_graduation import (
    evaluate_graduation, inspect_completed_career, normalize_ace_cycle,
)
from core.auto_trainer.config import load_goal_config
from core.auto_trainer.cut_farming import CutFarmingConfig, CutSession, atomic_json
from core.auto_trainer.runtime_control import RunStopped, bind, current_page_resume
from core.career_workflow import ace_cycle_node, ss_sieve_node, validate_start_document
from scripts.career_workflow_runtime import (DOCUMENT, SS_SIEVE_FAILURE, WorkflowState,
    authorized_continuation_restart, authorized_sieve_failure, load_for_run, read)

CYCLE = 'ss_sieve_cycle'
_ACTIVE_CAREER_TAKEOVER_STATES = frozenset({
    'main_menu', 'training_menu', 'dialogue', 'event_choice', 'skill_acquisition',
})


def _is_active_career_scene(scene) -> bool:
    if not isinstance(scene, dict):
        return False
    return scene.get('owner') == 'race' or scene.get('state') in _ACTIVE_CAREER_TAKEOVER_STATES


def create_sieve_batch(root, endpoint, document):
    """Freeze ordinary settings once. Cut options supply resource limits only."""
    from core.career_panel_presets import ai_connection, sanitize_settings

    node = ss_sieve_node(document) or ace_cycle_node(document)
    if node is None:
        raise ValueError('连续循环工作流缺少筛选或成品育成节点。')
    start_check = validate_start_document(document)
    if not start_check['ok']:
        raise ValueError('；'.join(start_check['errors']))
    ace_config = normalize_ace_cycle(node['ace_cycle']) if 'ace_cycle' in node else None
    policy = document['policies'][node.get('policy', 'default')]
    options = sanitize_settings(policy.get('career_options', {}), root, new_configuration=True,
                                allow_continuation_restart=True)
    setup = options['setup']
    if setup['character_id'] in {'current', 'validation'}:
        raise ValueError('SS筛局重开需要在普通育成配置中选择确切角色和服装。')
    connection = ai_connection(root, options)
    limits = policy.get('cut_options', {}).get('cut') or {}
    if not isinstance(limits, dict):
        raise ValueError('SS筛局预算需要参数对象。')
    budgets = {}
    for key in ('max_attempts', 'max_tp_items', 'max_carrots', 'refill_operation_limit'):
        if key in limits:
            value = limits[key]
            budgets[key] = int(value.strip()) if isinstance(value, str) and value.strip().isdigit() else value
    config = CutFarmingConfig.from_dict({**budgets,
        'character_id': setup['character_id'], 'validation_mode': True,
        'validation_character_name': setup['character_name'], 'strategy': 'baseline',
        'device_port': int(endpoint.rsplit(':', 1)[1]), 'retain_screenshots': options['retain_training'],
        **{key: setup[key] for key in ('support_deck', 'borrow_card_id', 'borrow_min_level', 'borrow_min_break')}})
    path = Path(root) / 'runs' / f'cut_pacer_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}'
    session = CutSession(path, config)
    session.data[CYCLE] = {
        'phase': 'ready',
        'mode': document.get('template_id', CYCLE),
        'ace_config': ace_config,
        'career_options': options,
        'ai_connection': connection,
    }
    session.save()
    atomic_json(path / DOCUMENT, document)
    WorkflowState(path, document, endpoint=endpoint).save()
    return path, config


def sieve_resumable(data):
    return (data.get(CYCLE, {}).get('phase') == 'growing'
            and not data.get('pending_action') and not data.get('tp_refill_pending')
            and bool(data.get('attempts')) and not data['attempts'][-1].get('ss_sieve_outcome'))


def current_sieve_attempt(value, *, root, endpoint):
    """Resolve only the exact current attempt for an explicit manual action."""
    path = Path(value).resolve(strict=True)
    batch = path.parent.parent if path.parent.name == 'attempts' else path
    if batch.parent != Path(root).resolve() / 'runs' or not batch.name.startswith('cut_pacer_'):
        return None
    data = read(batch / 'cut_farming.json')
    if CYCLE not in data:
        return None
    if not sieve_resumable(data):
        raise ValueError('SS筛局当前没有可继续的普通育成。')
    latest = data['attempts'][-1]
    attempt = Path(latest['run_root']).resolve(strict=True)
    progress = read(attempt / 'progress.json')
    if (path not in {attempt, batch} or attempt.parent != batch / 'attempts'
            or latest.get('run_id') != attempt.name or progress.get('run_session_id') != attempt.name
            or progress.get('device') != endpoint):
        raise ValueError('SS筛局原局或设备已改变，请刷新后重试。')
    store = load_for_run(attempt)
    if store is None or store.root != batch or (ss_sieve_node(store.document) is None and ace_cycle_node(store.document) is None):
        raise ValueError('SS筛局记录与原工作流不一致。')
    return attempt, batch, store.document


def _ordinary_attempt(session, endpoint):
    from core.career_panel_presets import save_new_run
    from scripts.larc_ordinary_setup import save_ordinary_setup
    from scripts.larc_panel_setup import create_panel_run

    cycle = session.data[CYCLE]
    options = deepcopy(cycle['career_options'])
    root = create_panel_run(device_port=session.config.device_port, runs_dir=session.root / 'attempts',
        targets=options['targets'], strategy='baseline', failure_limit=options['failure_limit'],
        mei_breaks=options['mei_breaks'], with_ramonu=options['with_ramonu'],
        early_skill_allowlist=options['early_skill_allowlist'], arc_policy=options.get('arc_policy'),
        larc_policy=options['larc_policy'], task_mode='career', mid_skill_learning=options['mid_skill_learning'],
        support_profile=options.get('support_profile'), race_retry=options.get('race_retry'),
        running_style=options.get('running_style'), race_running_styles=options.get('race_running_styles'),
        custom_races=options.get('custom_races'))
    options['strategy'] = 'baseline'
    options['larc_policy'] = read(root / 'goal.json')['larc_policy']
    save_ordinary_setup(root, read(root / 'progress.json'), options['setup'])
    save_new_run(root, endpoint, options, cycle['ai_connection'])
    if options.get('continuation') is not None:
        from scripts.larc_continuation_runtime import save_options
        save_options(root, options['continuation'])
    atomic_json(root / 'session/ace_cycle_binding.json', {
        'run_session_id': root.name,
        'endpoint': endpoint,
        'batch_root': str(session.root),
        'allow_continuation_restart': True,
    })
    WorkflowState(session.root, endpoint=endpoint).bind(root)
    return root


class SieveCycle:
    def __init__(self, session, control, *, runner=None, restart=None, opening=None,
                 graduation_inspector=None, takeover_observer=None):
        self.session = session
        self.control = control
        self.runner = runner
        self.restart = restart
        self.opening = opening
        self.graduation_inspector = graduation_inspector
        self.takeover_observer = takeover_observer
        self.cycle = session.data[CYCLE]
        self.port = session.config.device_port
        self.endpoint = f'127.0.0.1:{self.port}'

    def _publish_state(self, status='running'):
        store = WorkflowState(self.session.root, endpoint=self.endpoint)
        counts = {kind: sum(row.get('ss_sieve_outcome') == kind for row in self.session.data['attempts'])
                  for kind in ('completed', 'screened_out', 'failed')}
        if self.cycle.get('ace_config') is not None:
            counts['completed'] += sum(row.get('ss_sieve_outcome') == 'graduated' for row in self.session.data['attempts'])
            ace_counts = {
                kind: sum(row.get('ss_sieve_outcome') == kind for row in self.session.data['attempts'])
                for kind in ('graduated', 'unqualified', 'screened_out', 'failed', 'completed')
            }
            store.data['ace_cycle_counts'] = ace_counts
        store.data.update(status=status, ss_sieve_phase=self.cycle['phase'], ss_sieve_counts=counts)
        store.save()
        return store.data

    def _stop(self, status, reason, *, route=None):
        extras = {}
        if route and route.get('normal_stop') is True:
            extras = {'normal_stop': True, 'resource_status': route.get('resource_status'),
                      'message': route.get('message')}
        self.session.stop(status, reason=reason, **extras)
        state = self._publish_state(status)
        return {'status': status, 'reason': reason, 'session_root': str(self.session.root),
                'attempts': len(self.session.data['attempts']), 'workflow': state, **extras}

    def _args(self, root):
        from scripts import larc_autonomous_run as career
        from scripts.larc_control_window import build_argv
        from core.auto_trainer.larc_ai_options import load_options

        options = self.cycle['career_options']
        argv = build_argv(root, options['max_actions'], device_port=self.port,
                          retain_training=options['retain_training'])
        argv = [item for item in argv if item != '--d1-startup-only']
        argv += ['--ordinary-career-opening', '--same-account-reconnect']
        if not options['terminal_skills']:
            argv.append('--d1-startup-only')
        args = career.parse_args(argv)
        args._keep_run_control = True
        args._ai_decision_options = load_options(root)
        self.control.ss_planning_horizon = read(root / 'goal.json')['larc_policy'].get(
            'ss_planning_horizon', 'full_career')
        return args

    def _open(self, root, args):
        from scripts.larc_live_takeover import observe_current
        from scripts.larc_ordinary_setup import save_opening_support_observation
        from scripts.larc_ordinary_start import start_preselected_ordinary_career

        attempt = self.session.data['attempts'][-1]
        is_first_attempt = len(self.session.data['attempts']) == 1
        self.control.checkpoint('ss_sieve_opening')
        setup = self.cycle['career_options']['setup']
        d1_root = args.d1_skill_root
        self.cycle['phase'] = 'opening'
        # This attempt owns a fresh setup. Unknown consumption is checked
        # before entering the cycle and is never cleared here.
        for key in ('support_setup_observation', 'ordinary_start_submitted', 'legacy_borrow_target'):
            self.session.data.pop(key, None)
        self.session.save()
        goal = load_goal_config(root / 'goal.json') if (root / 'goal.json').is_file() else SimpleNamespace(race_retry=None)
        ctx = SimpleNamespace(root=root, run_id=root.name, port=self.port,
                              progress=read(root / 'progress.json'), goal=goal,
                              route=root / 'session/transition_session.json')
        if is_first_attempt and (self.takeover_observer is not None or self.opening is None):
            probe = None
            try:
                if self.takeover_observer is not None:
                    probe = self.takeover_observer(ctx, root / 'ss_sieve_takeover_probe')
                else:
                    probe = observe_current(ctx, root / 'ss_sieve_takeover_probe', persist_route=False)
            except Exception:
                probe = None
            if _is_active_career_scene(probe):
                route = {
                    'ok': True,
                    'reason': 'existing_career_takeover_attempt1',
                    'new_career_started': False,
                    'scene': probe,
                }
                atomic_json(root / 'ss_sieve_opening_receipt.json', route)
                attempt['ss_sieve_opening'] = {
                    'confirmed': True,
                    'reason': route['reason'],
                    'new_career_started': False,
                    'receipt': 'ss_sieve_opening_receipt.json',
                }
                self.cycle['phase'] = 'growing'
                self.session.save()
                return route
        opener = self.opening or start_preselected_ordinary_career
        if self.opening is None and (not d1_root or not (Path(d1_root) / 'core/autonomous_training/trainee.py').is_file()):
            route = {'ok': False, 'reason': 'ordinary_trainee_library_missing'}
        else:
            route = opener(ctx, root / 'ss_sieve_opening', trainee_name=setup['character_name'],
                           trainee_variant=setup['character_id'], setup=setup, d1_root=d1_root,
                           batch_session=self.session)
        atomic_json(root / 'ss_sieve_opening_receipt.json', route)
        no_pending = not self.session.data.get('pending_action') and not self.session.data.get('tp_refill_pending')
        takeover_handoff = (
            is_first_attempt
            and route.get('ok') is True
            and route.get('reason') == 'existing_career_handoff'
            and no_pending
        )
        confirmed = (route.get('ok') is True and route.get('reason') == 'ordinary_start_confirmed' and no_pending)
        if takeover_handoff:
            result_route = {**route, 'new_career_started': False}
            attempt['ss_sieve_opening'] = {
                'confirmed': True,
                'reason': route.get('reason'),
                'new_career_started': False,
                'receipt': 'ss_sieve_opening_receipt.json',
            }
            self.cycle['phase'] = 'growing'
            self.session.save()
            return result_route
        attempt['ss_sieve_opening'] = {'confirmed': confirmed, 'reason': route.get('reason'),
                                     'receipt': 'ss_sieve_opening_receipt.json'}
        self.session.save()
        if not confirmed:
            return {**route, 'ok': False}
        atomic_json(root / 'session/ordinary_opening_owner.json', {
            'run_session_id': root.name, 'device': self.endpoint, 'status': 'complete',
            'new_career_started': True, 'trainee_identity': {'variant_id': setup['character_id'],
                'character_id': setup['character_id'][:4], 'basis': 'ordinary_start_confirmed_with_selected_costume'}})
        save_opening_support_observation(ctx, setup, route.get('support_setup_observation'))
        if len(self.session.data['attempts']) > 1:
            previous = self.session.data['attempts'][-2]
            if previous.get('ss_sieve_outcome') == 'screened_out':
                previous['status'] = 'abandoned_and_restarted'
        self.cycle['phase'] = 'growing'
        self.session.save()
        return route

    def _abandon(self, root):
        from core.auto_trainer.cut_restart_route import CutRestartRoute

        self.control.checkpoint('ss_sieve_abandon')
        self.cycle['phase'] = 'abandoning'
        self.session.save()
        ctx = SimpleNamespace(root=root, run_id=root.name, port=self.port)
        route = (self.restart or CutRestartRoute(self.session))(
            ctx, root / 'ss_sieve_abandon', stop_after_abandon=True)
        atomic_json(root / 'ss_sieve_abandon_receipt.json', route)
        attempt = self.session.data['attempts'][-1]
        confirmed = (route.get('ok') is True and route.get('reason') == 'ordinary_abandon_confirmed'
                     and not self.session.data.get('pending_action') and not self.session.data.get('tp_refill_pending'))
        attempt['ss_sieve_abandon'] = {'confirmed': confirmed, 'reason': route.get('reason'),
                                     'receipt': 'ss_sieve_abandon_receipt.json'}
        self.session.save()
        if not confirmed:
            attempt['ss_sieve_outcome'] = 'failed'
            return False
        attempt.update(status='abandoned', ss_sieve_outcome='screened_out')
        self.cycle['phase'] = 'between_attempts'
        self.session.save()
        self._publish_state()
        return True

    def _maintain(self, root):
        try:
            from core.auto_trainer.pacer_storage import maintain
            maintain(Path(__file__).resolve().parents[1], exclude_run=root, exclude_batch=self.session.root,
                     runtime_owner={'run_root': root, 'batch_root': self.session.root, 'endpoint': self.endpoint})
        except Exception as error:
            logging.getLogger(__name__).warning('截图维护未完成：%s', type(error).__name__)

    def run(self, *, resume=False):
        from scripts import larc_autonomous_run as career

        data = self.session.data
        if resume and not sieve_resumable(data) or not resume and self.cycle['phase'] != 'ready':
            return {'status': 'stopped', 'reason': 'ss_sieve_resume_not_allowed', 'session_root': str(self.session.root)}
        if data.get('pending_action') or data.get('tp_refill_pending'):
            return {'status': 'stopped', 'reason': 'ss_sieve_pending_transaction', 'session_root': str(self.session.root)}
        runner = self.runner or career.run
        try:
            root = self.session.resume_attempt() if resume else None
            with bind(self.control, self.endpoint):
                while True:
                    if root is None:
                        if len(data['attempts']) >= self.session.config.max_attempts:
                            return self._stop('limit_reached', 'ss_sieve_attempt_limit')
                        self.control.checkpoint('ss_sieve_attempt_start')
                        root = _ordinary_attempt(self.session, self.endpoint)
                        self.session.begin_attempt(root)
                        self._publish_state()
                        args = self._args(root)
                        opening = self._open(root, args)
                        if opening.get('ok') is not True:
                            data['attempts'][-1].update(status='opening_failed', ss_sieve_outcome='failed')
                            self.cycle['phase'] = 'opening_failed'
                            return self._stop('stopped' if opening.get('normal_stop') else 'route_failed',
                                              opening.get('reason') or 'ss_sieve_opening_unverified', route=opening)
                        self._maintain(root)
                    else:
                        args = self._args(root)
                        args._player_live_takeover = True
                        args._continuation_user_continue = True
                        self._publish_state()
                    with current_page_resume(self.control):
                        report, code = runner(args)
                    store = load_for_run(root)
                    store.record_segment(report)
                    reason = report.get('stop_reason') or 'career_stopped'
                    if reason == 'player_resume_current_page' and not self.control.stopping:
                        continue
                    attempt = data['attempts'][-1]
                    if report.get('run_session_id') == root.name and report.get('career_complete') is True and code == 0:
                        ace_cfg = self.cycle.get('ace_config')
                        if ace_cfg is None:
                            attempt.update(status='completed', ss_sieve_outcome='completed')
                            self.cycle['phase'] = 'completed'
                            store.choose('completed', evidence={'same_attempt': root.name, 'career_complete': True})
                            return self._stop('completed', reason)
                        facts = (self.graduation_inspector or inspect_completed_career)(root)
                        grad = evaluate_graduation(ace_cfg, facts)
                        attempt['graduation'] = grad
                        atomic_json(root / 'ace_graduation_receipt.json', grad)
                        if grad['status'] == 'unknown':
                            attempt.update(status='completed_unknown', ss_sieve_outcome='completed')
                            self.cycle['phase'] = 'completed'
                            store.choose('completed', evidence={
                                'same_attempt': root.name, 'career_complete': True, 'graduation': 'unknown'})
                            return self._stop('completed', 'ace_graduation_unknown_preserved')
                        if grad['status'] == 'graduated':
                            attempt.update(status='completed', ss_sieve_outcome='graduated')
                            graduated_total = sum(row.get('ss_sieve_outcome') == 'graduated' for row in data['attempts'])
                            if graduated_total >= ace_cfg['target_graduates']:
                                self.cycle['phase'] = 'completed'
                                store.choose('completed', evidence={
                                    'same_attempt': root.name, 'career_complete': True, 'graduated': graduated_total})
                                return self._stop('completed', 'ace_target_graduates_reached')
                            self.session.finish_attempt('ace_graduated_continue_batch')
                        else:
                            attempt.update(status='completed_unqualified', ss_sieve_outcome='unqualified')
                            self.session.finish_attempt('ace_graduation_unqualified')
                        self.cycle['phase'] = 'between_attempts'
                        self.session.save()
                        self._publish_state()
                        root = None
                        continue
                    if self.control.stopping or report.get('stopped_by_control'):
                        return self._stop('stopped', reason)
                    if report.get('status') in {'batch_limit_reached', 'handoff'}:
                        return self._stop('stopped', reason)
                    self.session.finish_attempt(reason)
                    if (not data.get('pending_action') and not data.get('tp_refill_pending')
                            and (authorized_sieve_failure(store, root, report)
                                 or authorized_continuation_restart(store, root, report))):
                        if len(data['attempts']) >= self.session.config.max_attempts:
                            attempt.update(status='last_attempt_preserved', ss_sieve_outcome='completed')
                            self.cycle['phase'] = 'completed'
                            self.session.save()
                            return self._stop('limit_reached', 'last_attempt_preserved_no_remaining_budget')
                        if not self._abandon(root):
                            return self._stop('route_failed', 'ss_sieve_abandon_unverified')
                        root = None
                        continue
                    attempt['ss_sieve_outcome'] = 'failed'
                    self.cycle['phase'] = 'career_failed'
                    return self._stop('route_failed', 'ss_sieve_failure_unverified' if reason == SS_SIEVE_FAILURE else reason)
        except RunStopped as error:
            return self._stop('stopped', error.reason)
        except Exception as error:
            if data['attempts'] and self.cycle['phase'] in {'opening', 'abandoning', 'growing'}:
                data['attempts'][-1].setdefault('ss_sieve_outcome', 'failed')
            return self._stop('route_failed', type(error).__name__)
        finally:
            self.control.finish()
