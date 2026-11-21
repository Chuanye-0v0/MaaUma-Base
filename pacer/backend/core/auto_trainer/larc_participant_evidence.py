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
