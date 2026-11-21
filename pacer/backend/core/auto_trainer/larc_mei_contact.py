"""Run-local first Mei training contact; no promise of an outing unlock."""

from core.auto_trainer.larc_participant_evidence import is_mei, training_rows
import json
from pathlib import Path


FILE='larc_mei_contact.json'


def mei_present(candidate):
    view = training_rows(candidate)
    return view is not None and any(is_mei(person) for person in view[0])


def load_contact(session_dir,run_id,turn):
    root=Path(session_dir)
    unknown={'run_session_id':run_id,'status':'unrecorded','basis':'no_recorded_contact_not_proof_of_no_manual_contact'}
    for name in (FILE,'larc_friend_outing.json'):
        path=root/name
        if not path.is_file():continue
        try:
            data=json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data,dict):raise ValueError('contact_record_invalid')
            if data.get('run_session_id')!=run_id:
                if name==FILE:unknown={**unknown,'status':'unknown','reason':'contact_record_run_mismatch'}
                continue
            if name==FILE:
                if data.get('status')=='training_selected' and type(data.get('turn_index')) is int and data['turn_index']<turn:
                    return data
                unknown={**unknown,'status':'unknown','reason':'contact_record_not_historical'}
            elif data.get('unlocked') is True or (data.get('unlock_event') or {}).get('status') in {
                    'guaranteed_unlock','awaiting_result','unavailable'}:
                return {'run_session_id':run_id,'status':'unlock_event_seen','basis':'same_run_outing_session'}
        except (ValueError,OSError,TypeError):
            unknown={**unknown,'status':'unknown','reason':'contact_record_unreadable'}
    return unknown


def initial_contact_value(state,candidate,source,weights):
    record=state.scenario_state.get('larc_mei_contact') or {}
    result={'score':0.,'basis':'bounded_first_contact_preference_not_guaranteed_unlock','status':record.get('status','unknown')}
    if (not source or record.get('run_session_id')!=source.get('run_session_id')
            or record.get('status')!='unrecorded' or not mei_present(candidate)):
        return result
    from core.auto_trainer.larc_friend_outing import domestic_period
    if not domestic_period(state.scenario_state.get('larc_period')):return result
    view = training_rows(candidate)
    if view is None:
        return result
    people = view[0]
    # Existing >=60-bond coverage/unlocked context no longer needs this prior.
    if not any((p.get('identity') or {}).get('character_id')=='satake_mei'
               and (p.get('bond') or {}).get('status')=='accepted'
               and type(p['bond'].get('fill_ratio_estimate')) in {int,float}
               and 0<=p['bond']['fill_ratio_estimate']<.6 for p in people):return result
    history=state.scenario_state.get('larc_attribute_inputs') or {}
    if history.get('run_session_id')!=source['run_session_id']:return result
    missed={row['turn_index'] for row in history.get('training_history',[])
            if type(row.get('turn_index')) is int and row['turn_index']<state.turn and row.get('mei_available') is True}
    units=.5+.5*min(3,len(missed))
    period=(state.scenario_state.get('larc_period') or {}).get('value') or {}
    golden_start=any((p.get('identity') or {}).get('status')=='known'
        and p['identity'].get('character_id')=='gold_ship'
        and (p.get('ss_reward') or {}).get('status')=='accepted'
        and p['ss_reward'].get('kind')=='charming' and p['ss_reward'].get('earned') is not True for p in people)
    opening=period.get('year_index')==1 and period.get('pre_debut') is True and golden_start
    if opening:units=2.  # Use the existing cap when both opening routes can start on this same page.
    return {**result,'score':round(units*weights.get('friend_contact',weights['effective_bond']),4),'missed_observed_opportunities':len(missed),
            'contact_units':units,'maximum_units':2.,'opening_contact_priority':opening,'unlock_claimed':False}


def record_contact(session_dir,run_id,turn,state_path,receipt):
    """Call after the existing training receipt is committed, never on a preview."""
    if receipt.get('performed') is not True:return None
    try:
        from core.auto_trainer.state import TrainingTurnState
        state=TrainingTurnState.from_dict(json.loads(Path(state_path).read_text(encoding='utf-8-sig')))
    except (OSError,ValueError,TypeError):return None
    if state.turn!=turn:return None
    candidate=next((c for c in state.training_options if c.training_type==receipt.get('target')),None)
    if candidate is None or not mei_present(candidate):return None
    source=candidate.raw_data.get('capture') or {}
    if source.get('run_session_id')!=run_id or source.get('turn_index')!=turn:return None
    root=Path(session_dir);path=root/FILE
    if path.exists():
        old=json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(old,dict):raise ValueError('mei_contact_record_invalid')
        if old.get('run_session_id')!=run_id:raise ValueError('mei_contact_run_mismatch')
        if old.get('status')=='training_selected':return old
    value={'schema_version':'larc_mei_contact.v1','run_session_id':run_id,'status':'training_selected',
        'turn_index':turn,'training_type':candidate.training_type,'unlock_claimed':False,
        'basis':'committed_training_with_current_frame_mei_identity'}
    root.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8');temporary.replace(path)
    return value
