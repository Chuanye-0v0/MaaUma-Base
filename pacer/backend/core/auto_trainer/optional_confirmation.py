"""Recognize a career action that started with its confirmation disabled.

Call only after a successful, authorized action-selection tap. This module
observes the resulting page; it never clicks or treats a loading frame as proof.
"""
from pathlib import Path

import cv2


def _position(path):
    from core.main_menu_state import analyze_main_menu_state
    from core.auto_trainer.main_menu_state_provider import read_main_menu_date
    png = Path(path).read_bytes()
    observed = analyze_main_menu_state(png, scenario='larc')
    period = read_main_menu_date(png, observed, allow_pre_debut=True)
    if period.get('status') != 'verified':return None
    count = observed.get('fields', {}).get('remaining_turns', {})
    return {'period':period['value'], 'remaining_turns':count.get('value')
            if count.get('confidence') == 'high' else None}


def advanced_position(before, after):
    if not isinstance(before, dict) or not isinstance(after, dict):return False
    old, new = before.get('period') or {}, after.get('period') or {}
    if old.get('pre_debut') is True and new.get('pre_debut') is True:
        a, b = before.get('remaining_turns'), after.get('remaining_turns')
        return type(a) is int and type(b) is int and b == a-1
    if old.get('pre_debut') is True and before.get('remaining_turns') == 1:
        return new.get('year_index') == 1 and new.get('month') == 7
    def day(period):
        year, month, half = (period.get(k) for k in ('year_index','month','half'))
        if type(year) is int and year in {1,2,3} and type(month) is int and 1<=month<=12 and half in {'first','second'}:
            return year*24+(month-1)*2+(half=='second')
    a, b = day(old), day(new)
    return a is not None and b is not None and b>a


def direct_action_started(path, *, before_path=None, before_menu=None):
    """Return current UI evidence, or None for modals/unchanged/unknown pages."""
    from core.auto_trainer.safe_advance import SafeAdvanceExecutor, SafeAdvancePlanner
    from core.auto_trainer.event_options import event_option_rows_ready, help_carousel_visible
    from core.auto_trainer.larc_control_wait import modal_content_pending, modal_controls
    from core.auto_trainer.transition_wait import connecting_overlay_visible, career_comic_loading_visible
    image = cv2.imread(str(path))
    if image is None or image.shape[1::-1] not in {(720,1280),(1440,2560)}:return None
    if image.shape[1] != 720:image = cv2.resize(image,(720,1280))
    if (connecting_overlay_visible(image) or career_comic_loading_visible(image)
            or help_carousel_visible(image) or modal_content_pending(image)
            or SafeAdvancePlanner._looks_like_confirm_modal_panel(image) or modal_controls(image)):
        return None
    if SafeAdvanceExecutor._career_home_controls_ready(image):
        before = before_menu if before_menu is not None else (_position(before_path) if before_path else None)
        after = _position(path)
        return {'screen':'main_menu','position':after} if advanced_position(before,after) else None
    if event_option_rows_ready(image):return {'screen':'event_choice'}
    if (SafeAdvancePlanner._looks_like_active_bottom_skip_button(image)
            and (SafeAdvancePlanner._looks_like_training_result_dialogue(image)
                or SafeAdvancePlanner._looks_like_training_story_dialogue(image)
                or SafeAdvancePlanner._looks_like_dimmed_training_narration(image)
                or SafeAdvancePlanner._looks_like_story_dialogue_with_record_controls(image))):
        return {'screen':'dialogue'}
    return None
