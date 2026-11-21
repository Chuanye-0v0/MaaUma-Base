"""Adapt pure263 plans to the existing Pacer decision/purchase owners.

No executor, payment, receipt creation, or new stop gate. Missing coverage
returns the original161 result; conditional estimates remain labelled.
"""
from copy import deepcopy
from dataclasses import replace
from itertools import combinations
from math import isfinite

from . import consumer
from .forecast_fields import append_forecast
from core.auto_trainer.rejection_policy import is_hard_rejected

TABS=consumer.TABS

def enabled(goal):
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    return unified_larc_enabled(goal)

def in_scope(state,goal):
    if not enabled(goal):return False
    from core.auto_trainer.larc_calendar import period_ordinal
    return period_ordinal(((state.scenario_state or{}).get('larc_period')or{}).get('value')or{})in consumer.DAYS

def _num(x):return type(x)in(int,float)and isfinite(x)

def scenario(state,goal,profile=None):
    from core.auto_trainer.larc_capacity import capacity_bounds,planning_capacity
    from core.auto_trainer.larc_friend_outing import bound_outing_progress
    supplied_profile = profile
    cap=planning_capacity(state);bounds=list(capacity_bounds(state))
    outing=(state.scenario_state or{}).get('larc_friend_outing')or{}
    run=consumer._source(state);src=outing.get('source')or{}
    known=(outing.get('status')=='accepted'and src.get('run_session_id')==run
        and type(src.get('turn_index'))is int and src['turn_index']<=state.turn)
    if not known:
        progress=bound_outing_progress(state)
        if progress is not None:
            outing=progress
            known=True
    result={'capacity_points':cap,'capacity_bounds':bounds,
        'future_training_level_prior':6,'current_rest_HP_points':50,
        'terminal':{'race_bonus_percent':0,'mei_unlocked':bool(known and outing.get('unlocked')is True),
            'mei_completed':bool(known and outing.get('completed')==5),'mei_event_multiplier':1.,'reporter_band':'low'},
        'scenario_basis':{'capacity':'existing_capacity_bounds_midpoint_not_observed_exact_capacity',
            'future_training_level_prior':'retained263_original_JP_level6_prior_not_CN_observed',
            'current_rest_HP_points':'retained263_conditional_JP50_not_CN_observed',
            'reporter_band':'unknown_reporter_low_scenario_not_observed_zero_bond',
            'race_bonus_percent':'unknown_race_bonus_zero_bonus_scenario_not_observed_zero',
            'mei':'same_run_last_confirmed_outing_stage_not_future_completion'if known else'unknown_Mei_no_ending_reward_scenario'},
        'unknown_observations':['raw_stat_parity','stat_caps','future_layouts','future_risk','reporter_bond','race_bonus_percent']+([]if known else['Mei_ending_stage'])}
    profile=profile if profile is not None else(state.scenario_state or{}).get('larc_tail263_support_profile')
    if isinstance(profile,dict)and profile.get('run_session_id')==run:
        bonus=profile.get('race_bonus_percent');multi=profile.get('event_multiplier')
        if _num(bonus)and 0<=bonus<=100:
            result['terminal']['race_bonus_percent']=bonus;result['scenario_basis']['race_bonus_percent']='same_run_declared_support_profile';result['unknown_observations'].remove('race_bonus_percent')
        if _num(multi)and 0<multi<=3:result['terminal']['mei_event_multiplier']=multi
    from core.auto_trainer.larc_calendar import period_ordinal
    from core.auto_trainer.larc_strategy_params import params_for_goal
    sc_state = state.scenario_state or {}
    period = sc_state.get('larc_period') or {}
    if (isinstance(run,str) and bool(run) and params_for_goal(goal).r56_terminal_deck_effects_enabled
            and period.get('status')=='verified' and period_ordinal(period.get('value') or {})==65):
        from core.auto_trainer.run_support_profile import _aggregate_effects
        attached = sc_state.get('larc_friend_outing') or {}
        session_prof = sc_state.get('larc_support_card_profile')
        bridge = sc_state.get('larc_last_training_deck_effects') or {}
        candidates=[]
        for label, candidate in (('explicit_complete_support_profile',supplied_profile),
                                  ('session_complete_support_profile',session_prof),
                                  ('attached_complete_support_profile',attached.get('support_card_profile'))):
            if (isinstance(candidate,dict) and candidate.get('schema_version')=='uma_run_support_profile.v1'
                    and candidate.get('run_session_id')==run):
                candidates.append((label,_aggregate_effects({'terminal_deck_effects':candidate.get('terminal_deck_effects')},candidate.get('cards'))))
        if isinstance(bridge,dict) and bridge.get('run_session_id')==run and isinstance(bridge.get('terminal_deck_effects'),dict):
            candidates.append(('same_run_last_training_deck_effects',bridge['terminal_deck_effects']))
        legacy=profile if isinstance(profile,dict) and profile.get('run_session_id')==run else {}
        for key in ('race_bonus_percent','event_multiplier'):
            old=legacy.get(key)
            valid_old=_num(old) and (0<=old<=100 if key=='race_bonus_percent' else 0<old<=3)
            if valid_old:
                continue
            for label, aggregate in candidates:
                value=aggregate.get(key)
                if not _num(value) or not (0<=value<=100 if key=='race_bonus_percent' else 0<value<=3):
                    continue
                target='race_bonus_percent' if key=='race_bonus_percent' else 'mei_event_multiplier'
                result['terminal'][target]=value
                result['scenario_basis'][target]='r56_'+label
                if key=='race_bonus_percent' and key in result['unknown_observations']:
                    result['unknown_observations'].remove(key)
                break
    override=(goal.larc_policy or{}).get('tail263_scenario')
    if override is not None and not isinstance(override,dict):result['unsupported_user_scenario']=True
    if isinstance(override,dict):
        for key in ('capacity_points','capacity_bounds','future_training_level_prior','current_rest_HP_points'):
            if key in override:result[key]=deepcopy(override[key]);result['scenario_basis'][key]='explicit_user_conditional_scenario'
        if isinstance(override.get('terminal'),dict):
            result['terminal'].update({k:deepcopy(v)for k,v in override['terminal'].items()if k in result['terminal']})
            result['scenario_basis']['terminal_override']='explicit_user_conditional_scenario_not_observed'
    return result

def _valid_scenario(value):
    term=value.get('terminal')or{}
    return (not value.get('unsupported_user_scenario')
        and type(value.get('future_training_level_prior'))is int and 1<=value['future_training_level_prior']<=6
        and _num(value.get('current_rest_HP_points'))and 0<=value['current_rest_HP_points']<=120
        and _num(term.get('race_bonus_percent'))and 0<=term['race_bonus_percent']<=100
        and _num(term.get('mei_event_multiplier'))and 0<term['mei_event_multiplier']<=3
        and type(term.get('mei_unlocked'))is bool and type(term.get('mei_completed'))is bool
        and term.get('reporter_band')in('low','mid','high'))

def _risk_annotations(records):
    result={}
    for rec in records:
        risk=rec.get('risk_observations')or{};bound={}
        for tab in TABS:
            row=risk.get(tab)or{};src=row.get('source')or{}
            energy=((rec.get('forecast_metadata')or{}).get(tab)or{}).get('energy_observation')or{}
            es=energy.get('source')or{}
            if (row.get('run_session_id')==rec.get('run_session_id')and row.get('turn_index')==rec.get('turn_index')
                and _num(row.get('failure_rate'))and all(src.get(k)==es.get(k)and src.get(k)is not None for k in ('run_session_id','turn_index','sha256'))):
                bound[tab]=deepcopy(row)
        result[str(rec.get('turn_index'))]=bound
    return result

def _context(state,observation=None):
    run=consumer._source(state)
    if observation is not None:
        if observation.get('errors')or observation.get('pending_cost')!=0:return {}
        return {'status':'accepted','run_session_id':run,'through_turn':state.turn,'levels':deepcopy(observation.get('levels')),
            'points':observation.get('points'),'balance_current':True,'levels_observed':True,
            'basis':'existing_current_grid_observation_pending_zero'}
    old=deepcopy((state.scenario_state or{}).get('larc_expedition_context')or{})
    if old.get('status')=='accepted'and old.get('run_session_id')==run and type(old.get('through_turn'))is int and old['through_turn']<=state.turn:
        previous=old['through_turn'];old['through_turn']=state.turn
        if previous!=state.turn:old.update(balance_current=False,points=None,levels_observed=False,original_known_through_turn=previous,basis='carried_forward_original_owned_receipts_not_current_wallet')
    return old

def _quotes(observation,state):
    if not observation or observation.get('errors')or observation.get('pending_cost')!=0:return []
    from core.auto_trainer.larc_adaptation import GROWTH_PHASE_SLOTS,TRAINING_PHASE_SLOTS,growth_bundle_phase,build_adaptation_plan
    from core.auto_trainer.larc_adaptation_schedule import UNLOCK_DAYS
    from core.auto_trainer.larc_calendar import period_ordinal
    day=period_ordinal(state.scenario_state['larc_period']['value']);run=consumer._source(state);rows=[]
    phases=[p for p,s in GROWTH_PHASE_SLOTS.items()if day>=UNLOCK_DAYS.get(s,0)and s not in observation.get('locked_slots',[])]
    for size in (1,2):
        for members in combinations(phases,size):
            if sum(p in TRAINING_PHASE_SLOTS for p in members)>1:continue
            phase=members[0]if size==1 else growth_bundle_phase(members)
            try:plan=build_adaptation_plan(observation,phase=phase)
            except ValueError:continue
            if not plan['cost']or plan['deferred_slots']:continue
            rows.append({'basket_id':phase,'phase':phase,'purchase_phases':list(members),
                'targets':plan['targets'],'cost':plan['cost'],'run_session_id':run,'through_turn':state.turn,
                'calendar':day,'legal':True,'price_basis':'existing_rule_reference',
                'legality_basis':'current_pending_zero_grid_owned_levels_and_existing_catalog_unlocks; executor_rechecks_actual_shop',
                'purchase_verified':False})
    return rows

def _incomes(state, *, reference182=False):
    from core.auto_trainer.larc_adaptation_strategy import training_income
    result={}
    if reference182:
        from .strategy182 import conditional_income_quote
        period=((state.scenario_state or{}).get('larc_period')or{}).get('value')or{}
        run=consumer._source(state)
        for candidate in state.training_options:
            quoted=conditional_income_quote(candidate,period,run_id=run,turn=state.turn)
            if quoted is not None:result[candidate.action_id]=quoted
        return result
    for c in state.training_options:
        capture=(c.raw_data or{}).get('capture')or{}
        effects=((c.scenario_mechanics or{}).get('larc')or{}).get('training_effects')or{};src=effects.get('source')or{}
        amount,basis=training_income(c)
        if basis=='observed_training_preview'and all(src.get(k)==capture.get(k)and src.get(k)is not None for k in ('run_session_id','turn_index','sha256')):
            result[c.action_id]={'status':'accepted','conditional_amount':amount,'source':deepcopy(src),'basis':basis,'not_current_cash':True}
    return result


def _cash182_enabled(goal):
    from .strategy182 import enabled
    return enabled(goal)


def _cash182_context(state,goal,context):
    if not _cash182_enabled(goal):return context
    from .strategy182 import current_context
    return current_context(state,context)


_EVALUATE_CACHE: dict = {}


def evaluate(state,goal,baseline,*,observation=None,profile=None):
    if not in_scope(state,goal):return {'research':{'active':False,'reason':'outside263_scope'},'read_only':True}
    if baseline.action not in ('train','rest'):
        return {'research':{'active':False,'reason':'original_stop_handoff_or_unmodeled_action_preserved','new_stop_required':False},'read_only':True}
    if consumer.supported_failure_limit(goal) is None:
        return {'research':{'active':False,'reason':'unsupported_explicit_risk_baseline_kept','new_stop_required':False},'read_only':True}
    sc = state.scenario_state or {}
    cache_key = None
    if observation is None and profile is None:
        try:
            from core.auto_trainer.larc_strategy_params import params_for_goal
            cache_key = (
                id(state),
                id(goal),
                params_for_goal(goal),
                state.turn,
                state.vitality,
                baseline.action,
                baseline.target,
                tuple((r.get('action_id'), r.get('score')) for r in (baseline.scored_candidates or [])),
                id(sc.get('larc_expedition_context')),
                repr(sc.get('larc_expedition_context')),
                id(sc.get('larc_first_expedition_reference')),
                id(sc.get('larc_growth_observation')),
                id(sc.get('larc_support_card_profile')),
                id(sc.get('larc_last_training_deck_effects')),
            )
        except Exception:
            cache_key = None
        if cache_key is not None and cache_key in _EVALUATE_CACHE:
            return deepcopy(_EVALUATE_CACHE[cache_key])
    try:
        from core.auto_trainer.larc_attribute_plan import history_sample
        from core.auto_trainer.larc_energy_observation import training_energy_observation
        run=consumer._source(state)
        for c in state.training_options:
            if training_energy_observation(c,state).get('status')!='accepted':
                return {'research':{'active':False,'reason':'current_energy_capture_unbound','new_stop_required':False},'read_only':True}
        memory=(state.scenario_state or{}).get('larc_first_expedition_reference')or{}
        archive={'schema_version':'larc.first_expedition_reference.v1','run_session_id':memory.get('run_session_id'),'records':memory.get('records',[])}
        current=history_sample(state)
        packet={'status':'accepted','period_status':'verified','run_session_id':run,'sample':current,'source_kind':'existing_normal_pipeline_history_sample'}
        assumed=scenario(state,goal,profile)
        if not _valid_scenario(assumed):
            return {'research':{'active':False,'reason':'unsupported_conditional_scenario_baseline_kept','new_stop_required':False},'read_only':True,'assumptions':assumed}
        res = consumer.evaluate_current(state,goal,baseline_rows=baseline.scored_candidates,
            accepted_current_sample=packet,phase_archive=archive,past_risk_annotations=_risk_annotations(archive['records']),
            scenario=assumed,current_context=_cash182_context(state,goal,_context(state,observation)),
            current_conditional_incomes=_incomes(state,reference182=_cash182_enabled(goal)),cash182=_cash182_enabled(goal),baseline_advice={'action':baseline.action,'target':baseline.target},
            purchase_options=_quotes(observation,state))
        if cache_key is not None:
            if len(_EVALUATE_CACHE) >= 16:
                _EVALUATE_CACHE.clear()
            _EVALUATE_CACHE[cache_key] = deepcopy(res)
        return res
    except (ValueError,KeyError,TypeError,IndexError,ArithmeticError)as exc:
        return {'research':{'active':False,'reason':'optional263_input_unavailable:'+str(exc),'new_stop_required':False},'read_only':True}


def apply_ranking(state,goal,baseline):
    if not in_scope(state,goal):return baseline
    if baseline.action not in ('train','rest'):return baseline
    report=evaluate(state,goal,baseline);research=report.get('research')or{}
    rows=deepcopy(baseline.scored_candidates)
    for row in rows:row.setdefault('planning',{})['tail263']=summary(report)
    if not research.get('active'):return replace(baseline,scored_candidates=rows)
    selected=research.get('advice');winner=next((r for r in rows if r.get('action_id')==selected and not is_hard_rejected(r)),None)
    if winner is None:return replace(baseline,scored_candidates=rows)
    ranks={r['action_id']:r for r in research.get('ranking',[])}
    for row in rows:
        if row.get('action_id')in ranks:
            ranked=ranks[row['action_id']]
            row['baseline161_score']=row.get('score');row['baseline161_components']=deepcopy(row.get('components')or{})
            row['score']=ranked['score'];row['score_basis']=research['score_basis']
            parts={k:ranked[k]for k in ('unified_terminal_delta','current_residual','current_risk_aversion')if _num(ranked.get(k))}
            # Normal kernel rows expose these three existing terms. A caller's
            # diagnostic-only rank stub may expose only its existing Q scalar.
            complete=len(parts)==3
            if 'soft_risk_discount'in ranked:parts['soft_risk_discount']=ranked['soft_risk_discount']
            row['components']=parts if complete and abs(sum(parts.values())-row['score'])<1e-7 else{'tail263_reported_joint_value':row['score']}
    from core.auto_trainer.larc_timeline_workflow import selection_key
    return replace(baseline,action='train'if selected.startswith('train:')else'rest',target=selected.split(':',1)[1]if selected.startswith('train:')else None,
        confidence=winner.get('source_confidence',baseline.confidence),risk_flags=list(winner.get('risk_flags',[])),
        reason='263 conditional terminal-value joint comparison; original current eligibility retained',scored_candidates=sorted(rows,key=selection_key))

def purchase_recommendation(observation,period,state,goal,profile=None):
    from core.auto_trainer.larc_opening import LarcOpeningStrategy
    baseline=LarcOpeningStrategy(goal).evaluate_baseline(state)
    report=evaluate(state,goal,baseline,observation=observation,profile=profile)
    research=report.get('research')or{}
    if not research.get('active'):return None,report
    basket=research.get('selected_basket')or{};phase=basket.get('phase')
    action=research.get('advice');cost=basket.get('cost',0)
    plan=research.get('future_facility_plan')
    return {'phase':phase,'cost':cost,'training':action.split(':',1)[1]if action and action.startswith('train:')else None,
        'after_action_id':action,'purchase_phases':basket.get('purchase_phases',[]),'points_after_purchase':observation['points']-cost,
        **({'future_facility_plan':deepcopy(plan)} if plan is not None else {}),
        'reason':'tail263_joint_plan_existing_purchase_owner','policy_kind':'tail263_joint_current_purchase',
        'tail263':summary(report),'strategy182':deepcopy(report.get('strategy182')),'prediction_status':'conditional_plan_requires_original_payment_receipt_and_post_purchase_reobservation'},report

def summary(report):
    research=report.get('research')or{};coverage=report.get('coverage')or{};procurement=report.get('procurement')or{}
    result = {'schema_version':'larc.tail263_production.v1','active':research.get('active')is True,
        'reason':research.get('reason'),'advice':research.get('advice'),'selected_basket':research.get('selected_basket'),
        'score_basis':research.get('score_basis'),'read_only_planner':True,'original_owner_only':True,
        'operations':research.get('operations'),'coverage':{k:deepcopy(coverage.get(k))for k in ('past_menus_used','future_steps','financial_fix_active','current_training_pages','finance','past_menu_skipped','terminal_value_policy')if k in coverage},
        'conditional_scenario':report.get('assumptions'),'ranked_baskets':procurement.get('ranked_baskets',0),
        'finance':[{'basket_id':r.get('basket_id'),'action_id':r.get('action_id'),'current_cash':r.get('current_cash'),
            'basket_cost':r.get('basket_cost'),'required_reserve':r.get('new_reserve'),'allowed':r.get('allowed_by_purchase_gate'),
            'current_income_fix_active':r.get('current_income_fix_active')}for r in (report.get('finance')or[])[:24]],
        'CN_verified':False,'new_stop_required':False,'strategy182':deepcopy(report.get('strategy182'))}
    if research.get('future_facility_plan') is not None:
        result['future_facility_plan']=deepcopy(research['future_facility_plan'])
    if procurement.get('next_facility_investment') is not None:
        result['next_facility_investment']=deepcopy(procurement['next_facility_investment'])
    return append_forecast(result,report)
