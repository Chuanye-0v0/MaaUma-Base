"""Static normal-data adapter around the pure joint core.

No filesystem, Actor, Native, UI, simulator imports, or module replacement.
The caller owns version gates, recursion, current state and execution.
"""
from functools import lru_cache
from copy import deepcopy
from time import perf_counter
from .engine import select, need, Unsupported, continuation_anchor, continuation_outcomes
from .offers import build_offers
from .cycle_reserve import apply_reserve, _budget as preparation_budget


def _optional_facilities_lose_today(offers, reference, training_phases, income, gate):
    if reference is None or not reference['action_id'].startswith('train:'):
        return False
    best = next(offer for offer in offers
                if offer['action_id'] == reference['action_id'] and not offer['phases'])
    best_income = reference['income']['amount']
    payable = []
    for offer in offers:
        if not offer['eligible'] or training_phases.isdisjoint(offer['phases']):
            continue
        quote = income(offer)
        if gate(offer, quote)['allowed']:
            payable.append((offer, quote))
    return bool(payable) and all(
        best['original_current_score'] > offer['original_current_score'] + offer['expected_purchase_uplift']
        and best['current_risk'] <= offer['current_risk']
        and best_income >= quote['amount']
        for offer, quote in payable
    )


class Quote:
    def __init__(self,purchase_module,*,seconds=25.,states=80000,edges=500000):
        self.m=purchase_module;self.limits=(states,edges,seconds)

    def recommend(self,state,goal,grid,period,legacy,current_rows,*,support_profile=None,
                  income_resolver=None,only_no_buy=False):
        from core.auto_trainer import larc_adaptation as plans
        from core.auto_trainer import larc_adaptation_strategy as adaptation
        from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
        from core.auto_trainer.larc_training_value import gain_value
        from dataclasses import replace
        unified = unified_larc_enabled(goal)
        params = params_for_goal(goal)
        m=self.m;now=m.period_ordinal(period.get('value')or{})
        need(period.get('status')=='verified'and now in(36,37,38,39,41),'verified_firstcamp_period_required')
        need(not grid.get('errors')and grid.get('pending_cost')==0,'current_grid_without_pending_required')
        runs={c.raw_data.get('capture',{}).get('run_session_id')for c in state.training_options}
        need(len(runs)==1 and next(iter(runs)),'single_run_current_pool_required');run=next(iter(runs))
        state=replace(state,scenario_state={**state.scenario_state,'larc_purchase_model_v3':True,
            'larc_expedition_context':{'status':'accepted','run_session_id':run,'through_turn':state.turn,
                'levels':list(grid['levels']),'points':grid['points'],'balance_current':True}})
        if callable(current_rows):current_rows=current_rows(state,goal)
        built=build_offers(state,goal,grid,current_rows,deps={
            'period_ordinal':m.period_ordinal,'build_adaptation_plan':plans.build_adaptation_plan,
            'growth_bundle_phase':plans.growth_bundle_phase,'gains_after_purchase':adaptation.gains_after_purchase,
            'gain_value':gain_value,'training_income':adaptation.training_income})
        need(built.get('supported')is True,built.get('reason','current_offer_builder_unsupported'))
        started=perf_counter();counts={'states':0,'edges':0}
        def tick(states=0,edges=0):
            counts['states']+=states;counts['edges']+=edges
            if(counts['states']>self.limits[0]or counts['edges']>self.limits[1]
                    or perf_counter()-started>self.limits[2]):raise Unsupported('joint_career_budget_exceeded')
        diagnostic=m._diagnostic(state,period,grid['levels'],grid['points'],balance_resources=True,goal=goal)
        deferred=diagnostic['language_debuff_deferred']
        levels = grid['levels']
        power_deferred=unified and params.arc1_defer_niel_power
        eligible=[r for r in current_rows if r.get('rejected')is False]
        base=next((r for r in eligible if r['action_id']==legacy.get('baseline_action')),None)
        if base is None:base=max(eligible,key=lambda r:r['score'])
        share_fn=m.canonical_training_share if unified else m.training_share
        share=share_fn(base);days=m.free_action_days(now+1)
        deck=m._deck_upgrade_context(state,goal,period,support_profile)
        raw_utility,unknown=m._utility_model(state,goal,grid['levels'],days,deck_prior=deck)
        def utility(index,owned):tick();return raw_utility(index,owned)
        feasible_routes = {}
        @lru_cache(maxsize=None)
        def route(scenario,cash,owned):
            tick()
            r=m._route(now,cash,grid['levels'],days,utility,initial=owned,initial_cost=0,current_income=0,
                share=share,branch=scenario,phases=[],allow_language_deferral=deferred,
                balance_resources=True,focused_first_cycle=True,same_day_purchases=True,
                allow_power_deferral=power_deferred,keep_first_arc_cash_route=keep_first_arc_cash_route)
            work=r.get('future_purchase_action_space',{})
            tick(int(work.get('expanded_date_states',0)),int(work.get('enumerated_options',0)))
            # Within this single quote all other future assumptions are common.
            # Replay a previously discovered solvent route with the extra cash;
            # never turn an interrupted route into a completed one.
            key = (scenario, owned)
            best = r
            for prior_cash, prior in feasible_routes.get(key, []):
                if prior_cash > cash:
                    continue
                if (best.get('continuation_feasible') is True and
                    .95 * best['future_value'] - .35 * best['unfunded_estimate'] >=
                    .95 * prior['future_value'] - .35 * prior['unfunded_estimate']):
                    continue
                delta = cash - prior_cash
                best = {
                    **prior,
                    'actions': [
                        {
                            **action,
                            'points_before': round(action['points_before'] + delta, 3),
                            'points_after': round(action['points_after'] + delta, 3),
                        }
                        for action in prior.get('actions', [])
                    ],
                    'points_at_end_estimate': round(prior['points_at_end_estimate'] + delta, 3),
                    'cash_route_reused_from': prior_cash,
                }
            r = best
            if r.get('continuation_feasible') is True:
                feasible_routes.setdefault(key, []).append((cash, deepcopy(r)))
            return deepcopy(r)
        candidates={c.action_id:c for c in state.training_options}
        def income(offer):
            if offer['action_id']=='rest.normal':
                return {'amount':100,'basis':'existing_overseas_rest_reference','observed':False}
            candidate=candidates[offer['action_id']]
            if income_resolver is not None:
                value=income_resolver(candidate,state)
                if value is not None:return value
            amount,basis=adaptation.training_income(candidate)
            return {'amount':amount,'basis':basis,'observed':basis=='observed_training_preview'}
        def gate(offer,quote):
            due=[i for i in range(9)if offer['targets'][i]==1
                and m.mandatory_due(i,now,defer_niel_power=power_deferred)<=now+1
                and not(i==4 and deferred)]
            required=sum(m.LEVEL_TWO_COSTS[i]for i in due)
            item=candidates.get(offer['action_id'])
            credit=m._imminent_training_income_credit(item,quote['amount'])if item is not None else 0.
            allowed=not offer['cost']or grid['points']-offer['cost']+credit>=required
            return {'allowed':allowed,'required_before_next_action':required,
                'known_current_income_credit':credit,'future_income_funds_current_purchase':False}
        reference=None
        if unified:
            reference=continuation_anchor(built['offers'],income)
        # Preserve useful current purchases before comparing the reserve route.
        keep_first_arc_cash_route = (
            unified
            and params.arc1_keep_cash_route
            and levels[5] == 3
            and 3 in levels[:5]
            and (
                not params.arc1_optional_facility_requires_current_best
                or _optional_facilities_lose_today(
                    built['offers'], reference, plans.TRAINING_PHASE_SLOTS.keys(), income, gate)
            )
        )
        def preparation_gap(offer,quote):
            success=preparation_budget(offer,now,grid['points'],share,deferred,quote['amount'],
                                       defer_niel_power=power_deferred)
            rate=offer['current_risk']/100.
            failure=preparation_budget(offer,now,grid['points'],share,deferred,0,
                                       defer_niel_power=power_deferred)if rate else success
            return ((1-rate)*success['preparation_shortfall']+rate*failure['preparation_shortfall'])
        from core.auto_trainer.larc_adaptation_schedule import income_at
        from core.auto_trainer.larc_strategy_extension import active_provider
        from .cycle_reserve import RACE_COMPLETION_AP

        provider = active_provider()
        wisdom_ctx = (
            provider.quote_wisdom_gate_prepare(
                params=params,
                unified=unified,
                now=now,
                levels=levels,
                grid=grid,
                share=share,
                built=built,
                base=base,
                income=income,
                m=m,
                income_at=income_at,
                race_completion_ap=RACE_COMPLETION_AP,
            )
            if unified
            else None
        )
        # Compute lower-cash roots first, independent of offer ordering, so
        # every higher-cash candidate can replay the lower discovered route.
        roots = set()
        _selected_pool = []
        for offer in built['offers']:
            if offer.get('eligible') is not True or (only_no_buy and offer['phases']):
                continue
            q = income(offer)
            if not gate(offer, q)['allowed']:
                continue
            if wisdom_ctx is not None:
                wg = provider.quote_wisdom_gate_check(
                    wisdom_ctx,
                    offer,
                    q,
                    levels=levels,
                    candidates=candidates,
                    state=state,
                    goal=goal,
                    grid=grid,
                    now=now,
                    m=m,
                )
                if wg is not None and wg.get('active') and not wg.get('allowed'):
                    continue
            _selected_pool.append(offer)
            cash = grid['points'] - offer['cost']
            owned = tuple(offer['targets'])
            for _success, _probability, ap in continuation_outcomes(offer,q,reference):
                roots.add((owned,cash+ap))
        for owned, cash in sorted(roots):
            for scenario in ('conservative', 'ordinary'):
                route(scenario, cash, owned)
        result=select(_selected_pool,current_cash=grid['points'],current_levels=grid['levels'],
            now=now,route=route,income_quote=income,current_gate=gate,only_no_buy=only_no_buy,
            continuation_reference=reference,current_preparation_gap=preparation_gap)
        def _apply_early_pt50(res):
            if not (
                unified
                and params.protect_early_pt50_on_fallback
                and not only_no_buy
                and now in (36, 37, 38, 39)
                and levels[5] in (1, 2)
            ):
                return
            sel = res['selected']
            if 'expedition_skill_pt' not in sel['phases'] and (sel['phases'] or sel['action_id'] != 'rest.normal'):
                sel_cand = candidates.get(sel['action_id'])
                sel_is_rainbow = bool(sel['phases'] and sel_cand is not None and sel_cand.friendship)
                no_buy_score = res['NoBuy']['score']
                no_buy_aid = res['NoBuy']['action_id']

                def _early_pt_solvent(row):
                    if 'expedition_skill_pt' not in row['phases']:
                        return False
                    if row['score'] <= no_buy_score + 1e-6:
                        return False
                    return row.get('current_preparation_gap', 0.0) <= 1e-6

                if not sel_is_rainbow:
                    pt_rows = [
                        r for r in res['ranking']
                        if _early_pt_solvent(r)
                        and (
                            r['phases'] == ['expedition_skill_pt']
                            or r['action_id'] == no_buy_aid
                            or (candidates.get(r['action_id']) is not None and candidates[r['action_id']].friendship)
                        )
                    ]
                    if not sel['phases']:
                        if pt_rows:
                            best_pt = pt_rows[0]
                            res['selected'] = best_pt
                            res['ranking'] = [best_pt, *(r for r in res['ranking'] if r is not best_pt)]
                            res['selection_reason'] = 'first_expedition_early_pt50_priority'
                    else:
                        same_action_bundles = [
                            r for r in res['ranking']
                            if _early_pt_solvent(r)
                            and r['action_id'] == sel['action_id']
                            and set(sel['phases']).issubset(r['phases'])
                            and r['score'] >= sel['score'] - 5.0
                        ]
                        if same_action_bundles:
                            best_pt = same_action_bundles[0]
                            res['selected'] = best_pt
                            res['ranking'] = [best_pt, *(r for r in res['ranking'] if r is not best_pt)]
                            res['selection_reason'] = 'first_expedition_bundle_with_pt50'
                        elif pt_rows and pt_rows[0]['score'] >= sel['score'] - 15.0:
                            best_pt = pt_rows[0]
                            res['selected'] = best_pt
                            res['ranking'] = [best_pt, *(r for r in res['ranking'] if r is not best_pt)]
                            res['selection_reason'] = 'first_expedition_early_pt50_priority'
                else:
                    same_rainbow_bundles = [
                        r for r in res['ranking']
                        if _early_pt_solvent(r)
                        and r['action_id'] == sel['action_id']
                        and set(sel['phases']).issubset(r['phases'])
                        and r['score'] >= sel['score'] - 5.0
                    ]
                    if same_rainbow_bundles:
                        best_pt = same_rainbow_bundles[0]
                        res['selected'] = best_pt
                        res['ranking'] = [best_pt, *(r for r in res['ranking'] if r is not best_pt)]
                        res['selection_reason'] = 'first_expedition_rainbow_bundle_with_pt50'

        _apply_early_pt50(result)
        if wisdom_ctx is not None:
            provider.quote_wisdom_gate_finalize(
                wisdom_ctx,
                result=result,
                built=built,
                only_no_buy=only_no_buy,
                income=income,
                gate=gate,
                continuation_outcomes=continuation_outcomes,
                reference=reference,
                grid=grid,
                roots=roots,
                route=route,
                select=select,
                now=now,
                preparation_gap=preparation_gap,
                apply_early_pt50_fn=_apply_early_pt50,
            )
        if unified:
            apply_reserve(result, now=now, cash=grid['points'], share=share, defer_language=deferred,
                          current_rows=eligible,defer_niel_power=power_deferred, state=state, goal=goal)
        result.update(calendar=now,run_session_id=run,turn_index=state.turn,
            future_training_days=list(days),future_unknown=unknown,common_training_share=share,
            share_source_action=base['action_id'],language_debuff_deferred=deferred,
            power_debuff_deferred=power_deferred and grid['levels'][2]==1,power_due_ordinal=42,
            budget={**counts,'seconds':perf_counter()-started,'limits':list(self.limits)})
        return result
