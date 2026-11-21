"""Versioned extra preference; no additional game reward or resource claim."""
from core.auto_trainer.larc_phase import phase
from core.auto_trainer.larc_user_preferences import current_training_people


def presence_preference(state, candidate, source, goal):
    from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
    params = params_for_goal(goal)
    result = {'enabled': unified_larc_enabled(goal), 'components': {}, 'score': 0.,
              'reason': 'revision_disabled', 'score_kind': 'extra_policy_preference_not_game_reward',
              'user_mei_bonus_changed': False, 'actual_rewards_changed': False,
              'training_success_probability_applied': False, 'applied_at_most_once': True}
    if not result['enabled']:
        return result
    if getattr(state, 'screen', None) != 'training_menu':
        return {**result, 'reason': 'not_current_training_menu'}
    period = (getattr(state, 'scenario_state', {}) or {}).get('larc_period') or {}
    # Reuse phase()/period_value()'s existing verified-date semantics.
    if period.get('status') != 'verified':
        return {**result, 'reason': 'phase_unknown'}
    current_phase = phase(state)
    amount = {
        'junior_early': params.year1_mei_junior_early_bonus,
        'junior_late': params.year1_mei_junior_late_bonus,
    }.get(current_phase, 0.)
    result['phase'] = current_phase
    if current_phase == 'junior_unspecified':
        return {**result, 'reason': 'junior_month_unknown_no_phase_guessed'}
    if not amount:
        return {**result, 'reason': 'outside_junior_extra_preference_scope'}
    people = current_training_people(state, candidate, source)
    if people is None:
        return {**result, 'reason': 'current_participants_unknown_or_unbound'}
    packet = (candidate.scenario_mechanics.get('larc') or {}).get('training_participants') or {}
    packet_source = packet.get('source')
    if isinstance(packet_source, dict) and any(packet_source.get(k) != source.get(k)
                                              for k in ('run_session_id', 'turn_index')):
        return {**result, 'reason': 'participant_source_mismatch'}
    identified = any((p.get('identity') or {}).get('status') == 'known'
                     and p['identity'].get('character_id') == 'satake_mei' for p in people)
    if not identified:
        return {**result, 'reason': 'satake_mei_not_identified_here'}
    return {**result, 'score': amount,
            'components': {'mei_phase_presence_preference': amount},
            'reason': 'current_identified_Mei_additional_phase_preference',
            'source': {'run_session_id': source['run_session_id'], 'turn_index': source['turn_index']}}
