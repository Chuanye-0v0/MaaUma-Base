"""Pure full-remaining-career current action/purchase comparison.

Input current scores already contain original HP value and current risk.
The future model has no current-action HP transition. In common-reference
mode its cash outcome is a shared comparison basis, not the candidate's
income. Purchases still use actual cash, targets and current-action gates.
The route must not pay that purchase or grant current income again.
"""
from math import isfinite

SCENARIOS=('conservative','ordinary')

class Unsupported(ValueError): pass

def number(value):return type(value)in(int,float)and isfinite(value)
def need(ok,reason):
    if not ok:raise Unsupported(reason)

def continuation_anchor(offers, income_quote, *, only_no_buy=False):
    """Anchor the HP-blind future comparison before purchase ranking."""
    rows=[r for r in offers if r.get('eligible')is True and not r['phases'] and r['cost']==0]
    need(rows,'NoBuy_continuation_reference_required')
    for row in rows:
        need(number(row['original_current_score']),'reference_current_score_required')
    best=min(rows,key=lambda r:(-r['original_current_score'],r['offer_id']))
    return {'action_id':best['action_id'],'income':dict(income_quote(best)),
        'failure_rate':best['current_risk'],'basis':'highest_current_score_NoBuy',
        'forecast_only':True,'candidate_income_or_solvency_proof':False}


def continuation_outcomes(offer, quote, reference=None):
    """Used by both root precomputation and scoring; no differing cash roots."""
    rate=reference['failure_rate']if reference is not None else offer['current_risk']
    income=reference['income']if reference is not None else quote
    need(number(rate)and 0<=rate<=100,'continuation_failure_rate_required')
    need(isinstance(income,dict)and type(income.get('amount'))is int and income['amount']>=0
         and isinstance(income.get('basis'),str),'continuation_income_required')
    p=rate/100.
    return [(success,probability,income['amount']if success else 0)
        for success,probability in((True,1-p),(False,p))if probability>0]


def select(offers, *, current_cash, current_levels, now, route,
           income_quote, current_gate, credit=.95, only_no_buy=False,
           continuation_reference=None, current_preparation_gap=None):
    need(type(current_cash)is int and current_cash>=0,'current_cash_required')
    need(isinstance(current_levels,(list,tuple))and len(current_levels)==10,'current_levels_required')
    need(type(now)is int and now in(36,37,38,39,41),'firstcamp_free_action_scope_required')
    need(number(credit)and 0<=credit<=1,'future_credit_required')
    ranking=[];excluded=[]
    for offer in offers:
        if offer.get('eligible')is not True or(only_no_buy and offer['phases']):continue
        aid=offer['action_id'];cost=offer['cost'];owned=tuple(offer['targets'])
        rate=offer['current_risk'];cash=current_cash-cost
        need(type(cost)is int and 0<=cost<=current_cash,'current_purchase_must_use_existing_cash')
        need(len(owned)==10 and all(type(v)is int and a<=v<=3 for a,v in zip(current_levels,owned)),
             'current_monotone_targets_required')
        need(number(rate)and 0<=rate<=100,'observed_current_failure_rate_required')
        quote=income_quote(offer)
        need(isinstance(quote,dict)and type(quote.get('amount'))is int and quote['amount']>=0
             and isinstance(quote.get('basis'),str),'conditional_current_income_required')
        check=current_gate(offer,quote)
        need(isinstance(check,dict)and type(check.get('allowed'))is bool,'original_current_gate_required')
        if not check['allowed']:
            excluded.append({'offer_id':offer['offer_id'],'gate':check});continue
        original=offer['original_current_score'];uplift=offer['expected_purchase_uplift']
        need(number(original)and number(uplift),'current_score_and_expected_uplift_required')
        direct=original+uplift
        scenarios=[]
        for scenario in SCENARIOS:
            branches=[]
            for success,probability,ap in continuation_outcomes(offer,quote,continuation_reference):
                after_cash=cash+ap
                future=route(scenario,after_cash,owned)
                raw,gap=future['future_value'],future['unfunded_estimate']
                need(number(raw)and number(gap)and gap>=0,'finite_training_utility_and_gap_required')
                need(future.get('current_income')==0,'route_reapplied_current_income')
                for action in future.get('actions',[]):
                    need(action['ordinal']>now and action['points_before']>=action['cost']>=0,
                         'future_purchase_date_or_cash_invalid')
                value=credit*raw-.35*gap
                outcome=({'success':success,'current_conditional_AP':ap,'cash_after_current_outcome':after_cash}
                    if continuation_reference is None else
                    {'reference_success':success,'reference_conditional_AP':ap,
                     'reference_cash_after_outcome':after_cash,'candidate_outcome_claimed':False})
                branches.append({**outcome,'probability':probability,
                    'current_paid_cost':cost,'cash_after_current_payment':cash,
                    'owned_after_current_payment':owned,'future_raw':raw,'future_gap':gap,
                    'future_value':value,'future_route':future})
            scenarios.append({'scenario':scenario,'value':sum(b['probability']*b['future_value']for b in branches),
                'branches':branches,'weight':.5,'weight_is_calibrated_probability':False})
        continuation=sum(s['value']for s in scenarios)/2
        reference_details={}
        if continuation_reference is not None:
            own_gap=current_preparation_gap(offer,quote)if current_preparation_gap is not None else 0.
            need(number(own_gap)and own_gap>=0,'current_preparation_gap_required')
            def firstcamp_gap(branch):
                stopped=branch['future_route'].get('halted_at')or{}
                day=stopped.get('ordinal')
                return branch['future_gap']if type(day)is int and now<day<=42 else 0.
            reference_gap=sum(sum(b['probability']*firstcamp_gap(b)for b in s['branches'])for s in scenarios)/2
            # Retain the original soft shortage price, without counting the
            # same obligation twice or treating reference AP as our funds.
            extra_penalty=.35*max(0.,own_gap-reference_gap)
            reference_details={'continuation_reference':continuation_reference,
                'future_reference_value':continuation,'current_preparation_gap':own_gap,
                'reference_preparation_gap':reference_gap,
                'additional_preparation_penalty':extra_penalty}
            continuation-=extra_penalty
        ranking.append({**reference_details,'offer_id':offer['offer_id'],'action_id':aid,'phase':offer['phase'],
            'phases':list(offer['phases']),'targets':list(owned),'cost':cost,
            'score':direct+continuation,'direct_score':direct,'future_value':continuation,
            'current_risk':rate,'current_income':quote,'current_gate':check,
            'scenarios':scenarios,'current_energy_and_risk_retained_once':True,
            'additional_raw_failure_cost':0.,'native_raw_stats_required':False})
    need(ranking,'no_eligible_current_joint_candidate')
    ranking.sort(key=lambda r:(-r['score'],r['cost'],len(r['phases']),r['offer_id']))
    best=ranking[0];no_buy=max((r for r in ranking if not r['phases']),key=lambda r:r['score'],default=None)
    need(no_buy is not None,'NoBuy_comparison_required')
    result={'applied':True,'selected':best,'ranking':ranking,'NoBuy':no_buy,'excluded':excluded,
        'future_credit':credit,'source_scope':'current_and_confirmed_past_forecast',
        'future_model':'original_V3_fixed_reference_full_remaining_calendar',
        'cash_or_inventory_has_direct_terminal_value':False,'current_energy_retained_once':True,
        'current_probability_applied_once':True,'execution_authorized':False}
    if continuation_reference is not None:
        result.update(continuation_reference=continuation_reference,
            future_model='fixed_reference_career_purchase_value_with_current_HP_score',
            selection_reason='current_energy_score_and_common_reference_purchase_value')
    return result
