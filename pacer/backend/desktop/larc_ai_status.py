"""Bounded local AI display projection. No provider request, screenshot or raw reply."""
import json
import math
from pathlib import Path
import re
import time
import uuid

SCHEMA = 'uma.larc_ai_status.v1'
STATUS_FILE = 'session/ai_status.json'
_ACTIVE = {'starting','running','pause_requested','paused','resume_requested','stop_requested','unresponsive'}
_CODES = {
    'request_in_progress':'AI 正在比较候选，尚未采用最终动作。',
    'local_mode':'本局使用本地策略，不发送 AI 决策请求。',
    'not_a_critical_node':'当前节点未触发 AI，沿用本地策略。',
    'request_timeout':'AI 响应超时。', 'invalid_response':'AI 返回未通过结构化候选校验。',
    'provider_unavailable':'AI 接口暂不可用。',
}
_TRIGGERS = {'close_local_scores':'本地候选分差接近','ss_choice':'SS/SSS 候选比较',
    'ss_or_sss_choice':'SS/SSS 候选比较','overseas_adaptation_purchase':'海外适应性购买节点'}
_STATS = ('speed','stamina','power','guts','wisdom','skill_pt')
_ID = re.compile(r'[A-Za-z0-9_.:\-]{1,160}\Z')
_PATH = re.compile(r'(?:[A-Za-z]:[\\/]|\\\\)[^\s，。；]*')
_SECRET = re.compile(r'(?i)(?:Bearer\s+[^\s，。；]+|\bsk-[A-Za-z0-9_\-]{6,}|\b(?:api[_-]?key|token|authorization|password)\s*[:=]\s*[^\s，。；]+)')


def safe_text(value, limit=300):
    if not isinstance(value,str):return ''
    value=re.sub(r'(?is)<think\b[^>]*>.*?(?:</think>|\Z)','',value)
    value=_PATH.sub('[本地路径已隐藏]',value)
    value=_SECRET.sub('[凭据已隐藏]',value)
    return ''.join(c for c in value if c in '\n\t' or ord(c)>=32).strip()[:limit]


def _id(value):
    return value if isinstance(value,str) and _ID.fullmatch(value) else None


def _number(value):
    return value if type(value) in (int,float) and math.isfinite(value) else None


def _read(path, limit=256*1024):
    try:
        with Path(path).open('rb') as stream:data=stream.read(limit+1)
        value=json.loads(data.decode('utf-8-sig')) if len(data)<=limit else None
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError,UnicodeError):return {}


def _write(path,value):
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        path.parent.mkdir(parents=True,exist_ok=True)
        temp.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False),encoding='utf8')
        temp.replace(path)
    except (OSError,ValueError,TypeError):pass
    finally:
        try:temp.unlink(missing_ok=True)
        except OSError:pass


def decision_summary(payload,result):
    """Only existing numeric facts/candidate contracts and a short safe explanation."""
    context=payload.get('context') if isinstance(payload.get('context'),dict) else {}
    state=context.get('state') if isinstance(context.get('state'),dict) else context
    facts=[]
    for name,label in (('turn','回合'),('vitality','体力'),('max_vitality','体力上限')):
        value=_number(context.get(name,state.get(name)))
        if value is not None:facts.append({'label':label,'value':value})
    stats=state.get('current_stats') or {}
    if isinstance(stats,dict):
        for name in _STATS:
            if (value:=_number(stats.get(name))) is not None:facts.append({'label':name,'value':value})
    grid=context.get('grid') or {}
    if isinstance(grid,dict):
        for name in ('points','pending_cost'):
            if (value:=_number(grid.get(name))) is not None:facts.append({'label':'适应性 '+name,'value':value})
    chosen=_id(result.get('action_id'));local=_id(payload.get('local_action_id'))
    candidates=[]
    for row in (payload.get('candidates') or [])[:24]:
        if not isinstance(row,dict) or not (action:=_id(row.get('action_id'))):continue
        components={}
        raw=row.get('components')
        if isinstance(raw,dict):
            for key,value in list(raw.items())[:20]:
                if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,60}',str(key)) and _number(value) is not None:
                    components[str(key)]=value
        candidates.append({'action_id':action,'score':_number(row.get('score')),
            'allowed':row.get('allowed') is not False and row.get('rejected') is not True
                and row.get('hard_rejected') is not True and row.get('eligible') is not False,
            'selected':action==chosen,'local':action==local,'components':components,
            **{name:row[name] for name in ('cost','failure_rate','failure_rate_limit') if _number(row.get(name)) is not None}})
    status=result.get('status') if result.get('status') in ('local','ai','fallback','pause','pending') else 'local'
    reason=result.get('reason') if result.get('reason') in _CODES else 'ai_selected_allowed_candidate' if status=='ai' else 'decision_recorded'
    factors=list(dict.fromkeys(_TRIGGERS.get(item,'已启用的关键决策节点')
        for item in (payload.get('trigger_reasons') or []) if isinstance(item,str)))[:8]
    if chosen:factors.append('最终选择仍须通过现有执行器的候选与动作校验。')
    return {'node':_id(payload.get('node')),'status':status,'action_id':chosen,'local_action_id':local,
        'state':facts,'candidates':candidates,'key_factors':factors,
        'reason':_CODES.get(reason,'AI 已从当前可执行候选中完成选择。' if status=='ai' else '已记录本次选择。'),
        'explanation':safe_text(result.get('reason')) if status=='ai' else '',
        'elapsed_seconds':_number(result.get('elapsed_seconds')) or 0,
        'cached':result.get('cached') is True}


def publish_runtime(log_path,run_id,mode,payload=None,result=None,*,requesting=False):
    if not log_path:return
    result=result if isinstance(result,dict) else {}
    phase='requesting' if requesting else result.get('status') or ('waiting_trigger' if mode=='critical' else 'waiting_node')
    if phase=='local':phase='waiting_trigger' if mode=='critical' else 'local'
    value={'schema_version':SCHEMA,'run_id':run_id,'mode':mode,'phase':phase,'updated_at':time.time(),
        'elapsed_seconds':_number(result.get('elapsed_seconds')) or 0}
    if payload:value['decision']=decision_summary(payload,{'status':'pending','reason':'request_in_progress'} if requesting else result)
    _write(Path(log_path).parent/'ai_status.json',value)


def _last_legacy_decision(run):
    try:
        with (run/'session/ai_decisions.jsonl').open('rb') as stream:
            stream.seek(0,2);length=stream.tell();stream.seek(max(0,length-512*1024))
            lines=stream.read().decode('utf8').splitlines()
        for line in reversed(lines[-4:]):
            value=json.loads(line)
            if isinstance(value,dict) and isinstance(value.get('request'),dict) and isinstance(value.get('result'),dict):
                return decision_summary(value['request'],value['result'])
    except (OSError,ValueError,UnicodeError):pass
    return None


def _run(root,source,mode):
    if not source:return None
    try:
        root=Path(root).resolve();run=Path(source).resolve()
        if not run.is_relative_to(root/'runs'):return None
        if mode=='cut':
            data=_read(run/'cut_farming.json')
            attempts=data.get('attempts') or []
            if not attempts or not isinstance(attempts[-1],dict):return None
            attempt=Path(attempts[-1].get('run_root','')).resolve()
            if not attempt.is_relative_to(run/'attempts'):return None
            run=attempt
        return run
    except (OSError,ValueError,TypeError):return None


def status(root,*,run_root=None,mode='career',control=None,current_attempt=False):
    from desktop.larc_ai_client import provider_status
    control=control if isinstance(control,dict) else {}
    active=control.get('state') in _ACTIVE
    run=_run(root,run_root,'career' if current_attempt else mode)
    runtime={'mode':None,'phase':'not_started','active':active,'elapsed_seconds':0,
        'message':'尚未启动当前局；接口保存或连接测试不会自动启用 AI。'}
    decision=None
    if run:
        options_path=run/'session/ai_decision.json'
        options=_read(options_path)
        chosen=(options.get('options') or {}).get('mode')
        if chosen not in ('local','critical','full'):chosen='local' if not options_path.exists() else None
        saved=_read(run/STATUS_FILE)
        if saved.get('schema_version')!=SCHEMA or saved.get('run_id')!=run.name:saved={}
        if chosen=='local':
            runtime.update(mode='local',phase='local',message='本局使用本地策略，不请求 AI；已保存接口不代表正在用 AI。')
        elif chosen in ('critical','full'):
            phase=saved.get('phase') if saved.get('phase') in ('waiting_trigger','waiting_node','requesting','ai','fallback','pause') else ('waiting_trigger' if chosen=='critical' else 'waiting_node')
            if control.get('state') in ('paused','pause_requested'):phase='paused'
            elif not active:phase='stopped'
            messages={'waiting_trigger':'本局为关键时刻 AI；当前等待触发决策节点。',
                'waiting_node':'本局为每回合 AI；当前等待可决策节点。','requesting':'AI 请求中，正在等待响应。',
                'ai':'本局已采用最近一次 AI 候选选择。','fallback':'最近一次 AI 未可用，已按设置回退本地策略。',
                'pause':'AI 未可用，已按设置暂停并保留本局。','paused':'本局当前已暂停；暂停不会切换本局的 AI 模式。',
                'stopped':'当前没有运行中的 AI 请求，以下为本局最近一次记录。'}
            runtime.update(mode=chosen,phase=phase,message=messages[phase],elapsed_seconds=_number(saved.get('elapsed_seconds')) or 0)
            decision=saved.get('decision') if isinstance(saved.get('decision'),dict) else _last_legacy_decision(run)
        elif options_path.exists():
            runtime.update(phase='unknown',message='本局 AI 设置暂不可读，不推断为已启用或连接成功。')
    return {'connection':provider_status(root),'runtime':runtime,'decision':decision}


def for_host(host,root,*,snapshot=None):
    if isinstance(snapshot,dict):
        attempt=(snapshot.get('cut') or {}).get('current_attempt_root') if isinstance(snapshot.get('cut'),dict) else None
        return status(root,run_root=attempt or snapshot.get('run_root'),mode=snapshot.get('mode','career'),
            control=snapshot.get('control'),current_attempt=bool(attempt))
    control=host.service.snapshot() if host.service else {'state':'idle','runtime':None}
    return status(root,run_root=host.run_root,mode=host.mode,control=control)
