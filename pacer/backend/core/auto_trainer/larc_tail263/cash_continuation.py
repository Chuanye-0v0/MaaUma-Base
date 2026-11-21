"""Pure n7b tail cash continuation with the complete final_arc0..9 plan.

Current basket is paid once from today's cash. Known current/past-menu AP
scenarios are followed on separate success/failure branches. The complete original
terminal0..9 plan is budgeted; no future training-facility investment is added.
"""
from copy import deepcopy
from dataclasses import replace
from math import isclose, isfinite
import sys

VERSION = 'tail-cash-continuation-full-final-plan-n7b.v1'
POLICY = 'conditional_cash_terminal_0_through_9_no_future_facility_investment'
DAYS = (60, 61, 62, 63, 65)
FINAL_EVENT = 'final_race_and_ending_base_no_free_future_purchase'
EVENT_FIELDS = ('calendar','stats_gain','stats_unit','pt','hp_points','cap_points','label','condition','basis')


class _Unavailable(ValueError): pass
class _BudgetExceeded(RuntimeError): pass


def _number(value): return type(value) in (int,float) and isfinite(value)
def _amount(value): return type(value) is int and value >= 0

def _need(condition, reason):
    if not condition: raise _Unavailable(reason)


def _fallback(legacy, reason, operations=0):
    result = deepcopy(legacy)
    result['cash_continuation'] = dict(version=VERSION,active=False,reason=reason,
        whole_pool_legacy_retained=True,new_stop_requirement=False,new_action_rejections=False,
        operations=operations,Env_access=False,base_scorer_calls=0,
        hypothetical_only=True,execution_confirmed=False,ledger_written=False)
    return result


def _schedule(events, after):
    _need(isinstance(events,(tuple,list)), 'schedule_unknown')
    rows = []
    for event in events:
        _need(all(hasattr(event,k) for k in EVENT_FIELDS), 'schedule_fields_unknown')
        _need(type(event.calendar) is int and event.calendar in (59,64,66,67), 'schedule_calendar_unknown')
        _need(len(event.stats_gain)==5 and all(_number(x) for x in event.stats_gain)
            and event.stats_unit in ('raw','display')
            and all(_number(getattr(event,k)) for k in ('pt','hp_points','cap_points')), 'schedule_units_unknown')
        if event.calendar > after: rows.append(event)
    return tuple(rows)


def _signature(events):
    return tuple(tuple(tuple(getattr(e,k)) if k=='stats_gain' else getattr(e,k)
        for k in EVENT_FIELDS) for e in events)


def _project_purchases(levels, cash, events):
    """Original final_arc order0..9; defer required0..8, optional9 last.

    Any required deferral suppresses9, but never stops a later affordable
    required upgrade. Lv0 stays deferred: no invented unlock or price.
    """
    levels=list(levels);rows=[];deferred=[]
    for event in events:
        slot=event['slot'];before=levels[slot];cost=event['cost'];cash_before=cash
        required=slot<9
        if before >= 2: status='already_owned_skip'
        elif slot==9 and deferred: status='optional_skip_prior_deferred'
        elif not event['original_condition_met']:
            status='required_deferred_original_condition' if required else 'optional_skip_original_condition'
            if required:deferred.append(slot)
        elif before != 1:
            status='required_deferred_non_level1' if required else 'optional_skip_non_level1'
            if required:deferred.append(slot)
        elif cash < cost:
            status='required_deferred_unfunded' if required else 'optional_skip_unfunded'
            if required:deferred.append(slot)
        else:
            cash -= cost;levels[slot]=2;status='hypothetical_purchase_projected'
        rows.append(dict(slot=slot,from_level=before,projected_level=levels[slot],
            declared_cost=cost,cash_before=cash_before,cash_after=cash,status=status,
            projected_cost_deducted=cash_before-cash))
    return dict(projected_levels=levels,final_level=levels[9],remaining_cash=cash,
        events=rows,deferred_required_slots=deferred,
        hypothetical_only=True,execution_confirmed=False,actual_purchase_claimed=False,
        ledger_written=False,extra_upgrade_reward_credit=0)


def _check_purchase_events(events):
    _need(isinstance(events,(tuple,list)) and len(events)==10
        and all(isinstance(e,dict) for e in events)
        and [e.get('slot') for e in events]==list(range(10)),'original_final_arc_0_through_9_plan_unknown')
    _need(all(e.get('target_level')==2 and type(e.get('target_level')) is int
        and _amount(e.get('cost')) and type(e.get('original_condition_met')) is bool
        for e in events),'original_price_or_condition_unknown')


def _check_deferred(current, levels, deferred):
    from core.auto_trainer.larc_adaptation import TRAINING_UPGRADES, selected_upgrade_cost
    _need(isinstance(deferred,dict) and current.calendar in (60,61,62)
          and current.future_free_days and current.future_free_days[0]==current.calendar+1,
          'deferred_facility_requires_next_free_day')
    slot, cost, action = (deferred.get(key) for key in ('slot','cost','matching_action_id'))
    targets, menus = deferred.get('levels'), deferred.get('menus')
    matching = next((f'future:{tab}' for tab, (_, index) in TRAINING_UPGRADES.items() if index==slot), None)
    _need(type(slot) is int and matching is not None and action==matching and _amount(cost) and cost>0
          and isinstance(targets,(tuple,list)) and len(targets)==10 and all(type(level) is int for level in targets)
          and levels[slot] in (1,2)
          and targets[slot]==3 and all(a==b for i,(a,b) in enumerate(zip(levels,targets)) if i!=slot)
          and selected_upgrade_cost(levels,targets)==cost,'deferred_facility_scope_or_price_mismatch')
    _need(isinstance(menus,(tuple,list)) and len(menus)==len(current.menus), 'deferred_facility_menu_count_mismatch')
    for old,new in zip(current.menus,menus):
        _need((old.source_id,old.source_calendar)==(new.source_id,new.source_calendar)
              and len(old.actions)==len(new.actions),'deferred_facility_menu_source_mismatch')
        for a,b in zip(old.actions,new.actions):
            fields=('action_id','kind','training_type','p_percent','anchor_hp','public_training_level','allowed')
            _need(all(getattr(a,key)==getattr(b,key) for key in fields),'deferred_facility_changed_menu_risk_or_identity')
    _check_purchase_events(deferred.get('purchase_events'))


class _CashEvaluator:
    def __init__(self,current,fixed,budget,levels,events,schedule_for_level,
                 current_income,future_income,fixed_income,deferred_facility=None):
        self.c=current;self.fixed=fixed;self.base=fixed.Evaluator(current,budget)
        self.levels=levels;self.events=events;self.schedule_for_level=schedule_for_level
        self.current_income=current_income;self.future_income=future_income
        self.fixed_income=fixed_income;self.cache={};self.schedules={}
        self.deferred=deferred_facility
        self.retry_second=deferred_facility is not None and deferred_facility.get('retry_second',True) is True
        self.retry_values={}
        self.original_schedule=_schedule(current.scheduled_transitions,current.calendar)
        old=self.schedule(levels[9])
        _need(_signature(old)==_signature(self.original_schedule),'original_schedule_not_same_current')

    @property
    def ops(self): return self.base.ops

    def tick(self):
        try:self.base.tick()
        except self.fixed.BudgetExceeded as error:raise _BudgetExceeded from error

    def actions(self,state,menu,day):return self.base.actions(state,menu,day)

    def schedule(self,level):
        if level not in self.schedules:
            events=_schedule(self.schedule_for_level(level),self.c.calendar)
            original=self.original_schedule
            _need(len(events)==len(original),'branch_schedule_changed_event_count')
            _need(sum(e.label==FINAL_EVENT for e in events)==1,'final_reward_event_not_identified')
            for old,new in zip(original,events):
                mutable=('stats_gain','pt') if old.label==FINAL_EVENT else ()
                _need(all(getattr(old,k)==getattr(new,k) for k in EVENT_FIELDS if k not in mutable),
                      'branch_schedule_changed_non_final_contract')
            self.schedules[level]=events
        return self.schedules[level]

    def income(self,action,menu=None):
        if menu is None:
            return self.current_income[action.action_id]['conditional_amount']
        amounts=self.future_income.get(menu.source_id)
        _need(isinstance(amounts,dict) and _amount(amounts.get(action.action_id)),
              'past_menu_conditional_income_unknown:'+menu.source_id+':'+action.action_id)
        return amounts[action.action_id]

    def cash_between(self,cash,after,through):
        return cash+sum(e['amount'] for e in self.fixed_income if after<e['calendar']<=through)

    def advance(self,state,cash,after,through):
        return (self.fixed.apply_scheduled(state,self.c,after,through),
                self.cash_between(cash,after,through))

    def terminal(self,state,cash,after,paid=False):
        # Future fixed cash is conditional on the same declared normal path.
        # Applied only once in this branch's disjoint remaining interval.
        cash_before=self.cash_between(cash,after,65)
        levels,events=(self.deferred['levels'],self.deferred['purchase_events']) if paid else (self.levels,self.events)
        projection=_project_purchases(levels,cash_before,events)
        branch=replace(self.c,scheduled_transitions=self.schedule(projection['final_level']))
        end=self.fixed.apply_scheduled(state,branch,after,67)
        value=self.fixed.utility(end,branch)
        _need(_number(value),'terminal_utility_unknown')
        projection.update(cash_before_terminal_purchases=cash_before,
            normal_schedule_final_level=projection['final_level'],
            normal_schedule_still_conditional_on_final_win_and_settlement=True)
        return value,end,projection

    def immediate(self,state,cash,action,day,menu,paid=False):
        self.tick();p=action.p_percent/100.;income=self.income(action,menu)
        value=0.
        for probability,ok in ((1-p,True),(p,False)):
            next_state=(self.fixed.success if ok else self.fixed.failed)(state,action)
            amount=cash+(income if ok else 0)
            terminal,_,_=self.terminal(next_state,amount,day,paid)
            value+=probability*terminal
        return value-self.c.risk_aversion_per_percent*action.p_percent

    def second_day_value(self,state,cash,day,menu_index):
        """Value the second purchase opportunity after one simulated menu arrives."""
        key=(state,cash,day,menu_index)
        if key not in self.retry_values:
            menu=self.c.menus[menu_index]
            hold=max(self.immediate(state,cash,a,day,menu) for a in self.actions(state,menu,day))
            cost=self.deferred['cost']
            if cash>=cost:
                paid_menu=self.deferred['menus'][menu_index]
                best,value=max(((a,self.immediate(state,cash-cost,a,day,paid_menu,True))
                    for a in self.actions(state,paid_menu,day)),key=lambda quote:(quote[1],quote[0].action_id))
                if best.action_id==self.deferred['matching_action_id'] and value>hold:
                    hold=value
            self.retry_values[key]=hold
        return self.retry_values[key]

    def choose(self,state,cash,menu,index,paid=False):
        # Cash is physical branch state, not expected cash or an omitted key.
        key=(state,cash,menu.source_id,index,paid)
        if key in self.cache:return self.cache[key]
        day=self.c.future_free_days[index];options=self.actions(state,menu,day)
        menus=self.deferred['menus'] if paid else self.c.menus
        def q(action):
            if index+1==len(self.c.future_free_days):
                return self.immediate(state,cash,action,day,menu,paid)
            p=action.p_percent/100.;income=self.income(action,menu);result=0.
            for probability,ok in ((1-p,True),(p,False)):
                if not probability:continue
                next_state=(self.fixed.success if ok else self.fixed.failed)(state,action)
                next_cash=cash+(income if ok else 0)
                next_day=self.c.future_free_days[index+1]
                ready,money=self.advance(next_state,next_cash,day,next_day-1)
                if index==0 and not paid and self.retry_second:
                    value=sum(self.second_day_value(ready,money,next_day,j) for j in range(len(menus)))
                else:
                    value=sum(max(self.immediate(ready,money,b,next_day,m,paid)
                        for b in self.actions(ready,m,next_day)) for m in menus)
                result+=probability*value/len(menus)
            return result-self.c.risk_aversion_per_percent*action.p_percent
        chosen=max(((a,q(a)) for a in options),key=lambda quote:(quote[1],quote[0].action_id))
        self.cache[key]=chosen
        return chosen

    def rollout(self,state,cash,path,stratum):
        route=[];aversion=0.;previous=self.c.calendar;n=len(self.c.menus);paid=False
        for index,day in enumerate(self.c.future_free_days):
            state,cash=self.advance(state,cash,previous,day-1)
            menu_index=(path+index)%n
            menu=(self.deferred['menus'] if paid else self.c.menus)[menu_index]
            action,hold_q=self.choose(state,cash,menu,index,paid)
            decision=None
            if self.deferred is not None and not paid and (index==0 or index==1 and self.retry_second):
                cost=self.deferred['cost']; payable=cash>=cost; matches=False
                if payable:
                    self.tick()
                    paid_menu=self.deferred['menus'][menu_index]
                    paid_action,pay_q=self.choose(state,cash-cost,paid_menu,index,True)
                    matches=paid_action.action_id==self.deferred['matching_action_id']
                    if matches and pay_q>hold_q:
                        paid=True;cash-=cost;menu=paid_menu;action=paid_action
                decision={'payable':payable,'paid_basket_matches':matches,'paid':paid,'cost':cost if paid else 0,
                    'matching_training':action.action_id==self.deferred['matching_action_id']}
            u=((stratum+.5)/4.+index*.38196601125)%1.
            ok=u>=action.p_percent/100.
            amount=self.income(action,menu);before=cash;hp_before=state.hp
            state=(self.fixed.success if ok else self.fixed.failed)(state,action)
            cash+=amount if ok else 0
            route.append(dict(day=day,menu=menu.source_id,action=action.action_id,
                risk=action.p_percent,hp_before=hp_before,success_quadrature=ok,cash_before=before,cash_after=cash,
                conditional_income=amount,applied_income=amount if ok else 0,
                income_basis='strict_past_menu_conditional_AP_scenario_not_future_observation',
                **({'deferred_facility':decision} if decision is not None else {})))
            aversion+=self.c.risk_aversion_per_percent*action.p_percent;previous=day
        value,end,projection=self.terminal(state,cash,previous,paid)
        return value-aversion,route,end,projection


def rank_cash_current(current,*,owned_levels,candidate_levels,cash_before_candidate_cost,
        candidate_cost,income_by_action,income_by_menu,fixed_income_events,purchase_events,
        schedule_for_level,legacy_result,pure_math=None,max_ops=250000,deferred_facility=None):
    """Replace same-current tail numeric value; current/forecast cash stays split.

    income_by_action: {id:{status:'accepted',conditional_amount:int,p_percent:n}}.
    income_by_menu: {JointMenu.source_id:{future_action_id:int}}, including rest.
    fixed_income_events: explicit [{calendar:64,amount:int}], only future64.
    deferred_facility: optional validated next-day projection with its own menus,
        levels and terminal purchase events. No future AP pays the current basket.
    Missing inputs/compute budget retain the whole legacy pool, never stop play.
    """
    ev=None
    try:
        _need(isinstance(legacy_result,dict) and legacy_result.get('active') is True,'original_rank_not_active')
        _need(current.calendar in DAYS,'outside_tail_free_action_scope')
        expected=DAYS[DAYS.index(current.calendar)+1:]
        _need(current.future_free_days==expected,'future_free_actions_not_original_tail_suffix')
        fixed=pure_math if pure_math is not None else sys.modules.get(type(current).__module__)
        _need(fixed is not None and all(callable(getattr(fixed,k,None)) for k in
            ('Evaluator','success','failed','apply_scheduled','terminal_utility','utility','display')),
            'pure_math_not_supplied')
        _need(type(max_ops) is int and max_ops>=0,'compute_budget_unknown')
        for levels in (owned_levels,candidate_levels):
            _need(isinstance(levels,(tuple,list)) and len(levels)==10 and all(type(x) is int
                and 0<=x<=(3 if i<8 else 2) for i,x in enumerate(levels)),'owned_or_candidate_levels_unknown')
        _need(all(a<=b for a,b in zip(owned_levels,candidate_levels)),'candidate_levels_reduce_owned')
        _need(_amount(cash_before_candidate_cost) and _amount(candidate_cost),'cash_or_current_cost_unknown')
        _need(candidate_cost<=cash_before_candidate_cost,'current_basket_not_funded_by_current_cash')
        cash=cash_before_candidate_cost-candidate_cost
        _check_purchase_events(purchase_events)
        if deferred_facility is not None:
            _check_deferred(current,candidate_levels,deferred_facility)
            _need(type(deferred_facility.get('retry_second',True)) is bool,'deferred_retry_flag_unknown')
        _need(callable(schedule_for_level),'schedule_callback_unknown')
        _need(isinstance(income_by_action,dict) and isinstance(income_by_menu,dict),'income_maps_unknown')
        eligible=[a for a in current.actions if a.allowed];ids=[a.action_id for a in eligible]
        rows=legacy_result.get('ranking')
        _need(bool(ids) and len(ids)==len(set(ids)) and isinstance(rows,list)
            and all(isinstance(r,dict) for r in rows) and len(rows)==len(ids)
            and set(r.get('action_id') for r in rows)==set(ids),'original_eligible_pool_mismatch')
        old_by={r['action_id']:r for r in rows}
        for action in eligible:
            record=income_by_action.get(action.action_id)
            _need(isinstance(record,dict) and record.get('status')=='accepted'
                and _amount(record.get('conditional_amount')) and _number(record.get('p_percent'))
                and _number(action.p_percent) and 0<=action.p_percent<=100
                and isclose(record['p_percent'],action.p_percent,abs_tol=1e-9),'same_action_conditional_income_unknown')
            _need(all(_number(old_by[action.action_id].get(k)) for k in
                ('score','current_residual','current_risk_aversion')),'original_score_components_unknown')
        _need(not expected or len(current.menus) in (4,5),'original_joint_menus_missing')
        fixed_events=[];seen=set()
        _need(isinstance(fixed_income_events,(list,tuple)),'fixed_income_events_unknown')
        for event in fixed_income_events:
            _need(isinstance(event,dict) and event.get('calendar')==64 and _amount(event.get('amount')),
                  'fixed_income_not_declared64')
            _need(event['calendar'] not in seen,'duplicate_fixed64_income')
            seen.add(event['calendar'])
            if current.calendar<event['calendar']:fixed_events.append(deepcopy(event))
        ev=_CashEvaluator(current,fixed,max_ops,list(candidate_levels),deepcopy(purchase_events),
            schedule_for_level,deepcopy(income_by_action),deepcopy(income_by_menu),fixed_events,deferred_facility)
        baseline=fixed.terminal_utility(current.state,current,current.calendar)
        _need(_number(baseline),'common_baseline_unknown')
        output=[];paths=len(current.menus) if expected else 0
        for action in eligible:
            old=old_by[action.action_id];p=action.p_percent/100.;value=0.;mean=[0.]*7
            branches=[];representative=None;direct=0.
            deferred_paid=deferred_match=deferred_paid_basket_match=deferred_cost=0.
            first_paid=second_paid=paid_training_share=0.
            if not expected:ev.tick()
            for ok,probability in ((True,1-p),(False,p)):
                next_state=(fixed.success if ok else fixed.failed)(current.state,action)
                branch_cash=cash+(income_by_action[action.action_id]['conditional_amount'] if ok else 0)
                direct_value,direct_end,direct_projection=ev.terminal(next_state,branch_cash,current.calendar)
                direct+=probability*direct_value
                terminal_values=[];level_counts={};branch_representative=None
                if expected and probability:
                    for path in range(paths):
                        for stratum in range(4):
                            result,route,end,projection=ev.rollout(next_state,branch_cash,path,stratum)
                            if deferred_facility is not None:
                                decision=route[0]['deferred_facility'];weight=probability/(paths*4.)
                                retry=(route[1].get('deferred_facility') or {}) if ev.retry_second and len(route)>1 else {}
                                first_paid+=weight*decision['paid'];second_paid+=weight*retry.get('paid',False)
                                deferred_paid+=weight*(decision['paid'] or retry.get('paid',False))
                                deferred_match+=weight*decision['matching_training']
                                deferred_paid_basket_match+=weight*decision['paid_basket_matches']
                                deferred_cost+=weight*(decision['cost']+retry.get('cost',0))
                                if decision['paid'] or retry.get('paid',False):
                                    paid_training_share+=weight*sum(step['action'].startswith('future:')
                                        and step['action']!='future:rest' for step in route)/len(route)
                            terminal_values.append(result)
                            level_counts[projection['final_level']]=level_counts.get(projection['final_level'],0)+1
                            for i,x in enumerate([*(fixed.display(x) for x in end.raw),end.pt,end.hp]):
                                mean[i]+=probability*x/(paths*4.)
                            if branch_representative is None:branch_representative=dict(route=route,
                                terminal_display=[fixed.display(x) for x in end.raw],terminal_pt=end.pt,
                                terminal_hp=end.hp,terminal_projection=projection)
                    value+=probability*sum(terminal_values)/len(terminal_values)
                elif not expected:
                    value+=probability*direct_value;level_counts={direct_projection['final_level']:1}
                    for i,x in enumerate([*(fixed.display(x) for x in direct_end.raw),direct_end.pt,direct_end.hp]):
                        mean[i]+=probability*x
                    branch_representative=dict(route=[],terminal_display=[fixed.display(x) for x in direct_end.raw],
                        terminal_pt=direct_end.pt,terminal_hp=direct_end.hp,terminal_projection=direct_projection)
                branches.append(dict(outcome='success' if ok else 'failure',probability=probability,
                    cash_after_current_action=branch_cash,current_conditional_income=income_by_action[action.action_id]['conditional_amount'],
                    current_income_applied=income_by_action[action.action_id]['conditional_amount'] if ok else 0,
                    terminal_level_scenario_counts=level_counts,representative=branch_representative,
                    hypothetical_only=True,execution_confirmed=False))
                if representative is None and probability:representative=branch_representative
            numeric=value-baseline;direct-=baseline
            row=deepcopy(old)
            row.update(score=numeric+old['current_residual']+old['current_risk_aversion'],
                unified_terminal_delta=numeric,current_direct_expected_terminal_utility=direct,
                future_contribution_including_future_aversion=numeric-direct,
                mean_terminal_display=mean[:5],mean_terminal_pt=mean[5],mean_terminal_hp=mean[6],
                legacy_hold_levels_score=old['score'],cash_continuation_delta=numeric-old['unified_terminal_delta'],
                current_cash_outcome_branches=branches,representative_not_forecast=representative)
            if deferred_facility is not None:
                row['deferred_facility']=dict(paid_probability=deferred_paid,expected_cost=deferred_cost,
                    matching_training_probability=deferred_match,paid_basket_matching_probability=deferred_paid_basket_match)
                if ev.retry_second:
                    row['deferred_facility'].update(first_paid_probability=first_paid,second_paid_probability=second_paid,
                        paid_path_training_share=paid_training_share/deferred_paid if deferred_paid else None)
            output.append(row)
        result=deepcopy(legacy_result)
        result.update(ranking=sorted(output,key=lambda row:row['score'],reverse=True),operations=ev.ops,
            cache_states=len(ev.cache),future_purchase_policy=POLICY,
            legacy_future_purchase_policy=legacy_result.get('future_purchase_policy'),
            legacy_assumptions=deepcopy(legacy_result.get('assumptions')),
            assumptions=('condition_on_final_win_and_normal_settlement',
                'current_basket_payable_from_current_cash_only',
                'strict_past_menu_conditional_AP_scenario_not_future_observation',
                'branch_cash_not_expected_cash_before_terminal0_through9',
                'same_legacy_common_terminal_baseline_once'),
            common_terminal_baseline_utility=baseline,
            scheduled_transitions_role='legacy_common_baseline_only_branch_final_level_replaced_once')
        result['cash_continuation']=dict(version=VERSION,active=True,operations=ev.ops,
            current_cash_before_candidate_cost=cash_before_candidate_cost,candidate_cost_deducted_once=candidate_cost,
            current_cash_after_candidate_cost=cash,conditional_fixed_income_events=fixed_events,
            future_income_basis='strict_past_menu_conditional_AP_scenario_not_future_observation',
            current_success_failure_cash_mixed_only_after_terminal_utility=True,
            future_facility_investments_modelled=False,common_baseline_revalued=False,
            future_steps=len(expected),scenario_paths=paths,outcome_strata=4 if expected else 0,
            policy_lookahead=2,Env_access=False,base_scorer_calls=0,execution_confirmed=False,
            ledger_written=False,hypothetical_only=True,new_stop_requirement=False)
        if deferred_facility is not None:
            result['future_purchase_policy']='conditional_next_facility_only_if_used'
            result['cash_continuation'].update(future_facility_investments_modelled=True,
                deferred_facility={'calendar':expected[0],'cost':deferred_facility['cost'],
                                   'matching_action_id':deferred_facility['matching_action_id']})
            if ev.retry_second and len(expected)>1:
                result['cash_continuation']['deferred_facility']['retry_calendar']=expected[1]
        return result
    except _BudgetExceeded:
        return _fallback(legacy_result,'whole_pool_compute_budget_exceeded',ev.ops if ev else 0)
    except Exception as error:
        return _fallback(legacy_result,str(error),ev.ops if ev else 0)
