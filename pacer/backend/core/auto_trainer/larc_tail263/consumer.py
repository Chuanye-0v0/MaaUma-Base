"""Read-only real TrainingTurnState adapter to frozen263 pure kernel/finance.
No Native/Env, simulated identity, executor, record or ledger mutations.
Caller supplies the current baseline pool; no second Pacer decision is hidden here.
"""
from copy import deepcopy
from core.auto_trainer.rejection_policy import discount_soft_score, is_hard_rejected
from dataclasses import asdict,replace
from math import isfinite

TABS=('speed','stamina','power','guts','wisdom')
DAYS=(60,61,62,63,65)


def pure_modules():
    from core.auto_trainer import larc_expedition_kernel as K
    return K, K, K


def number(x):return type(x) in (int,float) and isfinite(x)


def supported_failure_limit(goal):
    """Use the Goal's percent limit while retaining the older strategy domain."""
    from core.auto_trainer.larc_opening import LarcOpeningStrategy
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled

    limit = goal.training_failure_rate_limit
    default = LarcOpeningStrategy.DEFAULT_FAILURE_LIMIT
    if limit is None:
        return default
    if type(limit) not in (int, float) or not 0 <= limit <= 100:
        return None
    if limit > default and not unified_larc_enabled(goal):
        return None
    return float(limit)


def levels(value):
    return (isinstance(value,(list,tuple)) and len(value)==10
        and all(type(x)is int and 0<=x<=(3 if i<8 else 2) for i,x in enumerate(value)))


def _source(state):
    opts=state.training_options
    if len(opts)!=5 or {c.training_type for c in opts}!=set(TABS):raise ValueError('current_five_pages_missing')
    captures=[(c.raw_data or {}).get('capture') or {} for c in opts]
    runs={s.get('run_session_id') for s in captures}
    if len(runs)!=1 or None in runs or any(s.get('turn_index')!=state.turn for s in captures):
        raise ValueError('current_run_turn_source_mismatch')
    return next(iter(runs))


def _row_id(r):
    if r.get('action_id'):return r['action_id']
    if r.get('training_type') in TABS:return 'train:'+r['training_type']
    if r.get('action')=='rest':return 'rest.normal'
    return None


def discount_soft_ranking(rows, soft_actions):
    if not soft_actions:
        return rows
    adjusted = []
    changed = False
    for row in rows:
        score = row['score']
        discounted = discount_soft_score(score, row['action_id'] in soft_actions)
        if discounted != score:
            row = dict(row, score=discounted, soft_risk_discount=discounted-score)
            changed = True
        adjusted.append(row)
    return sorted(adjusted, key=lambda row: (-row['score'], row['action_id'])) if changed else rows


def unreadable_failure_exclusions(state):
    # Optional forecast exclusions do not change the ordinary scoring pool.
    return [{'action_id': c.action_id, 'failure_rate': None, 'allowed': False,
             'soft_risk': True, 'reason': 'failure_rate_unreadable'}
            for c in state.training_options if not number(c.failure_rate)]


def _accepted_current(sample,run,turn,state,*,omitted=()):
    if (not isinstance(sample,dict) or sample.get('status')!='accepted'
        or sample.get('run_session_id')!=run or sample.get('sample',{}).get('turn_index')!=turn):
        raise ValueError('accepted_current_sample_same_run_turn_required')
    s=sample['sample'];raw=s.get('raw_gains') or {};pts=s.get('skill_points') or {}
    for c in state.training_options:
        if c.action_id in omitted:
            continue
        t=c.training_type
        if (set(raw.get(t,{}))!=set(TABS) or any(not number(raw[t][k]) or raw[t][k]<0 for k in TABS)
                or not number(pts.get(t))):raise ValueError('current_sample_complete_gains_or_Pt_missing:'+t)
        # Preserve every actually read display increment, not a guessed Native request.
        from core.auto_trainer.larc_opportunity_forecast import display_gain
        for k,v in c.stats_gain.items():
            if k in TABS and display_gain(state.current_stats[k],raw[t][k])!=v:
                raise ValueError('accepted_sample_disagrees_current_display:'+t+':'+k)
        if c.stats_gain.get('skill_pt')!=pts[t]:raise ValueError('sample_Pt_disagrees_current:'+t)
    return s


def _past(archive,run,turn):
    from core.auto_trainer.larc_first_expedition_reference import past
    return past(archive,run,turn)


def _current_hp(c,run,turn,capacity):
    vp=((c.scenario_mechanics or {}).get('larc') or {}).get('vitality_preview') or {}
    src=vp.get('source') or {}
    if (vp.get('status')!='accepted' or vp.get('unit')!='percent_of_current_max'
        or src.get('run_session_id')!=run or src.get('turn_index')!=turn
        or not number(vp.get('delta_percent'))):raise ValueError('bound_current_energy_preview_missing:'+c.training_type)
    return vp['delta_percent']*capacity/100


def _menus(records,annotations,state,owned,expectation,capacity,scenario,K):
    from core.auto_trainer.larc_training_forecast import project_gains
    from core.auto_trainer.larc_opportunity_forecast import display_gain,raw_gain
    from core.auto_trainer.larc_calendar import period_at
    out=[];audits=[]
    for rec in records:
        values=[]
        for i,t in enumerate(TABS):
            old=deepcopy((rec.get('forecast_metadata') or {}).get(t) or {})
            oldlevels=rec.get('known_owned_levels')
            if not levels(oldlevels) or not number(old.get('expectation')):
                raise ValueError('past_owned_or_expectation_unknown:T'+str(rec['turn_index']))
            e=old.get('energy_observation') or {};es=e.get('source') or {}
            if (e.get('status')!='accepted' or e.get('unit')!='points' or
                es.get('run_session_id')!=rec['run_session_id'] or es.get('turn_index')!=rec['turn_index']):
                raise ValueError('past_energy_source_unknown:T'+str(rec['turn_index']))
            request=e.get('requested_delta_points')
            if request is None and e.get('clipped')is False:request=e.get('applied_delta_points')
            before=e.get('before_points');ann=(annotations.get(str(rec['turn_index']),{}) or {}).get(t) or {}
            if (not isinstance(request,(list,tuple)) or len(request)!=2 or not all(number(x)for x in request)
                or not isinstance(before,(list,tuple)) or len(before)!=2 or not all(number(x)for x in before)):
                raise ValueError('past_requested_or_anchor_HP_unknown:T'+str(rec['turn_index'])+':'+t)
            if (ann.get('run_session_id')!=rec['run_session_id'] or ann.get('turn_index')!=rec['turn_index']
                or not number(ann.get('failure_rate'))):raise ValueError('past_bound_current_risk_missing:T'+str(rec['turn_index'])+':'+t)
            # Past raw_equivalent is a preview, transformed by formal161 projection.
            old.update(period=rec['period'],levels=list(oldlevels),purchase_v3=True)
            target={**old,'period':period_at(scenario['calendar']),'levels':list(owned),'expectation':expectation}
            gains={k:display_gain(state.current_stats[k],rec['raw_gains'][t][k]) for k in TABS}
            gains['skill_pt']=rec['skill_points'][t]
            projected,report=project_gains(state.current_stats,gains,t,old,target)
            hp=min(request);hp=hp*report['cost_factor'] if hp<0 else hp
            anchor=sum(before)/2
            lvl=ann.get('public_training_level')
            if lvl is None:lvl=scenario.get('future_training_level_prior')
            if type(lvl)is not int:raise ValueError('future_risk_training_level_prior_not_declared')
            values.append(K.Action('future:'+t,'train',tuple(raw_gain(state.current_stats[k],projected[k])for k in TABS),
                projected['skill_pt'],hp,p_percent=ann['failure_rate'],training_type=i,
                anchor_hp=anchor,public_training_level=lvl,assumptions=(
                    'past_menu_not_future_layout','formal_project_gains_estimate','HP_request_conservative_range_endpoint',
                    'past_anchor_HP_range_midpoint_scenario','future_JP_risk_curve_level_prior_not_current_observed_risk')))
            audits.append({'source_turn':rec['turn_index'],'source_calendar':rec['calendar_ordinal'],'training':t,
                'projection':report,'past_HP_request_range':request,'past_HP_anchor_range':before,
                'risk_anchor_percent':ann['failure_rate'],'risk_public_level_or_prior':lvl})
        out.append(K.JointMenu('real-past-T'+str(rec['turn_index']),rec['calendar_ordinal'],tuple(values)))
    return tuple(out),audits


def _joint_rank(current,state,owned,cash,expectation,records,annotations,scenario,baseline_rows,
                options,incomes,K,F,S,max_ops,*,cash182=False,prefer_current_facility_use=False,reserve_reference_income=False,
                next_facility_investment=False, optional_next_facility=False, excluded_future_tabs=frozenset(),
                defer_courage_zero_stamina=False, high_cash_monetization=False):
    """Frozen263 orchestration: paid quote -> same kernel -> coupling -> finance.
    Only caller-supplied accepted current legal/price facts or labelled rule
    scenarios are examined; never fabricates Native legal_actions.
    """
    from dataclasses import replace
    from core.auto_trainer.larc_adaptation import selected_upgrade_cost,TRAINING_UPGRADES
    from core.auto_trainer.larc_adaptation_schedule import UNLOCK_DAYS
    from core.auto_trainer.larc_calendar import period_at
    from core.auto_trainer.larc_training_forecast import project_gains
    from core.auto_trainer.larc_opportunity_forecast import display_gain,raw_gain
    specs=[{'basket_id':'empty','targets':list(owned),'cost':0,'price_basis':'no_purchase','purchase_verified':False}]
    rejected=[]
    for option in options:
        o=deepcopy(option);target=o.get('targets');cost=o.get('cost')
        changed=[i for i in range(10)if levels(target)and target[i]!=owned[i]]
        valid=(levels(target)and number(cost)and cost>=0 and number(cash)and cost<=cash
            and o.get('run_session_id')==_source(state)and o.get('through_turn')==state.turn
            and o.get('calendar')==current.calendar and o.get('legal')is True
            and o.get('price_basis')in('accepted_current_shop_quote','existing_rule_reference')
            and 1<=len(changed)<=2 and all(i in(0,1,2,3,4,6,7)and target[i]>owned[i]and target[i]==3 for i in changed)
            and sum(i<5 for i in changed)<=1 and selected_upgrade_cost(owned,target)==cost
            and all(current.calendar>=UNLOCK_DAYS.get(i,0) for i in changed))
        if not valid:rejected.append({'basket_id':o.get('basket_id'),'reason':'current_scope_price_legality_or_cash_not_established'});continue
        o['purchase_verified']=False;specs.append(o)
    proposals=[];financial=[];basket_results=[];prepared=[]
    bytab={c.training_type:c for c in state.training_options}
    for spec in specs:
        target=spec['targets'];actions=current.actions;project_audit=[];menus=current.menus
        if target!=list(owned):
            if not number(expectation):
                rejected.append({'basket_id':spec.get('basket_id'),'reason':'current_expectation_unknown_for_conditional_projection'});continue
            updated=[]
            for a in current.actions:
                if a.kind!='train':updated.append(a);continue
                t=TABS[a.training_type];candidate=bytab[t]
                source={'period':period_at(current.calendar),'levels':list(owned),'expectation':expectation,
                    'friendship':candidate.friendship,'purchase_v3':True}
                gains={k:display_gain(state.current_stats[k],a.raw_gain[j])for j,k in enumerate(TABS)};gains['skill_pt']=a.pt
                pg,report=project_gains(state.current_stats,gains,t,source,{**source,'levels':target})
                raw=a.raw_gain if report['attribute_factor']==1 else tuple(raw_gain(state.current_stats[k],pg[k])for k in TABS)
                pt=a.pt if report['skill_pt_factor']==1 and (report.get('skill_pt_scaling')or{}).get('base_bonus_delta',0)==0 else pg['skill_pt']
                hp=a.hp_delta*report['cost_factor']if a.hp_delta<0 else a.hp_delta
                updated.append(replace(a,raw_gain=raw,pt=pt,hp_delta=hp,
                    assumptions=(*a.assumptions,'conditional_paid_basket_formal_projection_not_observed_quote')))
                project_audit.append({'training':t,'projection':report})
            actions=tuple(updated)
            if current.future_free_days:
                menus,_=_menus(records,annotations,state,target,expectation,scenario['capacity_points'],
                    {**scenario,'calendar':current.calendar},K)
        quoted=replace(current,actions=actions,menus=menus)
        ranked=K.rank(quoted,max_ops=max_ops)
        if not ranked.get('active'):
            return {**ranked,'advice':None},{'status':'whole_pool_fallback','ranked_baskets':len(prepared),
                'financial_rows':financial,'basket_results':basket_results,'rejected_baskets':rejected,'recommended_buys':[]}
        prepared.append((spec,quoted,ranked,project_audit))
    cash_meta=None
    soft_actions={_row_id(r)for r in baseline_rows if r.get('soft_risk')is True}
    cash_rankings=[item[2]for item in prepared]
    if cash182:
        from .strategy182 import rank_pool
        cash_rankings,cash_meta=rank_pool(prepared,state,owned,cash,records,incomes,scenario,K,S,max_ops)
    for (spec,quoted,legacy,project_audit),ranked in zip(prepared,cash_rankings):
        target=spec['targets'];actions=quoted.actions
        ranking=discount_soft_ranking(ranked.get('ranking',[]),soft_actions)
        basket_results.append({'basket':spec,'active':ranked.get('active'),'reason':ranked.get('reason'),
            'operations':ranked.get('operations'),'current_projection':project_audit,
            'raw_ranking':ranking})
        specific=[i for i in range(5)if target[i]>owned[i]]
        matching=next((t for t,(_,slot)in TRAINING_UPGRADES.items()if specific and slot==specific[0]),None)
        if prefer_current_facility_use and matching and ranking:
            match=next((r for r in ranking if r['action_id']=='train:'+matching),None)
            best=max(ranking,key=lambda r:r['score'])
            deferred=match is not None and match['score'] < best['score']-1e-7
            basket_results[-1]['current_facility_use']={
                'matching_action':'train:'+matching,
                'best_paid_action':best['action_id'],
                'matching_score':match['score'] if match else None,
                'best_paid_score':best['score'],
                'deferred_until_current_use':deferred,
                'basis':'same_basket_same_observed_layout_after_soft_risk_not_a_future_layout_prediction'}
            if deferred:
                rejected.append({'basket_id':spec['basket_id'],
                    'reason':'facility_best_used_later_current_paid_best_is_other_action',
                    'matching_action':'train:'+matching,'best_paid_action':best['action_id']})
                continue
        courage_deferred_rows = 0
        eligible_rows = 0
        terminal_req = (200 if owned[8] == 1 else 0) + (150 if owned[9] == 1 else 0)
        for row in ranking:
            if matching and row['action_id']!='train:'+matching:continue
            eligible_rows += 1
            if defer_courage_zero_stamina and target[6] > owned[6] and current.future_free_days:
                act_obj = next((a for a in actions if a.action_id == row['action_id']), None)
                if act_obj is None or act_obj.kind != 'train' or act_obj.hp_delta >= 0:
                    courage_deferred_rows += 1
                    continue
            route=(row.get('representative_not_forecast')or{}).get('route',[])
            share=sum(x['action'].startswith('future:')and x['action']!='future:rest'for x in route)/len(route)if route else 0.
            inc=incomes.get(row['action_id'])or{}
            src=inc.get('source')or{}
            # This is a conditional future-income budget, not today's wallet.
            # Use the same bound reference quote already used by cash182; keep
            # its forecast label and the original failure-probability discount.
            reference=(reserve_reference_income and inc.get('status')=='forecast')
            accepted=((inc.get('status')=='accepted' or reference)and number(inc.get('conditional_amount'))and inc['conditional_amount']>=0
                and src.get('run_session_id')==_source(state)and src.get('turn_index')==state.turn)
            amount=inc.get('conditional_amount')if accepted else None
            p=next(a.p_percent for a in actions if a.action_id==row['action_id'])
            cf=F.reserve_for_action(current.calendar,target,amount,p,share,fixed_race_incomes=None,
                cash_before=cash,basket_cost=spec['cost'])
            reserve=cf.get('required_post_basket_reserve')
            allowed=spec['cost']==0 or(number(cash)and number(reserve)and cash-spec['cost']>=reserve)
            proposal_row = row
            if high_cash_monetization and 60 <= current.calendar <= 64 and spec['cost'] > 0 and number(cash) and cash - spec['cost'] >= terminal_req:
                allowed = True
                if matching and row['action_id'] == 'train:' + matching:
                    proposal_row = {**row, 'score': row['score'] + 4.0, 'high_cash_monetization_bonus': 4.0}
            financial.append({'basket_id':spec['basket_id'],'action_id':row['action_id'],'basket_cost':spec['cost'],
                'current_cash':cash,'legacy_reserve':cf.get('legacy_reserve'),'new_reserve':reserve,
                'current_income_fix_active':accepted,
                **({'current_income_reference_used':bool(accepted and reference)} if reserve_reference_income else {}),
                'current_income':deepcopy(inc)if inc else{'status':'unknown'},
                'cashflow':cf,'allowed_by_purchase_gate':allowed,'zero_cost_bypass':spec['cost']==0,
                'share_from_representative_route_not_guaranteed':share})
            if allowed:proposals.append({'basket':spec,'winner':proposal_row})
        if eligible_rows > 0 and courage_deferred_rows == eligible_rows:
            rejected.append({'basket_id': spec['basket_id'], 'reason': 'courage_deferred_on_zero_stamina_action'})
    proposals.sort(key=lambda x:(-x['winner']['score'],x['basket']['cost'],x['winner']['action_id']))
    if not proposals:
        return {'active':False,'reason':'no_financially_supported_current_plan','advice':None},{
            'status':'conditional_research_only','ranked_baskets':len(basket_results),'financial_rows':financial,
            'basket_results':basket_results,'rejected_baskets':rejected,'recommended_buys':[],'strategy182':cash_meta}
    original_operations = sum(row.get('operations') or 0 for row in basket_results)
    investment = None
    if next_facility_investment and (cash_meta or {}).get('active') is True:
        from .next_investment import compare
        try:
            extra, investment = compare(prepared, basket_results, proposals[0], soft_actions,
                state=state, owned=owned, cash=cash, expectation=expectation, records=records,
                annotations=annotations, scenario=scenario, incomes=incomes, K=K, F=F, S=S, max_ops=max_ops,
                optional_next_facility=optional_next_facility, excluded_future_tabs=excluded_future_tabs)
        except Exception as exc:
            extra = []
            investment = {'status': 'fallback', 'reason': str(exc) or type(exc).__name__}
        if investment is not None:
            investment['original_ranked_baskets'] = len(basket_results)
        if extra:
            proposals.extend(extra)
            basket_results.extend(proposal['forecast'] for proposal in extra)
            proposals.sort(key=lambda x:(-x['winner']['score'],x['basket']['cost'],x['winner']['action_id']))
    best=proposals[0]
    plan = best['winner'].get('future_facility_plan')
    return {'active':True,'kind':'frozen263_joint_current_basket_action_research','advice':best['winner']['action_id'],
        'ranking':[dict(x['winner'],basket_id=x['basket']['basket_id'],basket_cost=x['basket']['cost'])for x in proposals],
        'selected_basket':best['basket'],'operations':original_operations+(investment or {}).get('extra_operations',0),
        **({'future_facility_plan':plan} if plan is not None else {}),
        'future_steps':len(current.future_free_days),'score_basis':'frozen263_terminal_delta_plus_retained_residual_minus_risk_not_161_score_scale'},{
        'status':'conditional_research_only_not_execution','ranked_baskets':len(basket_results),
        'nonempty_baskets_ranked':sum(b['basket']['cost']>0 for b in basket_results),
        'selected_basket':best['basket'],'financial_rows':financial,'basket_results':basket_results,
        'rejected_baskets':rejected,'recommended_buys':[],
        'conditional_purchase_plan_only':best['basket']['cost']>0,'purchase_verified':False,
        **({'next_facility_investment':investment} if investment is not None else {}),
        'no_current_payment_or_ledger_mutation':True,'strategy182':cash_meta}


def evaluate_current(state,goal,*,baseline_rows,accepted_current_sample,phase_archive,
                     scenario,past_risk_annotations=None,current_context=None,
                     current_conditional_incomes=None,baseline_advice=None,purchase_options=(),max_ops=250000,cash182=False):
    """Return baseline plus conditional263 research advice, never an executable action.

    scenario is explicit estimation input (HP capacity, terminal assumptions,
    future-risk prior); none is promoted to real observation. current_context
    must be an accepted same-run/current-turn owned/balance packet. Procurement
    remains unavailable without actual current prices/legality; no fabricated buy.
    """
    K,F,S=pure_modules()
    result={'schema_version':'larc.retained263_real_consumer.v1','read_only':True,
        'execution_authorized':False,'purchase_authorized':False,'baseline':{
            'advice':deepcopy(baseline_advice),'rows':deepcopy(baseline_rows),
            'basis':'caller_supplied_current_161_pool_not_recomputed'},
        'research':{'active':False},'coverage':{},'assumptions':deepcopy(scenario),
        'procurement':{'status':'unavailable_current_legal_prices_not_supplied','recommended_buys':[],
            'reason':'owned_and_cash_are_facts_but_do_not_establish_current_shop_legality_or_prices'},
        'CN_verified':False,'model_factory_used':False}
    try:
        context=state.scenario_state or {}
        if context.get('simulation') or 'simulation_truth'in context:raise ValueError('real_semantic_state_required_no_simulation_truth')
        from core.auto_trainer.larc_ss_phase import phase_of
        from core.auto_trainer.larc_calendar import period_ordinal
        from core.auto_trainer.larc_phase import phase_weights
        from core.auto_trainer.larc_risk import risk_value
        period=context.get('larc_period') or {};day=period_ordinal(period.get('value') or {})
        result['period_provenance']=deepcopy(period)
        if phase_of(state)!='overseas' or day not in DAYS:raise ValueError('not_verified_last_expedition_free_action')
        failure_limit=supported_failure_limit(goal)
        if failure_limit is None:
            raise ValueError('unsupported_explicit_risk_baseline_kept')
        run=_source(state)
        rows={_row_id(r):r for r in baseline_rows if _row_id(r)}
        unreadable=unreadable_failure_exclusions(state)
        omitted=set()
        if unreadable:
            result['coverage']['current_forecast_exclusions']=unreadable
            for gap in unreadable:
                action=gap['action_id']
                row=rows.get(action)
                # A risk gap for a still-eligible action makes any specialized
                # winner incomplete. An independently hard-excluded action
                # needs no probability in the optional tail kernel.
                if row is not None and row.get('failure_rate') is None and is_hard_rejected(row):
                    omitted.add(action)
            if len(omitted)!=len(unreadable):
                result['research']={'active':False,'reason':'optional263_failure_rate_unreadable',
                    'baseline_current_advice_remains_available':True,'new_stop_required':False}
                return result
        sample=_accepted_current(accepted_current_sample,run,state.turn,state,omitted=omitted)
        result.update(run_session_id=run,turn=state.turn,calendar=day,
            scope='terminal_only_scope'if day==65 else'conditional_future_training_and_finance')
        cap=scenario.get('capacity_points')
        bounds=scenario.get('capacity_bounds')
        if not number(cap) or cap<=0:raise ValueError('explicit_HP_capacity_scenario_required')
        if bounds and not (len(bounds)==2 and bounds[0]<=cap<=bounds[1]):raise ValueError('capacity_scenario_outside_known_bounds')
        if not number(state.vitality)or not 0<=state.vitality<=100:raise ValueError('current_HP_percent_unknown')
        if any(not number(state.current_stats.get(t))for t in (*TABS,'skill_pt')):raise ValueError('current_stats_or_Pt_unknown')
        cc=current_context if current_context is not None else context.get('larc_expedition_context') or {}
        owned=cc.get('levels');cash=cc.get('points')
        owned_known=(cc.get('status')=='accepted'and cc.get('run_session_id')==run and cc.get('through_turn')==state.turn and levels(owned))
        cash_known=owned_known and cc.get('balance_current')is True and number(cash)and cash>=0
        result['current_facts']={'display_stats':deepcopy(state.current_stats),'HP_percent':state.vitality,
            'cash':cash if cash_known else None,'owned_levels':list(owned)if owned_known else None,
            'owned_balance_basis':cc.get('basis'),'owned_levels_source_status':cc.get('levels_status','caller_accepted_context'),
            'owned_levels_independently_observed':cc.get('levels_observed'),'capacity_bounds':deepcopy(bounds),
            'capacity_points_observed':False,'raw_stats_observed':False,'caps_observed':False}
        records,pastdiag=_past(phase_archive,run,state.turn)
        result['coverage']['past_phase_memory']=pastdiag
        supported={'train:'+t for t in TABS}|{'rest.normal'}
        if any(not is_hard_rejected(r) and _row_id(r)not in supported for r in baseline_rows):
            raise ValueError('eligible_current_action_unmodeled_original263_pool_baseline_kept')
        if any((r.get('score_workflow')or{}).get('priority_rank',0)!=0 or
               (r.get('score_workflow')or{}).get('status')=='matched'for r in baseline_rows):
            raise ValueError('explicit_workflow_priority_preserved_baseline_fallback')
        actions=[]
        for c in state.training_options:
            if c.action_id in omitted:
                continue
            t=c.training_type;r=rows.get('train:'+t)
            if r is None or r.get('failure_rate')!=c.failure_rate:raise ValueError('baseline_current_risk_row_mismatch:'+t)
            allowed=not is_hard_rejected(r) and c.failure_rate<=failure_limit
            residual=sum(float((r.get('components')or{}).get(k,0)or 0) for k in K.RESIDUAL_ALLOW|{'mood_recovery'})
            actions.append(K.Action('train:'+t,'train',tuple(sample['raw_gains'][t][k]for k in TABS),
                sample['skill_points'][t],_current_hp(c,run,state.turn,cap),p_percent=c.failure_rate,
                training_type=TABS.index(t),allowed=allowed,rejected_reason=','.join(r.get('risk_flags',[])),
                baseline_score=r.get('score'),residual=residual,
                assumptions=('current_display_to_raw_equivalent_not_observed_raw','HP_percent_times_explicit_capacity_scenario')))
        # The original263 current recovery is fixed C1 overseas50. Real pages do
        # not establish that. Only an explicit scenario can include an eligible rest.
        rr=rows.get('rest.normal')
        if rr and not is_hard_rejected(rr):
            rest=scenario.get('current_rest_HP_points')
            if not number(rest):raise ValueError('eligible_rest_requires_explicit_current_recovery_scenario')
            actions.append(K.Action('rest.normal','rest',hp_delta=rest,baseline_score=rr.get('score'),
                residual=sum(float((rr.get('components')or{}).get(k,0)or 0)for k in K.RESIDUAL_ALLOW|{'mood_recovery'}),
                assumptions=('explicit_recovery_scenario_not_CN_observed_fixed50',)))
        future=tuple(d for d in DAYS if d>day)
        if not owned_known:raise ValueError('current_owned_levels_unknown_terminal_and_projection_not_guessed')
        menus=();audits=[]
        growth=context.get('larc_growth_observation')or{}
        expectation=growth.get('expectation') if growth.get('status')=='accepted'and growth.get('turn_index')==state.turn else None
        if future:
            growth=context.get('larc_growth_observation')or{}
            expectation=growth.get('expectation') if growth.get('status')=='accepted'and growth.get('turn_index')==state.turn else None
            if not number(expectation):raise ValueError('current_expectation_unknown_past_projection_unavailable')
            if len(records)not in(4,5):raise ValueError('four_or_five_strict_past_first_camp_menus_required')
            valid=[];skipped=[]
            for rec in records:
                try:
                    new,detail=_menus([rec],past_risk_annotations or {},state,owned,expectation,cap,{**scenario,'calendar':day},K)
                    valid.extend(new);audits.extend(detail)
                except ValueError as exc:skipped.append({'turn':rec['turn_index'],'reason':str(exc)})
            result['coverage']['past_menu_skipped']=skipped
            if len(valid)not in(4,5):raise ValueError('four_or_five_valid_past_menus_required:'+str(skipped))
            menus=tuple(valid)
            records=[r for r in records if 'real-past-T'+str(r['turn_index']) in {m.source_id for m in menus}]
        terminal=scenario.get('terminal')or{}
        required=('race_bonus_percent','mei_unlocked','mei_completed','mei_event_multiplier','reporter_band')
        if any(k not in terminal for k in required):raise ValueError('explicit_conditional_terminal_scenario_required')
        events=S.normal_schedule(final_level=owned[9],**{k:terminal[k]for k in required})
        events=tuple(e for e in events if e.calendar>day)
        weights=phase_weights(state,goal)
        if weights['stats']!=1:raise ValueError('nondefault_numeric_scale_baseline_fallback')
        raw=tuple(K.inverse_display(state.current_stats[k])for k in TABS)
        from core.auto_trainer.larc_policy import attribute_options
        attr_opts=attribute_options(goal)
        eff_weights=attr_opts.get('attribute_weights') or (goal.larc_policy.get('attribute_weights') or {})
        eff_credit=attr_opts.get('after_target_credit',goal.larc_policy.get('after_target_credit',.5))
        current=K.Current(K.State(raw,state.vitality*cap/100,cap,state.current_stats['skill_pt'],None),day,future,
            tuple(actions),menus,tuple(goal.target_stats[k]for k in TABS),
            tuple(eff_weights.get(k,1.)for k in TABS),
            pt_factor=weights['skill_pt'],after_target_credit=eff_credit,
            failure_limit=failure_limit,risk_aversion_per_percent=risk_value(0.,1.)['preference_cost'],scheduled_transitions=events,
            assumptions=('same_frozen263_kernel_not_new_ranking_algorithm','raw_equivalent_parity_coordinate_not_observed_game_raw',
                'stat_caps_unknown_unbounded_valuation_not_game_cap','condition_on_normal_settlement',
                'explicit_reporter_band_scenario_not_observed_bond','hold_current_paid_levels_no_future_purchase',
                'past_layouts_not_predicted_future_pages','CN_mechanism_and_future_risk_unverified'))
        from core.auto_trainer.larc_attribute_plan import attribute_plan, late_surplus_options
        terminal_options=late_surplus_options({'attribute_weights':dict(zip(TABS,current.importance)),
            'after_target_credit':current.after_target_credit},
            (context.get('larc_period')or{}).get('value')or{},goal)
        if 'far_surplus_credit' in terminal_options:
            current=replace(current,far_surplus_credit=terminal_options['far_surplus_credit'],
                far_surplus_buffer=terminal_options['far_surplus_buffer'],
                assumptions=tuple(a for a in current.assumptions if a!='same_frozen263_kernel_not_new_ranking_algorithm')+
                    ('same263_transitions_with_late_far_surplus_utility_r1',))
        from core.auto_trainer.larc_strategy_extension import active_provider
        from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
        from core.auto_trainer.larc_strategy_rules import zero_card_tabs
        from .next_investment import enabled as next_investment_enabled
        params = params_for_goal(goal)
        current, tail_flags = active_provider().tail_prepare_options(
            params,
            state,
            goal,
            current,
            terminal_options,
            zero_card_tabs(state),
            attribute_plan,
        )
        experimental_tail = unified_larc_enabled(goal)
        research,joint=_joint_rank(current,state,owned,cash if cash_known else None,expectation,
            records,past_risk_annotations or {},scenario,baseline_rows,purchase_options,current_conditional_incomes or {},K,F,S,max_ops,
            cash182=cash182,prefer_current_facility_use=experimental_tail,
            reserve_reference_income=experimental_tail,
            next_facility_investment=next_investment_enabled(goal,current),
            optional_next_facility=experimental_tail and params.arc2_optional_next_facility,
            excluded_future_tabs=zero_card_tabs(state) if params.arc2_exclude_zero_card_future_reserve else frozenset(),
            defer_courage_zero_stamina=experimental_tail and bool(tail_flags.get('defer_courage_zero_stamina')),
            high_cash_monetization=experimental_tail and bool(tail_flags.get('high_cash_monetization')))
        if experimental_tail:
            result['coverage']['terminal_value_policy']={'name':'late-far-surplus-r1'if current.far_surplus_credit is not None else'frozen263',
                'after_target_credit':current.after_target_credit,'far_surplus_credit':current.far_surplus_credit,
                'buffer_minimum':current.far_surplus_buffer,'buffer_target_fraction':.1,'pt_factor':current.pt_factor,
                'exact_buffer':current.far_surplus_exact_buffer,'importance':list(current.importance),
                'deficit_convexity':current.deficit_convexity,'pt_soft_cap':current.pt_soft_cap,
                'pt_over_cap_factor':current.pt_over_cap_factor}
        result['research']=research
        result['finance']=joint['financial_rows']
        result['procurement']=joint
        if cash182:result['strategy182']=deepcopy(joint.get('strategy182'))
        result['coverage'].update(current_training_pages=5,past_menus_used=len(menus),future_steps=len(future),
            past_projection_audit=audits,raw_caps='explicit_coordinate_and_unbounded_scenario',
            terminal_schedule='conditional_JP_reference_not_CN_guarantee',current_rest_omitted_because_originally_rejected=rr is None or is_hard_rejected(rr),
            ranked_baskets=joint['ranked_baskets'],purchase_verified=False,
            financial_fix_active=any(r['current_income_fix_active']for r in joint['financial_rows']),
            future_recovery='unchanged_frozen263_rest50_JP_reference_not_CN_observed')
        result['coverage']['finance']='same_frozen263_cashflow_unknown_income_keeps_original_route_share_prior_no_CN_fixed_income_assumed'
    except (ValueError,KeyError,TypeError,IndexError)as exc:
        result['research']={'active':False,'reason':str(exc),'baseline_current_advice_remains_available':True,'new_stop_required':False}
    return result
