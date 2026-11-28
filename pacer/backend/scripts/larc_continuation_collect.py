"""Read-only, run-owned collectors for continuation checkpoints."""
from copy import deepcopy
import json
import logging
import math
from pathlib import Path
import re

from core.auto_trainer.cut_farming import atomic_json

_LOG = logging.getLogger(__name__)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig')) if Path(path).is_file() else {}


def send_notice(ctx, node, entry):
    facts = entry.get('facts', {})
    text = ('首次远征前已暂停，请号主接管。' if node == 'handoff' else
            f'续养节点 {node} 已暂停，请查看当前条件和现场。')
    if node == 'classic_arc':
        res = entry.get('result', {})
        actual_rank = res.get('actual_rank')
        rank_label = f'第 {actual_rank} 名' if type(actual_rank) is int else '未识别'
        text += f"\n首次凯旋门完赛名次：{rank_label}（要求 ≤ 第 {res.get('max_rank', 1)} 名）"
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


def _is_mid_career_takeover(ctx):
    root = Path(ctx.root)
    receipt = read(root / 'ss_sieve_opening_receipt.json')
    if isinstance(receipt, dict) and receipt.get('new_career_started') is False:
        return True
    state = read(root / 'session/larc_continuation_state.json')
    nodes = state.get('nodes') if isinstance(state, dict) else None
    if isinstance(nodes, dict):
        return any(
            isinstance(entry, dict) and entry.get('flow_status') == 'historical_not_observed'
            for entry in nodes.values()
        )
    return False


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
    from core.auto_trainer.larc_classic_arc_result import load_classic_arc_result
    classic_arc = load_classic_arc_result(ctx.root, ctx.run_id)
    facts = {'stats': deepcopy(state.get('current_stats', {})),
        'skill_pt': state.get('current_stats', {}).get('skill_pt'),
        'adaptation_points': growth.get('adaptation_points') if growth.get('status') == 'accepted' else None,
        'expectation_percent': growth.get('expectation') if growth.get('status') == 'accepted' else None,
        'supporter_points': None, 'supports': cards,
        'support_profile_complete': len(cards) == 6 and all(c['card_id'] and c['role'] != 'unknown' for c in cards),
        'cut_state': probe.get('state') if probe.get('returned') else 'unknown',
        'classic_arc_result': classic_arc,
        'skills': [], 'skills_complete': False, 'distances': {}, 'chains': [], 'cut_events': [],
        'event_history_complete': False, 'source_period': provider.get('larc_period', {}).get('value'),
        'mid_career_takeover': _is_mid_career_takeover(ctx)}
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


def _collect_realtime_luck(ctx, facts):
    try:
        from core.pacer_luck_records import read_current_review
        from core.pacer_realtime_luck import summarize_realtime_luck

        review = read_current_review(ctx.root)
        res = summarize_realtime_luck(review)
        res_map = res if isinstance(res, dict) else {}
        live_score = res_map.get('score')
        cum_score = res_map.get('cumulative_score')
        gate_score = (
            cum_score
            if not isinstance(cum_score, bool) and type(cum_score) in (int, float) and math.isfinite(cum_score)
            else live_score
        )
        dates = res_map.get('scored_dates')
        if (res_map.get('status') == 'recorded'
                and not isinstance(gate_score, bool)
                and type(gate_score) in (int, float)
                and math.isfinite(gate_score)
                and type(dates) is int and dates >= 3):
            facts['realtime_luck'] = float(gate_score)
            live_val = (
                float(live_score)
                if not isinstance(live_score, bool) and type(live_score) in (int, float) and math.isfinite(live_score)
                else float(gate_score)
            )
            facts['realtime_luck_detail'] = {
                'score': float(gate_score),
                'cumulative_score': float(gate_score),
                'live_score': live_val,
                'basis': res_map.get('basis'),
                'scored_dates': dates,
                'mood': res_map.get('mood'),
                'components': deepcopy(res_map.get('components')),
            }
            return
        obs_count = len(review.get('observations', [])) if isinstance(review, dict) else 0
        facts['realtime_luck_detail'] = {
            'status': res_map.get('status'),
            'scored_dates': dates,
            'observations': obs_count,
        }
        _LOG.warning(
            'realtime_luck unavailable for %s: status=%s scored_dates=%s observations=%s',
            getattr(ctx, 'run_id', None),
            res_map.get('status'),
            dates,
            obs_count,
        )
    except Exception as exc:
        facts['realtime_luck_detail'] = {
            'status': 'error',
            'error': f'{type(exc).__name__}:{exc}',
        }
        _LOG.warning(
            'realtime_luck collection failed for %s: %s: %s',
            getattr(ctx, 'run_id', None),
            type(exc).__name__,
            exc,
        )
    facts['realtime_luck'] = None


def inspect_node(ctx, node, source, provider, output, config):
    from core.auto_trainer.larc_continuation import custom_date_entry, _evaluate_condition_section

    output.mkdir(parents=True, exist_ok=True)
    facts = base_facts(ctx, provider)
    visited = False
    custom_entry = custom_date_entry(config, node)
    if node in ('junior', 'pre_expedition', 'inheritance2') or custom_entry is not None:
        section = custom_entry if custom_entry is not None else config.get(node, {})
        if any(row.get('kind') == 'realtime_luck_min' for row in section.get('conditions', [])):
            _collect_realtime_luck(ctx, facts)
    if node == 'junior':
        from scripts.larc_continuation_inspection import inspect_junior
        result = inspect_junior(ctx, source, provider, output, config, facts, section_name='junior')
    elif node == 'pre_expedition':
        needs_menu = any(
            row.get('kind') in {'supporter_points_min', 'friend_outing_unlocked', 'bond_ready_min', 'bond_card_min'}
            for row in config['pre_expedition'].get('conditions', [])
        )
        if needs_menu:
            from scripts.larc_continuation_inspection import inspect_junior
            result = inspect_junior(ctx, source, provider, output, config, facts, section_name='pre_expedition')
        else:
            result = {'ok': True, 'source': source, 'provider': provider, 'visited': False}
    elif node == 'inheritance':
        from scripts.larc_continuation_inspection import inspect_inheritance
        result = inspect_inheritance(ctx, source, provider, output, config, facts, section_name='inheritance')
    elif node == 'inheritance2':
        from scripts.larc_continuation_inspection import inspect_inheritance
        section2 = config['inheritance2']
        if section2['mode'] == 'filter' and section2.get('conditions'):
            pre_cond = _evaluate_condition_section('inheritance2', section2, facts)
            if pre_cond['status'] == 'failed':
                result = {'ok': True, 'source': source, 'provider': provider, 'visited': False}
            else:
                result = inspect_inheritance(ctx, source, provider, output, config, facts, section_name='inheritance2')
        else:
            result = inspect_inheritance(ctx, source, provider, output, config, facts, section_name='inheritance2')
    elif node == 'classic_arc':
        result = {'ok': True, 'source': source, 'provider': provider, 'visited': False}
    elif custom_entry is not None:
        needs_menu = any(
            row.get('kind') in {'supporter_points_min', 'friend_outing_unlocked', 'bond_ready_min', 'bond_card_min'}
            for row in custom_entry.get('conditions', [])
        )
        if needs_menu:
            from scripts.larc_continuation_inspection import inspect_junior
            result = inspect_junior(ctx, source, provider, output, config, facts, section_name=node)
        else:
            result = {'ok': True, 'source': source, 'provider': provider, 'visited': False}
    else:
        result = {'ok': True, 'source': source, 'provider': provider, 'visited': False}
        from scripts.larc_autonomy_growth import completed_levels
        facts['adaptation_levels'] = completed_levels(ctx.root)
    if not result.get('ok'):
        return result
    atomic_json(output / 'facts.json', facts)
    return {**result, 'facts': facts, 'visited': result.get('visited', visited)}
