"""Read only decision-bearing labels on already verified SS/race previews.

Every value comes from the current frame. Other pages, overlays, missing or
uncertain labels keep the existing full-page reader; no saved text is guessed.
"""
import re
import unicodedata

import cv2
import numpy as np


def default_local_reader(analyzer):
    from core.auto_trainer.frame_safety import FrameSafetyAnalyzer,LocalRapidOCRScreenTextReader
    return (type(analyzer) is FrameSafetyAnalyzer and
        type(getattr(getattr(analyzer,'rapidocr_reader',None),'__self__',None)) is LocalRapidOCRScreenTextReader)


def _frame(image):
    if image is None or image.shape not in {(1280,720,3),(2560,1440,3)}:return None
    return image if image.shape[1]==720 else cv2.resize(image,(720,1280),interpolation=cv2.INTER_AREA)


def _line(image,box,reader,*,minimum=.90):
    x,y,r,b=box
    text,confidence=reader(image[y:b,x:r])
    if confidence<minimum:return None
    return ''.join(unicodedata.normalize('NFKC',str(text)).split())


def _reader(reader):
    if reader is not None:return reader
    from core.auto_trainer.main_menu_state_provider import _CALENDAR_REGION_READER
    return _CALENDAR_REGION_READER.read_line


def _race_caption(image,reader):
    # Read the whole caption area so a Connecting overlay cannot be hidden by
    # cropping only the normal popularity label. Decorative rank rings are not
    # words; every other unexpected fragment sends us to the full-page reader.
    if reader is not None:
        value=_line(image,(480,50,708,128),reader,minimum=.85)
        return value if value and re.fullmatch(r'人气第\d+[◎○◉〇O0@]*',value) else None
    from core.auto_trainer.main_menu_state_provider import _CALENDAR_REGION_READER
    words=[]
    for box,text,confidence in _CALENDAR_REGION_READER.read_regions(image[50:128,480:708]):
        text=''.join(unicodedata.normalize('NFKC',str(text)).split())
        if not text or re.fullmatch(r'[◎○◉〇O0@]+',text):continue
        x=[point[0] for point in box];y=[point[1] for point in box]
        if (text in {'?','？'} and min(x)>=95 and 18<=max(x)-min(x)<=40
                and 18<=max(y)-min(y)<=40):continue # OCR of an individual rank ring.
        if confidence<.9 or not re.fullmatch(r'人气第\d+[◎○◉〇O0@]*',text):return None
        words.append(text)
    return words[0] if len(words)==1 else None


def ss_preview_text(image,*,reader=None):
    from core.auto_trainer.transition_wait import connecting_overlay_visible
    from core.auto_trainer.larc_control_wait import ss_preview_controls,modal_content_pending,modal_controls
    from scripts.capture_ug_parent_live_tabs import _selected_larc_tab_fast
    from core.auto_trainer.safe_advance import SafeAdvancePlanner
    image=_frame(image)
    if (image is None or connecting_overlay_visible(image) or _selected_larc_tab_fast(image)!='ss' or not ss_preview_controls(image)
            or modal_content_pending(image) or modal_controls(image)
            or SafeAdvancePlanner._looks_like_confirm_modal_panel(image)):
        return None
    try:
        read=_reader(reader)
        title=_line(image,(15,0,135,40),read)
        points=_line(image,(478,630,716,686),read)
        label=_line(image,(385,690,530,737),read)
        button=_line(image,(608,1048,705,1092),read)
        if (title not in {'群星赛','超星赛'} or button not in {'群星赛','超星赛'}
                or label is None or label.casefold()!='协助pt'
                or points is None or not re.fullmatch(r'全胜时\+[\d,]+积分',points)):
            return None
        # Counts, all gains, win badges, portraits, bonds and rewards are still
        # read from this same image by the existing structured SS readers.
        return '\n'.join((title,points,label,button))
    except (OSError,RuntimeError,ValueError,TypeError,cv2.error):
        return None


def ss_entry_text(image,*,reader=None):
    """The captured training screen's SS tab needs only its current labels."""
    from scripts.capture_ug_parent_live_tabs import _selected_larc_tab_fast
    from core.auto_trainer.larc_control_wait import modal_content_pending,modal_controls
    from core.auto_trainer.safe_advance import SafeAdvancePlanner
    from core.auto_trainer.larc_ss_count import read_larc_ss_count
    from core.auto_trainer.transition_wait import connecting_overlay_visible
    image=_frame(image)
    if (image is None or connecting_overlay_visible(image) or _selected_larc_tab_fast(image) not in {'speed','stamina','power','guts','wisdom'}
            or modal_content_pending(image) or modal_controls(image)
            or SafeAdvancePlanner._looks_like_confirm_modal_panel(image)
            or read_larc_ss_count(image,stage='entry').get('status')!='accepted'):
        return None
    try:
        read=_reader(reader)
        header=_line(image,(14,0,110,38),read)
        button=_line(image,(608,1100,705,1142),read)
        return '\n'.join((header,button)) if header=='训练' and button=='群星赛' else None
    except (OSError,RuntimeError,ValueError,TypeError,cv2.error):return None


def race_preview_text(image,policy,*,reader=None):
    from core.auto_trainer.race_route import RaceRoutePlanner
    from core.auto_trainer.safe_advance import SafeAdvancePlanner
    from core.auto_trainer.larc_control_wait import modal_content_pending,modal_controls
    image=_frame(image)
    if (image is None or policy.scenario!='larc' or policy.count_reserved_only or policy.wbc_only
            or policy.inspection_only or policy.allow_leave_race_detail
            or not (policy.allow_quick_result or policy.allow_skip_race_scene)
            or RaceRoutePlanner._race_preview_action_points(image) is None
            or modal_content_pending(image) or modal_controls(image)
            or SafeAdvancePlanner._looks_like_confirm_modal_panel(image)):
        return None
    try:
        read=_reader(reader)
        caption=_race_caption(image,reader)
        if caption is None:return None
        title=_line(image,(33,22,652,70),read)
        result=_line(image,(168,1140,322,1208),read)
        enter=_line(image,(382,1140,578,1208),read)
        ability=_line(image,(353,178,490,217),read)
        if (not title or len(title)>40 or result!='查看结果' or enter!='前往赛事' or ability!='基础能力'):
            return None
        lines=[title,ability,result,enter]
        if caption is not None:lines.append(caption)
        if policy.desired_run_style is not None or policy.solo_leader_to_front:
            distance=_line(image,(505,479,622,515),read,minimum=.85)
            style=_line(image,(505,516,622,554),read)
            change=_line(image,(515,721,632,773),read)
            if (distance is None or not re.fullmatch(r'(短距离|英里|中距离|长距离)[SABCDEFG]',distance)
                    or style is None or not re.fullmatch(r'(后追|居中|跟前|领跑)[SABCDEFG]',style)
                    or change!='更改战术'):return None
            lines.extend((distance,style,change))
        return '\n'.join(lines)
    except (OSError,RuntimeError,ValueError,TypeError,cv2.error):
        return None


def race_navigation_text(image, policy, session, *, reader=None, regions_reader=None,
                         _full_frame_observation=None):
    """Current-frame text for a race-day hub or fan reward list.

    Race lists/details and rank/retry screens keep their full reader. Splitting
    the candidate list into OCR regions was slower than the existing full read.
    """
    from core.auto_trainer.race_route import RaceRoutePlanner
    from core.auto_trainer.safe_advance import SafeAdvancePlanner
    from core.auto_trainer.larc_pages import larc_race_day_controls
    from core.auto_trainer.larc_control_wait import modal_content_pending, modal_controls
    from core.auto_trainer.transition_wait import connecting_overlay_visible
    from core.auto_trainer.frame_safety import _join_high_confidence_rapidocr_text
    image = _frame(image)
    if (image is None or policy.scenario != 'larc' or policy.count_reserved_only
            or policy.wbc_only or policy.inspection_only or policy.allow_leave_race_detail
            or connecting_overlay_visible(image) or modal_content_pending(image)
            or SafeAdvancePlanner._looks_like_confirm_modal_panel(image)):
        return None
    try:
        read = _reader(reader)

        def region(box):
            x, y, right, bottom = box
            if regions_reader is None:
                from core.auto_trainer.main_menu_state_provider import _CALENDAR_REGION_READER
                rows = _CALENDAR_REGION_READER.read_regions(image[y:bottom, x:right])
            else:
                rows = regions_reader(image[y:bottom, x:right])
            # Use the same confidence policy as the full reader, including
            # retention of low-confidence resource/payment warning text.
            return _join_high_confidence_rapidocr_text(rows)

        if (not policy.post_entry_only and policy.allow_enter_race
                and policy.allow_locked_race_entry and larc_race_day_controls(image)
                and not modal_controls(image)):
            lines = [_line(image, box, read) for box in (
                (14, 70, 147, 120), (10, 195, 146, 239),
                (288, 982, 414, 1035), (527, 1085, 683, 1136))]
            if lines != ['比赛日', '凯旋门信息', '实战', '海外适应性']:
                return None
            goal = region((158, 72, 710, 137))
            if '目标' not in goal or not any(word in goal for word in ('参加', '进入')):
                return None
            return '\n'.join([*lines, goal]), 'larc_race_day_regions'

        if (policy.allow_result_continue and session is not None
                and session.result_transition_pending
                and RaceRoutePlanner._looks_like_rank_points_result(image)):
            # This is the fan/item reward list, not the rank animation or its
            # retry controls. Reward icons are not a source of spend authority.
            title = _line(image, (12, 503, 702, 548), read)
            fans = _line(image, (35, 634, 629, 680), read)
            button = region((200, 1135, 520, 1270)).strip()
            footer_button = None
            if button == '下一步':
                footer_button = '下一步'
            else:
                left_btn = region((40, 1135, 360, 1270)).strip()
                right_btn = region((360, 1135, 680, 1270)).strip()
                next_btn = RaceRoutePlanner._race_result_next_button_from_image(image)
                if right_btn == '下一步' or (next_btn is not None and ('前往演出' in left_btn or not left_btn)):
                    footer_button = '前往演出\n下一步' if '前往演出' in left_btn else '下一步'
            if (not title or len(title) > 40 or not fans
                    or not re.fullmatch(r'粉丝数[\d,]+\(\+[\d,]+\)', fans)
                    or footer_button is None):
                return None
            # Failed regional reads already fall back to full-frame safety OCR.
            # Exclude a race list before accepting any successful regional read.
            if _race_list_postcondition(image, _full_frame_observation):
                return None
            return '\n'.join((title, fans, footer_button)), 'larc_race_reward_regions'
    except (OSError, RuntimeError, ValueError, TypeError, cv2.error):
        return None
    return None


def adaptation_grid_text(image, *, reader=None):
    """Read only header + bottom AP label on a verified adaptation grid.

    Confirmation modals, dimmed animation overlays, connecting overlays, and
    any unexpected text fall back to the full-frame safety reader.
    """
    from core.auto_trainer.safe_advance import SafeAdvancePlanner
    from core.auto_trainer.larc_control_wait import (
        adaptation_controls,
        adaptation_modal_content_pending,
        modal_controls,
    )
    from core.auto_trainer.transition_wait import connecting_overlay_visible
    image = _frame(image)
    if (image is None or connecting_overlay_visible(image)
            or adaptation_modal_content_pending(image)
            or modal_controls(image)
            or SafeAdvancePlanner._looks_like_confirm_modal_panel(image)
            or not adaptation_controls(image, allow_home=False)):
        return None
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    detail_bar = hsv[655:682, 50:670]
    bright_blue = ((detail_bar[:, :, 0] > 100) & (detail_bar[:, :, 0] < 130)
                   & (detail_bar[:, :, 1] > 100) & (detail_bar[:, :, 2] > 140))
    if float(bright_blue.mean()) < 0.60:
        return None
    try:
        read = _reader(reader)
        header = _line(image, (16, 4, 150, 42), read)
        footer = _line(image, (30, 978, 145, 1018), read)
        if header != '海外适应性' or footer != '消耗积分':
            return None
        return f'{header}\n{footer}'
    except (OSError, RuntimeError, ValueError, TypeError, cv2.error):
        return None


def _race_list_postcondition(image, observation):
    """Retain one successful full-frame read for this caller's safety reader.

    The list test keeps its original unfiltered text. Only a nonempty safety
    join with every box at the old confidence threshold may be reused; low
    confidence/failed reads retain the original later full-reader attempt.
    """
    from core.auto_trainer.race_route import RaceRoutePlanner
    if observation is None:
        return RaceRoutePlanner._looks_like_race_list_postcondition(image)
    from core.auto_trainer.frame_safety import (
        LocalRapidOCRScreenTextReader, _join_high_confidence_rapidocr_text,
        _RAPIDOCR_MIN_CONFIDENCE,
    )
    try:
        rows=LocalRapidOCRScreenTextReader().read_regions(image)
    except Exception:
        return False
    visible='\n'.join(str(row[1]) for row in rows if len(row)>1)
    try:
        reusable=bool(rows) and all(len(row)==3 and
            _RAPIDOCR_MIN_CONFIDENCE<=float(row[2])<=1. for row in rows)
        text=_join_high_confidence_rapidocr_text(rows) if reusable else ''
        if text.strip():observation.update(image=image,text=text)
    except (ValueError,TypeError,IndexError):
        pass
    return RaceRoutePlanner._looks_like_race_list_postcondition(image, visible)
