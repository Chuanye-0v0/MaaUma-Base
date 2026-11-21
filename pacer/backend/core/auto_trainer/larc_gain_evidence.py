"""Separate unknown extra gains from numbers actually used in a decision."""
from core.auto_trainer.state import STAT_KEYS
from core.training_gain_reliability import classify_reliable_exact_gain, validate_dynamic_gain_column


def gain_evidence(candidate):
    data=candidate.raw_data.get('stats_gain_ocr') or {}
    columns=data.get('columns',[]) if isinstance(data,dict) else []
    rows=[]
    for column in columns:
        if not isinstance(column,dict):continue
        for row in column.get('rows',[]):
            if not isinstance(row,dict) or row.get('occupancy')!='present' or row.get('status')=='accepted':continue
            rows.append({'attribute':column.get('attribute_id'),'row_index':row.get('row_index'),
                'status':row.get('status'),'reason':row.get('reason'),
                'raw_text':str(row.get('raw_text') or '')[:32],
                'column_status':column.get('status'),'canonical_status':column.get('canonical_status'),
                'canonical_value':column.get('canonical_value')})
    used={key:value for key,value in candidate.stats_gain.items() if key in STAT_KEYS and type(value) is int and value>0}
    affected={row['attribute'] for row in rows}
    # The formal capture adapter has already removed unreliable columns.
    # Re-check only an overlapping claim, or a malformed unattributed row.
    check=set(used) if any(key not in STAT_KEYS for key in affected) else set(used)&affected
    unverified=[]
    for key in sorted(check):
        matches=[c for c in columns if isinstance(c,dict) and c.get('attribute_id')==key]
        if len(matches)!=1:
            unverified.append(key);continue
        col=matches[0];value=used[key]
        exact=classify_reliable_exact_gain(col,attribute_id=key)
        if not (exact.get('status')=='reliable_exact' and exact.get('value')==value
                or validate_dynamic_gain_column(data,col,attribute=key,value=value)):
            unverified.append(key)
    omitted=sorted(key for key in affected if key in STAT_KEYS and key not in used)
    return {'status':'unverified_used_gain' if unverified else 'partial_confirmed_gains' if omitted else
            'confirmed_gains_with_auxiliary_unknown' if rows else 'complete_or_no_row_diagnostics',
        'used_gains':used,'unscored_unknown_attributes':omitted,'unverified_used_attributes':unverified,
        'unresolved_rows':rows,'basis':'only_confirmed_numeric_gains_are_scored; unknown_extra_gains_not_asserted_zero'}
