"""Career review bundles with an optional saved abnormal-stop game frame.

Export never captures a device, changes a run, or reconstructs missing facts.
Only selected gameplay fields are copied; credentials and local source paths
are not part of the exchange format.
"""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import uuid
from zipfile import ZIP_DEFLATED, ZipFile

SCHEMA = 'uma.career_review.v1'
TABS = ('speed','stamina','power','guts','wisdom')
STATS = (*TABS,'skill_pt')
DROP = {'source','sources','path','absolute_path','output','image','image_path',
        'screenshot_path','source_path','report_path','sha256','source_sha256',
        'source_frame_sha256','bbox','portrait_source','portrait_sources','raw_data',
        'source_manifest','execution_root','traceback','stack_trace'}
ORDINARY_MARKER = 'session/pacer_center_run.json'


def _clean(value, depth=0):
    if depth > 9:return None
    if value is None or type(value) in (bool,int):return value
    if isinstance(value,float):return round(value,5) if math.isfinite(value) else None
    if isinstance(value,str):
        if value.startswith('data:image/'):return None
        text=re.sub(r'(?:[A-Za-z]:[\\/]|\\\\)[^\s\"\']+', '<local-path>', value)
        text=re.sub(r'https?://\S+', '<url>', text)
        text=re.sub(r'(?i)((?:token|password|secret|authorization|api[_ -]?key)\s*[=:]\s*)[^\s,;]+',r'\1<redacted>',text)
        text=re.sub(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|\bsk-[A-Za-z0-9_-]{24,})','<credential-omitted>',text)
        return text[:4000]
    if isinstance(value,list):return [_clean(item,depth+1) for item in value[:250]]
    if isinstance(value,dict):
        return {str(key):_clean(item,depth+1) for key,item in value.items()
                if key not in DROP and not any(word in str(key).lower()
                    for word in ('token','password','cookie','secret','base64','api_key','apikey','authorization','credential'))}
    return None


def _pick(value, *fields):
    value=value if isinstance(value,dict) else {}
    return {field:_clean(value[field]) for field in fields if field in value}


def _read(path, issues=None):
    try:
        value=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):
        if issues is not None:issues.append({'file':Path(path).name,'reason':'record_unavailable'})
        return {}


def _owned_file(path, run):
    try:
        return Path(path).resolve().is_relative_to(Path(run).resolve())
    except (OSError, ValueError):
        return False


def _read_run(path, run, issues=None, *, check_run=True):
    if not _owned_file(path, run):
        if issues is not None:issues.append({'file':Path(path).name,'reason':'record_outside_run'})
        return {}
    value=_read(path,issues)
    if check_run and any(value.get(key) not in (None,Path(run).name) for key in ('run_id','run_session_id')):
        if issues is not None:issues.append({'file':Path(path).name,'reason':'record_run_mismatch'})
        return {}
    return value


def _ordinary_metadata(run, progress):
    metadata=_read_run(run/ORDINARY_MARKER,run)
    endpoint=metadata.get('endpoint')
    if (not re.fullmatch(r'larc_panel_[A-Za-z0-9_-]+',run.name)
            or metadata.get('schema_version')!='uma.pacer_center_run.v1'
            or metadata.get('source')!='private_pacer_center' or metadata.get('mode')!='career'
            or metadata.get('run_id')!=run.name or progress.get('run_session_id')!=run.name
            or not isinstance(endpoint,str) or not re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}',endpoint)
            or not 1<=int(endpoint.rsplit(':',1)[1])<=65535 or progress.get('device')!=endpoint
            or not isinstance(metadata.get('setup'),dict)):
        return None
    if 'cut_origin' in metadata:
        origin=metadata['cut_origin']
        # Restrict which files the shared operational validator may consult.
        # The validator remains the authority for found/current-attempt state.
        if (run.parent.name.casefold()!='attempts' or run.parent.parent.parent.name.casefold()!='runs'
                or not isinstance(origin,dict) or not isinstance(origin.get('batch_root'),str)
                or not _owned_file(run/'session/larc_terminal_settlement.json',run)):
            return None
        try:
            if Path(origin['batch_root']).resolve()!=run.parent.parent:return None
            from core.career_panel_presets import record_for_path
            record_for_path(run,root=run.parents[3],endpoint=endpoint,include_settings=False,review_only=True)
        except (ImportError,OSError,ValueError,TypeError,KeyError):
            return None
    elif run.parent.name.casefold()!='runs':
        return None
    return metadata


def _cut_origin_view(run, metadata):
    return {'batch_id':run.parent.parent.name,
            **_pick(metadata.get('cut_origin'),'attempt_id','status_at_handoff'),
            **_pick(metadata,'handed_off_at')}


def _review_source(run, progress):
    if run.parent.name.casefold()=='attempts':
        batch_root=run.parent.parent
        batch=_read_run(batch_root/'cut_farming.json',batch_root,check_run=False)
        attempts=batch.get('attempts') or []
        if not isinstance(attempts,list):attempts=[]
        attempt=next((a for a in attempts if isinstance(a,dict) and a.get('run_id')==run.name),None)
        if attempt is not None:
            metadata=_ordinary_metadata(run,progress)
            if metadata is not None and 'cut_origin' in metadata:
                return {'mode':'career','config':metadata['setup'],'attempt':attempt,
                    'metadata':metadata,'cut_origin':_cut_origin_view(run,metadata)}
            return {'mode':'cut','config':batch.get('config') or {},'attempt':attempt}
    if (run/ORDINARY_MARKER).exists():
        metadata=_ordinary_metadata(run,progress)
        if metadata is None:raise ValueError('普通育成记录归属不一致。')
        return {'mode':'career','config':metadata['setup'],'attempt':{},'metadata':metadata}
    # Explicit offline callers may inspect old unregistered records. They are
    # not added to the panel list or silently associated with a cut batch.
    return {'mode':'unknown','config':{},'attempt':{}}


def _diagnostic_owned(run):
    folder=run/'session/stop_diagnostic'
    return all(_owned_file(folder/name,run) for name in ('manifest.json','stop.jpg'))


def review_feedback_image(run):
    if not _diagnostic_owned(run):return None
    from core.auto_trainer.cut_stop_diagnostic import feedback_image
    return feedback_image(run)


def _review_diagnostic(run):
    if not _diagnostic_owned(run):return None,None
    from core.auto_trainer.cut_stop_diagnostic import read_stop_diagnostic
    return read_stop_diagnostic(run)


def _stats(value):
    value=value if isinstance(value,dict) else {}
    return {key:_clean(value.get(key,value.get('intelligence') if key=='wisdom' else None)) for key in STATS}


def _state(value):
    value=value if isinstance(value,dict) else {}
    scenario=value.get('scenario_state') or {}
    period=value.get('period') or (scenario.get('larc_period') or {}).get('value')
    return {**_pick(value,'turn','turn_index','remaining_turns','target_text','vitality'),
            'stats':_stats(value.get('current_stats',value.get('stats'))),
            'period':_clean(period),'mood':scenario.get('mood',value.get('mood')),
            'larc_remaining_turns':_clean(scenario.get('larc_remaining_turns')),
            'growth':_pick(scenario.get('larc_growth_observation'),
                'status','supporter_points','expectation','expectation_percent','adaptation_points')}


def _participant(value):
    return {
        **_pick(value,'slot','win_guaranteed','win_rate_band','win_guarantee_basis','member_match_reason'),
        'member':_pick(value.get('member_ref'),'status','member_id','identity_scope'),
        'identity':_pick(value.get('identity'),'status','matched_card_id','character_id','character_name','reason'),
        'role':_pick(value.get('role'),'status','kind','icon_type','confidence','reason'),
        'bond':{**_pick(value.get('bond'),'status','reason','fill_ratio_estimate','color','is_max'),
                'approximate':True},
        'charge':_pick(value.get('charge'),'status','reason','capacity','filled_cells','preview_cells','is_full'),
        'hint':_pick(value.get('training_hint'),'status','present','reason'),
        'reward':_pick(value.get('ss_reward') or value.get('current_reward'),
            'status','kind','amount','earned','reason'),
    }


def _diagnostic_pick(value, *fields):
    """Keep short scalar explanations, never nested solver/input objects."""
    value=value if isinstance(value,dict) else {}
    result={}
    for field in fields:
        item=value.get(field)
        if field not in value or item is not None and type(item) not in (bool,int,float,str):continue
        item=_clean(item)
        result[field]=item[:512] if isinstance(item,str) else item
    return result


def _diagnostic_stats(value):
    value=value if isinstance(value,dict) else {}
    return {key:_clean(value[key]) for key in STATS if key in value
            and (value[key] is None or type(value[key]) in (int,float))}


def _diagnostic_route(value):
    result=[]
    for item in value[:16] if isinstance(value,list) else []:
        if not isinstance(item,str):continue
        item=_clean(item)
        if isinstance(item,str):result.append(item[:80])
    return result


def _stateful_diagnostic(value):
    """Finite saved-model evidence, separate from actual game receipts.

    The solver has at most a two-step dynamic prefix. Three branch summaries
    and two before/after steps retain that explanation without exporting the
    full sampled layouts, recursive input packet, paths or source images.
    """
    if not isinstance(value,dict):return None
    result=_diagnostic_pick(value,'status','reason','basis','prefix_steps','tail_steps',
        'tail_basis','future_value_before','future_value_after','evaluated_states',
        'layout_sample_cap','historical_layout_count','sampled_layout_count',
        'sample_status','stat_cap_status','current_transition_basis','sample_basis',
        'excluded_current_layouts','remaining_steps','max_stateful_steps',
        'before_basis_state_only','state_budget_scope','before_evaluated_states','after_evaluated_states')
    counts=value.get('historical_layout_counts')
    if isinstance(counts,list):
        result['historical_layout_counts']=[[item for item in row[:2]
            if type(item) is int and item>=0] for row in counts[:3] if isinstance(row,list)]
    if isinstance(value.get('after_current_action_stats'),dict):
        result['after_current_action_stats']=_diagnostic_stats(value['after_current_action_stats'])
    for key in ('future_route_before','future_route_after'):
        if isinstance(value.get(key),list):result[key]=_diagnostic_route(value[key])
    branches=value.get('branches')
    if isinstance(branches,list):
        result['branch_count']=len(branches)
        result['branches']=[]
        for branch in branches[:3]:
            if not isinstance(branch,dict):continue
            summary=_diagnostic_pick(branch,'name','weight','future_value_before','future_value_after',
                'before_cycle_model_selected','after_cycle_model_selected',
                'before_proxy_value','before_cycle_value','after_proxy_value','after_cycle_value',
                'before_model_selection_basis','after_model_selection_basis',
                'before_original_charge_proxy_retained','after_original_charge_proxy_retained',
                'current_identified_charge_repriced','regeneration_in_representative_path','after_stateful_fallback')
            for key in ('future_route_before','future_route_after'):
                if isinstance(branch.get(key),list):summary[key]=_diagnostic_route(branch[key])
            for key in ('stat_trace_before','stat_trace_after'):
                trace=branch.get(key)
                if not isinstance(trace,list):continue
                summary[key]=[]
                for step in trace[:2]:
                    if not isinstance(step,dict):continue
                    record=_diagnostic_pick(step,'action','energy_after_action',
                        'scheduled_recovery_after','energy_after','immediate_value',
                        'held_skill_credit_status')
                    if isinstance(step.get('stats_after'),dict):
                        record['stats_after']=_diagnostic_stats(step['stats_after'])
                    summary[key].append(record)
                if len(trace)>2:summary[key+'_truncated']=True
            result['branches'].append(summary)
        if len(branches)>3:result['branches_truncated']=True
    return result


def _close_training_diagnostic(value):
    if not isinstance(value,dict):return None
    return _diagnostic_pick(value,'active','kind','steps','dedicated_recoveries','failure_rate',
        'first_recovery_index','productive_actions_before_recovery',
        'after_vitality_percent','usable_heal_percent','target_repair',
        'estimated_training_cost_percent','base_score','score_band_fraction','basis',
        'selected','previous_best','adjustment','training_group_score_ceiling',
        'game_gain_changed','energy_gain_recounted')


def _wisdom_reserve_diagnostic(value):
    if not isinstance(value,dict):return None
    result=_diagnostic_pick(value,'active','status','known_gap','reference_gain','history_samples',
        'remaining_training_budget','needed_trainings','pressure','current_quality',
        'score','basis','reference_basis','needed_trainings_ceil','remaining_free_actions',
        'reference_can_fit_shared_budget','attribute_importance','effective_wisdom_gain',
        'numeric_preference_cap','alternative_ss_wisdom_repair','alternative_ss_status',
        'incremental_wisdom_repair','real_gains_changed','fixed_training_quota')
    turns=value.get('sample_turns')
    if isinstance(turns,list):
        result['sample_turns']=[turn for turn in turns[:12] if type(turn) is int and turn>0]
    return result


def _recovery_policy_diagnostic(value):
    if not isinstance(value,dict):return None
    result=_diagnostic_pick(value,'active','mei_currently_available','preference',
        'reason','basis','no_local_action_reason','dedicated_recovery_cap')
    actions=value.get('excluded_recovery_actions')
    if isinstance(actions,list):
        result['excluded_recovery_actions']=[action for action in actions[:3]
            if action in {'rest.normal','outing.normal'}]
    return result


def _purchase_coupling_diagnostic(value):
    if not isinstance(value,dict):return None
    result=_diagnostic_pick(value,'enabled','active','allowed','reason',
        'training_action_forced','purchase_deferral_only','future_route_tail_used_as_current_score',
        'cash_or_receipts_modified','facility_phase','facility_training','candidate_after_action_id',
        'projected_current_best_action_id','projected_current_best_score','facility_projected_current_score',
        'missing_action_id','score_basis','normal_post_purchase_observation_still_selects_actual_action')
    basket=value.get('global_basket')
    if isinstance(basket,list):result['global_basket']=[v[:80]for v in basket[:4]if isinstance(v,str)]
    compared=value.get('current_comparison')
    if isinstance(compared,list):result['current_comparison']=[
        _diagnostic_pick(row,'action_id','score','priority_rank')for row in compared[:8]if isinstance(row,dict)]
    return result


def _focused_procurement_diagnostic(value):
    if not isinstance(value,dict) or value.get('schema_version')!='larc_procurement_focus.v1':return None
    result=_diagnostic_pick(value,'schema_version','revision','through_ordinal','cash_current',
        'current_payment_only','future_income_can_pay_current_purchase','candidate_count','forecast_only','baseline_score')
    levels=value.get('levels_current')
    if isinstance(levels,list) and len(levels)==10 and all(type(x)is int and 0<=x<=3 for x in levels):
        result['levels_current']=list(levels)
    result['income_assumptions']=_clean(value.get('income_assumptions'))
    timing_fields=('scenario_weight','income_branch','current_training_success','already_owned','purchase_ordinal',
        'met_reference_43','unacquired','missed_free_actions_after_43','continuation_feasible','halted_at','forecast_only')
    result['friendship_timing_before']=[_diagnostic_pick(r,*timing_fields)
        for r in value.get('friendship_timing_before',[])[:12] if isinstance(r,dict)]
    funding_fields=('deadline_ordinal','required_remaining','cash_current','income_estimate_before_deadline',
        'shortfall_estimate','forecast_only')
    rows=[]
    for row in value.get('considered',[])[:32]:
        if not isinstance(row,dict):continue
        item=_diagnostic_pick(row,'phase','after_action_id','training','considered','selected','rejected',
            'rejection_reason','value_improved','score','current_score_after','future_utility_difference',
            'funding_risk_difference','imminent_preparation_required')
        item['purchase_phases']=[p[:80]for p in row.get('purchase_phases',[])[:4]if isinstance(p,str)]
        item['current_payment']=_clean(row.get('current_payment'))
        targets=row.get('level_targets_after')
        if isinstance(targets,list)and len(targets)==10 and all(type(x)is int and 0<=x<=3 for x in targets):
            item['level_targets_after']=list(targets)
            item['level_targets_after_basis']='plan_only_not_consumption_receipt'
        item['conditional_current_action_income']=_clean(row.get('conditional_current_action_income'))
        item['budget']=_diagnostic_pick(row.get('budget'),'deadline_total','affordable_at_deadline',
            'protected_goal_blocked','unfunded_estimate','penalty')
        comparison=row.get('permanent_goal_comparison')or{}
        item['permanent_goal_comparison']={**_diagnostic_pick(comparison,'mode'),
            'before':_diagnostic_pick(comparison.get('before'),*funding_fields),
            'after':_diagnostic_pick(comparison.get('after'),*funding_fields)}
        item['friendship_timing_after']=[_diagnostic_pick(r,*timing_fields)
            for r in row.get('friendship_timing_after',[])[:12]if isinstance(r,dict)]
        item['training_windows']=_clean(row.get('training_windows'))
        coupling=_purchase_coupling_diagnostic(row.get('current_training_purchase_coupling'))
        if coupling is not None:item['current_training_purchase_coupling']=coupling
        rows.append(item)
    result['considered']=rows;result['exported_candidate_count']=len(rows)
    result['omitted_candidate_count']=max(0,int(value.get('candidate_count',len(rows)))-len(rows))
    return result


def _ss_cycle_diagnostic(value):
    if not isinstance(value,dict):return None
    result=_diagnostic_pick(value,'status','schema_version','mode','reason','identified_members',
        'future_SS_numeric_policy_estimate','future_SS_win_probability','future_SS_recovery_points',
        'future_SS_max_increase','max_generated_groups_in_route','third_or_later_cycles_not_modelled',
        'conditional_on_sampled_layout_and_training_success','ordinary_observed_sample_not_known_next_offer',
        'confirmed_future_full_members','conditional_scenario_credit','unknown_no_regeneration_weight',
        'replaces_future_charge_scalar','replaces_battery_release_credit','replaces_battery_release_fraction',
        'representative_path_not_committed_plan','no_extra_main_action_budget',
        'known_current_inventory_is_lower_bound','empirical_lag_does_not_replace_identified_cells',
        'unknown_scenario_preserves_identified_member_floor','future_SS_valuation_discount',
        'configured_release_fraction','release_overlap_basis')
    if isinstance(value.get('held_members'),list):
        result['held_members']=_clean(value['held_members'][:5])
    result['observed_training_lags']=[_diagnostic_pick(r,'training_actions','relative_empirical_weight')
        for r in value.get('observed_training_lags',[])[:12]if isinstance(r,dict)]
    result['scenario_branches']=[{**_diagnostic_pick(r,'name','weight','future_value_before','future_value_after',
        'before_cycle_model_selected','after_cycle_model_selected',
        'before_proxy_value','before_cycle_value','after_proxy_value','after_cycle_value',
        'before_model_selection_basis','after_model_selection_basis',
        'before_original_charge_proxy_retained','after_original_charge_proxy_retained',
        'current_identified_charge_repriced','regeneration_in_representative_path'),
        'future_route_before':_clean(r.get('future_route_before',[])[:12]),
        'future_route_after':_clean(r.get('future_route_after',[])[:12])}
        for r in value.get('scenario_branches',[])[:12]if isinstance(r,dict)]
    return result


def _departure18_diagnostic(value):
    """Planning values stay in diagnostics, never in observed five-stat fields."""
    if not isinstance(value,dict):return None
    return _clean(value)


def _strategy_identity(goal):
    from core.auto_trainer.larc_strategy_versions import (
        strategy_revision, policy_version_for_revision, preset_for_revision,
        behavior_base_revision,
    )
    from types import SimpleNamespace
    try:
        frozen=SimpleNamespace(scenario=goal.get('scenario'),larc_policy=goal.get('larc_policy',{}))
        revision=strategy_revision(frozen)
        if revision is None:return None
        return {'revision':revision,'strategy_preset':preset_for_revision(revision),
                'policy_version':policy_version_for_revision(revision),
                'behavior_base_revision':behavior_base_revision(frozen)}
    except (ValueError,TypeError):
        return None


def _joint192_low_diagnostic(value):
    """Only quote counters, elapsed time and outcome; never solver rows/state."""
    if not isinstance(value,dict):return None
    result=_diagnostic_pick(value,'applied','whole_quote_fallback','forecast_not_execution_or_payment')
    for field in ('applied','whole_quote_fallback','forecast_not_execution_or_payment'):
        result.setdefault(field,None)
    budget=value.get('budget')
    budget=budget if isinstance(budget,dict) else {}
    result['budget']={field:_clean(budget[field])
        for field in ('states','edges','tail_calls','seconds')
        if type(budget.get(field)) in (int,float)}
    limits=budget.get('limits')
    if (isinstance(limits,(list,tuple)) and len(limits)==3
            and all(type(item)is int or type(item)is float and math.isfinite(item) for item in limits)):
        result['budget']['limits']=[_clean(item) for item in limits]
    rows=value.get('rows')
    result['row_count']=len(rows) if isinstance(rows,list) else None
    return result


def _career_joint_diagnostic(value):
    """Bounded quote explanation, not a full future route or payment record."""
    if not isinstance(value,dict):return None
    result=_diagnostic_pick(value,'applied','whole_quote_fallback','future_fixed_reference_approximation','execution_authorized')
    result['is_execution_or_payment_receipt']=False
    if isinstance(value.get('power_deferral'),dict):
        result['power_deferral']=_diagnostic_pick(value['power_deferral'],'slot','cost','due_ordinal',
            'deferred_through_niel','forecast_only')
    reference=value.get('continuation_reference')
    if isinstance(reference,dict):
        result['continuation_reference']=_diagnostic_pick(reference,'action_id','failure_rate','basis',
            'forecast_only','candidate_income_or_solvency_proof')
        result['continuation_reference']['income']=_diagnostic_pick(reference.get('income'),
            'amount','basis','observed','source_kind','not_current_cash','conditional_amount')
    reserve=value.get('first_arc_reserve')
    if isinstance(reserve,dict):
        result['first_arc_reserve']=_clean(reserve)
    budget=value.get('budget')
    if isinstance(budget,dict):
        result['budget']={k:_clean(budget[k])for k in ('states','edges','seconds')
            if type(budget.get(k))in(int,float)and math.isfinite(budget[k])}
        limits=budget.get('limits')
        if (isinstance(limits,(tuple,list))and len(limits)==3
                and all(type(n)in(int,float)and math.isfinite(n)for n in limits)):
            result['budget']['limits']=list(limits)
    def current(row):
        if not isinstance(row,dict):return None
        out=_diagnostic_pick(row,'offer_id','action_id','phase','cost','score','direct_score','future_value',
                             'current_risk','current_energy_and_risk_retained_once',
                             'future_reference_value','current_preparation_gap','reference_preparation_gap',
                             'additional_preparation_penalty')
        phases=row.get('phases')
        if isinstance(phases,list):out['phases']=[_clean(n)for n in phases[:4]if isinstance(n,str)]
        out['current_income']=_diagnostic_pick(row.get('current_income'),'amount','basis','observed',
                                             'source_kind','not_current_cash','conditional_amount')
        out['current_gate']=_diagnostic_pick(row.get('current_gate'),'allowed','required_before_next_action',
                                           'known_current_income_credit','future_income_funds_current_purchase')
        return out
    selected=current(value.get('selected'))
    if selected is not None:result['selected']=selected
    ranking=value.get('ranking')
    if isinstance(ranking,list):
        result['ranking_count']=len(ranking)
        result['shown_candidates']=[current(row)for row in ranking[:6]if isinstance(row,dict)]
        result['shown_candidates_limit']=6
    intent=value.get('main_intent')
    if isinstance(intent,dict):result['main_intent']=_diagnostic_pick(intent,'status','action_id','phase','cost',
        'score','calendar','selection_reason','execution_authorized','payment_claimed')
    return result


def _segment_summary(value):
    result=_pick(value,'policy_version','policy_branch','status','stop_reason','error','elapsed_seconds',
        'actions_completed_this_run','consumed_turns_this_run','fixed_races_consumed_this_run',
        'race_reports','adaptation_reports','control_timing','route_reports')
    if value.get('policy_version') not in {'larc_unified_v1_7_preview','larc_unified_v1_7_1_preview','larc_unified_v1_7_2_preview','larc_unified_v1_7_3_preview','larc_unified_v1_7_4_preview','larc_unified_v1_7_4_1_preview','larc_unified_v1_7_5_preview','larc_unified_v1_7_15_preview','larc_unified_v1_7_16_1_preview','larc_unified_v1_7_26_3_preview','larc_unified_v1_8_preview','larc_unified_v1_8_1_preview','larc_unified_v1_8_2_preview','larc_unified_v1_8_3_preview','larc_unified_v1_8_4_preview','larc_unified_v1_8_5_preview','larc_unified_v1_8_5_1_preview','larc_unified_v1_9_preview','larc_unified_v1_9_2_preview','larc_unified_v1_9_2_1_preview','larc_unified_v1_9_3_preview','larc_unified_v1_9_4_preview','larc_unified_v1_9_5_preview','larc_unified_v1_9_6_preview','larc_unified_v1_9_7_preview','larc_unified_v1_9_8_preview','larc_unified_v1_9_9_preview','larc_unified_v1_9_10_preview', 'larc_unified_v1_9_11_preview', 'larc_unified_v1_9_12_preview', 'larc_unified_v2_0_preview', 'larc_unified_v2_1_preview', 'larc_unified_v2_2_preview', 'larc_unified_v2_3_preview', 'larc_unified_v2_4_preview'}:
        return result
    if isinstance(value.get('strategy181'),dict):result['strategy181']=_clean(value['strategy181'])
    if isinstance(value.get('strategy182'),dict):result['strategy182']=_clean(value['strategy182'])
    departure=_departure18_diagnostic(value.get('departure18'))
    if departure is not None:result['departure18']=departure
    rows=value.get('growth_adaptations')
    if not isinstance(rows,list) or not rows:return result
    diagnostics=[]
    for row in rows[-80:]:
        if not isinstance(row,dict):continue
        rec=row.get('recommendation') or {}
        rec=rec if isinstance(rec,dict) else {}
        item=_diagnostic_pick(row,'turn_index','ok','reason','upgraded','visited','requires_recollection')
        if isinstance(rec.get('tail263'),dict):item['tail263']=_clean(rec['tail263'])
        if isinstance(rec.get('strategy182'),dict):item['strategy182']=_clean(rec['strategy182'])
        departure=_departure18_diagnostic(rec.get('departure18'))
        if departure is not None:item['departure18']=departure
        if value.get('policy_version') == 'larc_unified_v1_9_2_1_preview':
            joint=_joint192_low_diagnostic(rec.get('joint192_low'))
            if joint is not None:item['joint192_low']=joint
        if value.get('policy_version') in {'larc_unified_v1_9_3_preview','larc_unified_v1_9_4_preview','larc_unified_v1_9_5_preview','larc_unified_v1_9_6_preview','larc_unified_v1_9_7_preview','larc_unified_v1_9_8_preview','larc_unified_v1_9_9_preview','larc_unified_v1_9_10_preview', 'larc_unified_v1_9_11_preview', 'larc_unified_v1_9_12_preview', 'larc_unified_v2_0_preview', 'larc_unified_v2_1_preview', 'larc_unified_v2_2_preview', 'larc_unified_v2_3_preview', 'larc_unified_v2_4_preview'}:
            joint=_career_joint_diagnostic(rec.get('career_joint'))
            if joint is not None:item['career_joint']=joint
        item['selection']=_diagnostic_pick(rec,'phase','cost','points_after_purchase',
            'reason','language_debuff_deferred','policy_kind')
        if value.get('policy_version') in {'larc_unified_v1_7_16_1_preview','larc_unified_v1_8_preview','larc_unified_v1_8_1_preview','larc_unified_v1_8_2_preview','larc_unified_v1_8_3_preview','larc_unified_v1_8_4_preview','larc_unified_v1_8_5_preview','larc_unified_v1_8_5_1_preview','larc_unified_v1_9_preview','larc_unified_v1_9_2_preview','larc_unified_v1_9_2_1_preview','larc_unified_v1_9_3_preview','larc_unified_v1_9_4_preview','larc_unified_v1_9_5_preview','larc_unified_v1_9_6_preview','larc_unified_v1_9_7_preview','larc_unified_v1_9_8_preview','larc_unified_v1_9_9_preview','larc_unified_v1_9_10_preview', 'larc_unified_v1_9_11_preview', 'larc_unified_v1_9_12_preview', 'larc_unified_v2_0_preview', 'larc_unified_v2_1_preview', 'larc_unified_v2_2_preview', 'larc_unified_v2_3_preview', 'larc_unified_v2_4_preview'}:
            coupling=_purchase_coupling_diagnostic(rec.get('current_training_purchase_coupling'))
            if coupling is not None:item['current_training_purchase_coupling']=coupling
        if value.get('policy_version') in {'larc_unified_v1_7_1_preview','larc_unified_v1_7_2_preview','larc_unified_v1_7_3_preview','larc_unified_v1_7_4_preview','larc_unified_v1_7_4_1_preview','larc_unified_v1_7_5_preview','larc_unified_v1_7_15_preview','larc_unified_v1_7_16_1_preview','larc_unified_v1_7_26_3_preview','larc_unified_v1_8_preview','larc_unified_v1_8_1_preview','larc_unified_v1_8_2_preview','larc_unified_v1_8_3_preview','larc_unified_v1_8_4_preview','larc_unified_v1_8_5_preview','larc_unified_v1_8_5_1_preview','larc_unified_v1_9_preview','larc_unified_v1_9_2_preview','larc_unified_v1_9_2_1_preview','larc_unified_v1_9_3_preview','larc_unified_v1_9_4_preview','larc_unified_v1_9_5_preview','larc_unified_v1_9_6_preview','larc_unified_v1_9_7_preview','larc_unified_v1_9_8_preview','larc_unified_v1_9_9_preview','larc_unified_v1_9_10_preview', 'larc_unified_v1_9_11_preview', 'larc_unified_v1_9_12_preview', 'larc_unified_v2_0_preview', 'larc_unified_v2_1_preview', 'larc_unified_v2_2_preview', 'larc_unified_v2_3_preview', 'larc_unified_v2_4_preview'}:
            focused=_focused_procurement_diagnostic(rec.get('procurement_trace'))
            if focused is not None:item['procurement_trace']=focused
        phases=rec.get('purchase_phases')
        if isinstance(phases,list):item['selection']['purchase_phases']=[
            phase[:80] for phase in phases[:4] if isinstance(phase,str)]
        grid=row.get('grid') or {}
        if isinstance(grid,dict):
            item['observed_grid']=_diagnostic_pick(grid,'points','pending_cost')
            levels=grid.get('levels')
            if value.get('policy_version') in {'larc_unified_v1_7_1_preview','larc_unified_v1_7_2_preview','larc_unified_v1_7_3_preview','larc_unified_v1_7_4_preview','larc_unified_v1_7_4_1_preview','larc_unified_v1_7_5_preview','larc_unified_v1_7_15_preview','larc_unified_v1_7_16_1_preview','larc_unified_v1_7_26_3_preview','larc_unified_v1_8_preview','larc_unified_v1_8_1_preview','larc_unified_v1_8_2_preview','larc_unified_v1_8_3_preview','larc_unified_v1_8_4_preview','larc_unified_v1_8_5_preview','larc_unified_v1_8_5_1_preview','larc_unified_v1_9_preview','larc_unified_v1_9_2_preview','larc_unified_v1_9_2_1_preview','larc_unified_v1_9_3_preview','larc_unified_v1_9_4_preview','larc_unified_v1_9_5_preview','larc_unified_v1_9_6_preview','larc_unified_v1_9_7_preview','larc_unified_v1_9_8_preview','larc_unified_v1_9_9_preview','larc_unified_v1_9_10_preview', 'larc_unified_v1_9_11_preview', 'larc_unified_v1_9_12_preview', 'larc_unified_v2_0_preview', 'larc_unified_v2_1_preview', 'larc_unified_v2_2_preview', 'larc_unified_v2_3_preview', 'larc_unified_v2_4_preview'} and isinstance(levels,list) and len(levels)==10:
                if all(type(x)is int and 0<=x<=3 for x in levels):item['observed_grid']['levels']=list(levels)
        plan=rec.get('purchase_plan') or {}
        if isinstance(plan,dict):
            funding=plan.get('stage_funding') or {}
            item['stage_funding']=_diagnostic_pick(funding,'deadline_ordinal','required_remaining',
                'cash_current','income_estimate_before_deadline','shortfall_estimate','forecast_only')
            goals=plan.get('goals') or {}
            friendship=goals.get('friendship') if isinstance(goals,dict) else None
            item['friendship_goal']=_diagnostic_pick(friendship,'target_level','status',
                'preferred_ordinal','deadline_ordinal','selection_priority')
        diagnostics.append(item)
    result['purchase_decisions']=diagnostics
    if len(rows)>80:result['purchase_decisions_truncated']=True
    return result


def _score(value):
    result=_pick(value,'action_id','action','training_type','target','score','phase','policy_version',
        'weights','components','features','rejected','source_confidence','failure_rate',
        'failure_rate_limit','unknown_fields','risk_flags','score_basis','training_equivalent_gains','gain_basis','decision_preferences','gain_evidence','score_tuning','score_workflow')
    if value.get('score_basis') in {'year1_b197_unified','year1_b198_combo'}:
        result.update(_pick(value,'parent_score','b197_components','b197_sources'))
    if type(value.get('baseline161_score'))in(int,float)and math.isfinite(value['baseline161_score']):
        result['baseline161_score']=_clean(value['baseline161_score'])
    old_components=value.get('baseline161_components')
    if isinstance(old_components,dict):
        result['baseline161_components']=_clean({str(k)[:80]:v for k,v in list(old_components.items())[:64]
            if type(v)in(int,float)and math.isfinite(v)})
    result['participants_progress']=[_pick(p,'slot','role','member_id','effective_bond','training_hint_bond',
        'hint_bond_fill_ratio_estimate','net_charge','newly_full','current_reward')
        for p in (value.get('evidence') or {}).get('participants',[]) if isinstance(p,dict)]
    if isinstance((value.get('planning')or{}).get('tail263'),dict):
        result.setdefault('planning',{})['tail263']=_clean(value['planning']['tail263'])
    plan=value.get('planning') or {}
    result['planning']=_pick(plan,'held_ss_recovery_percent','held_ss_available','unready_ss_reward','scheduled_recovery_percent',
        'planning_capacity','capacity_bounds','pre_expedition_weight','training_vitality_points',
        'training_vitality_percent','training_vitality_basis','ready_after_base_charge',
        'mei_charge','friend_startup_value','mei_initial_contact','recovery_timing','ss_redemption','reward_preparation_delay',
        'opening_preparation_preference','healing_charge_tie_preference','preferred_heads')
    if isinstance(plan.get('year1_b197'),dict):
        result['planning']['year1_b197']=_clean(plan['year1_b197'])
    if isinstance(plan.get('tail263'),dict):result['planning']['tail263']=_clean(plan['tail263'])
    if isinstance(plan.get('strategy181'),dict):result['planning']['strategy181']=_clean(plan['strategy181'])
    if isinstance(plan.get('strategy182'),dict):result['planning']['strategy182']=_clean(plan['strategy182'])
    departure=_departure18_diagnostic(plan.get('departure18') or value.get('departure18'))
    if departure is not None:result['planning']['departure18']=departure
    if isinstance(plan.get('current_charge_overlap'),dict):
        result['planning']['current_charge_overlap']=_clean(plan['current_charge_overlap'])
    if isinstance(plan.get('boundary_readiness'),dict):
        result['planning']['boundary_readiness']=_clean(plan['boundary_readiness'])
    result['planning']['energy_horizon']=_pick(plan.get('energy_horizon'),'active','score','kind','steps','unready_ss_reward',
        'continuation_value_delta','future_value_before','future_value_after','after_vitality_percent',
        'future_route_before','future_route_after','estimated_training_cost_percent','training_cost_basis',
        'future_training_value','future_wisdom_value_credit','terminal_energy_weight','terminal_weight',
        'future_numeric_training_value','rest_scenarios','basis','opening_charge_unlock',
        'opening_charge_future_credits','opening_charge_wisdom_credits',
        'held_inventory_timing_mix','historical_source_turns','future_numeric_reference_basis',
        'future_risk_basis','already_counted_recovery_value','planning_capacity','after_energy_points')
    energy=plan.get('energy_horizon') or {}
    cycle=_ss_cycle_diagnostic(energy.get('ss_resource_cycle')) if isinstance(energy,dict) else None
    if cycle is not None:result['planning']['energy_horizon']['ss_resource_cycle']=cycle
    attribution=(energy.get('expedition_forecast')or{}).get('expectation_growth_attribution') if isinstance(energy,dict) else None
    if isinstance(attribution,dict):
        result['planning']['energy_horizon']['expectation_growth_attribution']=_clean(attribution)
    stateful=_stateful_diagnostic(energy.get('stateful_prefix')) if isinstance(energy,dict) else None
    if stateful is not None:result['planning']['energy_horizon']['stateful_prefix']=stateful
    close=_close_training_diagnostic(plan.get('close_training_balance'))
    if close is not None:result['planning']['close_training_balance']=close
    reserve=_wisdom_reserve_diagnostic(plan.get('wisdom_reserve'))
    if reserve is not None:result['planning']['wisdom_reserve']=reserve
    recovery_policy=_recovery_policy_diagnostic(plan.get('recovery_policy'))
    if recovery_policy is not None:result['planning']['recovery_policy']=recovery_policy
    if (value.get('policy_version') == 'larc_unified_v1_9_preview'
            and isinstance(plan.get('energy_sequence'), dict)):
        result['planning']['energy_sequence'] = _clean(plan['energy_sequence'])
    result['attribute_plan']=_pick(value.get('attribute_plan'),'attributes','estimated_training_budget',
        'stage_pressure_weight','future_income_credit','utility','input_unknown')
    return result


def _ss_review(value, snapshot):
    """Keep SS's own comparison inputs, rather than an earlier capture state."""
    result=_pick(value,'selected','reason','kind','ready_count','score','components','unknown_fields',
        'policy_version','local_action','local_target','training_score','selection_basis','recovery','supporter_goal',
        'partial_group_policy','deadline_resolution','decision_preferences','score_tuning','score_workflow','ss_deadline','ss_phase')
    if (value.get('policy_version') in {'larc_unified_v1_9_7_preview','larc_unified_v1_9_8_preview','larc_unified_v1_9_9_preview','larc_unified_v1_9_10_preview', 'larc_unified_v1_9_11_preview', 'larc_unified_v1_9_12_preview', 'larc_unified_v2_0_preview', 'larc_unified_v2_1_preview', 'larc_unified_v2_2_preview', 'larc_unified_v2_3_preview', 'larc_unified_v2_4_preview'}
            and isinstance(value.get('year1_b197'),dict)):
        result['year1_b197']=_pick(value['year1_b197'],'action_id','reason','values','evidence',
            'ss','state_sources','projection_unknown','legacy_score_tuning_workflow',
            'effective_revision','score_basis','combo198')
    if (value.get('policy_version') in {'larc_unified_v1_8_5_1_preview','larc_unified_v1_9_preview','larc_unified_v1_9_2_preview','larc_unified_v1_9_2_1_preview','larc_unified_v1_9_3_preview','larc_unified_v1_9_4_preview','larc_unified_v1_9_5_preview','larc_unified_v1_9_6_preview','larc_unified_v1_9_7_preview','larc_unified_v1_9_8_preview','larc_unified_v1_9_9_preview','larc_unified_v1_9_10_preview', 'larc_unified_v1_9_11_preview', 'larc_unified_v1_9_12_preview', 'larc_unified_v2_0_preview', 'larc_unified_v2_1_preview', 'larc_unified_v2_2_preview', 'larc_unified_v2_3_preview', 'larc_unified_v2_4_preview'}
            and isinstance(value.get('matched_stock'), dict)):
        result['matched_stock'] = _clean(value['matched_stock'])
    if (value.get('policy_version') == 'larc_unified_v1_9_preview'
            and isinstance(value.get('energy_sequence'), dict)):
        result['energy_sequence'] = _clean(value['energy_sequence'])
    plan=value.get('planning') or {}
    plan=plan if isinstance(plan,dict) else {}
    result['planning']=_pick(plan,'reward_cycle','charming',
        'visible_full_group_wait','battery_release','fan_route','adaptation_resource',
        'expectation_training_progress','support_bond','phase_comparison','ss_victory','ss_phase')
    if isinstance(plan.get('boundary_readiness'),dict):
        result['planning']['boundary_readiness']=_clean(plan['boundary_readiness'])
    if isinstance(plan.get('strategy181'),dict):result['planning']['strategy181']=_clean(plan['strategy181'])
    if isinstance(plan.get('strategy182'),dict):result['planning']['strategy182']=_clean(plan['strategy182'])
    departure=_departure18_diagnostic(plan.get('departure18') or value.get('departure18'))
    if departure is not None:result['planning']['departure18']=departure
    if 'energy_horizon' in plan:
        energy=plan['energy_horizon']
        stateful=_stateful_diagnostic(energy.get('stateful_prefix')) if isinstance(energy,dict) else None
        if stateful is not None:energy={**energy,'stateful_prefix':stateful}
        if isinstance(energy,dict) and 'ss_resource_cycle' in energy:
            energy={**energy,'ss_resource_cycle':_ss_cycle_diagnostic(energy['ss_resource_cycle'])}
        result['planning']['energy_horizon']=_clean(energy)
    actual=snapshot.get('state') or {}
    result['state_before']=_state(actual) if actual else None
    result['state_basis']='ss_comparison_snapshot' if actual else 'unavailable'
    preview=snapshot.get('preview') or {}
    result['preview']={**_pick(preview,'status','kind','kind_basis','ready_count','stats_gain',
        'supporter_points_if_all_win','rewards','unknown_fields','win_policy'),
        'participants_status':(preview.get('participants_observation') or {}).get('status'),
        'current_frame':(preview.get('participants_observation') or {}).get('current_frame'),
        'win_rates':(preview.get('participants_observation') or {}).get('win_rates'),
        'win_rates_status':(preview.get('participants_observation') or {}).get('win_rates_status'),
        'participants':[_participant(p) for p in (preview.get('participants_observation') or {}).get('participants',[])
                        if isinstance(p,dict)]} if preview else None
    result['training_candidates']=[_score(s) for s in value.get('training_candidates',[]) if isinstance(s,dict)]
    return result


def _training(value):
    larc=(value.get('scenario_mechanics') or {}).get('larc') or {}
    observed=larc.get('training_participants') or {}
    from core.auto_trainer.state import TrainingCandidate
    from core.auto_trainer.larc_candidate_quality import row_diagnostics
    diagnostic=(row_diagnostics(TrainingCandidate.from_dict(value))
        if value.get('training_type') in TABS else {'failure_rate_ocr':None,'gain_rows':[]})
    return {**_pick(value.get('raw_data'),'visible_portrait_count','effective_support_count',
                    'non_support_npc_count','unresolved_portrait_count','unknown_fields'),
        **_pick(value,'training_type','stats_gain','base_gain','bonus_gain','total_gain',
                    'failure_rate','support_count','visible_portrait_count','effective_support_count',
                    'non_support_npc_count','unresolved_portrait_count','friendship','hint_count',
                    'vitality_delta','confidence','unknown_fields'),
        'participants_status':observed.get('status','unknown'),
        'participants':[_participant(p) for p in observed.get('participants',[]) if isinstance(p,dict)],
        'failure_rate_ocr':_clean(diagnostic['failure_rate_ocr']),
        'gain_row_diagnostics':_clean(diagnostic['gain_rows']),
        'numeric_evidence':_pick(diagnostic,'usable','reasons','verified_gains','unverified_used_attributes',
            'primary_gain','failure_rate_verified','basis'),
        'vitality_preview':_pick(larc.get('vitality_preview'),'status','reason','delta_percent','before_percent','after_percent'),
        'training_effects':_pick(larc.get('training_effects'),'status','certainty','vitality_delta','vitality_cost_reduction')}


def _ai_review(run):
    settings=_read_run(run/'session/ai_decision.json',run)
    if settings.get('run_id')!=run.name:return None
    rows=[];path=run/'session/ai_decisions.jsonl'
    if _owned_file(path,run) and path.is_file():
        try:
            with path.open(encoding='utf8') as stream:
                while True:
                    line=stream.readline(1024*1024+1)
                    if not line:break
                    if len(line)>1024*1024:break
                    try:record=json.loads(line)
                    except ValueError:continue
                    if not isinstance(record,dict):continue
                    request=record.get('request') or {};result=record.get('result') or {}
                    if not isinstance(request,dict) or not isinstance(result,dict):continue
                    if request.get('run_id')!=run.name:continue
                    context=request.get('context') or {}
                    if not isinstance(context,dict):continue
                    rows.append({'node':record.get('node'),
                        'turn':context.get('turn',(context.get('state') or {}).get('turn')),
                        **_pick(result,'decision_id','action_id','reason','status','trigger_reasons',
                            'cached','elapsed_seconds','provider_error_code')})
                    if len(rows)>256:rows.pop(0)
        except (OSError,ValueError,TypeError):pass
    return {'settings':_pick(settings.get('options'),'mode','timeout_seconds','close_score_gap','fallback'),
        'decisions':rows,'scope':'decision summaries only; no request bodies, provider configuration or credentials'}


def _information_review(value):
    value=value if isinstance(value,dict) else {}
    return {**_pick(value,'status','reason','returned_to_training','turn_consumed',
                    'turn_consuming_action_executed','same_training_hud','return_readiness','timings','navigation_recoveries'),
            'entry_precheck':_pick(value.get('entry_precheck'),'accepted','reason','selected_tab'),
            'return_precheck':_pick(value.get('return_precheck'),'accepted','reason','selected_tab'),
            'steps':[{**_pick(s,'input_performed','input_error','reobserved','text_source','timings'),
                      'decision':_pick(s.get('decision'),'action','reason'),
                      'safety_verdict':_pick(s.get('safety_verdict'),'allowed','reason')}
                     for s in value.get('steps',[]) if isinstance(s,dict)],
            'observations':[{**_pick(o.get('result'),'status','reason','errors'),**_pick(o,'text_source','timings')}
                            for o in value.get('observations',[]) if isinstance(o,dict)]}


def _local_reference(value, run):
    """Rebind old absolute references after moving a portable folder."""
    parts=str(value).replace('\\','/').split('/')
    if run.name not in parts:return None
    tail=parts[parts.index(run.name)+1:]
    candidate=run.joinpath(*tail).resolve()
    return candidate if candidate.is_relative_to(run) else None


def _review_status(run, attempt, *, mode='cut'):
    status=attempt.get('status','unknown')
    labels={'abandoned':'已弃养','abandoned_and_restarted':'已弃养','stopped':'已停止',
        'found':'发现切','found_kept':'有切保留','found_for_validation':'发现切（测试）',
        'no_cut_at_cutoff':'截止未出切','no_cut_after_character_event':'小切事件未出切',
        'mei_unlock_failed':'黄帽解锁失败','training_failed':'训练失败待重开','benchmark_cutoff':'测试截止',
        'supporter_deadline_missed':'期限积分不足待重开'}
    if mode!='cut':labels={'stopped':'已停止','paused':'已暂停','career_complete':'育成结束'}
    if isinstance(status,str) and status in labels:return labels[status]
    # A persisted attempt marked running is not evidence of a live worker.
    # Prefer its last recorded result, and otherwise say only unfinished.
    folders=sorted(run.glob('autonomous_*'),reverse=True)
    for folder in folders:
        path=folder/'summary.final.json'
        if not path.is_file():path=folder/'summary.json'
        if not path.is_file():continue
        summary=_read_run(path,run)
        if not summary:continue
        if summary.get('run_session_id') not in {None,run.name}:continue
        if summary.get('career_complete') is True:return '育成结束'
        last=summary.get('status')
        if last=='stopped':return '已停止' if summary.get('stop_reason')=='user_stop' else '已中断'
        if last in {'failed','error','handoff'}:return '已中断'
        if last=='paused':return '已暂停'
        if last=='batch_limit_reached':return '本轮已结束'
        break
    return '未结束' if status in {'running','unknown','ready'} else '待查看'


def _review_record(base, run, progress, config, attempt, *, mode, index=None, started=None):
    config=config if isinstance(config,dict) else {}
    name=config.get('character_name') or config.get('character_id') or '未知角色'
    status=attempt.get('status',progress.get('status','unknown'))
    if not isinstance(status,str):status='unknown'
    status_label=_review_status(run,{'status':status},mode=mode)
    timestamp=None
    if isinstance(started,str):
        try:timestamp=datetime.fromisoformat(started)
        except ValueError:pass
    if timestamp is None:
        match=re.fullmatch(r'larc_panel_(\d{8}_\d{6}|\d{12})_.+',run.name)
        if match:
            stamp=match[1]
            try:timestamp=datetime.strptime(stamp,'%Y%m%d_%H%M%S') if '_' in stamp else datetime.strptime('20'+stamp,'%Y%m%d%H%M%S')
            except ValueError:pass
    label=f"{timestamp.strftime('%m-%d %H:%M') if timestamp else '时间未知'} · {name} · {status_label}"
    if mode=='career':label='自动育成 · '+label
    return {'id':run.relative_to(base).as_posix(),'run_id':run.name,'label':label,
        'started_at':timestamp.isoformat() if timestamp else None,'attempt_index':index,
        'status_label':status_label,'character_name':config.get('character_name'),
        'actions_completed':progress.get('actions_completed'),'next_turn':progress.get('next_turn'),
        'status':status,'character_id':config.get('character_id'),'mode':mode}


def list_career_reviews(root, *, mode=None):
    if mode not in (None,'cut','career'):raise ValueError('复盘模式应为刷切或自动育成。')
    root=Path(root).resolve();base=(root/'runs').resolve();records=[]
    for batch_file in base.glob('*/cut_farming.json'):
        if batch_file.resolve()!=batch_file:continue
        batch=_read(batch_file);config=batch.get('config') or {}
        attempts=batch.get('attempts') or []
        if not isinstance(attempts,list):continue
        for index,attempt in enumerate(attempts,1):
            if not isinstance(attempt,dict):continue
            run_id=attempt.get('run_id')
            if not isinstance(run_id,str) or run_id in {'.','..'} or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}',run_id):continue
            run=batch_file.parent/'attempts'/run_id
            if run.resolve()!=run or not run.is_dir():continue
            progress=_read_run(run/'progress.json',run)
            if progress.get('run_session_id')!=run.name:continue
            metadata=_ordinary_metadata(run,progress)
            if metadata is not None and 'cut_origin' in metadata:
                record=_review_record(base,run,progress,metadata['setup'],{},mode='career',index=index,
                    started=metadata.get('started_at'))
                record['cut_origin']=_cut_origin_view(run,metadata)
                record['label']=record['label'].replace('自动育成 ·','自动育成（刷切保留转入） ·',1)
                records.append(record)
            else:
                records.append(_review_record(base,run,progress,config,attempt,mode='cut',index=index))
    # Only explicit Pacer-center records directly below runs belong here. A
    # progress.json in an experiment/another task is not enrollment evidence.
    for run in base.glob('larc_panel_*'):
        if not run.is_dir() or run.resolve()!=run:continue
        progress=_read_run(run/'progress.json',run)
        metadata=_ordinary_metadata(run,progress)
        if metadata is None:continue
        records.append(_review_record(base,run,progress,metadata['setup'],{},mode='career',
            started=metadata.get('started_at')))
    return sorted((record for record in records if mode is None or record['mode']==mode),
                  key=lambda r:r['run_id'],reverse=True)


def checked_review_run(root, identifier):
    root=Path(root).resolve();base=(root/'runs').resolve()
    if not isinstance(identifier,str) or identifier not in {r['id'] for r in list_career_reviews(root)}:
        raise ValueError('请选择复盘列表中的一局。')
    run=(base/identifier).resolve()
    if not run.is_relative_to(base):raise ValueError('复盘记录超出本包目录。')
    return run


def build_career_review(run_root, *, package_root=None):
    from core.auto_trainer.pacer_storage import read_access_for_run
    with read_access_for_run(run_root):
        return _build_career_review(run_root,package_root=package_root)


def _build_career_review(run_root, *, package_root=None):
    from core.auto_trainer.run_support_profile import evidence as support_evidence
    run=Path(run_root).resolve();issues=[]
    def read(path, issues=None, *, check_run=True):
        return _read_run(path,run,issues,check_run=check_run)
    progress=read(run/'progress.json',issues,check_run=False)
    if progress.get('run_session_id')!=run.name:raise ValueError('复盘局号与记录不一致。')
    goal=read(run/'goal.json',issues)
    source=_review_source(run,progress)
    config=source['config'];attempt=source['attempt'];mode=source['mode']
    profile_path=run/'session/support_card_profile.json'
    profile=read(profile_path,check_run=False)
    profile_status=('missing' if not profile_path.exists() else 'unreadable' if not profile else
        'run_mismatch' if profile.get('run_session_id')!=run.name else
        'recorded' if profile.get('cards') else 'empty')
    if profile_status!='recorded':profile={}
    roster=read(run/'session/larc_member_roster.json')
    summaries=[];rows={}
    for folder in sorted(run.glob('autonomous_*')):
        path=folder/'summary.final.json'
        if not path.is_file():path=folder/'summary.json'
        if not path.is_file():continue
        summary=read(path,issues)
        if not summary:continue
        summaries.append(summary)
        for row in summary.get('turns',[]):
            local=_local_reference(row.get('output',''),run)
            if local is not None:rows[local]=row
    observations=[]
    for path in sorted(run.glob('autonomous_*/turn_*/pipeline_report.json')):
        wrapper=read(path,issues);report=wrapper.get('report') or {}
        if not wrapper:continue
        if any(report.get(key) not in (None,run.name) for key in ('run_id','run_session_id')):
            issues.append({'file':path.name,'reason':'record_run_mismatch'});continue
        stages=report.get('stages') or {};live=stages.get('live_loop') or {}
        sidecar=read(path.parent/'capture/training_options.generated.json')
        decision_state=read(path.parent/'decision/turns/turn_000/state.json')
        row=rows.get(path.parent.resolve(),{})
        match=re.match(r'turn_(\d+)',path.parent.name)
        turn=row.get('turn_index',int(match[1]) if match else None)
        decision=live.get('decision') or {}
        ss=(stages.get('larc_ss') or {}).get('comparison') or {}
        ss_snapshot=read(path.parent/'capture/ss_comparison_snapshot.json')
        ss_state=ss_snapshot.get('state') or {}
        ss_source=(ss_snapshot.get('preview') or {}).get('source') or {}
        if (ss_state.get('turn')!=turn or ss_source.get('run_session_id',run.name)!=run.name):
            ss_snapshot={}
        if not ss and ss_snapshot:ss=ss_snapshot.get('decision') or {}
        scored=[s for s in (decision.get('scored_candidates') or ss.get('training_candidates') or []) if isinstance(s,dict)]
        selected_state=decision_state or (ss_snapshot.get('state') if ss.get('selected') else None) or {}
        options=selected_state.get('training_options') or sidecar.get('training_options') or []
        if isinstance(options,dict):options=list(options.values())
        previews=[_training(o) for o in options if isinstance(o,dict)]
        existing={o.get('training_type') for o in previews}
        missing=[tab for tab in TABS if tab not in existing]
        execution=row.get('execution') or live.get('execution') or {}
        selected=_pick(decision,'action','target','reason','confidence','risk_flags')
        if not selected and ss:
            selected={'action':'ss' if ss.get('selected') else ss.get('local_action'),
                      'target':ss.get('local_target'),'reason':ss.get('reason')}
        observations.append({'observation_id':path.parent.relative_to(run).as_posix(),
            'turn_index':turn,'status':report.get('status'),'pipeline_code':wrapper.get('code'),
            'policy_version':row.get('policy_version') or ss.get('policy_version') or next((s.get('policy_version') for s in scored if s.get('policy_version')),None),
            'state_before':_state(selected_state or sidecar.get('state_context') or row.get('before')),
            'training_basis':('decision_state' if decision_state.get('training_options') else
                'ss_comparison_snapshot' if selected_state.get('training_options') else 'capture_sidecar'),
            'trainings':previews,'missing_training_tabs':missing,
            'candidates':[_score(s) for s in scored if isinstance(s,dict)],
            'ss_comparison':_ss_review(ss,ss_snapshot),
            'ss_information':_information_review(stages.get('larc_ss_information')
                or (stages.get('capture') or {}).get('larc_ss_information')),
            'selected':selected,'consumed':row.get('turn_consumed'), 'kind':row.get('kind'),
            'execution':_pick(execution,'performed','tap_performed','is_valid','reason','target','confirmation_mode','required_event_choice'),
            'timing':_clean(report.get('pipeline_timing')),
            'home_preparation':_pick(row.get('home_preparation'),'provider_seconds','cut_check_seconds',
                'outing_prepare_seconds','total_seconds','scope'),
            'ss_preview_reading':{'unchanged_ui_cache_hits':sum(
                (step.get('result') or {}).get('frame_safety',{}).get('text_source')=='larc_ss_unchanged_ui_cache'
                for step in (stages.get('larc_ss') or {}).get('step_reports',[])),
                'steps':[{'reason':(step.get('result') or {}).get('reason'),
                    **_pick((step.get('result') or {}).get('reader_timing'),'connection_size_seconds',
                        'capture_seconds','text_seconds','planning_seconds','text_source')}
                    for step in (stages.get('larc_ss') or {}).get('step_reports',[])]},
            'capture':_pick(stages.get('capture'),'capture_transport','capture_backend_details','capture_timings',
                'candidate_quality_refresh','is_valid','errors','reason','stop_reason')})
    choices=[]
    for path in sorted(run.glob('autonomous_*/**/event_*/report.json')):
        report=read(path,issues)
        event_turn=next((re.match(r'turn_(\d+)',part) for part in path.relative_to(run).parts if re.match(r'turn_(\d+)',part)),None)
        for index,step in enumerate(report.get('step_reports',[])):
            result=step.get('result') or {};decision=result.get('decision') or {};evidence=decision.get('evidence') or {}
            selection=evidence.get('event_selection') or {}
            choices.append({'observation_id':path.parent.relative_to(run).as_posix()+f'/step{index+1}',
                'turn_index':int(event_turn[1]) if event_turn else None,'turn_basis':'action_turn_from_report_folder',
                'performed':result.get('performed'),'reason':result.get('reason'),
                'observed_text':_clean((evidence.get('screen_text') or {}).get('normalized_text')),
                'selection':_pick(selection,'status','reason','choice_index','expected_event','event_id','story_id',
                    'event_policy_version','effect_basis','candidates','vitality','vitality_basis','matched_rule_ids',
                    'unknown_fields','training_basis','energy_comparison','effective_preferences','preference_read','title_match','title_resolution','identity_basis','trainee_identity_basis','acupuncture_policy','acupuncture_decision',
                    'last_training_basis','last_training_action','energy_gap_reference_points','capacity_basis',
                    'recovery_priority_basis','catalog_reference','mei_unlock_policy','mei_unlock_decision','meal_preference')})
    events=read(run/'session/event_progress.json')
    mei_session=read(run/'session/larc_friend_outing.json')
    if mei_session.get('run_session_id')!=run.name:mei_session={}
    menu_files=list(run.glob('autonomous_*/menu_*.json'))
    latest_menu=read(max(menu_files,key=lambda p:p.stat().st_mtime)) if menu_files else {}
    ss_matches=[]
    for entry in roster.get('ss_execution_participants',[]):
        receipt=entry.get('receipt') or {};people=entry.get('participants_observation') or {}
        comparison=next((o['ss_comparison'] for o in observations if o['turn_index']==receipt.get('turn_index')
                         and o['kind']=='ss' and o.get('consumed') is True),{})
        ss_matches.append({'turn_index':receipt.get('turn_index'),'tap_performed':receipt.get('tap_performed'),
            'kind':comparison.get('kind'),'expected_count':people.get('expected_count'),
            'visible_portrait_count':people.get('visible_portrait_count'),
            'participants':[_participant(p) for p in people.get('participants',[])]})
    latest=summaries[-1] if summaries else {}
    closed=(latest.get('career_complete') is True or mode=='cut' and attempt.get('status') in
            {'abandoned','abandoned_and_restarted','completed','career_complete'})
    result={'schema_version':SCHEMA,'created_at_utc':datetime.now(timezone.utc).isoformat(),
        'scope':'one career; existing observations and optional saved stop diagnostic; no credentials',
        'units':{'vitality':'bar percent','vitality_delta':'game points unless preview says percent',
            'bond':'approximate observed bar ratio, not an exact bond value','charge':'filled cells and current training preview cells',
            'unknown':'null/unknown stays unknown; no retroactive recognition from deleted screenshots'},
        'run':{'run_id':run.name,'mode':mode,'character':_pick(config,'character_id','character_name','support_deck','borrow_card_id'),
            **({'cut_origin':source['cut_origin']} if source.get('cut_origin') else {}),
            'progress':_pick(progress,'next_turn','actions_completed'),
            'status':latest.get('status'),'stop_reason':_clean(latest.get('stop_reason')),'error':_clean(latest.get('error')),
            'observed_policy_versions':sorted({str(s['policy_version']) for s in [*summaries,*observations] if s.get('policy_version')}),
            'career_complete':latest.get('career_complete'), 'attempt_status':attempt.get('status'),
            'partial_snapshot':not closed},
        'goal':_pick(goal,'scenario','target_stats','risk_tolerance','training_failure_rate_limit','larc_policy','event_preferences'),
        'strategy_identity':_strategy_identity(goal),
        'supports':[_pick(card,'slot','card_id','character_id','name','title','type','rarity','level','limit_breaks','actual_effects','source','actual_effects_source')
                    for card in profile.get('cards',[])],
        'support_profile_status':profile_status,
        'support_profile_evidence':_clean(support_evidence(run,run.name)),
        'final_roster_labels':[_pick(member,'member_id','character_id','character_name','appearance_id','member_kind','matched_card_id')
                               for member in roster.get('members',[])],
        'observations':observations,'event_choices':choices,
        'ai_decision':_ai_review(run),
        'cut_checks':[{'turn_index':int(match[1]),**_pick(check,'state','check_reason')}
            for summary in summaries for check in summary.get('cut_farming_checks',[])
            if check.get('state') in {'present','absent','unknown'}
            and (match:=re.search(r'(?:^|[\\/])cut_status_(\d+)(?:[\\/]|$)',str(check.get('source',''))))] if mode=='cut' or source.get('cut_origin') else [],
        'training_failure_event':_clean(attempt.get('training_failure_event')),
        'supporter_deadline_failure':_clean(attempt.get('supporter_deadline_failure')),
        'mei_contact':_clean(read(run/'session/larc_mei_contact.json')),
        'character_cut_event':_pick(attempt,'character_cut_event_pending','character_cut_event_result'),
        'mei_outing':{**_pick(mei_session,'unlocked','completed'),
            'unlock_event':_pick(mei_session.get('unlock_event'),'event_id','option_id','selected_option_index',
                'status','gamble_armed','tap_performed','gamble_policy','gamble_decision','result_basis',
                'result_observation','menu_result_check','gamble_confounded','confounded_by')},
        'event_history':[_pick(e,'turn_index','turn_basis','phase','title','event_id','story_id','owner','option_count',
            'match_status','selected_option_text','selected_option_index','visible_option_texts','result_observed','observed_effects')
            for e in events.get('occurrences',[])],
        'ss_matches':ss_matches,
        'latest_observed_state':_state(latest_menu.get('state_context')),
        'run_segments':[_segment_summary(s) for s in summaries],
        'coverage':{'training_observations':len(observations),
            'five_tab_observations':sum(not o['missing_training_tabs'] for o in observations),
            'recorded_consumed_actions':sum(o.get('consumed') is True for o in observations),
            'event_choices':len(choices),'ss_receipts':len(ss_matches),'issues':issues}}
    diagnostic,_=_review_diagnostic(run)
    if diagnostic:result['stop_diagnostic']=_clean(diagnostic)
    from core.auto_trainer.candidate_failure_archive import review_candidate_failures
    failures,_=review_candidate_failures(run)
    if failures['records']:result['candidate_failure_evidence']=_clean(failures)
    from desktop.cut_package_profile import continuation_enabled
    if continuation_enabled(package_root):
        from core.auto_trainer.larc_career_assessment import assess_career_review
        result['continuation_assessment']=assess_career_review(result)
    return result


def export_career_review(root, identifier):
    from core.auto_trainer.pacer_storage import read_access_for_run
    run=checked_review_run(root,identifier)
    with read_access_for_run(run):
        return _export_career_review(root,identifier)


def _export_career_review(root, identifier):
    from desktop.distribution_identity import export_distribution
    root=Path(root).resolve();run=checked_review_run(root,identifier)
    package_root=root.parent if root.name.casefold()=='backend' else root
    review=build_career_review(run,package_root=package_root)
    review['export_distribution']=export_distribution(package_root)
    from desktop.cut_package_profile import continuation_enabled
    report_html=None
    if continuation_enabled(package_root):
        from core.auto_trainer.larc_career_assessment import assess_career_review
        from core.auto_trainer.larc_assessment_report import render_assessments
        assessment=review.get('continuation_assessment') or assess_career_review(review)
        report_html=render_assessments([(run.name,assessment)],export_trace_id=review['export_distribution'].get('trace_id'))
    folder=root.parent/'exports' if root.name.casefold()=='backend' else root/'runs/review_exports'
    folder.mkdir(parents=True,exist_ok=True)
    name=f"复盘_{run.name}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}.zip"
    target=folder/name
    diagnostic,stop_image=_review_diagnostic(run)
    from core.auto_trainer.candidate_failure_archive import review_candidate_failures
    failures,failure_images=review_candidate_failures(run,include_previews=True)
    if failures['records']:review['candidate_failure_evidence']=_clean(failures)
    with ZipFile(target,'x',ZIP_DEFLATED,compresslevel=6) as archive:
        archive.writestr('review.json',json.dumps(review,ensure_ascii=False,separators=(',',':')).encode('utf8'))
        if stop_image is not None:
            cached=(diagnostic or {}).get('scope')=='game_screen_before_abnormal_stop'
            archive.writestr('停机前最近游戏画面_非实时.jpg' if cached else '异常停机现场.jpg',stop_image)
        if report_html is not None:archive.writestr('续养评估.html',report_html.encode('utf8'))
        for attachment,raw in failure_images.items():archive.writestr(attachment,raw)
        diagnostic_note=(diagnostic or {}).get('note','')
        archive.writestr('说明.txt','本包包含一局文字复盘；如已保存异常停机画面，会附上异常停机现场.jpg；实时截图失败时可能附停机前最近游戏画面_非实时.jpg。具体来源、原时间和距停机秒数见review.json的stop_diagnostic。截图为压缩预览，原始PNG保留在本局session/stop_diagnostic中。未知信息保留未知；羁绊是读条估计。\n'+diagnostic_note+'\n不包含推送令牌或完整本地日志。observations保留重复识别/重采样，consumed只按原执行记录标注。\n'
            'export_distribution 仅标记本次导出所在程序的公开来源编号，不代表历史育成的实际运行者；客户端未校验凭证签名。\n'
            '如有已保存的局部训练识别异常，会另附有限压缩预览；索引见review.json的candidate_failure_evidence。它们是当时失败候选帧，不是新截图或停机画面。\n')
    extra={}
    if report_html is not None:
        assessment_path=target.with_suffix('.html');assessment_path.write_text(report_html,encoding='utf8')
        extra['assessment_path']=str(assessment_path)
    return {'path':str(target),'size_bytes':target.stat().st_size,**extra,
            'run_id':run.name,'coverage':review['coverage'],'partial_snapshot':review['run']['partial_snapshot']}
