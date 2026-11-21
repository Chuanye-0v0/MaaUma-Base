"""Ordinary career repetition under one existing panel control and device lease.

Each career remains an independent run. Only a confirmed normal completion
advances this batch; resource stops and manual handoffs keep the current run.
The existing ordinary opener uses zero TP items and zero carrots.
"""
from copy import deepcopy
from pathlib import Path

from core.career_batch import normalize


def _read(path):
    from scripts.career_workflow_runtime import read
    return read(path)


def owns_child(state_root, run_root, endpoint):
    state_root, run_root = Path(state_root).resolve(), Path(run_root).resolve()
    data = _read(state_root / 'session/career_workflow_state.json')
    batch = data.get('career_batch') or {}
    return (state_root.parent == run_root.parent and state_root.name.startswith('larc_panel_')
            and run_root.name.startswith('larc_panel_') and data.get('endpoint') == endpoint
            and batch.get('current_run_id') == run_root.name
            and any(row.get('run_id') == run_root.name and row.get('run_root') == str(run_root)
                    for row in batch.get('runs', []) if isinstance(row, dict)))


def ensure_batch(store, first_root):
    maximum = normalize(store.document.get('career_batch'))['max_careers']
    if maximum == 1:
        return None
    first_root = Path(first_root).resolve()
    batch = store.data.get('career_batch')
    if batch is None:
        batch = {'max_careers': maximum, 'started': 1, 'completed': 0,
                 'current_run_id': first_root.name, 'runs': [{'run_id': first_root.name,
                    'run_root': str(first_root), 'status': 'created'}],
                 'tp_items_used': 0, 'carrots_used': 0,
                 'refill_policy': 'ordinary_opening_no_automatic_refill'}
        store.data['career_batch'] = batch
        store.save()
    rows = batch.get('runs', [])
    roots = [Path(row.get('run_root', '')).resolve() for row in rows if isinstance(row, dict)]
    if (batch.get('max_careers') != maximum or not rows or len(roots) != len(rows)
            or len(rows) > maximum or len(set(roots)) != len(rows)
            or batch.get('started') != len(rows)
            or batch.get('completed') != sum(row.get('status') == 'completed' for row in rows)
            or batch.get('current_run_id') != rows[-1].get('run_id')
            or any(root.parent != store.root.parent or root.name != row.get('run_id')
                   or not root.name.startswith('larc_panel_') for root, row in zip(roots, rows))):
        raise ValueError('连续育成记录与本轮配置不一致，请保留原记录。')
    return batch


def _store_for(run_root):
    from scripts.career_workflow_runtime import CONTEXT, WorkflowState
    root = Path(run_root).resolve()
    binding = _read(root / CONTEXT)
    state_root = Path(binding.get('state_root', root)).resolve()
    if state_root != root and not owns_child(state_root, root, binding.get('device')):
        return None
    if not (state_root / 'session/career_workflow.json').is_file():
        return None
    return WorkflowState(state_root, endpoint=binding.get('device'))


def current_run(run_root, endpoint):
    """Resolve this same batch's active career for the normal panel snapshot."""
    root = Path(run_root).resolve()
    store = _store_for(root)
    if store is None or store.data.get('endpoint') != endpoint:
        return root
    batch = ensure_batch(store, store.root)
    return Path(batch['runs'][-1]['run_root']) if batch is not None else root


def _reset_graph(store):
    store.data.update(active_node=store.document['run_graph']['entry'], status='running')
    store.advance_controls()
    store.save()


def _create_next_run(store):
    """Create only local records here; the ordinary opener owns game setup."""
    from core.career_panel_presets import save_new_run, settings_from_storage
    from scripts.larc_panel_setup import create_panel_run
    from scripts.larc_ordinary_setup import parse_ordinary_setup, save_ordinary_setup
    batch = ensure_batch(store, store.root)
    options = settings_from_storage(_read(store.root / 'session/umaa_panel_options.json'))
    endpoint = store.data['endpoint']
    setup = parse_ordinary_setup(options.get('setup'))
    root = create_panel_run(device_port=int(endpoint.rsplit(':', 1)[1]),
        runs_dir=store.root.parent, targets=options['targets'],
        strategy=options.get('strategy', 'baseline'), failure_limit=options.get('failure_limit', 25),
        mei_breaks=options.get('mei_breaks'), with_ramonu=options.get('with_ramonu') is True,
        early_skill_allowlist=options.get('early_skill_allowlist', []), arc_policy=options.get('arc_policy'),
        larc_policy=options.get('larc_policy'), task_mode='career',
        mid_skill_learning=options.get('mid_skill_learning'), support_profile=options.get('support_profile'),
        race_retry=options.get('race_retry'), running_style=options.get('running_style'),
        race_running_styles=options.get('race_running_styles'), custom_races=options.get('custom_races'))
    save_ordinary_setup(root, _read(root / 'progress.json'), setup)
    connection = _read(store.root / 'session/ai_decision.json').get('connection', {})
    save_new_run(root, endpoint, options, connection)
    batch['runs'].append({'run_id': root.name, 'run_root': str(root), 'status': 'created'})
    batch.update(started=len(batch['runs']), current_run_id=root.name)
    store.bind(root)
    _reset_graph(store)
    return root


def prepare_resume(store):
    """Recover a completed-career / next-record boundary without replaying it."""
    batch = ensure_batch(store, store.root)
    if batch is None or batch['completed'] == batch['max_careers']:
        return
    root = Path(batch['runs'][-1]['run_root'])
    receipt = _completed_receipt(root, store.data['endpoint'])
    if batch['runs'][-1]['status'] != 'completed' and receipt is not None:
        record_completion(store, {'stop_reason': 'normal_settlement_complete',
            'output': receipt, 'run_session_id': root.name})
        if batch['completed'] == batch['max_careers']:
            return
    if batch['runs'][-1]['status'] == 'completed':
        _create_next_run(store)
    elif batch['runs'][-1]['status'] == 'created':
        _reset_graph(store)


def _completed_receipt(root, endpoint):
    """Reuse a real settled/home receipt when a crash preceded batch counting."""
    state = _read(root / 'session/larc_terminal_settlement.json')
    progress = _read(root / 'progress.json')
    if state.get('status') != 'complete':
        return None
    path = Path(state.get('report_path', '')).resolve()
    if (not path.is_relative_to(root) or state.get('run_session_id') != root.name
            or state.get('device') != endpoint or state.get('turn_index') != progress.get('next_turn')):
        return None
    report = _read(path)
    if (report.get('ok') is True and report.get('final_page') == 'home'
            and report.get('run_session_id') == root.name and report.get('endpoint') == endpoint
            and report.get('turn_index') == progress.get('next_turn')):
        return str(path)
    return None


def record_completion(store, report, code=0):
    batch = ensure_batch(store, store.root)
    if batch is None:
        store.data['status'] = 'completed'
        return
    if code != 0:
        store.data['status'] = 'stopped'
        store.save()
        return
    row = batch['runs'][-1]
    if report.get('run_session_id') != row['run_id']:
        store.data['status'] = 'stopped'
        store.save()
        return
    if row['status'] != 'completed':
        row.update(status='completed', completion={'reason': report.get('stop_reason'),
            'output': report.get('output'), 'run_id': row['run_id']})
        batch['completed'] += 1
    store.data['status'] = 'completed' if batch['completed'] == batch['max_careers'] else 'running'
    store.save()


def _adopt_run(args, root, store):
    from scripts.larc_autonomous_run import parse_args
    from scripts.larc_control_window import build_argv
    options = _read(root / 'session/umaa_panel_options.json')
    argv = build_argv(root, options.get('max_actions', 72), device_port=args.adb_port,
                      retain_training=options.get('retain_training') is True)
    argv += ['--ordinary-career-opening', '--same-account-reconnect']
    if '--d1-startup-only' in argv:
        argv.remove('--d1-startup-only')
    if options.get('terminal_skills', True) is not True:
        argv.append('--d1-startup-only')
    args.__dict__.update(vars(parse_args(argv)))
    args._keep_run_control = True
    args._player_live_takeover = False


def run_careers(args, single_runner):
    store = _store_for(args.run_root)
    if store is None or normalize(store.document.get('career_batch'))['max_careers'] == 1:
        return single_runner(args)
    batch = ensure_batch(store, store.root)
    args._keep_run_control = True
    while batch['completed'] < batch['max_careers']:
        control = getattr(args, '_run_control', None)
        if control is not None:
            control.note = f"本轮育成 {batch['completed']}/{batch['max_careers']} 已完成"
            control.checkpoint('career_batch_next' if batch['runs'][-1]['status'] == 'completed' else 'career_batch_current')
        prepare_resume(store)
        batch = ensure_batch(store, store.root)
        if batch['completed'] == batch['max_careers']:
            break
        root = Path(batch['runs'][-1]['run_root'])
        if Path(args.run_root).resolve() != root:
            _adopt_run(args, root, store)
        batch['runs'][-1]['status'] = 'running'
        store.data['status'] = 'running'
        store.save()
        report, code = single_runner(args)
        store = _store_for(root)
        batch = ensure_batch(store, store.root)
        if code != 0 or not (report.get('career_complete') is True or report.get('status') == 'completed'):
            if batch['runs'][-1]['status'] != 'completed':
                batch['runs'][-1]['status'] = 'stopped'
                store.save()
            return {**report, 'career_batch': deepcopy(batch)}, code
        record_completion(store, report)
        batch = ensure_batch(store, store.root)
    return {**(locals().get('report') or {}), 'status': 'completed', 'career_complete': True,
            'stop_reason': 'career_batch_completed', 'career_batch': deepcopy(batch)}, 0
