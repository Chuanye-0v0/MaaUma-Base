"""Private host protocol for saved documents and existing owner execution."""
from copy import deepcopy
from pathlib import Path
import re

from core.auto_trainer.cut_farming import atomic_json
from core.career_workflow import ace_cycle_node, normalize, schema, ss_sieve_node, templates, validate_start_document
from core.career_workflow_library import LibraryStore, SCHEMA as LIBRARY_SCHEMA
from core.career_workflow_library_v2 import (
    BUILTIN_MODES,
    BuildLibraryStore,
    SCHEMA_V2 as LIBRARY_SCHEMA_V2,
    builtin_templates,
)
from scripts.career_workflow_runtime import DOCUMENT, STATE, WorkflowState, read

ACTIONS = {'workflow_schema', 'validate_workflow', 'validate_workflow_start', 'save_workflow', 'load_workflow', 'start_workflow',
    'workflow_library', 'workflow_library/list', 'list_workflow_library', 'load_workflow_library',
    'save_workflow_library', 'resolve_workflow_configuration',
    'workflow_library_v2', 'load_workflow_library_v2', 'save_workflow_library_v2', 'resolve_workflow_configuration_v2'}
PRESET = 'runs/standalone-panel/career-workflow.json'
CURRENT = 'runs/standalone-panel/career-workflow-current.json'


def _endpoint_pointer(root: Path, endpoint: str | None) -> Path:
    if endpoint:
        safe = re.sub(r'[^0-9A-Za-z._-]+', '_', str(endpoint).strip())
        if safe:
            return Path(root) / f'runs/standalone-panel/career-workflow-current-{safe}.json'
    return Path(root) / CURRENT


def _workflow_resume_status(host, store, root, endpoint, sieve, ace):
    """Pure read-only check mirroring dispatch(..., workflow_root=...) without writing."""
    status = store.data.get('status')
    if status == 'completed':
        return False, '该工作流已完成，不会重复执行。'
    if status == 'preserved':
        return False, '当前育成已保留。'
    try:
        if sieve or ace:
            from scripts.larc_umaa_cut import checked_batch, resume_status
            batch, data, config = checked_batch(root, root=host.root, endpoint=endpoint)
            return resume_status(batch, data, config)
        run_root = Path(store.data.get('attempt_root', root)).resolve()
        from scripts.career_workflow_runtime import checkpoint_restart_permission, checkpoint_restart_record
        restart_permission = checkpoint_restart_permission(store, run_root)
        if (store.node['kind'] == 'restart' and checkpoint_restart_record(store, run_root) is not None
                and restart_permission is None and status != 'manual_career'):
            return False, '原局配置或重开授权已改变，或上次提交尚未确认，请先处理原记录。'
        if restart_permission is not None:
            return True, None
        from core.career_panel_presets import RUN_MARKER, record_for_path
        if (run_root != root and (run_root / RUN_MARKER).is_file()) or (
                run_root == root and not (root / 'cut_farming.json').is_file()):
            rec = record_for_path(run_root, root=host.root, endpoint=endpoint, include_settings=False)
            if rec.get('can_resume'):
                return True, None
            batch = store.data.get('career_batch')
            if isinstance(batch, dict):
                max_careers = int(batch.get('max_careers') or 1)
                completed = int(batch.get('completed') or 0)
                manual_terminal = bool((rec.get('manual_handoff') or {}).get('terminal'))
                if max_careers > 1 and not manual_terminal:
                    rows = batch.get('runs') or []
                    last_done = bool(rows and rows[-1].get('status') == 'completed')
                    effective_completed = completed if last_done else completed + 1
                    if effective_completed < max_careers:
                        return True, None
                    return False, '该工作流已完成，不会重复执行。'
            return False, rec.get('resume_reason') or '当前记录不能续跑。'
        from scripts.larc_umaa_cut import checked_batch, resume_status
        batch, data, config = checked_batch(root, root=host.root, endpoint=endpoint)
        data_copy = deepcopy(data)
        if (data_copy.get('status') == 'handoff' and (data_copy.get('stop') or {}).get('reason') == 'continuation_hold'
                and not data_copy.get('pending_action') and store.data.get('waiting_on')):
            data_copy['status'] = 'stopped'
        return resume_status(batch, data_copy, config)
    except (ValueError, OSError, KeyError, TypeError) as error:
        return False, str(error)


def snapshot(host):
    endpoint = getattr(host, 'endpoint', None)
    pointer_path = _endpoint_pointer(host.root, endpoint)
    if pointer_path.is_file():
        pointer = read(pointer_path)
    else:
        pointer = read(host.root / CURRENT)
        if endpoint and pointer.get('endpoint') and pointer.get('endpoint') != endpoint:
            return None
    if not pointer.get('root'):
        return None
    root = Path(pointer['root']).resolve()
    if root == host.root / 'runs' or not root.is_relative_to((host.root / 'runs').resolve()):
        return None
    if not (root / DOCUMENT).is_file():
        return None
    effective_endpoint = endpoint or pointer.get('endpoint')
    store = WorkflowState(root, endpoint=effective_endpoint)
    sieve = ss_sieve_node(store.document) is not None
    ace = ace_cycle_node(store.document) is not None
    can_resume, resume_reason = _workflow_resume_status(host, store, root, effective_endpoint, sieve, ace)
    result = {'root': str(root), 'document': store.document, 'state': store.data,
              'can_resume': can_resume, 'resume_reason': resume_reason,
              'execution_mode': 'career' if not (sieve or ace) and (not any(node['kind'] in {'cut', 'restart'} for node in store.nodes.values())
                                  or store.node['kind'] == 'career'
                                  or store.data.get('status') == 'manual_career') else 'cut'}
    if sieve:
        result['ss_sieve_counts'] = store.data.get('ss_sieve_counts', {'completed': 0, 'screened_out': 0, 'failed': 0})
    if ace:
        result['ace_cycle_counts'] = store.data.get('ace_cycle_counts', {'graduated': 0, 'unqualified': 0, 'screened_out': 0, 'failed': 0, 'completed': 0})
    return result


def _start_sieve(host, payload, document):
    from scripts.larc_umaa_host import PacerHost

    original = host.cut_service_factory
    def create(directory, profiles, **kwargs):
        from scripts.larc_umaa_cut import create_cut_control_service
        batch = next(iter(profiles.values()))['batch_root']
        atomic_json(_endpoint_pointer(host.root, payload.get('endpoint')), {'root': batch, 'endpoint': payload['endpoint']})
        return (original or create_cut_control_service)(directory, profiles, **kwargs)
    try:
        host.cut_service_factory = create
        result = PacerHost.start(host, payload, sieve_document=document)
    finally:
        host.cut_service_factory = original
    result['workflow'] = snapshot(host)
    return result


def dispatch(host, request, *, endpoint):
    action = request['action']
    if action == 'workflow_schema':
        return {
            **schema(),
            'library_schema_version': LIBRARY_SCHEMA,
            'library_v2_schema_version': LIBRARY_SCHEMA_V2,
            'v2_supported': True,
            'builtin_modes': [{'id': mid, 'name': mname} for mid, mname in BUILTIN_MODES],
            'builtin_templates': builtin_templates(),
        }
    if action in {'workflow_library_v2', 'load_workflow_library_v2', 'save_workflow_library_v2', 'resolve_workflow_configuration_v2'}:
        store_v2 = BuildLibraryStore(host.root)
        if action == 'save_workflow_library_v2':
            store_v2.save(request.get('library'))
        return store_v2.payload(
            request.get('mode_id'),
            build_id=request.get('build_id'),
            profile_id=request.get('profile_id'),
            initial_policies=request.get('initial_policies'),
        )
    if action in {'workflow_library', 'workflow_library/list', 'list_workflow_library', 'load_workflow_library',
                  'save_workflow_library', 'resolve_workflow_configuration'}:
        store = LibraryStore(host.root)
        if action == 'save_workflow_library':
            store.save(request.get('library'))
        return store.payload(request.get('mode_id'), request.get('profile_id'),
            initial_policies=request.get('initial_policies'))
    if action == 'load_workflow':
        document = read(host.root / PRESET) or templates()[0]
        return {'document': normalize(document), 'workflow': snapshot(host)}
    if action in {'validate_workflow', 'save_workflow'}:
        document = normalize(request.get('document'))
        if action == 'save_workflow':
            atomic_json(host.root / PRESET, document)
        return {'is_valid': True, 'runnable': True, 'document': document}
    if action == 'validate_workflow_start':
        if request.get('document') is not None:
            doc = request.get('document')
        elif request.get('build_id') is not None:
            doc = BuildLibraryStore(host.root).resolve(
                request.get('mode_id'), build_id=request.get('build_id'), profile_id=request.get('profile_id')
            )
        else:
            doc = LibraryStore(host.root).resolve(request.get('mode_id'), request.get('profile_id'))
        return validate_start_document(doc)
    identifier = request.get('request_id')
    if not isinstance(identifier, str) or not 8 <= len(identifier) <= 128:
        raise ValueError('request_id_required')
    from scripts.larc_umaa_host import ACTIVE, PacerHost
    if host.service and host.service.snapshot()['state'] in ACTIVE:
        if any(request.get(key) is not None for key in ('document', 'mode_id', 'build_id', 'profile_id')):
            raise ValueError('旧任务尚未结束或已切换到其他任务，未开始新工作流；当前配置保留。')
        # Resume is bound to the current task, never the newly edited document.
        result = PacerHost.start(host, {'action': 'start', 'mode': host.mode,
            'request_id': identifier, 'endpoint': endpoint})
        result['workflow'] = snapshot(host)
        return result
    resume_root = request.get('workflow_root')
    if resume_root:
        root = Path(resume_root).resolve(strict=True)
        if root == host.root / 'runs' or not root.is_relative_to((host.root / 'runs').resolve()):
            raise ValueError('工作流记录必须位于本程序runs目录。')
        store = WorkflowState(root, endpoint=endpoint)
        document = store.document
        from scripts.career_batch_runtime import prepare_resume
        prepare_resume(store)
        if store.data.get('status') == 'completed':
            raise ValueError('该工作流已完成，不会重复执行。')
        if ss_sieve_node(document) is not None or ace_cycle_node(document) is not None:
            return _start_sieve(host, {'action': 'start', 'mode': 'cut', 'endpoint': endpoint,
                'request_id': identifier, 'run_root': str(root)}, document)
        run_root = Path(store.data.get('attempt_root', root)).resolve()
        from scripts.career_workflow_runtime import checkpoint_restart_permission, checkpoint_restart_record
        restart_permission = checkpoint_restart_permission(store,run_root)
        if (store.node['kind'] == 'restart' and checkpoint_restart_record(store,run_root) is not None
                and restart_permission is None and store.data.get('status') != 'manual_career'):
            raise ValueError('原局配置或重开授权已改变，或上次提交尚未确认，请先处理原记录。')
        from core.career_panel_presets import RUN_MARKER, save_cut_handoff
        if store.node['kind'] in {'continue_current', 'career'} and not (run_root / RUN_MARKER).is_file() and run_root != root:
            from scripts.larc_umaa_cut import checked_batch
            batch, data, config = checked_batch(root, root=host.root, endpoint=endpoint)
            origin = {'batch_root': str(batch), 'attempt_id': run_root.name, 'status_at_handoff': data['status']}
            save_cut_handoff(run_root, endpoint, origin, config, data['attempts'][-1], host.settings(run_root), root=host.root)
            if store.node['kind'] == 'continue_current':
                store.choose('next', evidence={'same_attempt': run_root.name, 'resumed_transfer': True})
            store.bind(run_root)
        if restart_permission is not None:
            from core.auto_trainer.cut_farming import CutSession, CutFarmingConfig
            from scripts.career_workflow_runtime import prepare_checkpoint_restart
            saved = read(root/'cut_farming.json')
            session = CutSession(root,CutFarmingConfig.from_dict(saved['config']))
            if prepare_checkpoint_restart(session,run_root,{'stop_reason':'continuation_gate_failed'}) is not True:
                raise ValueError('原局重开授权与待处理步骤不一致。')
            mode, original = 'cut', root
        elif run_root != root and (run_root / RUN_MARKER).is_file() or run_root == root and not (root / 'cut_farming.json').is_file():
            mode, original = 'career', run_root
        else:
            mode, original = 'cut', root
            batch = read(root / 'cut_farming.json')
            if (batch.get('status') == 'handoff' and batch.get('stop', {}).get('reason') == 'continuation_hold'
                    and not batch.get('pending_action') and store.data.get('waiting_on')):
                batch['status'] = 'stopped'
                atomic_json(root / 'cut_farming.json', batch)
        options = host.settings(original) if mode == 'career' else {}
        payload = {'action': 'start', 'mode': mode, 'endpoint': endpoint, 'run_root': str(original),
                   'request_id': identifier, 'options': options}
    else:
        if request.get('document') is not None:
            if any(request.get(key) is not None for key in ('mode_id', 'build_id', 'profile_id')):
                raise ValueError('请选择配置库标识或显式执行文档，不要同时传入。')
            document = normalize(request['document'])
        elif request.get('build_id') is not None:
            document = BuildLibraryStore(host.root).resolve(
                request.get('mode_id'), build_id=request.get('build_id'), profile_id=request.get('profile_id')
            )
        else:
            document = LibraryStore(host.root).resolve(request.get('mode_id'), request.get('profile_id'))
        start_check = validate_start_document(document)
        if not start_check['ok']:
            raise ValueError('；'.join(start_check['errors']))
        if ss_sieve_node(document) is not None or ace_cycle_node(document) is not None:
            return _start_sieve(host, {'action': 'start', 'mode': 'cut', 'endpoint': endpoint,
                'request_id': identifier}, document)
        nodes = document['run_graph']['nodes']
        mode = 'cut' if any(node['kind'] in {'cut', 'restart'} for node in nodes) else 'career'
        by_id={node['id']:node for node in nodes}
        initial=by_id[document['run_graph']['entry']]
        while initial['kind']=='entry':
            target=next(edge['to'] for edge in document['run_graph']['edges']
                if edge['from']==initial['id'] and edge['outcome']=='next')
            initial=by_id[target]
        policy = document['policies'][initial.get('policy','default')]
        options = deepcopy(policy.get('cut_options' if mode == 'cut' else 'career_options', {}))
        if mode == 'career' and isinstance(options.get('continuation'), dict):
            from core.auto_trainer.larc_continuation import enforce_pause_only
            options['continuation'] = enforce_pause_only(options['continuation'])
        if mode == 'cut' and (options.get('cut') or {}).get('discard_found_for_validation'):
            raise ValueError('工作流找到切后不支持耐久测试弃养。')
        if mode=='cut':
            from core.auto_trainer.larc_continuation import defaults
            continuation=defaults();continuation['enabled']=True
            continuation['junior']['mode']=continuation['inheritance']['mode']='observe'
            options['cut']={**(options.get('cut') or {}),'continuation':continuation}
        payload = {'action': 'start', 'mode': mode, 'endpoint': endpoint,
                   'request_id': identifier, 'options': options}
        if mode == 'cut':
            payload.update(new_cycle=True, take_over_current=True)
    original_career, original_cut = host.service_factory, host.cut_service_factory
    def wrap(factory, cut):
        def create(directory, profiles, **kwargs):
            settings = next(iter(profiles.values()))
            if cut:
                root = Path(settings['batch_root'])
                if not (root / DOCUMENT).is_file():
                    atomic_json(root / DOCUMENT, document)
                    store = WorkflowState(root, document, endpoint=endpoint); store.save()
            else:
                argv = settings if isinstance(settings, list) else settings.get('argv', [])
                root = Path(argv[argv.index('--run-root') + 1])
                if not (root / DOCUMENT).is_file():
                    atomic_json(root / DOCUMENT, document)
                    store = WorkflowState(root, document, endpoint=endpoint); store.bind(root)
                    from scripts.career_batch_runtime import ensure_batch
                    ensure_batch(store, root)
            state_root = Path(read(root / 'session/career_workflow_context.json').get('state_root', root))
            atomic_json(_endpoint_pointer(host.root, endpoint), {'root': str(state_root), 'endpoint': endpoint})
            if not cut:
                from scripts.career_workflow_runtime import run_career
                kwargs['run'] = run_career
            return factory(directory, profiles, **kwargs)
        return create
    try:
        host.service_factory = wrap(original_career, False)
        host.cut_service_factory = wrap(original_cut, True)
        result = PacerHost.start(host, payload)
    finally:
        host.service_factory, host.cut_service_factory = original_career, original_cut
    result['workflow'] = snapshot(host)
    return result
