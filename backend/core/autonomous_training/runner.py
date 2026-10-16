"""Bounded autonomous start and harvest routes; skill purchases are separate."""
import subprocess
import time
from core.action_recovery import ReobserveRequired, ReobserveBudget, failure_details, receipt_replan
import json
import hashlib
from pathlib import Path

def load_unsubmitted_setup(path, endpoint):
    """Resume a stale pre-start setup using the original selected-trainee receipt."""
    path = Path(path).resolve()
    prior = json.loads(path.read_text(encoding='utf8'))
    records = prior.get('records', [])
    if prior.get('reason') == 'no_safe_action:unknown':
        source = prior.get('last_observation', {})
        png = Path(source.get('screenshot_path', '')).resolve()
        if prior.get('mode') != 'start' or prior.get('start_submitted') is not False or prior.get('start_confirmed') is not False or (records != []) or (source.get('page') != 'unknown') or (source.get('actions') != {}) or (source.get('endpoint') != endpoint) or (png.parent != path.parent / 'plant') or list(png.parent.glob('*-receipt.json')) or (not png.is_file()) or (hashlib.sha256(png.read_bytes()).hexdigest() != source.get('frame_sha256')):
            raise ValueError('unsubmitted_initial_observation_not_verified')
        return dict(required_trainee='大和赤骥', prior_setup=str(path))
    allowed = {'close_notice', 'continue_scenario', 'select_scarlet', 'continue_trainee'}
    final_setup = prior.get('reason') == 'no_safe_action:final_autonomous' and prior.get('last_observation', {}).get('page') == 'final_autonomous' and records and (records[-1].get('action_id') == 'select_autonomous')
    if final_setup:
        allowed |= {'open_training', 'auto_inheritance', 'toggle_borrow', 'confirm_inheritance', 'continue_inheritance', 'auto_support', 'confirm_auto_support', 'continue_support', 'continue_autonomous_tutorial', 'select_autonomous'}
    if prior.get('reason') != 'stale_frame' and (not final_setup) or prior.get('mode') != 'start' or prior.get('start_submitted') is not False or (prior.get('start_confirmed') is not False) or (not final_setup and (prior.get('last_observation', {}).get('page') != 'inheritance' or not records or records[-1].get('action_id') != 'continue_trainee')):
        raise ValueError('unsubmitted_setup_not_verified')
    selected = None
    for record in records:
        receipt_path = Path(record['receipt_path']).resolve()
        if not receipt_path.is_relative_to(path.parent) or record.get('action_id') not in allowed or record.get('performed') is not True or (record.get('endpoint') != endpoint):
            raise ValueError('unsubmitted_setup_not_verified')
        receipt = json.loads(receipt_path.read_text(encoding='utf8'))
        if any((receipt.get(k) != record.get(k) for k in ('action_id', 'performed', 'endpoint', 'source_frame_sha256'))):
            raise ValueError('unsubmitted_setup_receipt_mismatch')
        if record['action_id'] == 'continue_trainee':
            sources = [json.loads(p.read_text(encoding='utf8')) for p in receipt_path.parent.glob('*.json')]
            sources = [s for s in sources if s.get('frame_sha256') == record['source_frame_sha256']]
            if len(sources) != 1:
                raise ValueError('unsubmitted_setup_source_missing')
            source = sources[0]
            png = Path(source['screenshot_path']).resolve()
            if png.parent != receipt_path.parent or hashlib.sha256(png.read_bytes()).hexdigest() != source['frame_sha256'] or source.get('page') != 'trainee' or (source.get('selected_trainee_name') != '大和赤骥'):
                raise ValueError('unsubmitted_setup_trainee_mismatch')
            selected = source['selected_trainee_name']
    if selected is None:
        raise ValueError('unsubmitted_setup_trainee_missing')
    return dict(required_trainee=selected, verified_trainee=selected, prior_setup=str(path))

def load_submission(path, endpoint):
    prior = json.loads(path.read_text(encoding='utf-8'))
    starts = [record for record in prior.get('records', []) if record.get('action_id') == 'start_autonomous']
    if prior.get('kind') != 'autonomous_training' or prior.get('mode') != 'start' or prior.get('start_submitted') is not True or (len(starts) != 1) or (starts[0].get('performed') is not True) or (starts[0].get('endpoint') != endpoint):
        raise ValueError('prior_submission_not_verified')
    context = dict(start_submitted=True, prior_submission=str(path.resolve()), start_confirmed=prior.get('start_confirmed') is True, request_id=starts[0].get('request_id') or str(path.resolve()))
    for receipt_path in path.parent.glob('*-receipt.json'):
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        if receipt.get('action_id') == 'confirm_autonomous_start' and receipt.get('request_id') == context['request_id']:
            context['start_confirmed'] = True
    if starts[0].get('start_cost'):
        context.update(start_cost=starts[0]['start_cost'], start_cost_at=starts[0].get('start_cost_at'))
    else:
        for source_path in path.parent.glob('*.json'):
            source = json.loads(source_path.read_text(encoding='utf-8'))
            if source.get('frame_sha256') != starts[0].get('source_frame_sha256'):
                continue
            png = Path(source.get('screenshot_path', ''))
            if png.resolve().parent != path.parent.resolve() or not png.is_file() or hashlib.sha256(png.read_bytes()).hexdigest() != source['frame_sha256'] or (source.get('page') != 'final_autonomous') or (source.get('autonomous_selected') is not True):
                continue
            context.update(start_cost=dict(before=source.get('tp_before'), after=source.get('tp_after'), cost=source.get('tp_cost')), start_cost_at=source.get('captured_at_unix_ms'))
            break
    return context

def choose_action(mode, observed, context):
    page, actions = (observed['page'], observed['actions'])
    if mode == 'start':
        from core.autonomous_training.plant_plan import choose
        preparation = choose(observed, context)
        if not preparation:
            from core.autonomous_training.plant_support import choose as choose_support
            preparation = choose_support(observed, context)
        if preparation:
            if preparation not in actions:
                raise RuntimeError('no_safe_action:' + preparation)
            return preparation
    if page == 'home':
        action = 'open_training'
    elif page == 'new_scenario':
        action = 'close_notice'
    elif page == 'training_status':
        seconds = observed.get('remaining_seconds')
        if type(seconds) is not int or seconds < 0:
            raise RuntimeError('training_time_unknown')
        if context.get('start_submitted') and seconds > 0:
            action = 'cancel_training_status'
        elif seconds > 0:
            raise RuntimeError('still_running')
        elif mode == 'start':
            raise RuntimeError('harvest_required')
        else:
            action = 'forward_harvest'
    elif mode == 'harvest':
        if page == 'skills':
            raise RuntimeError('skill_plan_required')
        if page in ('history_summary', 'history_races', 'skill_tutorial'):
            from core.autonomous_training.harvest import choose_harvest_action
            return choose_harvest_action(observed, context)
        action = {'history': 'confirm_history', 'finish_ready': 'open_skills'}.get(page)
    elif context.get('start_submitted'):
        if page == 'autonomous_start_notice' and context.get('start_confirmed'):
            raise RuntimeError('start_confirmation_already_submitted')
        action = {'autonomous_start_notice': 'confirm_autonomous_start', 'autonomous_running': 'open_training_menu', 'training_menu': 'return_home'}.get(page)
    elif page == 'scenario':
        action = 'continue_scenario' if 'continue_scenario' in actions else 'scenario_next'
    elif page == 'exam_mode':
        if context.get('exam_selected') and context.get('selected_course_name') and (observed.get('course_name') != context['selected_course_name']):
            raise RuntimeError('selected_course_mismatch')
        action = 'confirm_mode' if context.get('exam_selected') else 'change_exam'
    elif page == 'exam_list':
        if context.get('exam_selected'):
            if 'close_exams' not in actions:
                raise RuntimeError('selected_exam_list_close_missing')
            return 'close_exams'
        rows = observed.get('course_rows', [])
        eligible = [r for r in rows if r.get('status') == '未挑战' and r.get('locked') is False and (r.get('action_id') in actions)]
        if not eligible and observed.get('all_courses_seen') is True:
            if any((r.get('locked') is not True and r.get('course_name') != '自由课程' and (r.get('status') not in ('未挑战', '可', '良', '优')) for r in rows)):
                raise RuntimeError('course_status_unknown')
            eligible = [r for r in rows if r.get('course_name') == '自由课程' and r.get('locked') is False and (r.get('action_id') in actions)]
        if eligible:
            action = eligible[0]['action_id']
        elif 'scroll_exams' in actions and (not observed.get('all_courses_seen')):
            action = 'scroll_exams'
        else:
            raise RuntimeError('no_unchallenged_or_unlocked_free_course')
    elif page == 'exam_detail':
        action = 'close_exam_detail' if observed.get('exam_selection_applied') is True else 'confirm_exam'
    elif page == 'inheritance':
        from core.autonomous_training.plant_inheritance import choose as choose_inheritance
        action = choose_inheritance(observed, context)
    elif page == 'inherit_confirm':
        from core.autonomous_training.plant_inheritance import choose as choose_inheritance
        action = choose_inheritance(observed, context)
    elif page == 'support_deck':
        if type(observed.get('support_full')) is not bool:
            raise RuntimeError('support_completeness_unknown')
        if observed.get('support_full') is not True:
            raise RuntimeError('current_support_incomplete')
        action = 'continue_support'
    elif page == 'trainee' and context.get('required_trainee'):
        if context.get('required_variant'):
            if observed.get('selected_variant') == context['required_variant']:
                action = 'continue_trainee'
            else:
                candidates = sorted((k for k in actions if k.startswith('inspect_trainee_')), key=lambda k: int(k.rsplit('_', 1)[1]))
                index = context.get('variant_card_index', 0)
                scroll = 'scroll_trainees_up' if context.get('variant_scan_up') else 'scroll_trainees'
                if any(('portrait_score' in actions[k] for k in candidates)) and (not context.get('variant_fast_scan_complete')):
                    hints = [k for k in candidates if actions[k].get('portrait_score', 0) >= 0.8 and k not in context.get('variant_fast_inspected', [])]
                    action = max(hints, key=lambda k: actions[k]['portrait_score']) if hints else scroll
                else:
                    action = candidates[index] if index < len(candidates) else scroll
        elif observed.get('selected_trainee_name') == context['required_trainee']:
            action = 'continue_trainee'
        else:
            from core.autonomous_training.plant_catalog import TRAINEES
            selection = {name: 'select_' + key for key, name in TRAINEES.items()}.get(context['required_trainee'])
            if selection is None:
                raise RuntimeError('unsupported_required_trainee')
            action = selection if selection in actions else 'scroll_trainees_up' if context['required_trainee'] != '樱花进王' and context.get('trainee_rewind_count', 0) < 6 else 'scroll_trainees'
    else:
        action = {'exam_detail': 'confirm_exam', 'trainee': 'continue_trainee', 'autonomous_tutorial': 'continue_autonomous_tutorial', 'auto_support_confirm': 'confirm_auto_support', 'final_confirm': 'select_autonomous', 'final_autonomous': 'start_autonomous'}.get(page)
    if not action or action not in actions:
        raise RuntimeError('no_safe_action:' + page)
    return action
NEXT_PAGES = {'open_training': {'training_status', 'new_scenario', 'scenario'}, 'close_notice': {'new_scenario', 'scenario'}, 'forward_harvest': {'history', 'history_summary', 'history_races', 'finish_ready', 'training_rating', 'factor_result'}, 'confirm_history': {'finish_ready'}, 'history_next': {'history_races'}, 'history_scroll': {'history_races'}, 'continue_skill_tutorial': {'skill_tutorial', 'skills'}, 'open_skills': {'skills', 'skill_tutorial'}, 'continue_scenario': {'exam_mode', 'trainee'}, 'scenario_next': {'scenario'}, 'change_exam': {'exam_list'}, 'scroll_exams': {'exam_list'}, 'confirm_exam': {'exam_mode', 'exam_detail'}, 'close_exam_detail': {'exam_list', 'exam_mode'}, 'close_exams': {'exam_mode'}, 'confirm_mode': {'trainee'}, 'continue_trainee': {'inheritance'}, 'select_bakushin': {'trainee'}, 'select_scarlet': {'trainee'}, 'select_gold_ship': {'trainee'}, 'select_vodka': {'trainee'}, 'select_urara': {'trainee'}, 'scroll_trainees_up': {'trainee'}, 'scroll_trainees': {'trainee'}, 'auto_inheritance': {'inherit_confirm'}, 'toggle_borrow': {'inherit_confirm'}, 'confirm_inheritance': {'inheritance'}, 'continue_inheritance': {'support_deck'}, 'auto_support': {'auto_support_confirm'}, 'confirm_auto_support': {'support_deck'}, 'continue_support': {'final_confirm', 'final_autonomous', 'autonomous_tutorial'}, 'select_autonomous': {'final_autonomous', 'autonomous_tutorial'}, 'continue_autonomous_tutorial': {'autonomous_tutorial', 'final_confirm', 'final_autonomous'}, 'start_autonomous': {'autonomous_start_notice', 'autonomous_running', 'training_status'}, 'confirm_autonomous_start': {'autonomous_running', 'training_status'}, 'open_training_menu': {'training_menu', 'autonomous_running'}, 'return_home': {'home', 'training_menu'}, 'cancel_training_status': {'home'}}
NEXT_PAGES.update({'inspect_trainee_' + str(i): {'trainee'} for i in range(15)})
from core.autonomous_training.plant_plan import NEXT_PAGES as PLAN_NEXT_PAGES
NEXT_PAGES.update(PLAN_NEXT_PAGES)
from core.autonomous_training.plant_support import NEXT_PAGES as SUPPORT_NEXT_PAGES, SEARCH_ACTION_LIMITS
NEXT_PAGES.update(SUPPORT_NEXT_PAGES)

class AutonomousRunner:

    def __init__(self, device, emit, stop_event):
        self.device, self.emit, self.stop = (device, emit, stop_event)

    def run(self, mode):
        if mode not in ('start', 'harvest'):
            raise ValueError('invalid_mode')
        report = dict(kind='autonomous_training', mode=mode, ok=False, start_submitted=False, start_confirmed=False, no_refill=True, records=[])
        context = self.device.context
        report.update(start_submitted=bool(context.get('start_submitted')), start_confirmed=bool(context.get('start_confirmed')))
        if context.get('prior_submission'):
            report['prior_submission'] = context['prior_submission']
        context['mode'] = mode
        if mode == 'harvest':
            context['harvest_active'] = True
        counts, previous = ({}, None)
        active_verified = False
        try:
            observed = self.device.capture()
            if mode == 'start':
                for _ in range(8):
                    if observed['page'] not in {'unknown', 'loading'}:
                        break
                    if self.stop.wait(0.5):
                        raise RuntimeError('operator_stop')
                    observed = self.device.capture()
            replans = ReobserveBudget()
            for _ in range(480 if context.get('required_variant') or context.get('plant_options') else 50):
                report['last_observation'] = observed
                if self.stop.is_set():
                    raise RuntimeError('operator_stop')
                if mode == 'start' and context.get('start_submitted') and (observed['page'] == 'training_menu' and 'return_home' not in observed['actions'] or (observed['page'] == 'autonomous_running' and 'open_training_menu' not in observed['actions'])):
                    deadline = time.monotonic() + 20
                    while observed['page'] == 'training_menu' and 'return_home' not in observed['actions'] or (observed['page'] == 'autonomous_running' and 'open_training_menu' not in observed['actions']) or (observed['page'] in ('unknown', 'loading') and (not observed['actions'])):
                        if time.monotonic() >= deadline:
                            if observed['page'] == 'autonomous_running':
                                raise RuntimeError('training_running_menu_not_ready')
                            raise RuntimeError('training_menu_not_ready')
                        if self.stop.wait(0.5):
                            raise RuntimeError('operator_stop')
                        observed = self.device.capture()
                        report['last_observation'] = observed
                if context.get('plant_options'):
                    from core.autonomous_training.plant_plan import reobserve_once
                    observed = reobserve_once(observed, context, self.device.capture)
                    report['last_observation'] = observed
                if mode == 'start' and observed['page'] == 'inherit_confirm' and (type(observed.get('borrow_checked')) is not bool):
                    if self.stop.is_set():
                        raise RuntimeError('operator_stop')
                    observed = self.device.capture()
                    report['last_observation'] = observed
                page = observed['page']
                if previous:
                    allowed = {'exam_detail'} if previous.startswith('course_') else NEXT_PAGES[previous]
                    if page not in allowed:
                        raise RuntimeError('unexpected_transition:' + previous + '->' + page)
                if mode == 'start' and page == 'exam_detail' and (observed.get('exam_selection_applied') is True):
                    if not observed.get('course_name') or observed.get('selected_course_status') != '未挑战':
                        raise RuntimeError('selected_course_not_unchallenged')
                    context.update(exam_selected=True, selected_course_name=observed['course_name'])
                if context.get('start_submitted'):
                    if page == 'training_status' and (observed.get('remaining_seconds') or 0) > 0:
                        active_verified = True
                    if page == 'autonomous_running' and observed.get('autonomous_active') is True:
                        active_verified = True
                        if type(observed.get('remaining_seconds')) is int:
                            report['ready_at'] = time.time() + observed['remaining_seconds']
                    if page == 'home' and active_verified:
                        check = self.device.capture()
                        if check['page'] != 'home':
                            raise RuntimeError('start_or_home_not_verified')
                        report.update(ok=True, reason='started_home_verified', last_observation=check)
                        return report
                action = choose_action(mode, observed, context)
                counts[action] = counts.get(action, 0) + 1
                limit = 12 if action == 'history_scroll' else 6 if action in ('scenario_next', 'scroll_exams', 'scroll_trainees', 'scroll_trainees_up', 'continue_skill_tutorial') else 3
                if context.get('required_variant') and (action.startswith('inspect_trainee_') or action in ('scroll_trainees', 'scroll_trainees_up')):
                    limit = 40
                if action in PLAN_NEXT_PAGES:
                    limit = 32
                if action in SUPPORT_NEXT_PAGES:
                    limit = SEARCH_ACTION_LIMITS.get(action, 24)
                if action == 'plan_next_half':
                    limit = 128
                if action == 'scenario_next' and context.get('plant_options'):
                    limit = 18
                if counts[action] > limit:
                    raise RuntimeError('action_repeat_limit:' + action)
                self.emit(dict(type='progress', phase=page, message=f'自主训练 {mode}: {page} → {action}'))
                try:
                    receipt = self.device.step(action, self.stop)
                except ReobserveRequired as error:
                    observed = replans.accept(error)
                    counts[action] -= 1
                    continue
                if receipt.get('execution_status') == 'executed':
                    replans.executed()
                fresh = receipt_replan(receipt, replans)
                if fresh is not None:
                    observed = fresh
                    counts[action] -= 1
                    continue
                report['records'].append({k: v for k, v in receipt.items() if k != 'observation'})
                if mode == 'harvest' and action == 'forward_harvest' and (receipt.get('observation', {}).get('page') in {'training_rating', 'factor_result'}):
                    from core.autonomous_training.harvest_direct_settlement import bind_forward_settlement
                    bind_forward_settlement(self.device, receipt, self.stop)
                    from core.autonomous_training.harvest import HarvestRunner
                    settled = HarvestRunner(self.device, self.emit, self.stop, preset=context.get('harvest_preset')).run(execute=True, learn_skills=context.get('harvest_learn_skills', False), update_team=context.get('harvest_update_team', False))
                    settled['records'] = [*report['records'], *settled.get('records', [])]
                    settled['device_actions'] = len(report['records']) + settled.get('device_actions', 0)
                    return settled
                if context.get('plant_options'):
                    from core.autonomous_training.plant_plan import after_action, effective_scenario, reobserve_once
                    if self.stop.is_set():
                        raise RuntimeError('operator_stop')
                    receipt['observation'] = reobserve_once(receipt['observation'], context, self.device.capture, action)
                    after_action(action, receipt['observation'], context)
                    if action == 'continue_scenario':
                        context['verified_scenario'] = effective_scenario(context)
                    if context.get('plan_empty') and (not context.get('plan_empty_reported')):
                        self.emit(dict(type='progress', message='账号计划槽位为空，保留当前剧本默认赛程继续'))
                        context['plan_empty_reported'] = True
                if context.get('required_variant'):
                    if action.startswith('inspect_trainee_'):
                        context['variant_card_index'] = context.get('variant_card_index', 0) + 1
                        context.setdefault('variant_fast_inspected', []).append(action)
                    if action in ('scroll_trainees', 'scroll_trainees_up'):
                        context['variant_card_index'] = 0
                        context['variant_fast_inspected'] = []
                        before = observed.get('trainee_list_signature', [])
                        after = receipt['observation'].get('trainee_list_signature', [])
                        if before and len(before) == len(after) and (sum((abs(a - b) for a, b in zip(before, after))) / len(before) < 2):
                            if context.get('variant_scan_up'):
                                if not context.get('variant_fast_scan_complete'):
                                    context['variant_fast_scan_complete'] = True
                                    context['variant_scan_up'] = False
                                else:
                                    raise RuntimeError('requested_trainee_or_outfit_not_found')
                            else:
                                context['variant_scan_up'] = True
                elif context.get('required_trainee') and action in ('scroll_trainees', 'scroll_trainees_up'):
                    before = observed.get('trainee_list_signature', [])
                    after = receipt['observation'].get('trainee_list_signature', [])
                    if before and len(before) == len(after) and (sum((abs(a - b) for a, b in zip(before, after))) / len(before) < 2):
                        if action == 'scroll_trainees_up':
                            context['trainee_rewind_count'] = 6
                        else:
                            raise RuntimeError('requested_trainee_not_found')
                if action == 'scroll_trainees_up':
                    context['trainee_rewind_count'] = context.get('trainee_rewind_count', 0) + 1
                if action == 'confirm_exam':
                    context['exam_selected'] = True
                if action == 'continue_trainee' and context.get('required_trainee'):
                    context['verified_trainee'] = observed.get('selected_trainee_name')
                    if context.get('required_variant'):
                        context['verified_variant'] = observed.get('selected_variant')
                if action == 'start_autonomous':
                    context['start_submitted'] = True
                if action == 'confirm_autonomous_start':
                    context['start_confirmed'] = True
                    report['started_at'] = time.time()
                report['start_submitted'] = bool(context.get('start_submitted'))
                report['start_confirmed'] = bool(context.get('start_confirmed'))
                observed, previous = (receipt['observation'], action)
            raise RuntimeError('autonomous_step_limit')
        except (RuntimeError, subprocess.SubprocessError, OSError) as error:
            report.update(failure_details(error))
            reason = str(error) if isinstance(error, RuntimeError) else 'device_transport_failed'
            report.update(reason=reason, start_submitted=bool(context.get('start_submitted')), start_confirmed=bool(context.get('start_confirmed')))
            return report
