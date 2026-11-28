"""One bounded game-frame diagnostic per stopped career; never desktop capture."""
import base64
from datetime import datetime, timezone
import json
import hashlib
import math
import re
from pathlib import Path
import subprocess
import time

import cv2
import numpy as np

MAX_IMAGE_BYTES=512*1024
FOLDER='session/stop_diagnostic'
MAX_RECENT_AGE_SECONDS=120


def preview_jpeg(frame, *, max_bytes=MAX_IMAGE_BYTES):
    """Compress an already saved game frame; no capture or device access."""
    h,w=frame.shape[:2];scale=min(1.,1600/max(h,w))
    view=cv2.resize(frame,(round(w*scale),round(h*scale)),interpolation=cv2.INTER_AREA) if scale<1 else frame
    for quality in (92,85,75,60):
        ok,encoded=cv2.imencode('.jpg',view,[cv2.IMWRITE_JPEG_QUALITY,quality])
        if ok and len(encoded)<=max_bytes:
            return encoded.tobytes(),view.shape[1],view.shape[0],quality
    raise ValueError('diagnostic_preview_too_large')


def _folder(run):
    run=Path(run).resolve();folder=run/FOLDER
    from core.cut_portable_runtime import screenshot_output_allowed
    if not screenshot_output_allowed(run) or not folder.resolve().is_relative_to(run):raise ValueError('diagnostic_path_outside_run')
    if any(not (folder/name).resolve().is_relative_to(run) for name in ('manifest.json','stop.png','stop.jpg','recent.json','recent.png')):
        raise ValueError('diagnostic_path_outside_run')
    folder.mkdir(parents=True,exist_ok=True)
    return run,folder


def _decode(raw):
    if not raw or len(raw)>16*1024*1024:raise ValueError('diagnostic_frame_unavailable')
    frame=cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_COLOR)
    if frame is None or not 1<=frame.shape[0]*frame.shape[1]<=16_000_000:
        raise ValueError('diagnostic_frame_invalid')
    return frame


def _recent_metadata(folder):
    try:
        value=json.loads((folder/'recent.json').read_text(encoding='utf8'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def remember_recent_game_frame(run,port,*,paths=None,captured_after=0,now=None):
    """Keep one complete frame from this active run before disposable images go."""
    from core.auto_trainer.pacer_storage import _io, _rglob
    run,folder=_folder(run);now=time.time() if now is None else now
    previous=_recent_metadata(folder)
    same=(previous.get('schema_version')=='uma.recent_game_frame.v1' and
        previous.get('run_id')==run.name and previous.get('run_root')==str(run) and previous.get('device_port')==port)
    previous_time=previous.get('captured_at_unix',0) if same else 0
    if type(previous_time) not in (int,float):previous_time=0
    if previous and (not same or previous_time<captured_after or not 0<=now-previous_time<=MAX_RECENT_AGE_SECONDS):
        previous['usable']=False
        (folder/'recent.json').write_text(json.dumps(previous,ensure_ascii=False),encoding='utf8')
    candidates=[]
    for source in paths if paths is not None else _rglob(run, '*'):
        try:
            source=Path(source).resolve()
            if (not source.is_relative_to(run) or source.is_relative_to(folder) or not _io(source).is_file()
                    or source.suffix.lower() not in {'.png','.jpg','.jpeg','.webp'}
                    or any(part in {'member_portraits','crops','roi','assets'} for part in source.relative_to(run).parts)):
                continue
            info=_io(source).stat();captured_at=info.st_mtime
            if (captured_at<captured_after or captured_at<=previous_time or not 0<=now-captured_at<=MAX_RECENT_AGE_SECONDS
                    or not 0<info.st_size<=16*1024*1024):continue
            candidates.append((captured_at,source,info.st_size))
        except OSError:continue
    for captured_at,source,size in sorted(candidates,reverse=True):
        try:
            frame=_decode(_io(source).read_bytes());h,w=frame.shape[:2]
            # Runtime images are game-only. Exclude portrait/ROI crops and
            # sheets; only a complete portrait game-sized frame is eligible.
            if w<360 or h<640 or not 1.6<=h/w<=2.3:continue
            current=_io(source).stat()
            if current.st_mtime!=captured_at or current.st_size!=size:continue
            ok,encoded=cv2.imencode('.png',frame)
            if not ok:continue
            (folder/'recent.png').write_bytes(encoded.tobytes())
            metadata={'schema_version':'uma.recent_game_frame.v1','run_id':run.name,'run_root':str(run),
                'device_port':port,'usable':True,'captured_at_unix':captured_at,
                'captured_at':datetime.fromtimestamp(captured_at,timezone.utc).isoformat(),
                'capture_time_source':'game_frame_file_mtime','source_relative_path':source.relative_to(run).as_posix()}
            (folder/'recent.json').write_text(json.dumps(metadata,ensure_ascii=False),encoding='utf8')
            return {'status':'remembered','captured_at':metadata['captured_at']}
        except (OSError,ValueError,cv2.error):continue
    return {'status':'unchanged'}


def _recent_frame(run,folder,port,now):
    metadata=_recent_metadata(folder)
    if (metadata.get('schema_version')!='uma.recent_game_frame.v1' or metadata.get('run_id')!=run.name
            or metadata.get('run_root')!=str(run) or metadata.get('device_port')!=port):
        raise ValueError('recent_frame_scope_mismatch')
    if metadata.get('usable') is not True:raise ValueError('recent_frame_not_current')
    captured_at=metadata.get('captured_at_unix')
    if type(captured_at) not in (int,float) or not 0<=now-captured_at<=MAX_RECENT_AGE_SECONDS:
        raise ValueError('recent_frame_expired')
    raw=(folder/'recent.png').read_bytes()
    return _decode(raw),metadata,round(now-captured_at,3)


def _consume_recent(folder):
    metadata=_recent_metadata(folder)
    if metadata:
        metadata['usable']=False
        (folder/'recent.json').write_text(json.dumps(metadata,ensure_ascii=False),encoding='utf8')
        (folder/'recent.png').unlink(missing_ok=True)


def forget_recent_game_frame(run,port,*,captured_after=0):
    """Discard this invocation's redundant cache after a confirmed normal close."""
    run=Path(run).resolve();folder=run/FOLDER
    if not folder.resolve().is_relative_to(run) or not (folder/'recent.png').resolve().is_relative_to(run):return
    metadata=_recent_metadata(folder)
    timestamp=metadata.get('captured_at_unix')
    if (metadata.get('run_id')==run.name and metadata.get('run_root')==str(run)
            and metadata.get('device_port')==port and type(timestamp) in (int,float) and timestamp>=captured_after):
        _consume_recent(folder)


def _capture_error(error):
    if isinstance(error,subprocess.TimeoutExpired):return 'diagnostic_capture_timeout'
    code=str(error)
    return code if code.startswith('diagnostic_') and len(code)<100 else type(error).__name__


def _capture(port):
    from config.settings import ADB_PATH
    from core.cut_portable_runtime import device_ports
    if port not in device_ports():raise ValueError('diagnostic_device_not_selected')
    # The controller still holds this device's lease. Do not reconnect, restart,
    # tap, or consult another device when the selected connection has failed.
    result=subprocess.run([str(ADB_PATH),'-s',f'127.0.0.1:{port}','exec-out','screencap','-p'],
        capture_output=True,timeout=4,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise RuntimeError('diagnostic_capture_failed')
    return result.stdout


def _saved_game_frame(run,segment_id,snapshot,now):
    """One in-memory frame from this invocation, never another segment/run."""
    if not isinstance(snapshot,dict):raise ValueError('diagnostic_saved_frame_unavailable')
    if (snapshot.get('run_session_id')!=run.name
            or snapshot.get('segment_id')!=segment_id):raise ValueError('diagnostic_saved_frame_scope_mismatch')
    segment=run/segment_id
    from core.auto_trainer.stop_screenshot_evidence import safe_frame_observation
    observed=(snapshot.get('capture_time_source')=='capture_return_time'
        and bool(safe_frame_observation(snapshot.get('observation'))))
    source=Path(snapshot.get('path','')).resolve()
    run_resolved=run.resolve()
    segment_resolved=segment.resolve()
    allowed_roots=(segment_resolved,(run/'s').resolve()) if snapshot.get('source_scope')=='terminal_skills' else (segment_resolved,)
    matched_root=next(
        (root for root in allowed_roots if root.is_relative_to(run_resolved) and source.is_relative_to(root) and source!=root),
        None,
    )
    if not observed and (
            matched_root is None
            or source.suffix.lower()not in {'.png','.jpg','.jpeg','.webp'}
            or any(part in {'member_portraits','crops','roi','assets'}for part in source.relative_to(matched_root).parts)):
        raise ValueError('diagnostic_saved_frame_scope_mismatch')
    captured=snapshot.get('captured_at_unix')
    started=segment.stat().st_ctime
    if (type(captured)not in(int,float)or not math.isfinite(captured)
            or not started-.001<=captured<=now):raise ValueError('diagnostic_saved_frame_not_current')
    frame=snapshot.get('frame')
    if frame is None and not observed and source.is_file():
        info=source.stat()
        if (0<info.st_size<=16*1024*1024 and started-.001<=info.st_mtime<=now):
            frame=_decode(source.read_bytes())
    if (not isinstance(frame,np.ndarray)or frame.dtype!=np.uint8 or frame.ndim!=3 or frame.shape[2]!=3
            or not 1<=frame.shape[0]*frame.shape[1]<=16_000_000):raise ValueError('diagnostic_saved_frame_unavailable')
    h,w=frame.shape[:2]
    if w<360 or h<640 or not 1.6<=h/w<=2.3:raise ValueError('diagnostic_saved_frame_not_full_game')
    return frame,float(captured)


def save_stop_diagnostic(run,port,reason,*,capture=None,now=None,saved_frame=None,saved_only=False,
                         segment_id=None,capture_failure=None):
    from core.auto_trainer.run_support_profile import evidence as support_evidence
    run,folder=_folder(run);now=time.time() if now is None else now
    stopped_at=datetime.fromtimestamp(now,timezone.utc).isoformat()
    metadata={'schema_version':'uma.stop_diagnostic.v1','run_id':run.name,'status':'unavailable',
        'captured_at':stopped_at,'stop_recorded_at':stopped_at,'reason':str(reason)[:200],
        'scope':'game_screen_at_abnormal_stop','capture_timeout_seconds':4,
        'support_profile_evidence':support_evidence(run,run.name)}
    if isinstance(segment_id,str)and re.fullmatch(r'autonomous_[A-Za-z0-9_-]{1,130}',segment_id):
        metadata.update(segment_id=segment_id,source_run_session_id=run.name)
    from core.auto_trainer.stop_screenshot_evidence import safe_capture_failure
    failure=safe_capture_failure(capture_failure)
    if failure:metadata['capture_failure']=failure
    try:
        if saved_only:
            metadata.update(capture_performed=False,scope='game_screen_before_abnormal_stop',captured_at=None)
            if 'segment_id'not in metadata:raise ValueError('diagnostic_saved_segment_unavailable')
            frame,captured=_saved_game_frame(run,segment_id,saved_frame,now)
            age=now-captured
            time_source=saved_frame.get('capture_time_source')
            if time_source not in {'same_segment_saved_game_frame_mtime','capture_return_time'}:
                time_source='same_segment_last_successful_game_frame'
            metadata.update(captured_at=datetime.fromtimestamp(captured,timezone.utc).isoformat(),
                frame_age_seconds=round(age,3),capture_time_source=time_source,
                note=f'此图为停机前 {age:.1f} 秒本局本育成段已观察的游戏画面；不能据此确认停机时页面，未重新截图。')
            from core.auto_trainer.stop_screenshot_evidence import safe_frame_observation,frame_facts
            observation=safe_frame_observation(saved_frame.get('observation'))
            if observation:metadata['frame_observation']=observation
            metadata.update(frame_facts(metadata))
        else:
            try:
                frame=_decode((capture or _capture)(port))
            except Exception as error:
                metadata['capture_error']=type(error).__name__
                metadata['capture_error_reason']=_capture_error(error)
                try:frame,recent,age=_recent_frame(run,folder,port,now)
                except (OSError,ValueError,cv2.error) as fallback_error:
                    metadata['cache_rejection_reason']=str(fallback_error) if isinstance(fallback_error,ValueError) else type(fallback_error).__name__
                    raise
                metadata.update(scope='game_screen_before_abnormal_stop',captured_at=recent['captured_at'],
                    frame_age_seconds=age,capture_time_source=recent['capture_time_source'],
                    note=f'实时停机截图失败；此图为停机前 {age:.1f} 秒的本局游戏缓存，不是实时画面。')
            ok,png=cv2.imencode('.png',frame)
            if not ok:raise ValueError('diagnostic_encode_failed')
            (folder/'stop.png').write_bytes(png.tobytes())
        h,w=frame.shape[:2]
        encoded,width,height,quality=preview_jpeg(frame,max_bytes=MAX_IMAGE_BYTES)
        (folder/'stop.jpg').write_bytes(encoded)
        metadata['image_sha256']=hashlib.sha256(encoded).hexdigest()
        metadata.update(status='ready',width=width,height=height,
            original_width=w,original_height=h,size_bytes=len(encoded),jpeg_quality=quality,
            original_file=None if saved_only else 'stop.png',upload_file='stop.jpg')
    except Exception as error:
        # Diagnostics must not replace the actual stop or delay it with retries.
        metadata.setdefault('capture_error',type(error).__name__)
        metadata.setdefault('capture_error_reason',_capture_error(error))
    finally:
        # A later stop needs a newly observed frame; never reuse this stop's
        # cache, even if the user resumes the same attempt immediately.
        try:_consume_recent(folder)
        except OSError:pass
    temporary=folder/'manifest.json.tmp'
    temporary.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf8')
    temporary.replace(folder/'manifest.json')
    return metadata


def _pick_newer_saved_frame(run,segment,choices,best,best_time,now,*,source_scope=None):
    for captured,source in sorted(choices,reverse=True)[:16]:
        try:
            frame=_decode(source.read_bytes())
            best_frame=(best or {}).get('frame') if isinstance(best,dict) else None
            if (isinstance(best,dict) and isinstance(best.get('observation'),dict)
                    and best['observation'].get('schema_version')=='uma.stop_frame_observation.v1'
                    and hasattr(best_frame,'shape') and frame.shape==best_frame.shape
                    and frame.dtype==best_frame.dtype
                    and memoryview(frame).tobytes()==memoryview(best_frame).tobytes()):
                break
            row={'frame':frame,'path':str(source),'captured_at_unix':captured,
                'run_session_id':run.name,'segment_id':segment.name,
                'capture_time_source':'same_segment_saved_game_frame_mtime'}
            if source_scope=='terminal_skills':
                row['source_scope']='terminal_skills'
            _saved_game_frame(run,segment.name,row,now)
            return row,captured
        except (OSError,ValueError,cv2.error):continue
    return best,best_time


def _skill_worker_saved_frame(run,segment,summary,best,best_time,now):
    """Harvest the newest valid game frame from active/stopped skill-worker batches, bounded."""
    candidates=[]
    terminal=summary.get('terminal_skills')
    if (isinstance(terminal,dict) and terminal.get('run_session_id')==run.name
            and isinstance(terminal.get('batches'),list)):
        for value in reversed(terminal['batches'][-2:]):
            candidates.append((value,'terminal_skills'))
    prerace=summary.get('prerace_skills')
    if (isinstance(prerace,list) and prerace and isinstance(prerace[-1],dict)
            and isinstance(prerace[-1].get('batches'),list)):
        for value in reversed(prerace[-1]['batches'][-2:]):
            candidates.append((value,'prerace_skills'))
    run_resolved=run.resolve()
    segment_resolved=segment.resolve()
    terminal_root=(run_resolved/'s').resolve()
    scan_dirs=(
        ('capture',re.compile(r'[0-9]{10,}\.png')),
        ('transaction/selection/live_capture',re.compile(r'batch_[0-9]{3,}\.png')),
        ('transaction/prepare/capture',re.compile(r'page_[0-9]{2,}\.png')),
    )
    for value,source_scope in candidates:
        if not isinstance(value,str):continue
        try:
            worker_path=Path(value).resolve()
            if worker_path.name!='worker_report.json' or not worker_path.is_file():continue
            in_terminal=terminal_root.is_relative_to(run_resolved) and worker_path.is_relative_to(terminal_root)
            in_segment=segment_resolved.is_relative_to(run_resolved) and worker_path.is_relative_to(segment_resolved)
            if source_scope=='terminal_skills':
                if not (in_segment or in_terminal):continue
            elif not in_segment:
                continue
            if worker_path.stat().st_size>4*1024*1024:continue
            loaded=json.loads(worker_path.read_text('utf-8-sig'))
            if not isinstance(loaded,dict) or loaded.get('run_session_id')!=run.name:continue
            worker_dir=worker_path.parent.resolve()
            choices=[]
            for rel_dir,pattern in scan_dirs:
                target_dir=(worker_dir/rel_dir).resolve()
                if not target_dir.is_relative_to(worker_dir) or not target_dir.is_dir():continue
                for count,entry in enumerate(target_dir.iterdir()):
                    if count>=256:break
                    if not pattern.fullmatch(entry.name):continue
                    source=entry.resolve()
                    if not source.is_relative_to(worker_dir) or not source.is_file():continue
                    info=source.stat()
                    if best_time<info.st_mtime<=now and 0<info.st_size<=16*1024*1024:
                        choices.append((info.st_mtime,source))
            if choices:
                best,best_time=_pick_newer_saved_frame(
                    run,segment,choices,best,best_time,now,source_scope=source_scope)
        except (OSError,ValueError,TypeError):continue
    return best


def _ordinary_saved_frame(run,segment,summary,memory,now):
    """Known current race output plus the last owned memory frame, bounded."""
    best=memory
    try:_,best_time=_saved_game_frame(run,segment.name,best,now)
    except (OSError,ValueError,TypeError):best=None;best_time=0
    report=None;rows=summary.get('race_reports')
    if isinstance(rows,list)and rows and isinstance(rows[-1],dict):
        value=rows[-1].get('path')
        if isinstance(value,str):
            path=Path(value).resolve()
            if path.is_relative_to(segment)and path.name=='report.json':
                try:
                    if path.stat().st_size<=4*1024*1024:
                        loaded=json.loads(path.read_text('utf-8-sig'))
                        if isinstance(loaded,dict)and loaded.get('run_session_id')==run.name:report=loaded
                    if report is not None:
                        choices=[]
                        for count,source in enumerate(path.parent.iterdir()):
                            if count>=256:break
                            if not re.fullmatch(r'race_(?:route_step_)?[0-9]{3,}\.png',source.name):continue
                            source=source.resolve()
                            if not source.is_relative_to(segment)or not source.is_file():continue
                            info=source.stat()
                            if best_time<info.st_mtime<=now and 0<info.st_size<=16*1024*1024:
                                choices.append((info.st_mtime,source))
                        if choices:
                            best,best_time=_pick_newer_saved_frame(run,segment,choices,best,best_time,now)
                except (OSError,ValueError,TypeError):pass
    best=_skill_worker_saved_frame(run,segment,summary,best,best_time,now)
    return best,report


def save_ordinary_stop_diagnostic(run,port,summary,*,last_game_frame=None,capture_failure=None,now=None):
    """Freeze one ordinary abnormal stop before working-image cleanup."""
    if (not isinstance(summary,dict)or summary.get('status')!='stopped'
            or summary.get('stopped_by_control')is True or summary.get('normal_stop')is True):return {'status':'not_abnormal_stop'}
    run=Path(run).resolve();segment=Path(summary.get('output','')).resolve()
    if (summary.get('run_session_id')!=run.name or segment.parent!=run
            or not re.fullmatch(r'autonomous_[A-Za-z0-9_-]{1,130}',segment.name)):
        return {'status':'stop_context_unavailable'}
    now=time.time()if now is None else now
    last_game_frame,report=_ordinary_saved_frame(run,segment,summary,last_game_frame,now)
    metadata=save_stop_diagnostic(run,port,summary.get('stop_reason','execution_exception'),now=now,
        saved_only=True,saved_frame=last_game_frame,segment_id=segment.name,capture_failure=capture_failure)
    from core.auto_trainer.stop_screenshot_evidence import safe_technical_wait
    for owner in (summary,report or {}):
        rows=owner.get('technical_wait_stops')
        if isinstance(rows,list)and rows and isinstance(rows[-1],dict):
            row=rows[-1]
            if row.get('run_session_id')==run.name and row.get('reason')==summary.get('stop_reason'):
                wait=safe_technical_wait(row)
                if wait:
                    metadata['technical_wait']=wait
                    temporary=run/FOLDER/'manifest.json.tmp'
                    temporary.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf8')
                    temporary.replace(run/FOLDER/'manifest.json')
                    break
    return metadata


def save_outing_failure_diagnostic(run,port,reason,summary,*,now=None,app_root=None):
    """Bind a saved SS/outing failure to the existing report-button slot.

    None means this is another fault and its existing capture owner proceeds.
    An invalid current reader binding writes unavailable instead of replacing
    it with a fresh game screen or an unrelated cached frame.
    """
    from core.auto_trainer.recognition_failure_archive import verified_outing_failure_frame
    from core.auto_trainer.stop_screenshot_evidence import safe_reader_failure
    evidence=verified_outing_failure_frame(run,summary,app_root=app_root)
    if evidence['status']=='not_outing_failure':return None
    run,folder=_folder(run);now=time.time() if now is None else now
    stopped_at=datetime.fromtimestamp(now,timezone.utc).isoformat()
    metadata={'schema_version':'uma.stop_diagnostic.v1','run_id':run.name,'status':'unavailable',
        'captured_at':stopped_at,'stop_recorded_at':stopped_at,'reason':str(reason)[:200],
        'scope':'game_screen_before_abnormal_stop','capture_time_source':'bound_reader_failure_frame',
        'capture_performed':False,'capture_error_reason':evidence['status']}
    from core.auto_trainer.run_support_profile import evidence as support_evidence
    metadata['support_profile_evidence']=support_evidence(run,run.name)
    leaf=safe_reader_failure(evidence.get('diagnostics'))
    if leaf:
        metadata.update(reader_failure=leaf,segment_id=evidence['segment_id'],source_run_session_id=run.name)
    try:
        if str(reason)!=summary.get('stop_reason') or evidence['status']!='available':
            raise ValueError('outing_failure_binding_unavailable')
        raw=evidence['image_bytes'];source=evidence['source'];captured=evidence['captured_at_unix']
        (folder/'stop.jpg').write_bytes(raw)
        metadata.update(status='ready',captured_at=datetime.fromtimestamp(captured,timezone.utc).isoformat(),
            frame_age_seconds=max(0.,round(now-captured,3)),width=evidence['width'],height=evidence['height'],
            size_bytes=len(raw),image_sha256=hashlib.sha256(raw).hexdigest(),upload_file='stop.jpg',
            segment_id=evidence['segment_id'],source_run_session_id=run.name,
            source_relative_path=Path(source['path']).relative_to(run).as_posix(),
            source_frame_sha256=source['sha256'],
            source_report_relative_path=Path(evidence['report_path']).relative_to(run).as_posix(),
            has_pending_operation=evidence['has_pending_operation'],
            note='此图为识别失败时保存的本局同回合游戏现场，未重新截图。')
        metadata.pop('capture_error_reason',None)
        if evidence.get('source_recognition_archive_id'):
            metadata['source_recognition_archive_id']=evidence['source_recognition_archive_id']
    except Exception as error:
        metadata['capture_error']=type(error).__name__
    finally:
        try:_consume_recent(folder)
        except OSError:pass
    temporary=folder/'manifest.json.tmp'
    temporary.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf8')
    temporary.replace(folder/'manifest.json')
    return metadata


def read_stop_diagnostic(run):
    run=Path(run).resolve();folder=run/FOLDER
    try:
        if not folder.resolve().is_relative_to(run):return None,None
        metadata=json.loads((folder/'manifest.json').read_text(encoding='utf8'))
        if metadata.get('schema_version')!='uma.stop_diagnostic.v1' or metadata.get('run_id')!=run.name:return None,None
        fields=('status','captured_at','stop_recorded_at','reason','scope','capture_error','capture_error_reason',
            'cache_rejection_reason','frame_age_seconds','capture_time_source','note',
            'width','height','original_width','original_height','size_bytes','jpeg_quality')
        info={key:metadata[key] for key in fields if key in metadata}
        from core.auto_trainer.stop_screenshot_evidence import safe_capture_failure,safe_technical_wait
        for key,sanitize in (('capture_failure',safe_capture_failure),('technical_wait',safe_technical_wait)):
            clean=sanitize(metadata.get(key))
            if clean:info[key]=clean
        image=folder/'stop.jpg'
        if metadata.get('status')!='ready':return info,None
        if any(type(info.get(key)) is not int or not 1<=info[key]<=1600 for key in ('width','height')):return {**info,'status':'unavailable'},None
        if not image.resolve().is_relative_to(run) :return {**info,'status':'unavailable'},None
        data=image.read_bytes()
        if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):return {**info,'status':'unavailable'},None
        return info,data
    except (OSError,ValueError,TypeError):return None,None


def feedback_image(run):
    info,data=read_stop_diagnostic(run)
    if data is None:return None
    return {'name':'stop.jpg','media_type':'image/jpeg','width':info['width'],'height':info['height'],
            'data_base64':base64.b64encode(data).decode('ascii')}
