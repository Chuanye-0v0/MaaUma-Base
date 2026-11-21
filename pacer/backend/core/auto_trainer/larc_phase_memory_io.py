"""Optional observed-phase file bridge. Never changes the original recent12."""
from copy import deepcopy
import json
from pathlib import Path
from core.auto_trainer import larc_first_expedition_reference as producer

FILE='first_expedition_reference.json'


def _read(path):return json.loads(path.read_text(encoding='utf-8-sig'))


def _planned(packet):
    if not isinstance(packet,dict):return False
    source=str(packet.get('source_kind','')).lower()
    return (any(packet.get(k)is True for k in('planned','projected','hypothetical','is_projection'))
        or packet.get('status')in('planned','projected','hypothetical')
        or source.startswith(('planning','projected','hypothetical','forecast','simulation','model','accepted_model')))


def observed_layout_sample(state, run_id, sample):
    """Attach domestic risk/energy/ownership to the SAME accepted five-page sample.

    This is observation storage, never a new score, main action or prediction.
    It is opt-in for 1.8 so saved older strategies keep their existing records.
    """
    from math import isfinite
    from core.auto_trainer.larc_calendar import period_ordinal
    context = getattr(state, 'scenario_state', {}) or {}
    period = context.get('larc_period') or {}
    options = getattr(state, 'training_options', ())
    captures = [(getattr(c, 'raw_data', {}) or {}).get('capture') or {} for c in options]
    owned = context.get('larc_expedition_context') or {}
    if (context.get('simulation') or 'simulation_truth' in context or _planned(context)
            or _planned(owned) or any(_planned(c) for c in captures)):
        return None
    if (period.get('status') != 'verified' or not isinstance(sample, dict)
            or sample.get('turn_index') != state.turn or sample.get('period') != period.get('value')
            or len(options) != 5 or {c.training_type for c in options} != {'speed','stamina','power','guts','wisdom'}
            or any(c.get('run_session_id') != run_id or c.get('turn_index') != state.turn
                   or not c.get('sha256') for c in captures)):
        return None
    result = deepcopy(sample)
    result.update(run_session_id=run_id, calendar_ordinal=period_ordinal(sample['period']),
                  source_kind='accepted_normal_pipeline_history_sample',
                  known_owned_levels=None, risk_observations={})
    levels = owned.get('levels')
    if (owned.get('status') == 'accepted' and owned.get('run_session_id') == run_id
            and type(owned.get('through_turn')) is int and owned['through_turn'] <= state.turn
            and isinstance(levels, list) and len(levels) == 10):
        result['known_owned_levels'] = [v if type(v) is int and 0 <= v <= 3 else None for v in levels]
        result['owned_source_through_turn'] = owned['through_turn']
    for candidate, capture in zip(options, captures):
        failure = candidate.failure_rate
        if type(failure) not in (int, float) or not isfinite(failure) or not 0 <= failure <= 100:
            continue
        energy = (result.get('forecast_metadata', {}).get(candidate.training_type) or {}).get('energy_observation') or {}
        from core.auto_trainer.larc_tail263.strategy182 import conditional_income_quote
        income=conditional_income_quote(candidate,sample['period'],run_id=run_id,turn=state.turn)
        if income is not None:
            key='conditional_income_observation'if income.get('observed')is True else'conditional_income_reference'
            result.setdefault('forecast_metadata',{}).setdefault(candidate.training_type,{})[key]=income
        result['risk_observations'][candidate.training_type] = {
            'run_session_id': run_id, 'turn_index': state.turn, 'failure_rate': failure,
            'source': deepcopy(capture), 'public_training_level': None,
            'energy_observation': deepcopy(energy),
            'anchor_before_points': deepcopy(energy.get('before_points')),
            'capacity_points': deepcopy(energy.get('capacity_points'))}
    return result


def record_optional(session_dir,run_id,state,sample):
    """Use the SAME already-computed sample. Unknown ownership stays unknown."""
    path=Path(session_dir)/FILE
    context=getattr(state,'scenario_state',{})or{}
    owned=context.get('larc_expedition_context')or{}
    if context.get('simulation')or 'simulation_truth'in context:
        return {'updated':False,'reason':'model_source_not_production_memory'}
    if _planned(context)or _planned(owned):return {'updated':False,'reason':'planned_state_not_observed_memory'}
    period=context.get('larc_period')or{}
    captures=[(getattr(c,'raw_data',{})or{}).get('capture')or{}for c in getattr(state,'training_options',[])or[]]
    if any(_planned(c)for c in captures):return {'updated':False,'reason':'planned_capture_not_observed_memory'}
    if (period.get('status')!='verified' or not isinstance(sample,dict)
            or sample.get('turn_index')!=state.turn or sample.get('period')!=period.get('value')
            or len(captures)!=5 or any(c.get('run_session_id')!=run_id or c.get('turn_index')!=state.turn for c in captures)):
        return {'updated':False,'reason':'sample_source_run_turn_or_period_unknown'}
    from math import isfinite
    if any(type(c.failure_rate)not in(int,float)or not isfinite(c.failure_rate)or not 0<=c.failure_rate<=100
           or not isinstance((c.raw_data or{}).get('capture',{}).get('sha256'),str)
           for c in state.training_options):
        return {'updated':False,'reason':'same_sample_risk_source_unknown_previous_memory_preserved'}
    optional_owned=None
    through=owned.get('through_turn')
    if (owned.get('status')=='accepted'and owned.get('run_session_id')==run_id
            and type(through)is int and through<=state.turn and 'levels'in owned):
        optional_owned={'status':'accepted','run_session_id':run_id,'turn_index':state.turn,
            'levels':deepcopy(owned['levels']),'original_known_through_turn':through}
    source_kinds={c.get('source_kind')for c in captures}
    source_kind=('accepted_offline_feedback_numeric_history_sample'if any(str(s).startswith('offline_')for s in source_kinds)else
                 'accepted_normal_pipeline_history_sample')
    sample=deepcopy(sample)
    sample['risk_observations']={c.training_type:{'run_session_id':run_id,'turn_index':state.turn,
        'failure_rate':c.failure_rate,'source':deepcopy((c.raw_data or {}).get('capture')or{}),
        'public_training_level':None}for c in state.training_options}
    packet={'status':'accepted','period_status':'verified','run_session_id':run_id,
            'sample':sample,'source_kind':source_kind}
    steps={c.get('step_count')for c in captures}
    if len(steps)==1 and type(next(iter(steps)))is int:packet['observation_step']=next(iter(steps))
    try:
        existing=_read(path)if path.is_file()else None
        archive,diag=producer.update(existing,packet,optional_owned)
        if not diag.get('updated'):return diag
        temporary=path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(archive,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        temporary.replace(path)
        return {**diag,'path':str(path),'owned_source_through_turn':through if optional_owned else None}
    except (OSError,ValueError,TypeError,KeyError,UnicodeError)as exc:
        return {'updated':False,'reason':'optional_file_unavailable','error_type':type(exc).__name__,
                'path':str(path),'original_file_preserved':True}


def load_optional(session_dir,run_id,turn):
    path=Path(session_dir)/FILE
    try:
        if not path.is_file():rows,diag=[],{'status':'unavailable','reason':'optional_file_missing'}
        else:rows,diag=producer.past(_read(path),run_id,turn)
    except (OSError,ValueError,TypeError,KeyError,UnicodeError)as exc:
        rows,diag=[],{'status':'unavailable','reason':'optional_file_invalid','error_type':type(exc).__name__}
    accepted=[]
    for row in rows:
        metadata=row.get('forecast_metadata')or{}
        modeled=(_planned(row)or row.get('simulation')or 'simulation_truth'in row
            or any(_planned((m.get('energy_observation')or{}).get('source')or{})for m in metadata.values()if isinstance(m,dict)))
        if not modeled:accepted.append(row)
    if len(accepted)!=len(rows):
        diag={**diag,'reason':'model_or_projected_records_excluded_from_production_memory','model_records_excluded':len(rows)-len(accepted),'records':len(accepted)}
    rows=accepted
    packet={'schema_version':'larc.first_expedition_reference.context.v1','run_session_id':run_id,
            'through_turn':turn,'status':diag['status'],'records':rows,
            'source_kind':'optional_observed_phase_memory','future_same_yield_guaranteed':False}
    return packet,{**diag,'path':str(path),'not_a_new_execution_gate':True}
