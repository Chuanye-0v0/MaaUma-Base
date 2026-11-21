"""Read-only native diagnostics adapter; no device or task-control imports.

The RPC accepts only a record ID. Source paths come from existing host records
or a directly registered run under this backend's runs directory. The R2
exporter is reused without a shell or an upload transport.
"""
from pathlib import Path
import re

from desktop import native_stop_exporter as exporter

_ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,159}\Z')
_ALLOWED={'action','mode','endpoint','run_id','request_id'}


def _read(path,root):
    return exporter._read_json(path,root,[]) or {}


def _inside(path,root):
    return exporter._within(path,root)


def _attempt(batch,data,wanted,runs):
    attempts=data.get('attempts')
    config=data.get('config')
    if data.get('schema_version')!='uma.cut_farming_session.v1' or not isinstance(attempts,list) or not isinstance(config,dict):
        return None
    selected=attempts[-1:] if wanted==batch.name else [row for row in attempts if isinstance(row,dict) and row.get('run_id')==wanted]
    for row in selected:
        if not isinstance(row,dict) or not isinstance(row.get('run_root'),str):continue
        path=_inside(row['run_root'],runs)
        if path is None or path.parent!=batch/'attempts' or row.get('run_id')!=path.name:continue
        progress=_read(path/'progress.json',runs)
        if (progress.get('run_session_id')==path.name and type(config.get('device_port')) is int
                and progress.get('device')==f"127.0.0.1:{config['device_port']}"):
            return path
    return None


def _career(path,wanted,runs):
    if path.name!=wanted:return None
    marker=_read(path/'session/pacer_center_run.json',runs)
    progress=_read(path/'progress.json',runs)
    if (marker.get('schema_version')!='uma.pacer_center_run.v1' or marker.get('source')!='private_pacer_center'
            or marker.get('mode')!='career' or marker.get('run_id')!=path.name
            or progress.get('run_session_id')!=path.name or not isinstance(progress.get('device'),str)
            or marker.get('endpoint')!=progress.get('device')):
        return None
    if path.parent==runs and path.name.startswith('larc_panel_'):return path
    origin=marker.get('cut_origin')
    if isinstance(origin,dict) and isinstance(origin.get('batch_root'),str):
        batch=_inside(origin['batch_root'],runs)
        if batch is not None and batch!=runs and _attempt(batch,_read(batch/'cut_farming.json',runs),wanted,runs)==path:
            return path
    return None


def resolve_registered_run(host,request):
    if not isinstance(request,dict) or set(request)-_ALLOWED:raise ValueError('diagnostic_request_fields_invalid')
    wanted=request.get('run_id');mode=request.get('mode',getattr(host,'mode','career'))
    if not isinstance(wanted,str) or not _ID.fullmatch(wanted):raise ValueError('diagnostic_run_id_required')
    if mode not in {'career','cut'}:raise ValueError('diagnostic_mode_invalid')
    root=exporter._resolved(host.root);runs=root/'runs'
    panel=exporter._resolved(Path(host.umaa_root).parent)
    if runs not in {panel/'pacer/backend/runs',panel/'backend/runs'}:raise ValueError('diagnostic_panel_layout_invalid')
    candidates=[runs/wanted]
    active=getattr(host,'run_root',None)
    if active is not None:candidates.append(active)
    current=getattr(host,'_current_record',None)
    if callable(current):
        try:
            row=current(mode)
            if isinstance(row,dict) and isinstance(row.get('path'),str):candidates.append(row['path'])
        except Exception:pass
    seen=set()
    for raw in candidates:
        path=_inside(raw,runs)
        if path is None or path==runs or path in seen or not path.is_dir():continue
        seen.add(path)
        if mode=='career':
            result=_career(path,wanted,runs)
            if result is not None:return panel,result
        else:
            batch=path.parent.parent if path.parent.name=='attempts' else path
            if batch==runs or _inside(batch,runs) is None:continue
            result=_attempt(batch,_read(batch/'cut_farming.json',runs),wanted,runs)
            if result is not None:return panel,result
    raise ValueError('diagnostic_registered_run_not_found')


def _display(path):
    value=str(path)
    if value.startswith('\\\\?\\UNC\\'):return '\\\\'+value[8:]
    return value[4:] if value.startswith('\\\\?\\') else value


def export_for_host(host,request):
    """Never changes host mode/run_root/service or invokes task-control methods."""
    try:
        panel,run=resolve_registered_run(host,request)
        from desktop.pacer_stop_context import for_report
        outer=for_report(host,request,run)
        path,index=exporter.export_report(panel,run_directory=run,run_id=run.name,registered_run_directory=True,
            diagnostic_context=outer)
        count=len(index.get('failures',[]));frames=len(index.get('stop_evidence',[]))
        return {'ok':True,'action':'export_stop_diagnostics','read_only':True,'run_id':run.name,
            'zip_path':_display(path),'failure_count':count,'saved_stop_frame_count':frames,'status':index.get('status'),
            'message':f'已导出本局保存的失败诊断：{count} 个失败回合、{frames} 张停机截图。ZIP仅保存在本机，当前任务未改变。',
            'device_actions':0,'automatic_upload':False}
    except Exception:
        return {'ok':False,'action':'export_stop_diagnostics','read_only':True,
            'error':'diagnostic_export_unavailable','message':'暂时无法导出这份已登记记录，可稍后重试。当前任务及原记录未改变。',
            'device_actions':0,'automatic_upload':False}


def augment_stop_record(record,summary,summary_path):
    """Expose only the summary-bound last pipeline's small inner failure fields.

    This path never collects a ZIP or reads images. A resumed/running segment is
    not made to look failed because an earlier segment failed.
    """
    if not isinstance(record,dict) or not isinstance(summary,dict):return
    try:
        source=exporter._resolved(summary_path);segment=source.parent
        if source.name!='summary.json' or not exporter.SEGMENT.fullmatch(segment.name):return
        turns=summary.get('turns');turn=turns[-1] if isinstance(turns,list) and turns and isinstance(turns[-1],dict) else {}
        if not isinstance(turn.get('output'),str):return
        folder=_inside(turn['output'],segment)
        if folder is None or folder.parent!=segment or not exporter.TURN.fullmatch(folder.name):return
        pipeline=_read(folder/'pipeline_report.json',segment)
        detail=exporter.failure_details(pipeline)
        status=detail.get('status')
        if status not in exporter.TARGET_STATUSES:return
        progress=summary.get('final_progress')
        current=progress.get('next_turn') if isinstance(progress,dict) else None
        observed=turn.get('turn_index')
        prior=type(current) is int and type(observed) is int and observed<current
        diagnostic=record.get('stop_diagnostics')
        diagnostic=dict(diagnostic) if isinstance(diagnostic,dict) else {}
        diagnostic.update({'pipeline_status':status,'pipeline_diagnostic_only':True,
            'pipeline_source_is_prior_turn':prior})
        provider=detail.get('provider_error')
        if provider:diagnostic['provider_error']=provider
        observation=detail.get('training_page_observation')
        if isinstance(observation,dict):diagnostic['training_page_observation']=observation
        record['stop_diagnostics']=diagnostic
        if summary.get('status') not in {'running','dry_run','completed','batch_limit_reached','year_checkpoint','reattach'} and not prior:
            fields=[]
            if provider:fields.append('state_context_provider.error='+provider)
            if isinstance(observation,dict) and observation.get('errors'):
                fields.append('training_page_observation.errors='+', '.join(observation['errors'][:8]))
            if fields:
                existing=record.get('error')
                if isinstance(existing,str) and existing not in fields:fields.append(existing)
                record['error']='; '.join(fields)[:1600]
    except Exception:
        return
