"""Run-scoped AI choice among existing executable candidates, never game input."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import queue
import re
import threading
import time
from core.auto_trainer.rejection_policy import is_hard_rejected

_CURRENT = ContextVar('larc_ai_runtime', default=None)
_FALLBACK_NOTE='AI暂未取得有效选择，本回合沿用本地策略；原因已记入决策记录。'


def _show_result(result):
    from core.auto_trainer.runtime_control import current
    control=current()
    if control is not None:
        if result.get('status')=='fallback':control.note=_FALLBACK_NOTE
        elif getattr(control,'note',None)==_FALLBACK_NOTE:control.note=None

_PRIVATE = {'source', 'raw_data', 'evidence', 'headers', 'authorization', 'credentials',
    'command', 'args', 'environment', 'env', 'device', 'endpoint', 'base_url', 'url',
    'capture', 'captured_at', 'observed_at', 'created_at', 'updated_at', 'timestamp',
    'bbox', 'roi', 'pixels', 'coordinates', 'connector',
    'reasoning_content','reasoning','chain_of_thought','raw_response'}
_PRIVATE_PARTS = ('password', 'passphrase', 'private_key', 'secret', 'token', 'api_key', 'apikey', 'path', 'screenshot',
    'image', 'sha256', 'adb', 'credential', 'command')
_PATH_OR_URL = re.compile(r'(?:[A-Za-z]:[\\/]|\\\\|https?://|Bearer\s+\S+|[^\s]+[\\/][^\s]+\.(?:json|jsonl|png|jpg|py|exe|dll|zip|yaml|toml))', re.I)


def clean_facts(value, depth=0):
    """Keep structured observations and unknowns; omit local/credential material."""
    if depth > 16:
        return None
    if isinstance(value, dict):
        return {str(k):clean_facts(v, depth+1) for k,v in value.items()
            if isinstance(k,str) and k.lower() not in _PRIVATE
            and not any(part in k.lower() for part in _PRIVATE_PARTS)}
    if isinstance(value, (list,tuple)):
        return [clean_facts(v, depth+1) for v in value[:200]]
    if isinstance(value,str):
        from desktop.larc_ai_status import safe_text
        return '[local detail omitted]' if _PATH_OR_URL.search(value) else safe_text(value,4000)
    if value is None or type(value) in (bool,int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    return None


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode('utf8')).hexdigest()


def current_larc_ai():
    return _CURRENT.get()


@contextmanager
def bind_larc_ai(runtime):
    token=_CURRENT.set(runtime)
    try:
        yield runtime
    finally:
        _CURRENT.reset(token)


class LarcAIPause(RuntimeError):
    code='larc_ai_pause'
    def __init__(self,result):
        self.result=deepcopy(result)
        super().__init__('larc_ai_pause:'+result.get('reason','decision_unavailable'))


class _ProviderFailure(RuntimeError):
    def __init__(self,code=None,timed_out=False):
        self.code=code
        self.timed_out=timed_out or code=='ai_request_timeout'
        super().__init__('provider_error')


class LarcAIRuntime:
    def __init__(self,run_id,options,client,log_path=None):
        options=dict(options or {})
        if not isinstance(run_id,str) or not run_id.strip():raise ValueError('larc_ai_run_id_required')
        self.run_id=run_id
        self.mode=options.get('mode','local')
        self.fallback=options.get('fallback','local')
        self.timeout_seconds=options.get('timeout_seconds',20)
        self.close_score_gap=options.get('close_score_gap',5)
        if self.mode not in {'local','critical','full'} or self.fallback not in {'local','pause'}:
            raise ValueError('larc_ai_options_invalid')
        for name,value,minimum,maximum in [('timeout_seconds',self.timeout_seconds,.01,120),
                ('close_score_gap',self.close_score_gap,0,1000)]:
            if type(value) not in (int,float) or not math.isfinite(value) or not minimum<=value<=maximum:
                raise ValueError('larc_ai_'+name+'_invalid')
        self.client=client
        self.log_path=Path(log_path) if log_path else None
        self._cache={}
        self._pending_training=None
        self._request_thread=None
        self._last_request_elapsed=0.
        self._publish()

    def _publish(self,payload=None,result=None,*,requesting=False):
        try:
            from desktop.larc_ai_status import publish_runtime
            publish_runtime(self.log_path,self.run_id,self.mode,payload,result,requesting=requesting)
        except Exception:pass  # Display state never controls a game decision.

    def _record(self,payload,result):
        self._publish(payload,result)
        if self.log_path is None:return
        try:
            self.log_path.parent.mkdir(parents=True,exist_ok=True)
            with self.log_path.open('a',encoding='utf8') as stream:
                stream.write(json.dumps({'node':payload['node'],'request':payload,'result':result},ensure_ascii=False)+'\n')
        except OSError:
            result['audit_write_failed']=True

    def _request(self,payload):
        self._last_request_elapsed=0.
        from core.auto_trainer.runtime_control import current
        control=current()
        if control is not None:control.checkpoint('ai_decision')
        # A misbehaving injected client cannot block the run indefinitely or
        # create a new background request on every frame while the old one hangs.
        if self._request_thread is not None and self._request_thread.is_alive():
            raise TimeoutError('previous_request_pending')
        replies=queue.Queue(maxsize=1)
        def call():
            try:replies.put((True,self.client.request_choice(deepcopy(payload),self.timeout_seconds)))
            except Exception as exc:
                code=getattr(exc,'code',None)
                if not isinstance(code,str) or not code.startswith('ai_') or re.fullmatch(r'[a-z_]{1,80}',code) is None:code=None
                replies.put((False,{'code':code,'timed_out':isinstance(exc,TimeoutError)}))
        self._request_thread=threading.Thread(target=call,daemon=True,name='larc-ai-choice')
        started=time.monotonic()
        self._publish(payload,requesting=True)
        self._request_thread.start()
        try:ok,value=replies.get(timeout=self.timeout_seconds)
        except queue.Empty:raise TimeoutError('request_timeout') from None
        finally:self._last_request_elapsed=round(max(0.,time.monotonic()-started),6)
        self._request_thread=None
        if not ok:
            raise _ProviderFailure(value['code'],value['timed_out'])
        return value

    def choose(self,node,candidates,local_action_id,context,critical_reasons=()):
        rows=[clean_facts(row) for row in candidates if isinstance(row,dict)]
        allowed=[row for row in rows if isinstance(row.get('action_id'),str)
            and row.get('allowed') is not False and row.get('eligible') is not False
            and not is_hard_rejected(row)]
        ids=[row['action_id'] for row in allowed]
        if len(ids)!=len(set(ids)) or local_action_id not in ids:
            raise ValueError('larc_ai_local_action_not_allowed')
        reasons=list(dict.fromkeys(str(r) for r in critical_reasons))
        scores=sorted([r['score'] for r in allowed if type(r.get('score')) in (int,float)
            and math.isfinite(r['score'])],reverse=True)
        if len(scores)>=2 and scores[0]-scores[1]<=self.close_score_gap:
            reasons.append('close_local_scores')
        if node in {'ss','ss_comparison'}:reasons.append('ss_choice')
        payload={'schema_version':'uma.larc_ai_choice.v1','node':str(node),
            'run_id':clean_facts(self.run_id),'context':clean_facts(context),'candidates':rows,
            'allowed_action_ids':ids,'local_action_id':local_action_id,
            'trigger_reasons':list(dict.fromkeys(reasons))}
        # The fingerprint excludes file locations/capture clocks but includes
        # actual facts, scores and eligibility. Never reuse across a changed turn.
        key=_digest(payload);payload['decision_id']=key
        if key in self._cache:
            saved=deepcopy(self._cache[key]);saved['cached']=True
            if saved.get('status')=='pause':raise LarcAIPause(saved)
            self._publish(payload,saved)
            _show_result(saved)
            return saved
        result={'decision_id':key,'action_id':local_action_id,'reason':'local_mode',
            'status':'local','trigger_reasons':payload['trigger_reasons'],'cached':False,'elapsed_seconds':0.}
        if self.mode=='local' or self.mode=='critical' and not reasons:
            if self.mode=='critical':result['reason']='not_a_critical_node'
        else:
            error=None
            try:
                response=self._request(payload)
                if (not isinstance(response,dict) or response.get('decision_id')!=key
                        or response.get('action_id') not in ids or not isinstance(response.get('reason'),str)
                        or not response['reason'].strip()):
                    error='invalid_response'
                    result['provider_error_code']='ai_response_invalid'
                else:
                    result.update(status='ai',action_id=response['action_id'],reason=clean_facts(response['reason'])[:1000])
            except _ProviderFailure as exc:
                error='request_timeout' if exc.timed_out else 'provider_unavailable'
                if exc.code:result['provider_error_code']=exc.code
            except TimeoutError:
                error='request_timeout';result['provider_error_code']='ai_request_timeout'
            except Exception:error='provider_unavailable'
            finally:result['elapsed_seconds']=self._last_request_elapsed
            if error:
                result.update(status='pause' if self.fallback=='pause' else 'fallback',reason=error)
                if self.fallback=='pause':result['action_id']=None
        self._cache[key]=deepcopy(result)
        if len(self._cache)>256:self._cache.pop(next(iter(self._cache)))
        self._record(payload,result)
        if result['status']=='pause':raise LarcAIPause(result)
        _show_result(result)
        return result

    def remember_training(self,state,goal,result,preview):
        self._pending_training=(_digest(training_context(state,goal)),deepcopy(result),_digest(_preview_facts(preview)))

    def take_training(self,state,goal,candidates):
        pending=self._pending_training;self._pending_training=None
        if not pending or pending[0]!=_digest(training_context(state,goal)):return None
        observed=state.scenario_state.get('larc_ss_preview')
        if not isinstance(observed,dict) or pending[2]!=_digest(_preview_facts(observed)):return None
        allowed={row['action_id'] for row in candidates if _execution_allowed(row)}
        if pending[1].get('action_id') not in allowed:return None
        return {**pending[1],'cached':True,'handoff':'ss_comparison'}


def training_context(state,goal):
    from core.auto_trainer.career_review_export import _training
    raw=state.to_dict()
    scenario={k:v for k,v in (raw.get('scenario_state') or {}).items()
        if k not in {'larc_ss_entry','larc_ss_preview'}}
    # Preview evidence is supplied separately at the SS hook. Main-page facts
    # bind a non-SS choice to the later training executor on this same turn.
    raw['scenario_state']=scenario
    # Use the same bounded game-fact projection as exported reviews before
    # removing raw capture internals. Preserve per-person bond/charge/reward
    # facts and raw-data observation counts, not just aggregated scores.
    observations=[_training(option) for option in raw.get('training_options',[]) if isinstance(option,dict)]
    return clean_facts({'state':raw,'training_observations':observations,'goal':goal.to_dict()})


def _preview_facts(preview):
    # Capture/confirmation wrappers add comparisons and source file names;
    # they are not new game facts and must not cause duplicate API calls.
    return clean_facts({key:preview[key] for key in ('status','stage','kind','kind_basis','ready_count',
        'stats_gain','supporter_points_if_all_win','rewards','unknown_fields','win_policy','participants_observation')
        if key in preview})


def _execution_allowed(row):
    # Missing verdicts and explicit denials cannot create execution authority.
    verdict = row.get('hard_rejected', row.get('rejected'))
    return (verdict is False and row.get('allowed') is not False
            and row.get('eligible') is not False and not is_hard_rejected(row))


def execution_candidate(row,state,goal):
    """Return only action contracts supported by the existing executor."""
    row=deepcopy(row);action_id=row.get('action_id')
    if not isinstance(action_id,str):return None
    if action_id.startswith('train:'):
        target=action_id.split(':',1)[1]
        candidate=next((item for item in state.training_options if item.training_type==target),None)
        if candidate is None:return None
        row.update(action='train',target=target)
        from core.auto_trainer.larc_opening import LarcOpeningStrategy
        rate = candidate.failure_rate
        known_rate = type(rate) in (int, float) and math.isfinite(rate) and 0 <= rate <= 100
        if ((known_rate and rate > LarcOpeningStrategy.failure_limit(goal))
                or (not known_rate and row.get('hard_rejected') is not False)):
            row.update(rejected=True, hard_rejected=True)
    elif action_id=='rest.normal':row.update(action='rest',target=None)
    elif row.get('action')=='outing':
        from core.auto_trainer.larc_friend_outing import bound_outing_packet
        packet=bound_outing_packet(state)
        if row.get('target')=='normal':
            from core.auto_trainer.larc_charge_unlock import charge_unlock_window
            if packet is not None or not charge_unlock_window(state,goal):row.update(rejected=True,hard_rejected=True)
        elif packet is None or row.get('target')!=f"satake_mei:{packet['completed']+1}":row.update(rejected=True,hard_rejected=True)
    else:return None
    row['allowed']=_execution_allowed(row)
    return row


def training_candidates(state,goal,decision,require_primary_gain=False):
    rows=[]
    for raw in decision.get('scored_candidates') or []:
        row=execution_candidate(raw,state,goal)
        if row is None:continue
        if require_primary_gain and row.get('action')=='train':
            from core.auto_trainer.live_loop import UGParentTrainingLoop
            if not UGParentTrainingLoop._selected_primary_gain_is_reliable(state,row['target']):
                row.update(rejected=True,hard_rejected=True,allowed=False)
        rows.append(row)
    return rows


def action_id_for(decision,rows):
    return next((row['action_id'] for row in rows if row.get('action')==decision.get('action')
        and row.get('target')==decision.get('target')),None)


def candidate_facts(rows):
    """Bound training/SS explanations to the established review projection.

    Executable rows remain untouched in the caller; only the sent copy drops
    expanded forecast trees and duplicate planning evidence.
    """
    from core.auto_trainer.career_review_export import _score
    result=[]
    for row in rows:
        item=_score(row)
        for key in ('action_id','action','target','allowed','rejected','eligible','hard_rejected','soft_risk',
                'risk_flags','failure_rate','failure_rate_limit','source_confidence','rejection_reasons'):
            if key in row:item[key]=deepcopy(row[key])
        result.append(clean_facts(item))
    return result


def _selected_decision(decision,row,outcome):
    result=deepcopy(decision)
    label='AI候选取舍：' if outcome['status']=='ai' else '沿用 SS 比较后的本地取舍：'
    result.update(action=row['action'],target=row.get('target'),reason=label+outcome['reason'])
    if type(row.get('source_confidence')) in (int,float):result['confidence']=row['source_confidence']
    result['risk_flags']=list(row.get('risk_flags') or [])
    result['larc_ai']=deepcopy(outcome)
    return result


def apply_training_ai(state,goal,decision,*,require_primary_gain=False):
    runtime=current_larc_ai()
    if runtime is None or runtime.mode=='local' or goal.scenario!='larc':return decision
    if decision.get('action') not in {'train','rest','outing'}:return decision
    rows=training_candidates(state,goal,decision,require_primary_gain)
    local=action_id_for(decision,rows)
    if not any(row['action_id']==local and row['allowed'] for row in rows):return decision
    outcome=runtime.take_training(state,goal,rows) or runtime.choose('training',candidate_facts(rows),local,training_context(state,goal))
    chosen=next(row for row in rows if row['action_id']==outcome['action_id'] and row['allowed'])
    if outcome['status'] in {'local','fallback'} and outcome.get('handoff') is None:
        return {**decision,'larc_ai':outcome}
    return _selected_decision(decision,chosen,outcome)


def apply_ss_ai(state,goal,preview,comparison,*,require_primary_gain=False):
    from core.auto_trainer.larc_ss_deadline_execution import current_deadline_evidence,requires_ss_observation
    deadline_evidence=current_deadline_evidence(state,goal,preview)
    deadline=deadline_evidence['status']
    if requires_ss_observation(deadline):
        return {**comparison,'selected':deadline_evidence['executable'],
            'reason':'user_full_group_ss_deadline'if deadline_evidence['executable']else'user_full_group_ss_deadline_blocked',
            'local_action':'stop','local_target':None,'ss_deadline':deadline}
    runtime=current_larc_ai()
    if runtime is None or runtime.mode=='local':return comparison
    if preview.get('status')!='accepted' or preview.get('stage')!='preview':return comparison
    decision={'action':comparison.get('local_action'),'target':comparison.get('local_target'),
        'scored_candidates':comparison.get('training_candidates') or []}
    rows=training_candidates(state,goal,decision,require_primary_gain)
    # A computed final score exists only after current SS eligibility checks.
    score=comparison.get('score')
    if comparison.get('selected') is True or type(score) in (int,float) and math.isfinite(score):
        rows.append({'action_id':'ss.confirm','action':'ss','target':None,'score':score,
            'components':comparison.get('components',{}),'allowed':True,'rejected':False})
    local='ss.confirm' if comparison.get('selected') else action_id_for(decision,rows)
    if not any(row['action_id']==local and row['allowed'] for row in rows):return comparison
    outcome=runtime.choose('ss',candidate_facts(rows),local,{**training_context(state,goal),'ss_preview':_preview_facts(preview)},critical_reasons=['ss_or_sss_choice'])
    result={**comparison,'selected':outcome['action_id']=='ss.confirm','larc_ai':outcome}
    if outcome['status']=='ai':result['reason']='ai_ss_candidate_choice'
    if not result['selected']:
        runtime.remember_training(state,goal,outcome,preview)
        chosen=next(row for row in rows if row['action_id']==outcome['action_id'])
        result.update(local_action=chosen['action'],local_target=chosen.get('target'))
    return result
