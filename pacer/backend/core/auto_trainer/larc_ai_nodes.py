"""AI choices at existing adaptation/race boundaries; no new game executor."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path

from core.auto_trainer.larc_ai_runtime import LarcAIRuntime, LarcAIPause, bind_larc_ai, current_larc_ai


@contextmanager
def run_ai_scope(args):
    if getattr(args,'run_root',None) is None:
        with bind_larc_ai(None):yield None
        return
    root=Path(args.run_root).resolve()
    path=root/'session/ai_decision.json'
    if not path.is_file():
        with bind_larc_ai(None):yield None
        return
    from core.auto_trainer.larc_ai_options import normalize,feature_available
    saved=json.loads(path.read_text(encoding='utf8'))
    # An explicitly local snapshot never uses AI state or credentials. Old AI
    # metadata must not stop ordinary local play. Do not infer local mode from
    # unreadable/missing options in a foreign snapshot, however.
    raw_options=saved.get('options') if isinstance(saved,dict) else None
    if isinstance(raw_options,dict) and raw_options.get('mode')=='local':
        with bind_larc_ai(None):yield None
        return
    if (not isinstance(saved,dict) or saved.get('schema_version')!='uma.larc_ai_decision.v1'
            or saved.get('run_id')!=root.name):
        raise ValueError('larc_ai_run_mismatch')
    options=normalize(saved.get('options'))
    if options['mode']=='local' or not getattr(args,'execute',False):
        with bind_larc_ai(None):yield None
        return
    if not feature_available():raise ValueError('本版本未启用AI决策，请使用支持AI的测试版。')
    # Tests can inject a fake client; production credentials remain solely in
    # the installation's encrypted configuration, never the run snapshot.
    client=getattr(args,'_ai_client',None)
    if client is None:
        from desktop.larc_ai_client import create_client,provider_status
        installation=Path(__file__).resolve().parents[2]
        status=provider_status(installation)
        if not status.get('configured'):raise ValueError(status.get('message') or '请先保存AI接口配置。')
        connection=saved.get('connection') or {}
        if any(connection.get(key)!=status.get(key) for key in ('base_url','model')):
            raise ValueError('AI接口或模型已改变，请恢复本局启动时的接口设置后再继续。')
        client=create_client(installation)
    runtime=LarcAIRuntime(root.name,options,client,root/'session/ai_decisions.jsonl')
    with bind_larc_ai(runtime):yield runtime


def growth_choice(ctx,period,result,*,state=None,goal=None,race_day=False):
    runtime=current_larc_ai()
    if runtime is None or runtime.mode=='local' or result.get('ai_choice') is not None:return result
    grid=result.get('grid') or {};recommendation=result.get('recommendation') or {}
    if (not result.get('ok') or grid.get('errors') or grid.get('pending_cost')!=0
            or type(grid.get('points')) is not int):return result
    from core.auto_trainer.larc_adaptation import build_adaptation_plan
    available={}
    workflow_restricted=(recommendation.get('workflow_adaptation') or {}).get('restricted') is True
    considered=list(recommendation.get('workflow_candidates' if workflow_restricted else 'considered') or [])
    if recommendation.get('phase') and not considered and not workflow_restricted:considered.append(recommendation)
    for row in considered:
        phase=row.get('phase')
        if (not isinstance(phase,str) or row.get('rejected') is True
                or (row.get('budget') or {}).get('affordable_at_deadline') is False):continue
        try:plan=build_adaptation_plan(grid,phase=phase)
        except (ValueError,TypeError,KeyError):continue
        if plan.get('cost')!=row.get('cost') or plan['cost']>grid['points']:continue
        old=available.get(phase)
        if old is None or (row.get('score') or 0)>(old.get('score') or 0):available[phase]=row
    if not available:return result
    rows=[{'action_id':'adaptation.keep','allowed':True,'rejected':False,
        'score':recommendation.get('baseline_score'),'description':'暂不购买，保留适应性积分'}]
    fields=('phase','cost','training','score','estimated_gains','after_action_id','purchase_phases','budget',
        'point_opportunity_cost','future_value_change','repeat_training_share_estimate','assumptions')
    rows.extend({**{key:row[key] for key in fields if key in row},
        'action_id':'adaptation.'+phase,'allowed':True,'rejected':False}
        for phase,row in available.items())
    local_phase=recommendation.get('phase')
    if local_phase is not None and local_phase not in available:return result
    local='adaptation.'+local_phase if local_phase else 'adaptation.keep'
    from core.auto_trainer.career_review_export import _state
    context={'turn':ctx.progress['next_turn'],'period':period,'grid':grid,'race_day':race_day,
        'state':_state(state.to_dict()) if state is not None else None,
        'goal':goal.to_dict() if goal is not None else None,
        'purchase_effects':'estimated; reobserve training after actual purchase',
        'constraints':'existing balance, locks, deadline budgets and purchase execution remain binding',
        'workflow_adaptation':recommendation.get('workflow_adaptation')}
    choice=runtime.choose('adaptation',rows,local,context,critical_reasons=['overseas_adaptation_purchase'])
    updated=deepcopy(result);updated['ai_choice']=choice
    if choice['status'] in {'local','fallback'}:return updated
    phase=choice['action_id'].removeprefix('adaptation.')
    if phase=='keep':
        updated['recommendation']={**recommendation,'phase':None,'cost':0,'reason':'ai_keep_adaptation_points'}
    else:
        selected=available[phase]
        updated['recommendation']={**recommendation,**selected,'phase':phase,
            'reason':'ai_adaptation_candidate_choice','points_after_purchase':grid['points']-selected['cost']}
    if workflow_restricted:
        from core.auto_trainer.larc_workflow_adaptation import record_selection
        if phase=='keep':
            updated['recommendation'].update(training=None,purchase_phases=[],points_after_purchase=grid['points'],
                budget={'purchase_selected':False})
            updated['recommendation'].pop('after_action_id',None)
            updated['recommendation'].pop('score',None)
        record_selection(updated['recommendation'], None if phase=='keep' else available[phase])
    return updated


def checkpoint_choice(ctx,node,context,*,reason):
    """Required progression may have only one legal action; expose that honestly."""
    runtime=current_larc_ai()
    if runtime is None or runtime.mode=='local':return None
    choice=runtime.choose(node,[{'action_id':'continue.configured','allowed':True,'rejected':False,
        'description':'按已配置的准备和必跑流程继续；本节点没有可选比赛或任意购买权限'}],
        'continue.configured',{'turn':ctx.progress['next_turn'],**context,
            'decision_scope':'single supported continuation; assessment does not invent extra actions'},critical_reasons=[reason])
    return choice


def expedition_checkpoint(ctx,provider):
    period=provider.get('larc_period') or {};date=period.get('value') or {}
    if (period.get('status')!='verified' or date.get('year_index') not in {2,3}
            or date.get('month')!=6 or date.get('half')!='second'):return None
    return checkpoint_choice(ctx,'before_expedition',{'period':period,
        'state':provider.get('state_context') or {}},reason='before_overseas_expedition')


def pause_summary(control,summary,error,save):
    """Wait at an unconsumed boundary and reattach after the user's resume."""
    result=getattr(error,'result',{})
    summary.update(status='paused',stop_reason='larc_ai_pause',ai_pause=result)
    save()
    if control is None:
        summary['status']='stopped';return
    from core.auto_trainer.runtime_control import ResumeFromCurrentPage,RunStopped
    control.note='AI暂不可用，已暂停保留；恢复后重新观察当前页面。'
    control.restart_on_resume=True
    if control.pause_steps is not None:
        control.pause_steps=frozenset((*control.pause_steps,'larc_ai_pause'))
    control.request('pause')
    try:
        control.checkpoint('larc_ai_pause')
        control.note=None
        summary.update(status='reattach',stop_reason='player_resume_current_page')
    except ResumeFromCurrentPage:
        control.note=None
        summary.update(status='reattach',stop_reason='player_resume_current_page')
    except RunStopped as stopped:
        summary.update(status='stopped',stop_reason=stopped.reason,stopped_by_control=True)
