"""Lower bound on fully charged members outside the current five-person offer."""

from collections import Counter, defaultdict

from core.auto_trainer import larc_charge_value
from core.auto_trainer.larc_ss_visible import visible_ss_participants


_GROUP_SIZE = 5
_STABLE_SCOPES = {'run_visual_alias', 'run_simulation_entity'}


def _stable_id(row, run_id):
    ref = row.get('member_ref') or {}
    member_id = ref.get('member_id')
    if (ref.get('status') == 'accepted' and ref.get('run_session_id') == run_id
            and ref.get('identity_scope') in _STABLE_SCOPES
            and isinstance(member_id, str) and member_id):
        return member_id
    return None


def held_ss_stock(state):
    """Use only current observations. Each unidentified slot can hide one full ID."""
    unknown = {'status': 'current_stock_unknown', 'stable_full_count': None,
               'known_current_group_count': None, 'known_group_full_overlap': None,
               'unidentified_current_group_slots': _GROUP_SIZE,
               'extra_ready_lower_bound': 0, 'whole_roster_observed': False}
    preview = state.scenario_state.get('larc_ss_preview') or {}
    if (preview.get('status') != 'accepted' or preview.get('stage') != 'preview'
            or preview.get('kind') not in {'SS', 'SSS'}
            or type(preview.get('ready_count')) is not int or preview['ready_count'] != _GROUP_SIZE):
        return unknown
    observation = preview.get('participants_observation') or {}
    group_source = observation.get('source') or {}
    source = preview.get('source') or group_source
    run_id = source.get('run_session_id')
    if (not isinstance(run_id, str) or not run_id
            or not larc_charge_value._bound(source, state, run_id)):
        return unknown

    inventory = larc_charge_value.battery_inventory(state, run_id)
    if not inventory:
        return unknown
    stable_full = {member_id for member_id, row in inventory.items()
                   if row['filled'] == 3 and _stable_id(row['person'], run_id) == member_id}
    # Optional member provenance inherits the bound current offer. A separate
    # screenshot or an omitted redundant hash does not contradict its scope.
    group_bound = (group_source.get('run_session_id') in (None, run_id)
                   and group_source.get('turn_index') in (None, state.turn))
    ids_by_slot = defaultdict(set)
    for row in visible_ss_participants(preview) if group_bound else ():
        slot = row.get('slot')
        member_id = _stable_id(row, run_id)
        if member_id and type(slot) is int and 0 <= slot < _GROUP_SIZE:
            ids_by_slot[slot].add(member_id)
    occurrences = Counter(member_id for ids in ids_by_slot.values() for member_id in ids)
    known_group = {member_id for ids in ids_by_slot.values() if len(ids) == 1
                   for member_id in ids if occurrences[member_id] == 1
                   and (member_id not in inventory or inventory[member_id]['filled'] == 3)}
    unidentified = _GROUP_SIZE - len(known_group)
    return {'status': 'current_visible_lower_bound', 'stable_full_count': len(stable_full),
            'known_current_group_count': len(known_group),
            'known_group_full_overlap': len(stable_full & known_group),
            'unidentified_current_group_slots': unidentified,
            'group_source_bound': group_bound, 'whole_roster_observed': False,
            'extra_ready_lower_bound': max(0, len(stable_full - known_group) - unidentified)}
