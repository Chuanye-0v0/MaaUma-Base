"""One explicitly conditional SS recharge cycle, not future reward knowledge.

Only identified current batteries and source-labelled success-preview increments
are carried. Missing identities/gross charge/next offers are never filled with
anonymous cells, guaranteed wins, or a copy of a current SSS reward.
"""
from dataclasses import dataclass, replace
import math

VERSION='larc_ss_resource_cycle.v1'


def _number(v):return type(v) in (int,float) and math.isfinite(v)


def select_cycle_side(legacy, resource):
    """One side's alternative valuations; never add proxy and redemption.

    Before callers supply only the unchanged state. The after result has no
    role in choosing this baseline. Ties preserve the existing proxy route.
    """
    selected=(_number(resource[0]) and resource[0]>legacy[0]+1e-9)
    return (resource if selected else legacy),selected


@dataclass(frozen=True)
class SSCycle:
    ids:tuple
    initial:tuple
    held:tuple
    current_maps:dict
    source_turn:int
    run_id:str
    numeric:float
    gains:dict
    evidence:dict
    empirical_lags:tuple=()
    ready_after:int|None=None
    ready_after_actions:int|None=None
    valuation_discount:float=1.
    known_inventory_floor:bool=False

    def consume_held(self,cells):
        if self.known_inventory_floor:
            result=[0 if mid in self.held else value for mid,value in zip(self.ids,cells)]
            # An anonymous selected member may be one of the other known-full
            # members. Consume that worst-case overlap, not every full member.
            missing=5-len(self.held)
            for i,value in enumerate(result):
                if missing and value==3:result[i]=0;missing-=1
            return tuple(result)
        return tuple(0 if mid in self.held or self.ready_after is not None and value==3 else value
                     for mid,value in zip(self.ids,cells))

    def available(self,cells,post_trainings=0,post_actions=0):
        if self.known_inventory_floor and sum(value==3 for value in cells)>=5:return True
        return (post_trainings>=self.ready_after and post_actions>=self.ready_after_actions
            if self.ready_after is not None else sum(value==3 for value in cells)>=5)

    def variants(self):
        if not self.empirical_lags:return [(self,1.,'identified_members')]
        from core.auto_trainer.larc_attribute_plan import CONDITIONAL_CREDIT
        floor=(replace(self,empirical_lags=(),ready_after=None,ready_after_actions=None)
               if self.known_inventory_floor else None)
        return [(floor,1-CONDITIONAL_CREDIT,
                 'identified_floor_without_anonymous_regeneration' if floor is not None else 'unknown_no_regeneration')]+[
            (replace(self,empirical_lags=(),ready_after=lag,ready_after_actions=mains),CONDITIONAL_CREDIT*weight,
                f'observed_ready_after_{lag}_trainings_{mains}_mains') for lag,mains,weight in self.empirical_lags]

    def consume_generated(self,cells):
        # Unknown future lineup: discard all full overflow rather than invent
        # control over which five can remain for a third matching.
        return tuple(0 if value==3 else value for value in cells)

    def charge_map(self,row):
        metadata=row.get('forecast_metadata') or {}
        packet=metadata.get('ss_cycle_charge')
        if isinstance(packet,dict) and packet.get('run_session_id')==self.run_id:
            turn=packet.get('turn_index');values=packet.get('member_cells')
            if type(turn)is int and turn<=self.source_turn and isinstance(values,dict):
                if all(mid in self.ids and type(n)is int and 0<=n<=3 for mid,n in values.items()):return values
        if metadata.get('source_turn_index')==self.source_turn:
            return self.current_maps.get(row.get('tab'))
        return None

    def advance(self,cells,row,*,domestic=True,success=True):
        if not domestic or not success:return cells
        values=self.charge_map(row)
        if values is None:return cells
        return tuple(min(3,value+values.get(mid,0)) for mid,value in zip(self.ids,cells))

    def after_current(self,cells,training_type):
        if training_type is None:return cells
        return self.advance(cells,{'tab':training_type,'forecast_metadata':{'source_turn_index':self.source_turn}})

    def diagnostic(self):
        return dict(status='conditional_member_cycle',schema_version=VERSION,identified_members=len(self.ids),
            held_members=list(self.held),anonymous_current_held_members=5-len(self.held),initial_cells=list(self.initial),source=self.evidence,
            future_SS_numeric_policy_estimate=self.numeric,future_SS_win_probability=None,
            future_SS_valuation_discount=self.valuation_discount,
            future_SS_recovery_points=0,future_SS_max_increase=0,max_generated_groups_in_route=1,
            third_or_later_cycles_not_modelled=True,real_SS_count_limit_added=False,
            conditional_on_sampled_layout_and_training_success=True,
            ordinary_observed_sample_not_known_next_offer=True,full_bar_zero_not_unclipped_charge=True,
            replaces_future_charge_scalar=True,replaces_battery_release_credit=True,
            **({'known_current_inventory_is_lower_bound':True}if self.known_inventory_floor else {}),
            future_recovery_charming_expectation_and_reward_cycle_not_added=True)


def from_packet(packet,*,followup=False):
    if not isinstance(packet,dict) or packet.get('schema_version')!=VERSION:
        return None,{'status':'unknown','reason':'member_cycle_packet_missing'}
    members=packet.get('members');held=packet.get('held_member_ids');offer=packet.get('next_ss_estimate')or{}
    source=packet.get('source')or{};maps=packet.get('current_training_charge')or{}
    if not isinstance(source,dict)or not isinstance(offer,dict):
        return None,{'status':'unknown','reason':'cycle_source_or_offer_invalid'}
    empirical=packet.get('observed_readiness_scenarios')or[]
    empirical_valid=(isinstance(empirical,list)and bool(empirical)and all(isinstance(r,dict)
        and type(r.get('training_actions'))is int and 0<=r['training_actions']<=12
        and type(r.get('main_actions',r['training_actions']))is int and r.get('main_actions',r['training_actions'])>=r['training_actions']
        and _number(r.get('weight'))and r['weight']>0 and r.get('source') for r in empirical))
    if (not isinstance(members,dict)or not (0 if empirical_valid else 5)<=len(members)<=24
        or not isinstance(held,(list,tuple))or not (0<=len(held)<=5 if empirical_valid else len(held)==5)
        or len(set(held))!=len(held) or any(mid not in members or members[mid]!=3 for mid in held)
        or any(not isinstance(mid,str)or not mid or type(n)is not int or not 0<=n<=3 for mid,n in members.items())):
        return None,{'status':'unknown','reason':'complete_held_member_identity_or_batteries_unknown'}
    if (not source.get('run_session_id')or type(source.get('turn_index'))is not int or not source.get('basis')
        or offer.get('kind')!='ordinary_SS_policy_estimate' or not offer.get('source')
        or not _number(offer.get('numeric_value'))or offer['numeric_value']<0
        or offer.get('future_win_probability')is not None):
        return None,{'status':'unknown','reason':'ordinary_future_SS_estimate_or_source_unknown'}
    if not isinstance(maps,dict)or any(not isinstance(v,dict)or any(mid not in members or type(n)is not int or not 0<=n<=3 for mid,n in v.items())for v in maps.values()):
        return None,{'status':'unknown','reason':'member_training_charge_unknown'}
    gains=offer.get('estimated_gains')
    if not isinstance(gains,dict)or any(not _number(v)or v<0 for v in gains.values()):
        return None,{'status':'unknown','reason':'future_SS_conditional_gain_estimate_unknown'}
    discount=offer.get('valuation_discount',1.)
    if not _number(discount)or not 0<discount<=1:return None,{'status':'unknown','reason':'future_SS_discount_unknown'}
    lags=()
    if empirical_valid:
        total=sum(r['weight']for r in empirical);grouped={}
        for r in empirical:
            key=(r['training_actions'],r.get('main_actions',r['training_actions']))
            grouped[key]=grouped.get(key,0.)+r['weight']
        lags=tuple((a,b,w/total)for (a,b),w in sorted(grouped.items()))
    ids=tuple(sorted(members));model=SSCycle(ids,tuple(members[i]for i in ids),tuple(held),maps,source['turn_index'],
        source['run_session_id'],float(offer['numeric_value']),dict(gains),dict(source),lags,None,None,float(discount),bool(followup))
    diagnostic=model.diagnostic()
    if lags:
        from core.auto_trainer.larc_attribute_plan import CONDITIONAL_CREDIT
        diagnostic.update(mode='observed_readiness_conditional_scenarios',anonymous_future_full_member_bounds=[0,5],
            confirmed_future_full_members=0,observed_training_lags=[{'training_actions':n,'main_actions':m,'relative_empirical_weight':w}for n,m,w in lags],
            observed_readiness_sources=[r['source']for r in empirical],
            conditional_scenario_credit=CONDITIONAL_CREDIT,unknown_no_regeneration_weight=1-CONDITIONAL_CREDIT,
            weights_are_policy_scenarios_not_future_win_probabilities=True,
            replaces_battery_release_fraction=CONDITIONAL_CREDIT)
    else:diagnostic.update(mode='identified_member_cells',replaces_battery_release_fraction=1.)
    if followup and empirical_valid:
        diagnostic.update(empirical_lag_does_not_replace_identified_cells=True,
            unknown_scenario_preserves_identified_member_floor=True)
    return model,diagnostic


def current_packet(state,goal,held_numeric_value,held_gains,forecast_discount=.85):
    """Strict current observation adapter; optional historical maps stay unknown.

    Positive preview cells are lower-bound increments on a successful sampled
    training. A full-bar preview0 stays0; we do not infer hidden gross gain1.
    The future ordinary offer uses one shared forecast discount, not a win rate.
    """
    preview=state.scenario_state.get('larc_ss_preview')or{}
    if not isinstance(preview,dict):return None
    from core.auto_trainer.larc_ss_visible import visible_ss_participants
    from core.auto_trainer.larc_charge_value import battery_inventory
    rows=visible_ss_participants(preview);source=preview.get('source')or{}
    if not isinstance(source,dict):return None
    run=source.get('run_session_id')
    if (preview.get('ready_count')!=5 or len(rows)!=5
        or not run or source.get('turn_index')!=state.turn):
        return None
    inventory=battery_inventory(state,run);held=[]
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    followup = unified_larc_enabled(goal)
    members={mid:p['filled']for mid,p in inventory.items()}
    for person in rows:
        ref=person.get('member_ref')or{};mid=ref.get('member_id')
        if (followup and ref.get('status')=='accepted' and ref.get('run_session_id')==run
                and isinstance(mid,str) and mid):
            # Current accepted ready-five preview itself proves this selected
            # stable member full. It need not also be on today's training page.
            if mid in members and members[mid]!=3:return None
            members[mid]=3
            if mid not in held:held.append(mid)
            continue
        if ref.get('status')=='accepted' and ref.get('run_session_id')==run and mid in inventory and inventory[mid]['filled']==3:
            if mid not in held:held.append(mid)
    maps={}
    for mid,item in inventory.items():
        for tab,n in item['previews'].items():maps.setdefault(tab,{})[mid]=n
    history=state.scenario_state.get('larc_ss_cycle_history')or{}
    if not isinstance(history,dict):history={}
    history_bound=(history.get('status')=='accepted'and history.get('run_session_id')==run
        and type(history.get('through_turn'))is int and history['through_turn']<=state.turn)
    scenarios=[]
    if history_bound:
        for interval in history.get('readiness_intervals',[]):
            a,b=interval.get('ss_turn_index'),interval.get('ready_turn_index');n=interval.get('intervening_training_actions')
            if (type(a)is int and type(b)is int and a<b<=state.turn and type(n)is int and 0<=n<=min(12,b-a-1)
                and interval.get('all_intervening_main_actions_accounted')is True and interval.get('domestic_only')is True
                and interval.get('source_receipt')and interval.get('source_ready_frame')):
                scenarios.append({'training_actions':n,'main_actions':b-a-1,'weight':1.,'source':dict(interval)})
    if len(held)!=5 and not scenarios:return None
    estimate=None
    if preview.get('kind')=='SS'and isinstance(held_gains,dict)and _number(held_numeric_value)and held_numeric_value>0:
        estimate=(dict(held_gains),held_numeric_value/.85,'current ordinary SS offer; undo held .85 then one shared forecast discount')
    elif history_bound and goal is not None:
        from core.auto_trainer.larc_training_value import gain_value
        from core.auto_trainer.larc_opportunity_forecast import raw_gain,display_gain
        from core.auto_trainer.state import TRAINING_TYPES
        for sample in sorted(history.get('ordinary_offers',[]),key=lambda r:r.get('turn_index',-1),reverse=True):
            turn=sample.get('turn_index');old=sample.get('stats_before')or{};g=sample.get('stats_gain')or{}
            if (sample.get('kind')!='SS' or type(turn)is not int or turn>state.turn or not sample.get('source')
                or any(not _number(old.get(k))or not _number(g.get(k))or g[k]<0 for k in TRAINING_TYPES)
                or not _number(g.get('skill_pt'))or g['skill_pt']<0):continue
            if any(not _number(state.current_stats.get(k))for k in TRAINING_TYPES):continue
            # gain_value's observed display API accepts integer gains. Floor
            # only this policy projection, never relabel fractional estimates
            # as extra displayed game points or silently discard all attributes.
            projected={k:int(math.floor(display_gain(state.current_stats[k],raw_gain(old[k],g[k]))))for k in TRAINING_TYPES}
            projected['skill_pt']=g['skill_pt']
            estimate=(projected,gain_value(state,projected,goal,project=False)['score'],
                f'ordinary SS offer at turn {turn}, display->raw at source stats then current display')
            break
    if estimate is None:return None
    estimated_gains,estimated_value,offer_source=estimate
    if not _number(forecast_discount)or not 0<forecast_discount<=1:return None
    return dict(schema_version=VERSION,source={'run_session_id':run,'turn_index':state.turn,
        'basis':'bound current identified battery union and success-preview lower-bound cells'},members=members,held_member_ids=held,
        current_training_charge=maps,observed_readiness_scenarios=scenarios,next_ss_estimate={'kind':'ordinary_SS_policy_estimate',
            'numeric_value':estimated_value*forecast_discount,'valuation_discount':forecast_discount,
            'estimated_gains':dict(estimated_gains),'future_win_probability':None,
            'source':offer_source+' * shared future training forecast_discount once; not known next offer or current SSS'})
