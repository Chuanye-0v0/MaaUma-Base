"""Fresh-frame action executor restricted to the currently authorized test device."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import json
import hashlib
from pathlib import Path
import threading
import time
import cv2
import numpy as np
from core.image_io import read_image
from core.skill_scroll import SKILL_OVERLAP_SWIPE, skill_overlap_shell, skill_rewind_shell, skill_list_frames_match
from core.support_library.live import SupportDevice
from core.observation_timing import observation_is_stale
from core.action_recovery import ReobserveRequired, action_boundary, await_candidate
from core.autonomous_training.runner import EVENT_BOOST_FINAL_PAGES, event_boost_change_required
from core.autonomous_training.finish import finish_control_visible
from core.autonomous_training.harvest import HARVEST_ACTION_PAGES, REPEATABLE_RESULT_ACTIONS, authorize_harvest_action, mark_harvest_dispatch, NEXT_PAGES, RESULT_ACTIONS
ACTION_PAGES = {'leave_empty_scenario': 'scenario', 'open_training': 'home', 'close_notice': 'new_scenario', 'forward_harvest': 'training_status', 'cancel_training_status': 'training_status', 'confirm_history': 'history', 'open_skills': 'finish_ready', 'finish_training': 'finish_ready', 'scroll_skill_list': 'skills', 'scroll_skill_reveal_bottom': 'skills', 'scroll_skill_up': 'skills', 'scroll_skill_overlap': 'skills', 'reset_skill_selection': 'skills', 'open_skill_confirmation': 'skills', 'cancel_skill_confirmation': 'skill_confirmation', 'scroll_skill_confirmation': 'skill_confirmation', 'rewind_skill_confirmation': 'skill_confirmation', 'return_harvest_skills': 'skills', 'continue_scenario': 'scenario', 'scenario_next': 'scenario', 'change_exam': 'exam_mode', 'confirm_mode': 'exam_mode', 'scroll_exams': 'exam_list', 'close_exam_detail': 'exam_detail', 'close_exams': 'exam_list', 'confirm_exam': 'exam_detail', 'continue_trainee': 'trainee', 'select_bakushin': 'trainee', 'select_scarlet': 'trainee', 'scroll_trainees': 'trainee', 'select_gold_ship': 'trainee', 'select_vodka': 'trainee', 'select_urara': 'trainee', 'scroll_trainees_up': 'trainee', 'auto_inheritance': 'inheritance', 'continue_inheritance': 'inheritance', 'toggle_borrow': 'inherit_confirm', 'confirm_inheritance': 'inherit_confirm', 'auto_support': 'support_deck', 'continue_support': 'support_deck', 'confirm_auto_support': 'auto_support_confirm', 'select_autonomous': 'final_confirm', 'toggle_event_boost': EVENT_BOOST_FINAL_PAGES, 'continue_autonomous_tutorial': 'autonomous_tutorial', 'start_autonomous': 'final_autonomous', 'open_training_menu': 'autonomous_running', 'confirm_autonomous_start': 'autonomous_start_notice', 'cancel_autonomous_start': 'autonomous_start_notice', 'return_home': 'training_menu', 'return_title_after_disconnect': 'connection_interrupted'}
HARVEST_ACTIONS = {'open_training', 'forward_harvest', 'confirm_history', 'open_skills', 'finish_training', 'scroll_skill_list', 'scroll_skill_reveal_bottom', 'scroll_skill_up', 'reset_skill_selection', 'open_skill_confirmation', 'cancel_skill_confirmation', 'return_title_after_disconnect'}
HARVEST_ACTIONS.add('return_harvest_skills')
HARVEST_ACTIONS.add('scroll_skill_overlap')
HARVEST_ACTIONS.add('scroll_skill_confirmation')
HARVEST_ACTIONS.add('rewind_skill_confirmation')
HARVEST_ACTIONS.add('close_notice')
HARVEST_ACTIONS.add('leave_empty_scenario')
ACTION_PAGES.update(HARVEST_ACTION_PAGES)
from core.autonomous_training.plant_plan import ACTION_PAGES as PLAN_ACTION_PAGES
ACTION_PAGES.update(PLAN_ACTION_PAGES)
from core.autonomous_training import plant_support
from core.autonomous_training import plant_inheritance
ACTION_PAGES.update(plant_support.ACTION_PAGES)
ACTION_PAGES.update({'inspect_trainee_' + str(i): 'trainee' for i in range(15)})
HARVEST_ACTIONS.update(HARVEST_ACTION_PAGES)
START_ACTIONS = set(ACTION_PAGES) - (HARVEST_ACTIONS - {'open_training', 'close_notice'})
FAST_NAVIGATION = {'return_home', 'cancel_training_status', 'close_exam_detail', 'close_exams', 'close_trained_detail', 'formation_home'}
SKILL_SCROLL_BOUNDARIES = {'scroll_skill_up': 'skill_list_at_top', 'scroll_skill_list': 'skill_list_at_bottom', 'scroll_skill_reveal_bottom': 'skill_list_at_bottom', 'scroll_skill_overlap': 'skill_list_at_bottom'}
MAX_UNKNOWN_SKILL_SCROLLS = 2

def _resume_start_allowed(context):
    return context.get('resume_start_confirmation') is True and context.get('resume_start_cancelled') is True and (context.get('start_submitted') is True) and (not context.get('start_confirmed')) and (not context.get('resume_start_open_attempted'))

def _authorize_skill_scroll(action, observed, context):
    boundary = SKILL_SCROLL_BOUNDARIES.get(action)
    if boundary is None:
        return
    state = observed.get(boundary)
    if state is True or (state is not None and state is not False):
        raise RuntimeError('skill_list_top_not_verified' if boundary.endswith('top') else 'skill_list_bottom_not_verified')
    if state is None:
        if observed.get('skill_list_navigation_verified') is not True:
            raise RuntimeError('skill_scroll_page_not_verified')
        attempts = context.get('skill_scroll_unknown_attempts', {})
        count = attempts.get(boundary, 0) if isinstance(attempts, dict) else MAX_UNKNOWN_SKILL_SCROLLS
        if type(count) is not int or not 0 <= count < MAX_UNKNOWN_SKILL_SCROLLS:
            raise RuntimeError('skill_scroll_unknown_budget_exhausted')

def transition_pending(action, before, after):
    if action == 'open_training' and after['page'] == 'home':
        return True
    if action == 'confirm_inheritance' and after['page'] == 'inheritance' and (after.get('inheritance_full') is not True):
        return True
    if action == 'toggle_borrow' and after['page'] == 'inherit_confirm':
        value = before.get('borrow_checked')
        return type(value) is not bool or after.get('borrow_checked') is not (not value)
    if action == 'toggle_event_boost' and after['page'] in EVENT_BOOST_FINAL_PAGES:
        return after.get('event_boost_available') is True and type(after.get('event_boost_enabled')) is bool and (after['event_boost_enabled'] is before.get('event_boost_enabled'))
    if action in plant_support.ACTION_PAGES:
        if after['page'] in ('unknown', 'loading'):
            return True
        if after['page'] == 'support_friend' and (not after.get('support_rows')):
            return True
        if action in ('support_prev', 'support_next') and after['page'] == 'support_deck':
            expected = (before['support_deck_index'] - 1 + (1 if action == 'support_next' else -1)) % 10 + 1
            return after.get('support_deck_index') != expected
        if action == 'support_filter_reset' and after['page'] == 'support_filter':
            return set(after.get('support_filters', {})) != set(plant_support.FILTER_LABELS) or any((v is not False for v in after['support_filters'].values()))
        if action.startswith('support_filter_') and action.removeprefix('support_filter_').isdigit() and (after['page'] == 'support_filter'):
            label = before['actions'][action]['label']
            return after.get('support_filters', {}).get(label) is not (not before['support_filters'][label])
        return after['page'] not in plant_support.NEXT_PAGES[action]
    if action == 'plan_next_half' and after['page'] == 'plan_races':
        return after.get('plan_date') != before['actions'][action]['next_date']
    if action == 'plan_deselect_race' and after['page'] == 'plan_races':
        return after.get('plan_has_selection', True)
    if action == 'plan_select_race' and after['page'] == 'plan_races':
        return not after.get('plan_race_selected')
    if action.startswith('plan_year_') and after['page'] == 'plan_calendar':
        return after.get('plan_year') != int(action.rsplit('_', 1)[1])
    if action in PLAN_ACTION_PAGES and after['page'] == before['page']:
        return False
    if action.startswith('inspect_trainee_') and after['page'] == 'trainee':
        return False
    if action in FAST_NAVIGATION and after['page'] == before['page']:
        return True
    if action in ('open_skills', 'return_harvest_skills', 'formation_home', 'close_completed_training') and after['page'] == before['page']:
        return True
    if after['page'] in NEXT_PAGES.get(action, ()) and (after['page'] in RESULT_ACTIONS or after['page'] == 'team_rank_reward') and (not after['actions']):
        return True
    if action == 'complete_training' and after['page'] in ('finish_ready', 'finish_confirm'):
        return True
    if action == 'complete_training' and after['page'] == 'training_rating' and ('confirm_training_rating' not in after['actions']):
        return True
    if action == 'forward_harvest':
        if after['page'] in {'home', 'history', 'training_status'}:
            return True
        if after['page'] in {'history_summary', 'history_races', 'finish_ready'} and (not after['actions']):
            return True
    if action == 'confirm_history' and after['page'] in ('history', 'history_races'):
        return True
    if action == 'return_home' and after['page'] in ('autonomous_running', 'training_menu'):
        return True
    if action == 'return_home' and after['page'] == 'training_status' and (not after['actions']):
        return True
    if action == 'confirm_autonomous_start' and after['page'] in ('support_deck', 'final_confirm', 'final_autonomous'):
        return True
    return after['page'] in ('unknown', 'loading') or (after['page'] == before['page'] and action in after['actions'] and (action not in REPEATABLE_RESULT_ACTIONS) and (action not in ('scroll_skill_list', 'scroll_skill_reveal_bottom', 'scroll_skill_up', 'scroll_skill_overlap', 'scroll_exams', 'scroll_trainees', 'scroll_trainees_up', 'scenario_next', 'toggle_borrow', 'close_notice', 'reset_skill_selection', 'history_scroll', 'continue_skill_tutorial', 'continue_autonomous_tutorial')))

def authorize_action(action, observed, context):
    from core.autonomous_training.harvest_direct_settlement import ensure_settlement_scope
    ensure_settlement_scope(observed, context, action)
    plant_support.authorize(action, observed, context)
    if action in ('auto_support', 'confirm_auto_support'):
        raise RuntimeError('support_auto_compose_not_requested')
    plant_inheritance.authorize(action, observed, context)
    if action == 'leave_empty_scenario' and (context.get('empty_harvest') is not True or context.get('start_submitted')):
        raise RuntimeError('empty_harvest_return_not_bound')
    if action in PLAN_ACTION_PAGES:
        if 'plant_options' not in context or context.get('start_submitted'):
            raise RuntimeError('plan_preparation_not_authorized')
        if action == 'plan_accept_overwrite' and (not context['plant_options']['overwrite_plan']):
            raise RuntimeError('plan_overwrite_not_authorized')
        if action == 'plan_accept_overwrite' and observed.get('plan_overwrite_slot') != context['plant_options']['save_slot']:
            raise RuntimeError('plan_overwrite_slot_not_verified')
        if action == 'plan_next_half' and observed.get('actions', {}).get(action, {}).get('next_date') != context.get('plan_expected_date'):
            raise RuntimeError('plan_date_transition_not_verified')
        if action == 'plan_deselect_race' and (observed.get('plan_race_selected') or observed.get('plan_date_mandatory') or 'plan_select_race' not in observed.get('actions', {})):
            raise RuntimeError('plan_deselect_not_authorized')
    if action == 'continue_scenario' and 'plant_options' in context:
        from core.autonomous_training.plant_plan import effective_scenario
        if observed.get('scenario_id') != effective_scenario(context):
            raise RuntimeError('required_scenario_not_selected')
    if action.startswith('inspect_trainee_'):
        if not context.get('required_variant') or observed.get('actions', {}).get(action, {}).get('variant_key') != context['required_variant']:
            raise RuntimeError('required_variant_not_bound')
    if action == 'continue_trainee' and context.get('required_variant'):
        if observed.get('selected_variant') != context['required_variant']:
            raise RuntimeError('required_variant_not_selected')
    if action in ('select_gold_ship', 'select_vodka', 'select_urara'):
        from core.autonomous_training.plant_catalog import trainee_name
        name = trainee_name(action.removeprefix('select_'))
        if context.get('required_trainee') != name or observed.get('actions', {}).get(action, {}).get('trainee_name') != name:
            raise RuntimeError('required_trainee_not_bound')
    if action == 'select_scarlet':
        if context.get('required_trainee') != '大和赤骥' or observed.get('actions', {}).get(action, {}).get('trainee_name') != '大和赤骥':
            raise RuntimeError('required_trainee_not_bound')
    if action == 'select_bakushin':
        from core.autonomous_training.trainee import REQUIRED_TRAINEE
        if context.get('required_trainee') != REQUIRED_TRAINEE or observed.get('actions', {}).get(action, {}).get('trainee_name') != REQUIRED_TRAINEE:
            raise RuntimeError('required_trainee_not_bound')
    if action == 'continue_trainee' and context.get('required_trainee'):
        if observed.get('selected_trainee_name') != context['required_trainee']:
            raise RuntimeError('required_trainee_not_selected')
    if action in HARVEST_ACTION_PAGES or (action == 'confirm_history' and observed.get('page') == 'history_races'):
        authorize_harvest_action(action, observed, context)
        return
    expected = 'exam_list' if action.startswith('course_') else ACTION_PAGES.get(action)
    if not expected or observed['page'] not in (expected if isinstance(expected, tuple) else (expected,)):
        raise RuntimeError('autonomous_action_forbidden')
    mode = context.get('mode')
    if mode not in ('start', 'harvest') or (mode == 'harvest' and action not in HARVEST_ACTIONS):
        raise RuntimeError('action_mode_forbidden')
    if mode == 'start' and action not in START_ACTIONS and (not action.startswith('course_')):
        raise RuntimeError('action_mode_forbidden')
    if action == 'toggle_event_boost' and (not event_boost_change_required(observed, context)):
        raise RuntimeError('event_boost_adjustment_not_required')
    if action in ('select_autonomous', 'start_autonomous') and event_boost_change_required(observed, context):
        raise RuntimeError('event_boost_adjustment_required')
    if action == 'forward_harvest' and (mode != 'harvest' or observed.get('remaining_seconds') != 0):
        raise RuntimeError('training_not_finished')
    if action == 'return_harvest_skills':
        from core.autonomous_training.harvest_skills import authorize_no_purchase_return
        if context.get('harvest_active') is not True:
            raise RuntimeError('harvest_chain_not_requested')
        if context.get('harvest_low_pt_return'):
            from core.autonomous_training.low_pt_resume import authorize_low_pt_return
            authorize_low_pt_return(observed, context)
        else:
            authorize_no_purchase_return(observed, context)
    _authorize_skill_scroll(action, observed, context)
    if action in ('open_skill_confirmation', 'cancel_skill_confirmation') and context.get('confirmation_preview_only') is not True:
        raise RuntimeError('skill_confirmation_preview_not_requested')
    if action in ('scroll_skill_confirmation', 'rewind_skill_confirmation') and context.get('confirmation_scan') is not True:
        raise RuntimeError('skill_confirmation_scan_not_requested')
    if action == 'reset_skill_selection':
        recovery = context.get('skill_cart_recovery') or {}
        initial, cost = (recovery.get('initial_points'), recovery.get('selected_cost'))
        checkpoint_sha = recovery.get('checkpoint_sha256')
        if any((type(value) is not int for value in (initial, cost))) or not 0 < cost <= initial or type(observed.get('current_skill_points')) is not int or (observed['current_skill_points'] != initial - cost) or (not isinstance(checkpoint_sha, str)) or (len(checkpoint_sha) != 64) or any((char not in '0123456789abcdef' for char in checkpoint_sha)):
            raise RuntimeError('skill_cart_reset_not_bound')
    if action == 'continue_inheritance' and observed.get('inheritance_full') is not True:
        raise RuntimeError('inheritance_not_full')
    if action == 'continue_support' and observed.get('support_full') is not True:
        raise RuntimeError('support_not_full')
    if action == 'close_exam_detail' and (not (observed.get('exam_selection_applied') is True and observed.get('selected_course_status') == '未挑战')):
        raise RuntimeError('selected_exam_not_verified')
    if action == 'close_exams' and (not context.get('exam_selected')):
        raise RuntimeError('exam_close_not_bound')
    if action == 'confirm_mode' and (not context.get('exam_selected') or (context.get('selected_course_name') and observed.get('course_name') != context['selected_course_name'])):
        raise RuntimeError('selected_course_mismatch')
    if action == 'start_autonomous':
        from core.factor_goals import check_training_tp
        check_training_tp(observed.get('tp_cost'))
        if 'plant_options' in context:
            if context.get('plan_switching') or context.get('verified_scenario') != context['plant_options']['scenario'] or observed.get('scenario_id') != context['plant_options']['scenario'] or (context['plant_options']['plan_source'] != 'default' and (not context.get('plan_done'))):
                raise RuntimeError('plant_preparation_incomplete')
        if context.get('required_variant') and context.get('verified_variant') != context['required_variant']:
            raise RuntimeError('required_variant_chain_unverified')
        if context.get('required_trainee') and context.get('verified_trainee') != context['required_trainee']:
            raise RuntimeError('required_trainee_chain_unverified')
        before, after, cost = (observed.get(k) for k in ('tp_before', 'tp_after', 'tp_cost'))
        if context.get('start_submitted') and (not _resume_start_allowed(context)) or mode != 'start' or observed.get('autonomous_selected') is not True or any((type(n) is not int for n in (before, after, cost))) or (not 0 < cost <= before) or (after < 0) or (before - after != cost):
            raise RuntimeError('start_mode_or_tp_invalid_or_already_submitted')
    if action == 'cancel_autonomous_start':
        if context.get('resume_start_confirmation') is not True or context.get('start_submitted') is not True or context.get('start_confirmed'):
            raise RuntimeError('start_notice_cancel_not_bound')
    if action == 'confirm_autonomous_start':
        budget = context.get('start_cost') or {}
        before, after, cost = (budget.get(k) for k in ('before', 'after', 'cost'))
        captured = context.get('start_cost_at')
        if not context.get('start_submitted') or context.get('start_confirmed') or any((type(n) is not int for n in (before, after, cost, captured))) or (not 0 < cost <= before) or (after < 0) or (before - after != cost) or (not 0 <= int(time.time() * 1000) - captured <= 30 * 60 * 1000):
            raise RuntimeError('start_notice_request_not_bound_or_already_confirmed')
    if action in ('return_home', 'open_training_menu') and (not context.get('start_submitted')):
        raise RuntimeError('autonomous_return_not_bound')
    if action == 'cancel_training_status':
        seconds = observed.get('remaining_seconds')
        preserve = mode == 'start' and context.get('preserve_existing_training') is True and (not context.get('start_submitted'))
        if not (type(seconds) is int and (context.get('start_submitted') and seconds > 0 or (context.get('restart_return_only') is True and context.get('start_submitted') is True and (context.get('start_confirmed') is True) and (seconds >= 0)) or (preserve and seconds >= 0))):
            raise RuntimeError('autonomous_return_not_bound')
    if action == 'return_title_after_disconnect':
        recovery = context.get('disconnect_recovery')
        values = (recovery.get('child_plan_sha256'), recovery.get('final_confirmation_report_sha256')) if isinstance(recovery, dict) else ()
        if len(values) != 2 or any((not isinstance(value, str) or len(value) != 64 or any((char not in '0123456789abcdef' for char in value)) for value in values)):
            raise RuntimeError('disconnect_recovery_not_bound')
    if action == 'finish_training':
        submission = context.get('finish_submission')
        chain_kind = submission.get('finish_chain_kind') if isinstance(submission, dict) else None
        chain_keys = ('skip_skills_observation_sha256', 'finish_ready_observation_sha256') if chain_kind == 'skip_skills' else ('no_purchase_evidence_sha256', 'no_purchase_return_report_sha256') if chain_kind == 'no_purchase' else ('child_plan_sha256', 'return_after_learning_report_sha256')
        values = tuple((submission.get(key) for key in (*chain_keys, 'finish_ready_frame_sha256'))) if isinstance(submission, dict) else ()
        no_purchase_return = context.get('harvest_no_purchase_return') or {}
        intent_path = Path(submission['intent_path']) if isinstance(submission, dict) and isinstance(submission.get('intent_path'), str) else None
        try:
            intent = json.loads(intent_path.read_text(encoding='utf-8')) if intent_path is not None else None
            intent_sha = hashlib.sha256(intent_path.read_bytes()).hexdigest() if intent_path is not None else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            intent, intent_sha = (None, None)
        if context.get('finish_submitted') or len(values) != 3 or chain_kind not in (None, 'purchase', 'no_purchase', 'skip_skills') or (chain_kind == 'skip_skills' and (context.get('harvest_active') is not True or context.get('harvest_learn_skills') is not False or observed.get('endpoint') != _public_endpoint() or (observed.get('current_skill_points') != submission.get('accepted_remaining_skill_points')))) or (chain_kind == 'no_purchase' and (context.get('harvest_active') is not True or no_purchase_return.get('submitted') is not True or no_purchase_return.get('evidence_sha256') != submission.get('no_purchase_evidence_sha256') or (observed.get('current_skill_points') != submission.get('accepted_remaining_skill_points')))) or (not isinstance(submission.get('intent_path'), str)) or (not submission['intent_path']) or (submission.get('intent_sha256') != intent_sha) or (not isinstance(intent, dict)) or (intent.get('schema_version') != 'uma.autonomous_finish_entry_intent.v1') or (intent.get('status') != 'dispatch_intent') or (intent.get('action_id') != 'finish_training') or (intent.get('skip_skills') is not True if chain_kind == 'skip_skills' else intent.get('recommended_skills_complete') is not True) or (intent.get('discard_remaining_skill_points_accepted') is not True) or any((intent.get(key) != submission.get(key) for key in (*chain_keys, 'finish_chain_kind', 'finish_ready_observation_sha256', 'finish_ready_frame_sha256', 'accepted_remaining_skill_points', 'plan_id', 'session_id'))) or any((not isinstance(value, str) or len(value) != 64 or any((char not in '0123456789abcdef' for char in value)) for value in values)) or (type(submission.get('accepted_remaining_skill_points')) is not int) or (not finish_control_visible(observed)):
            raise RuntimeError('finish_submission_not_bound')
    if action.startswith('course_'):
        rows = [r for r in observed.get('course_rows', []) if r.get('action_id') == action]
        free_statuses_known = all((r.get('locked') is True or r.get('course_name') == '自由课程' or r.get('status') in ('未挑战', '可', '良', '优') for r in observed.get('course_rows', [])))
        if len(rows) != 1 or rows[0].get('locked') is not False or (not (rows[0].get('status') == '未挑战' or (rows[0].get('course_name') == '自由课程' and observed.get('all_courses_seen') is True and free_statuses_known))):
            raise RuntimeError('course_not_eligible')

class AutonomousDevice(SupportDevice):

    def __init__(self, endpoint, adb_path, output_dir):
        if endpoint != _public_endpoint():
            raise ValueError('endpoint_not_authorized_for_single_account_test')
        self.context = {}
        super().__init__(endpoint, adb_path, output_dir)

    def capture(self, *, skill_scroll_only=False, start_notice_only=False):
        from core.autonomous_training.observer import observe, read_tokens, read_skill_scroll_tokens, read_start_notice_tokens
        for _ in range(2):
            raw = self.command('-s', self.endpoint, 'exec-out', 'screencap', '-p')
            captured = time.monotonic()
            captured_at = int(time.time() * 1000)
            image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                break
        else:
            raise RuntimeError('screenshot_invalid')
        reader = read_start_notice_tokens if start_notice_only else read_skill_scroll_tokens if skill_scroll_only else read_tokens
        tokens = reader(image, self.ocr)
        required_variant = getattr(self, 'context', {}).get('required_variant')
        observed = observe(raw, tokens, required_variant) if required_variant else observe(raw, tokens)
        if start_notice_only and (observed['page'] != 'autonomous_start_notice' or not {'confirm_autonomous_start', 'cancel_autonomous_start'}.intersection(observed['actions'])):
            tokens = read_tokens(image, self.ocr)
            observed = observe(raw, tokens, required_variant) if required_variant else observe(raw, tokens)
        from core.autonomous_training.plant_plan import enrich
        enrich(observed, image, tokens, getattr(self, 'context', {}))
        plant_support.enrich(observed, image, tokens, getattr(self, 'context', {}), self.ocr)
        if (self.context.get('harvest_preset') or {}).get('factor_profile'):
            from core.harvest_presets import catalog
            from core.harvest_factors import observe_factor_controls
            if not hasattr(self, '_harvest_factor_catalog'):
                self._harvest_factor_catalog = catalog()
            observe_factor_controls(image, tokens, observed, self.context, self._harvest_factor_catalog)
        from core.autonomous_training.harvest_direct_settlement import completion_is_bound
        completed = completion_is_bound(self.context, self.endpoint)
        if observed['page'] == 'score_record' and completed and (self.context.get('factor_choice_submitted') is True) and (not self.context.get('formation_requested')) and (set(observed['actions']) == {'continue_score_record'}):
            observed.update(page='settlement_navigation', actions={'advance_settlement': observed['actions']['continue_score_record']})
        if observed['page'] == 'unknown' and completed and (self.context.get('factor_choice_submitted') is True) and (not self.context.get('formation_requested')):
            from core.autonomous_training.harvest_observer import observe_harvest
            navigation = observe_harvest(raw, tokens, allow_navigation=True)
            if navigation is not None:
                observed.update(navigation)
        if skill_scroll_only:
            observed['actions'] = {key: value for key, value in observed['actions'].items() if key in ('scroll_skill_list', 'scroll_skill_reveal_bottom', 'scroll_skill_up', 'scroll_skill_overlap')}
        stem = str(time.time_ns())
        path = self.output_dir / (stem + '.png')
        path.write_bytes(raw)
        observed.update(screenshot_path=str(path), captured_monotonic=captured, endpoint=self.endpoint, captured_at_unix_ms=captured_at, expires_at_unix_ms=captured_at + 30 * 60 * 1000)
        (self.output_dir / (stem + '.json')).write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding='utf-8')
        return observed

    def capture_skill_scroll(self):
        return self.capture(skill_scroll_only=True)

    def capture_start_notice(self):
        return self.capture(start_notice_only=True)

    @action_boundary
    def step(self, action_id, stop_event=None):
        stop = stop_event or threading.Event()
        if stop.is_set():
            raise RuntimeError('operator_stop')
        if action_id in ('open_training_menu', 'return_home') and (not self.context.get('start_submitted')):
            raise RuntimeError('autonomous_return_not_bound')
        if action_id == 'start_autonomous' and self.context.get('start_submitted') and (not _resume_start_allowed(self.context)):
            raise RuntimeError('start_mode_or_tp_invalid_or_already_submitted')
        if action_id == 'toggle_event_boost' and (self.context.get('start_submitted') or self.context.get('start_confirmed')):
            raise RuntimeError('event_boost_start_already_submitted')
        from core.skill_ocr_performance import active_harvest_ocr
        scroll_only = active_harvest_ocr() is not None and action_id in ('scroll_skill_list', 'scroll_skill_reveal_bottom', 'scroll_skill_up', 'scroll_skill_overlap')
        capture = self.capture_start_notice if action_id in ('confirm_autonomous_start', 'cancel_autonomous_start') else self.capture_skill_scroll if scroll_only else self.capture
        before = capture()
        for _ in range(6):
            waiting_for_reward = action_id == 'continue_team_rank_reward' and before['page'] == 'team_rank_reward' and (not before['actions'])
            waiting_for_skill_entry = action_id == 'open_skills' and self.context.get('harvest_active') is True and (self.context.get('harvest_summary') is not None) and (before['page'] in ('unknown', 'loading'))
            waiting_for_result_control = action_id in RESULT_ACTIONS.values() and self.context.get('harvest_active') is True and (before['page'] in (HARVEST_ACTION_PAGES[action_id], 'unknown', 'loading')) and (action_id not in before['actions'])
            waiting_for_running_menu = action_id == 'open_training_menu' and self.context.get('start_submitted') is True and (before['page'] in ('autonomous_running', 'training_status', 'unknown', 'loading')) and (not before['actions'])
            waiting_for_formation_home = action_id == 'formation_home' and self.context.get('formation_save_confirmed') is True and (before['page'] == 'team_formation') and (action_id not in before['actions'])
            waiting_for_home = action_id == 'open_training' and before['page'] in {'home', 'unknown', 'loading'} and (action_id not in before['actions'])
            waiting_for_urara = action_id == 'select_urara' and before['page'] == 'trainee' and (action_id not in before['actions']) and (before.get('selected_trainee_name') != self.context.get('required_trainee')) and bool(self.context.get('required_trainee'))
            if not (waiting_for_reward or waiting_for_skill_entry or waiting_for_result_control or waiting_for_running_menu or waiting_for_formation_home or waiting_for_home or waiting_for_urara):
                break
            if stop.wait(0.5):
                raise RuntimeError('operator_stop')
            before = capture()
        for _ in range(2):
            if action_id not in before['actions']:
                break
            if not observation_is_stale(before, action_id):
                break
            if stop.is_set():
                raise RuntimeError('operator_stop')
            before = capture()
        if action_id in ('open_training_menu', 'return_home') and self.context.get('mode') == 'start' and (self.context.get('start_submitted') is True):
            deadline = time.monotonic() + 20
            while before['page'] in ('unknown', 'loading') and (not before['actions']) or (action_id == 'open_training_menu' and before['page'] == 'autonomous_running' and (action_id not in before['actions'])) or (action_id == 'return_home' and before['page'] == 'training_menu' and (action_id not in before['actions'])):
                if time.monotonic() >= deadline:
                    if before['page'] == 'training_menu':
                        raise RuntimeError('training_menu_not_ready')
                    if before['page'] == 'autonomous_running':
                        raise RuntimeError('training_running_menu_not_ready')
                    raise RuntimeError('action_not_allowed:' + action_id)
                if stop.wait(0.3):
                    raise RuntimeError('operator_stop')
                before = capture()
            destination = 'training_menu' if action_id == 'open_training_menu' else 'home'
            if before['page'] == destination:
                return dict(action_id=action_id, performed=False, status='navigation_arrived', endpoint=self.endpoint, source_frame_sha256=before['frame_sha256'], observation=before)
        if (action_id.startswith('inspect_trainee_') or action_id in ('scroll_trainees', 'scroll_trainees_up')) and self.context.get('required_variant') and (before.get('selected_variant') == self.context['required_variant']):
            return dict(action_id=action_id, performed=False, status='target_identified', endpoint=self.endpoint, source_frame_sha256=before['frame_sha256'], observation=before)
        if action_id.startswith('select_') and before.get('page') == 'trainee' and self.context.get('required_trainee') and (before.get('selected_trainee_name') == self.context['required_trainee']):
            return dict(action_id=action_id, performed=False, status='target_identified', endpoint=self.endpoint, source_frame_sha256=before['frame_sha256'], observation=before)
        if action_id in ('support_scroll', 'support_refresh') and before.get('page') == 'support_friend' and self.context.get('support_options') and (plant_support.choose(before, self.context) != action_id):
            return dict(action_id=action_id, performed=False, status='support_search_changed', endpoint=self.endpoint, source_frame_sha256=before['frame_sha256'], observation=before)
        if action_id == 'toggle_borrow' and before.get('page') == 'inherit_confirm' and (plant_inheritance.choose(before, self.context) == 'confirm_inheritance'):
            return dict(action_id=action_id, performed=False, status='inheritance_borrow_ready', endpoint=self.endpoint, source_frame_sha256=before['frame_sha256'], observation=before)
        if action_id == 'toggle_event_boost' and before.get('page') in EVENT_BOOST_FINAL_PAGES and (not event_boost_change_required(before, self.context)):
            raise ReobserveRequired(action_id, before)
        if action_id in ('select_autonomous', 'start_autonomous') and event_boost_change_required(before, self.context):
            raise ReobserveRequired(action_id, before)
        if action_id not in before['actions']:
            before = await_candidate(capture, stop, action_id, before, 'exam_list' if action_id.startswith('course_') else before['page'] if action_id == 'toggle_event_boost' else ACTION_PAGES.get(action_id), reason='action_not_allowed:' + action_id)
        candidate = before['actions'].get(action_id)
        if candidate is None:
            raise RuntimeError('action_not_allowed:' + action_id)
        authorize_action(action_id, before, self.context)
        if observation_is_stale(before, action_id):
            raise RuntimeError('stale_frame')
        if stop.is_set():
            raise RuntimeError('operator_stop')
        if action_id == 'auto_inheritance':
            self.context['inheritance_requested'] = True
        if action_id == 'auto_support':
            self.context['support_requested'] = True
        if action_id == 'confirm_inheritance':
            self.context['inheritance_confirmation_submitted'] = True
        if action_id == 'toggle_borrow':
            plant_inheritance.mark_borrow_toggle(before, self.context)
        if action_id == 'confirm_auto_support':
            self.context.pop('support_requested', None)
        if self.context.get('support_options'):
            if action_id in plant_support.ACTION_PAGES or action_id.startswith('plan_back_') or action_id == 'plan_cancel_final':
                self.context.pop('verified_support', None)
            if action_id == 'support_settings':
                self.context['support_filter_open'] = True
            if action_id == 'continue_scenario':
                for key in ('support_selected', 'support_filters_applied', 'support_filter_reset', 'support_filter_open', 'support_origin_deck', 'support_scrolls', 'support_refreshes', 'verified_support'):
                    self.context.pop(key, None)
        if action_id == 'start_autonomous':
            resumed_start = _resume_start_allowed(self.context)
            rebuilding_start = self.context.get('resume_unsubmitted_setup') is True
            if resumed_start or rebuilding_start:
                self.context['resume_start_open_attempted'] = True
            self.context['start_submitted'] = True
            self.context.update(start_cost=dict(before=before['tp_before'], after=before['tp_after'], cost=before['tp_cost']), start_cost_at=before.get('captured_at_unix_ms'))
        if action_id == 'finish_training':
            self.context['finish_submitted'] = True
        if action_id == 'confirm_autonomous_start':
            self.context['start_confirmed'] = True
        receipt = dict(action_id=action_id, source_frame_sha256=before['frame_sha256'], endpoint=self.endpoint, performed=None, status='dispatch_intent')
        if action_id == 'toggle_event_boost':
            receipt['event_boost'] = dict(before=before['event_boost_enabled'], requested=self.context.get('event_boost') is True)
        if action_id == 'scroll_skill_overlap':
            if candidate.get('point') != SKILL_OVERLAP_SWIPE['start'] or candidate.get('end') != SKILL_OVERLAP_SWIPE['end']:
                raise RuntimeError('skill_scroll_profile_mismatch')
            receipt['gesture_profile'] = dict(SKILL_OVERLAP_SWIPE)
        path = self.output_dir / (str(time.time_ns()) + '-receipt.json')
        receipt['receipt_path'] = str(path)
        mark_harvest_dispatch(action_id, before, self.context, path)
        if action_id == 'return_harvest_skills':
            request = self.context.get('harvest_low_pt_return') or self.context['harvest_no_purchase_return']
            request['submitted'] = True
            receipt.update(source_screenshot_path=before.get('screenshot_path'), source_current_skill_points=before.get('current_skill_points'), source_captured_at_unix_ms=before.get('captured_at_unix_ms'), source_expires_at_unix_ms=before.get('expires_at_unix_ms'), no_purchase_evidence_sha256=request.get('evidence_sha256'), low_pt_purchase_sha256=request.get('child_sha256'))
        if action_id == 'start_autonomous':
            if not resumed_start and (not rebuilding_start):
                self.context['request_id'] = str(path.resolve())
            receipt.update(start_cost=self.context['start_cost'], start_cost_at=self.context['start_cost_at'])
        if action_id in ('start_autonomous', 'confirm_autonomous_start'):
            receipt['request_id'] = self.context.get('request_id')
        if action_id == 'finish_training':
            receipt['finish_intent_path'] = self.context['finish_submission'].get('intent_path')
            receipt.update(source_screenshot_path=before.get('screenshot_path'), source_captured_at_unix_ms=before.get('captured_at_unix_ms'), source_finish_summary=before.get('finish_summary'), source_finish_action_point=before['actions'][action_id].get('point'))
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        if action_id in SKILL_SCROLL_BOUNDARIES:
            boundary = SKILL_SCROLL_BOUNDARIES[action_id]
            attempts = self.context.setdefault('skill_scroll_unknown_attempts', {})
            attempts[boundary] = attempts.get(boundary, 0) + 1 if before.get(boundary) is None else 0
        x, y = candidate['point']
        if action_id == 'scroll_skill_overlap':
            self.command('-s', self.endpoint, 'shell', skill_overlap_shell())
        elif action_id == 'scroll_skill_up':
            if candidate.get('point') != [360, 610] or candidate.get('end') != [360, 900]:
                raise RuntimeError('skill_rewind_profile_mismatch')
            self.command('-s', self.endpoint, 'shell', skill_rewind_shell())
        elif action_id in ('scroll_skill_confirmation', 'rewind_skill_confirmation'):
            self.command('-s', self.endpoint, 'shell', skill_overlap_shell(reverse=action_id == 'rewind_skill_confirmation'))
        elif candidate.get('gesture') == 'swipe':
            end_x, end_y = candidate['end']
            duration = '350' if action_id == 'scroll_skill_reveal_bottom' else '450' if action_id in ('scroll_skill_list', 'scroll_skill_up') else '700'
            self.command('-s', self.endpoint, 'shell', 'input', 'swipe', str(x), str(y), str(end_x), str(end_y), duration)
        else:
            self.command('-s', self.endpoint, 'shell', 'input', 'tap', str(x), str(y))
        if action_id in ('confirm_autonomous_start', 'cancel_autonomous_start'):
            capture = self.capture
        if stop.wait(0.15 if action_id == 'scroll_skill_overlap' else 0.2 if action_id in FAST_NAVIGATION else 1):
            raise RuntimeError('operator_stop')
        after = capture()
        if action_id.startswith('inspect_trainee_'):
            for _ in range(4):
                if stop.wait(0.3):
                    raise RuntimeError('operator_stop')
                settled = capture()
                stable = settled.get('page') == 'trainee' and bool(settled.get('selected_trainee_header')) and (settled.get('selected_trainee_header') == after.get('selected_trainee_header'))
                after = settled
                if stable or after.get('page') not in ('trainee', 'unknown', 'loading'):
                    break
        if action_id == 'scroll_skill_overlap' and after.get('skill_list_at_bottom') is True:
            for _ in range(3):
                if stop.wait(0.25):
                    raise RuntimeError('operator_stop')
                settled = capture()
                if settled.get('page') != 'skills' or settled.get('skill_list_at_bottom') is not True or (type(before.get('current_skill_points')) is int and type(settled.get('current_skill_points')) is int and (settled['current_skill_points'] != before['current_skill_points'])):
                    raise RuntimeError('skill_bottom_settlement_unverified')
                stable = skill_list_frames_match(read_image(str(after.get('screenshot_path', ''))), read_image(str(settled.get('screenshot_path', ''))))
                after = settled
                if stable:
                    break
            else:
                raise RuntimeError('skill_bottom_settlement_unverified')
        navigation_retry_at = time.monotonic() + 3
        deadline = time.monotonic() + (30 if action_id == 'forward_harvest' else 20)
        while time.monotonic() < deadline:
            if not transition_pending(action_id, before, after):
                break
            if action_id == 'return_home' and after['page'] == before['page'] and (action_id in after['actions']) and (time.monotonic() >= navigation_retry_at):
                break
            if stop.wait(0.3 if action_id in FAST_NAVIGATION else 0.5):
                raise RuntimeError('operator_stop')
            after = capture()
        if action_id == 'forward_harvest' and transition_pending(action_id, before, after):
            reads = []
            for _ in range(2):
                if stop.wait(1):
                    raise RuntimeError('operator_stop')
                after = capture()
                reads.append(dict(page=after.get('page'), frame_sha256=after.get('frame_sha256')))
                if not transition_pending(action_id, before, after):
                    break
            receipt['history_arrival_reobservations'] = reads
        receipt.update(performed=True, status='observed', observation=after)
        if action_id == 'toggle_event_boost':
            receipt['event_boost'].update(available=after.get('event_boost_available') is True, after=after.get('event_boost_enabled'))
        if action_id == 'forward_harvest' and transition_pending(action_id, before, after):
            receipt['reason'] = 'history_load_timeout'
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        plant_support.after_action(action_id, before, after, self.context)
        if plant_inheritance.after_action(action_id, before, after, self.context, receipt):
            receipt['inheritance_confirmation'] = dict(status='confirmed', selection='auto', borrow_friend=self.context.get('inheritance_options', {}).get('inheritance_borrow_friend', True))
            path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        if action_id == 'continue_support' and self.context.get('verified_support'):
            receipt['verified_support'] = self.context['verified_support']
            path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        return receipt
