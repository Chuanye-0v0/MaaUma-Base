"""Current earned-SS lower bound and alternative resource valuation, never input.

This module consumes already accepted public facts. It never promotes a future
offer to observed data, changes a wallet, or imports a simulator/environment.
"""

from core.auto_trainer.larc_participant_evidence import training_rows
from copy import deepcopy
import math

ATTRS=('speed','stamina','power','guts','wisdom')
# Conservative coverage: include the whole current SS-progress block, even
# overflow/random progress not necessarily redeemed by this one estimated SS.
# The alternative never deletes the old side; it must beat all this credit.
PROXY_COMPONENTS=('net_charge','newly_full','ss_preparation',
    'cycle_reward_preparation','charming_charge_progress',
    'future_recovery_cycle_progress','mei_expected_charge')


def number(value):return type(value)in(int,float)and math.isfinite(value)


def earned_qualification(state,candidate,valuation,*,calendar):
    result={'eligible':False,'calendar':calendar,'conditional_on_current_training_success':True,
        'max_new_groups':1,'random_mei_charge_in_qualification':False,'future_win_probability':None}
    if type(calendar)is not int or not 43<=calendar<=55:
        return {**result,'reason':'outside_ordinary_domestic_scope'}
    source=(valuation.get('evidence')or{}).get('training_source')or{}
    run,turn=source.get('run_session_id'),source.get('turn_index')
    if not isinstance(run,str)or not run or type(turn)is not int or turn!=state.turn:
        return {**result,'reason':'current_training_source_unavailable'}
    entry=state.scenario_state.get('larc_ss_entry')or{}
    es=entry.get('source')or{}
    ready=entry.get('ready_count')
    if not(entry.get('status')=='accepted'and entry.get('stage')=='entry'
        and es.get('run_session_id')==run and es.get('turn_index')==turn
        and type(ready)is int and 0<=ready<=5):
        return {**result,'reason':'current_ready_lower_bound_unavailable'}
    result.update(run_session_id=run,source_turn=turn,ready_before=ready)
    if ready==5:return {**result,'reason':'current_held_group_not_a_second_generated_group'}
    rate=candidate.failure_rate
    if not number(rate)or not 0<=rate<=100:
        return {**result,'reason':'current_training_failure_rate_unknown'}
    packet=(candidate.scenario_mechanics.get('larc')or{}).get('training_participants')or{}
    ps=packet.get('source')or{}
    view = training_rows(candidate, typed=True)
    if view is None or not all(ps.get(k)==source.get(k)for k in('run_session_id','turn_index','sha256')):
        return {**result,'reason':'current_participant_packet_unbound'}
    people = view[0]
    seen={};new=[];full=[]
    for person in people:
        mid=(person.get('member_ref')or{}).get('member_id')
        charge=person.get('charge')or{}
        if not isinstance(mid,str)or not mid:continue
        values=(charge.get('filled_cells'),charge.get('preview_cells'),charge.get('capacity'))
        if mid in seen and seen[mid]!=values:
            return {**result,'reason':'conflicting_member_battery'}
        if mid in seen:continue
        seen[mid]=values
        filled,delta,cap=values
        if not(charge.get('status')=='accepted'and type(filled)is int and type(delta)is int
            and type(cap)is int and cap==3 and 0<=filled<=3 and 0<=delta<=3-filled):continue
        if filled==3:full.append(mid)
        elif filled+delta==3:new.append(mid)
    if len(full)>ready:
        return {**result,'reason':'known_full_conflicts_with_current_entry'}
    lower=ready+len(new)
    result.update(newly_full_member_ids=new,known_already_full_ids=full,
        success_ready_lower_bound=min(5,lower),failure_rate=rate,
        current_training_success_probability=1-rate/100)
    return {**result,'eligible':lower>=5,'reason':('identified_success_lower_bound_crosses_five'
        if lower>=5 else'not_enough_identified_newly_full_members')}


def estimate_asset(state,goal,prior,current_public,reference,gain_value,*,qualification_only=False):
    """Same-current-state revaluation for every candidate, not next roster truth."""
    out={'available':False,'future_win_probability':None,'AP_credit':0,'fan_credit':0,
         'capacity_credit':0,'qualification_only':bool(qualification_only)}
    if prior.get('available')is not True:return {**out,'reason':'same_phase_executed_prior_unavailable'}
    raw=prior.get('stats_raw_applied_gain');points=prior.get('skill_pt_gain')
    hp=prior.get('energy_delta_points')
    current=current_public.get('stats_raw');caps=current_public.get('stat_caps_raw')
    if not(isinstance(raw,list)and len(raw)==5 and all(type(v)is int and v>=0 for v in raw)
        and type(points)is int and points>=0 and number(hp)and hp>=0
        and isinstance(current,list)and len(current)==5 and isinstance(caps,list)and len(caps)==5
        and all(number(v)for v in current+caps)
        and all(number(state.current_stats.get(k))for k in ATTRS)):
        return {**out,'reason':'prior_or_public_cap_units_unavailable'}
    discounts=[r.get('forecast_discount')for layout in reference.get('opportunity_layouts',[])
        for r in layout if isinstance(r,dict)and number(r.get('forecast_discount'))
        and 0<r['forecast_discount']<=1]
    if not discounts:return {**out,'reason':'shared_future_numeric_discount_unavailable'}
    discount=min(discounts)
    bounded=[min(g,max(0,cap-now))for g,now,cap in zip(raw,current,caps)]
    gains={}
    for key,g in zip(ATTRS,bounded):
        # Floor a policy estimate in the existing displayed-gain API. The
        # actual past applied raw input is never reverse-expanded to request.
        below=min(g,max(0,1200-state.current_stats[key]))
        gains[key]=int(math.floor(below+(g-below)/2))
    gains['skill_pt']=points
    numeric=gain_value(state,gains,goal,project=False)['score']*discount
    if not number(numeric)or numeric<0:return {**out,'reason':'shared_numeric_valuation_unavailable'}
    return {**out,'available':True,'reason':'strict_past_conditional_resource_estimate',
        'numeric_value':0. if qualification_only else round(numeric,8),
        'recovery_points':0. if qualification_only else float(hp),
        'projected_display_gains':gains,'past_applied_raw_input':deepcopy(raw),
        'current_cap_bounded_raw_estimate':bounded,'numeric_discount':discount,
        'discount_basis':'same_current_future_training_forecast_discount_not_win_probability',
        'HP_basis':'strict_past_applied_clipped_lower_bound_estimate_not_current_request',
        'historical_capacity_diagnostic_only':prior.get('max_energy_delta_points'),
        'prior_sample_turns':deepcopy(prior.get('sample_action_turns',[])),
        'prior_sample_count':prior.get('sample_count'),
        'conditional_on_future_ordinary_SS_success':True,
        'future_reward_observed':False,'before_after_use_same_current_estimate':True}


def replacement_credit(components,success_resource_delta,failure_rate):
    """Combined progress value is max(proxy, explicit), not their sum.

    Original proxies already receive training failure discount in the parent
    scorer. Only the extra alternative-value margin receives that discount
    here, exactly once; the old energy continuation is not multiplied again.
    """
    if not(number(success_resource_delta)and success_resource_delta>=0
        and number(failure_rate)and 0<=failure_rate<=100):
        return {'active':False,'credit':0.,'reason':'resource_margin_or_failure_rate_unknown'}
    covered={k:max(0.,float(components.get(k,0)))for k in PROXY_COMPONENTS
        if number(components.get(k,0))}
    total=sum(covered.values())
    margin=max(0.,success_resource_delta-total)
    probability=1-failure_rate/100
    return {'active':margin>1e-9,'credit':round(probability*margin,4),
        'covered_proxy_components':covered,'covered_proxy_total':total,
        'success_resource_delta':success_resource_delta,'success_uplift_over_proxy':margin,
        'current_training_success_probability':probability,
        'reason':'alternative_max_of_whole_current_SS_progress_block_and_earned_resource_margin',
        'all_overflow_and_random_proxy_included_conservatively':True,
        'old_energy_continuity_reweighted':False,'future_SS_win_probability':None}
