"""Exact revision gate and quote recursion boundary for the static core."""
from contextvars import ContextVar
from copy import deepcopy
from .quote import Quote
from .engine import Unsupported
from . import binding

DEPTH=ContextVar('larc_joint_career_quote_depth',default=0)

def enabled(goal):
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    return unified_larc_enabled(goal)

def existing_income(candidate,state):
    """Use bound AP or existing182 head-count reference; never wallet credit."""
    from core.auto_trainer.larc_adaptation_strategy import training_income
    amount,basis=training_income(candidate)
    capture=(candidate.raw_data or{}).get('capture')or{}
    kind=str(capture.get('source_kind','')).lower()
    model=any(capture.get(k)for k in('simulation','simulation_truth','hypothetical','projected')) or (
        capture.get('underlying_source_kind')=='simulation' or 'model' in kind or 'simulation' in kind)
    if basis=='observed_training_preview':
        return {'amount':amount,'basis':basis if not model else 'declared_model_training_preview',
            'observed':not model,'source_kind':capture.get('source_kind'),'not_current_cash':True}
    if not model:
        from core.auto_trainer.larc_tail263.strategy182 import conditional_income_quote
        period=(state.scenario_state.get('larc_period')or{}).get('value')or{}
        quote=conditional_income_quote(candidate,period,run_id=capture.get('run_session_id'),turn=state.turn)
        if quote is not None:return {**quote,'amount':quote['conditional_amount']}
    return {'amount':amount,'basis':basis,'observed':False,'not_current_cash':True}

def recommend(observation,period,state,goal,legacy,*,parent,support_profile=None,race_day=False):
    from core.auto_trainer import larc_purchase_v3 as purchase
    kwargs={'support_profile':support_profile,'race_day':race_day}
    now=purchase.period_ordinal((period or{}).get('value')or{})
    if (not enabled(goal)or DEPTH.get()or race_day or state is None or now not in(36,37,38,39,41)
        or not isinstance(observation,dict)or observation.get('pending_cost')!=0):
        return parent(observation,period,state,goal,legacy,**kwargs)
    token=DEPTH.set(DEPTH.get()+1)
    try:
        from core.auto_trainer.larc_opening import LarcOpeningStrategy
        from core.auto_trainer.larc_return_budget import purchase_scope
        with purchase_scope():
            result=Quote(purchase).recommend(state,goal,observation,period,legacy,
                lambda s,g:LarcOpeningStrategy(g).evaluate(s).scored_candidates,
                support_profile=support_profile,income_resolver=existing_income)
        best=result['selected'];packet=binding.proposed(state,goal,observation,result)
        if packet is None:raise Unsupported('normal_current_source_binding_unavailable')
        output={**legacy,'phase':best['phase'],'cost':best['cost'],'purchase_phases':best['phases'],
            'after_action_id':best['action_id'],'training':best['action_id'].split(':',1)[1]if best['action_id'].startswith('train:')else None,
            'score':best['score'],'baseline_score':result['NoBuy']['score'],'baseline_action':result['NoBuy']['action_id'],
            'level_targets':best['targets'],'points_after_purchase':observation['points']-best['cost'],
            'policy_kind':'joint_full_career_action_and_purchase',
            'reason':result.get('selection_reason','full_career_joint_value_preferred'),
            'language_debuff_deferred':result['language_debuff_deferred'],
            'current_training_purchase_coupling':{'enabled':True,'active':bool(best['phase']),'allowed':True,
                'reason':'same_action_selected_by_full_career_comparison','planned_main_action_id':best['action_id']},
            'career_joint':{'applied':True,'main_intent':packet,'selected':best,'ranking':result['ranking'],
                'budget':result['budget'],'future_training_days':result['future_training_days'],
                'future_fixed_reference_approximation':True,'execution_authorized':False}}
        if 'first_arc_reserve' in result:
            output['career_joint']['first_arc_reserve'] = result['first_arc_reserve']
        if 'continuation_reference' in result:
            output['career_joint']['continuation_reference'] = result['continuation_reference']
        if 'w_entry_wisdom_700' in result:
            output['w_entry_wisdom_700'] = result['w_entry_wisdom_700']
        if result.get('power_debuff_deferred'):
            output['career_joint']['power_deferral'] = {'slot':2,'cost':100,
                'due_ordinal':42,'deferred_through_niel':True,'forecast_only':True}
        if result['language_debuff_deferred']:
            output['language_deferral_basis']={'kind':'permanent_upgrade_budget_competition','slot':4,
                'deferred_cost':purchase.LEVEL_TWO_COSTS[4],'points_current':observation['points'],
                'selected_ordinal':now,'exclusive_end_ordinal':43,'scope':'first_expedition_language_only',
                'other_unresolved_hazards_allowed':False,'race_performance_effect':'not_quantified; explicit_user_tradeoff'}
        return output
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,ArithmeticError)as error:
        result=parent(observation,period,state,goal,legacy,**kwargs)
        return {**result,'career_joint':{'applied':False,'whole_quote_fallback':str(error),
            'execution_authorized':False}}
    finally:DEPTH.reset(token)
