"""独立刷切控制器；默认仅检查配置，--execute 才允许操作游戏。"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from core.auto_trainer.cut_farming import (
    CHARACTERS, CutFarmingConfig, CutFarmingHook, CutSession, TERMINAL_STATES,
    atomic_json, load_event_references,
)
from core.auto_trainer.cut_condition_probe import CutConditionProbe
from core.auto_trainer.cut_restart_route import CutRestartRoute
from core.auto_trainer.runtime_control import RunControl,RunStopped,bind,current_page_resume


def make_attempt(session, *, template_root=None):
    from scripts.larc_panel_setup import create_panel_run,DEFAULT_TARGETS,prepare_panel_policy,save_policy_migration
    from core.auto_trainer.config import UGParentGoal,validate_goal_config
    from core.auto_trainer.run_support_profile import load_profile,save_profile,merge_observation
    template=Path(template_root) if template_root else None
    goal=json.loads((template/'goal.json').read_text(encoding='utf-8-sig')) if template else None
    root=create_panel_run(device_port=session.config.device_port,
        targets=goal.get('target_stats',DEFAULT_TARGETS) if goal else DEFAULT_TARGETS,
        runs_dir=session.root/'attempts',strategy=session.config.strategy,
        larc_policy=session.data.get('larc_policy'),task_mode='cut',support_profile=session.data.get('support_profile'))
    if goal:
        source={**goal.get('larc_policy',{}),**session.data.get('larc_policy',{})}
        prepared=prepare_panel_policy(source,strategy=session.config.strategy,task_mode='cut')
        goal['larc_policy']=prepared['policy']
        validate_goal_config(UGParentGoal.from_dict(goal))
        save_policy_migration(root/'session',prepared,label='attempt-template')
        atomic_json(root/'session/larc_task_goal.json',prepared['task_goal'])
        atomic_json(root/'goal.json',goal)
        profile=load_profile(template,template.name)
        if profile and not session.data.get('support_profile'):
            fields={'slot','card_id','character_id','rarity','type','card_type','title','name','level','limit_breaks','actual_effects','actual_effects_source'}
            cards=[{key:value for key,value in card.items() if key in fields} for card in profile['cards']]
            save_profile(root,root.name,{'schema_version':'uma_current_support_deck.v1',
                'source':{'kind':'cut_farming_user_static_setup'},'cards':cards,'max_vitality':None})
    goal=json.loads((root/'goal.json').read_text(encoding='utf-8-sig'))
    # Each attempt gets the immutable batch's explicit settings, never a
    # template's old high-peak opt-in. Old batches stay disabled on restart.
    from core.auto_trainer.larc_support_skill_startup import normalize_mid_skill_learning
    from scripts.larc_panel_setup import parse_early_skill_allowlist
    if session.data.get('race_retry') is not None:goal['race_retry']=dict(session.data['race_retry'])
    if session.data.get('running_style') is not None:goal['running_style']=session.data['running_style']
    if session.data.get('race_running_styles'):goal['race_running_styles']=dict(session.data['race_running_styles'])
    if session.data.get('custom_races') is not None:goal['custom_races']=list(session.data['custom_races'])
    timing=goal.setdefault('skill_timing',{})
    timing['mid_skill_learning']=normalize_mid_skill_learning(session.data.get('mid_skill_learning'))
    timing['early_skill_allowlist']=parse_early_skill_allowlist(session.data.get('early_skill_allowlist',[]))
    if session.config.support_deck or session.config.borrow_card_id:
        # A selected deck invalidates the template's five own-card facts.
        # A configured borrowed minimum is a request, never an observed level.
        profile=load_profile(root,root.name)
        original_cards=(profile or {}).get('cards',[])
        if not session.config.borrow_card_id and not session.data.get('legacy_borrow_target'):
            from types import SimpleNamespace
            from core.auto_trainer.cut_friend_support import target_from_profile
            target=target_from_profile(SimpleNamespace(root=root,run_id=root.name))
            if target:
                session.data['legacy_borrow_target']=target;session.save()
        cards=[] if session.config.support_deck else [card for card in original_cards if card['slot']<5]
        from core.auto_trainer.cut_support_selection import actual_deck_profile
        observed_cards=actual_deck_profile(session.data.get('support_setup_observation'),expected_deck=session.config.support_deck)
        for observed in observed_cards:
            previous=next((card for card in original_cards if card.get('slot')==observed['slot']
                and card.get('card_id')==observed['card_id'] and card.get('level')==observed['level']
                and observed['limit_breaks'] is not None and card.get('limit_breaks')==observed['limit_breaks']),None)
            if previous and not observed.get('actual_effects'):
                observed['actual_effects']=previous.get('actual_effects',{})
                observed['actual_effects_source']=previous.get('actual_effects_source')
            cards=[card for card in cards if card['slot']!=observed['slot']]
            cards.append(observed)
        if profile and profile.get('declared_profile'):
            merge_observation(root,root.name,{'schema_version':'uma_current_support_deck.v1',
                'source':{'kind':'observed','phase':'cut_support_setup'},'cards':observed_cards,'max_vitality':None})
        else:
            save_profile(root,root.name,{'schema_version':'uma_current_support_deck.v1',
                'source':{'kind':'cut_support_explicit_setup','owned_cards':'observed' if any(c['slot']<5 for c in observed_cards)
                    else 'unknown' if session.config.support_deck else 'template_static'},
                'cards':cards,'max_vitality':None})
    policy=goal.setdefault('larc_policy',{})
    from core.auto_trainer.larc_user_preferences import normalize, feature_available
    if feature_available() and session.data.get('decision_preferences') is not None:
        policy['decision_preferences']=normalize(session.data['decision_preferences'])
    elif session.config.strategy!='baseline':policy.pop('decision_preferences',None)
    if session.config.strategy!='baseline':
        if session.config.discard_found_for_validation:policy['ss_planning_horizon']='junior_cutoff_discard'
        else:policy.pop('ss_planning_horizon',None)
    validate_goal_config(UGParentGoal.from_dict(goal));atomic_json(root/'goal.json',goal)
    if session.config.strategy=='baseline' and session.data.get('larc_task_goal'):
        atomic_json(root/'session/larc_task_goal.json',session.data['larc_task_goal'])
    if session.config.continuation is not None:
        from scripts.larc_continuation_runtime import save_options
        save_options(root, session.config.continuation)
    from core.auto_trainer.larc_ai_options import save_options as save_ai_options
    save_ai_options(root,session.data.get('ai_options'),session.data.get('ai_connection'))
    if (session.root/'session/career_workflow.json').is_file():
        from scripts.career_workflow_runtime import attach_attempt
        attach_attempt(session,root)
    return root


def local_alert(message):
    print(message)
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except (ImportError,RuntimeError):
        pass


class CutFarmingController:
    def __init__(self, session, *, control=None, career_runner=None, probe=None, restart=None, alert=local_alert, notifier=None,freeze_recovery=None,race_recovery=None,stop_capture=None):
        self.session=session;self.control=control or RunControl()
        self.control.pause_steps=None
        self.control.ss_planning_horizon=((session.data.get('larc_policy') or {}).get('ss_planning_horizon','junior_cutoff_discard')
            if session.config.strategy=='baseline' else
            'junior_cutoff_discard' if session.config.discard_found_for_validation else 'full_career')
        self.career_runner=career_runner
        from core.auto_trainer.cut_stop_diagnostic import save_stop_diagnostic
        # Injected career runners are simulations unless they explicitly supply
        # their own diagnostic capture. They must not start a real ADB process.
        self.stop_capture=stop_capture or (save_stop_diagnostic if career_runner is None else None)
        self.probe=probe or CutConditionProbe()
        self.restart=restart or CutRestartRoute(session)
        self.alert,self.notifier=alert,notifier
        from core.auto_trainer.cut_training_freeze_recovery import CutTrainingFreezeRecovery
        self.freeze_recovery=freeze_recovery or CutTrainingFreezeRecovery(session)
        from core.auto_trainer.cut_training_transition_recovery import CutTrainingTransitionFreezeRecovery
        self.transition_freeze_recovery=CutTrainingTransitionFreezeRecovery(session)
        from core.auto_trainer.cut_race_freeze_recovery import CutRaceFreezeRecovery
        self.race_freeze_recovery=race_recovery or CutRaceFreezeRecovery(session)

    def run(self, *, execute=False, template_root=None,resume=False,take_over_current=False,checkpoint_restart_report=None):
        config=self.session.config
        if self.session.data.get('ss_sieve_cycle'):
            if not execute:
                return {'status':'dry_run','config':config.public_dict(),'device_actions':0}
            from scripts.career_sieve_cycle import SieveCycle
            return SieveCycle(self.session,self.control,runner=self.career_runner,restart=self.restart).run(resume=resume)
        if not execute:
            references=load_event_references(config)
            return {'status':'dry_run','config':config.public_dict(),'target_events':list(references),'device_actions':0}
        if checkpoint_restart_report is None and resume and (self.session.root/'session/career_workflow.json').is_file():
            from scripts.career_workflow_runtime import load_for_run, checkpoint_restart_permission
            latest=Path((self.session.data.get('attempts') or [{}])[-1].get('run_root',''))
            if latest.is_dir() and checkpoint_restart_permission(load_for_run(latest),latest) is not None:
                checkpoint_restart_report={'stop_reason':'continuation_gate_failed','run_session_id':latest.name}
        if checkpoint_restart_report is not None:
            from scripts.career_workflow_runtime import prepare_checkpoint_restart
            target=Path((self.session.data.get('attempts') or [{}])[-1].get('run_root',''))
            if prepare_checkpoint_restart(self.session,target,checkpoint_restart_report) is not True:
                return {'status':'workflow_stopped','reason':'checkpoint_restart_not_authorized','session_root':str(self.session.root)}
            resume=True
        if not resume and (self.session.data['status'] in TERMINAL_STATES or self.session.data.get('pending_action')):
            return {'status':self.session.data['status'],'reason':'cut_session_already_stopped_or_pending'}
        resumed_root=None
        if resume:
            try:resumed_root=self.session.resume_attempt()
            except (ValueError,OSError):
                return {'status':self.session.data['status'],'reason':'cut_resume_not_allowed','session_root':str(self.session.root)}
        from scripts import larc_autonomous_run as career
        runner=self.career_runner or career.run
        images=None;root=None;diagnostic_started=time.time();outing_stop_diagnostic=None
        def remember_current_frame():
            if root is not None and self.stop_capture:
                from core.auto_trainer.cut_stop_diagnostic import remember_recent_game_frame
                remember_recent_game_frame(root,config.device_port,captured_after=diagnostic_started)
        def release_images(*,closed=False,remember=True):
            if closed and root is not None and self.stop_capture:
                from core.auto_trainer.cut_stop_diagnostic import forget_recent_game_frame
                try:forget_recent_game_frame(root,config.device_port,captured_after=diagnostic_started)
                except (OSError,ValueError) as exc:self.session.data['recent_frame_warning']=type(exc).__name__
                remember=False
            if images is not None:
                try:
                    result=images.release(keep_identity_references=not closed,
                        current_run=root if remember and self.stop_capture else None,
                        device_port=config.device_port,captured_after=diagnostic_started)
                    self.session.data.setdefault('screenshot_releases',[]).append(result)
                except (OSError, ValueError, KeyError) as exc:
                    self.session.data['screenshot_release_warning']=type(exc).__name__
                self.session.save()
            elif remember:
                try:remember_current_frame()
                except (OSError,ValueError) as exc:self.session.data['recent_frame_warning']=type(exc).__name__
        try:
            if not config.retain_screenshots:
                from core.auto_trainer.cut_screenshot_lifecycle import NewScreenshotLifecycle
                images=NewScreenshotLifecycle(self.session.root)
                if resumed_root is not None:
                    images.adopt_resumed_identity_cache(resumed_root)
            root=resumed_root if resume else make_attempt(self.session,template_root=template_root)
            resume_current=resume
            try:
                from core.auto_trainer.pacer_storage import maintain
                maintain(Path(__file__).resolve().parents[1],
                         exclude_run=root,exclude_batch=self.session.root,
                         runtime_owner={'run_root':Path(root).absolute(),
                             'batch_root':self.session.root.absolute(),
                             'endpoint':f'127.0.0.1:{config.device_port}',
                             'pins':[Path(template_root).absolute()] if template_root else []})
            except Exception as error:
                import logging
                logging.getLogger(__name__).warning("截图维护未完成：%s",type(error).__name__)
            current_cycle_first=take_over_current and not resume
            take_over_current=resume or take_over_current
            if current_cycle_first:
                from core.auto_trainer.run_support_profile import clear_observation
                clear_observation(root,root.name)
            with bind(self.control,f'127.0.0.1:{config.device_port}'):
                while True:
                    try:
                        from core.auto_trainer.pacer_storage import maintain
                        maintain(Path(__file__).resolve().parents[1],
                                 exclude_run=root,exclude_batch=self.session.root,
                                 runtime_owner={'run_root':Path(root).absolute(),
                                     'batch_root':self.session.root.absolute(),
                                     'endpoint':f'127.0.0.1:{config.device_port}',
                                     'pins':[Path(template_root).absolute()] if template_root else []})
                    except Exception as error:
                        import logging
                        logging.getLogger(__name__).warning("截图维护未完成：%s",type(error).__name__)
                    if resume_current:resume_current=False
                    elif not self.session.begin_attempt(root):break
                    self.control.checkpoint('cut_attempt_start')
                    kwargs={'probe':self.probe,'local_alert':self.alert}
                    if self.notifier is not None:kwargs['notify']=self.notifier
                    hook=CutFarmingHook(self.session,**kwargs)
                    args=career.parse_args(['--run-root',str(root),'--adb-port',str(config.device_port),
                        '--max-actions','72','--execute','--confirm-token',career.CONFIRM_TOKEN,
                        '--allow-fixed-races','--allow-adaptations','--d1-startup-only','--training-records',
                        'keep' if config.retain_screenshots else 'discard'])
                    args._cut_farming_hook=hook;args._keep_run_control=True
                    args._player_live_takeover=take_over_current
                    args._continuation_user_continue = resume is True
                    args._new_cycle_live_takeover=current_cycle_first
                    args._new_cycle_previous_transaction=(self.session.data.get('entry_context') or {}).get('previous_transaction')
                    from core.auto_trainer.larc_ai_options import load_options as load_ai_options
                    args._ai_decision_options=load_ai_options(root)
                    current_cycle_first=False
                    take_over_current=False
                    call_started=int(time.time()*1000);call_clock=time.monotonic()
                    with current_page_resume(self.control):
                        if checkpoint_restart_report is not None:
                            report,code=checkpoint_restart_report,2
                            checkpoint_restart_report=None
                        else:
                            report,code=runner(args)
                    outing_stop_diagnostic=None
                    if self.stop_capture:
                        # The actual failed frame must be preserved before
                        # release_images can remove this attempt's PNGs.
                        from core.auto_trainer.cut_stop_diagnostic import save_outing_failure_diagnostic
                        started=int(time.time()*1000)
                        try:
                            saved=save_outing_failure_diagnostic(root,config.device_port,
                                report.get('stop_reason','career_stopped'),report)
                            if saved is not None:
                                outing_stop_diagnostic={'run_root':root,'segment':report.get('output'),
                                    'capture_started_at_ms':started,'metadata':saved}
                        except Exception as error:
                            self.session.data['stop_diagnostic_warning']=type(error).__name__
                            # The helper returns None before any I/O for other
                            # faults. A persistence error here belongs to this
                            # reader frame and never authorizes a fresh image.
                            from core.auto_trainer.recognition_failure_archive import bound_reader_failure
                            if (bound_reader_failure(report) and report.get('run_session_id')==root.name):
                                outing_stop_diagnostic={'run_root':root,'segment':report.get('output'),
                                    'capture_started_at_ms':started,'metadata':{'status':'unavailable'}}
                    attempt=self.session.data['attempts'][-1]
                    attempt.setdefault('career_runs',[]).append({'started_at_unix_ms':call_started,
                        'stopped_at_unix_ms':int(time.time()*1000),'elapsed_seconds':max(0.,time.monotonic()-call_clock),
                        'stop_reason':report.get('stop_reason')})
                    attempt['career_execution_seconds']=sum(item['elapsed_seconds'] for item in attempt['career_runs'])
                    usage=attempt.setdefault('race_clock_usage',{})
                    for race in report.get('race_reports',[]):
                        if not race.get('cut_debut_race'):continue
                        key=str(race.get('race_turn'));previous=usage.setdefault(key,{})
                        for field in ('clock_retries_used','clock_items_committed','clock_free_retries'):
                            previous[field]=max(previous.get(field,0),race.get(field,0))
                    for field in ('clock_retries_used','clock_items_committed','clock_free_retries'):
                        attempt[field]=sum(item.get(field,0) for item in usage.values())
                    if (self.session.root/'session/career_workflow.json').is_file():
                        from scripts.career_workflow_runtime import segment_disposition
                        disposition=segment_disposition(self.session,root,report)
                        if disposition=='reobserve':
                            report={**report,'workflow_previous_stop_reason':report.get('stop_reason'),
                                'stop_reason':'player_resume_current_page'}
                        elif disposition=='hold':
                            report={**report,'workflow_previous_stop_reason':report.get('stop_reason'),
                                'stop_reason':'continuation_hold'}
                    if report.get('stop_reason')=='player_resume_current_page' and not self.control.stopping:
                        attempt.setdefault('player_resumes',[]).append({
                            'resumed_at_unix_ms':int(time.time()*1000),
                            'resume_epoch':self.control.epoch,'previous_output':report.get('output')})
                        self.session.save();release_images()
                        resume_current=True;take_over_current=True
                        continue
                    if (report.get('stop_reason') in {'larc_ss_information_failed','career_transition_no_progress_timeout','race_transition_no_progress_timeout'} and not self.control.stopping
                            and self.session.data['status']=='running'):
                        recover=(self.freeze_recovery if report.get('stop_reason')=='larc_ss_information_failed'
                            else self.race_freeze_recovery if report.get('stop_reason')=='race_transition_no_progress_timeout'
                            else self.transition_freeze_recovery)
                        recovered=recover(root,report,career_run_index=len(attempt['career_runs'])-1)
                        attempt.setdefault('recovery_checks',[]).append({'stop_reason':report.get('stop_reason'),
                            **{k:recovered[k] for k in ('handled','reason','ok','restart_performed') if k in recovered}})
                        if recovered.get('handled') is not False:
                            self.session.save()
                            if recovered.get('ok'):
                                release_images()
                                resume_current=True;continue # Same root/progress, no new begin_attempt.
                            self.session.stop('unknown',reason=recovered.get('reason','automatic_freeze_recovery_unknown'));break
                    release_images()
                    if (self.session.root/'session/career_workflow.json').is_file():
                        from scripts.career_workflow_runtime import continue_after_cut_report
                        workflow_result=continue_after_cut_report(self.session,root,report,self.control,
                            career_runner=self.career_runner)
                        if workflow_result is not None:
                            if workflow_result.get('resume_cut_restart') is True:
                                report=workflow_result['career_report']
                            else:
                                return workflow_result
                        from scripts.career_workflow_runtime import prepare_checkpoint_restart
                        authorization=prepare_checkpoint_restart(self.session,root,report)
                        if authorization is False:
                            self.session.stop('stopped',reason='checkpoint_restart_not_authorized',run_id=root.name)
                            break
                    if report.get('stop_reason') in {'continuation_handoff', 'continuation_hold'}:
                        # Not a completed career or a failed attempt to replace.
                        self.session.stop('handoff', reason=report['stop_reason'], run_id=root.name)
                        self.session.save()
                        break
                    self.session.finish_attempt(report.get('stop_reason','career_stopped'))
                    self.session.data['attempts'][-1]['career_report']={'stop_reason':report.get('stop_reason'),'output':report.get('output')}
                    self.session.save()
                    if self.session.data['status'] in TERMINAL_STATES:
                        break
                    if self.control.stopping or report.get('stopped_by_control'):
                        self.session.stop('stopped',reason='user_stop');break
                    if report.get('normal_stop') is True:
                        self.session.stop('stopped',reason=report.get('stop_reason','tp_resources_unavailable'),
                            normal_stop=True,resource_status=report.get('resource_status'),
                            message=report.get('stop_message'),run_id=root.name)
                        break
                    if report.get('stop_reason') not in {'cut_cutoff_absent','cut_found_for_validation','cut_benchmark_cutoff','cut_mei_unlock_failed','cut_character_event_absent','cut_training_failed','cut_supporter_deadline_missed','continuation_gate_failed'}:
                        self.session.stop('route_failed',reason=report.get('stop_reason','career_did_not_reach_cut_checkpoint'));break
                    final_attempt=len(self.session.data['attempts'])>=config.max_attempts
                    self.control.checkpoint('cut_restart')
                    from types import SimpleNamespace
                    ctx=SimpleNamespace(root=root,run_id=root.name,port=config.device_port)
                    restart_started=int(time.time()*1000);restart_clock=time.monotonic()
                    attempt['restart_started_at_unix_ms']=restart_started;self.session.save()
                    if (self.session.root/'session/career_workflow.json').is_file():
                        from scripts.career_workflow_runtime import checkpoint_restart_started
                        if checkpoint_restart_started(self.session,root) is False:
                            self.session.stop('stopped',reason='checkpoint_restart_already_submitted',run_id=root.name)
                            break
                    try:route=self.restart(ctx,root/'cut_restart',stop_after_abandon=True) if final_attempt else self.restart(ctx,root/'cut_restart')
                    finally:
                        attempt['restart_stopped_at_unix_ms']=int(time.time()*1000)
                        attempt['restart_elapsed_seconds']=max(0.,time.monotonic()-restart_clock)
                        self.session.save()
                    atomic_json(root/'cut_restart_report.json',route)
                    if (self.session.root/'session/career_workflow.json').is_file():
                        from scripts.career_workflow_runtime import checkpoint_restart_finished
                        checkpoint_restart_finished(self.session,root,route)
                    release_images(closed=route.get('ok') is True)
                    if not route.get('ok'):
                        normal_stop=route.get('normal_stop') is True
                        evidence={'reason':route.get('reason','restart_failed'),'run_id':root.name}
                        if normal_stop:evidence.update(normal_stop=True,resource_status=route.get('resource_status'),message=route.get('message'))
                        self.session.stop('stopped' if normal_stop else 'route_failed',**evidence);break
                    if route.get('reason') in {'ordinary_abandon_confirmed', 'ordinary_start_confirmed'}:
                        try:
                            from core.auto_trainer.recognition_failure_archive import complete_run_feedback
                            complete_run_feedback(root, 'abandoned')
                        except Exception:
                            pass
                    if final_attempt:
                        if route.get('reason')!='ordinary_abandon_confirmed':
                            self.session.stop('route_failed',reason='final_abandon_unverified',run_id=root.name);break
                        attempt['status']='abandoned'
                        self.session.stop('limit_reached',reason='attempt_limit_after_abandon');break
                    self.session.data['attempts'][-1]['status']='abandoned_and_restarted'
                    self.session.save()
                    root=make_attempt(self.session,template_root=template_root)
        except RunStopped:
            self.session.stop('stopped',reason='user_stop')
        except KeyboardInterrupt:
            self.control.request('stop');self.session.stop('stopped',reason='user_stop')
        except Exception as exc:
            self.session.stop('route_failed',reason=type(exc).__name__)
        finally:
            if self.stop_capture and root is not None and self.session.data['status'] in {'unknown','route_failed'}:
                saved=(outing_stop_diagnostic if outing_stop_diagnostic is not None
                    and outing_stop_diagnostic['run_root']==root
                    and outing_stop_diagnostic['segment']==(locals().get('report')or {}).get('output') else None)
                if saved is not None:
                    capture_started_at_ms=saved['capture_started_at_ms']
                else:
                    try:remember_current_frame()
                    except Exception as error:self.session.data['recent_frame_warning']=type(error).__name__
                    capture_started_at_ms=int(time.time()*1000)
                    try:
                        # Other faults keep their existing fresh/cache path.
                        self.stop_capture(root,config.device_port,(self.session.data.get('stop') or {}).get('reason',self.session.data['status']))
                    except Exception as error:
                        self.session.data['stop_diagnostic_warning']=type(error).__name__
                try:
                    from core.auto_trainer.recognition_failure_archive import archive_abnormal_stop
                    self.session.data['recognition_failure_archive']=archive_abnormal_stop(root,self.session.data,
                        summary=locals().get('report'),capture_started_at_ms=capture_started_at_ms,operator_stopping=self.control.stopping)
                    self.session.save()
                except Exception:pass
            if (root is not None and self.session.data.get('status') == 'stopped'
                    and (self.session.data.get('stop') or {}).get('reason') == 'user_stop'):
                try:
                    from core.auto_trainer.recognition_failure_archive import complete_run_feedback
                    complete_run_feedback(root, 'operator_stop')
                except Exception:
                    pass
            release_images(remember=False)
            self.control.finish()
            if execute and not getattr(self, '_defer_storage_maintenance', False):
                try:
                    from core.auto_trainer.pacer_storage import maintain
                    maintain(Path(__file__).resolve().parents[1], min_interval=0,
                             exclude_run=root,exclude_batch=self.session.root)
                except Exception as error:
                    import logging
                    logging.getLogger(__name__).warning("截图维护未完成：%s", type(error).__name__)
        result={'status':self.session.data['status'],'session_root':str(self.session.root),'attempts':len(self.session.data['attempts'])}
        if (self.session.root/'session/career_workflow.json').is_file():
            from scripts.career_workflow_runtime import finish_cut_state
            result['workflow']=finish_cut_state(self.session)
        stopped=self.session.data.get('stop') or {}
        if stopped.get('normal_stop') is True:
            result.update(normal_stop=True,reason=stopped.get('reason'),message=stopped.get('message'),resource_status=stopped.get('resource_status'))
        return result


def parse_args(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--session-root',type=Path)
    parser.add_argument('--template-root',type=Path,help='只复用目标配置与卡片等级破数，不复用旧局状态')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--resume',action='store_true',help='明确续跑最后一局；保留原局号和进度，不恢复切状态未知或未确认动作')
    parser.add_argument('--window',action='store_true')
    parser.add_argument('--control-window',action='store_true',help='本次CLI运行附带暂停／继续小窗口；使用同一控制器和育成进程')
    return parser.parse_args(argv)


def open_window():
    import tkinter as tk
    from tkinter import ttk,filedialog,messagebox
    from scripts.larc_panel_setup import DEVICES
    root=tk.Tk();root.title('Uma 独立刷切');root.geometry('540x520')
    frame=ttk.Frame(root,padding=20);frame.pack(fill='both',expand=True)
    character=tk.StringVar(value=CHARACTERS['101001']['name']);device=tk.StringVar(value='MuMu 安卓设备 2')
    root.title(f'Uma 独立刷切 · {device.get()}')
    strategy=tk.StringVar(value='统一基线');attempts=tk.StringVar(value='20')
    check_mode=tk.StringVar(value='事件触发＋间隔补查');interval=tk.StringVar(value='4')
    debut_retry=tk.BooleanVar(value=True)
    training_failure_restart=tk.BooleanVar(value=False)
    retain_images=tk.BooleanVar(value=True)
    items=tk.StringVar(value='0');carrots=tk.StringVar(value='0');template=tk.StringVar(value='');token=tk.StringVar(value='')
    deck=tk.StringVar(value='0');borrow=tk.StringVar(value='0');borrow_level=tk.StringVar(value='1');borrow_break=tk.StringVar(value='0')
    status=tk.StringVar(value='请先在游戏配好角色／卡组／种马，停在第一回合主页。')
    entries=[]
    for label,variable,choices in [('角色预设',character,[v['name'] for v in CHARACTERS.values()]),('模拟器',device,list(DEVICES)),
        ('基础策略',strategy,['统一基线']),('查切模式',check_mode,['事件触发＋间隔补查','每回合（对照）']),
        ('保底间隔（0关闭）',interval,None),('最多尝试',attempts,None),('TP药最多使用',items,None),('萝卜最多消耗',carrots,None),
        ('卡组编号（0沿用）',deck,None),('借卡ID（0沿用）',borrow,None),('借卡最低等级',borrow_level,None),('借卡最低突破',borrow_break,None)]:
        row=ttk.Frame(frame);row.pack(fill='x',pady=3);ttk.Label(row,text=label,width=18).pack(side='left')
        widget=ttk.Combobox(row,textvariable=variable,values=choices,state='readonly') if choices else ttk.Entry(row,textvariable=variable)
        widget.pack(side='left',fill='x',expand=True);entries.append(widget)
    retry_widget=ttk.Checkbutton(frame,text='出道失利用已有闹钟重赛（最多5次，不购买）',variable=debut_retry)
    retry_widget.pack(anchor='w',pady=4);entries.append(retry_widget)
    failure_widget=ttk.Checkbutton(frame,text='训练失败后弃养重开',variable=training_failure_restart)
    failure_widget.pack(anchor='w',pady=4);entries.append(failure_widget)
    image_widget=ttk.Checkbutton(frame,text='保留运行截图（关闭后只留文本记录和必要身份参考）',variable=retain_images)
    image_widget.pack(anchor='w',pady=4);entries.append(image_widget)
    for label,variable in [('复用目标记录（可留空）',template),('PushPlus令牌文件（可留空）',token)]:
        ttk.Label(frame,text=label).pack(anchor='w',pady=(8,0))
        entry=ttk.Entry(frame,textvariable=variable);entry.pack(fill='x');entries.append(entry)
    confirmed=tk.BooleanVar(value=False)
    confirmation=ttk.Checkbutton(frame,text='已配好所选角色、卡组和种马，当前在该局第一回合',variable=confirmed)
    confirmation.pack(anchor='w',pady=10);entries.append(confirmation)
    ttk.Label(frame,text='12月下开始仍没切就重开；确认有切立即保留本局并停止。\n仅本模式按填写的上限补TP；不充值，不自动续养。',wraplength=490).pack(anchor='w')
    ttk.Label(frame,textvariable=status,wraplength=490).pack(anchor='w',pady=10)
    active={'controller':None,'thread':None,'result':None,'alert':None}

    def start():
        if active['thread'] and active['thread'].is_alive():return
        if not confirmed.get():messagebox.showerror('需要配置好当前局','请先配好所选角色、卡组和种马，再勾选本局确认。',parent=root);return
        try:
            identity=next(k for k,v in CHARACTERS.items() if v['name']==character.get())
            config=CutFarmingConfig.from_dict({'character_id':identity,'device_port':DEVICES[device.get()],
                'strategy':'baseline','max_attempts':int(attempts.get()),
                'check_mode':'event_triggered' if check_mode.get()=='事件触发＋间隔补查' else 'every_turn',
                'fallback_interval_turns':int(interval.get()),
                'debut_clock_retry':debut_retry.get(),
                'restart_after_training_failure':training_failure_restart.get(),
                'retain_screenshots':retain_images.get(),
                'support_deck':int(deck.get()),'borrow_card_id':int(borrow.get()),
                'borrow_min_level':int(borrow_level.get()),'borrow_min_break':int(borrow_break.get()),
                'max_tp_items':int(items.get()),'max_carrots':int(carrots.get()),'pushplus_token_file':token.get().strip() or None})
            load_event_references(config)
            directory=ROOT/'runs'/'cut_farming'/f'{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}'
            controller=CutFarmingController(CutSession(directory,config),alert=lambda message:active.update(alert=message))
            root.title(f'Uma 独立刷切 · {device.get()}')
            active.update(controller=controller,result=None)
            chosen_template=template.get().strip() or None
            def work():active['result']=controller.run(execute=True,template_root=chosen_template)
            active['thread']=threading.Thread(target=work,name='cut-farming',daemon=True);active['thread'].start()
            status.set('正在刷切，开始后参数已锁定。')
            for entry in entries:entry.configure(state='disabled')
        except (ValueError,OSError) as exc:messagebox.showerror('配置未完成',str(exc),parent=root)

    def stop():
        if active['controller']:active['controller'].control.request('stop');status.set('正在停止，保留当前局。')
    def pause():
        if active['controller']:active['controller'].control.request('pause');status.set('正在准备暂停，请等显示“已暂停”后查看游戏。')
    def resume_inspection():
        if active['controller']:active['controller'].control.request('resume');status.set('正在重新识别当前页面…')
    def refresh():
        if active['controller'] and active['thread'] and active['thread'].is_alive():
            snapshot=active['controller'].control.snapshot()
            if snapshot['state']=='paused':status.set(snapshot.get('note') or '已暂停，可以查看游戏；检查完请点继续。')
            elif snapshot.get('pause_requested'):status.set('正在准备暂停，请等显示“已暂停”后查看游戏。')
            else:status.set(snapshot.get('note') or '正在刷切。需要查看游戏时，请先暂停。')
        if active['alert']:
            message=active.pop('alert');active['alert']=None;local_alert(message);messagebox.showinfo('已确认才能出众',message,parent=root)
        if active['result']:
            result=active['result']
            message={
                'found':'已确认才能出众，本局已保留。',
                'unknown':'状态没有读清，已停止并保留现场。',
                'stopped':'已按要求停止，当前局已保留。',
                'limit_reached':'已达到设置的次数上限，刷切已停止。',
                'route_failed':'流程未能继续，已停止，请查看当前游戏页面。',
            }.get(result['status'],'已停止，请查看运行记录。')
            status.set(f"{message}\n记录：{result.get('session_root','')}")
        if active['thread'] and not active['thread'].is_alive():
            for entry in entries:entry.configure(state='readonly' if isinstance(entry,ttk.Combobox) else 'normal')
        root.after(250,refresh)
    buttons=ttk.Frame(frame);buttons.pack(fill='x',pady=12)
    ttk.Button(buttons,text='开始刷切',command=start).pack(side='left',fill='x',expand=True,padx=4)
    ttk.Button(buttons,text='暂停查看',command=pause).pack(side='left',fill='x',expand=True,padx=4)
    ttk.Button(buttons,text='继续识别',command=resume_inspection).pack(side='left',fill='x',expand=True,padx=4)
    ttk.Button(buttons,text='停止并保留当前局',command=stop).pack(side='left',fill='x',expand=True,padx=4)
    def close():
        if active['thread'] and active['thread'].is_alive():stop();root.after(250,close)
        else:root.destroy()
    root.protocol('WM_DELETE_WINDOW',close);root.update_idletasks();root.geometry(f'540x{max(520,frame.winfo_reqheight()+20)}')
    root.after(250,refresh);root.mainloop()


def run_control_window(controller, *, template_root=None, resume=False):
    """A single CLI controller with an optional local control surface."""
    import tkinter as tk
    from tkinter import ttk
    device_name={16384:'设备0',16416:'设备1',16448:'设备2'}[controller.session.config.device_port]
    root=tk.Tk();root.title(f'Uma 刷切运行控制 · {device_name}')
    frame=ttk.Frame(root,padding=16);frame.pack(fill='both',expand=True)
    status=tk.StringVar(value='正在开始刷切…');ttk.Label(frame,textvariable=status,wraplength=420).pack(pady=8)
    active={'result':None}
    def work():active['result']=controller.run(execute=True,template_root=template_root,resume=resume)
    thread=threading.Thread(target=work,name='cut-farming',daemon=True)
    def pause():controller.control.request('pause');status.set('正在准备暂停，请等显示“已暂停”后查看游戏。')
    def resume_inspection():controller.control.request('resume');status.set('正在重新识别当前页面…')
    def stop():controller.control.request('stop');status.set('正在停止，保留当前局。')
    for label,callback in [('暂停查看',pause),('继续识别',resume_inspection),('停止',stop)]:
        ttk.Button(frame,text=label,command=callback).pack(side='left',padx=4,pady=8)
    def poll():
        if active['result'] is not None:
            status.set('运行已结束，记录已保存。');root.after(800,root.destroy);return
        snapshot=controller.control.snapshot()
        if snapshot['state']=='paused':status.set(snapshot.get('note') or '已暂停，可以查看游戏；检查完请点继续。')
        elif snapshot.get('pause_requested'):status.set('正在准备暂停，请等显示“已暂停”后查看游戏。')
        elif snapshot.get('note'):status.set(snapshot['note'])
        else:status.set('运行中。需要查看游戏时，请先暂停。')
        root.after(200,poll)
    def close():
        if thread.is_alive():stop();root.after(200,close)
        else:root.destroy()
    root.protocol('WM_DELETE_WINDOW',close)
    thread.start();root.after(200,poll);root.mainloop()
    return active['result'] or {'status':'stopped','session_root':str(controller.session.root)}


def main(argv=None):
    args=parse_args(argv)
    if args.window:open_window();return 0
    if args.config is None:raise ValueError('请提供刷切配置，或使用 --window 打开独立窗口。')
    config=CutFarmingConfig.from_dict(json.loads(args.config.read_text(encoding='utf-8-sig')))
    if not args.execute:
        references=load_event_references(config)
        print(json.dumps({'status':'dry_run','config':config.public_dict(),'target_events':list(references),'device_actions':0},ensure_ascii=False));return 0
    if args.session_root is None:raise ValueError('执行刷切需要独立记录目录 --session-root。')
    controller=CutFarmingController(CutSession(args.session_root,config))
    result=(run_control_window(controller,template_root=args.template_root,resume=args.resume) if args.control_window
        else controller.run(execute=True,template_root=args.template_root,resume=args.resume))
    print(json.dumps(result,ensure_ascii=False));return 0 if result['status']=='found' or result.get('normal_stop') is True else 2


if __name__=='__main__':raise SystemExit(main())
