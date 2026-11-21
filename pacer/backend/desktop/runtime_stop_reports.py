"""Explicit manual RuntimeStop reports with an already-saved, owned game frame.

No game capture/control. Preview is local; only submit sends the frozen draft
to the existing private bug-feedback endpoint. Missing images never gate text.
"""
import base64,hashlib,importlib.util,json,re,uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

from core.auto_trainer.stop_screenshot_evidence import load_stop_evidence,same_context,valid_jpeg,CONTEXT_KEYS,SCOPES,_time,safe_reader_failure,safe_capture_failure,safe_technical_wait,frame_facts
from desktop import recognition_failure_feedback as feedback

SCHEMA='maauma.runtime_stop_report.v1'
FOLDER=Path('logs/runtime-stop-feedback/drafts')
_ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,159}\Z')
_UUID=re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z')
_TEXT_FIELDS={'submission_id','app_version','area','title','steps','expected','actual','contact'}

def _root(host):
    root=feedback.application_root(Path(host.umaa_root).parent)
    if not(root/'maauma-suite.json').is_file():raise ValueError('runtime_stop_report_root_unavailable')
    return root
def _read(path,limit=800*1024):
    if not path.is_file()or limit is not None and path.stat().st_size>limit:raise ValueError('runtime_stop_report_unavailable')
    value=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value,dict):raise ValueError('runtime_stop_report_unavailable')
    return value
def _write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8');temporary.replace(path)
def _text_helper(root):
    path=root/'backend/desktop/maauma_error_feedback.py'
    if not path.is_file()or not path.resolve().is_relative_to(root):raise ValueError('runtime_stop_report_text_helper_missing')
    spec=importlib.util.spec_from_file_location('_runtime_stop_existing_text_redactor',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
def _resolve(host,request):
    from desktop.native_stop_diagnostics import resolve_registered_run
    return resolve_registered_run(host,{key:request[key]for key in ('action','mode','run_id','endpoint','request_id')if key in request})
def _enabled(root):
    try:return feedback._enabled(root)
    except Exception:return False
def _public(evidence):
    if evidence.get('status')!='available':return None
    raw=evidence['image_bytes'];meta=evidence['metadata']
    return {'media_type':'image/jpeg','width':meta['width'],'height':meta['height'],'size_bytes':len(raw),
        'sha256':hashlib.sha256(raw).hexdigest(),'data_base64':base64.b64encode(raw).decode('ascii')}

def _image_reason(evidence):
    context=evidence.get('context')or {};failure=safe_capture_failure(context.get('capture_failure'))
    if context.get('frame_unavailable_reason')=='diagnostic_save_failed':return 'write_failed'
    reason=(failure.get('reason')if failure else context.get('frame_unavailable_reason'))
    return reason if isinstance(reason,str)and re.fullmatch(r'[a-z][a-z0-9_.-]{0,159}',reason)else evidence.get('status','no_saved_stop_frame')

def _request_evidence(host,request,run):
    """A current report cannot fall back to a prior segment's ready manifest."""
    from desktop.pacer_stop_context import for_report
    outer=for_report(host,request,run)
    evidence=load_stop_evidence(run)
    if outer.get('selection_scope')=='historical_record':return evidence,outer
    last=outer.get('last_segment_result')or {};segment=last.get('segment_id')
    summary={};source=None
    if isinstance(segment,str)and re.fullmatch(r'autonomous_[A-Za-z0-9_-]{1,130}',segment):
        source=run/segment/'summary.json'
        try:
            loaded=_read(source,4*1024*1024)
            if (loaded.get('run_session_id')==run.name and source.parent.resolve().parent.samefile(run)
                    and Path(loaded.get('output','')).samefile(source.parent)):summary=loaded
        except (OSError,ValueError,TypeError):pass
    if not summary:
        # Before a cut attempt has a segment, its existing stop clock still
        # proves a newly saved frame. Never accept an earlier ready manifest.
        current=outer.get('current_stop_context')or {}
        if current.get('mode')=='cut'and run.parent.name=='attempts':
            try:
                batch=_read(run.parent.parent/'cut_farming.json',4*1024*1024)
                stopped=batch.get('stopped_at_unix_ms');reason=(batch.get('stop')or {}).get('reason')
                attempt=(batch.get('attempts')or [{}])[-1]
                if (batch.get('schema_version')=='uma.cut_farming_session.v1'
                        and batch.get('status')in {'unknown','route_failed'}and attempt.get('run_id')==run.name
                        and type(stopped)is int and stopped>0 and reason==current.get('reason')):
                    saved=load_stop_evidence(run,expected_reason=reason,stopped_at_unix_ms=stopped)
                    if isinstance(saved.get('context'),dict)and saved['context'].get('segment_id')is None:
                        return saved,outer
                    return {'status':'no_saved_stop_frame','context':{'run_id':run.name,'segment_id':None,
                        'segment_status':'not_recorded','stop_reason':reason,
                        'stop_recorded_at':datetime.fromtimestamp(stopped/1000,timezone.utc).isoformat(),
                        'frame_status':'unavailable','frame_relative_path':None,'frame_sha256':None,
                        'captured_at':None,'frame_scope':None,'frame_unavailable_reason':'current_stop_frame_unavailable'}},outer
            except (OSError,ValueError,TypeError,IndexError):pass
        return {'status':'no_saved_stop_frame'},outer
    diagnostic=summary.get('stop_diagnostic')if isinstance(summary.get('stop_diagnostic'),dict)else {}
    reason=(outer.get('current_stop_context')or {}).get('reason')or summary.get('stop_reason')
    if not isinstance(reason,str)or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}',reason):reason=None
    recorded=diagnostic.get('stop_recorded_at')
    try:_time(recorded)
    except (TypeError,ValueError,OverflowError):recorded=None
    current=evidence.get('context')or {}
    matched=(current.get('run_id')==run.name and current.get('segment_id')==segment
        and current.get('stop_reason')==reason)
    if matched and recorded is not None:
        try:matched=_time(recorded)==_time(current.get('stop_recorded_at'))
        except (TypeError,ValueError,OverflowError):matched=False
    if matched:return evidence,outer
    if recorded is None:
        recorded=datetime.fromtimestamp(source.stat().st_mtime,timezone.utc).isoformat()
    context={'run_id':run.name,'segment_id':segment,'segment_status':'verified','stop_reason':reason,
        'stop_recorded_at':recorded,'frame_status':'unavailable','frame_relative_path':None,
        'frame_sha256':None,'captured_at':None,'frame_scope':None,
        'frame_unavailable_reason':'diagnostic_save_failed'if diagnostic.get('error')else 'current_stop_frame_unavailable'}
    same_stop=summary.get('stop_reason')==reason
    capture=safe_capture_failure(summary.get('capture_failure')or diagnostic.get('capture_failure'))if same_stop else {}
    if capture:context['capture_failure']=capture
    wait=safe_technical_wait(diagnostic.get('technical_wait'))if same_stop else {}
    if wait:context['technical_wait']=wait
    return {'status':'no_saved_stop_frame','context':context},outer

def evidence_for_host(host,request):
    root=_root(host);evidence={'status':'no_saved_stop_frame'}
    try:
        _,run=_resolve(host,request);evidence,_=_request_evidence(host,request,run)
    except Exception:pass
    return {'ok':True,'status':evidence['status'],'context':evidence.get('context'),'image':_public(evidence),
        'image_unavailable_reason':None if evidence['status']=='available'else _image_reason(evidence),
        'include_screenshot_default':_enabled(root)and (evidence.get('context')or {}).get('frame_relation')in {'at_stop','last_observed'},
        'read_only':True,'device_actions':0,'network_requests':0}

def _historical(root,run,context):
    """Only an exact archived report UUID from this same registered run."""
    identifier=context.get('archive_report_id')if isinstance(context,dict)else None
    if not isinstance(identifier,str)or not _UUID.fullmatch(identifier):return None
    folder=root/feedback.QUEUE/identifier
    if any(not(folder/name).resolve().is_relative_to(root)for name in ('report.json','failure.jpg')):return None
    try:
        report=_read(folder/'report.json',64*1024);image=(folder/'failure.jpg')
        if (report.get('schema_version')!=feedback.REPORT_SCHEMA or report.get('report_id')!=identifier
                or report.get('run_id')!=run.name or report.get('report_kind')!='abnormal_flow_stop'):
            return None
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}',str(report.get('stop_reason','')))
                or report.get('segment_id')is not None and not re.fullmatch(r'autonomous_[A-Za-z0-9_-]{1,130}',str(report['segment_id']))):return None
        if report.get('frame_status')=='unavailable':
            leaf=safe_reader_failure(report.get('reader_failure'))
            capture=safe_capture_failure(report.get('capture_failure'))
            unavailable_reason=report.get('frame_unavailable_reason')
            reason_valid=isinstance(unavailable_reason,str)and re.fullmatch(r'[a-z][a-z0-9_.-]{0,159}',unavailable_reason)
            if (not(leaf or capture or reason_valid)or any(report.get(key)is not None for key in
                    ('image','source_relative_path','source_sha256','frame_sha256','captured_at','frame_scope'))):return None
            _time(report.get('stop_recorded_at'))
            actual={key:report.get(key)for key in ('run_id','segment_id','segment_status','stop_reason','stop_recorded_at')}
            actual.update(frame_relative_path=None,frame_sha256=None,captured_at=None,frame_scope=None,
                frame_status='unavailable',archive_report_id=identifier)
            if leaf:actual['reader_failure']=leaf
            if capture:actual['capture_failure']=capture
            if reason_valid:actual['frame_unavailable_reason']=unavailable_reason
            wait=safe_technical_wait(report.get('technical_wait'))
            if wait:actual['technical_wait']=wait
            return {'status':'no_saved_stop_frame','context':actual}if same_context(context,actual)else None
        if (report.get('source_relative_path')!='session/stop_diagnostic/stop.jpg'
                or report.get('frame_scope')not in SCOPES
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}',str(report.get('stop_reason','')))
                or report.get('segment_id')is not None and not re.fullmatch(r'autonomous_[A-Za-z0-9_-]{1,130}',str(report['segment_id']))):return None
        _time(report.get('stop_recorded_at'));_time(report.get('captured_at'))
        actual={key:report.get(key)for key in ('run_id','segment_id','segment_status','stop_reason','stop_recorded_at',
            'frame_sha256','captured_at','frame_scope')}
        actual.update(frame_relative_path=report.get('source_relative_path'),archive_report_id=identifier)
        actual.update(frame_facts(report))
        leaf=safe_reader_failure(report.get('reader_failure')or report.get('diagnostics'))
        if leaf:actual['reader_failure']=leaf
        capture=safe_capture_failure(report.get('capture_failure'))
        if capture:actual['capture_failure']=capture
        wait=safe_technical_wait(report.get('technical_wait'))
        if wait:actual['technical_wait']=wait
        if not same_context(context,actual)or not image.is_file()or image.stat().st_size<=0:return None
        raw=image.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=context.get('frame_sha256'):return None
        w,h=valid_jpeg(raw,report.get('width'),report.get('height'))
        return {'status':'available','context':actual,'image_bytes':raw,
            'metadata':{'width':w,'height':h,'size_bytes':len(raw)}}
    except Exception:return None

def prepare_for_host(host,request):
    root=_root(host);include=request.get('include_screenshot')is True;expected=request.get('context')
    evidence={'status':'no_saved_stop_frame'};run=None;outer={}
    try:
        _,run=_resolve(host,request);evidence,outer=_request_evidence(host,request,run)
    except Exception:pass
    if (outer.get('selection_scope')=='historical_record'and isinstance(expected,dict)
            and run is not None and not same_context(expected,evidence.get('context')or {})):
        evidence=_historical(root,run,expected)or {'status':'no_saved_stop_frame'}
    matched=same_context(expected,evidence.get('context')or {})
    status='excluded'if not include else 'available'if evidence.get('status')=='available'and matched else 'no_saved_stop_frame'if matched else(
        'stop_context_changed'if isinstance(expected,dict)else 'no_saved_stop_frame')
    context=evidence.get('context')if matched or outer.get('selection_scope')=='current_attempt'else None
    if context is None:
        identifier=request.get('run_id');context={'run_id':identifier if isinstance(identifier,str)and _ID.fullmatch(identifier)else None,
            'segment_id':None,'segment_status':'not_recorded','context_status':'not_revalidated'}
    helper=_text_helper(root);identifier=str(uuid.uuid4())
    message=request.get('message');stack=request.get('stack_trace');version=request.get('app_version')
    if any(not isinstance(value,str)for value in (message,stack,version))or len(message)>8000 or len(stack)>24000 or len(version)>100:
        raise ValueError('runtime_stop_report_invalid')
    if not outer:
        from desktop.pacer_stop_context import for_report
        outer=for_report(host,request,run)
    leaf=safe_reader_failure(context.get('reader_failure'))
    detail=('失败步骤与原因：\n'+json.dumps(leaf,ensure_ascii=False,sort_keys=True)+'\n'if leaf else '')
    capture=safe_capture_failure(context.get('capture_failure'))
    if capture:detail+='截图采集失败原因：\n'+json.dumps(capture,ensure_ascii=False,sort_keys=True)+'\n'
    if context.get('frame_relation')in {'historical_context','legacy_unverified'}:
        detail+='图片仅作历史上下文；无法确认与停止步骤相同，不能作为停机现场。\n'
    detail+=json.dumps(context,ensure_ascii=False,sort_keys=True)+'\n外层控制与上一育成段（分开记录）：\n'+json.dumps(outer,ensure_ascii=False,sort_keys=True)+'\n界面附加信息（可能含上一育成段）：\n'+stack
    payload=helper._payload({'app_version':version,'exception_type':'RuntimeStop','message':message,
        'stack_trace':detail,'recent_actions':[],'report_context':'training'},identifier)
    if status=='available':
        payload['attachment']={'name':'runtime-stop.jpg','media_type':'image/jpeg',
            'data_base64':base64.b64encode(evidence['image_bytes']).decode('ascii')}
    body=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf8')
    preview='MaaUma 停止报告\n版本：'+payload['app_version']+'\n'+payload['actual']+'\n'+(
        ('附上历史游戏画面作上下文，不能作为停机现场。'if context.get('frame_relation')in {'historical_context','legacy_unverified'}
            else '附上停止前已观察的游戏画面；采集与停止时间已记录。')if status=='available'
        else '本次未附截图；可继续提交文字报告。')
    path=root/FOLDER/identifier/'report.json'
    if not path.resolve().is_relative_to(root):raise ValueError('runtime_stop_report_path_invalid')
    _write(path,{'schema_version':SCHEMA,'report_id':identifier,'context':context,'attachment_status':status,
        'payload':payload,'outer_context':outer,'body_sha256':hashlib.sha256(body).hexdigest()})
    return {'ok':True,'report_id':identifier,'preview_text':preview,'screenshot_count':1 if status=='available'else 0,
        'attachment_status':status,'attachment_requested':include,'image_status':evidence['status'],
        'image_unavailable_reason':None if evidence['status']=='available'else _image_reason(evidence),
        'context':context,'network_requests':0,'device_actions':0}

def submit_for_host(host,request,*,endpoint=feedback.DEFAULT_ENDPOINT):
    root=_root(host);identifier=request.get('report_id')
    if not isinstance(identifier,str)or not _UUID.fullmatch(identifier):raise ValueError('runtime_stop_report_invalid')
    path=root/FOLDER/identifier/'report.json'
    if not path.resolve().is_relative_to(root):raise ValueError('runtime_stop_report_path_invalid')
    report=_read(path,None)
    if report.get('schema_version')!=SCHEMA or report.get('report_id')!=identifier:raise ValueError('runtime_stop_report_invalid')
    payload=report.get('payload')
    if not isinstance(payload,dict)or set(payload)not in(_TEXT_FIELDS,_TEXT_FIELDS|{'attachment'}):raise ValueError('runtime_stop_report_invalid')
    if payload.get('submission_id')!=identifier:raise ValueError('runtime_stop_report_invalid')
    limits={'app_version':100,'area':80,'title':160,'steps':4000,'expected':2000,'actual':4000,'contact':200}
    if any(not isinstance(payload.get(k),str)or len(payload[k].encode('utf-16-le'))>limit*2 for k,limit in limits.items()):
        raise ValueError('runtime_stop_report_invalid')
    attachment=payload.get('attachment')
    if attachment is not None:
        if (not isinstance(attachment,dict)or set(attachment)!={'name','media_type','data_base64'}
                or attachment.get('name')!='runtime-stop.jpg'or attachment.get('media_type')!='image/jpeg'):
            raise ValueError('runtime_stop_report_invalid')
        raw=base64.b64decode(attachment['data_base64'],validate=True);valid_jpeg(raw)
        if hashlib.sha256(raw).hexdigest()!=(report.get('context')or {}).get('frame_sha256'):
            raise ValueError('runtime_stop_report_changed')
    body=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf8')
    if hashlib.sha256(body).hexdigest()!=report.get('body_sha256'):raise ValueError('runtime_stop_report_changed')
    ack=report.get('ack')
    submitted={'screenshot_count':1 if attachment is not None else 0,
        'attachment_status':'submitted_with_image'if attachment is not None else 'text_only',
        'submission_confirmed':True}
    if isinstance(ack,str)and feedback._FEEDBACK_ID.fullmatch(ack):
        return {'ok':True,'feedback_id':ack,'status':'open','idempotent':True,**submitted}
    import sys
    def progress(done,total):
        print(json.dumps({'schema_version':'maauma.feedback_upload_progress.v1','stage':'upload','completed_parts':done,'total_parts':total}),file=sys.stderr,flush=True)
    feedback_id=feedback._send(body,feedback._endpoint(endpoint),state_path=path.with_name('upload-state.json'),progress=progress)
    report['ack']=feedback_id;_write(path,report)
    path.with_name('upload-state.json').unlink(missing_ok=True)
    return {'ok':True,'feedback_id':feedback_id,'status':'open','idempotent':False,**submitted}

def dispatch_for_host(host,request):
    try:
        if not isinstance(request,dict):raise ValueError('runtime_stop_report_invalid')
        action=request.get('action')
        common={'action','mode','run_id','endpoint','request_id'}
        allowed=common if action=='runtime_stop_evidence'else common|{'context','include_screenshot','app_version','message','stack_trace'}if action=='prepare_runtime_stop_report'else common|{'report_id'}
        if set(request)-allowed:raise ValueError('runtime_stop_report_fields_invalid')
        if action=='runtime_stop_evidence':return evidence_for_host(host,request)
        if action=='prepare_runtime_stop_report':return prepare_for_host(host,request)
        if action=='submit_runtime_stop_report':return submit_for_host(host,request)
        raise ValueError('runtime_stop_report_invalid')
    except HTTPError as error:
        return {'ok':False,'code':'runtime_stop_report_rate_limited'if error.code==429 else 'runtime_stop_report_unavailable',
            'device_actions':0,'automatic_retry':False}
    except Exception:return {'ok':False,'code':'runtime_stop_report_unavailable','device_actions':0,'automatic_retry':False}

def append_stop_export(panel_root,run_root,index,entries):
    """One current saved abnormal frame + immutable same-run queued stop frames."""
    root=Path(panel_root).resolve();run=Path(run_root).resolve();rows=[];seen=set()
    evidence=load_stop_evidence(run)
    if evidence.get('status')=='available':
        context=evidence['context'];name='saved-stop/'+context['frame_sha256']+'/failure.jpg'
        entries[name]=evidence['image_bytes'];rows.append({'context':context,'image':name})
        seen.add((context['stop_recorded_at'],context['frame_sha256']))
    queue=root/feedback.QUEUE
    if queue.is_dir()and queue.resolve().is_relative_to(root):
        for folder in sorted(queue.iterdir(),key=lambda p:p.name):
            if not _UUID.fullmatch(folder.name)or not folder.is_dir()or not folder.resolve().is_relative_to(queue.resolve()):continue
            try:
                report=_read(folder/'report.json',64*1024)
                if report.get('run_id')!=run.name or report.get('report_kind')!='abnormal_flow_stop':continue
                context={key:report[key]for key in ('run_id','segment_id','segment_status','stop_reason','stop_recorded_at',
                    'frame_sha256','captured_at','frame_scope')if key in report}
                context.update(frame_relative_path=report.get('source_relative_path'),archive_report_id=folder.name)
                leaf=safe_reader_failure(report.get('reader_failure')or report.get('diagnostics'))
                if leaf:context['reader_failure']=leaf
                saved=_historical(root,run,context)
                if saved is None:continue
                context=saved['context']
                identity=(context['stop_recorded_at'],context['frame_sha256'])
                if identity in seen:continue
                seen.add(identity)
                name='saved-stop/'+folder.name+'/failure.jpg';entries[name]=saved['image_bytes'];rows.append({'context':context,'image':name})
            except Exception:continue
    if rows:
        index['stop_evidence']=rows
        entries['saved-stop/contexts.json']=(json.dumps({'run_id':run.name,'stop_evidence':rows},ensure_ascii=False,indent=2)+'\n').encode('utf8')
