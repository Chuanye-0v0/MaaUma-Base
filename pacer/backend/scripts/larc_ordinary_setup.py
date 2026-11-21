"""Normal-career setup preferences, separate from observed current-deck facts."""
from pathlib import Path
import json
import re
from types import SimpleNamespace

from scripts.ug_parent_one_turn_pipeline import _atomic_write_json

SETUP_FIELDS = frozenset({'character_id', 'character_name', 'support_deck',
    'borrow_card_id', 'borrow_min_level', 'borrow_min_break'})


def deck_profile_observation_enabled(ctx):
    """Follow this run's saved revision, never the global/new-run default."""
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    try:
        saved = json.loads((Path(ctx.root) / 'goal.json').read_text(encoding='utf-8-sig'))
        goal = SimpleNamespace(scenario=saved.get('scenario'),
                               larc_policy=saved.get('larc_policy', {}))
        return unified_larc_enabled(goal)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def parse_ordinary_setup(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value)-SETUP_FIELDS:
        raise ValueError('育成开局配置包含未知选项。')
    character = value.get('character_id') or 'current'
    name = value.get('character_name') or ''
    if (not isinstance(character, str) or character not in {'current','validation'}
            and re.fullmatch(r'[0-9]{6}', character) is None):
        raise ValueError('请选择有效的育成角色。')
    if not isinstance(name, str) or len(name.strip()) > 100:
        raise ValueError('角色名称应为有效文本。')
    name = name.strip()
    if character != 'current' and not name:
        raise ValueError('请选择育成角色；名单没有的角色请填写确切中文名。')
    result = {'character_id':character, 'character_name':name}
    for field, default, minimum, maximum in (
            ('support_deck',0,0,10), ('borrow_card_id',0,0,999999),
            ('borrow_min_level',1,1,50), ('borrow_min_break',0,0,4)):
        item = value.get(field, default)
        if isinstance(item, str) and re.fullmatch(r'[0-9]+', item.strip()):
            item = int(item.strip())
        if type(item) is not int or not minimum <= item <= maximum:
            raise ValueError({'support_deck':'卡组请选择沿用当前或 1～10。',
                'borrow_card_id':'请选择有效的借卡。', 'borrow_min_level':'借卡最低等级请填 1～50。',
                'borrow_min_break':'借卡最低突破请选择 0～4。'}[field])
        result[field] = item
    return result


def save_ordinary_setup(root, progress, setup):
    root = Path(root)
    if progress.get('run_session_id') != root.name:
        raise ValueError('ordinary_setup_run_mismatch')
    _atomic_write_json(root/'session/ordinary_setup.json', {
        'schema_version':'uma.ordinary_setup.v1', 'run_session_id':root.name,
        'device':progress['device'], 'source':'user_panel_preferences',
        'observed_current_deck':False, 'setup':setup})


def save_opening_support_observation(ctx, setup, observation):
    """Reuse U2's support facts, not its minimum filters or old-deck facts."""
    from core.auto_trainer.cut_support_selection import actual_borrow_profile, actual_deck_profile
    from core.auto_trainer.run_support_profile import load_profile, save_profile, merge_observation, SCHEMA
    if not observation:
        return
    previous=load_profile(ctx.root,ctx.run_id)
    cards=(previous or {}).get('cards',[])
    fresh=[]
    full_deck = deck_profile_observation_enabled(ctx)
    # A fresh partial observation must not inherit unobserved old slots and
    # masquerade as a complete current deck. Prior facts only enrich an
    # observed matching card's known effects below.
    cards=[] if full_deck or setup['support_deck'] else [card for card in cards if card['slot']<5]
    if full_deck:
        for observed in actual_deck_profile(observation, expected_deck=setup['support_deck']):
            matching = next((card for card in (previous or {}).get('cards', []) if
                card.get('card_id') == observed.get('card_id') and card.get('level') == observed.get('level')
                and observed.get('limit_breaks') is not None
                and card.get('limit_breaks') == observed.get('limit_breaks')), None)
            if matching and not observed.get('actual_effects'):
                observed['actual_effects'] = matching.get('actual_effects', {})
                observed['actual_effects_source'] = matching.get('actual_effects_source')
            observed['source'] = {**observed.get('source', {}), 'kind': 'ordinary_support_observation'}
            fresh=[card for card in fresh if card['slot']!=observed['slot']];fresh.append(observed)
            cards = [card for card in cards if card['slot'] != observed['slot']]
            cards.append(observed)
    borrowed=actual_borrow_profile(observation)
    if borrowed:
        provenance={**borrowed['source'],'kind':'ordinary_support_observation'}
        borrowed.update(source=provenance,actual_effects_source=provenance)
        matching=next((card for card in (previous or {}).get('cards',[]) if
            card.get('card_id')==borrowed['card_id'] and card.get('level')==borrowed['level']
            and borrowed['limit_breaks'] is not None and card.get('limit_breaks')==borrowed['limit_breaks']),None)
        if matching:
            borrowed['actual_effects']=matching.get('actual_effects',{})
            borrowed['actual_effects_source']=matching.get('actual_effects_source')
        cards = [card for card in cards if card['slot'] != borrowed['slot']]
        cards.append(borrowed)
        fresh=[card for card in fresh if card['slot']!=borrowed['slot']];fresh.append(borrowed)
    if previous and previous.get('declared_profile'):
        saved=merge_observation(ctx.root,ctx.run_id,{'schema_version':SCHEMA,
            'source':{'kind':'observed','phase':'ordinary_support_setup'},'cards':fresh,'max_vitality':None})
        ctx.support_profile=saved;return
    saved = save_profile(ctx.root,ctx.run_id,{'schema_version':SCHEMA,'source':{
        'kind':'ordinary_support_explicit_setup','owned_cards':
            'observed' if full_deck and any(card.get('source', {}).get('kind') == 'ordinary_support_observation'
                                         and card['slot'] < 5 for card in cards)
            else 'unknown' if setup['support_deck'] else 'user_static'},
        'cards':cards,'max_vitality':None})
    if full_deck:
        ctx.support_profile = saved
