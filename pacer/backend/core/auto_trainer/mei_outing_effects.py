"""Run-local Mei planning inputs with explicit/reference/assumed provenance.

This resolver never changes the profile, a receipt, or physical recovery. A
confirmed current Mei outing may use the user's three-break planning default
when card data are missing. The default is not a card observation.
"""
from copy import deepcopy

SCHEMA = 'uma_mei_outing_effect_resolution.v1'
PROFILE_SCHEMA = 'uma_run_support_profile.v1'
MEI_IDS = {'30160': 'SSR', '10094': 'R'}
MEI_CHARACTERS = frozenset(('9043', 'satake_mei'))
LIMITS = {'recovery': 80, 'event': 20}
EFFECT_KEYS = {'recovery': 'event_recovery_percent', 'event': 'event_effectiveness_percent'}
# Exact common level-cap points, extracted from the dated local support
# library effect25/26 tables and corroborated by C1's explicit max-level
# tables. No arbitrary intermediate-level interpolation is performed.
REFERENCE = {
    'SSR': {30: (40, 15), 35: (50, 16), 40: (60, 18), 45: (70, 20), 50: (80, 20)},
    'R': {20: (30, 5), 25: (32, 6), 30: (35, 8), 35: (37, 10), 40: (40, 10)},
}
CAPS = {'SSR': (30, 35, 40, 45, 50), 'R': (20, 25, 30, 35, 40)}
REFERENCE_SOURCE = {
    'kind': 'support_library_reference', 'region': 'jp_reference',
    'dataset_version': '2026-10-02', 'master_boundary': '2024-02-13_pre_UAF',
    'support_library': 'data/support_knowledge/cn_larc_end/support_library.json',
    'effect_ids': [25, 26], 'cn_verified': False, 'observed': False,
}


def _valid_percent(value, key):
    return type(value) is int and 0 <= value <= LIMITS[key]


def _stable_string(value):
    if isinstance(value, str):
        return value if value not in ('', 'unknown') else None
    return str(value) if type(value) is int else None


def _identity(card):
    card_id = _stable_string(card.get('card_id'))
    character = _stable_string(card.get('character_id'))
    if card_id in MEI_IDS:
        if character not in (None, *MEI_CHARACTERS):
            return None, 'card_and_character_conflict'
        if card.get('rarity') not in (None, '', MEI_IDS[card_id]):
            return None, 'card_and_rarity_conflict'
        return {'card_id': card_id, 'rarity': MEI_IDS[card_id],
                'basis': 'stable_mei_card_id'}, None
    if character in MEI_CHARACTERS and card_id is None:
        rarity = card.get('rarity')
        return {'card_id': None, 'rarity': rarity if rarity in REFERENCE else None,
                'basis': 'stable_mei_character_id'}, None
    if character in MEI_CHARACTERS:
        return None, 'explicit_non_mei_card_ID_conflicts_with_mei_character'
    return None, None


def _reference(rarity, card):
    """Select a known point; use declared level before declared break cap."""
    assumed_rarity = rarity not in REFERENCE
    selected_rarity = rarity if rarity in REFERENCE else 'SSR'
    level, breaks = card.get('level'), card.get('limit_breaks')
    valid_breaks = type(breaks) is int and 0 <= breaks <= 4
    source = deepcopy(REFERENCE_SOURCE)
    source.update(rarity=selected_rarity, card_id='10094' if selected_rarity == 'R' else '30160',
                  actual_profile_rarity=rarity, rarity_assumed=assumed_rarity)
    if not assumed_rarity and type(level) is int and level in REFERENCE[selected_rarity] and (
            not valid_breaks or level <= CAPS[selected_rarity][breaks]):
        source.update(reference_level=level, level_source='declared_level',
                      defaulted=False, assumed=False)
        return REFERENCE[selected_rarity][level], source
    if not assumed_rarity and valid_breaks:
        selected_level = CAPS[selected_rarity][breaks]
        source.update(reference_level=selected_level, limit_breaks=breaks,
            level_source='declared_break_cap_level_assumption', defaulted=False, assumed=True,
            unsupported_or_conflicting_level=level,
            note='Break count is declared; full level at that cap is a planning assumption, not observed.')
        return REFERENCE[selected_rarity][selected_level], source
    selected_level = CAPS[selected_rarity][3]
    source.update(kind='user_requested_three_break_planning_default',
        reference_level=selected_level, limit_breaks=3, defaulted=True, assumed=True,
        level_source='three_break_default',
        reason='unknown_rarity_or_missing_or_unsupported_level_and_break_data',
        note='User requested a three-break estimate instead of the naked minimum; actual receipt is never rewritten.')
    return REFERENCE[selected_rarity][selected_level], source


def resolve_mei_effects(profile, run_id, *, confirmed_outing=False):
    """Resolve only current-run Mei inputs; legacy scalar keys stay compatible.

    Explicit numeric effects (including zero) win per field. Known level-cap
    reference points follow. Missing data on a confirmed Mei card/outing use
    the requested three-break estimate and retain that assumption in metadata.
    No other friend card or yellow portrait is treated as Mei.
    """
    if not isinstance(run_id, str) or not run_id:
        return None
    usable = (isinstance(profile, dict) and profile.get('schema_version') == PROFILE_SCHEMA
              and profile.get('run_session_id') == run_id and isinstance(profile.get('cards'), list))
    invalid_reason = None if usable or profile is None else 'unbound_or_invalid_profile_ignored'
    matches, conflicts = [], []
    if usable:
        for card in profile['cards']:
            if not isinstance(card, dict):
                continue
            identity, conflict = _identity(card)
            if identity is not None:
                matches.append((card, identity))
            if conflict:
                conflicts.append(conflict)
    if len(matches) > 1:
        # A current accepted outing still supplies its own identity, but no
        # ambiguous card's explicit effects may be promoted into that outing.
        conflicts.append('ambiguous_multiple_mei_cards_ignored')
        matches = []
    found = matches[0] if matches else None
    if found is None and not confirmed_outing:
        if not usable:
            return None
        return {'recovery': None, 'event': None, 'rarity': None,
                'max_vitality': profile.get('max_vitality'), 'card': None,
                'resolution': {'schema_version': SCHEMA, 'status': 'no_confirmed_mei',
                               'conflicts': conflicts, 'defaults_applied': False}}
    card, identity = found if found else ({}, {'card_id': None, 'rarity': None,
        'basis': 'accepted_current_mei_outing_without_card_identity'})
    values, reference_source = _reference(identity['rarity'], card)
    resolved, sources = {}, {}
    explicit = card.get('actual_effects') if isinstance(card.get('actual_effects'), dict) else {}
    original_source = deepcopy(card.get('actual_effects_source') or card.get('source') or {})
    for index, key in enumerate(('recovery', 'event')):
        candidate = explicit.get(EFFECT_KEYS[key])
        if _valid_percent(candidate, key):
            resolved[key] = candidate
            sources[key] = {'kind': 'explicit_profile_effect', 'original_source': original_source,
                'observed': isinstance(original_source, dict) and original_source.get('kind') == 'observed',
                'defaulted': False, 'assumed': False}
        else:
            resolved[key] = values[index]
            sources[key] = deepcopy(reference_source)
            if candidate is not None:
                sources[key]['invalid_explicit_value_ignored'] = True
    return {**resolved, 'rarity': identity['rarity'],
        'max_vitality': profile.get('max_vitality') if usable else None,
        'card': deepcopy(card) if found else None,
        'resolution': {'schema_version': SCHEMA, 'status': 'resolved_planning_inputs',
            'run_session_id': run_id, 'identity': deepcopy(identity),
            'confirmed_current_outing': bool(confirmed_outing),
            'sources': sources, 'defaults_applied': any(source.get('defaulted') for source in sources.values()),
            'contains_assumed_values': any(source.get('assumed') for source in sources.values()),
            'conflicts': conflicts, 'ignored_profile_reason': invalid_reason,
            'original_profile_or_receipt_changed': False,
            'guaranteed_game_reward_or_CN_observation_claimed': False}}


def merge_verified_packet_effects(resolved, bonus):
    """Keep caller-verified current packet effects above a table/default.

    The caller retains its existing same-run source validation. Explicit
    profile values keep the old precedence; estimates never replace an
    already accepted current packet value, including a valid zero.
    """
    if not isinstance(resolved, dict) or not isinstance(bonus, dict):
        return resolved
    result = deepcopy(resolved)
    resolution = result.get('resolution') or {}
    sources = resolution.get('sources') or {}
    for key in ('recovery', 'event'):
        if (sources.get(key) or {}).get('kind') == 'explicit_profile_effect':
            continue
        if _valid_percent(bonus.get(key), key):
            result[key] = bonus[key]
            sources[key] = {'kind': 'accepted_current_packet_effect',
                'original_source': deepcopy(bonus.get('source') or {}),
                'observed': isinstance(bonus.get('source'), dict) and bonus['source'].get('kind') == 'observed',
                'defaulted': False, 'assumed': False}
    resolution.update(sources=sources,
        defaults_applied=any(source.get('defaulted') for source in sources.values()),
        contains_assumed_values=any(source.get('assumed') for source in sources.values()))
    result['resolution'] = resolution
    return result
