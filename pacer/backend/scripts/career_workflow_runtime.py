"""Persistent graph adapter around existing career, probe and restart owners."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from core.auto_trainer.cut_farming import atomic_json, at_cutoff
from core.career_workflow import normalize, evaluate, ss_sieve_node, ace_cycle_node, cycle_sieve_config

DOCUMENT = 'session/career_workflow.json'
STATE = 'session/career_workflow_state.json'
CONTEXT = 'session/career_workflow_context.json'
SS_SIEVE_FAILURE = 'ss_sieve_confirmed_failure'
CONTINUATION_GATE_FAILURE = 'continuation_gate_failed'
MEI_UNLOCK_FAILURE = 'mei_unlock_failed'
TRAINING_FAILURE_RESTART = 'training_failure_restart'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig')) if Path(path).is_file() else {}


def sieve_source_matches(source, run_root, turn):
    if (not isinstance(source, dict) or source.get('run_session_id') != run_root.name
            or source.get('turn_index') != turn):
        return False
    try:
        path = Path(source['path']).resolve(strict=True)
        return (path.is_relative_to(run_root.resolve()) and path.stat().st_size <= 32 * 1024 * 1024
                and _file_sha256(path) == source.get('sha256'))
    except (OSError, KeyError, TypeError, ValueError):
        return False


def authorized_sieve_failure(store, run_root, report):
    run_root = Path(run_root).resolve()
    progress = read(run_root / 'progress.json')
    packet = report.get('ss_sieve')
    if (not isinstance(packet, dict) or report.get('stop_reason') != SS_SIEVE_FAILURE
            or report.get('run_session_id') != run_root.name or cycle_sieve_config(store.document) is None
            or store.data.get('run_id') != run_root.name or store.data.get('attempt_root') != str(run_root)
            or progress.get('run_session_id') != run_root.name or progress.get('device') != store.data.get('endpoint')
            or packet.get('run_id') != run_root.name or packet.get('endpoint') != progress.get('device')
            or packet.get('turn') != progress.get('next_turn') or packet.get('turn_consumed') is not False
            or packet.get('decision') != 'screen_out' or packet.get('known') is not True
            or read(run_root / DOCUMENT) != store.document):
        return False
    saved = store.data.get('ss_sieve_checks', {}).get(run_root.name, {}).get(packet.get('checkpoint'))
    return packet == saved and sieve_source_matches(packet.get('source'), run_root, packet['turn'])


def authorized_continuation_restart(store, run_root, report):
    run_root = Path(run_root).resolve()
    progress = read(run_root / 'progress.json')
    packet = read(run_root / 'session/ace_continuation_failure.json')
    if (not isinstance(packet, dict) or not packet
            or report.get('stop_reason') != CONTINUATION_GATE_FAILURE
            or report.get('run_session_id') != run_root.name
            or (ss_sieve_node(store.document) is None and ace_cycle_node(store.document) is None)
            or store.data.get('run_id') != run_root.name or store.data.get('attempt_root') != str(run_root)
            or progress.get('run_session_id') != run_root.name or progress.get('device') != store.data.get('endpoint')
            or packet.get('run_id') != run_root.name or packet.get('endpoint') != progress.get('device')
            or packet.get('turn') != progress.get('next_turn') or packet.get('turn_consumed') is not False
            or packet.get('decision') != 'screen_out' or packet.get('known') is not True
            or read(run_root / DOCUMENT) != store.document):
        return False
    saved = store.data.get('ace_continuation_checks', {}).get(run_root.name, {}).get(packet.get('node'))
    return packet == saved and sieve_source_matches(packet.get('source'), run_root, packet['turn'])


def authorized_mei_unlock_restart(store, run_root, report):
    from types import SimpleNamespace
    from core.auto_trainer.larc_mei_unlock_policy import failed_mei_gamble

    run_root = Path(run_root).resolve()
    progress = read(run_root / 'progress.json')
    if (report.get('stop_reason') != MEI_UNLOCK_FAILURE
            or report.get('run_session_id') != run_root.name
            or (ss_sieve_node(store.document) is None and ace_cycle_node(store.document) is None)
            or store.data.get('run_id') != run_root.name or store.data.get('attempt_root') != str(run_root)
            or progress.get('run_session_id') != run_root.name or progress.get('device') != store.data.get('endpoint')
            or read(run_root / DOCUMENT) != store.document):
        return False
    failure = failed_mei_gamble(SimpleNamespace(root=run_root, run_id=run_root.name, progress=progress))
    return isinstance(failure, dict)


def authorized_training_failure_restart(store, run_root, report):
    from core.auto_trainer.cut_training_failure import training_failure_should_restart

    run_root = Path(run_root).resolve()
    progress = read(run_root / 'progress.json')
    if (report.get('stop_reason') != TRAINING_FAILURE_RESTART
            or report.get('run_session_id') != run_root.name
            or (ss_sieve_node(store.document) is None and ace_cycle_node(store.document) is None)
            or store.data.get('run_id') != run_root.name or store.data.get('attempt_root') != str(run_root)
            or progress.get('run_session_id') != run_root.name or progress.get('device') != store.data.get('endpoint')
            or read(run_root / DOCUMENT) != store.document):
        return False
    packet = read(run_root / 'session/ace_training_failure.json')
    if (not isinstance(packet, dict) or packet.get('run_id') != run_root.name
            or packet.get('endpoint') != progress.get('device')):
        return False
    binding = read(run_root / 'session/ace_cycle_binding.json')
    tf_cfg = binding.get('training_failure_restart') if isinstance(binding, dict) else None
    if not isinstance(tf_cfg, dict) or tf_cfg.get('enabled') is not True:
        return False
    failure = packet.get('failure')
    return (isinstance(failure, dict)
            and failure.get('run_id') == run_root.name
            and training_failure_should_restart(
                failure,
                ignore_wisdom=bool(tf_cfg.get('ignore_wisdom', True)),
                stop_at_senior_expedition=bool(tf_cfg.get('stop_at_senior_expedition', True)),
                period=packet.get('period'),
            ))


def screening_node(store):
    node = store.node if store.node['kind'] == 'cut' else store.nodes.get(store.data.get('screening_node'))
    return node if node and node['kind'] == 'cut' and (node.get('screening') or {}).get('enabled') is True else None


def screening_due(point, provider, data):
    """Dates are lower bounds; a late attach observes the current home only."""
    timing = point['check_at']
    if timing in {'home', 'cut_checkpoint'}:
        return True
    period = provider.get('larc_period') or {}
    from core.auto_trainer.larc_calendar import period_ordinal
    actual = period_ordinal(period.get('value') or {}) if period.get('status') == 'verified' else None
    if actual is None:
        return None
    if timing == 'game_date':
        date = point['game_date']
        planned = (date['year'] - 1) * 24 + (date['month'] - 1) * 2 + (date['half'] == 'second')
        return actual >= planned
    if timing == 'junior':
        return actual >= 23
    return bool(data.get('inheritance_seen') or actual >= 32)


def screening_terminal(store, report):
    """A real game completion cannot fabricate an unobserved future checkpoint."""
    if (store.node['kind'] != 'cut' or screening_node(store) is None
            or not (report.get('career_complete') is True or report.get('status') == 'completed')):
        return False
    result = {'outcome': 'unknown', 'reason': 'career_completed_before_screening_checkpoints',
              'run_id': store.data.get('run_id'), 'source': report.get('output')}
    store.data['last_result'] = result
    store.choose('unknown', evidence=result)
    store.data.update(status='preserved', terminal_screening_unknown=True)
    store.save()
    return True


def checkpoint_restart_record(store, run_root):
    record = store.data.get('checkpoint_restart') or {}
    return record if isinstance(record, dict) and record.get('run_id') == Path(run_root).name else None


def screening_signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _file_sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _checkpoint_signatures(document, point):
    return {
        'document_sha256': screening_signature(document),
        'checkpoint_sha256': screening_signature(point),
    }


def _matches_checkpoint_signatures(record, store_document, disk_document, point):
    if not isinstance(record, dict):
        return False
    doc_sig = screening_signature(store_document)
    if record.get('document_sha256') != doc_sig:
        return False
    if record.get('document_sha256') != screening_signature(disk_document):
        return False
    if record.get('checkpoint_sha256') != screening_signature(point):
        return False
    return True


def checkpoint_restart_permission(store, run_root):
    """An explicit failed-point action replaces absence proof, never its value."""
    run_root = Path(run_root).resolve()
    record = checkpoint_restart_record(store, run_root)
    if (not record or record.get('status') not in {'requested', 'authorized'} or store.node['kind'] != 'restart'
            or store.data.get('status') == 'manual_career' or store.data.get('manual_continue_current') is True):
        return None
    node = store.nodes.get(record.get('screen_id')) or {}
    if (node.get('screening') or {}).get('enabled') is not True:
        return None
    point = next((p for p in (node.get('screening') or {}).get('checkpoints', []) if p['id'] == record.get('checkpoint_id')), {})
    entry = store.data.get('screening', {}).get(run_root.name + ':' + node.get('id', ''), {})
    result = entry.get('checks', {}).get(point.get('id'), {})
    progress = read(run_root / 'progress.json')
    if (not _matches_checkpoint_signatures(record, store.document, read(run_root / DOCUMENT), point)
            or point.get('on_fail') != 'not_matched_branch' or result.get('outcome') != 'not_matched'
            or result.get('action') != 'not_matched_branch' or result.get('action_explicit') is not True
            or result.get('normal_home_verified') is not True
            or record.get('turn') != progress.get('next_turn')
            or progress.get('run_session_id') != run_root.name or progress.get('device') != store.data.get('endpoint')
            or store.data.get('run_id') != run_root.name or store.data.get('attempt_root') != str(run_root)
            or store.edges.get((node.get('id'), 'not_matched')) != store.node['id']):
        return None
    return record


def prepare_checkpoint_restart(session, run_root, report):
    """Release only this explicit decision to the existing batch restart owner."""
    if not (session.root / DOCUMENT).is_file():
        return None
    store = load_for_run(run_root)
    record = checkpoint_restart_record(store, run_root)
    if record is None:
        return None
    permission = checkpoint_restart_permission(store, run_root)
    attempt = (session.data.get('attempts') or [{}])[-1]
    if (permission is None or report.get('stop_reason') != 'continuation_gate_failed'
            or attempt.get('run_id') != Path(run_root).name or Path(attempt.get('run_root', '')).resolve() != Path(run_root).resolve()
            or session.data.get('pending_action') or session.data.get('tp_refill_pending')
            or session.data.get('status') == 'limit_reached' or attempt.get('status') in {'abandoned', 'abandoned_and_restarted'}):
        return False
    if permission['status'] == 'requested':
        permission.update(status='authorized', authorized_at=time.time())
        attempt.setdefault('workflow_restart_authorizations', []).append({
            'id': permission['id'], 'checkpoint_id': permission['checkpoint_id'],
            'cut_proof': deepcopy(store.data.get('last_cut')), 'previous_batch_status': session.data['status'],
            'tp_items_reserved': session.data['tp_items_reserved'], 'carrots_reserved': session.data['carrots_reserved']})
        store.save()
    # Do not clear cut_found/last_probe or reset configuration/reservations.
    session.data['status'] = 'running'
    attempt['status'] = 'workflow_restart_authorized'
    session.save()
    return True


def checkpoint_restart_started(session, run_root):
    if not (session.root / DOCUMENT).is_file():
        return None
    store = load_for_run(run_root)
    record = checkpoint_restart_record(store, run_root)
    if record is None:
        return None
    if checkpoint_restart_permission(store, run_root) is None or record['status'] != 'authorized':
        return False
    record.update(status='started', started_at=time.time())
    store.save()
    return True


def checkpoint_restart_finished(session, run_root, route):
    if not (session.root / DOCUMENT).is_file():
        return
    store = load_for_run(run_root)
    record = checkpoint_restart_record(store, run_root)
    if record and record.get('status') == 'started':
        record.update(status='consumed' if route.get('ok') is True else 'uncertain',
            completed_at=time.time(), route_reason=route.get('reason'))
        store.save()


class WorkflowState:
    def __init__(self, root, document=None, *, endpoint=None):
        self.root = Path(root).resolve()
        self.document = normalize(document or read(self.root / DOCUMENT))
        self.nodes = {node['id']: node for node in self.document['run_graph']['nodes']}
        self.edges = {(edge['from'], edge['outcome']): edge['to']
                      for edge in self.document['run_graph']['edges']}
        self.data = read(self.root / STATE) or {'schema_version': 'uma.career_workflow_state.v1',
            'workflow_id': self.document['id'], 'endpoint': endpoint,
            'active_node': self.document['run_graph']['entry'], 'status': 'running',
            'branches': [], 'segments': [], 'actions_consumed': 0}
        if (self.data['workflow_id'] != self.document['id']
                or endpoint is not None and self.data.get('endpoint') != endpoint):
            raise ValueError('工作流记录与原配置或设备不一致。')
        self.advance_controls()

    @property
    def node(self):
        return self.nodes[self.data['active_node']]

    def save(self):
        atomic_json(self.root / STATE, self.data)

    def advance_controls(self):
        for _ in range(len(self.nodes)):
            if self.node['kind'] != 'entry':
                return
            self.choose('next')
        raise ValueError('工作流入口循环。')

    def choose(self, outcome, *, evidence=None):
        current = self.data['active_node']
        target = self.edges[(current, outcome)]
        self.data['branches'].append({'node': current, 'outcome': outcome, 'next': target,
            'run_id': self.data.get('run_id'), 'turn': self.data.get('turn'),
            'evidence': deepcopy(evidence), 'at': time.time()})
        self.data.update(active_node=target, status='running')
        self.save()
        return self.node

    def bind(self, run_root):
        run_root = Path(run_root).resolve()
        progress = read(run_root / 'progress.json')
        if progress.get('run_session_id') != run_root.name or progress.get('device') != self.data['endpoint']:
            raise ValueError('工作流本局标识或设备不匹配。')
        if self.data.get('run_id') not in (None, run_root.name):
            for key in ('cut_found', 'cut_found_run_id', 'last_cut', 'screening_recheck_cut', 'inheritance_seen'):
                self.data.pop(key, None)
        self.data.update(run_id=run_root.name, attempt_root=str(run_root), turn=progress['next_turn'])
        self.save()
        atomic_json(run_root / DOCUMENT, self.document)
        atomic_json(run_root / CONTEXT, {'run_id': run_root.name, 'device': progress['device'],
            'state_root': str(self.root)})
        if ss_sieve_node(self.document) is not None or ace_cycle_node(self.document) is not None:
            return
        from scripts.larc_continuation_runtime import save_options
        from core.auto_trainer.larc_continuation import defaults
        config = defaults(); config['enabled'] = True
        config['junior']['mode'] = config['inheritance']['mode'] = 'observe'
        save_options(run_root, config)

    def record_segment(self, report):
        key = report.get('output')
        if key and not any(row.get('output') == key for row in self.data['segments']):
            consumed = max(0, int(report.get('consumed_turns_this_run') or 0))
            self.data['segments'].append({'output': key, 'reason': report.get('stop_reason'),
                'consumed': consumed, 'run_id': report.get('run_session_id')})
            self.data['actions_consumed'] += consumed
            self.save()


def load_for_run(run_root):
    run_root = Path(run_root).resolve()
    binding = read(run_root / CONTEXT)
    if not binding:
        return None
    progress = read(run_root / 'progress.json')
    state_root = Path(binding['state_root']).resolve()
    allowed = {run_root}
    if run_root.parent.name == 'attempts':
        allowed.add(run_root.parent.parent)
    if state_root not in allowed:
        from scripts.career_batch_runtime import owns_child
        if owns_child(state_root, run_root, progress.get('device')):
            allowed.add(state_root)
    if (state_root not in allowed or binding.get('run_id') != run_root.name
            or binding.get('device') != progress.get('device')):
        raise ValueError('工作流不能复用其他局或外部目录。')
    store = WorkflowState(state_root, endpoint=progress['device'])
    if store.data.get('run_id') != run_root.name:
        raise ValueError('工作流当前attempt不匹配。')
    return store


def attach_attempt(session, root):
    if not (session.root / DOCUMENT).is_file():
        return
    store = WorkflowState(session.root, endpoint=f'127.0.0.1:{session.config.device_port}')
    if store.node['kind'] == 'restart':
        store.choose('next', evidence={'restart_owner': 'CutRestartRoute', 'new_attempt_created': True})
    store.bind(root)


def cut_observed(ctx, result, provider, *, cutoff=False):
    store = getattr(getattr(ctx, 'continuation_runtime', None), 'store', None) or load_for_run(ctx.root)
    if store is None:
        return
    ctx.workflow_continue_found = False
    state = result.get('state', 'unknown')
    store.data.update(turn=ctx.progress['next_turn'], last_cut={
        'state': state, 'returned': result.get('returned') is True,
        'turn': ctx.progress['next_turn'], 'run_id': ctx.run_id,
        'source': result.get('source')})
    enhanced = screening_node(store)
    if enhanced is not None:
        store.data.pop('screening_recheck_cut', None)
        if state == 'present':
            store.data.update(cut_found=True, cut_found_run_id=ctx.run_id)
            ctx.workflow_continue_found = (result.get('returned') is True and
                (store.node['kind'] in {'continue_current', 'career'} or store.node['id'] == enhanced['id']))
        store.save()
        if result.get('returned') is True and checkpoint_restart_permission(store, ctx.root) is not None:
            return 'restart'
        # Let an active explicit screening plan decide absent/unknown results.
        # A restart still uses the original owner's fail-closed probe handling.
        return store.node['id'] == enhanced['id']
    if state == 'present':
        store.data['cut_found'] = True
        # A confirmed hit outranks subsequent screening criteria and cannot restart.
        cut=(store.node if store.node['kind']=='cut' else
            store.nodes.get(store.data.get('screening_node')))
        if cut and store.node['kind'] not in {'career', 'finish'}:
            store.data['active_node'] = cut['id']
            store.choose('matched', evidence={'cut_state': 'present'})
        ctx.workflow_continue_found = (store.node['kind'] in {'continue_current', 'career'}
                                      and result.get('returned') is True)
    elif state != 'absent' or not result.get('returned'):
        if store.node['kind'] == 'cut':
            waiting = store.node['id']
            store.choose('unknown', evidence={'cut_state': 'unknown'})
            store.data.update(waiting_on=waiting, status='waiting')
    elif cutoff and store.node['kind'] == 'cut':
        store.choose('not_matched', evidence={'cut_state': 'absent', 'deadline': True})
    store.save()


def _facts(ctx, provider):
    from scripts.larc_continuation_collect import base_facts
    facts = base_facts(ctx, provider)
    state = provider.get('state_context') or {}
    stats = state.get('current_stats') or {}
    facts.update({key: stats.get(key) for key in ('speed', 'stamina', 'power', 'guts', 'wisdom', 'skill_pt')})
    values = [stats.get(key) for key in ('speed', 'stamina', 'power', 'guts', 'wisdom')]
    facts.update(vitality=state.get('vitality'), remaining_turns=state.get('remaining_turns'),
                 stat_sum=sum(values) if all(type(v) in {int, float} for v in values) else None)
    period = provider.get('larc_period') or {}
    date = period.get('value') or {} if period.get('status') == 'verified' else {}
    from core.auto_trainer.larc_calendar import period_ordinal
    ordinal = period_ordinal(date)
    facts.update(calendar_year=date.get('year_index'), calendar_month=date.get('month'),
                 calendar_turn=ordinal + 1 if ordinal is not None else None)
    return facts


def checkpoint_due(timing, provider, data):
    period=provider.get('larc_period') or {}
    date=period.get('value') or {}
    if timing=='home':return True
    if timing=='cut_checkpoint':
        return (data.get('last_cut') or {}).get('turn')==data.get('turn')
    if period.get('status')!='verified':return False
    if timing=='junior':
        return (date.get('year_index',0)>1 or
            (date.get('year_index'),date.get('month'),date.get('half'))==(1,12,'second'))
    return (date.get('year_index',0)>=2 and (data.get('inheritance_seen')
        or date.get('year_index',0)>2 or date.get('month',0)>=5))


def apply_policy(ctx, store, node):
    policy_id=node.get('policy',store.data.get('active_policy','default'))
    key=(ctx.run_id,node['id'],policy_id)
    if tuple(store.data.get('applied_policy',[]))==key:return
    policy=store.document['policies'][policy_id]
    phase=(node['kind'] if node['kind'] in {'cut','career'} else store.data.get('active_task',
        'cut' if any(row['kind']=='cut' for row in store.nodes.values()) else 'career'))
    options=policy.get('cut_options' if phase=='cut' else 'career_options',{})
    goal=read(Path(ctx.root)/'goal.json')
    if goal:
        from core.auto_trainer.run_support_profile import freeze_declaration
        if 'support_profile' in options:ctx.support_profile=freeze_declaration(ctx.root,ctx.run_id,options['support_profile'])
        from scripts.larc_panel_setup import parse_targets,parse_strategy_options
        from core.auto_trainer.config import UGParentGoal,load_goal_config,validate_goal_config
        from core.auto_trainer.larc_policy import normalize_larc_policy
        from core.auto_trainer.larc_user_preferences import normalize as preferences
        if 'targets' in options:goal['target_stats']=parse_targets(options['targets'])
        if 'failure_limit' in options:
            _,goal['training_failure_rate_limit']=parse_strategy_options(options.get('strategy','baseline'),options['failure_limit'])
        # A null template uses the policy already frozen in this run.
        if options.get('larc_policy') is not None:
            supplied = options['larc_policy']
            policy = normalize_larc_policy(supplied, phase)
            saved = goal.get('larc_policy') or {}
            if policy.get('revision') == saved.get('revision') in ('1.9.5', '1.9.6', '1.9.7', '1.9.8', '1.9.9', '1.9.10', '1.9.11', '1.9.12', '2.0', '2.1', '2.2'):
                # Keep defaults already frozen by the ordinary Goal factory
                # when applying the same revision's unmaterialized UI snapshot.
                for key in ('year1_cycle_opportunity_enabled', 'after_target_credit'):
                    if key not in supplied and key in saved:
                        policy[key] = deepcopy(saved[key])
                policy['attribute_weights'] = {**saved.get('attribute_weights', {}),
                                               **policy.get('attribute_weights', {})}
            goal['larc_policy'] = policy
        configured=options.get('decision_preferences',(options.get('cut') or {}).get('decision_preferences'))
        if configured is not None:
            policy = goal.setdefault('larc_policy', {})
            inherited = policy.get('decision_preferences') or {}
            policy['decision_preferences'] = preferences({**inherited, **configured})
        configured_events=options.get('event_preferences',(options.get('cut') or {}).get('event_preferences'))
        if configured_events is not None:
            from core.auto_trainer.larc_event_library import normalize_preferences as normalize_event_preferences
            goal['event_preferences']=normalize_event_preferences(configured_events)
        if options.get('race_retry') is not None:
            from core.auto_trainer.race_retry_budget import normalize as normalize_retry
            goal['race_retry']=normalize_retry(options['race_retry'])
        if 'running_style' in options:
            from core.auto_trainer.larc_custom_race_catalog import normalize_running_style
            goal['running_style']=normalize_running_style(options.get('running_style'))
        if 'race_running_styles' in options:
            from core.auto_trainer.larc_custom_race_catalog import normalize_race_running_styles
            goal['race_running_styles']=normalize_race_running_styles(options.get('race_running_styles'))
        if phase=='career' and 'custom_races' in options:
            from core.auto_trainer.larc_custom_race_catalog import normalize_custom_races
            goal['custom_races']=normalize_custom_races(options.get('custom_races'))
        validate_goal_config(UGParentGoal.from_dict(goal))
        atomic_json(Path(ctx.root)/'goal.json',goal)
        ctx.goal=load_goal_config(Path(ctx.root)/'goal.json')
        if phase=='career' and 'manual_handoff' in options:
            from core.auto_trainer.larc_manual_handoff import apply_workflow_options
            apply_workflow_options(ctx.root,options['manual_handoff'],
                request_id='workflow:'+':'.join(str(v) for v in key))
        from core.auto_trainer.runtime_control import current
        control=current()
        if control is not None:
            control.ss_planning_horizon=(goal.get('larc_policy') or {}).get('ss_planning_horizon',
                'junior_cutoff_discard' if phase=='cut' else 'full_career')
    store.data.update(active_policy=policy_id,applied_policy=list(key),active_task=phase)
    store.save()


class WorkflowRuntime:
    def __init__(self, ctx, store):
        self.ctx, self.store, self.path = ctx, store, store.root / STATE
        self.state = store.data
        self.sync_career_flow()

    def _cycle_continuation_runtime(self):
        if ss_sieve_node(self.store.document) is None and ace_cycle_node(self.store.document) is None:
            return None
        opt_path = Path(self.ctx.root) / 'session/larc_continuation_options.json'
        if not opt_path.is_file():
            return None
        saved = read(opt_path)
        if saved.get('run_session_id') != self.ctx.run_id:
            raise ValueError('continuation_options_run_mismatch')
        cfg = saved.get('config') or {}
        if not cfg.get('enabled'):
            return None
        from scripts.larc_continuation_runtime import ContinuationRuntime
        return ContinuationRuntime(
            self.ctx, cfg,
            inspect=getattr(self.ctx, '_continuation_inspect', None),
            notify=getattr(self.ctx, '_continuation_notify', None),
        )

    def sieve_checkpoint_due(self, checkpoint):
        node = ss_sieve_node(self.store.document) or ace_cycle_node(self.store.document)
        if (node is None or self.store.node['id'] != node['id'] or self.state.get('status') != 'running'
                or checkpoint not in {'pre_debut_end', 'year1_end'}):
            return None
        config = node['ss_sieve'] if 'ss_sieve' in node else (node.get('ace_cycle') or {}).get('ss_sieve')
        if not config:
            return None
        if checkpoint == 'pre_debut_end' and not config['pre_debut']:
            return None
        if checkpoint in self.state.get('ss_sieve_checks', {}).get(self.ctx.run_id, {}):
            return None
        return dict(config)

    def before_ss_sieve(self, checkpoint, evidence):
        config = self.sieve_checkpoint_due(checkpoint)
        if config is None:
            return {}
        from core.auto_trainer.larc_ss_sieve import decide, needs_ready_count
        evidence = evidence if isinstance(evidence, dict) else {}
        root = Path(self.ctx.root).resolve()
        turn = self.ctx.progress['next_turn']
        endpoint = self.state['endpoint']
        source = evidence.get('source')
        ready = evidence.get('ready_count')
        count = evidence.get('full5_ss')
        period = evidence.get('period')
        period = period if isinstance(period, dict) else {}
        date = period.get('value')
        date = date if isinstance(date, dict) else {}
        if checkpoint == 'pre_debut_end':
            page_matches = (evidence.get('page_kind') == 'race_day' and date.get('pre_debut') is True
                            and evidence.get('target_id') == 'participate_in_debut_race')
        else:
            page_matches = (evidence.get('page_kind') == 'training_home'
                            and (date.get('year_index'), date.get('month'), date.get('half')) == (2, 1, 'first'))
        bound = (evidence.get('schema_version') == 'larc.ss_sieve_evidence.v1'
                 and evidence.get('checkpoint') == checkpoint and evidence.get('run_id') == self.ctx.run_id
                 and evidence.get('turn') == turn and evidence.get('endpoint') == endpoint
                 and page_matches and period.get('status') == 'verified' and isinstance(source, dict)
                 and period.get('source_frame_sha256') == source.get('sha256')
                 and sieve_source_matches(source, root, turn))
        ready_source = evidence.get('ready_source') if needs_ready_count(checkpoint, count, config) else None
        if not sieve_source_matches(ready_source, root, turn):
            ready, ready_source = None, None
        if bound:
            decision = decide(checkpoint=checkpoint, full5_ss=count, ready_count=ready, config=config)
        else:
            decision = {'decision': 'continue', 'known': False, 'reason': 'checkpoint_source_unverified'}
        packet = {**decision, 'checkpoint': checkpoint, 'run_id': self.ctx.run_id, 'turn': turn,
                  'endpoint': endpoint, 'turn_consumed': False, 'full5_ss': count, 'ready_count': ready,
                  'source': source if bound else None, 'ready_source': ready_source if bound else None,
                  'evidence_reason': evidence.get('evidence_reason')}
        self.state.setdefault('ss_sieve_checks', {}).setdefault(self.ctx.run_id, {})[checkpoint] = packet
        try:
            self.store.save()
        except OSError:
            # Without a persisted decision there is no abandonment authority.
            self.state['ss_sieve_record_error'] = 'checkpoint_record_unavailable'
            return {}
        if packet['known'] is True and packet['decision'] == 'screen_out':
            return {'stop_reason': SS_SIEVE_FAILURE, 'ss_sieve': packet}
        return {}

    def observe_report(self, report):
        cont = self._cycle_continuation_runtime()
        if cont is not None:
            cont.observe_report(report)
        def seen(value):
            if isinstance(value, dict):
                if value.get('performed') is True and (value.get('decision') or {}).get('reason') == 'inheritance_factor_confirm':
                    return True
                return any(seen(item) for item in value.values())
            return isinstance(value, list) and any(seen(item) for item in value)
        if seen(report):
            self.state['inheritance_seen'] = True
            self.store.save()

    def can_continue_junior(self):
        return (self.store.node['kind'] == 'cut' and screening_node(self.store) is not None
            or self.store.node['kind'] not in {'cut', 'restart'} or self.state.get('status') == 'manual_career')

    def sync_career_flow(self, period=None):
        if ss_sieve_node(self.store.document) is not None or ace_cycle_node(self.store.document) is not None:
            # The ordinary attempt already froze its full Goal and policy.
            self.state.update(active_task='career', owner_phase='career', career_flow='full_career')
            return True
        full = self.can_continue_junior()
        hook = getattr(self.ctx, 'cut_farming_hook', None)
        if hook is not None:
            if self.store.node['kind']=='cut' and self.state.get('owner_phase')!='cut':
                hook.pending=True;hook.pending_reasons.add('initial')
            hook.continuation_flow = full
        if self.store.node['kind']=='cut':
            self.state['screening_node']=self.store.node['id']
        if self.store.node['kind'] in {'career','cut','condition'}:
            apply_policy(self.ctx,self.store,self.store.node)
        self.state['owner_phase']=self.store.node['kind']
        self.state['career_flow'] = 'full_career' if full else 'cut_screening'
        return full

    def _resume_pending(self):
        if self.state.get('status') == 'waiting' and self.state.get('waiting_on'):
            self.state.update(active_node=self.state.pop('waiting_on'), status='running')
            if self.store.node['kind'] == 'cut' and screening_node(self.store) is not None:
                self.state['screening_recheck_cut'] = True
            self.store.save()
            return {'status': 'reobserve'}
        return None

    def _screening_home(self, node, source, provider, output):
        plan = node['screening']['checkpoints']
        key = self.ctx.run_id + ':' + node['id']
        entry = self.state.setdefault('screening', {}).setdefault(key,
            {'run_id': self.ctx.run_id, 'node_id': node['id'], 'cursor': 0, 'checks': {}})
        if entry['cursor'] >= len(plan):
            return {'status': 'not_due'}
        point = plan[entry['cursor']]
        due = screening_due(point, provider, self.state)
        if due is False:
            self.store.save()
            return {'status': 'not_due', 'checkpoint_id': point['id']}
        rows = next(iter(point['when'].values()))
        cut = self.state.get('last_cut') or {}
        known_found = self.state.get('cut_found') and self.state.get('cut_found_run_id', self.state.get('run_id')) == self.ctx.run_id
        needs_cut = point['check_at'] == 'cut_checkpoint' or any(row['kind'] == 'cut_present' for row in rows)
        fresh = (cut.get('run_id') == self.ctx.run_id and cut.get('turn') == self.ctx.progress['next_turn']
                 and cut.get('returned') is True and not self.state.get('screening_recheck_cut'))
        if due is True and needs_cut and not known_found and not fresh:
            hook = getattr(self.ctx, 'cut_farming_hook', None)
            if hook is not None:
                hook.pending = True; hook.pending_reasons.add('continuation_restart')
                self.store.save()
                return {'status': 'reobserve', 'checkpoint_id': point['id']}
        facts = _facts(self.ctx, provider)
        facts['cut_state'] = 'present' if known_found else (cut.get('state') if fresh else 'unknown')
        collected_source, collected_provider, visited = source, provider, False
        failure = None
        if due is None:
            failure = 'screening_calendar_unknown'
        else:
            from core.auto_trainer.larc_continuation import defaults, KINDS
            from scripts.larc_continuation_collect import inspect_node
            config = defaults(); config['enabled'] = True
            config['junior']['mode'] = config['inheritance']['mode'] = 'observe'
            config['junior']['conditions'] = [row for row in rows if row['kind'] in KINDS]
            for row in rows:
                if row['kind'] == 'skill_count_min':
                    config['inheritance']['skills'].extend(row['skills'])
                elif row['kind'] == 'distance_min':
                    config['inheritance']['distances'][row['distance']] = row['rank']
            junior = any(row['kind'] in {'supporter_points_min', 'bond_ready_min', 'bond_card_min', 'friend_outing_unlocked'} for row in rows)
            inheritance = bool(config['inheritance']['skills'] or config['inheritance']['distances'])
            jobs = []
            if junior:
                jobs.append(('junior', {'supporter_points', 'friend_outing_unlocked', 'supports', 'support_profile_complete'}))
            if inheritance:
                jobs.append(('inheritance', {'skills', 'skills_complete', 'skills_abstentions', 'skills_unresolved_count', 'distances'}))
            for timing, keys in jobs:
                try:
                    collected = inspect_node(self.ctx, timing, collected_source, collected_provider,
                        Path(output) / point['id'] / timing, config)
                except (OSError, ValueError) as error:
                    failure = str(error) or type(error).__name__; break
                if not collected.get('ok'):
                    failure = collected.get('reason', 'screening_inspection_unknown'); break
                facts.update({k: v for k, v in collected['facts'].items() if k in keys})
                collected_source = collected.get('source', collected_source)
                collected_provider = collected.get('provider', collected_provider)
                visited = visited or collected.get('visited') is True
                if visited and (not collected_source or collected_provider.get('valid_for_training') is not True):
                    failure = 'screening_return_menu_unverified'; break
        result = ({'outcome': 'unknown', 'reason': failure, 'conditions': []} if failure
                  else evaluate(point, facts))
        result.update(checkpoint_id=point['id'], check_at=point['check_at'],
            planned_game_date=deepcopy(point.get('game_date')) if point['check_at'] == 'game_date' else None,
            actual_game_date=deepcopy((provider.get('larc_period') or {}).get('value')),
            run_id=self.ctx.run_id, turn=self.ctx.progress['next_turn'], source=collected_source,
            normal_home_verified=collected_provider.get('valid_for_training') is True)
        field = 'on_pass' if result['outcome'] == 'matched' else 'on_fail'
        default = ('next_checkpoint' if entry['cursor'] + 1 < len(plan) else 'matched_branch') if field == 'on_pass' else 'not_matched_branch'
        action = point.get(field, default) if result['outcome'] != 'unknown' else 'hold'
        result.update(action=action, action_explicit=field in point and result['outcome'] != 'unknown')
        entry['checks'][point['id']] = deepcopy(result)
        self.state['last_result'] = deepcopy(result)
        self.state.setdefault('screening_receipts', []).append(deepcopy(result))
        if result['outcome'] != 'unknown' and action == 'next_checkpoint':
            entry['cursor'] += 1
            if entry['cursor'] < len(plan):
                self.store.save()
                return ({'status': 'reobserve', 'source': collected_source, 'checkpoint_id': point['id']}
                        if visited else {'status': 'screening_next'})
            action = 'hold'
            result['hold_reason'] = 'screening_no_next_checkpoint'
        if result['outcome'] != 'unknown' and action == 'hold':
            entry.update(selected_action='hold', hold_reason=result.get('hold_reason', 'screening_checkpoint_hold'))
            self.state.update(active_node=self.store.edges[(node['id'], 'unknown')], status='preserved',
                preservation_reason=entry['hold_reason'])
            self.state.pop('waiting_on', None)
            self.store.save()
            return {'status': 'screening_advanced'}
        branch = 'matched' if action == 'matched_branch' else 'not_matched' if action == 'not_matched_branch' else 'unknown'
        if branch == 'matched':
            entry['cursor'] += 1
        entry['selected_outcome'] = branch
        if branch == 'not_matched' and point.get('on_fail') == 'not_matched_branch' and result['outcome'] == 'not_matched':
            target = self.store.nodes[self.store.edges[(node['id'], 'not_matched')]]
            if target['kind'] == 'restart':
                self.state['checkpoint_restart'] = {'id': key + ':' + point['id'] + ':' + str(self.ctx.progress['next_turn']),
                    'run_id': self.ctx.run_id, 'screen_id': node['id'], 'checkpoint_id': point['id'],
                    'turn': self.ctx.progress['next_turn'], 'status': 'requested',
                    **_checkpoint_signatures(self.store.document, point)}
        self.store.choose(branch, evidence=result)
        if result['outcome'] == 'unknown':
            self.state.update(waiting_on=node['id'], status='waiting')
        self.store.save(); self.sync_career_flow()
        if visited:
            return {'status': 'reobserve', 'source': collected_source}
        return {'status': 'screening_advanced'}

    def before_home(self, source, provider, output):
        if self.state.get('status') == 'manual_career':
            return {'status': 'not_due'}
        self.state['turn'] = self.ctx.progress['next_turn']
        node = self.store.node
        kind = node['kind']
        changed_node=False
        if kind == 'cut' and screening_node(self.store) is not None:
            for _ in range(len(node['screening']['checkpoints']) + 1):
                value = self._screening_home(node, source, provider, output)
                if value['status'] != 'screening_next':
                    break
            if value['status'] != 'screening_advanced':
                return value
            node, kind, changed_node = self.store.node, self.store.node['kind'], True
        if kind=='career':
            cont = self._cycle_continuation_runtime()
            if cont is not None:
                outcome = cont.before_home(source, provider, output)
                if outcome.get('stop_reason') == CONTINUATION_GATE_FAILURE and isinstance(outcome.get('ace_continuation'), dict):
                    pkt = outcome['ace_continuation']
                    self.state.setdefault('ace_continuation_checks', {}).setdefault(self.ctx.run_id, {})[pkt['node']] = pkt
                    self.store.save()
                    return outcome
                if outcome.get('status') != 'not_due':
                    return outcome
            target=self.store.nodes[self.store.edges[(node['id'],'completed')]]
            checked_now=any(row['node']==node['id'] and row['outcome']=='completed'
                and row.get('run_id')==self.ctx.run_id and row.get('turn')==self.ctx.progress['next_turn']
                and (row.get('evidence') or {}).get('checkpoint_reached')
                for row in self.state['branches'])
            if target['kind']=='condition' and not checked_now and checkpoint_due(target['check_at'],provider,self.state):
                self.store.choose('completed',evidence={'checkpoint_reached':target['check_at'],
                    'career_complete':False})
                node,kind=self.store.node,self.store.node['kind']
        if kind == 'condition':
            timing = node['check_at']
            date = provider.get('larc_period', {}).get('value') or {}
            if not checkpoint_due(timing,provider,self.state):
                return {'status': 'not_due'}
            if any(row['kind']=='cut_present' for row in next(iter(node['when'].values()))):
                cut=self.state.get('last_cut') or {}
                if cut.get('run_id')!=self.ctx.run_id or cut.get('turn')!=self.ctx.progress['next_turn']:
                    hook=getattr(self.ctx,'cut_farming_hook',None)
                    if hook is not None:
                        hook.pending=True;hook.pending_reasons.add('continuation_restart')
                        return {'status':'reobserve'}
            facts = _facts(self.ctx, provider)
            historical_missing=timing=='junior' and date.get('year_index',0)>1
            if timing in {'junior', 'inheritance'} and not historical_missing:
                from core.auto_trainer.larc_continuation import defaults, KINDS
                from scripts.larc_continuation_collect import inspect_node
                config = defaults(); config['enabled'] = True
                config['junior']['mode'] = config['inheritance']['mode'] = 'observe'
                rows = next(iter(node['when'].values()))
                config['junior']['conditions'] = [row for row in rows if row['kind'] in KINDS]
                for row in rows:
                    if row['kind'] == 'skill_count_min':
                        config['inheritance']['skills'].extend(row.get('skills', []))
                    if row['kind'] == 'distance_min':
                        config['inheritance']['distances'][row['distance']] = row['rank']
                collected = inspect_node(self.ctx, timing, source, provider, Path(output), config)
                if collected.get('ok'):
                    facts.update(collected['facts'])
                else:
                    self.state.update(status='waiting', waiting_on=node['id'],
                        last_result={'outcome': 'unknown', 'reason': collected.get('reason')})
                    self.store.save()
                    return {'status': 'waiting', 'stop_reason': 'continuation_hold'}
            result = ({'outcome':'unknown','reason':'junior_checkpoint_not_observed','conditions':[]}
                if historical_missing else evaluate(node, facts))
            self.state['last_result'] = result
            self.store.choose(result['outcome'], evidence=result)
            changed_node=True
            if result['outcome'] == 'unknown':
                self.state.update(waiting_on=node['id'], status='waiting')
                self.store.save()
            node, kind = self.store.node, self.store.node['kind']
            self.sync_career_flow()
        if kind == 'restart':
            permission = checkpoint_restart_permission(self.store, self.ctx.root)
            if permission is not None:
                return {'status': 'failed', 'stop_reason': 'continuation_gate_failed', 'checkpoint_restart_id': permission['id']}
            if checkpoint_restart_record(self.store, self.ctx.root) is not None:
                return {'status': 'waiting', 'stop_reason': 'continuation_hold'}
            cut = self.state.get('last_cut') or {}
            fresh_absent = (cut.get('state') == 'absent' and cut.get('returned') is True
                and cut.get('run_id') == self.ctx.run_id and cut.get('turn') == self.ctx.progress['next_turn'])
            hook = getattr(self.ctx, 'cut_farming_hook', None)
            if self.state.get('cut_found'):
                self.state.update(status='preserved', preservation_reason='confirmed_cut_screening_not_matched')
                self.store.save()
                return {'status': 'waiting', 'stop_reason': 'continuation_hold'}
            if not fresh_absent:
                if hook is not None:
                    hook.pending = True; hook.pending_reasons.add('continuation_restart')
                    return {'status': 'reobserve'}
                return {'status': 'waiting', 'stop_reason': 'continuation_hold'}
            return {'status': 'failed', 'stop_reason': 'continuation_gate_failed'}
        career_checkpoint=(kind=='career' and self.store.nodes[
            self.store.edges[(node['id'],'completed')]]['kind']=='condition')
        if kind=='continue_current' and getattr(self.ctx,'cut_farming_hook',None) is None:
            self.store.choose('next',evidence={'same_attempt':self.ctx.run_id,'new_attempt_created':False})
            self.sync_career_flow()
            return {'status':'reobserve'}
        if kind == 'continue_current' or (kind == 'career' and not career_checkpoint
                and getattr(self.ctx, 'cut_farming_hook', None) is not None):
            return {'status': 'handoff', 'stop_reason': 'continuation_handoff'}
        if kind=='cut':
            self.sync_career_flow()
            return ({'status':'reobserve'} if changed_node else {'status':'not_due'}) if getattr(self.ctx,'cut_farming_hook',None) is not None else {
                'status':'waiting','stop_reason':'continuation_hold'}
        if kind in {'hold', 'finish'}:
            if not (self.state.get('status') == 'waiting' and self.state.get('waiting_on')):
                self.state['status'] = 'preserved' if self.state.get('cut_found') else 'waiting'
            self.store.save()
            return {'status': 'waiting', 'stop_reason': 'continuation_hold'}
        return {'status': 'not_due'}


def continue_after_cut_report(session, root, report, control, *, career_runner=None):
    if not (session.root / DOCUMENT).is_file():
        return None
    store = WorkflowState(session.root, endpoint=f'127.0.0.1:{session.config.device_port}')
    store.record_segment(report)
    screening_transfer = store.node['kind'] == 'cut' and screening_node(store) is not None
    if store.node['kind'] not in {'continue_current', 'career'} and not screening_transfer:
        if session.data.get('status') == 'found':
            store.data['status'] = 'preserved'; store.save()
        return None
    if report.get('stop_reason') not in {'cut_found', 'continuation_handoff'}:
        return None
    if control.stopping:
        return None
    if screening_transfer:
        proof = store.data.get('last_cut') or {}
        if (proof.get('state') != 'present' or proof.get('returned') is not True
                or proof.get('run_id') != Path(root).name):
            return None
    transfer_key = [Path(root).name, report.get('output'), report.get('stop_reason')]
    if screening_transfer and store.data.get('screening_transfer', {}).get('key') == transfer_key:
        return None
    if report.get('stop_reason') == 'continuation_handoff' and session.data['status'] != 'found':
        session.stop('handoff', reason='continuation_handoff', run_id=Path(root).name)
    from core.career_panel_presets import save_cut_handoff, RUN_MARKER
    from scripts.larc_umaa_host import PacerHost
    root = Path(root)
    if not (root / RUN_MARKER).is_file():
        origin = {'batch_root': str(session.root), 'attempt_id': root.name,
                  'status_at_handoff': session.data['status']}
        options = PacerHost.settings(None, root)
        save_cut_handoff(root, f'127.0.0.1:{session.config.device_port}', origin,
            session.config, session.data['attempts'][-1], options, root=session.root.parents[1],
            workflow_internal=screening_transfer)
    if store.node['kind'] == 'continue_current':
        store.choose('next', evidence={'same_attempt': root.name, 'new_attempt_created': False})
    store.bind(root)
    if screening_transfer:
        store.data['screening_transfer'] = {'key': transfer_key, 'run_id': root.name, 'status': 'started'}
        store.save()
    control.ss_planning_horizon=(read(root/'goal.json').get('larc_policy') or {}).get(
        'ss_planning_horizon','full_career')
    from scripts.larc_control_window import build_argv
    from scripts import larc_autonomous_run as career
    options = PacerHost.settings(None, root)
    args = career.parse_args(build_argv(root, options.get('max_actions', 72), device_port=session.config.device_port)
        + ['--ordinary-career-opening', '--same-account-reconnect'])
    args._keep_run_control = True
    args._player_live_takeover = True
    final, code = (career_runner or career.run)(args)
    store = load_for_run(root)
    store.record_segment(final)
    if final.get('career_complete') is True or final.get('status') == 'completed':
        if not screening_terminal(store, final):
            if store.node['kind'] == 'career':
                store.choose('completed', evidence={'normal_settlement_complete': True})
            store.data['status'] = 'completed'
    elif store.data.get('status') not in {'waiting', 'preserved'}:
        store.data['status'] = 'stopped'
    if screening_transfer:
        store.data['screening_transfer']['status'] = 'finished'
    store.data['last_career_result'] = {'reason': final.get('stop_reason'), 'code': code}
    store.save()
    if final.get('stop_reason') == 'continuation_gate_failed' and checkpoint_restart_permission(store, root) is not None:
        return {'status': 'workflow_restart_pending', 'resume_cut_restart': True, 'career_report': final,
                'same_attempt': root.name, 'automatic_career_started': True}
    completed = store.data['status'] == 'completed'
    return {'status': ('found' if store.data.get('cut_found') else 'completed') if completed else 'workflow_stopped',
            'normal_stop': completed or final.get('career_complete') is True or final.get('status') == 'completed',
            'workflow': deepcopy(store.data), 'career_report': final,
            'same_attempt': root.name, 'automatic_career_started': True}


def run_career(args):
    from scripts.career_batch_runtime import run_careers
    return run_careers(args, _run_single_career)


def _run_single_career(args):
    """Ordinary owner callback; graph completion follows real career completion."""
    from scripts.larc_autonomous_run import run
    previous=load_for_run(args.run_root)
    if previous is not None and previous.data.get('status')=='completed':
        return {'status':'completed','career_complete':True,'stop_reason':'workflow_already_completed',
                'run_session_id':Path(args.run_root).name,'consumed_turns_this_run':0},0
    if previous is not None and any(p.get('on_fail') == 'not_matched_branch'
            for n in previous.nodes.values() if n['kind'] == 'cut' and (n.get('screening') or {}).get('enabled') is True
            for p in n['screening']['checkpoints']):
        args._keep_run_control = True  # The panel factory still finishes it.
    if previous is not None and checkpoint_restart_permission(previous, args.run_root) is not None:
        restored = resume_checkpoint_restart(args, {'stop_reason': 'continuation_gate_failed',
            'run_session_id': Path(args.run_root).name})
        if restored is not None:
            return restored
    report, code = run(args)
    store = load_for_run(args.run_root)
    if store is not None:
        batching = (store.document.get('career_batch') or {}).get('max_careers', 1) > 1
        if batching and report.get('run_session_id') != Path(args.run_root).name:
            report = {**report, 'status': 'stopped', 'career_complete': False,
                'stop_reason': 'career_batch_result_run_mismatch'}
            store.data['status'] = 'stopped'
            store.save()
            return report, 2
        store.record_segment(report)
        if (report.get('career_complete') is True or report.get('status') == 'completed') and (not batching or code == 0):
            if not screening_terminal(store, report):
                if store.node['kind'] == 'career':
                    store.choose('completed', evidence={'normal_settlement_complete': True})
                from scripts.career_batch_runtime import record_completion
                record_completion(store, report, code)
        elif store.data.get('status') not in {'waiting', 'preserved'}:
            store.data['status'] = 'stopped'
        store.save()
        if report.get('stop_reason') == 'continuation_gate_failed' and checkpoint_restart_permission(store, args.run_root) is not None:
            restored = resume_checkpoint_restart(args, report)
            if restored is not None:
                return restored
    return report, code


def resume_checkpoint_restart(args, report):
    """Cold ordinary owner hands the same immutable batch back to its executor."""
    store = load_for_run(args.run_root)
    if checkpoint_restart_permission(store, args.run_root) is None:
        return None
    from core.auto_trainer.runtime_control import current
    control = getattr(args, '_run_control', None) or current()
    if control is None or control.stopping:
        return None
    from core.auto_trainer.cut_farming import CutSession, CutFarmingConfig
    from scripts.cut_farming import CutFarmingController
    data = read(store.root / 'cut_farming.json')
    if not data or Path(args.run_root).resolve().parent != store.root / 'attempts':
        return None
    session = CutSession(store.root, CutFarmingConfig.from_dict(data['config']))
    result = CutFarmingController(session, control=control).run(execute=True, resume=True,
        checkpoint_restart_report=report)
    result = {**result, 'stop_reason': 'workflow_checkpoint_restart_' + result.get('status', 'stopped'),
              'workflow_batch_root': str(store.root), 'run_session_id': Path(args.run_root).name}
    code = 0 if result.get('status') in {'found', 'completed', 'limit_reached'} or result.get('normal_stop') else 2
    return result, code


def segment_disposition(session, root, report):
    """Restart requires fresh absence or an explicit immutable checkpoint grant."""
    if not (session.root / DOCUMENT).is_file():
        return None
    store = load_for_run(root)
    store.record_segment(report)
    if screening_terminal(store, report):
        return 'hold'
    reasons = {'cut_cutoff_absent', 'cut_mei_unlock_failed', 'cut_character_event_absent',
               'cut_training_failed', 'cut_supporter_deadline_missed', 'continuation_gate_failed'}
    if report.get('stop_reason') not in reasons:
        return None
    if checkpoint_restart_permission(store, root) is not None:
        return 'restart'
    if checkpoint_restart_record(store, root) is not None:
        return 'hold'
    cut = store.data.get('last_cut') or {}
    progress = read(Path(root) / 'progress.json')
    if (store.data.get('cut_found') or cut.get('state') != 'absent' or cut.get('returned') is not True
            or cut.get('run_id') != Path(root).name or cut.get('turn') != progress.get('next_turn')):
        store.data.update(status='waiting', waiting_on=store.node['id'])
        store.save()
        return 'hold'
    if store.node['kind'] == 'cut':
        store.choose('not_matched', evidence={'cut_state': 'absent', 'reason': report['stop_reason']})
    if store.node['kind'] == 'restart':
        return 'restart'
    return 'hold' if store.node['kind'] == 'hold' else 'reobserve'


def finish_cut_state(session):
    if not (session.root/DOCUMENT).is_file():return None
    store=WorkflowState(session.root,endpoint=f'127.0.0.1:{session.config.device_port}')
    status=session.data['status']
    if status=='limit_reached':store.data['status']='limit_reached'
    elif status=='found' and store.data.get('status')!='completed':store.data['status']='preserved'
    elif store.data.get('status') not in {'waiting','preserved','completed'}:store.data['status']='stopped'
    store.data['batch_stop']=deepcopy(session.data.get('stop'))
    store.save()
    return deepcopy(store.data)
