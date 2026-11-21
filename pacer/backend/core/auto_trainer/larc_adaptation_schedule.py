"""Small conditional Pt/friendship purchase schedules, never purchase authority."""

from core.auto_trainer.larc_adaptation import LEVEL_TWO_COSTS
from core.auto_trainer.larc_calendar import free_action_days,period_at,is_overseas,FORCED_RACES

OPTIONAL=((5,'expedition_skill_pt'),(7,'willpower_three'))
UNLOCK_DAYS={5:36,6:41,7:41,8:60,9:43}
ORDERS=((),(5,),(7,),(5,7),(7,5))


def price(slot,level):
    return ({1:300,2:200} if slot==5 else {1:500,2:300}).get(level)


def training_share(row):
    route=row.get('planning',{}).get('energy_horizon',{}).get('future_route_after',[])
    return sum(action in {'train','wisdom'} for action in route)/len(route) if route else .5


def canonical_training_share(row):
    """Same fraction, with the five typed names emitted by the HP channel.

    This classifies a planned route, not an observed future training count or
    guaranteed income. Old callers keep training_share unchanged by default.
    """
    planning=row.get('planning')if isinstance(row,dict)else None
    energy=planning.get('energy_horizon')if isinstance(planning,dict)else None
    route=energy.get('future_route_after')if isinstance(energy,dict)else None
    if not isinstance(route,(list,tuple))or not route:
        return .5
    trainings={'train','wisdom','train:speed','train:stamina','train:power','train:guts','train:wisdom'}
    return sum(isinstance(action,str)and action in trainings for action in route)/len(route)


def mandatory_due(slot,now,*,defer_niel_power=False):
    if defer_niel_power and slot==2 and 36<=now<=42:return 42
    if slot<5:return 40 if now<40 else 64
    if slot<8:return 42 if now<42 else 64
    return 66


def income_at(day,branch,share):
    return (50 if branch=='conservative' else 100)*share if is_overseas(day) else (5 if branch=='conservative' else 10)


def obligations(levels,now,*,respect_observed_locks=False,skip_language=False,defer_niel_power=False):
    debts=[]
    for slot,level in enumerate(levels):
        if slot==4 and skip_language and now<43:continue
        if level>=2:continue
        if respect_observed_locks and level==0 and UNLOCK_DAYS.get(slot,now+1)<=now:continue
        due=mandatory_due(slot,now,defer_niel_power=defer_niel_power)
        # Future locked essentials enter the money plan, not today's executor.
        if level==0 and (slot not in UNLOCK_DAYS or UNLOCK_DAYS[slot]>due):continue
        debts.append((slot,due,LEVEL_TWO_COSTS[slot]))
    return debts


def future_reserve(levels,day,branch,share,*,include_today_income=False,end_day=None,respect_observed_locks=False,skip_language=False):
    debts=[debt for debt in obligations(levels,day,respect_observed_locks=respect_observed_locks,skip_language=skip_language) if end_day is None or debt[1]<=end_day];required=0.
    for deadline in sorted({due for _,due,_ in debts}):
        expense=sum(cost for _,due,cost in debts if due<=deadline)
        income=sum(income_at(d,branch,share) for d in free_action_days(day if include_today_income else day+1,deadline))
        required=max(required,expense-income)
    return max(0.,required)


def simulate(now,points,levels,days,*,order,current_income,branch,share,initial_targets=None,initial_cost=0,start=1,respect_observed_locks=False,skip_language=False):
    owned=list(initial_targets or levels);cash=float(points-initial_cost);spent=float(initial_cost)
    dates={slot:now for slot in (5,7) if owned[slot]==3 and levels[slot]!=3}
    snapshots=[];payments=[];deficits=[]
    cash+=current_income
    last=days[-1] if days else now
    funding_end=last+1 if last+1 in FORCED_RACES else last
    failed=False
    for day in range(now+1,funding_end+1):
        for slot,unlock in UNLOCK_DAYS.items():
            if owned[slot]==0 and now<unlock<=day:owned[slot]=1
        for slot,due,cost in obligations(owned,now,respect_observed_locks=respect_observed_locks,skip_language=skip_language):
            if due==day:
                if cash<cost:
                    required=sum(amount for _,deadline,amount in obligations(owned,now,respect_observed_locks=respect_observed_locks,skip_language=skip_language) if deadline<=day)
                    deficits.append({'day':day,'slot':slot,'shortfall':required-cash,'all_due_cost':required})
                    payments.append({'day':day,'slot':slot,'cost':cost,'fully_funded':False})
                    failed=True;break # No fractional payment or invented level.
                cash-=cost;spent+=cost;owned[slot]=2
                payments.append({'day':day,'slot':slot,'cost':cost,'fully_funded':True})
        if failed:break
        if day in days:
            if day>=now+start:
                for slot in order:
                    if owned[slot]>=3:continue
                    cost=price(slot,owned[slot])
                    # An order that waits for friendship must not silently buy
                    # Pt first while its first upgrade remains locked/unfunded.
                    if cost is None:break
                    targets=list(owned);targets[slot]=3
                    if cash-cost<future_reserve(targets,day,branch,share,include_today_income=True,end_day=funding_end,respect_observed_locks=respect_observed_locks,skip_language=skip_language):break
                    cash-=cost;spent+=cost;owned=targets;dates[slot]=day
            snapshots.append(tuple(owned))
            cash+=income_at(day,branch,share)
    # Money and benefit forecasts share a window. Its adjacent forced race is
    # included; more distant costs remain for the next turn's fresh plan.
    reserve=future_reserve(owned,funding_end,branch,share,end_day=funding_end,respect_observed_locks=respect_observed_locks,skip_language=skip_language)
    unfunded=max([max(0.,reserve-cash)]+[d['shortfall'] for d in deficits])
    return {'order':[dict(OPTIONAL)[slot] for slot in order],'purchase_days':dates,'levels_by_action':snapshots,
        'spent':spent,'comparison_spending':spent+sum(d['all_due_cost'] for d in deficits),
        'points_at_window_end_estimate':cash,'mandatory_payments':payments,
        'unfunded_estimate':unfunded,'branch':branch,'current_income':current_income,
        'final_levels':owned,'feasible':not failed,
        'deadline_shortfalls':deficits,
        'funding_window_end':period_at(funding_end),'funding_window_end_ordinal':funding_end,
        'forecast_only':True,'future_win_income':0.}


def build_comparison(now,points,levels,days,utilities,*,initial_targets,initial_cost,
        baseline_share,after_share,baseline_income,baseline_failure,after_income,after_failure,point_rate,respect_observed_locks=False,skip_language=False):
    """Same future actions; common benefits/spending cancel between schedules."""
    comparisons=[]
    a,b=1-after_failure/100,1-baseline_failure/100
    # Couple counterfactual outcomes symmetrically: identical actions have
    # identical success/failure money, rather than asymmetric mean cash.
    outcomes=((True,True,min(a,b)),(True,False,max(0,a-b)),(False,True,max(0,b-a)),(False,False,1-max(a,b)))
    for branch in ('conservative','ordinary'):
        for success,before_success,probability in outcomes:
            if probability<=0:continue
            current=after_income if success else 0.
            baseline_current=baseline_income if before_success else 0.
            alternatives=[simulate(now,points,levels,days,order=order,current_income=current,branch=branch,share=after_share,
                initial_targets=initial_targets,initial_cost=initial_cost,respect_observed_locks=respect_observed_locks,skip_language=skip_language) for order in ORDERS]
            waits=[simulate(now,points,levels,days,order=order,current_income=baseline_current,branch=branch,share=baseline_share,respect_observed_locks=respect_observed_locks,skip_language=skip_language) for order in ORDERS]
            def utility(route,share):
                values=[utilities[i][(row[5]==3,row[7]==3)]*.5*.95**(i+1)*share for i,row in enumerate(route['levels_by_action'])]
                return sum(values)
            before=max(waits,key=lambda r:utility(r,baseline_share)-r['comparison_spending']*point_rate-r['unfunded_estimate']*.35)
            after=max(alternatives,key=lambda r:utility(r,after_share)-r['comparison_spending']*point_rate-r['unfunded_estimate']*.35)
            weight=.5*probability
            net_spending=after['comparison_spending']-before['comparison_spending']
            if not after['feasible']:net_spending=max(0,net_spending) # Default is not a saving.
            comparisons.append({'weight':weight,'income_branch':branch,'current_training_success':success,
                'baseline_training_success':before_success,
                'before':before,'after':after,'future_incremental_value':utility(after,after_share)-utility(before,baseline_share),
                'net_spending':net_spending,'unfunded_estimate':after['unfunded_estimate'],
                'incremental_unfunded':max(0,after['unfunded_estimate']-before['unfunded_estimate'])})
    return {'respect_observed_locks':respect_observed_locks,
        **({'skip_first_expedition_language':True} if skip_language else {}),'comparisons':comparisons,'future_difference':sum(r['weight']*r['future_incremental_value'] for r in comparisons),
        'net_spending':sum(r['weight']*r['net_spending'] for r in comparisons),
        'unfunded_estimate':sum(r['weight']*r['unfunded_estimate'] for r in comparisons),
        'incremental_unfunded':sum(r['weight']*r['incremental_unfunded'] for r in comparisons),
        'common_window':[period_at(day) for day in days],'window_actions':len(days),
        'income_priors':{'conservative':{'domestic':5,'overseas':50},'ordinary':{'domestic':10,'overseas':100}},
        'basis':'same finite action window; Pt/friendship marginal timing only; unknown income/recovery/attendance discounted',
        'unknown_fields':['future_income','future_friendship_attendance','conditional_unlock_after_prep_race','future_race_results']}


def imminent_shortfall(schedule,levels,now):
    deadlines=[due for _,due,_ in obligations(levels,now,respect_observed_locks=schedule.get("respect_observed_locks",False),
        skip_language=schedule.get("skip_first_expedition_language",False))]
    if not deadlines or len(free_action_days(now,min(deadlines)))>1:return 0.
    return sum(row['weight']*sum(gap['shortfall'] for gap in row['after']['deadline_shortfalls']
        if gap['day']==min(deadlines)) for row in schedule['comparisons'])
