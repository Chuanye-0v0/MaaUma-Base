"""Decide from structured observations; no screenshot/AI decision path."""
from .options import normalize_options


ALLOWED = {
    'home': {'open_event','next_banner','open_races'},
    'races': {'open_events'}, 'events': {'open_event'},
    'st_hub': {'home','open_captain','open_opponents','open_missions'},
    'st_closed': {'home'}, 'st_network': {'retry_network'}, 'st_paid': {'cancel'},
    'st_captain': {'confirm_captain'}, 'st_tactics': {'confirm_tactics'},
    'st_team_confirm': {'confirm_team','cancel'},
    'st_adaptation_warning': {'confirm_adaptation'},
    'st_opponents': {'home','back_event'},
    'st_preview': {'start_normal','start_special','open_refill','cancel'},
    'st_race_detail': {'view_result','continue_race'}, 'st_race': {'skip_race'},
    'st_result': {'open_retry','next_result'},
    'st_retry': {'confirm_retry','cancel'},
    'st_refill': {'confirm_refill','cancel','minimum_refill','maximum_refill','increase_refill','decrease_refill'},
    'st_refill_limit': {'close_refill_limit'},
    'st_refill_prompt': {'open_refill','cancel'},
    'st_refill_items': {'select_jelly','close_refill_items'},
    'st_refill_done': {'close_refill_done'},
    'st_scout': {'open_recruit','open_replace','request_recruit','no_recruit'},
    'st_recruit_select': {'confirm_grid','cancel_grid'},
    'st_replace_select': {'confirm_grid','cancel_grid'},
    'st_recruit_confirm': {'confirm_recruit','cancel'},
    'st_no_recruit_confirm': {'finish_no_recruit','cancel'},
    'st_recruit_done': {'continue_recruit_done','return_event'},
    'st_special_done': {'continue_special_done','return_event'},
    'st_grade_reward': {'continue_grade'},
    'st_missions': {'close_missions','tab_daily','tab_period','claim_all'},
    'st_claimed': {'close_claimed'}, 'st_notice': {'continue_notice'},
}
PREFIXES = {'st_captain':'captain_', 'st_tactics':'style_', 'st_opponents':'opponent_',
            'st_recruit_select':'select_card_', 'st_replace_select':'select_card_'}


def authorize(action, frame, data, options=None):
    options=normalize_options(options)
    page=frame.get('page');candidate=frame.get('actions',{}).get(action)
    if not candidate or candidate.get('enabled') is False:
        return False, 'strongest_team_action_unavailable'
    if (action not in ALLOWED.get(page,set())
            and not action.startswith(PREFIXES.get(page,'\0'))):
        return False, 'strongest_team_action_out_of_page'
    if frame.get('requires_context',True) and not data.get('context_verified'):
        return False, 'strongest_team_context_unverified'
    if candidate.get('resource_kind') not in {'free','event_ticket','free_special','free_retry','mini_jelly','existing_alarm','event_points','earned_rewards'}:
        return False, 'strongest_team_paid_resource_forbidden'
    if candidate.get('effect')=='purchase':
        return False, 'strongest_team_paid_resource_forbidden'
    op=data.get('pending_operation')
    if action=='confirm_adaptation':
        entry=data.get('pending_entry') or {};continuation=(op or {}).get('continuation')
        bound=(op and entry.get('id')==op.get('id')==candidate.get('entry_id')==candidate.get('parent_intent_id')
            and op.get('resource')==entry.get('kind') and entry.get('status')=='submitted' and not entry.get('progress_seen'))
        active=(bound and op.get('status')=='continuation_reserved' and continuation
            and continuation.get('dispatch_id')==(data.get('last_action')or{}).get('dispatch_id'))
        ready=bound and op.get('status')=='submitted' and not continuation
        return (True,'strongest_team_original_admission_continuation') if active or ready else (False,'strongest_team_continuation_not_owned')
    if op and action in {'start_normal','start_special','confirm_retry','confirm_refill','confirm_recruit','claim_all'}:
        # reserve() has already bound the only legitimate dispatch.
        if op.get('status')!='reserved' or op.get('action')!=action:
            return False, 'strongest_team_operation_pending'
    if action.startswith('select_card_') or action.startswith('captain_'):
        card=candidate.get('card',{})
        if card.get('captain') or card.get('unavailable'):
            return False,'strongest_team_card_unavailable'
    return True,'strongest_team_observed_action'


def choose(frame, journal, options=None):
    options=normalize_options(options)
    data=journal.data;page=frame.get('page');actions=frame.get('actions',{})
    count=data['normal_completed'];entry=data.get('pending_entry');op=data.get('pending_operation')

    def finish(status, reason):
        return dict(kind='finish',status=status,reason=reason)
    def wait(reason):
        return dict(kind='wait',reason=reason)
    def action(name, reason, **updates):
        if name not in actions:
            return wait('strongest_team_control_rendering:'+name)
        ok,error=authorize(name,frame,data,options)
        return dict(kind='action',action=name,reason=reason,updates=updates) if ok else finish('failed',error)
    def first(names, reason):
        for name in names:
            if name in actions:return action(name,reason)
        return wait(reason+':controls_rendering')

    if page in {'connection_conflict','connection_interrupted','disconnected'}:
        return finish('failed','strongest_team_'+page)
    if frame.get('requires_context',True) and not data.get('context_verified'):
        if page in {'unknown','loading'}:return wait('strongest_team_loading')
        return finish('failed','strongest_team_context_unverified')
    if page=='st_paid':
        return action('cancel','strongest_team_cancel_paid_dialog',exit_error='paid_resource_dialog')
    if page=='st_network':
        return action('retry_network','strongest_team_neutral_network_retry')
    if frame.get('native_busy'):
        return wait('strongest_team_native_request_in_flight')
    if op and op.get('status') in {'uncertain','continuation_reserved'}:
        # A fresh same-entry postcondition can resolve it in journal.observe.
        # The exact same confirmation is never issued a second time.
        if page in {'unknown','loading'}:return wait('strongest_team_uncertain_observe')
        if op['resource']=='clock' and page=='st_result' and 'open_retry' in actions:
            return action('open_retry','strongest_team_original_clock_stock_recheck')
        return finish('failed','strongest_team_uncertain_operation_preserved')
    if op and op.get('status')=='submitted' and page==op.get('source_page'):
        return wait('strongest_team_submitted_operation_rendering')
    if page in {'unknown','loading'}:
        return wait(frame.get('reason','strongest_team_loading'))
    if page=='home':
        if journal.pending:return finish('failed','strongest_team_pending_owner_at_home')
        if data.get('day_changed'):return finish('cancelled','strongest_team_game_day_changed_checkpoint_yield')
        if data.get('exit_error'):return finish('failed','strongest_team_'+data['exit_error'])
        if data.get('done') or data.get('exit_requested') or options['max_entries']==0:
            return finish('done' if count or data['special_completed'] else 'skipped',
                          'strongest_team_home_verified')
        if data.get('home_swipes',0)>=8:
            return finish('failed','strongest_team_entry_not_found_home_verified')
        if 'open_event' in actions:return action('open_event','strongest_team_enter_named_event')
        if 'next_banner' in actions:return action('next_banner','strongest_team_find_named_banner',home_swipes=data.get('home_swipes',0)+1)
        return first(['open_races'],'strongest_team_original_races_navigation')
    if page in {'races','events'}:
        return first(['open_events','open_event'],'strongest_team_original_event_navigation')
    if page=='st_closed':
        if journal.pending:return finish('failed','strongest_team_pending_closed_event')
        return action('home','strongest_team_closed_return_home',exit_requested=True)
    scans_complete=data.get('mission_scan_at')==count and data.get('period_scan_at')==count
    exhausted=(data.get('resources_exhausted') or journal.cap_remaining()==0)
    need_missions=((frame.get('tickets')==0 or exhausted or count>=5 and not data['daily_tickets_claimed']) and not scans_complete)
    if page=='st_hub':
        if journal.pending:return finish('failed','strongest_team_pending_entry_at_hub')
        if data.get('day_changed'):return action('home','strongest_team_day_boundary_return_home',exit_requested=True)
        if data.get('exit_error'):return action('home','strongest_team_failed_return_home',exit_requested=True)
        if need_missions:return action('open_missions','strongest_team_free_missions_before_refill')
        if exhausted:
            if (options['special_extra'] and options['special_race'] and data['special_reserved']==0
                    and count>=10 and data.get('special_probe_at')!=count):
                return action('open_opponents','strongest_team_probe_free_special_before_exit',special_probe_at=count)
            return action('home','strongest_team_budget_or_resources_finished',exit_requested=True)
        if 'open_captain' in actions:return action('open_captain','strongest_team_choose_own_captain')
        return action('open_opponents','strongest_team_open_opponents')
    if page=='st_captain':
        cards=frame.get('cards',[])
        last=data.get('last_action',{})
        if last.get('source_page')==page and last.get('action','').startswith('captain_') and last.get('action_outcome',{}).get('execution_status')=='executed':
            return action('confirm_captain','strongest_team_confirm_measured_captain')
        if not cards:return wait('strongest_team_captain_cards_rendering')
        i=max(range(len(cards)),key=lambda n:cards[n]['value'])
        if cards[i].get('selected') and 'confirm_captain' in actions:
            return action('confirm_captain','strongest_team_keep_selected_highest_captain')
        return action('captain_'+str(i),'strongest_team_highest_visible_captain')
    if page=='st_tactics':
        styles=frame.get('styles',[]);last=data.get('last_action',{})
        if not styles:return wait('strongest_team_tactics_rendering')
        best=max(s['grade'] for s in styles)
        if any(s['selected'] for s in styles) or (last.get('source_page')==page and last.get('action','').startswith('style_') and last.get('action_outcome',{}).get('execution_status')=='executed'):
            return action('confirm_tactics','strongest_team_keep_best_native_style')
        return action(next(s['action'] for s in styles if s['grade']==best),'strongest_team_choose_native_style')
    if page=='st_team_confirm':
        if journal.pending:return finish('failed','strongest_team_pending_entry_at_team_confirmation')
        if data.get('day_changed'):return action('cancel','strongest_team_day_boundary_no_team_registration')
        return action('confirm_team','strongest_team_confirm_existing_initial_team')
    if page=='st_adaptation_warning':
        if data.get('day_changed'):return finish('failed','strongest_team_day_boundary_admission_preserved')
        if op and (op.get('continuation') or {}).get('status')=='submitted':
            return wait('strongest_team_continuation_submitted_rendering')
        return action('confirm_adaptation','strongest_team_original_reserved_entry_confirmation')
    if page=='st_opponents':
        if entry:return finish('failed','strongest_team_original_entry_not_settled')
        if data.get('day_changed'):return action('home','strongest_team_day_boundary_return_home',exit_requested=True)
        if frame.get('entry_kind')=='special':
            allowed=options['special_race'] and data['special_reserved']<1 and (options['special_extra'] or journal.cap_remaining()>0)
            if not allowed:
                if journal.cap_remaining()>0 and not data.get('resources_exhausted'):
                    return action('home','strongest_team_used_special_without_normal_entry',exit_error='ordinary_entry_unavailable_after_special',exit_requested=True)
                return action('home','strongest_team_special_already_used',exit_requested=True)
        elif journal.cap_remaining()==0 or data.get('resources_exhausted') or need_missions:
            return action('back_event','strongest_team_return_for_missions_or_limit')
        opponents=frame.get('opponents',[])
        if not opponents:return wait('strongest_team_opponents_rendering')
        i=min(range(len(opponents)),key=lambda n:opponents[n]['rating'])
        return action('opponent_'+str(i),'strongest_team_lowest_visible_opponent')
    if page=='st_preview':
        if entry:return finish('failed','strongest_team_entry_already_submitted')
        if data.get('day_changed'):return action('cancel','strongest_team_day_boundary_no_new_entry')
        if frame.get('entry_kind')=='special':
            return action('start_special','strongest_team_free_special_once')
        if journal.cap_remaining()==0:return action('cancel','strongest_team_normal_cap')
        if frame.get('tickets')==0:
            if not scans_complete:return action('cancel','strongest_team_check_free_ticket_missions')
            if not options['allow_existing_jelly'] or data['jelly_reserved']>=options['max_jellies']:
                return action('cancel','strongest_team_no_more_refill',resources_exhausted=True)
            return action('open_refill','strongest_team_verified_zero_ticket_refill',refill_tickets_before=0)
        return action('start_normal','strongest_team_one_existing_ticket')
    if page in {'st_race','st_race_detail'}:
        if not entry:return finish('failed','strongest_team_unowned_race')
        return first(['view_result','skip_race','continue_race'],'strongest_team_same_entry_progress')
    if page=='st_result':
        if not entry:return finish('failed','strongest_team_unowned_result')
        if (not data.get('day_changed') and frame.get('outcome')=='loss' and (data['clock_reserved']<options['max_clocks'] or data['free_retry_reserved']<5)
                and data.get('decline_retry_for')!=entry['id']):
            return action('open_retry','strongest_team_existing_alarm_budget')
        return action('next_result','strongest_team_accept_result')
    if page=='st_retry':
        if not entry:return action('cancel','strongest_team_unowned_retry')
        if data.get('day_changed'):return action('cancel','strongest_team_day_boundary_no_retry')
        candidate=actions.get('confirm_retry',{})
        cost=candidate.get('cost')
        if (cost==1 and data['clock_reserved']>=options['max_clocks']
                or cost==0 and data['free_retry_reserved']>=5
                or 'confirm_retry' not in actions):
            return action('cancel','strongest_team_retry_not_available',decline_retry_for=entry['id'])
        return action('confirm_retry','strongest_team_one_existing_alarm')
    if page=='st_refill_done':return action('close_refill_done','strongest_team_close_verified_ticket_refill')
    if page=='st_refill_items':
        if entry:return finish('failed','strongest_team_owned_entry_at_item_selector')
        if (data.get('day_changed') or not options['allow_existing_jelly'] or journal.cap_remaining()==0
                or data['jelly_reserved']>=options['max_jellies'] or frame.get('stock_before')==0):
            return action('close_refill_items','strongest_team_no_existing_item_budget',resources_exhausted=True)
        return action('select_jelly','strongest_team_select_existing_mini_jelly_preview')
    if page=='st_refill_prompt':
        if entry:return finish('failed','strongest_team_owned_entry_at_refill_prompt')
        if (data.get('day_changed') or not options['allow_existing_jelly'] or journal.cap_remaining()==0
                or data['jelly_reserved']>=options['max_jellies']):
            return action('cancel','strongest_team_no_refill_budget',resources_exhausted=True)
        if not scans_complete:return action('cancel','strongest_team_free_missions_before_refill')
        return action('open_refill','strongest_team_open_existing_item_selector',refill_tickets_before=0)
    if page=='st_refill':
        if entry:return finish('failed','strongest_team_entry_pending_at_refill')
        if data.get('day_changed'):return action('cancel','strongest_team_day_boundary_no_refill')
        tickets=frame.get('tickets') if frame.get('tickets') is not None else data.get('refill_tickets_before')
        if type(tickets) is not int:return wait('strongest_team_refill_ticket_count_unknown')
        left=min(journal.cap_remaining(),options['max_jellies']-data['jelly_reserved'],5-tickets)
        if not options['allow_existing_jelly'] or left<=0:
            return action('cancel','strongest_team_refill_budget_finished',resources_exhausted=True)
        if frame.get('stock_before')==0:
            return action('cancel','strongest_team_no_existing_jelly',resources_exhausted=True)
        if frame.get('quantity') is None or frame.get('stock_before') is None:
            return wait('strongest_team_refill_values_rendering')
        wanted=min(left,frame['stock_before'])
        if frame['quantity']>wanted:
            return first(['decrease_refill','minimum_refill'],'strongest_team_reduce_refill_quantity')
        if frame['quantity']<wanted:
            if wanted==5 and 'maximum_refill' in actions:return action('maximum_refill','strongest_team_set_bounded_refill_quantity')
            return action('increase_refill','strongest_team_set_bounded_refill_quantity')
        return action('confirm_refill','strongest_team_existing_mini_jelly_only')
    if page=='st_refill_limit':
        return action('close_refill_limit','strongest_team_native_refill_limit_verified',resources_exhausted=True)
    if page=='st_scout':
        prior=data.get('last_completed_entry')
        if not prior:return finish('failed','strongest_team_unowned_scout')
        if (data.get('day_changed') or not options['recruit'] or data.get('scout_declined')==prior['id']
                or data.get('recruited_for')==prior['id']):
            return action('no_recruit','strongest_team_no_recruit_selected')
        if not frame.get('recruit_selected'):return action('open_recruit','strongest_team_choose_available_recruit')
        if not frame.get('replace_selected'):return action('open_replace','strongest_team_replace_non_captain')
        return action('request_recruit','strongest_team_read_final_recruit_confirmation')
    if page in {'st_recruit_select','st_replace_select'}:
        cards=frame.get('cards',[]);last=data.get('last_action',{})
        if last.get('source_page')==page and last.get('action','').startswith('select_card_') and last.get('action_outcome',{}).get('execution_status')=='executed':
            return action('confirm_grid','strongest_team_confirm_observed_card_selection')
        valid=[i for i,c in enumerate(cards) if page=='st_replace_select' or type(frame.get('points')) is int and c['value']<=frame['points']]
        if not valid:
            return action('cancel_grid','strongest_team_no_safe_scout_candidate',scout_declined=(data.get('last_completed_entry') or {}).get('id'))
        i=(max if page=='st_recruit_select' else min)(valid,key=lambda n:cards[n]['value'])
        if cards[i].get('selected') and 'confirm_grid' in actions:
            return action('confirm_grid','strongest_team_keep_native_selected_card')
        return action('select_card_'+str(i),'strongest_team_current_affordable_candidate')
    if page=='st_recruit_confirm':
        if data.get('day_changed'):return action('cancel','strongest_team_day_boundary_no_recruit')
        if 'confirm_recruit' not in actions:
            return action('cancel','strongest_team_recruit_improvement_or_cost_unverified',scout_declined=(data.get('last_completed_entry') or {}).get('id'))
        return action('confirm_recruit','strongest_team_confirm_bounded_event_points')
    if page=='st_no_recruit_confirm':return action('finish_no_recruit','strongest_team_confirm_no_recruit')
    if page=='st_recruit_done':return first(['continue_recruit_done','return_event'],'strongest_team_leave_settled_scout')
    if page=='st_special_done':return first(['return_event','continue_special_done'],'strongest_team_leave_settled_special_race')
    if page=='st_grade_reward':
        if op and op['resource']=='points' and frame.get('team_total') is None:
            return wait('strongest_team_grade_score_rendering')
        return action('continue_grade','strongest_team_close_actual_grade_reward')
    if page=='st_missions':
        if data.get('day_changed'):return action('close_missions','strongest_team_day_boundary_no_new_claim')
        if frame.get('claimable',0)>0:
            if op:return wait('strongest_team_claim_submission_settling')
            return action('claim_all','strongest_team_claim_visible_earned_rewards_once')
        if data.get('mission_scan_at')!=count:return action('tab_daily','strongest_team_scan_daily_ticket_rewards')
        if data.get('period_scan_at')!=count:return action('tab_period','strongest_team_scan_period_rewards')
        return action('close_missions','strongest_team_missions_checked')
    if page=='st_claimed':return action('close_claimed','strongest_team_close_actual_rewards')
    if page=='st_notice':return action('continue_notice','strongest_team_neutral_presentation')
    return wait('strongest_team_wait_known_page')
