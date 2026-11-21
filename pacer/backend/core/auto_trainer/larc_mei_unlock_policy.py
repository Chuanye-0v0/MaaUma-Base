"""Optional Mei unlock gamble; unknown outcomes never authorize restart."""
from copy import deepcopy
import json
import math
from pathlib import Path


STORY_ID = '809043004'
GAMBLE_REASON = 'mei_unlock_low_vitality_gamble'
DEFAULT_POLICY = {'mode':'safe','below_vitality_percent':50.0}


def normalize_mei_unlock_policy(value=None):
    if value is None:return dict(DEFAULT_POLICY)
    if not isinstance(value,dict) or set(value)-set(DEFAULT_POLICY):
        raise ValueError('黄帽解锁策略应包含mode与below_vitality_percent。')
    result={**DEFAULT_POLICY,**value}
    if not isinstance(result['mode'],str) or result['mode'] not in {'safe','gamble_when_low'}:
        raise ValueError('黄帽解锁策略请选择safe或gamble_when_low。')
    threshold=result['below_vitality_percent']
    if type(threshold) not in (int,float) or not math.isfinite(threshold) or not 0<=threshold<=100:
        raise ValueError('黄帽赌解锁体力阈值须为0到100之间的数字。')
    result['below_vitality_percent']=float(threshold)
    return result


def select_mei_unlock(event,expected,state,context,preferences):
    if str(event.get('story_id'))!=STORY_ID or len(event.get('options',[]))!=3:return None
    policy=normalize_mei_unlock_policy(preferences.get('mei_unlock_policy'))
    vitality=state.vitality
    known=type(vitality) in (int,float) and math.isfinite(vitality) and 0<=vitality<=100
    cut_active=context.get('cut_farming_active') is True
    cut=context.get('cut_restart_authority',cut_active) is True
    if 'mei_gamble_authority' in context:
        gamble_allowed=context.get('mei_gamble_authority') is True
    elif cut_active:
        gamble_allowed=cut
    else:
        gamble_allowed=context.get('cut_restart_authority') is not False
    gamble=gamble_allowed and policy['mode']=='gamble_when_low' and known and vitality<policy['below_vitality_percent']
    maximum=context.get('max_vitality')
    capacity_known=type(maximum) in (int,float) and math.isfinite(maximum) and 100<=maximum<=120
    return {'status':'selected','reason':GAMBLE_REASON if gamble else 'mei_unlock_guaranteed_safe',
        'choice_index':2 if gamble else 1,'event_id':event['event_id'],'story_id':STORY_ID,
        'expected_event':expected,'event_policy_version':'larc_events_v5',
        'effect_basis':'explicit_optional_mei_unlock_policy; reference_branch_probability_unknown',
        'candidates':[{'index':option['index'],'score':None,
            'branch_basis':'worst_known_branch' if option.get('conditional_branches') else 'unconditional',
            'reference_effects':deepcopy(option.get('effects',[])),
            'reference_branches':deepcopy(option.get('conditional_branches',[])),
            'effects_are_observed':False} for option in event['options']],
        'vitality':vitality,'mei_unlock_policy':policy,
        'mei_unlock_decision':{'cut_farming_active':cut_active,'cut_restart_authority':cut,'mei_gamble_authority':gamble_allowed,'current_vitality_percent':vitality if known else None,
            'below_vitality_percent':policy['below_vitality_percent'],'comparison':'strictly_below',
            'capacity_bounds_points':[maximum,maximum] if capacity_known else [100,120],
            'capacity_basis':'profile' if capacity_known else 'unknown_100_120_range',
            'next_action_turn':context.get('next_action_turn'),'control_epoch':context.get('control_epoch'),
            'gamble_selected':gamble,'restart_requires':'executed_option_2_and_explicit_failure_and_no_confirmed_cut'},
        'learned_skills_written':False}


def gamble_selection(selection):
    """A proposed choice alone is never an execution or failure receipt."""
    if not isinstance(selection,dict):return False
    decision=selection.get('mei_unlock_decision') or {}
    policy=selection.get('mei_unlock_policy') or {}
    return (selection.get('status')=='selected' and selection.get('reason')==GAMBLE_REASON
        and str(selection.get('story_id'))==STORY_ID and selection.get('choice_index')==2
        and decision.get('mei_gamble_authority',decision.get('cut_restart_authority',decision.get('cut_farming_active'))) is True
        and decision.get('gamble_selected') is True
        and policy.get('mode')=='gamble_when_low')


def failed_mei_gamble(ctx):
    """Read only a previously verified, executed and settled same-run gamble."""
    try:
        root=Path(ctx.root)
        data=json.loads((root/'session/larc_friend_outing.json').read_text(encoding='utf8'))
    except (AttributeError,OSError,ValueError,TypeError):return None
    event=data.get('unlock_event') or {}
    source=event.get('result_source') or {}
    choice=event.get('source') or {}
    turn=getattr(ctx,'progress',{}).get('next_turn')
    if (data.get('run_session_id')!=ctx.run_id or data.get('unlocked') is True
            or event.get('status')!='confirmed_failed' or event.get('gamble_armed') is not True
            or event.get('selected_option_index')!=2 or event.get('tap_performed') is not True
            or source.get('run_session_id')!=ctx.run_id or choice.get('run_session_id')!=ctx.run_id
            or type(source.get('turn_index')) is not int or type(choice.get('turn_index')) is not int
            or source['turn_index']<choice['turn_index'] or type(turn) is not int or source['turn_index']>turn
            or not source.get('sha256') or source.get('sha256')==choice.get('sha256')
            or event.get('result_basis') not in {'explicit_outing_unavailable_observation',
                'user_confirmed_energy_loss_and_absent_friend_badge'}):return None
    return deepcopy(event)


def confound_mei_gamble(report,root,run_id):
    """Another observed event prevents attributing the later energy bar to Mei."""
    from core.auto_trainer.larc_outing_session import MeiOutingSession
    if report.get('route_transition',{}).get('run_session_id') not in (None,run_id):return
    path=Path(root)/'session/larc_friend_outing.json'
    if not path.is_file():return
    try:session=MeiOutingSession.load(path,run_id)
    except (OSError,ValueError,TypeError):return
    event=session.data.get('unlock_event') or {}
    if event.get('gamble_armed') is not True or event.get('status')!='awaiting_result':return
    titles=[report.get(key) for key in ('title','event_title') if report.get(key)]
    for row in report.get('step_reports',[]):
        result=row.get('result') or {};frame=result.get('frame_safety') or {}
        if frame.get('text_status')!='ok':continue
        lines=[line.strip() for line in str(frame.get('visible_text','')).splitlines() if line.strip()]
        titles.extend(lines[i+1] for i,line in enumerate(lines[:-1])
            if line in {'养成优俊少女事件','协助卡事件','支援卡事件'})
    if any(title not in {'有时也要忘记工作','時には仕事を忘れて'} for title in titles):
        event.update(gamble_confounded=True,confounded_by={'basis':'another_observed_event','titles':titles})
        session.save(path)


def observe_mei_gamble_menu(ctx,source,provider):
    """One existing stable menu, before another action; no capture or menu taps."""
    from core.auto_trainer.larc_outing_session import MeiOutingSession
    from core.auto_trainer.larc_friend_outing import verified_source
    from core.auto_trainer.runtime_control import current
    import cv2
    from core.larc_outing_reader import read_friend_outing_hint
    try:
        path=Path(ctx.root)/'session/larc_friend_outing.json'
        if not path.is_file():return None
        session=MeiOutingSession.load(path,ctx.run_id)
    except (AttributeError,OSError,ValueError,TypeError):return None
    event=session.data.get('unlock_event') or {}
    if (session.data['unlocked'] or event.get('gamble_armed') is not True
            or event.get('status')!='awaiting_result'):return None
    choice=event.get('source') or {};decision=event.get('gamble_decision') or {}
    turn=getattr(ctx,'progress',{}).get('next_turn')
    expected=decision.get('next_action_turn') or choice.get('turn_index')
    epoch=current().epoch if current() is not None else None
    note={'status':'result_unknown','basis':'first_post_event_stable_menu_only',
          'current_action_turn':turn,'expected_action_turn':expected}
    def close(reason):
        note['reason']=reason
        event.update(status='result_unknown',menu_result_check=deepcopy(note))
        session.save(path)
        return note
    if type(turn) is not int or turn!=expected or event.get('gamble_confounded'):
        return close('another_action_or_event_before_result_check')
    if decision.get('control_epoch')!=epoch:return close('control_intervention_before_result_check')
    state=provider.get('state_context') or {}
    body=state.get('vitality');before=decision.get('current_vitality_percent')
    menu_source={'run_session_id':ctx.run_id,'turn_index':turn,'path':str(Path(source).resolve()),
        'sha256':provider.get('source',{}).get('sha256')}
    # valid_for_training already verifies the main-menu screen. The provider
    # deliberately projects numerical context without a second screen field.
    if (provider.get('valid_for_training') is not True
            or not verified_source(menu_source,run_id=ctx.run_id) or menu_source['sha256']==choice.get('sha256')
            or any(type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=100
                for value in (before,body))):
        return close('current_body_or_menu_source_unverified')
    try:hint=read_friend_outing_hint(cv2.imread(str(source)))
    except (cv2.error,OSError,ValueError,TypeError):return close('friend_badge_read_unavailable')
    bounds=decision.get('capacity_bounds_points') or [100,120]
    if (len(bounds)!=2 or any(type(value) not in (int,float) or not math.isfinite(value) for value in bounds)
            or not 100<=bounds[0]<=bounds[1]<=120):return close('capacity_basis_unverified')
    # Failure consumes a fixed ten game points. Recovery support bonuses do
    # not scale that loss. Unknown capacity remains a range, not exact 110.
    expected_delta=[max(-before,-1000/bounds[0]),max(-before,-1000/bounds[1])]
    delta=body-before;tolerance=1.0
    note.update(before_vitality_percent=before,after_vitality_percent=body,
        observed_delta_percent=delta,expected_delta_percent_bounds=expected_delta,
        capacity_bounds_points=bounds,capacity_basis=decision.get('capacity_basis'),
        tolerance_bar_percent=tolerance,failure_energy_game_points=-10,
        usable_failure_loss_points_bounds=[min(10,before*bounds[0]/100),min(10,before*bounds[1]/100)],
        friend_hint=deepcopy(hint),source=menu_source,choice_source=deepcopy(choice))
    if before<=0 or delta>=0:return close('no_observable_energy_loss')
    if hint.get('status')!='accepted' or hint.get('available') is not False:
        return close('friend_badge_present_or_unverified')
    if not expected_delta[0]-tolerance<=delta<=expected_delta[1]+tolerance:
        return close('body_change_not_the_bound_event_failure')
    observation={'status':'accepted','result_observed':True,'story_id':STORY_ID,
        'character':'satake_mei','available':False,
        'basis':'user_confirmed_energy_loss_and_absent_friend_badge','evidence':deepcopy(note)}
    if not session.observe_unlock_result(observation,menu_source):return close('result_binding_unverified')
    note.update(status='confirmed_failed',reason='user_confirmed_energy_loss_and_absent_friend_badge')
    event['menu_result_check']=deepcopy(note)
    session.save(path)
    return note
