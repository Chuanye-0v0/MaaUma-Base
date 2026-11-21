"""Read-only, run-owned collectors for continuation checkpoints."""
from copy import deepcopy
import json
from pathlib import Path
import re

from core.auto_trainer.cut_farming import atomic_json


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig')) if Path(path).is_file() else {}


def send_notice(ctx, node, entry):
    facts = entry.get('facts', {})
    text = ('首次远征前已暂停，请号主接管。' if node == 'handoff' else
            f'续养节点 {node} 已暂停，请查看当前条件和现场。')
    text += f"\n局号：{ctx.run_id}\n适应性Pt：{facts.get('adaptation_points', '未知')}"
    text += f"\n距离适性：{facts.get('distances', {})}\n大小切事件记录：{len(facts.get('cut_events', []))}"
    hook = getattr(ctx, 'cut_farming_hook', None)
    config = getattr(getattr(hook, 'session', None), 'config', None)
    try:
        from desktop.notifications import send_event
        return send_event(event_id='manual_takeover', title='Uma 续养：等待接管',
            content=text, legacy_token_file=getattr(config, 'pushplus_token_file', None))
    except Exception:
        return {'status': 'failed'}


def base_facts(ctx, provider):
    state = provider.get('state_context', {})
    growth = state.get('scenario_state', {}).get('larc_growth_observation', {})
    profile = read(Path(ctx.root) / 'session/support_card_profile.json')
    if profile and profile.get('run_session_id') != ctx.run_id:
        raise ValueError('continuation_support_profile_run_mismatch')
    cards = []
    for card in profile.get('cards', []):
        kind = card.get('type', card.get('card_type'))
        role = 'training' if kind in {'speed', 'stamina', 'power', 'guts', 'wisdom', 'intelligence'} else (
            'friend' if kind == 'friend' else 'unknown')
        cards.append({'card_id': card.get('card_id'), 'character_id': card.get('character_id'),
                      'role': role, 'slot': card.get('slot'), 'type': kind})
    hook = getattr(ctx, 'cut_farming_hook', None)
    probe = {}
    if hook is not None and hook.last_probe_turn == ctx.progress['next_turn']:
        probe = hook.session.data['attempts'][-1].get('last_probe', {})
    facts = {'stats': deepcopy(state.get('current_stats', {})),
        'skill_pt': state.get('current_stats', {}).get('skill_pt'),
        'adaptation_points': growth.get('adaptation_points') if growth.get('status') == 'accepted' else None,
        'expectation_percent': growth.get('expectation') if growth.get('status') == 'accepted' else None,
        'supporter_points': None, 'supports': cards,
        'support_profile_complete': len(cards) == 6 and all(c['card_id'] and c['role'] != 'unknown' for c in cards),
        'cut_state': probe.get('state') if probe.get('returned') else 'unknown',
        'skills': [], 'skills_complete': False, 'distances': {}, 'chains': [], 'cut_events': [],
        'event_history_complete': False, 'source_period': provider.get('larc_period', {}).get('value')}
    add_event_facts(ctx, facts)
    return facts


def add_event_facts(ctx, facts, *, catalog=None):
    ledger = read(Path(ctx.root) / 'session/event_progress.json')
    if ledger and ledger.get('run_id') != ctx.run_id:
        raise ValueError('continuation_event_history_run_mismatch')
    if catalog is None:
        from core.auto_trainer.larc_event_catalog import event_catalog
        catalog = event_catalog()
    rows = list(catalog.values())
    hook = getattr(ctx, 'cut_farming_hook', None)
    variant = hook.session.config.character_id if hook else (
        read(Path(ctx.root) / 'session/ordinary_setup.json').get('character_id'))
    chain_map = {}
    for event in ledger.get('occurrences', []):
        matches = [row for row in rows if (event.get('story_id') and str(row.get('story_id')) == str(event['story_id']))
            or (event.get('event_id') and row.get('key') == event['event_id'])]
        if not matches:
            matches = [row for row in rows if event.get('title') in row.get('titles', [])]
        selected = event.get('selected_option_index') is not None and event.get('option_match_status') == 'matched'
        settled = selected and event.get('result_observed') is True
        if len(matches) == 1:
            reference = matches[0]
            if any('才能出众' in option.get('effects', '') for option in reference.get('options', [])):
                scope = reference.get('scope_kind')
                kind = ('small' if scope == 'character' and reference.get('trainee_variant_id') == variant else
                        'big' if scope in {'common', 'universal'} else 'unknown')
                facts['cut_events'].append({'kind': kind, 'event_id': reference.get('key'),
                    'story_id': reference.get('story_id'), 'title': event.get('title'),
                    'occurrence_id': event.get('occurrence_id'), 'turn_id': event.get('turn_id'),
                    'seen': True, 'selected_option': event.get('selected_option_index'),
                    'selection_executed': selected, 'completed': settled,
                    'result_observed': event.get('result_observed') is True,
                    'result': event.get('outcome') or 'unknown'})
        owner = event.get('owner') or {}
        card_id = str(owner.get('owner_id', '')) if owner.get('owner_id_namespace') == 'support_card' else None
        stage = event.get('chain_stage') or {}
        if card_id and type(stage.get('step')) is int:
            key = (card_id, stage.get('kind'))
            chain = chain_map.setdefault(key, {'card_id': card_id, 'chain_id': stage.get('kind'),
                'seen_stage': 0, 'settled_stage': None, 'completed': None})
            chain['seen_stage'] = max(chain['seen_stage'], stage['step'])
            if settled:
                chain['settled_stage'] = max(chain['settled_stage'] or 0, stage['step'])
            if event.get('chain_ended'):
                chain['terminated'] = True
    facts['chains'] = list(chain_map.values())
    profile = read(Path(ctx.root) / 'session/support_card_profile.json')
    if profile and profile.get('run_session_id') == ctx.run_id:
        from core.support_card_knowledge import default_library
        context = default_library().build_run_context(profile, run_id=ctx.run_id, progress=ledger,
            through_turn=ctx.progress['next_turn'])
        facts['chains'] = [{'card_id': row['support_card_id'], 'chain_id': row['chain_id'],
            'seen_stage': max(row.get('seen_stages', []), default=0),
            'settled_stage': max(row.get('completed_stages', []), default=0) if row.get('history') else None,
            'completed': True if row['status'] == 'completed' else False if row['status'] == 'terminated' else None,
            'status': row['status']} for row in context['chain_progress']]


def bond_facts_from_pages(cards, observations):
    result = deepcopy(cards)
    for page in observations:
        for person in page.get('participants', []):
            identity, role, bond = (person.get(key) or {} for key in ('identity', 'role', 'bond'))
            if role.get('status') != 'accepted' or role.get('kind') not in {
                    'support', 'support_card', 'friend', 'owned_support'}:
                continue
            candidates = [card for card in result if card['card_id'] and identity.get('status') == 'known' and (
                str(identity.get('matched_card_id') or '') == str(card['card_id']) or
                card.get('character_id') and identity.get('character_id') == card['character_id'])]
            identity_source = 'current_portrait_identity'
            if not candidates and identity.get('status') != 'known':
                # A verified support type badge identifies a card only when
                # that type occurs once in this actual six-card profile.
                icon = {'intelligence': 'wisdom'}.get(role.get('icon_type'), role.get('icon_type'))
                candidates = [card for card in result if icon in {
                    'speed', 'stamina', 'power', 'guts', 'wisdom', 'friend'}
                    and card.get('type') == icon and card.get('card_id')]
                identity_source = 'unique_actual_deck_type_badge'
            if len(candidates) != 1:
                continue
            card = candidates[0]
            if card.get('bond_conflict') is True:
                continue
            ratio = bond.get('fill_ratio_estimate')
            if bond.get('status') == 'accepted' and bond.get('is_max') is True:
                if card.get('bond_max') is not None and card['bond_max'] < 100:
                    card.update(bond_exact=None, bond_min=None, bond_max=None, bond_conflict=True,
                                bond_source='conflicting_current_observations')
                    continue
                card.update(bond_exact=100, bond_min=100, bond_max=100,
                            bond_source='current_max_label', bond_identity_source=identity_source)
                continue
            if bond.get('status') == 'accepted' and type(ratio) in {int, float}:
                low, high = max(0, (ratio - .03) * 100), min(100, (ratio + .03) * 100)
                if card.get('bond_min') is not None:
                    low, high = max(low, card['bond_min']), min(high, card['bond_max'])
                if low <= high:
                    card.update(bond_min=round(low, 2), bond_max=round(high, 2),
                                bond_source='current_training_pages', bond_identity_source=identity_source)
                else:
                    card.update(bond_exact=None, bond_min=None, bond_max=None, bond_conflict=True,
                                bond_source='conflicting_current_observations')
    return result


def inspect_node(ctx, node, source, provider, output, config):
    output.mkdir(parents=True, exist_ok=True)
    facts = base_facts(ctx, provider)
    visited = False
    if node == 'junior':
        from scripts.larc_continuation_inspection import inspect_junior
        result = inspect_junior(ctx, source, provider, output, config, facts)
    elif node == 'inheritance':
        from scripts.larc_continuation_inspection import inspect_inheritance
        result = inspect_inheritance(ctx, source, provider, output, config, facts)
    else:
        result = {'ok': True, 'source': source, 'provider': provider, 'visited': False}
        from scripts.larc_autonomy_growth import completed_levels
        facts['adaptation_levels'] = completed_levels(ctx.root)
    if not result.get('ok'):
        return result
    atomic_json(output / 'facts.json', facts)
    return {**result, 'facts': facts, 'visited': result.get('visited', visited)}
