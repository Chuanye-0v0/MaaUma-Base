"""Read-only outer control context, separated from the last career segment.

No resume, capture, resource check, or game input. A normal no-cut checkpoint
never clears a real pause and never becomes the current batch failure.
"""
from pathlib import Path
import json
import re
from core.auto_trainer.control_metadata_io import safe_metadata_failure

SCHEMA='maauma.pacer_stop_context.v1'
NORMAL_CUT={'cut_character_event_absent','cut_cutoff_absent','cut_found_for_validation',
    'cut_benchmark_cutoff','cut_mei_unlock_failed','mei_unlock_failed','cut_training_failed',
    'cut_supporter_deadline_missed','continuation_gate_failed'}
ACTIVE={'starting','running','pause_requested','paused','resume_requested','stop_requested'}
PAUSED={'pause_requested','paused','resume_requested'}
_ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z')

def _obj(value):return value if isinstance(value,dict) else {}
def _text(value,limit=500):return value[:limit] if isinstance(value,str) else None
def _id(value):return value if isinstance(value,str) and _ID.fullmatch(value) else None

def _inside(value,root):
    if not isinstance(value,(str,Path)):return None
    try:
        path=Path(value).resolve()
        return path if path!=root and path.is_relative_to(root) else None
    except (OSError,ValueError):return None

def _metadata(snapshot,root):
    cut=_obj(snapshot.get('cut'));batch={};batch_id=None;attempt_id=None;segment_id=None
    try:
        runs=(Path(root)/'runs').resolve()
        path=_inside(snapshot.get('run_root'),runs)
        if snapshot.get('mode')=='cut' and path is not None:
            data=json.loads((path/'cut_farming.json').read_text(encoding='utf-8-sig'))
            config=_obj(data.get('config'))
            port=config.get('device_port')
            if (data.get('schema_version')=='uma.cut_farming_session.v1'
                    and type(port) is int and snapshot.get('endpoint')==f'127.0.0.1:{port}'):
                batch=data;batch_id=_id(path.name)
        raw=cut.get('current_attempt_root') if snapshot.get('mode')=='cut' else snapshot.get('run_root')
        attempt=_inside(raw,runs)
        if attempt is not None:
            # A cut child must belong to the actual displayed batch.
            if snapshot.get('mode')!='cut' or batch_id and attempt.parent==path/'attempts':
                attempt_id=_id(attempt.name)
                summaries=[p for p in attempt.glob('autonomous_*/summary.json')
                    if p.resolve().parent.parent==attempt]
                if summaries:segment_id=_id(max(summaries,key=lambda p:p.stat().st_mtime).parent.name)
    except (OSError,ValueError,TypeError,AttributeError):pass
    return batch,batch_id,attempt_id,segment_id

def _manual_pause(snapshot,runtime,attempt_id):
    """Accept only the displayed career's bound manual-pause evidence."""
    if snapshot.get('mode')!='career' or not attempt_id:return None
    endpoint=snapshot.get('endpoint')
    record=_obj(snapshot.get('record'))
    if (('run_session_id' in record and record['run_session_id']!=attempt_id)
            or ('device' in record and record['device']!=endpoint)):return None
    saved=_obj(snapshot.get('manual_handoff')) or _obj(record.get('manual_handoff'))
    note=runtime.get('note')
    if isinstance(note,dict) and note:
        # Current structured evidence wins. A foreign or different live pause
        # cannot borrow an old saved manual wait from the selected record.
        if (note.get('kind')!='manual_handoff' or runtime.get('state')!='paused'
                or runtime.get('pause_requested') is not True
                or runtime.get('input_in_flight') is not False):return None
        candidates=[{**note,'status':'waiting'}]
    else:
        if record.get('run_session_id')!=attempt_id or record.get('device')!=endpoint:return None
        candidates=[saved]
    reasons={'first_arc':'manual_first_arc_skill_handoff','date':'manual_career_date_handoff'}
    for value in candidates:
        stage=value.get('stage')
        if (value.get('run_session_id')==attempt_id and value.get('device')==endpoint
                and value.get('status')=='waiting' and stage in reasons
                and value.get('reason')==reasons[stage]):
            return {'run_id':attempt_id,'stage':stage,'reason':reasons[stage],
                'pause_id':_id(value.get('pause_id')),'status':'waiting'}
    return None

def _load_runner_error(snapshot, root, batch_id, attempt_id, run_id):
    """Read bounded runner_error diagnostic from run/batch or control state dir."""
    try:
        from core.auto_trainer.runner_error_diagnostics import load_runner_error
    except Exception:
        return None
    runs = (Path(root) / 'runs').resolve()
    candidates = []
    run_root_raw = snapshot.get('run_root')
    selected = _inside(run_root_raw, runs)
    if selected is not None:
        candidates.append((selected / 'runner_error.json', None))
    cut_attempt = _inside(_obj(snapshot.get('cut')).get('current_attempt_root'), runs)
    if cut_attempt is not None:
        candidates.append((cut_attempt / 'runner_error.json', None))
    endpoint = snapshot.get('endpoint')
    if isinstance(endpoint, str) and ':' in endpoint:
        port = endpoint.rsplit(':', 1)[1]
        if port.isdigit():
            candidates.append((runs / 'umaa-panel-control' / port / 'last_runner_error.json', run_id))
    for path, expected_run_id in candidates:
        loaded = load_runner_error(path, expected_run_id=expected_run_id)
        if loaded is not None:
            return loaded
    return None


def annotate(snapshot,root):
    """Add display/report fields without altering old records or task state."""
    control=_obj(snapshot.get('control'));runtime=_obj(control.get('runtime'))
    cut=_obj(snapshot.get('cut'));record=_obj(snapshot.get('record'))
    mode=snapshot.get('mode');state=_text(control.get('state'),64) or 'idle'
    step=_text(runtime.get('step'),160) or _text(control.get('active_boundary'),160)
    note=_text(runtime.get('note'),1000)
    license=_obj(runtime.get('runtime_license'))
    batch,batch_id,attempt_id,segment_id=_metadata(snapshot,root)
    manual=_manual_pause(snapshot,runtime,attempt_id)
    if manual and not step and license.get('status')!='paused':step=manual['reason']
    stop=_obj(batch.get('stop')) or _obj(cut.get('stop'))
    batch_stop=None
    if stop:
        batch_stop={key:stop.get(key) for key in ('reason','message','normal_stop','resource_status')
            if isinstance(stop.get(key),(str,bool,int,float)) or stop.get(key) is None}
        for key in ('reason','message','resource_status'):
            if isinstance(batch_stop.get(key),str):batch_stop[key]=batch_stop[key][:1000]
    raw_pending=_obj(batch.get('pending_action'))
    pending={key:_text(raw_pending.get(key),160) for key in ('state','reason')
        if isinstance(raw_pending.get(key),str)} or None
    batch_status=_text(batch.get('status'),80) or _text(cut.get('status'),80)
    segment_reason=_text(record.get('stop_reason'),200)
    segment_status=_text(record.get('summary_status'),80)
    normal=segment_reason in NORMAL_CUT and segment_status=='cut_farming_checkpoint'
    last={'run_id':attempt_id or _id(record.get('run_session_id')) or _id(record.get('diagnostic_run_id')),
        'segment_id':segment_id,'status':segment_status,'reason':segment_reason,
        'message':'上一育成段：角色事件后未发现才能出众' if segment_reason=='cut_character_event_absent' else
            '上一育成段已到正常刷切检查点' if normal else _text(record.get('error'),800) or '上一育成段结果',
        'is_normal_checkpoint':normal,'is_current_attempt':attempt_id is not None}
    source='control';reason=None;message=None;pause_source='not_recorded'
    reportable=False
    if license.get('status')=='paused':
        source='runtime_license';reason=_text(license.get('code'),200)
        message=_text(license.get('message'),1000) or note;pause_source='license'
        reportable=state=='unresponsive'
    elif state in PAUSED and note:
        source='runtime_note';message=note;pause_source='runtime_note'
    elif pending:
        source='batch_pending';reason=pending.get('reason') or pending.get('state')
        message='存在未确认的操作，保持原处理和资源保护。'
        reportable=state in {'stopped','unresponsive'}
    elif batch_stop and batch_status not in {'ready','running'}:
        source='batch_stop';reason=_text(batch_stop.get('reason'),200)
        message=_text(batch_stop.get('message'),1000)
        reportable=(state in {'stopped','unresponsive'} and batch_status in {'unknown','route_failed'}
            and batch_stop.get('normal_stop') is not True)
    elif state in PAUSED:
        message='当前已暂停，暂停来源未记录；保持本局，等待原控制继续。'
    elif state in {'starting','running','resume_requested'}:
        message='当前任务正在运行。'
    elif state in {'stopped','unresponsive'}:
        result=_obj(control.get('last_result'))
        reason=_text(control.get('last_error'),200) or _text(result.get('reason'),200)
        if reason:
            # A successful current segment can intentionally yield to another
            # owner. Match its recorded outcome; a reason name alone is not success.
            matched_success=(result.get('outcome')=='completed'
                and not control.get('last_error') and not record.get('error')
                and segment_status in {'completed','handoff','batch_limit_reached','year_checkpoint'}
                and segment_reason==reason)
            reportable=(not matched_success and reason not in
                {'user_stop','stop_requested','backend_reconnected','cut_limit_reached','cut_found'})
            if matched_success:
                message=('当前已到正常交接节点。' if segment_status=='handoff' else
                    '当前任务已正常完成。' if segment_status=='completed' else '当前任务已到正常检查点。')
        if not reason and segment_reason and not normal and segment_status not in {'running','completed','reattach','dry_run','batch_limit_reached','year_checkpoint','handoff'}:
            source='segment';reason=segment_reason;message=_text(record.get('error'),1000);reportable=True
    if manual:
        manual_message=('首次凯旋门人工交接等待确认。' if manual['stage']=='first_arc' else '育成日期人工交接等待确认。')
        if state in PAUSED and source in {'control','runtime_note'}:
            pause_source='manual_handoff'
            source='manual_handoff';reason=manual['reason'];message=manual_message;reportable=False
        elif state=='unresponsive' and source!='runtime_license':
            pause_source='manual_handoff'
            message=manual_message+'后台心跳中断，尚不能确认运行状态；保持本局。'
    if normal and source=='control' and reason==segment_reason:reason=None;reportable=False
    if source=='control' and reason=='runner_heartbeat_failed' and not message:
        message=('后台运行心跳更新失败，已停止脚本并保留当前育成。'
            if state=='stopped' else '后台运行心跳更新失败，已请求停止脚本；请等待后台确认。')
        message+='可提交停止报告查看已记录的异常类型。'
    runner_error=None
    if source=='control' and reason in {'runner_error','device_busy','execution_exception'}:
        runner_error=_load_runner_error(snapshot,root,batch_id,attempt_id,control.get('run_id'))
        if runner_error and not message:
            err_msg=(runner_error.get('error_message') or '')
            loc=runner_error.get('location') or '<unknown>'
            line=runner_error.get('line') or 0
            detail=f"（{runner_error['error_type']} @ {loc}:{line}）"
            message=(f"后台运行异常停止{detail}：{err_msg}" if err_msg else f"后台运行异常停止{detail}。")[:500]
    context={'schema_version':SCHEMA,'mode':mode,'state':state,'source':source,'reason':reason,
        'message':message,'step':step,'pause_source':pause_source,'batch_id':batch_id,'attempt_id':attempt_id,
        'batch_status':batch_status,'batch_stop':batch_stop,'pending_action':pending,
        'manual_handoff':manual,
        'restart_pending':step=='cut_restart' and batch_status=='running' and state in ACTIVE,'reportable_failure':reportable}
    if runner_error is not None:
        context['runner_error']=runner_error
    # The inner career sees the heartbeat owner's stop as user_stop. Preserve
    # the initiating exception type with the outer failure, without exporting
    # exception messages, paths, or an old run's unrelated diagnostic value.
    heartbeat_error=control.get('heartbeat_error_type')
    if (source=='control' and reason=='runner_heartbeat_failed'
            and isinstance(heartbeat_error,str)
            and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}',heartbeat_error)):
        context['heartbeat_error_type']=heartbeat_error
        io_failure=safe_metadata_failure(control.get('heartbeat_io_failure'))
        if io_failure:context['heartbeat_io_failure']=io_failure
    snapshot['current_stop_context']=context;snapshot['last_segment_result']=last
    return snapshot

def for_report(host,request,run=None):
    """Current outer state is marked separately when exporting an old run."""
    try:
        value=host.snapshot(mode=request.get('mode',getattr(host,'mode','career')))
        if not isinstance(value,dict):return {'status':'unavailable'}
        if not isinstance(value.get('current_stop_context'),dict):annotate(value,host.root)
        context=value['current_stop_context'];segment=value.get('last_segment_result')
        wanted=run.name if isinstance(run,Path) else request.get('run_id')
        scope='current_attempt' if wanted==context.get('attempt_id') else 'historical_record'
        return {'status':'available','selection_scope':scope,'requested_run_id':_id(wanted),
            'current_stop_context':context,'last_segment_result':segment,
            'missing_historical_frame_note':'旧报告未保存的画面无法补回；本次不采集新画面替代。'}
    except Exception:return {'status':'unavailable'}
