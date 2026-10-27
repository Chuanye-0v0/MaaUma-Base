"""One original executor bridge; the account/day ledger survives attempts."""
import json
from pathlib import Path

from core.strongest_team.journal import Journal, atomic_write, batch_scope
from core.strongest_team.options import normalize_options


def validate_options(options):
    return normalize_options(options)


def active_source(root, options, endpoint):
    root=Path(root).resolve();marker=root/'active.json'
    if not marker.exists():return None
    value=json.loads(marker.read_text(encoding='utf8'))
    source=Path(value.get('output','')).resolve()
    if (value.get('task')!='strongest_team' or value.get('endpoint')!=endpoint
            or value.get('options')!=normalize_options(options)
            or source.parent.parent!=root or source.name!='strongest_team' or not source.is_dir()):
        raise RuntimeError('strongest_team_active_source_mismatch')
    return source


def _execute_operation(options, *, endpoint, adb_path, output, emit, stop, resume_from=None,
            device=None, ledger_path=None, scope=None):
    """device/explicit scope are test seams, never a second live executor."""
    from core.strongest_team.live import StrongestTeamDevice
    from core.strongest_team.runner import Runner
    from core.screenshot_retention import ScreenshotSession
    options=normalize_options(options)
    output=Path(output).resolve();state_path=None;owner=None
    if device is not None and ledger_path is not None and endpoint!='127.0.0.1:16416':
        raise ValueError('strongest_team_device_not_allowed')
    if (ledger_path is not None or scope is not None) and device is None:
        raise ValueError('strongest_team_explicit_scope_test_only')
    try:
        from desktop.mfa_strongest_team_public import execution_scope
        if resume_from:
            state_path=Path(resume_from).resolve()
            if state_path.is_dir():state_path=state_path/'strongest-team-state.json'
            saved=json.loads(state_path.read_text(encoding='utf8'))
            if saved.get('schema')!='uma.strongest_team.operation.v1' or saved.get('endpoint')!=endpoint:
                raise RuntimeError('strongest_team_resume_scope_invalid')
            if device is None:
                expected_path,expected_scope=execution_scope(output,endpoint,day=saved['scope']['day'])
                if (expected_scope!=saved['scope']
                        or expected_path.resolve()!=Path(saved['journal_path']).resolve()):
                    raise RuntimeError('strongest_team_original_account_scope_mismatch')
            ledger_path=Path(saved['journal_path']);scope=saved['scope']
            directory=state_path.parent
        else:
            directory=output
            if ledger_path is None:ledger_path,scope=execution_scope(output,endpoint)
        from core.strongest_team.storage import maintain
        if device is None:
            maintain(Path(ledger_path).parent.parent)
        owner=Journal(ledger_path,scope,endpoint,options)
        # One canonical operation directory for this account/day also keeps
        # consumed-capture ownership reachable after a non-pending retry.
        if not resume_from and (owner.data.get('operation_root') or owner.pending or owner.data.get('navigation_pending')):
            original=owner.data.get('operation_root')
            if not original:raise RuntimeError('strongest_team_original_operation_missing')
            directory=Path(original).resolve()
            if device is None:
                expected_path,expected_scope=execution_scope(directory,endpoint,day=scope['day'])
                if expected_scope!=scope or expected_path.resolve()!=Path(ledger_path).resolve():
                    raise RuntimeError('strongest_team_original_operation_scope_mismatch')
        directory.mkdir(parents=True,exist_ok=True)
        state_path=directory/'strongest-team-state.json'
        atomic_write(state_path,dict(schema='uma.strongest_team.operation.v1',endpoint=endpoint,
            journal_path=str(owner.path),scope=scope,operation_root=str(directory)))
        owner.data['operation_root']=str(directory);owner.save()
        with ScreenshotSession(full=False) as screenshots:
            created=device is None
            if created:device=StrongestTeamDevice(endpoint,adb_path,directory,journal=owner,options=options)
            if device.endpoint!=endpoint:raise ValueError('strongest_team_device_scope_mismatch')
            device.output_dir=directory
            if not created:
                from core.screenshot_retention import install_capture_retention
                install_capture_retention(device)
            if created:device.connect()
            try:
                result=Runner(device,owner,emit,stop,options=options,screenshots=screenshots).run()
                result['state_path']=str(state_path)
                if resume_from:result['resumed_from']=str(state_path)
                if result['ok'] and not result['pending']:
                    from core.strongest_team.storage import consume
                    result['consumed_capture_release']=consume(owner,screenshots,result['last_observation'],keep_current=False)
                result['screenshot_retention']=screenshots.finish(result,complete=result['ok'])
                result['capture_policy']=dict(ordinary_max_age_seconds=4*3600,failure_max_age_seconds=12*3600,
                    pending_recovery_protected=True,consumed_work_released=True,full_dump=False)
                return result
            finally:
                if created:device.close()
    except Exception as error:
        prior=getattr(error,'journal_path',None)
        prior_state=None
        prior_scope=None
        if prior:
            try:
                saved=json.loads(Path(prior).read_text(encoding='utf8'))
                prior_scope=saved.get('scope')
                candidate=Path(saved['operation_root'])/'strongest-team-state.json'
                if candidate.is_file():prior_state=str(candidate)
            except (OSError,ValueError,KeyError,TypeError):
                pass
        return dict(kind='strongest_team',ok=False,game_task_complete=False,skip_verified=False,
            status='cancelled' if stop.is_set() else 'failed',reason=str(error),
            pending=bool(prior or resume_from or owner is not None and (owner.pending or owner.data.get('context_verified'))),
            state_path=prior_state or (str(state_path) if state_path else None),
            journal_path=prior or (str(owner.path) if owner is not None else None),scope=prior_scope,
            device_action_count_complete=False)


def execute(options, **kwargs):
    """Settle at most one prior-day owner before any new-day reservation."""
    result=_execute_operation(options,**kwargs)
    if (result.get('reason')!='strongest_team_prior_day_pending' or not result.get('state_path')
            or kwargs.get('resume_from')):
        return result
    continued=dict(kwargs,resume_from=result['state_path'])
    continued.pop('ledger_path',None);continued.pop('scope',None)
    original=_execute_operation(options,**continued)
    if (original.get('pending') is not False or (original.get('last_observation') or {}).get('page')!='home'
            or original.get('reason')!='strongest_team_game_day_changed_checkpoint_yield'):
        return original
    proof=Path(kwargs['output'])/'prior-day-settlement.json'
    atomic_write(proof,original)
    fresh=dict(kwargs);fresh.pop('resume_from',None)
    answer=_execute_operation(options,**fresh)
    answer['prior_day_settlement']=str(proof)
    return answer


def classify(result,endpoint):
    options=normalize_options(result.get('effective_options'))
    final=result.get('last_observation') or {}
    settled=(result.get('pending') is False and result.get('entry_pending') is False
        and final.get('page')=='home' and final.get('endpoint')==endpoint
        and not result.get('account_conflict') and not result.get('account_paused'))
    counts_ok=True
    for kind in ['normal','special']:
        values=[result.get(kind+'_'+key) for key in ['reserved','submitted','completed']]
        counts_ok=counts_ok and all(type(v) is int and v>=0 for v in values) and len(set(values))==1
    normal=result.get('normal_completed',0);special=result.get('special_completed',0)
    counts_ok=counts_ok and normal<=options['max_entries'] and special<=1
    if not options['special_extra']:counts_ok=counts_ok and normal+special<=options['max_entries']
    for kind,budget in [('clock','max_clocks'),('jelly','max_jellies')]:
        a,b=result.get(kind+'_reserved'),result.get(kind+'_confirmed')
        counts_ok=counts_ok and type(a) is int and type(b) is int and 0<=a==b<=options[budget]
    valid=(settled and counts_ok and result.get('ok') is True and result.get('reason')=='strongest_team_home_verified'
        and result.get('completion_basis') in {'daily_entry_cap','available_resources_exhausted','event_unavailable_verified','already_completed_today'})
    done=valid and result.get('status')=='done'
    skipped=valid and result.get('status')=='skipped'
    return dict(ok=bool(done or skipped),game_task_complete=bool(done),skip_verified=bool(skipped))
