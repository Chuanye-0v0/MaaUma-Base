"""Preparation-only inheritance: keep a complete pair, otherwise fill explicitly missing parents."""
OPTION_KEYS = {'inheritance_selection', 'inheritance_borrow_friend'}
ACTIONS = {'auto_inheritance', 'continue_inheritance', 'toggle_borrow', 'confirm_inheritance'}

def validate(options):
    if not isinstance(options, dict):
        raise ValueError('mfa_inheritance_options_invalid')
    result = {key: options[key] for key in OPTION_KEYS if key in options}
    if 'inheritance_selection' in result and result['inheritance_selection'] not in ('keep', 'auto') or ('inheritance_borrow_friend' in result and type(result['inheritance_borrow_friend']) is not bool):
        raise ValueError('mfa_inheritance_options_invalid')
    return result

def _options(context):
    return validate(context.get('inheritance_options', {}))

def _selection(context):
    return _options(context).get('inheritance_selection', 'keep')

def _borrow(context):
    return _options(context).get('inheritance_borrow_friend', True)

def _auto_confirmation_scope(context):
    _selection(context)
    if context.get('inheritance_requested') is not True or context.get('start_submitted'):
        raise RuntimeError('inheritance_auto_selection_not_requested')
    if context.get('inheritance_confirmation_submitted'):
        raise RuntimeError('inheritance_confirmation_outcome_unverified')

def reconcile_borrow_toggle(observed, context):
    """A fresh target checkbox can settle an uncertain toggle, never repeat it."""
    marker = context.get('inheritance_borrow_toggle')
    if marker is None:
        return False
    if not isinstance(marker, dict) or type(marker.get('before_checked')) is not bool or type(marker.get('target_checked')) is not bool or (marker['before_checked'] is marker['target_checked']) or (marker['target_checked'] is not _borrow(context)) or (not isinstance(marker.get('source_frame_sha256'), str)) or (not marker['source_frame_sha256']):
        raise RuntimeError('inheritance_borrow_toggle_context_unverified')
    if observed.get('page') != 'inherit_confirm' or observed.get('borrow_checked') is not marker['target_checked']:
        raise RuntimeError('inheritance_borrow_toggle_outcome_unverified')
    context.pop('inheritance_borrow_toggle', None)
    return True

def mark_borrow_toggle(observed, context):
    """Persist intent before transport, including an uncertain transport error."""
    if not observed.get('frame_sha256'):
        raise RuntimeError('inheritance_borrow_toggle_source_unverified')
    context['inheritance_borrow_toggle'] = dict(before_checked=observed['borrow_checked'], target_checked=_borrow(context), source_frame_sha256=observed['frame_sha256'])

def choose(observed, context):
    page = observed.get('page')
    if page not in ('inheritance', 'inherit_confirm'):
        return None
    selection = _selection(context)
    if page == 'inherit_confirm':
        _auto_confirmation_scope(context)
        reconcile_borrow_toggle(observed, context)
        current = observed.get('borrow_checked')
        if type(current) is not bool:
            raise RuntimeError('borrow_state_unknown')
        return 'confirm_inheritance' if current is _borrow(context) else 'toggle_borrow'
    if not context.get('inheritance_confirmed'):
        if context.get('inheritance_confirmation_submitted'):
            raise RuntimeError('inheritance_confirmation_outcome_unverified')
        if context.get('inheritance_requested'):
            raise RuntimeError('inheritance_selection_outcome_unverified')
        if selection == 'auto' or observed.get('inheritance_full') is False:
            return 'auto_inheritance'
    complete = observed.get('inheritance_full')
    if type(complete) is not bool:
        raise RuntimeError('inheritance_completeness_unknown')
    if not complete:
        raise RuntimeError('current_inheritance_incomplete')
    return 'continue_inheritance'

def authorize(action, observed, context):
    if action not in ACTIONS:
        return
    selection = _selection(context)
    if context.get('mode') != 'start' or context.get('start_submitted'):
        raise RuntimeError('inheritance_preparation_not_authorized')
    if action == 'continue_inheritance':
        if observed.get('inheritance_full') is not True:
            raise RuntimeError('inheritance_not_full')
        if (selection == 'auto' or context.get('inheritance_requested') or context.get('inheritance_confirmation_submitted')) and context.get('inheritance_confirmed') is not True:
            raise RuntimeError('inheritance_auto_selection_not_confirmed')
    elif action == 'auto_inheritance':
        if selection != 'auto' and observed.get('inheritance_full') is not False or context.get('inheritance_requested') or context.get('inheritance_confirmation_submitted') or context.get('inheritance_confirmed'):
            raise RuntimeError('inheritance_auto_selection_not_requested')
    else:
        _auto_confirmation_scope(context)
        reconcile_borrow_toggle(observed, context)
        checked = observed.get('borrow_checked')
        if type(checked) is not bool:
            raise RuntimeError('borrow_state_unknown')
        if action == 'confirm_inheritance' and checked is not _borrow(context):
            raise RuntimeError('inheritance_confirmation_not_bound')
        if action == 'toggle_borrow' and checked is _borrow(context):
            raise RuntimeError('inheritance_borrow_toggle_not_needed')

def after_action(action, before, after, context, receipt):
    """Mark Auto done only from the actual confirmed action and resulting pair."""
    if action == 'toggle_borrow':
        marker = context.get('inheritance_borrow_toggle') or {}
        if receipt.get('performed') is True and receipt.get('status') == 'observed' and (receipt.get('source_frame_sha256') == marker.get('source_frame_sha256')) and (before.get('frame_sha256') == marker.get('source_frame_sha256')) and (after.get('page') == 'inherit_confirm') and (type(after.get('borrow_checked')) is bool) and (after['borrow_checked'] is marker.get('target_checked')):
            reconcile_borrow_toggle(after, context)
        return False
    if action != 'confirm_inheritance' or context.get('inheritance_requested') is not True or context.get('inheritance_confirmation_submitted') is not True or (receipt.get('performed') is not True) or (receipt.get('status') != 'observed') or (before.get('page') != 'inherit_confirm') or (before.get('borrow_checked') is not _borrow(context)) or (not before.get('frame_sha256')) or (receipt.get('source_frame_sha256') != before['frame_sha256']) or (after.get('page') != 'inheritance') or (after.get('inheritance_full') is not True):
        return False
    context['inheritance_confirmed'] = True
    context.pop('inheritance_requested', None)
    context.pop('inheritance_borrow_toggle', None)
    return True
