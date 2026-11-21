"""Run-local support facts and sourced outing estimates; no device I/O."""
from __future__ import annotations

from copy import deepcopy
import json
from math import isfinite
import os
from pathlib import Path
import tempfile


SCHEMA = "uma_run_support_profile.v1"
SESSION_FILE = "session/support_card_profile.json"
EFFECT_LIMITS = {"event_recovery_percent": 80, "event_effectiveness_percent": 20}
CARD_TYPES = frozenset({'speed','stamina','power','guts','wisdom','friend','group'})


def canonical_type(value):
    if not isinstance(value,str):return None
    aliases={'速度':'speed','耐力':'stamina','力量':'power','根性':'guts','毅力':'guts',
             '智力':'wisdom','intelligence':'wisdom','友人':'friend','团队':'group'}
    return aliases.get(value,value) if aliases.get(value,value) in CARD_TYPES else None


def _optional_int(value, low, high, name):
    if value is not None and (type(value) is not int or not low <= value <= high):
        raise ValueError(f"support_profile_invalid_{name}")
    return value


def _complete_deck(cards):
    return (isinstance(cards, list) and len(cards) == 6
            and all(isinstance(row, dict) and type(row.get('slot')) is int
                    and 0 <= row['slot'] < 6 and isinstance(row.get('card_id'), str)
                    and row['card_id'] not in ('', 'unknown')
                    and isinstance(row.get('type'), str) and row['type'] in CARD_TYPES for row in cards)
            and {row['slot'] for row in cards} == set(range(6)))


def _aggregate_effects(payload, cards):
    if not _complete_deck(cards):
        return {}
    result = {}
    nested = payload.get('terminal_deck_effects')
    nested = nested if isinstance(nested, dict) else {}
    for key, low, high, inclusive_low in (('race_bonus_percent', 0, 100, True), ('event_multiplier', 0, 3, False)):
        value = nested[key] if key in nested else payload.get(key)
        if type(value) in (int, float) and isfinite(value) and (value >= low if inclusive_low else value > low) and value <= high:
            result[key] = value
    return result


def normalize_profile(payload, run_id, *, source=None):
    """Import an explicit deck selection; never fetch the global current deck.

    Existing card metadata/stat_bonuses and actual_effects are preserved.
    Outing estimates may separately resolve a dated reference/default;
    such estimates are never stored as actual run facts.
    """
    if not isinstance(payload, dict) or not isinstance(run_id, str) or not run_id:
        raise ValueError("support_profile_invalid")
    if payload.get("schema_version") not in {SCHEMA, "uma_current_support_deck.v1"}:
        raise ValueError("support_profile_schema_invalid")
    if payload.get("run_session_id") not in (None, run_id):
        raise ValueError("support_profile_run_mismatch")
    provenance = deepcopy(payload.get("source") or source)
    if not isinstance(provenance, dict) or not provenance:
        raise ValueError("support_profile_source_required")
    cards = payload.get("cards", [])
    if not isinstance(cards, list) or len(cards) > 6:
        raise ValueError("support_profile_cards_invalid")
    result = []
    slots = set()
    mei_count = 0
    for entry in cards:
        if not isinstance(entry, dict):
            raise ValueError("support_profile_card_invalid")
        card = deepcopy(entry)
        slot = _optional_int(card.get("slot"), 0, 5, "slot")
        if slot is None or slot in slots:
            raise ValueError("support_profile_slot_duplicate_or_missing")
        slots.add(slot)
        for name in ("card_id", "character_id", "rarity"):
            value = card.get(name)
            if value in (None, "", "unknown"):
                card[name] = None
            elif not isinstance(value, str):
                raise ValueError(f"support_profile_invalid_{name}")
        if card["rarity"] not in {None, "R", "SR", "SSR"}:
            raise ValueError("support_profile_invalid_rarity")
        card["level"] = _optional_int(card.get("level"), 1, 50, "level")
        card["limit_breaks"] = _optional_int(card.get("limit_breaks"), 0, 4, "limit_breaks")
        card['type']=canonical_type(card.get('type') or card.get('card_type'))
        card["source"] = deepcopy(card.get("source") or provenance)
        effects = card.get("actual_effects") or {}
        if not isinstance(effects, dict):
            raise ValueError("support_profile_actual_effects_invalid")
        card["actual_effects"] = {name: _optional_int(effects.get(name), 0, limit, name)
                                  for name, limit in EFFECT_LIMITS.items()}
        card["actual_effects_source"] = deepcopy(card.get("actual_effects_source") or card["source"])
        mei_count += card["character_id"] == "satake_mei"
        result.append(card)
    if mei_count > 1:
        raise ValueError("support_profile_mei_ambiguous")
    maximum = _optional_int(payload.get("max_vitality"), 100, 120, "max_vitality")
    normalized={"schema_version": SCHEMA, "run_session_id": run_id, "source": provenance,
            "cards": result, "max_vitality": maximum,
            "max_vitality_source": deepcopy(payload.get("max_vitality_source") or provenance)
                                   if maximum is not None else None}
    aggregate = _aggregate_effects(payload, result)
    if aggregate:
        normalized['terminal_deck_effects'] = aggregate
    for key in ('declared_profile','observed_profile','reference_snapshot','observation_history',
                'conflicts','legacy_confirmations','observation_status','declaration_frozen','deck_inspect'):
        if key in payload:normalized[key]=deepcopy(payload[key])
    return normalized


def normalize_declaration(payload):
    """Configuration data only. Old observed imports remain references, not new observations."""
    if payload is None:return None
    if not isinstance(payload,dict):raise ValueError('support_profile_configuration_invalid')
    if payload.get('schema_version') not in {SCHEMA,'uma_current_support_deck.v1'}:
        raise ValueError('support_profile_schema_invalid')
    if not isinstance(payload.get('source'),dict) or payload['source'].get('kind')!='user_declared':return deepcopy(payload)
    value=deepcopy(payload);value.pop('run_session_id',None)
    value['source']={**(value.get('source') or {}),'kind':'user_declared'}
    for card in value.get('cards',[]):
        if isinstance(card,dict):
            card['source']={'kind':'user_declared'}
            # A level is not an effect lookup. This UI declares identity/type/level only.
            card.pop('actual_effects',None);card.pop('actual_effects_source',None)
    checked=normalize_profile(value,'configuration')
    checked.pop('run_session_id',None)
    return checked


def _snapshot(value):
    return {key:deepcopy(value[key]) for key in ('schema_version','run_session_id','source','cards',
        'max_vitality','max_vitality_source','reference_snapshot') if key in value}


def freeze_declaration(run_root,run_id,payload):
    """Freeze the selected configuration once; never refresh it from global/panel state."""
    value=normalize_declaration(payload)
    previous=load_profile(run_root,run_id)
    if value is None or previous and previous.get('declaration_frozen'):return previous
    if not isinstance(value.get('source'),dict) or value['source'].get('kind')!='user_declared':
        if previous:return previous
        return save_profile(run_root,run_id,{'schema_version':SCHEMA,'source':{'kind':'reference_import'},
            'cards':[],'max_vitality':None,'reference_snapshot':deepcopy(value),'declaration_frozen':True})
    declaration=normalize_profile(value,run_id)
    saved=_snapshot(declaration)
    saved.update(declared_profile=_snapshot(declaration),declaration_frozen=True,
                 observation_status='missing',conflicts=[])
    return save_profile(run_root,run_id,saved)


def merge_observation(run_root,run_id,payload):
    """Keep declarations and fresh observations separately; compose fields with provenance."""
    previous=load_profile(run_root,run_id) or {}
    observed=deepcopy(payload);observed['schema_version']=SCHEMA
    observed['source']={**(observed.get('source') or {}),'kind':'observed'}
    for row in observed.get('cards',[]):
        row['source']={**(row.get('source') or {}),'kind':'observed'}
    observed=normalize_profile(observed,run_id)
    declaration=previous.get('declared_profile')
    if not declaration:
        result=_snapshot(observed)
        result['observed_profile']=_snapshot(observed)
    else:
        declared=normalize_profile(declaration,run_id)
        by_slot={row['slot']:deepcopy(row) for row in declared['cards']}
        conflicts=deepcopy(previous.get('conflicts',[]))
        for row in observed['cards']:
            before=by_slot.get(row['slot'],{})
            different_id=bool(before.get('card_id') and row.get('card_id') and before['card_id']!=row['card_id'])
            effective={} if different_id else deepcopy(before)
            sources={key:{'kind':'user_declared'} for key,value in effective.items() if value is not None}
            for key,value in row.items():
                if key in {'source','actual_effects_source'} or value is None:continue
                if key=='actual_effects':
                    value={name:amount for name,amount in value.items() if amount is not None}
                    if not value:continue
                if before.get(key) is not None and value!=before[key] and key in {'card_id','type','level','limit_breaks'}:
                    conflict={'slot':row['slot'],'field':key,'declared':before[key],
                        'observed':deepcopy(value),'resolution':'observed_field; declaration_retained'}
                    if conflict not in conflicts:conflicts.append(conflict)
                effective[key]=deepcopy(value);sources[key]={'kind':'observed'}
            effective['source']={'kind':'composed','field_sources':sources}
            if row.get('actual_effects_source'):effective['actual_effects_source']=deepcopy(row['actual_effects_source'])
            by_slot[row['slot']]=effective
        result={'schema_version':SCHEMA,'run_session_id':run_id,'source':{'kind':'composed'},
            'cards':[by_slot[slot] for slot in sorted(by_slot)],
            'max_vitality':observed.get('max_vitality') if observed.get('max_vitality') is not None else declared.get('max_vitality'),
            'declared_profile':_snapshot(declared),'observed_profile':_snapshot(observed),
            'declaration_frozen':True,'conflicts':conflicts}
    complete=len(observed['cards'])==6 and all(row.get('card_id') and row.get('type') for row in observed['cards'])
    result['observation_status']='complete' if complete else 'partial' if observed['cards'] else 'missing'
    history=deepcopy(previous.get('observation_history',[]))
    if previous.get('observed_profile'):history.append(_snapshot(previous['observed_profile']))
    if history:result['observation_history']=history[-4:]
    for key in ('reference_snapshot','legacy_confirmations'):
        if key in previous:result[key]=deepcopy(previous[key])
    return save_profile(run_root,run_id,result)


def clear_observation(run_root,run_id):
    previous=load_profile(run_root,run_id)
    if previous and previous.get('declared_profile'):
        return merge_observation(run_root,run_id,{'schema_version':SCHEMA,
            'source':{'kind':'observed','status':'not_observed_at_takeover'},'cards':[],'max_vitality':None})
    return save_profile(run_root,run_id,{'schema_version':SCHEMA,
        'source':{'kind':'new_cycle_current_deck_unobserved; preset_applies_to_later_restarts'},'cards':[],'max_vitality':None})


def record_legacy_confirmation(run_root,run_id,profile,card_id,source):
    value=deepcopy(profile)
    notes=value.setdefault('legacy_confirmations',[])
    note={'card_id':str(card_id),'source':deepcopy(source),'resolution':'six_slot_declaration_preserved; no_card_added'}
    if note not in notes:notes.append(note)
    return save_profile(run_root,run_id,value)


def declaration_settings(profile):
    declaration=(profile or {}).get('declared_profile')
    if not declaration:return None
    result=_snapshot(declaration);result.pop('run_session_id',None);return result


def evidence(run_root,run_id):
    """Backward-compatible optional review evidence; no images, config files or credentials."""
    try:profile=load_profile(run_root,run_id)
    except (OSError,ValueError):return {'status':'unavailable'}
    if profile is None:return {'status':'missing'}
    def layer(value):
        if not isinstance(value,dict):return None
        return {'source_kind':(value.get('source') or {}).get('kind'), 'run_session_id':value.get('run_session_id'),
            'cards':[{**{key:deepcopy(row.get(key)) for key in ('slot','card_id','type','level','limit_breaks')},
                'source_kind':(row.get('source') or {}).get('kind')} for row in value.get('cards',[])]}
    return {'status':'recorded','source_kind':(profile.get('source') or {}).get('kind'),
        'observation_status':profile.get('observation_status','legacy_unclassified'),
        'declared':layer(profile.get('declared_profile')),'observed':layer(profile.get('observed_profile')),
        'conflicts':deepcopy(profile.get('conflicts',[])),
        'legacy_confirmations':[{key:deepcopy(row.get(key)) for key in ('card_id','resolution')}
            for row in profile.get('legacy_confirmations',[])]}


def load_profile(run_root, run_id):
    path = Path(run_root) / SESSION_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA or data.get("run_session_id") != run_id:
        raise ValueError("support_profile_run_mismatch")
    return normalize_profile(data, run_id)


def save_profile(run_root, run_id, payload, *, source=None):
    data = normalize_profile(payload, run_id, source=source)
    path = Path(run_root) / SESSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return data


def outing_inputs(profile, run_id, *, confirmed_outing=False):
    """Explicit/reference/assumed planning inputs; never rewrite run facts.

    A current accepted Mei-outing packet may request the user's three-break
    default when its deck data are missing. Two-argument callers retain their
    old unbound-profile behavior. Actual numeric effects keep precedence.
    """
    from core.auto_trainer.mei_outing_effects import resolve_mei_effects

    def unavailable(reason):
        result = resolve_mei_effects(None, run_id, confirmed_outing=confirmed_outing)
        if result is not None:
            result['resolution']['ignored_profile_reason'] = reason
        return result

    if profile is None:
        return unavailable('profile_missing')
    if (not isinstance(profile, dict) or profile.get("schema_version") != SCHEMA
            or profile.get("run_session_id") != run_id):
        return unavailable('profile_unbound_or_wrong_schema')
    try:
        data = normalize_profile(profile, run_id)
    except ValueError:
        return unavailable('profile_normalization_failed')
    return resolve_mei_effects(data, run_id, confirmed_outing=confirmed_outing)

def attach_profile(packet, profile, run_id):
    """Snapshot into the existing decision packet; no extra screen or proof chain."""
    if packet is None or profile is None:
        return packet
    data = normalize_profile(profile, run_id)
    if packet.get("source", {}).get("run_session_id") != run_id:
        raise ValueError("support_profile_packet_run_mismatch")
    result = deepcopy(packet)
    result["support_card_profile"] = data
    if data["max_vitality"] is not None:
        result["max_vitality"] = data["max_vitality"]
    return result
