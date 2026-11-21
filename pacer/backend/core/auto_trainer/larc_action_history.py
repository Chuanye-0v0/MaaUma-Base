"""Optional strategy history from existing observations and committed receipts.

This module does not execute, authorize, reconstruct or retry any action.
"""
from copy import deepcopy
import hashlib,json
from pathlib import Path

FILE='larc_confirmed_strategy_history.json'
TYPES={'train','ss','rest','outing','race'}
STATS=('speed','stamina','power','guts','wisdom','skill_pt')


def ss_wins_context(run_id, turn, *, current=None, roster=None):
    """Read explicit cumulative results only; a confirmation/preview is not a win.

    The current production roster stores pre-match participants and confirmation
    receipts. Those alone deliberately leave this value unknown. An accepted
    display can supply the total without requiring historical result coverage.
    """
    result = {'schema_version': 'larc.ss_wins.v1', 'run_session_id': run_id,
        'through_turn': turn, 'status': 'unknown', 'value': None,
        'basis': 'actual_member_results_or_explicit_cumulative_display',
        'coverage': {'ss_execution_count_is_not_win_count': True}}
    current_source = current.get('source') or {} if isinstance(current, dict) else {}
    if (isinstance(current, dict) and current.get('status') == 'accepted'
            and current.get('run_session_id') == run_id and current.get('through_turn') == turn
            and current.get('basis') in {'current_display', 'accepted_member_receipts'}
            and type(current.get('value')) is int and current['value'] >= 0
            and current_source.get('run_session_id') == run_id and current_source.get('turn_index') == turn
            and current_source.get('sha256')
            and not any(current.get(k) is True for k in ('planned','projected','hypothetical','simulation'))):
        return {**result, **deepcopy(current)}
    if not isinstance(roster, dict) or roster.get('run_session_id') != run_id:
        return {**result, 'reason': 'accepted_cumulative_ss_wins_unavailable'}
    entries = roster.get('ss_execution_participants') or []
    completed = {}; total = 0; known = 0
    for entry in entries:
        receipt = entry.get('receipt') or {}
        at = receipt.get('turn_index'); identity = entry.get('source_sha256')
        if (type(at) is not int or at >= turn or not identity or identity in completed
                or receipt.get('run_session_id') != run_id or receipt.get('tap_performed') is not True
                or receipt.get('action_id') != 'safe_advance.larc_ss_confirm'):
            continue
        completed[identity] = True
        # A future real result producer may attach this packet to its accepted
        # receipt. Never read win_guaranteed, blue badges or all-wins rewards.
        actual = receipt.get('member_results') or {}
        rows = actual.get('participants') or [] if isinstance(actual, dict) else []
        source = actual.get('source') or {} if isinstance(actual, dict) else {}
        expected = len((entry.get('participants_observation') or {}).get('participants') or [])
        if (not isinstance(actual, dict) or actual.get('status') != 'accepted'
                or actual.get('stage') != 'result' or source.get('run_session_id') != run_id
                or source.get('turn_index') != at or not source.get('sha256') or not rows
                or len(rows) != expected
                or any(row.get('outcome') not in {'win','loss'} for row in rows)):
            continue
        known += 1; total += sum(row['outcome'] == 'win' for row in rows)
    execution_ids = set(roster.get('ss_executions') or [])
    result['coverage'].update(executions=len(execution_ids), result_receipts=known,
                              known_wins_lower_bound=total)
    if (execution_ids or turn == 1) and execution_ids == set(completed) and known == len(execution_ids):
        return {**result, 'status': 'accepted', 'value': total, 'basis': 'accepted_member_receipts'}
    return {**result, 'reason': 'actual_ss_result_coverage_incomplete'}

def _read(root,run_id):
    path=Path(root)/FILE
    data=json.loads(path.read_text(encoding='utf-8-sig'))if path.is_file()else{
        'schema_version':'larc.confirmed_strategy_history.v1','run_session_id':run_id,'actions':[],'ss_offers':[]}
    if not isinstance(data,dict)or data.get('run_session_id')!=run_id:raise ValueError('strategy_history_run_mismatch')
    if data.get('schema_version')!='larc.confirmed_strategy_history.v1':raise ValueError('strategy_history_schema_unknown')
    if any(not isinstance(data.get(key),list)or any(not isinstance(r,dict)for r in data[key])
           for key in ('actions','ss_offers')):raise ValueError('strategy_history_rows_unavailable')
    return data

def _save(root,data):
    path=Path(root)/FILE;path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.json.tmp');temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
    temporary.replace(path);return path

def record_confirmed_action(root,run_id,turn,kind,receipt_path):
    """Called only after the original orchestrator commits its action receipt."""
    aliases={'normal_outing':'outing'};kind=aliases.get(kind,kind)
    if kind not in TYPES or type(turn)is not int or turn<0:raise ValueError('confirmed_strategy_action_identity')
    path=Path(receipt_path);raw=path.read_bytes();receipt=json.loads(raw)
    if not isinstance(receipt,dict):raise ValueError('confirmed_strategy_receipt_required')
    if receipt.get('run_session_id',run_id)!=run_id or receipt.get('turn_index',turn)!=turn:
        raise ValueError('confirmed_strategy_receipt_scope')
    if kind=='ss':
        confirmed=(receipt.get('tap_performed')is True and receipt.get('action_id')=='safe_advance.larc_ss_confirm')
    else:
        reasons={'train':'executed','rest':'executed_rest_confirmed','outing':('executed_normal_outing','executed_mei_outing'),
                 'race':'fan_shortfall_entry_consumed'}
        expected=reasons[kind];expected=(expected,)if isinstance(expected,str)else expected
        confirmed=receipt.get('performed')is True and receipt.get('reason')in expected
    if not confirmed:raise ValueError('strategy_history_requires_existing_confirmed_consumption')
    digest=hashlib.sha256(raw).hexdigest();data=_read(root,run_id)
    row={'turn_index':turn,'action_type':kind,'execution_confirmed':True,
        'source':{'kind':'executed_action_receipt','path':str(path),'receipt_id':digest,
                  'run_session_id':run_id,'turn_index':turn}}
    old=[a for a in data['actions']if a.get('turn_index')==turn]
    if old and old!=[row]:raise ValueError('strategy_history_ambiguous_action_turn')
    if not old:data['actions'].append(row)
    data['actions']=sorted(data['actions'],key=lambda r:r['turn_index'])[-128:]
    return _save(root,data)

def record_ss_offer(root,run_id,turn,state,preview):
    """A seen full offer is evidence of readiness, never an executed match."""
    if (type(turn)is not int or turn!=state.turn or preview.get('status')!='accepted'
            or preview.get('stage')!='preview' or preview.get('ready_count')!=5
            or preview.get('kind')not in ('SS','SSS')):return None
    source=preview.get('source',{})
    if not isinstance(source,dict):return None
    if source.get('run_session_id')!=run_id or source.get('turn_index')!=turn:return None
    if not source.get('sha256')or not(source.get('path')or source.get('absolute_path')):return None
    from core.auto_trainer.larc_attribute_plan import period_value
    gains=preview.get('stats_gain',{})
    complete=all(type(gains.get(k))is int and gains[k]>=0 for k in STATS)
    row={'turn_index':turn,'kind':preview['kind'],'ready_count':5,'period':deepcopy(period_value(state)),
        'stats_gain':deepcopy(gains)if complete else None,'stats_before':deepcopy(state.current_stats),
        'units':'observed_display_gains_at_stats_before; not future raw gains',
        'source':{'kind':'observed_ss_ready_frame','path':source.get('path',source.get('absolute_path')),
            'sha256':source['sha256'],'run_session_id':run_id,'turn_index':turn}}
    data=_read(root,run_id)
    # Recollecting the same turn replaces this observation, not another cycle.
    data['ss_offers']=[r for r in data['ss_offers']if r.get('turn_index')!=turn]+[row]
    data['ss_offers']=sorted(data['ss_offers'],key=lambda r:r['turn_index'])[-128:]
    return _save(root,data)

def ss_deadline_history(root,run_id,turn):
    """Project full offers + committed main actions, without a new clock.

    Repeated scoring and free purchases never write consumption here. Sources
    were checked by the existing producer; do not turn model receipts into
    device receipts or perform another input while reading this projection.
    """
    result={'schema_version':'larc.ss_deadline_history.v1','status':'unknown',
        'run_session_id':run_id,'through_turn':turn,'actions':[],'full_offers':[]}
    if not isinstance(run_id,str)or not run_id or type(turn)is not int or turn<1:
        return {**result,'reason':'strategy_deadline_identity_unknown'}
    try:
        path=Path(root)/FILE
        if not path.is_file():
            # A missing optional history later in a career is not evidence that
            # its one deferral has never been used. Only a first main
            # observation has a known empty preceding main-action prefix.
            if turn==1:
                return {**result,'status':'accepted','reason':'new_run_empty_main_prefix'}
            return {**result,'reason':'strategy_history_file_missing'}
        data=_read(root,run_id)
        actions={};offers={}
        def source_of(row,kind,identifier):
            source=row.get('source')
            if (not isinstance(source,dict)or source.get('kind')!=kind
                    or source.get('run_session_id')!=run_id
                    or source.get('turn_index')!=row['turn_index']
                    or not isinstance(source.get('path'),str)or not source['path']
                    or not isinstance(source.get(identifier),str)or not source[identifier]):
                raise ValueError('strategy_deadline_source_scope_unknown')
            return deepcopy(source)
        for row in data['actions']:
            index=row.get('turn_index')
            if type(index)is not int or index<0:
                raise ValueError('strategy_deadline_action_turn_unknown')
            if index>=turn:continue
            if row.get('execution_confirmed')is not True or row.get('action_type')not in TYPES:
                raise ValueError('strategy_deadline_action_not_confirmed_main')
            item={'turn_index':index,'action_type':row['action_type'],'execution_confirmed':True,
                'source':source_of(row,'executed_action_receipt','receipt_id')}
            if index in actions and actions[index]!=item:
                raise ValueError('strategy_history_ambiguous_action_turn')
            actions[index]=item
        # Merely writing today's full offer must not launder a missing late
        # history into a new grace. A committed SS is a real reset anchor;
        # otherwise every preceding main action from the new run is required.
        latest_ss=max((i for i,row in actions.items()if row['action_type']=='ss'),default=None)
        prefix_start=latest_ss if latest_ss is not None else 1
        missing=[i for i in range(prefix_start,turn)if i not in actions]
        if missing:
            return {**result,'reason':'strategy_deadline_main_prefix_incomplete',
                'prefix_start_turn':prefix_start,'latest_confirmed_ss_turn':latest_ss,
                'missing_main_turns':missing}
        for row in data['ss_offers']:
            index=row.get('turn_index')
            if type(index)is not int or index<0:
                raise ValueError('strategy_deadline_offer_turn_unknown')
            if index>turn or row.get('kind')not in ('SS','SSS')or row.get('ready_count')!=5:
                continue
            # Readiness does not require complete numeric rewards. An SSS is
            # the same full-group deferral obligation, not a fresh budget.
            item={'turn_index':index,'kind':row['kind'],'ready_count':5,
                'source':source_of(row,'observed_ss_ready_frame','sha256')}
            if isinstance(row.get('period'),dict):item['period']=deepcopy(row['period'])
            offers[index]=item # A same-turn recollection replaces an offer.
        return {**result,'status':'accepted','reason':'confirmed_main_prefix_and_observed_full_offers',
            'prefix_start_turn':prefix_start,'latest_confirmed_ss_turn':latest_ss,
            'actions':[actions[i]for i in sorted(actions)],
            'full_offers':[offers[i]for i in sorted(offers)]}
    except (OSError,ValueError,TypeError,KeyError,UnicodeError)as error:
        return {**result,'reason':str(error)or type(error).__name__}


def action_prefix(root,run_id,turn,training_samples):
    """Provide previous sample + real action, leaving current date unclaimed."""
    data=_read(root,run_id)
    previous=[s for s in training_samples if type(s.get('turn_index'))is int and s['turn_index']<turn]
    if not previous:return None
    previous=max(previous,key=lambda r:r['turn_index'])
    if previous['turn_index']+1!=turn:return None
    actions=[a for a in data['actions']if previous['turn_index']<=a['turn_index']<turn]
    if len(actions)!=1 or actions[0]['turn_index']!=previous['turn_index']:return None
    return {'run_session_id':run_id,'to_turn_index':turn,'previous_sample':deepcopy(previous),
            'confirmed_action':deepcopy(actions[0])}

def expectation_transition(state,prefix=None):
    prefix=prefix if prefix is not None else state.scenario_state.get('larc_expectation_action_prefix')
    if not isinstance(prefix,dict)or prefix.get('to_turn_index')!=state.turn:return None
    inputs=state.scenario_state.get('larc_attribute_inputs',{})
    run_id=prefix.get('run_session_id')
    if not isinstance(inputs,dict)or inputs.get('run_session_id')!=run_id:return None
    previous=prefix.get('previous_sample',{});action=prefix.get('confirmed_action',{})
    if not isinstance(previous,dict)or not isinstance(action,dict):return None
    if action.get('execution_confirmed')is not True or action.get('turn_index')!=previous.get('turn_index'):return None
    from core.auto_trainer.larc_attribute_plan import period_value
    from core.auto_trainer.larc_calendar import period_ordinal
    before=previous.get('period',{});after=period_value(state)
    a,b=period_ordinal(before),period_ordinal(after)
    if a is None or b is None or b!=a+1 or previous.get('turn_index')+1!=state.turn:return None
    return {'schema_version':'larc.expectation_transition.v1','run_session_id':run_id,
        'from_turn_index':previous['turn_index'],'to_turn_index':state.turn,
        'from_period':deepcopy(before),'to_period':deepcopy(after),'action_type':action['action_type'],
        'execution_confirmed':True,'calendar_advanced':True,'source':deepcopy(action['source'])}

def ss_cycle_history(root,run_id,turn):
    data=_read(root,run_id)
    actions=[a for a in data['actions']if a['turn_index']<turn and a.get('execution_confirmed')is True]
    offers=[r for r in data['ss_offers']if r['turn_index']<=turn]
    cycles=[]
    for ss in (a for a in actions if a['action_type']=='ss'):
        later=[r for r in offers if r['turn_index']>ss['turn_index']]
        if not later:continue
        ready=min(later,key=lambda r:r['turn_index']);start,end=ss['turn_index'],ready['turn_index']
        between=[a for a in actions if start<=a['turn_index']<end]
        if sorted(a['turn_index']for a in between)!=list(range(start,end)):continue
        if any(a['action_type']=='ss'and a['turn_index']>start for a in between):continue
        # Unknown unrecorded fixed races/interventions cannot form a clean cycle.
        if any(a['action_type']not in ('ss','train','rest','outing')for a in between):continue
        starting=[r for r in offers if r['turn_index']==start]
        if not starting:continue
        from core.auto_trainer.larc_calendar import period_ordinal,is_overseas
        a,b=period_ordinal(starting[-1]['period']),period_ordinal(ready['period'])
        if a is None or b is None or b-a!=end-start:continue
        domestic=all(not is_overseas(day)for day in range(a,b+1))
        if not domestic:continue
        cycles.append({'ss_turn_index':start,'ready_turn_index':end,'domestic_only':True,
            'from_period':deepcopy(starting[-1]['period']),'to_period':deepcopy(ready['period']),
            'intervening_training_actions':sum(a['action_type']=='train'for a in between),
            'all_intervening_main_actions_accounted':True,'source_receipt':deepcopy(ss['source']),
            'source_ready_frame':deepcopy(ready['source'])})
    return {'status':'accepted','run_session_id':run_id,'through_turn':turn,
        'ordinary_offers':[deepcopy(r)for r in offers if r['kind']=='SS'and r.get('stats_gain')is not None],
        'readiness_intervals':cycles[-12:],'unknown_next_reward_and_win_probability':True}
