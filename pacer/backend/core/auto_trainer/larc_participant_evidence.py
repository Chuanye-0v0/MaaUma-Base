"""Current L'Arc training rows, separate from completeness of the page."""


def training_rows(candidate, *, typed=False, require_tab=False):
    packet = (candidate.scenario_mechanics.get('larc') or {}).get('training_participants') or {}
    rows = packet.get('participants')
    status = packet.get('status')
    if status not in {'accepted', 'abstained'} or not isinstance(rows, list):
        return None
    if typed and packet.get('schema_version') != 'larc_training_participants.v1':
        return None
    tab = packet.get('training_type')
    if tab is not None and tab != candidate.training_type:
        return None
    if status == 'accepted':
        if (typed or require_tab) and tab is None:
            return None
        return rows, True
    # Completeness and provenance are separate. Consumers keep their own
    # source rules and decide which fields of these rows are usable.
    observed = [row for row in rows if isinstance(row, dict)]
    return (observed, False) if observed else None


def is_mei(row):
    identity = row.get('identity') or {}
    role = row.get('role') or {}
    return (identity.get('status') == 'known' and identity.get('character_id') == 'satake_mei'
            and role.get('status') == 'accepted' and role.get('kind') == 'friend')


def visible_charge(row):
    charge = row.get('charge') or {}
    filled, net = charge.get('filled_cells'), charge.get('preview_cells')
    if (charge.get('status') == 'accepted' and charge.get('capacity') == 3
            and type(filled) is int and type(net) is int and 0 <= filled <= 3
            and 0 <= net <= 3 - filled):
        return filled, net
    return None


def domestic_charge_unlocked(state) -> bool:
    """Return True when L'Arc domestic +1..+3 direct training charge is active (Turn 3+, non-overseas)."""
    if state is None:
        return False
    scenario_state = (
        state.get('scenario_state')
        if isinstance(state, dict)
        else getattr(state, 'scenario_state', None)
    )
    scenario_state = scenario_state if isinstance(scenario_state, dict) else {}
    scenario = scenario_state.get('scenario')
    if scenario not in (None, 'larc'):
        return False
    turn = state.get('turn') if isinstance(state, dict) else getattr(state, 'turn', None)
    if type(turn) is int and turn < 3:
        return False
    period_wrap = scenario_state.get('larc_period')
    period_ok = (
        isinstance(period_wrap, dict)
        and period_wrap.get('status') in {'accepted', 'verified'}
        and isinstance(period_wrap.get('value'), dict)
    )
    period = period_wrap['value'] if period_ok else {}
    year_index = period.get('year_index')
    month = period.get('month')
    pre_debut = period.get('pre_debut')
    stage = period.get('stage')
    if stage in {'overseas', 'abroad'} or (year_index in (2, 3) and month in (7, 8, 9)):
        return False
    if period_ok and pre_debut is False:
        return turn >= 3 if type(turn) is int else True
    return type(turn) is int and turn >= 3


def effective_domestic_charge(
    person,
    *,
    state=None,
    tab_participants=None,
    candidate=None,
    training_type=None,
    friendship=None,
    allow_derived=None,
) -> tuple[int, int, str] | None:
    """Resolve (filled, net_gain, basis) for consumers without mutating person['charge']."""
    if not isinstance(person, dict):
        return None
    charge = person.get('charge')
    if not isinstance(charge, dict) or charge.get('status') != 'accepted':
        return None
    cap = charge.get('capacity')
    filled = charge.get('filled_cells')
    raw_preview = charge.get('preview_cells')
    if (
        type(cap) is not int or cap != 3
        or type(filled) is not int or not 0 <= filled <= 3
        or type(raw_preview) is not int or not 0 <= raw_preview <= 3 - filled
    ):
        return None
    if filled == 3 or raw_preview > 0:
        return (filled, 0 if filled == 3 else raw_preview, 'observed')

    role = person.get('role')
    role_kind = role.get('kind') if isinstance(role, dict) else None
    if is_mei(person) or role_kind in {'friend', 'director', 'reporter'}:
        return (filled, 0, 'observed')

    derived_ok = bool(allow_derived) if allow_derived is not None else domestic_charge_unlocked(state)
    if not derived_ok:
        return (filled, 0, 'observed')

    tab_rows = [row for row in (tab_participants or ()) if isinstance(row, dict)]
    for other in tab_rows:
        if other is person:
            continue
        oc = other.get('charge')
        if not isinstance(oc, dict) or oc.get('status') != 'accepted':
            continue
        ocap = oc.get('capacity')
        ofilled = oc.get('filled_cells')
        oprev = oc.get('preview_cells')
        if (
            type(ocap) is int and ocap == 3
            and type(ofilled) is int and ofilled == 0
            and type(oprev) is int and 1 <= oprev <= 3
        ):
            return (filled, min(3 - filled, oprev), 'derived_from_tab_intensity')

    tab = training_type or (
        candidate.get('training_type')
        if isinstance(candidate, dict)
        else getattr(candidate, 'training_type', None)
    )
    friend_flag = (
        friendship
        if friendship is not None
        else (
            candidate.get('friendship')
            if isinstance(candidate, dict)
            else getattr(candidate, 'friendship', None)
        )
    )
    rb_count = 0
    all_support_bonds_accepted = True
    for row in tab_rows:
        r_role = row.get('role')
        if not isinstance(r_role, dict) or r_role.get('kind') != 'support':
            continue
        r_bond = row.get('bond')
        bond_ok = isinstance(r_bond, dict) and r_bond.get('status') == 'accepted'
        if not bond_ok:
            all_support_bonds_accepted = False
            continue
        fill = r_bond.get('fill_ratio_estimate')
        if fill is None:
            fill = r_bond.get('fill')
        is_rb_bond = (
            r_bond.get('is_max') is True
            or r_bond.get('color') in {'orange', 'yellow'}
            or (type(fill) in {int, float} and fill >= 0.79)
        )
        if tab is not None and r_role.get('icon_type') == tab and is_rb_bond:
            rb_count += 1

    if tab is not None and tab_rows and all_support_bonds_accepted:
        intensity = 1 + rb_count
        return (filled, min(3 - filled, intensity), 'derived_from_rainbow')
    if friend_flag is True:
        intensity = max(2, 1 + rb_count)
        return (filled, min(3 - filled, intensity), 'derived_from_rainbow')
    return (filled, 1, 'derived_lower_bound')
