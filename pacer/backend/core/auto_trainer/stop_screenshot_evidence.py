"""Read one saved, game-only abnormal-stop frame. Never captures or uploads."""
from datetime import datetime
import hashlib,json,math,re
from pathlib import Path

SCOPES={'game_screen_at_abnormal_stop','game_screen_before_abnormal_stop'}
CONTEXT_KEYS={'run_id','segment_id','segment_status','stop_reason','stop_recorded_at','frame_relative_path',
    'frame_sha256','captured_at','frame_scope','frame_age_seconds','archive_report_id','reader_failure','frame_status',
    'capture_failure','frame_unavailable_reason','technical_wait','frame_observation',
    'frame_relation','frame_warning'}
_ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,159}\Z')
_REASON=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z')
_LEAF_CODE=re.compile(r'[a-z][a-z0-9_.-]{0,159}\Z')
_LEAF_STAGES={'entry','open','collect','scroll_up','scroll_down','close','return','cleanup',
    'larc_ss_information','unknown','preflight','connecting','initial_capture','main_menu',
    'opening_chooser','reading_chooser','cancelling_inspection','verifying_return',
    'returned_main_menu','selecting_outing','reading_confirmation','rebuilding_return_provider'}


def safe_menu_identity_comparison(value):
    """Re-project the finite outing comparison; never preserve raw text/paths."""
    if not isinstance(value,dict) or value.get('schema_version') != 'larc.outing_menu_comparison.v1':return {}
    result={'schema_version':value['schema_version']}
    if type(value.get('turn_index')) is int and 1<=value['turn_index']<=1000:result['turn_index']=value['turn_index']
    for key in ('reobserved','recovered','same_career_position','session_unchanged'):
        if type(value.get(key)) is bool:result[key]=value[key]
    def number(item):
        if type(item) is int:return item if -(2**63)<=item<2**63 else None
        return item if type(item) is float and math.isfinite(item) else None
    def text(item):
        if not isinstance(item,dict):return None
        clean={}
        if type(item.get('known')) is bool:clean['known']=item['known']
        if type(item.get('length')) is int and 0<=item['length']<=4096:clean['length']=item['length']
        digest=item.get('text_sha256')
        if isinstance(digest,str) and re.fullmatch(r'[a-f0-9]{16}',digest):clean['text_sha256']=digest
        return clean or None
    def field(key,item):
        if key=='stats':
            return {name:number(item[name]) for name in ('speed','stamina','power','guts','wisdom')
                if isinstance(item,dict) and name in item}
        if key=='target_text':return text(item)
        if key=='period':
            if not isinstance(item,dict):return None
            clean={name:item[name] for name,allowed in (
                ('year_index',{1,2,3}),('month',set(range(1,13))),('half',{'first','second'}),
                ('stage',{'junior','classic','senior'})) if type(item.get(name)) in (int,str) and item[name] in allowed}
            if type(item.get('pre_debut')) is bool:clean['pre_debut']=item['pre_debut']
            return clean
        if key=='mood':
            return item if isinstance(item,str) and item in {'great','excellent','good','normal','bad','very_bad','poor','awful'} else text(item)
        return number(item)
    observations=[]
    for row in value.get('observations',[])[:2] if isinstance(value.get('observations'),list) else []:
        if not isinstance(row,dict):continue
        clean={};fields={}
        digest=row.get('source_sha256')
        if isinstance(digest,str) and re.fullmatch(r'[a-f0-9]{64}',digest):clean['source_sha256']=digest
        original=row.get('fields')
        if not isinstance(original,dict):continue
        for key in ('stats','skill_pt','remaining_turns','target_text','vitality','period','mood'):
            data=original.get(key)
            if not isinstance(data,dict) or type(data.get('same')) is not bool:continue
            fields[key]={'same':data['same'],**{name:field(key,data[name]) for name in ('expected','observed') if name in data}}
        if fields:
            clean.update(fields=fields,changed_fields=[key for key,row in fields.items() if not row['same']])
            observations.append(clean)
    if not observations:return {}
    result['observations']=observations
    return result


def safe_capture_failure(value):
    """Keep the capture adapter's small enum diagnostic, never stderr/paths."""
    if not isinstance(value,dict)or value.get('schema_version')!='uma.capture_failure.v1':return {}
    backends={'adb-png','adb-raw','adb-shell-png','mumu-ipc','ldplayer-opengl','portable-native','unknown'}
    stages={'connect','native','exec_out','shell','pull','decode','write','capture'}
    reasons={'command_failed','timeout','empty_frame','decode_failed','write_failed','connection_failed',
        'native_unavailable','exception','capture_failed','invalid_budget','unsupported_raw_protocol'}
    classes={'TimeoutExpired','TimeoutError','OSError','RuntimeError','ValueError','TypeError','PermissionError',
        'FileNotFoundError','ConnectionError','AttributeError','Exception','UnicodeDecodeError'}
    if value.get('backend')not in backends or value.get('stage')not in stages or value.get('reason')not in reasons:return {}
    result={key:value[key]for key in ('schema_version','backend','stage','reason')}
    if type(value.get('timed_out'))is bool:result['timed_out']=value['timed_out']
    for key in ('return_code','os_error_code'):
        if type(value.get(key))is int and -(2**31)<=value[key]<2**31:result[key]=value[key]
    if value.get('error_class')in classes:result['error_class']=value['error_class']
    native=value.get('native_fallback')
    if (isinstance(native,dict)and native.get('backend')in backends and native.get('reason')in reasons):
        result['native_fallback']={key:native[key]for key in ('backend','reason')}
        if native.get('error_class')in classes:result['native_fallback']['error_class']=native['error_class']
    return result


def safe_technical_wait(value):
    if not isinstance(value,dict)or value.get('schema_version')!='uma.technical_wait_stop.v1':return {}
    result={'schema_version':value['schema_version']}
    for key in ('owner','reason','surface'):
        item=value.get(key)
        if isinstance(item,str)and _LEAF_CODE.fullmatch(item):result[key]=item
    wait=value.get('wait')
    if isinstance(wait,dict):
        clean={}
        if wait.get('phase')in {'unknown','transition','connection','loading'}:clean['phase']=wait['phase']
        for key in ('idle_elapsed_seconds','phase_elapsed_seconds','total_elapsed_seconds',
                    'idle_limit_seconds','phase_limit_seconds','total_limit_seconds'):
            item=wait.get(key)
            if type(item)in(int,float)and math.isfinite(item)and 0<=item<=86400:clean[key]=item
        expired=wait.get('expired_by')
        if isinstance(expired,list):clean['expired_by']=[x for x in expired if x in {'idle','phase','total'}]
        if clean:result['wait']=clean
    return result if len(result)>1 else {}


def safe_reader_failure(value):
    """Small saved reader context; never OCR, raw exceptions or source paths."""
    if not isinstance(value,dict):return {}
    reason=value.get('decision_reason');stage=value.get('stage')
    if (not isinstance(reason,str)or not _LEAF_CODE.fullmatch(reason)
            or not isinstance(stage,str)or stage not in _LEAF_STAGES):return {}
    result={'decision_reason':reason,'stage':stage};observation=value.get('observation')
    if not isinstance(observation,dict):return result
    fields={key:observation[key]for key in ('returned_to_training','turn_consumed',
        'turn_consuming_action_executed','same_training_hud','decision_safe','performed',
        'tap_performed','has_pending_operation')if key in observation
        and (observation[key]is None or type(observation[key])is bool)}
    pending=observation.get('pending_operation')
    if isinstance(pending,dict):
        status={key:pending[key]for key in ('stage','turn_index')
            if type(pending.get(key))is int and 0<=pending[key]<=1000}
        if type(pending.get('tap_performed'))is bool:status['tap_performed']=pending['tap_performed']
        action=pending.get('action_id')
        if isinstance(action,str)and re.fullmatch(r'(?:outing\.satake_mei\.[1-5]|mei_outing\.select)',action):
            status['action_id']=action
        if status:fields['pending_operation']=status
    comparison=safe_menu_identity_comparison(observation.get('menu_identity_comparison'))
    if comparison:fields['menu_identity_comparison']=comparison
    if fields:result['observation']=fields
    return result

def _time(value):
    if not isinstance(value,str)or not 1<=len(value)<=48:raise ValueError('invalid_stop_time')
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('invalid_stop_time')
    return parsed.timestamp()


def safe_frame_observation(value):
    if not isinstance(value,dict)or value.get('schema_version')!='uma.stop_frame_observation.v1':return {}
    if value.get('capture_method')not in {'screenshot','screenshot_exec_out','read_frame','read_adb_frame','read_adb_raw_frame'}:return {}
    result={key:value[key]for key in ('schema_version','capture_method')}
    for key in ('sequence','owner_turn_index','stop_owner_turn_index'):
        item=value.get(key)
        if key!='sequence'and item is None:
            result[key]=None;continue
        if type(item)is not int or not 0<=item<=1_000_000:return {}
        result[key]=item
    return result


def frame_facts(meta):
    """Describe observed time/turn gaps; a same-run cache alone proves no stop scene."""
    age=_time(meta.get('stop_recorded_at'))-_time(meta.get('captured_at'))
    if age<0:raise ValueError('stop_frame_from_future')
    result={'frame_age_seconds':round(age,3)}
    observation=safe_frame_observation(meta.get('frame_observation'))
    if observation:result['frame_observation']=observation
    if meta.get('scope',meta.get('frame_scope'))=='game_screen_at_abnormal_stop':
        result['frame_relation']='at_stop'
    elif not observation:
        result.update(frame_relation='legacy_unverified',frame_warning='capture_binding_not_recorded')
    elif observation['owner_turn_index']is None or observation['stop_owner_turn_index']is None:
        result.update(frame_relation='legacy_unverified',frame_warning='owner_turn_not_recorded')
    elif observation['owner_turn_index']!=observation['stop_owner_turn_index']:
        result.update(frame_relation='historical_context',frame_warning='turn_changed_since_frame')
    else:
        result.update(frame_relation='last_observed',frame_warning='earlier_observation')
    return result

def valid_jpeg(raw,width=None,height=None):
    if not isinstance(raw,bytes)or not len(raw)>0 or not raw.startswith(b'\xff\xd8')or not raw.endswith(b'\xff\xd9'):
        raise ValueError('invalid_stop_image')
    import cv2,numpy as np
    frame=cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_COLOR)
    if frame is None:raise ValueError('invalid_stop_image')
    h,w=frame.shape[:2]
    if w<360 or h<640 or max(w,h)>1600 or not 1.6<=h/w<=2.3:raise ValueError('not_full_game_image')
    if width is not None and width!=w or height is not None and height!=h:raise ValueError('stop_image_dimensions_changed')
    return w,h

def load_stop_evidence(run_root,*,expected_reason=None,stopped_at_unix_ms=None):
    """Use only manifest.json/stop.jpg; old timestamps are labelled, never guessed."""
    unavailable={'status':'no_saved_stop_frame'}
    try:
        run=Path(run_root).resolve(strict=True);folder=run/'session/stop_diagnostic'
        manifest=folder/'manifest.json';image=folder/'stop.jpg'
        if any(not p.resolve().is_relative_to(run)for p in (folder,manifest,image)):return unavailable
        if not manifest.is_file()or manifest.stat().st_size>64*1024:return unavailable
        before=manifest.read_bytes();meta=json.loads(before.decode('utf-8-sig'))
        if not isinstance(meta,dict)or meta.get('schema_version')!='uma.stop_diagnostic.v1'or meta.get('run_id')!=run.name:
            return unavailable
        reason=meta.get('reason');scope=meta.get('scope')
        if meta.get('status')not in {'ready','unavailable'}or scope not in SCOPES or not isinstance(reason,str)or not _REASON.fullmatch(reason):return unavailable
        if expected_reason is not None and reason!=expected_reason:return unavailable
        stopped=_time(meta.get('stop_recorded_at'))
        if type(stopped_at_unix_ms)in(int,float) and stopped*1000+1<stopped_at_unix_ms:return unavailable
        segment=meta.get('segment_id')
        verified=(isinstance(segment,str)and re.fullmatch(r'autonomous_[A-Za-z0-9_-]{1,130}',segment)
            and meta.get('source_run_session_id')==run.name)
        leaf=safe_reader_failure(meta.get('reader_failure'));capture=safe_capture_failure(meta.get('capture_failure'))
        wait=safe_technical_wait(meta.get('technical_wait'))
        if (leaf or capture or meta.get('status')=='unavailable')and manifest.read_bytes()==before:
            context={'run_id':run.name,'segment_id':segment if verified else None,
                'segment_status':'verified'if verified else 'not_recorded','stop_reason':reason,
                'stop_recorded_at':meta['stop_recorded_at'],'frame_relative_path':None,'frame_sha256':None,
                'captured_at':None,'frame_scope':None,'frame_status':'unavailable'}
            if leaf:context['reader_failure']=leaf
            if capture:context['capture_failure']=capture
            if wait:context['technical_wait']=wait
            unavailable_reason=meta.get('capture_error_reason')
            context['frame_unavailable_reason']=(unavailable_reason if isinstance(unavailable_reason,str)
                and _LEAF_CODE.fullmatch(unavailable_reason)else 'stop_frame_unavailable')
            archived=meta.get('archive_report_id')
            if isinstance(archived,str)and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',archived):
                context['archive_report_id']=archived
            unavailable['context']=context
        if meta.get('status')!='ready'or not image.is_file():return unavailable
        captured=_time(meta.get('captured_at'))
        info=image.stat()
        if info.st_size<=0:return unavailable
        raw=image.read_bytes();digest=hashlib.sha256(raw).hexdigest()
        recorded=meta.get('image_sha256')
        if recorded is not None:
            if recorded!=digest:return unavailable
        elif not stopped<=info.st_mtime<=manifest.stat().st_mtime:return unavailable
        if manifest.read_bytes()!=before or image.stat().st_size!=info.st_size or image.stat().st_mtime_ns!=info.st_mtime_ns:return unavailable
        w,h=valid_jpeg(raw,meta.get('width'),meta.get('height'))
        context={'run_id':run.name,'segment_id':segment if verified else None,
            'segment_status':'verified'if verified else 'not_recorded','stop_reason':reason,
            'stop_recorded_at':meta['stop_recorded_at'],'frame_relative_path':'session/stop_diagnostic/stop.jpg',
            'frame_sha256':digest,'captured_at':meta['captured_at'],'frame_scope':scope}
        context.update(frame_facts(meta))
        archived=meta.get('archive_report_id')
        if isinstance(archived,str)and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',archived):context['archive_report_id']=archived
        if leaf:context['reader_failure']=leaf
        if capture:context['capture_failure']=capture
        if wait:context['technical_wait']=wait
        return {'status':'available','context':context,'image_bytes':raw,'image_path':image,
            'metadata':{'width':w,'height':h,'size_bytes':len(raw),'image_sha256':digest,'scope':scope}}
    except Exception:return unavailable

def _same_instant(expected,actual):
    """JSON date round-trips may change UTC spelling or fractional zeros."""
    try:
        _time(expected);_time(actual)  # Require valid timezone-aware timestamps.
        return datetime.fromisoformat(expected.replace('Z','+00:00'))==datetime.fromisoformat(actual.replace('Z','+00:00'))
    except (TypeError,ValueError,OverflowError):return False

def same_context(expected,actual):
    return (isinstance(expected,dict)and isinstance(actual,dict)and set(expected)<=CONTEXT_KEYS
        and all(expected.get(k)==actual.get(k)
            for k in ('run_id','segment_id','stop_reason','frame_relative_path','frame_sha256','frame_scope'))
        and _same_instant(expected.get('stop_recorded_at'),actual.get('stop_recorded_at'))
        and (expected.get('frame_status')==actual.get('frame_status')=='unavailable'
            and expected.get('captured_at')is None and actual.get('captured_at')is None
            or _same_instant(expected.get('captured_at'),actual.get('captured_at'))))
