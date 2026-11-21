"""One exact common character-event family; references never assert outcomes."""
import math

from core.auto_trainer.larc_event_library import title_key
from core.auto_trainer.larc_recovery import recovery_value
from core.auto_trainer.larc_training_value import gain_value

TITLES=('追加的自主训练','额外的自主训练','額外的自主訓練','追加の自主トレ')
TITLE_KEYS={title_key(value) for value in TITLES}


def family_title(text):return title_key(text) in TITLE_KEYS


def reference_effects(options):
    if len(options)!=2 or [option.get('index') for option in options]!=[1,2]:return None
    upper,lower=(option.get('effect_text','') for option in options)
    if not all(term in upper for term in ('体力-5','直前のトレーニング','ステータス+5','NPCの絆ゲージ+5')) or '体力+5' not in lower:return None
    return {'upper_energy':-5,'lower_energy':5,'last_training_stat':5,'scenario_npc_bond':5,
            'condition_cure_supported':False}


def additional_training_identity(text,count,context):
    """Bind only this exact family; a visible speaker identifies no costume."""
    if count!=2:return None
    lines=[line.strip() for line in str(text).splitlines() if line.strip()]
    titles=[line for line in lines if family_title(line)]
    if len(titles)!=1:return None
    variant=str(context.get('trainee_variant_id') or '')
    base=str(context.get('trainee_id') or '')
    observed_owner=None;visible_base=None
    # Read the speaker field, never a character mentioned in the narrative.
    markers=[i for i,line in enumerate(lines) if line in {'选项效果','選項效果'}]
    if '养成优俊少女事件' in lines and len(markers)==1 and markers[0]+1<len(lines):
        from core.support_card_knowledge import default_library
        name=lines[markers[0]+1]
        owners={str(card['character_id']) for card in default_library().cards.values()
                if card.get('character_name_zh_cn')==name and card.get('character_id')}
        if len(owners)==1:visible_base=next(iter(owners));observed_owner=name
    known_bases={value for value in (base,variant[:4],visible_base) if value}
    if len(known_bases)>1:
        return {'status':'owner_conflict','title':titles[0],'observed_owner':observed_owner,
                'visible_character_id':visible_base,'trainee_id':base or None,
                'trainee_variant_id':variant or None}
    character_id=next(iter(known_bases),None)
    return {'status':'bound' if base or variant else 'visible_character' if visible_base else 'unverified',
            'title':titles[0],'character_id':character_id,'observed_owner':observed_owner,
            'trainee_variant_id':variant or None}


def resolve_additional_training(text,count,context):
    if count!=2:return None
    titles=[line.strip() for line in text.splitlines() if family_title(line.strip())]
    if len(titles)!=1:return None
    effects={'upper_energy':-5,'lower_energy':5,'last_training_stat':5,'scenario_npc_bond':5,
             'condition_cure_supported':False}
    return {'kind':'additional_training','title':titles[0],
        'event':{'event_id':'common:additional_training','options':[{'index':1},{'index':2}]},
        'reference_ids':[], 'effects':effects,
        'identity_basis':'exact_common_family_title_and_option_count',
        'event_family_resolution':{'status':'effect_equivalent_family','title':titles[0]},
        'reference_basis':'U3_local_character_reference; user_confirmed_CN_upper_lower_order','effects_are_observed':False}


def score_additional_training(match,state,goal,context):
    from core.auto_trainer.larc_event_choice import _compact,_event_energy
    effects=match['effects'];observed=context.get('last_training_action') or {}
    training=(observed.get('training_type') if observed.get('status')=='accepted'
        and observed.get('run_session_id')==context.get('run_session_id') and bool(context.get('run_session_id'))
        and observed.get('turn_index')==state.turn and observed.get('basis')=='executed_training_action' else None)
    if training not in {'speed','stamina','power','guts','wisdom'}:training=None
    numeric=0.;unknown=['scenario_npc_bond_value','last_training_success']
    if training is not None and type(state.current_stats.get(training)) is int:
        numeric=gain_value(state,{training:effects['last_training_stat']},goal,project=False)['score']
    else:unknown.append('last_training_attribute_or_current_need')
    vitality=state.vitality;rows=[]
    for index,points in ((1,effects['upper_energy']),(2,effects['lower_energy'])):
        body=_event_energy([{'kind':'energy','value':points}],state,context,{})
        maximum=body['maximum_energy_points']
        usable=body['usable_energy_points']*100/maximum if body['usable_energy_points'] is not None else None
        energy=recovery_value(state,usable,include_held_ss=False) if usable is not None else None
        value=numeric if index==1 else 0.
        rows.append({'index':index,'score':round(value+energy,4) if energy is not None else None,
            'components':{'numeric':round(value,4),'energy':energy,'scenario_npc_bond':0.,'condition_cure':0.},
            **body,'usable_energy_delta_percent':usable,
            'attribute_gain_reference':{training:5} if index==1 and training else {},
            'unknown_fields':unknown+(['current_event_energy'] if energy is None else []),
            'condition_cure_basis':'no_cure_in_bound_U3_reference; no_guarantee_or_bonus',
            'npc_bond_basis':'scenario_NPC_reference_not_support_bond; unpriced',
            'reference_effects_not_observed':True})
    preferences=context.get('event_preferences') or goal.event_preferences
    if preferences.get('conserve_energy') is True and rows[1]['usable_energy_delta_percent'] is not None:
        recovered=rows[1]['usable_energy_delta_percent']*maximum/100
        consumed=-rows[0]['usable_energy_delta_percent']*maximum/100
        # Existing event-conservation floor applies to the real choice gap.
        # At a full bar the rejected heal is entirely overflow, so there is
        # no foregone recovery floor; the original loss curve still charges5.
        if recovered>0:
            floor=preferences.get('energy_cost_floor',1.6)
            if type(floor) not in {int,float} or not math.isfinite(floor) or floor<0:floor=1.6
            existing=rows[1]['components']['energy']-rows[0]['components']['energy']
            penalty=max(0.,(recovered+consumed)*floor-existing)
            rows[0]['components']['net_energy_conservation']=-round(penalty,4)
            rows[0]['score']=round(rows[0]['score']-penalty,4)
    if type(vitality) not in {int,float}:
        index=2;reason='additional_training_unknown_energy_conserve'
    elif vitality<100 and rows[1]['usable_energy_delta_percent']>0:
        index=2;reason='usable_recovery_saves_future_turn'
    else:
        best=max(rows,key=lambda row:(row['score'],row['index']))
        index=best['index'];reason='additional_training_numeric_vs_net_energy'
    return {'status':'selected','reason':reason,'choice_index':index,'event_id':match['event']['event_id'],
        'event_family':'additional_training','event_family_resolution':match['event_family_resolution'],
        'expected_event':(_compact(match['title']),'option:1','option:2'),'event_policy_version':'larc_events_v5',
        'effect_basis':match['reference_basis'],'identity_basis':match['identity_basis'],'reference_ids':match['reference_ids'],
        'candidates':rows,'energy_gap_reference_points':10,'capacity_basis':'known_profile' if context.get('max_vitality')==maximum else '100_120_midpoint_estimate',
        'last_training_basis':observed.get('basis') if training else 'unknown; not_inferred_from_best_training',
        'vitality':vitality,'unknown_fields':unknown,'learned_skills_written':False,'effects_are_observed':False,
        'recovery_priority_basis':'real_body_deficit; guaranteed_usable_recovery; manual_choices_first' if reason=='usable_recovery_saves_future_turn' else None}

