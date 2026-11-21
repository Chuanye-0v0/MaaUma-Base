"""Read-only handoffs to an existing route owner."""


_TARGETS = {
    'ready_main_menu': 'main_menu',
    'ready_training_menu': 'training_menu',
    'event_choice': 'event_choice',
    'race_list': 'race_list',
    'post_race_dialogue': 'dialogue',
    'race_day_main_menu': 'main_menu',
    'skill_acquisition_handoff': 'skill_acquisition',
    'career_end_confirmation': 'modal',
    'terminal_rating_verified': 'modal',
}


def handoff(reason, *, evidence=None, **extra):
    return {**extra, 'action': 'handoff', 'reason': reason, 'target': _TARGETS[reason],
            'evidence': evidence or {}}


def is_route_handoff(decision, *, reason=None):
    """Accept a typed handoff or its known read-only historical form."""
    if not isinstance(decision, dict) or any(decision.get(key) is not None for key in ('tap', 'swipe', 'gesture', 'keyevent')):
        return False
    observed = decision.get('reason')
    if not isinstance(observed, str) or observed not in _TARGETS or reason is not None and reason != observed:
        return False
    return (decision.get('action') == 'stop' or decision.get('action') == 'handoff'
            and decision.get('target') == _TARGETS[observed])


def is_route_handoff_result(result):
    """Classify only the successfully returned, non-input step as a handoff."""
    if (not isinstance(result, dict) or result.get('performed') is not False
            or any(result.get(key) for key in ('tap_performed', 'input_receipts', 'input_may_have_occurred'))
            or any(result.get(key) is not None for key in ('tap_outcome', 'input_outcome'))):
        return False
    decision = result.get('decision')
    return (is_route_handoff(decision) and decision.get('action') == 'handoff'
            and result.get('reason') == decision['reason'])
