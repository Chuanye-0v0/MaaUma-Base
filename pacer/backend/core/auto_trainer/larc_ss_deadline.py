"""One real main-action grace after a full SS offer; pure status only.

This rule neither executes SS nor claims that a visible match is guaranteed
to win. Free purchases and repeated evaluation do not consume the grace.
"""
from collections.abc import Mapping

MAIN_ACTIONS=frozenset({'train','rest','outing','race','ss'})
NON_MAIN_ACTIONS=frozenset({'buy','purchase','buy_adaptation','skill_checkpoint','fixed_race'})


def _source_bound(source,run_id,turn):
    return (isinstance(source,Mapping)and isinstance(run_id,str)and bool(run_id)
        and source.get('run_session_id')==run_id
        and type(source.get('turn_index'))is int and source['turn_index']==turn)


def _run_id(state,preview):
    """Reuse the current run/turn scope, without another path/hash contract."""
    turn=getattr(state,'turn',None);context=getattr(state,'scenario_state',{})or{}
    captures=[]
    for candidate in getattr(state,'training_options',())or():
        source=(getattr(candidate,'raw_data',{})or{}).get('capture')
        if isinstance(source,Mapping)and source.get('run_session_id'):captures.append(source)
    truth=(context.get('simulation_truth')or{}).get('source')
    if isinstance(truth,Mapping)and truth.get('run_session_id'):captures.append(truth)
    if captures:
        if any(not isinstance(s.get('run_session_id'),str)for s in captures):return None
        ids={s.get('run_session_id')for s in captures}
        if len(ids)!=1:return None
        run_id=next(iter(ids))
        return run_id if all(_source_bound(s,run_id,turn)for s in captures)else None
    source=preview.get('source')if isinstance(preview,Mapping)else None
    if isinstance(source,Mapping)and _source_bound(source,source.get('run_session_id'),turn):
        return source['run_session_id']
    return None


def _current_full(preview,run_id,turn):
    if (not isinstance(preview,Mapping)or preview.get('status')!='accepted'
            or preview.get('stage')not in('entry','preview')
            or not _source_bound(preview.get('source'),run_id,turn)):
        return None
    kind=preview.get('kind')
    if kind not in('SS','SSS')and not(preview.get('stage')=='entry'and kind is None):return None
    count=preview.get('ready_count')
    return count==5 if type(count)is int and 0<=count<=5 else None


def _domain_available(state,goal):
    context=getattr(state,'scenario_state',{})or{}
    if (getattr(goal,'scenario',None)!='larc'or context.get('scenario')!='larc'
            or getattr(state,'screen',None)!='training_menu'):
        return False
    period=context.get('larc_period')
    if not isinstance(period,Mapping)or period.get('status')not in('verified','accepted'):return False
    value=period.get('value')
    if not isinstance(value,Mapping):return False
    # The normal frontend explicitly labels early junior pages pre-debut;
    # their unknown month is not an unknown domestic/foreign domain.
    if value.get('pre_debut')is True:
        return value.get('year_index')==1 and value.get('stage')=='junior'
    from core.auto_trainer.larc_calendar import is_overseas,period_ordinal
    day=period_ordinal(value)
    return day is not None and not is_overseas(day)


def _history(packet,run_id,turn,current_full):
    """Return only already-confirmed main actions, deduplicated by turn."""
    if (not isinstance(packet,Mapping)or packet.get('schema_version')!='larc.ss_deadline_history.v1'
            or packet.get('status')!='accepted'or not isinstance(run_id,str)
            or packet.get('run_session_id')!=run_id
            or type(packet.get('through_turn'))is not int or packet['through_turn']!=turn
            or not isinstance(packet.get('actions'),list)or not isinstance(packet.get('full_offers'),list)):
        return None,None
    actions={}
    for row in packet['actions']:
        if not isinstance(row,Mapping):return None,None
        kind=row.get('action_type')
        # These explicit non-main kinds cannot spend grace, even if a caller
        # retained a free transaction in an otherwise accepted projection.
        if kind in NON_MAIN_ACTIONS:continue
        action_turn=row.get('turn_index')
        if (kind not in MAIN_ACTIONS or row.get('execution_confirmed')is not True
                or type(action_turn)is not int or not 0<=action_turn<turn
                or not _source_bound(row.get('source'),run_id,action_turn)):
            return None,None
        if action_turn in actions and actions[action_turn]!=kind:return None,None
        actions[action_turn]=kind
    full_turns=set()
    for row in packet['full_offers']:
        if not isinstance(row,Mapping):return None,None
        offer_turn=row.get('turn_index')
        if (row.get('kind')not in('SS','SSS')or type(row.get('ready_count'))is not int
                or row['ready_count']!=5 or type(offer_turn)is not int or not 0<=offer_turn<=turn
                or not _source_bound(row.get('source'),run_id,offer_turn)):
            return None,None
        full_turns.add(offer_turn)
    latest_ss=max((t for t,kind in actions.items()if kind=='ss'),default=-1)
    full_turns={t for t in full_turns if t>latest_ss}
    if current_full is True:full_turns.add(turn)
    first=min(full_turns)if full_turns else None
    # A logged old full offer plus a missing subsequent action receipt is not
    # evidence of an unused grace. Validate the confirmed identity prefix;
    # never manufacture actions from elapsed turns to fill its holes.
    if first is not None and any(t not in actions for t in range(first,turn)):
        return None,None
    deferred=sum(kind!='ss'and t>=first for t,kind in actions.items())if first is not None else 0
    return first,deferred


def deadline_status(state,goal,preview=None):
    """Pure current status for the versioned, user-requested SS deadline.

    ``current_full`` is True/False/None. Bad/missing history cannot grant a
    fresh grace: an exact current full offer forces SS but is not labelled
    historically overdue. A historical deadline with an unknown current
    preview requests recollection through due=True, force_current_ss=False.
    """
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    enabled = unified_larc_enabled(goal)
    result={'enabled':bool(enabled),'due':False,'force_current_ss':False,'current_full':None,
        'history_status':'disabled'if not enabled else'unknown','first_ready_turn':None,
        'deferred_main_actions':None,'max_deferred_main_actions':1,'reason':'ss_deadline_disabled'}
    if not enabled:return result
    from core.auto_trainer.larc_ss_phase import phase_of, PRE_FIRST_ARC_DOMESTIC, POST_FIRST_ARC_DOMESTIC
    result['phase']=phase_of(state)
    turn=getattr(state,'turn',None);context=getattr(state,'scenario_state',{})or{}
    if type(turn)is not int or turn<0:
        result['reason']='current_turn_unavailable';return result
    if preview is None:
        preview=context.get('larc_ss_preview')
        if not isinstance(preview,Mapping):preview=context.get('larc_ss_entry')
    run_id=_run_id(state,preview)
    current_full=_current_full(preview,run_id,turn);result['current_full']=current_full
    result.update(history_status='not_used_by_phase_policy',max_deferred_main_actions=None)
    if result['phase']==PRE_FIRST_ARC_DOMESTIC:
        from core.auto_trainer.larc_ss_phase import year1_full_comparison_status
        comparison=year1_full_comparison_status(state,goal,preview)
        if comparison['enabled']:
            result['year1_full_comparison']=comparison
        if comparison['collect_pending']:
            result.update(collect_pending=True,reason='first_year_full_preview_collection_pending')
            return result
        if comparison['active']:
            result.update(reason='first_year_full_current_value_comparison')
            return result
        result.update(max_deferred_main_actions=0,due=current_full is True,
            force_current_ss=current_full is True,
            reason=('pre_first_arc_full_group_priority'if current_full is True else
                'pre_first_arc_current_not_full'if current_full is False else'pre_first_arc_preview_unknown'))
        if comparison['enabled']:
            result['fallback_basis']='legacy_pre_arc_full_advance_and_risk_policy'
    elif result['phase']==POST_FIRST_ARC_DOMESTIC:
        result['reason']='post_first_arc_current_value_comparison'
    else:result['reason']='ss_deadline_domain_unavailable'
    return result
