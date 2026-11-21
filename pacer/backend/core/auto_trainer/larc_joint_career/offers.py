"""Pure all-current firstcamp offers; no old current-max purchase gate.

Caller supplies the already-scored full current pool and original pure helper
functions. The builder does not rank, buy, create pending state or call an Actor.
It is deliberately firstcamp/current-HP-invariant only; Unsupported means the
caller must retain its complete parent policy, not mix a partial offer pool.
Original current energy, risk loss and aversion stay in the current score;
only the additional purchase numeric uplift is weighted by current success.
"""
from copy import copy,deepcopy
from dataclasses import is_dataclass,replace
from itertools import combinations
from math import isfinite

SCHEMA='larc.firstcamp_current_offers.v1'
FIRSTCAMP_CALENDARS=(36,37,38,39,41)
STATS=('speed','stamina','power','guts','wisdom')
FACILITIES={'guts':('guts_expedition',0),'stamina':('stamina_expedition',1),
            'power':('power_expedition',2),'speed':('speed_expedition',3),'wisdom':('french_three',4)}
GLOBALS=(('expedition_skill_pt',5),('courage_three',6),('willpower_three',7))
REQUIRED_DEPS=('period_ordinal','build_adaptation_plan','growth_bundle_phase',
               'gains_after_purchase','gain_value','training_income')


class Unsupported(ValueError):pass


def _number(x):return type(x)in(int,float)and isfinite(x)


def _required(ok,reason):
    if not ok:raise Unsupported(reason)


def _workflow(row):
    workflow=row.get('score_workflow')or{}
    tuning=row.get('score_tuning')or{}
    _required(isinstance(workflow,dict)and isinstance(tuning,dict),'current_workflow_metadata_invalid')
    _required(workflow.get('priority_rank',0)==0
              and workflow.get('status')not in('matched','applied')
              and (tuning.get('status')!='applied' or tuning.get('mode')=='detailed'),
              'active_custom_workflow_requires_parent')


def _tuned_gain_score(gain_result,goal,row):
    score=_score(gain_result)
    tuning=(row or{}).get('score_tuning')or{}
    if tuning.get('status')=='applied'and tuning.get('mode')=='detailed':
        from core.auto_trainer.larc_score_tuning import settings as tuning_settings
        factors=tuning_settings(goal).get('factors')or{}
        parts=gain_result.get('components')or{}
        score+=sum(parts.get(k,0.0)*(factor-1.0)
                   for k,factor in factors.items() if k in parts)
    return score


def _score(result):
    _required(isinstance(result,dict)and _number(result.get('score')),'original_numeric_value_required')
    return result['score']


def _income(candidate,action,deps):
    if action=='rest.normal':
        return{'conditional_amount':100,'basis':'existing_overseas_rest_reference_rule',
               'observed':False,'conditional_on_action_completion':True,
               'not_current_cash':True,'not_a_payment_receipt':True}
    result=deps['training_income'](candidate)
    _required(isinstance(result,(tuple,list))and len(result)==2
              and type(result[0])is int and result[0]>=0
              and isinstance(result[1],str)and bool(result[1]),'original_training_income_required')
    return{'conditional_amount':result[0],'basis':result[1],
           'observed':result[1]=='observed_training_preview',
           'conditional_on_training_success':True,'not_current_cash':True,'not_a_payment_receipt':True}


def _projection_state(state,run,turn,owned,cash):
    # Same quote-local shop binding used by original recommend_growth. It is
    # not a new observation and does not write state, context files or wallet.
    scene={**(state.scenario_state or{}),'larc_purchase_model_v3':True,
           'larc_expedition_context':{'status':'accepted','run_session_id':run,
               'through_turn':turn,'levels':owned[:],'points':cash,'balance_current':True}}
    if is_dataclass(state):return replace(state,scenario_state=scene)
    try:
        duplicate=copy(state);duplicate.scenario_state=scene;return duplicate
    except (AttributeError,TypeError)as exc:
        raise Unsupported('quote_local_shop_state_binding_unavailable')from exc


def _build_offers(state,goal,observation,current_rows,*,deps,calendars):
    """Build all five train/rest NoBuy plus payable eligible train baskets.

    deps are original period_ordinal, build_adaptation_plan(observation,phase=),
    growth_bundle_phase(phases), gains_after_purchase(state,candidate,phase,
    targets=,purchase_v3=True), gain_value(state,gains,goal), training_income(c).

    direct_score = original row.score + (1-current_p)*purchase_uplift.
    Current vitality and energy-continuity components remain inside row.score.
    Other original components, preferences, aversion and warning flags survive.
    This function does not reproduce old future funding/current-max gates;
    the common planner prices the future and original execution owns payment.
    """
    _required(isinstance(deps,dict)and all(callable(deps.get(k))for k in REQUIRED_DEPS),
              'original_pure_dependencies_required')
    _required(getattr(state,'screen',None)=='training_menu'and getattr(goal,'scenario',None)=='larc',
              'current_firstcamp_training_state_required')
    scene=getattr(state,'scenario_state',{})or{};period=scene.get('larc_period')or{}
    _required(isinstance(calendars,(tuple,list))and bool(calendars)
              and all(type(x)is int for x in calendars)and len(set(calendars))==len(calendars)
              and set(calendars)<=set(FIRSTCAMP_CALENDARS),'supported_firstcamp_calendar_scope_required')
    now=deps['period_ordinal'](period.get('value')or{})
    _required(period.get('status')=='verified'and now in calendars,'current_firstcamp_free_calendar_required')
    _required(not getattr(state,'errors',None),'current_state_errors_preserved')
    candidates=list(getattr(state,'training_options',())or())
    _required(len(candidates)==5 and {c.training_type for c in candidates}==set(STATS),
              'all_five_current_training_candidates_required')
    turn=getattr(state,'turn',None)
    captures=[(getattr(c,'raw_data',{})or{}).get('capture')or{}for c in candidates]
    runs={c.get('run_session_id')for c in captures}
    _required(type(turn)is int and len(runs)==1 and next(iter(runs))
              and all(c.get('turn_index')==turn for c in captures),'current_five_page_run_turn_binding_required')
    run=next(iter(runs))
    _required(isinstance(observation,dict)and not observation.get('errors')
              and observation.get('pending_cost')==0,'current_grid_without_pending_required')
    owned=observation.get('levels');cash=observation.get('points')
    _required(isinstance(owned,list)and len(owned)==10 and all(type(x)is int and 0<=x<=3 for x in owned)
              and type(cash)is int and cash>=0,'current_owned_and_spendable_cash_required')
    _required(isinstance(current_rows,(list,tuple))and all(isinstance(r,dict)for r in current_rows),
              'complete_original_current_rows_required')
    byid={}
    for row in current_rows:
        aid=row.get('action_id')
        _required(isinstance(aid,str)and aid not in byid,'unique_original_current_action_ids_required')
        byid[aid]=row
    expected={'train:'+tab for tab in STATS}|{'rest.normal'}
    _required(expected<=set(byid),'all_five_training_and_rest_original_rows_required')
    for aid,row in byid.items():
        if aid not in expected:
            _required(row.get('rejected')is True,'other_eligible_current_action_requires_parent:'+aid)
    lookup={c.action_id:c for c in candidates}
    _required(set(lookup)==expected-{'rest.normal'},'Pacer_training_action_id_mapping_required')
    source={'run_session_id':run,'turn_index':turn,'calendar_ordinal':now,
            'basis':'original_current_five_page_candidates_and_shop_grid',
            'captures':[{k:c[k]for k in('run_session_id','turn_index','sha256','path','source_kind')if k in c}
                        for c in captures]}
    projection_state=_projection_state(state,run,turn,owned,cash)
    offers=[];skipped=[]
    for action in [*('train:'+s for s in STATS),'rest.normal']:
        base=byid[action];rest=action=='rest.normal';candidate=None if rest else lookup[action]
        _required(type(base.get('rejected'))is bool,'original_current_eligibility_required:'+action)
        eligible=base['rejected']is False
        # Original rejects are retained, never revived using post-purchase
        # gains. They are represented by their NoBuy row without made-up0s.
        if not eligible:
            offers.append({'offer_id':action+'|NoBuy','action_id':action,'phase':None,'phases':[],
                'targets':owned[:],'cost':0,'current_cash_after_payment':cash,
                'gains':deepcopy(getattr(candidate,'stats_gain',None)),
                'direct_score':None,'post_hp_percent':None,'conditional_income':None,
                'eligible':False,'rejected':True,'current_rejection_preserved':True,
                'current_risk':None if rest else candidate.failure_rate,
                'conditional_purchase_uplift':None,'expected_purchase_uplift':None,
                'post_hp_semantics':'unavailable_original_reject',
                'risk_flags':deepcopy(base.get('risk_flags',[])),
                'original_current_components':deepcopy(base.get('components')),'source':deepcopy(source)})
            continue
        _workflow(base)
        _required(_number(base.get('score')),'original_current_score_required:'+action)
        rate=0. if rest else candidate.failure_rate
        if not rest:
            _required(_number(rate)and 0<=rate<=100
                      and base.get('failure_rate')==rate,
                      'current_training_observed_risk_required:'+action)
        failure_probability=rate/100.
        # This core retains the complete original current score, including
        # energy. After-action HP is not used by the legacy lifetime route;
        # missing optional HP diagnostics must not reject a usable score.
        H,energy_parts=0.,{}
        horizon=(base.get('planning')or{}).get('energy_horizon')or{}
        post_hp=horizon.get('after_vitality_percent')
        outcomes=deepcopy(horizon.get('rest_outcomes'))
        income=_income(candidate,action,deps)
        old_gains={}if rest else deepcopy(candidate.stats_gain)
        numeric0=None if rest else _tuned_gain_score(deps['gain_value'](state,old_gains,goal),goal,base)
        # Current baskets: one matching facility and any applicable globals.
        # The original plan builder, not a handwritten cost table, settles all
        # locks, direct1->3 rules, supported bundle identities and current cash.
        from core.auto_trainer.larc_strategy_extension import active_provider
        from core.auto_trainer.larc_strategy_params import params_for_goal
        from core.auto_trainer.larc_strategy_rules import banned_on_day, first_arc_basket_allowed
        rest_globals = (('expedition_skill_pt', 5), ('willpower_three', 7))
        if rest and active_provider().rest_global_offers_enabled(params_for_goal(goal)):
            available = [pair for pair in rest_globals if owned[pair[1]] in (1, 2) and not banned_on_day(pair[1], now)]
        elif rest:
            available = []
        else:
            available = [pair for pair in [FACILITIES[candidate.training_type], *GLOBALS]
                         if owned[pair[1]] in (1, 2) and not banned_on_day(pair[1], now)]
        subsets=[()]+[parts for n in range(1,len(available)+1)for parts in combinations(available,n)]
        for parts in subsets:
            phases=[p for p,_ in parts];plan=None;phase=None;targets=owned[:];cost=0;assumptions=[]
            if phases:
                phase=deps['growth_bundle_phase'](phases)
                try:
                    plan=deps['build_adaptation_plan'](deepcopy(observation),phase=phase)
                except ValueError as exc:
                    skipped.append({'action_id':action,'phases':phases,'reason':'original_plan_unavailable','detail':str(exc)})
                    continue
                _required(isinstance(plan,dict)and type(plan.get('cost'))is int
                          and 0<=plan['cost']<=cash,'current_plan_must_use_current_cash')
                targets=plan.get('targets');cost=plan['cost']
                _required(isinstance(targets,list)and len(targets)==10
                          and all(type(x)is int and 0<=x<=3 for x in targets),'original_plan_targets_required')
                if not first_arc_basket_allowed(targets, owned, cash, cost, now):
                    skipped.append({'action_id':action,'phases':phases,'reason':'first_arc_budget_or_zero_card_gate'})
                    continue
                changed={i for i,(a,b)in enumerate(zip(owned,targets))if a!=b}
                chosen={i for _,i in parts}
                _required(changed<=chosen and all(targets[i]==3 and targets[i]>owned[i]for i in changed),
                          'unexpected_current_plan_side_effect')
                if not changed or cost==0:
                    skipped.append({'action_id':action,'phases':phases,'reason':'already_owned_or_no_effect'});continue
                _required(targets[6]==owned[6],'current_HP_changing_basket_unsupported_keep_parent')
                if rest:
                    gains,assumptions,uplift={},['rest_zero_current_training_gain'],0.
                else:
                    projection=deps['gains_after_purchase'](projection_state,candidate,phase,targets=targets[:],purchase_v3=True)
                    _required(isinstance(projection,(tuple,list))and len(projection)==2
                              and isinstance(projection[0],dict),'original_current_gain_projection_required')
                    gains,assumptions=deepcopy(projection[0]),deepcopy(projection[1])
                    uplift=_tuned_gain_score(deps['gain_value'](state,gains,goal),goal,base)-numeric0
            else:gains=deepcopy(old_gains);uplift=0.
            expected_uplift=(1.-failure_probability)*uplift
            direct=base['score']-H+expected_uplift
            offers.append({'offer_id':action+'|'+(phase or'NoBuy'),'action_id':action,
                'phase':phase,'phases':phases,'targets':targets[:],'cost':cost,
                'current_cash_after_payment':cash-cost,'gains':gains,'direct_score':round(direct,4),
                'direct_score_unrounded':direct,'original_current_score':base['score'],
                'removed_energy_score':H,'removed_energy_components':energy_parts,
                'original_current_numeric':numeric0,'current_numeric_uplift':expected_uplift,
                'conditional_purchase_uplift':uplift,'expected_purchase_uplift':expected_uplift,
                'current_failure_probability':failure_probability,
                'post_hp_percent':post_hp,'post_hp_basis':'optional_diagnostic_not_used_by_career_route',
                'post_hp_semantics':'original_rest_planning_mean'if rest else'conditional_success',
                'post_hp_conditional_on_training_success':not rest,
                'rest_outcomes':outcomes,'conditional_income':deepcopy(income),
                'current_income_credited_to_payment':0,'eligible':True,'rejected':False,
                'current_risk':0 if rest else candidate.failure_rate,'risk_already_in_original_score':True,
                'risk_accounting':{'original_current_score_risk_loss_and_aversion_retained':True,
                    'whole_direct_score_multiplied_by_success_probability':False,
                    'purchase_uplift_success_weighted_once':True,
                    'failed_HP_cash_owned_not_projected_by_builder':True},
                'source_confidence':base.get('source_confidence'),
                'risk_flags':deepcopy(base.get('risk_flags',[])),'projection_assumptions':assumptions,
                'current_max_gate_applied':False,'future_value_not_priced_here':True,
                'original_current_components':deepcopy(base.get('components')),'source':deepcopy(source)})
    _required(len({o['offer_id']for o in offers})==len(offers),'duplicate_current_offer_id')
    return{'schema_version':SCHEMA,'supported':True,'calendar_ordinal':now,'offers':offers,'rejected_baskets':skipped,
           'source':source,'current_wallet':cash,'owned_levels':owned[:],
           'current_max_gate_removed':True,'original_current_rejects_preserved':True,
           'future_score_or_funding_not_priced_here':True,
           'execution_authorized':False,'purchase_or_pending_created':False,
           'same_HP_baskets_only':True,'current_risk0_scope':False,
           'current_risk_scope':'original_local_eligibility_and_its_configured_limit_or_exception_preserved'}


def build_offers(state,goal,observation,current_rows,*,deps,calendars=FIRSTCAMP_CALENDARS):
    """Return the complete current offer pool or raise Unsupported, never partial.

    See _build_offers for the score decomposition and dependency signatures.
    Caller retains its complete parent on Unsupported; no helper or exception
    is converted into an executable fallback action in this pure leaf.
    """
    try:return _build_offers(state,goal,observation,current_rows,deps=deps,calendars=calendars)
    except Unsupported:raise
    except (KeyError,TypeError,ValueError,AttributeError,IndexError)as exc:
        raise Unsupported('firstcamp_current_offer_helper_unavailable:'+str(exc))from exc
