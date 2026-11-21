"""Per-run preferences over the existing policy, not additional game rewards."""

from core.auto_trainer.larc_participant_evidence import training_rows
import math
from functools import lru_cache


DEFAULTS={'numeric':1.,'bond':1.,'charge':1.,'gold_ship_bonus':0.,'mei_bonus':0.}
HEADS={'gold_ship':('gold_ship_bonus','黄金船'),'satake_mei':('mei_bonus','黄帽')}

def feature_available(root=None):
    from desktop.cut_package_profile import profile
    return profile(root).get('decision_preferences') is True

@lru_cache(maxsize=1)
def runtime_enabled():
    # Installed code changes only after restart; avoid disk I/O during forecasts.
    return feature_available()


def normalize(value=None):
    if value is None:value={}
    if not isinstance(value,dict) or set(value)-set(DEFAULTS)-{'tuning','workflow'}:
        raise ValueError('自定义取舍包含未知项目。')
    result={**DEFAULTS,**{key:amount for key,amount in value.items() if key in DEFAULTS}}
    for key,amount in result.items():
        if type(amount) not in (int,float) or not math.isfinite(amount):
            raise ValueError('权重和额外分请填写有效数字。')
        if key in ('numeric','bond','charge') and amount not in (.8,1.,1.2):
            raise ValueError('基础偏好请选择80%、100%或120%。')
        if key.endswith('_bonus') and not 0<=amount<=20:
            raise ValueError('人头额外分应在0到20之间。')
        result[key]=float(amount)
    if 'tuning' in value:
        from core.auto_trainer.larc_score_tuning import normalize as normalize_tuning
        result['tuning']=normalize_tuning(value['tuning'])
    if value.get('workflow') is not None:
        from core.auto_trainer.larc_timeline_workflow import normalize as normalize_workflow
        workflow=normalize_workflow(value['workflow'])
        if workflow['enabled'] or workflow['stages'] or workflow.get('editor_graph') is not None:
            result['workflow']=workflow
    return result


from core.auto_trainer.larc_strategy_extension import ensure_bootstrapped, get_registry

BUILTIN_2_3_PRO_HEADS={'gold_ship_bonus':1.09,'mei_bonus':1.18}
BASE_BUILTIN_PRESET_HEADS={
    '2.3':{'gold_ship_bonus':1.09,'mei_bonus':1.18},
    '2.3_pro':{'gold_ship_bonus':1.09,'mei_bonus':1.18},
}
BUILTIN_PRESET_HEADS=dict(BASE_BUILTIN_PRESET_HEADS)


def _sync_head_bonuses(registry):
    BUILTIN_PRESET_HEADS.clear()
    BUILTIN_PRESET_HEADS.update(BASE_BUILTIN_PRESET_HEADS)
    BUILTIN_PRESET_HEADS.update(getattr(registry, 'head_bonuses', {}) or {})


get_registry().add_reload_hook(_sync_head_bonuses)
_sync_head_bonuses(ensure_bootstrapped())


def builtin_preset_revision(goal):
    if not runtime_enabled():return None
    policy=getattr(goal,'larc_policy',{}) or {}
    rev=policy.get('revision')
    if rev not in BUILTIN_PRESET_HEADS:return None
    explicit=policy.get('decision_preferences') or {}
    if 'tuning' in explicit or 'workflow' in explicit:return None
    return rev


def builtin_2_3_pro_enabled(goal):
    return builtin_preset_revision(goal) in ('2.3','2.3_pro')


def values(goal):
    # Goals are validated at load/start. Avoid revalidating inside forecasts.
    if not runtime_enabled():return dict(DEFAULTS)
    policy=getattr(goal,'larc_policy',{}) or {}
    explicit=policy.get('decision_preferences') or {}
    rev=builtin_preset_revision(goal)
    if rev is not None:
        merged={**DEFAULTS,**explicit}
        for k,v in BUILTIN_PRESET_HEADS[rev].items():
            if k not in explicit or float(explicit.get(k) or 0.0)==0.0:
                merged[k]=v
        return merged
    return {**DEFAULTS,**explicit}


def configured(goal):
    return runtime_enabled() and 'decision_preferences' in (getattr(goal,'larc_policy',{}) or {})


def _panel_preference_origins(path):
    from pathlib import Path
    import json
    try:
        value = json.loads(Path(path).with_name('decision-preferences-origin.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def panel_preferences_saved(path, index, preferences, *, embedded=False):
    """Historical preferences are explicit; only our bound factory record is derived."""
    index = str(index)
    if embedded:
        return True
    if index not in preferences:
        return False
    origins = _panel_preference_origins(path)
    return not (index in origins and origins[index] == preferences[index])


def save_panel_preferences(path, index, value, preferences, *, factory=False):
    from copy import deepcopy
    from pathlib import Path
    from core.auto_trainer.cut_farming import atomic_json
    index = str(index)
    origins = _panel_preference_origins(path)
    before = deepcopy(origins)
    if factory:
        origins[index] = deepcopy(value)
    else:
        origins.pop(index, None)
    if origins != before:
        atomic_json(Path(path).with_name('decision-preferences-origin.json'), origins)
    preferences[index] = deepcopy(value)
    atomic_json(path, preferences)


def weighted_phase(base,goal):
    weights=dict(base);pref=values(goal)
    if pref['numeric']!=1.:
        for key in ('stats','skill_pt'):weights[key]*=pref['numeric']
    if pref['bond']!=1.:
        # Ordinary support friendship is separate from Mei's own startup.
        weights['friend_contact']=base['effective_bond']
        weights['effective_bond']*=pref['bond']
    if pref['charge']!=1.:
        weights['unscaled_net_charge']=base['net_charge']
        weights['charge_preference']=pref['charge']
        for key in ('net_charge','newly_full','ss_preparation'):weights[key]*=pref['charge']
    return weights


def current_training_people(state,candidate,source):
    if not isinstance(source,dict) or source.get('turn_index')!=state.turn or not source.get('run_session_id'):
        return None
    view = training_rows(candidate, typed=True)
    return [person for person in view[0] if isinstance(person, dict)] if view is not None else None


def head_preference(state,candidate,source,goal):
    pref=values(goal)
    requested={identity:pref[field] for identity,(field,_) in HEADS.items() if pref[field]>0}
    if not requested:return {'components':{},'total':0.,'requested':{},'status':'disabled'}
    people=current_training_people(state,candidate,source)
    view = training_rows(candidate, typed=True) if people is not None else None
    status = ('observed' if view[1] else 'partially_observed') if view is not None else 'unverified_training_participants'
    people = people or []
    identified={p.get('identity',{}).get('character_id') for p in people
        if p.get('identity',{}).get('status')=='known'}
    rows=[{'character_id':identity,'label':HEADS[identity][1],'requested_bonus':amount,
           'identified_here':True if identity in identified else None if status == 'partially_observed' else False,
           'applied_bonus':amount if identity in identified else 0.}
          for identity,amount in requested.items()]
    components={'user_'+HEADS[row['character_id']][0]:row['applied_bonus'] for row in rows}
    return {'components':components,'total':sum(components.values()),'requested':requested,
        'status':status,'participants':rows,
        'unidentified_count':(None if status == 'partially_observed' else
                              sum(p.get('identity',{}).get('status')!='known' for p in people)),
        'basis':'current_training_identity_only; once_per_character; user_preference_not_game_reward'}
