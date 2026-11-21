"""Pure same-turn main-action handoff; never buys or writes receipts."""
from core.auto_trainer.rejection_policy import is_hard_rejected
from copy import deepcopy
from dataclasses import replace

KEY='larc_career_joint_intent'
SCHEMA='larc.current_joint_intent.v1'
TABS=('speed','stamina','power','guts','wisdom')

def source(state):
    pages=list(getattr(state,'training_options',())or())
    if len(pages)!=5 or {p.training_type for p in pages}!=set(TABS):return None
    captures={p.training_type:(p.raw_data or{}).get('capture')or{}for p in pages}
    runs={c.get('run_session_id')for c in captures.values()}
    if len(runs)!=1 or not next(iter(runs)):return None
    if any(c.get('turn_index')!=state.turn or not isinstance(c.get('sha256'),str)
           or not c['sha256']for c in captures.values()):return None
    return {'run_session_id':next(iter(runs)),'turn_index':state.turn,
        'page_sha256':{tab:captures[tab]['sha256']for tab in TABS},
        'source_kinds':{tab:captures[tab].get('source_kind')for tab in TABS}}

def proposed(state,goal,grid,result):
    bound=source(state)
    if bound is None:return None
    best=result['selected']
    return {'schema_version':SCHEMA,'status':'proposed','source':bound,'goal':deepcopy(goal.to_dict()),
        'calendar':result.get('calendar'),
        'action_id':best['action_id'],'phase':best['phase'],'cost':best['cost'],
        'levels_before':list(grid['levels']),'targets':list(best['targets']),'cash_before':grid['points'],
        'score':best['score'],'selection_reason':result.get('selection_reason',
            'full_remaining_career_training_and_paid_purchase_comparison'),
        'execution_authorized':False,'payment_claimed':False}

def same_recommendation(packet,recommendation):
    return (isinstance(packet,dict) and packet.get('schema_version')==SCHEMA
        and packet.get('action_id')==recommendation.get('after_action_id')
        and packet.get('phase')==recommendation.get('phase')
        and packet.get('cost')==recommendation.get('cost',0)
        and packet.get('targets')==recommendation.get('level_targets'))

def no_buy_context(recommendation):
    packet=(recommendation.get('career_joint')or{}).get('main_intent')
    if not same_recommendation(packet,recommendation) or packet['phase']is not None or packet['cost']!=0:
        return None
    return {**deepcopy(packet),'status':'same_capture_no_buy','payment_claimed':False}

def settled_context(recommendation, *, run_id,turn,receipt,receipt_path,verified):
    packet=(recommendation.get('career_joint')or{}).get('main_intent')
    if (verified is not True or not same_recommendation(packet,recommendation)
        or packet['phase']is None or packet['cost']<=0 or not isinstance(receipt,dict)):
        return None
    bound=packet['source']
    if bound.get('run_session_id')!=run_id or bound.get('turn_index')!=turn:return None
    levels=receipt.get('levels');after=receipt.get('after_points')
    if levels!=packet['targets'] or type(after)is not int or after!=packet['cash_before']-packet['cost']:
        return None
    return {**deepcopy(packet),'status':'verified_purchase_same_turn',
        'payment_claimed':True,'receipt_path':str(receipt_path),
        'receipt_levels':list(levels),'receipt_after_points':after,
        'receipt_verification_owner':'original_growth_adaptation_executor'}

def attach_preparation(state,state_context,preparation,goal):
    """Share a same-capture NoBuy selection through the existing context path."""
    from .dispatch import enabled
    if not enabled(goal)or not isinstance(preparation,dict)or preparation.get('ok')is not True:
        return False
    packet=no_buy_context(preparation.get('recommendation')or{})
    if packet is None or source(state)!=packet.get('source')or packet.get('goal')!=goal.to_dict():return False
    state.scenario_state[KEY]=deepcopy(packet)
    state_context.setdefault('scenario_state',{})[KEY]=deepcopy(packet)
    return True

def inject_settled_marker(marker,state_context,*,run_id,turn,goal):
    """Receipt-derived packet only; ordinary capture will recheck eligibility."""
    from .dispatch import enabled
    if not enabled(goal)or not isinstance(marker,dict)or marker.get('status')!='verified_purchase_same_turn':return False
    bound=marker.get('source')or{}
    if (marker.get('schema_version')!=SCHEMA or bound.get('run_session_id')!=run_id
        or bound.get('turn_index')!=turn or state_context.get('turn')!=turn
        or marker.get('goal')!=goal.to_dict()):return False
    state_context.setdefault('scenario_state',{})[KEY]=deepcopy(marker)
    return True

def _current_resources_match(state,current,packet):
    """Known current facts may disprove an old intent; unknown facts add no gate."""
    context=getattr(state,'scenario_state',{})or{}
    run=current['run_session_id'];turn=current['turn_index']
    expected_points=packet['receipt_after_points']
    growth=context.get('larc_growth_observation')or{}
    if isinstance(growth,dict) and growth.get('status')=='accepted' and growth.get('turn_index')==turn:
        source=growth.get('source')or{}
        frame=growth.get('source_frame_sha256')
        if (isinstance(source,dict) and source.get('run_session_id')==run
                and source.get('turn_index')==turn and isinstance(frame,str) and frame
                and source.get('sha256')==frame and growth.get('run_session_id',run)==run):
            points=growth.get('adaptation_points')
            if type(points)is int and points>=0 and points!=expected_points:return False
    # The ordinary owned-level loader rewrites through_turn even when its
    # cache predates this purchase. It has no capture generation, so neither
    # its balance_current flag nor its levels can disprove a newer receipt.
    # Do not turn that historical cache into a fresh grid observation.
    return True


def _same_calendar(state,packet):
    quoted=packet.get('calendar')
    period=(getattr(state,'scenario_state',{})or{}).get('larc_period')or{}
    if type(quoted)is not int or not isinstance(period,dict) or period.get('status')!='verified':return True
    from core.auto_trainer.larc_calendar import period_ordinal
    try:current=period_ordinal(period.get('value')or{})
    except(TypeError,ValueError,KeyError,AttributeError):current=None
    return type(current)is not int or current==quoted


def apply_local(state,goal,result):
    from .dispatch import enabled,DEPTH
    if DEPTH.get() or not enabled(goal) or getattr(result,'action',None)not in('train','rest'):
        return result
    packet=(state.scenario_state or{}).get(KEY)
    if not isinstance(packet,dict) or packet.get('schema_version')!=SCHEMA:return result
    current=source(state);old=packet.get('source')or{}
    if (current is None or current['run_session_id']!=old.get('run_session_id')
        or current['turn_index']!=old.get('turn_index') or packet.get('goal')!=goal.to_dict()):return result
    status=packet.get('status')
    if status=='same_capture_no_buy':
        if packet.get('cost')!=0 or packet.get('phase')is not None or current['page_sha256']!=old.get('page_sha256'):
            return result
    elif status=='verified_purchase_same_turn':
        if (packet.get('payment_claimed')is not True or packet.get('receipt_levels')!=packet.get('targets')
            or packet.get('receipt_after_points')!=packet.get('cash_before',0)-packet.get('cost',0)):
            return result
        if not _current_resources_match(state,current,packet) or not _same_calendar(state,packet):return result
    else:return result
    aid=packet.get('action_id')
    row=next((r for r in result.scored_candidates if r.get('action_id')==aid),None)
    if not row or is_hard_rejected(row) or row.get('allowed') is False:return result
    if aid=='rest.normal':action,target='rest',None
    elif isinstance(aid,str)and aid.startswith('train:')and aid.split(':',1)[1]in TABS:
        from core.auto_trainer.larc_opening import LarcOpeningStrategy
        candidate=next((c for c in state.training_options if c.action_id==aid),None)
        if candidate is None or type(candidate.failure_rate)not in(int,float) or not 0<=candidate.failure_rate<=LarcOpeningStrategy.failure_limit(goal):return result
        action,target='train',aid.split(':',1)[1]
    else:return result
    return replace(result,action=action,target=target,
        confidence=row.get('source_confidence',result.confidence),risk_flags=list(row.get('risk_flags')or[]),
        reason='Current action selected with full remaining career purchase value')
