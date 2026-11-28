"""Bounded ordinary-career restart, exclusively owned by cut farming."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import time

import cv2
import numpy as np

from core.auto_trainer.cut_condition_probe import ocr_tokens,locate_ability_button,locate_race_ability_button,analyze_cut_details
from core.auto_trainer.condition_probe import analyze_ability_details_button_anchor
from core.auto_trainer.career_setup_perception import CareerSetupPerceptor
from core.auto_trainer.career_start_perception import _home_layout_anchors
from core.auto_trainer.global_home_layout import global_home_portal_point
from core.auto_trainer.safe_advance import SafeAdvancePlanner
from core.auto_trainer.cut_farming import atomic_json,expected_character_name
from core.execution_frames import ExecutionFrames
from core.native_capture_adb import adb_for_capture
from core.perception import Perception
from core.coordinates import CoordinateConfig,CoordinateScaler
from core.auto_trainer.cut_tp_refill import analyze_tp_refill,analyze_tp_controls,tp_resource_shortage,_foreground,_green_enabled,energy_drink_30
from core.auto_trainer.transition_wait import connecting_overlay_visible,career_comic_loading_visible,TransitionBudget
from core.auto_trainer.cut_friend_support import empty_friend_slot,choose_friend_support,target_from_profile
from core.auto_trainer.runtime_control import Reobserve,input_guard
from core.auto_trainer.cut_setup_observation import support_slot_observation,selected_mode_label

SETUP_UNREADABLE_REASONS=frozenset({'final_confirm_character_unreadable',
    'final_confirm_scenario_unreadable','ordinary_mode_unreadable','support_self_cards_unreadable'})
SETUP_UNREADABLE_READ_LIMIT=3


def _selected_mode_visible(image,token):
    return selected_mode_label(image,token)


def _explicit_missing_own_support(text):
    return any(term in text for term in ('请选择协助卡','请选择辅助卡','请选择支援卡','自有卡未编成','未编成协助卡'))


def button_boxes(image):
    boxes = [tuple(b['bbox']) for b in Perception().analyze_image(image)['buttons'] if b.get('score',0)>=.8]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 40, 100)
    contours, _ = cv2.findContours(edges,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x,y,w,h=cv2.boundingRect(contour)
        if 70<=w<=650 and 30<=h<=120 and 1.5<=w/h<=12:
            boxes.append((x,y,w,h))
    return boxes


def visible_control(tokens, boxes, labels):
    matches=[]
    for token in tokens:
        if token['text'].strip('！!。') not in labels:
            continue
        x0,y0,x1,y1=token['bbox'];x,y=(x0+x1)/2,(y0+y1)/2
        containing=[box for box in boxes if box[0]<=x<box[0]+box[2] and box[1]<=y<box[1]+box[3]]
        if containing:
            matches.append((round(x),round(y)))
    unique=set(matches)
    return list(unique)[0] if len(unique)==1 else None


def _detect_dismissible_home_popup(tokens, boxes):
    """Detect only real modal confirmation/notice dialogs, never floating badges."""
    modal_tokens = [
        t for t in tokens
        if 120 <= (t['bbox'][1] + t['bbox'][3]) / 2 <= 1120
        and not (t['bbox'][0] >= 540 and t['bbox'][1] >= 540)
    ]
    modal_text = ''.join(t['text'] for t in modal_tokens)
    if not modal_text:
        return None
    if any(t in modal_text for t in (
        '放弃', '最终确认', '协助卡编成', '选择育成优俊少女', '选择养成优俊少女',
    )):
        return None
    is_pass_renewal = (
        ('因子通行证' in modal_text or '训练通行证' in modal_text
         or ('通行证' in modal_text and any(k in modal_text for k in ('续费', '到期', '过期', '重置', '高级通行证', '特权'))))
        and ('取消' in modal_text or '关闭' in modal_text)
    )
    if is_pass_renewal:
        center = visible_control(modal_tokens, boxes, {'取消', '关闭'})
        if center is not None:
            return ('pass_renewal_cancel', center)
        return None
    if any(t in modal_text for t in (
        '训练值', 'TP', '萝卜', '宝石', '充值', '购买', '支付',
    )):
        return None
    has_activity_cue = ('活动确认' in modal_text) or ('要前往活动页面吗' in modal_text)
    has_activity_go = any(lbl in modal_text for lbl in ('前往活动首页', '前往活动页面', '前往活动'))
    if has_activity_cue and has_activity_go:
        center = visible_control(modal_tokens, boxes, {'取消'})
        if center is not None:
            return ('activity_confirm', center)
        return None
    if (any(t in modal_text for t in ('公告', '通知', '活动通知'))
            and not any(t in modal_text for t in (
                '选择剧本', '育成剧本', '剧本选择', '选择养成剧本', '选择育成剧本',
                '选择继承', '继承优俊少女', '强化编成',
            ))):
        center = visible_control(modal_tokens, boxes, {'取消', '关闭'})
        if center is not None:
            return ('notice_popup', center)
    return None


def resource_quantity(text, kind):
    if kind=='tp_items':
        patterns=(r'(?:使用|消耗)(?:数量)?[:：]?(\d+)个',r'使用数量[:：]?(\d+)')
    else:
        patterns=(r'(?:消耗|使用)(?:胡萝卜|萝卜|宝石)[:：]?(\d+)',r'(\d+)(?:个)?(?:胡萝卜|萝卜|宝石)')
    values={int(m) for pattern in patterns for m in re.findall(pattern,text)}
    return values.pop() if len(values)==1 and next(iter(values))>0 else None


def _home_hud_verified(image, tokens, text):
    anchors = _home_layout_anchors(image)
    if ('TP' in text and 'RP' in text) or anchors[0]:
        return True
    # The updated home artwork can obscure OCR of the stylized TP/RP labels.
    # Its TP fraction remains in the same HUD slot; require the home navigation
    # and selected home tab too. This identifies the layout, not a TP budget.
    return all(anchors[1:]) and any(
        re.fullmatch(r'\d{1,3}/100', token['text'])
        and 205 < token['bbox'][0] < 400 and token['bbox'][1] < 80
        for token in tokens)


def menu_icon_center(image):
    coordinates=CoordinateConfig(Path(__file__).resolve().parents[2]/'config/coordinates.json')
    center=CoordinateScaler(coordinates.reference_size,(720,1280)).scale_point(*coordinates.point('training_menu','menu'))
    x,y=center;crop=image[max(0,y-40):min(1280,y+40),max(0,x-40):min(720,x+40)]
    hsv=cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
    mask=cv2.inRange(hsv,(0,40,40),(35,255,215))
    _,_,stats,_=cv2.connectedComponentsWithStats(mask)
    bars=[(bx,by,w,h) for bx,by,w,h,area in stats[1:] if 15<=w<=60 and 2<=h<=12 and w/h>=3 and area>w*h*.6]
    bars.sort(key=lambda b:b[1])
    for index in range(len(bars)-2):
        group=bars[index:index+3]
        if max(b[0] for b in group)-min(b[0] for b in group)<=6 and all(5<=group[i+1][1]-group[i][1]<=22 for i in (0,1)):
            return (x-40+round(sum(b[0]+b[2]/2 for b in group)/3),
                    y-40+round(sum(b[1]+b[3]/2 for b in group)/3))
    return None


def analyze_restart_frame(path, *, config, phase, references=None, reader=None, ordinary_start_verified=False,support_start_verified=False,remaining_resources=None,selected_resource_kind=None,borrow_target=None,check_start_tp=True,support_preparation=None,abandon_pending=False,refill_selection=None,unavailable_resources=()):
    image=cv2.imread(str(path));result={'state':'unknown','reason':'restart_page_unrecognized','source':str(path)}
    if image is None or image.shape[1::-1] not in {(720,1280),(1440,2560)}:
        return result
    scale=image.shape[1]/720;image=cv2.resize(image,(720,1280))
    result['source_sha256']=hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if connecting_overlay_visible(image):
        return {**result,'state':'wait','reason':'restart_connecting','next_phase':phase}
    if career_comic_loading_visible(image):
        return {**result,'state':'wait','reason':'restart_loading_comic','next_phase':phase}
    remaining=remaining_resources or {'tp_items':config.max_tp_items,'carrots':config.max_carrots}
    refill_entry_verified=ordinary_start_verified or support_start_verified
    if phase in {'setup','starting','refill','opening','refill_return'} and refill_entry_verified:
        refill=analyze_tp_refill(image,remaining=remaining,selected_resource_kind=selected_resource_kind,
            returning=phase=='refill_return' or (refill_selection or {}).get('stage')=='confirmed',reader=reader,
            medicine_enabled=config.max_tp_items>0,carrots_enabled=config.max_carrots is None or config.max_carrots>0,
            refill_selection=refill_selection,unavailable_resources=unavailable_resources)
        if refill is not None:
            if refill.get('center'):refill['center']=[round(v*scale) for v in refill['center']]
            return {**result,**refill}
    tokens=ocr_tokens(image,reader);text=''.join(t['text'] for t in tokens);boxes=button_boxes(image)
    result['ocr_text']=text[:400]
    if SafeAdvancePlanner._is_account_login_conflict_text(text):
        return {**result,'reason':'account_login_conflict'}
    if '养成结束确认' in text and '养成结束' in text:
        return {**result,'state':'existing_career_handoff','reason':'career_end_confirmation_visible'}
    if phase in {'setup','starting','refill','opening','refill_return'} and refill_entry_verified:
        shortage=tp_resource_shortage(tokens,boxes,selected_resource_kind=selected_resource_kind)
        if shortage is not None:
            if shortage.get('center'):shortage['center']=[round(v*scale) for v in shortage['center']]
            return {**result,**shortage}
    dismissible_home = _detect_dismissible_home_popup(tokens,boxes) if phase in {'setup','refill_return'} else None
    if (any(term in text for term in ('充值','人民币','支付','购买商品','购买宝石','购买萝卜','前往商城','前往商店'))
            and not (dismissible_home is not None and dismissible_home[0]=='pass_renewal_cancel')):
        return {**result,'reason':'resource_action_outside_cut_scope'}
    if ''.join(text.split()) in {'加载中','连接中'}:
        return {**result,'state':'wait','reason':'restart_loading','next_phase':phase}
    loading_caption=any(re.fullmatch(r'加载中[.．。…]*',t['text'])
        and t['bbox'][0]>400 and t['bbox'][1]>1150 for t in tokens)
    if SafeAdvancePlanner._looks_like_white_loading_tips(image):
        if not loading_caption:
            # Animated dots can lower the whole-line confidence. Read only the
            # three caption characters in the same verified loading layout.
            from core.auto_trainer.main_menu_state_provider import _CALENDAR_REGION_READER
            caption,confidence=_CALENDAR_REGION_READER.read_line(image[1215:1260,510:610])
            loading_caption=str(caption).replace(' ','')=='加载中' and confidence>=.9
        if loading_caption:
            return {**result,'state':'wait','reason':'restart_loading_tips','next_phase':phase}

    def action(state,labels,next_phase,**extra):
        center=visible_control(tokens,boxes,set(labels))
        if center is None:
            return {**result,'reason':state+'_control_unverified'}
        return {**result,'state':state,'reason':state,'center':[round(center[0]*scale),round(center[1]*scale)],
            'next_phase':next_phase,**extra}

    # Refill is permitted only while starting the next ordinary cut attempt.
    tp=('TP' in text or '训练值' in text or '训练点' in text)
    if phase in {'setup','starting','refill','opening'} and tp and refill_entry_verified:
        if any(t in text for t in ('训练值不足','TP不足','是否恢复训练值','是否回复训练值','是否进行回复','是否进行恢复')) and not any(t in text for t in ('竞技值不足','RP不足')):
            return action('open_tp_refill',('恢复','回复','确定','OK'),'refill')
        if any(t in text for t in ('恢复TP','回复TP','恢复训练值','回复训练值')):
            # The legacy full-page layout uses the same resource controls and
            # optional-inventory policy as the foreground modal reader.
            refill=analyze_tp_controls(image,foreground={'tokens':tokens,'bbox':[0,0,720,1280],'header_bottom':0},
                boxes=boxes,remaining=remaining,selected_resource_kind=selected_resource_kind,
                returning=phase=='refill_return' or (refill_selection or {}).get('stage')=='confirmed',
                medicine_enabled=config.max_tp_items>0,carrots_enabled=config.max_carrots is None or config.max_carrots>0,
                refill_selection=refill_selection,unavailable_resources=unavailable_resources)
            if refill.get('center'):refill['center']=[round(v*scale) for v in refill['center']]
            return {**result,**refill}
    if phase=='opening' and ordinary_start_verified and '最终确认' in text:
        # The submitted ordinary start may leave its previous confirmation
        # visible while the network/opening transition completes. Read only.
        top=''.join(t['text'] for t in tokens if t['bbox'][1]<500)
        autonomous=[t for t in tokens if t['text'] in {'自主训练养成','自主训练育成','自主育成','自主训练'}]
        if len(autonomous)==1 and _selected_mode_visible(image,autonomous[0]):
            return {**result,'reason':'ordinary_mode_not_selected'}
        if expected_character_name(config) in top and any(term in top for term in ('凯旋门',"ProjectL'Arc",'ProjectL’Arc')):
            # Ordinary Start already has its receipt. A fading selector is
            # presentation only here: wait; never submit Start a second time.
            return {**result,'state':'wait','reason':'ordinary_start_confirmation_transition','next_phase':'opening',
                'mode_basis':'previously_verified_ordinary_start'}
    if phase=='abandon' or abandon_pending:
        if config.discard_found_for_validation and '优俊少女详情' in text and '状态' in text:
            status=analyze_cut_details(path,expected_name=expected_character_name(config))
            if status.get('state')!='present' or status.get('trainee_name')!=expected_character_name(config):
                return {**result,'reason':'validation_cut_details_unverified'}
            return action('close_found_status',('关闭','關閉'),'abandon')
        foreground=_foreground(image,reader,titles={'菜单','放弃'})
        if foreground is not None and foreground.get('tokens'):
            current_tokens=foreground['tokens'];foreground_text=''.join(t['text'] for t in current_tokens)
            x0,y0,x1,y1=foreground['bbox'];header_bottom=foreground['header_bottom']
            # Never match the green title or a blurred button behind this modal.
            tokens=[t for t in current_tokens if t['bbox'][1]>=header_bottom]
            boxes=[b for b in boxes if x0<=b[0] and header_bottom<=b[1] and b[0]+b[2]<=x1 and b[1]+b[3]<=y1]
            if all(term in foreground_text for term in ('即将放弃养成','是否确认执行','当前数据会被删除','已消耗的训练值不会返还','取消')):
                return action('confirm_abandon',('放弃',),'setup',foreground_bbox=foreground['bbox'],
                    abandon_confirmation_verified=True)
            if phase=='abandon' and '菜单' in foreground_text and all(term in foreground_text for term in ('保存并退出','放弃','关闭')):
                return action('choose_abandon',('放弃',),'abandon',foreground_bbox=foreground['bbox'])
            return {**result,'reason':'abandon_foreground_unverified'}
    if phase=='abandon':
        if any(t in text for t in ('是否放弃','放弃本次育成','放弃当前育成','结束本次育成','放弃本次养成','放弃当前养成','结束本次养成')) and ('取消' in text or '返回' in text):
            return action('confirm_abandon',('放弃','确定','结束育成','结束养成'),'setup')
        if '菜单' in text and any(t in text for t in ('放弃育成','结束育成','放弃养成','结束养成','养成结束')):
            return action('choose_abandon',('放弃育成','结束育成','放弃养成','结束养成','养成结束'),'abandon')
        page_bytes=Path(path).read_bytes()
        normal_home=locate_ability_button(page_bytes)
        race_home=locate_race_ability_button(page_bytes) if normal_home['abstained'] else None
        if not normal_home['abstained'] or race_home is not None and not race_home['abstained']:
            center=menu_icon_center(image)
            if center:return {**result,'state':'open_career_menu','reason':'open_career_menu',
                'home_kind':'race_day' if normal_home['abstained'] else 'training',
                'center':[round(v*scale) for v in center],'next_phase':'abandon'}
            return action('open_career_menu',('菜单',),'abandon')
        return result
    if phase in {'setup','starting','refill','refill_return'}:
        if phase in {'setup','refill_return'}:
            home_popup=dismissible_home or _detect_dismissible_home_popup(tokens,boxes)
            if home_popup is not None:
                popup_kind,center=home_popup
                return {**result,'state':'dismiss_home_popup','reason':'dismiss_home_popup',
                    'center':[round(center[0]*scale),round(center[1]*scale)],
                    'next_phase':phase,'popup_kind':popup_kind}
            if ('获得了' in text
                    and not any(term in text for term in (
                        '购买', '使用', '消费', '消耗', '支付', '补充', '恢复', '不足', '商店', '招募', '重试', '继续吗',
                    ))):
                login_skip = SafeAdvancePlanner._received_login_reward_skip_point(
                    image,
                    {'buttons': []},
                    {'normalized_text': '\n'.join(t['text'] for t in tokens), 'resource_terms': []},
                )
                if login_skip is not None:
                    return {**result,'state':'dismiss_home_popup','reason':'dismiss_home_popup',
                        'center':[round(login_skip[0]*scale),round(login_skip[1]*scale)],
                        'next_phase':phase,'popup_kind':'login_reward_skip'}
        if support_preparation is not None:
            prepared=support_preparation.analyze(image,tokens,scale=scale,source=path)
            if prepared is not None:
                if prepared['state']=='support_ready':
                    return action('continue_support',('开始育成','育成开始','开始养成'),'starting',
                        support_observation={**prepared['support_observation'],'source_sha256':result['source_sha256']})
                return {**result,**prepared}
        if '选择好友协助卡' in text:
            friend=choose_friend_support(image,borrow_target)
            if friend.get('center'):friend['center']=[round(value*scale) for value in friend['center']]
            if friend.get('gesture'):friend['gesture']=[round(value*scale) for value in friend['gesture']]
            return {**result,**friend}
        if '最终确认' in text:
            normal=[t for t in tokens if t['text'] in {'普通养成','普通育成'}]
            autonomous=[t for t in tokens if t['text'] in {'自主训练养成','自主训练育成','自主育成','自主训练'}]
            normal_selected=len(normal)==1 and _selected_mode_visible(image,normal[0])
            autonomous_selected=len(autonomous)==1 and _selected_mode_visible(image,autonomous[0])
            if autonomous_selected and not normal_selected:
                return {**result,'reason':'ordinary_mode_not_selected'}
            if visible_control(tokens,boxes,{'开始育成','育成开始','开始养成'}) is None:
                return {**result,'state':'wait','reason':'final_confirm_content_loading','next_phase':phase}
            expected=expected_character_name(config)
            top=''.join(t['text'] for t in tokens if t['bbox'][1]<500)
            if expected not in top:
                names=[t['text'] for t in tokens if 150<=t['bbox'][0]<510 and 365<=t['bbox'][1]<435
                    and re.fullmatch(r'[\u4e00-\u9fff·・]{2,20}',t['text'])]
                if names:return {**result,'reason':'final_confirm_character_mismatch','observed_character':names}
                return {**result,'reason':'final_confirm_character_unreadable'}
            if not any(term in top for term in ('凯旋门',"ProjectL'Arc",'ProjectL’Arc')):
                if any(term in top for term in ('URA','青春杯','巅峰杯','大师杯','女神杯','U.A.F')):
                    return {**result,'reason':'final_confirm_scenario_not_larc'}
                return {**result,'reason':'final_confirm_scenario_unreadable'}
            if not normal_selected or autonomous_selected:
                return {**result,'reason':'ordinary_mode_unreadable'}
            if not check_start_tp:
                # This caller relies on the game's own insufficient-TP popup.
                # Medicine/carrot limits are still handled by the refill owner.
                return action('confirm_ordinary_start',('开始育成','育成开始','开始养成','确定','OK'),
                    'opening',ordinary_start_verified=True)
            localized=tuple((t['text'],tuple(round(v) for v in t['bbox'])) for t in tokens)
            equation=re.search(r'(\d{1,3})[→>➡]+(\d{1,3})',text)
            cost_match=re.search(r'消耗(?:训练值|TP|训练点)?[:：]?(\d{1,3})',text)
            verified=CareerSetupPerceptor._training_points_equation(localized)
            cost=re.search(r'消耗(\d{1,3})(?:训练值|TP|训练点)',text)
            balance_tokens=sorted((t for t in tokens if 1080<=t['bbox'][1]<=1140 and re.fullmatch(r'\d{1,3}',t['text'])),key=lambda t:t['bbox'][0])
            balance_text=''.join(t['text'] for t in tokens if 1080<=t['bbox'][1]<=1140)
            balance_equation=re.search(r'(\d{1,3})[→>➡]+(\d{1,3})',balance_text)
            before_tokens=[t for t in balance_tokens if (t['bbox'][0]+t['bbox'][2])/2<415]
            available=int(balance_equation[1]) if balance_equation else int(before_tokens[0]['text']) if len(before_tokens)==1 else None
            if cost and len(balance_tokens)==1 and balance_tokens[0]['bbox'][2]-balance_tokens[0]['bbox'][0]>=110:
                digits=balance_tokens[0]['text']
                # RapidOCR merges both sides of the arrow. For an insufficient
                # balance the UI clips the after balance to zero (10→0 = 100).
                # Require a unique canonical split, a wide arrow-spanning token,
                # and a left balance below the separately read ordinary TP cost.
                splits={int(digits[:split]) for split in range(1,len(digits))
                    if digits[split:]=='0' and str(int(digits[:split]))==digits[:split]
                    and int(digits[:split])<int(cost[1])}
                available=next(iter(splits)) if len(splits)==1 else None
            if cost and available is not None and available<int(cost[1]):
                if not any(remaining.values()):return {**result,'reason':'tp_refill_limit'}
                return action('request_tp_refill',('开始育成','育成开始','开始养成'),'refill',ordinary_start_verified=True,
                    observed_tp_before=available,observed_tp_cost=int(cost[1]))
            if not verified and (not equation or not cost_match or int(equation[1])-int(equation[2])!=int(cost_match[1]) or int(equation[2])<0):
                return {**result,'reason':'ordinary_start_tp_equation_unverified'}
            return action('confirm_ordinary_start',('开始育成','育成开始','开始养成','确定','OK'),'opening',ordinary_start_verified=True)
        if any(t in text for t in ('自主训练','自主育成')) and '普通' not in text:
            return {**result,'reason':'autonomous_training_not_ordinary_career'}
        if any(t in text for t in ('再次育成','沿用上次设置','使用上次设置')):
            return action('reuse_previous_setup',('再次育成','沿用上次设置','使用上次设置'),'setup',configuration_reused=True)
        if any(term in text for term in ('育成剧本','选择剧本','剧本选择','选择养成剧本','选择育成剧本')):
            if visible_control(tokens,boxes,{'下一步','继续'}) is None:
                return {**result,'state':'wait','reason':'scenario_select_content_loading','next_phase':phase}
            if not ('凯旋门' in text or 'ProjectL’Arc' in text or "ProjectL'Arc" in text):
                return {**result,'reason':'selected_scenario_not_larc'}
            if references and 'scenario' in references and not retained_reference_matches(image,'scenario',references):
                return {**result,'reason':'selected_larc_scenario_unverified'}
            return action('continue_scenario',('下一步','继续'),'setup')
        filled_parent_cards = (all(term in text for term in ('第1人','第2人','契合度','继承技能'))
            and sum(token['text']=='更改' for token in tokens) == 2)
        if filled_parent_cards or '继承' in text and any(t in text for t in ('选择继承','继承优俊少女')):
            continued=SafeAdvancePlanner._inheritance_select_continue_button(Perception().analyze_image(image),image)
            missing=any(t in text for t in ('请选择继承','请选择优俊少女'))
            if continued is None and not missing:
                point=visible_control(tokens,boxes,{'继续'})
                changes=[t for t in tokens if t['text']=='更改' and t['bbox'][1]>500]
                centers=sorted(((t['bbox'][0]+t['bbox'][2])/2,(t['bbox'][1]+t['bbox'][3])/2) for t in changes)
                selected=(len(centers)==2 and centers[0][0]<360<centers[1][0]
                    and abs(centers[0][1]-centers[1][1])<=30
                    and all(label in text for label in ('第1人','第2人')))
                if selected and point and _green_enabled(image,point,boxes):continued=point
            if not missing and continued is None:
                return {**result,'state':'wait','reason':'inheritance_select_content_loading','next_phase':phase}
            if missing or (references and 'inheritance' in references and not retained_reference_matches(image,'inheritance',references)):
                return {**result,'reason':'inheritance_missing_or_changed','needs_configuration':'种马'}
            return action('continue_inheritance',('下一步','继续'),'setup')
        if any(t in text for t in ('选择育成优俊少女','育成优俊少女选择','选择养成优俊少女','养成优俊少女选择')):
            if visible_control(tokens,boxes,{'下一步','继续'}) is None:
                return {**result,'state':'wait','reason':'trainee_select_content_loading','next_phase':phase}
            expected=expected_character_name(config)
            selected_name=any(expected in t['text'] and t['bbox'][1]<600 for t in tokens)
            if not selected_name or (references and 'trainee' in references and not retained_reference_matches(image,'trainee',references)):
                return {**result,'reason':'retained_character_version_unverified','needs_configuration':'角色及衣装'}
            return action('continue_trainee',('下一步','继续'),'setup')
        support_header=any(t in text for t in ('协助卡编成','辅助卡编成','支援卡编成','协助卡牌编成'))
        support_group=any(re.fullmatch(r'卡组\d+',t['text']) and 180<t['bbox'][1]<260 for t in tokens)
        if support_header or support_group:
            if _explicit_missing_own_support(text):
                return {**result,'reason':'support_self_cards_missing','needs_configuration':'自有卡组'}
            slots=support_slot_observation(image,tokens)
            result['support_slots']=slots
            if not support_header:
                # A new deck's title fades in after its card group and slots.
                # Bind this family to five visible slots, not its Home footer.
                if not slots['owned_full']:
                    return {**result,'state':'wait','reason':'support_select_content_loading','next_phase':phase}
            if '请选择好友' in text:
                return {**result,'state':'wait','reason':'support_friend_toast_loading','next_phase':phase}
            empty=empty_friend_slot(image,tokens)
            if empty is not None:
                if borrow_target is None:return {**result,'reason':'borrow_support_target_missing','needs_configuration':'已记录的借卡目标'}
                if not slots['owned_full']:
                    return {**result,'reason':'support_self_cards_unreadable'}
                return {**result,'state':'open_friend_support','reason':'open_friend_support',
                    'center':[round(value*scale) for value in empty],'next_phase':'setup','borrow_target':borrow_target}
            if visible_control(tokens,boxes,{'开始育成','育成开始','开始养成'}) is None:
                return {**result,'state':'wait','reason':'support_select_content_loading','next_phase':phase}
            occupied=slots['full']
            if SafeAdvancePlanner._support_deck_empty_friend_slot_button(image) is not None or (references and 'support' in references and not retained_reference_matches(image,'support',references)):
                return {**result,'reason':'support_deck_missing_or_changed','needs_configuration':'卡组'}
            if not occupied:return {**result,'reason':'support_self_cards_unreadable'}
            return action('continue_support',('开始育成','育成开始','开始养成'),'starting')
        if (phase in {'setup','refill_return'} and ('育成' in text or '养成' in text) and any(t in text for t in ('主页','商店','任务'))
                and not any(t in text for t in ('卡组','自动编成','效果一览','开始养成','开始育成','育成开始'))):
            home_hud=_home_hud_verified(image,tokens,text)
            if home_hud and any(t['text']=='主页' and t['bbox'][1]>1150 for t in tokens):
                # The demonstrated home uses artwork around the large career
                # label, not a rectangular action button. Bind its exact label
                # and green surface to the verified post-refill home only.
                labels=[t for t in tokens if t['text'] in {'育成','养成'} and t['bbox'][0]>380 and 1000<t['bbox'][1]<1150 and t['bbox'][3]-t['bbox'][1]>=35]
                if len(labels)==1:
                    x0,y0,x1,y1=map(round,labels[0]['bbox']);crop=image[y0-10:y1+10,x0-10:x1+10]
                    green=cv2.inRange(cv2.cvtColor(crop,cv2.COLOR_BGR2HSV),(30,60,90),(90,255,255))
                    if float(np.mean(green>0))>.18:
                        return {**result,'state':'enter_ordinary_career','reason':'verified_post_refill_home_career_artwork',
                            'center':[round((x0+x1)/2*scale),round((y0+y1)/2*scale)],'next_phase':'setup','global_home_verified':True}
            entry=action('enter_ordinary_career',('育成','养成'),'setup',
                global_home_verified=bool(home_hud and any(t['text']=='主页' and t['bbox'][1]>1150 for t in tokens)))
            if entry['state']!='unknown':return entry
        portal = global_home_portal_point(image) if phase in {'setup','refill_return'} else None
        if portal is not None:
            return {**result,'state':'enter_ordinary_career','reason':'verified_global_home_portal',
                'center':[round(value*scale) for value in portal], 'next_phase':'setup',
                'global_home_verified':True}
        if (phase in {'setup','refill_return'} and _home_hud_verified(image,tokens,text)
                and all(_home_layout_anchors(image)[1:])
                and any(t['text']=='主页' and t['bbox'][1]>1150 for t in tokens)):
            return {**result,'state':'wait','reason':'home_career_entry_loading','next_phase':phase}
    return result


def retained_reference_matches(image, key, references):
    """Only compare retained selected settings, never select/auto-form them."""
    if not references or key not in references:
        return False
    entry=references[key];reference=cv2.imread(str(entry['path']))
    if reference is None:return False
    reference=cv2.resize(reference,(720,1280))
    x0,y0,x1,y1=entry['roi']
    if not (0<=x0<x1<=720 and 0<=y0<y1<=1280):return False
    actual=image[y0:y1,x0:x1];expected=reference[y0:y1,x0:x1]
    # The selected card/parents/deck must be unchanged. Animated background and
    # resource balances belong outside these user-supplied selected-setting ROIs.
    return float(cv2.matchTemplate(actual,expected,cv2.TM_CCOEFF_NORMED)[0,0])>=.97


class CutRestartRoute:
    def __init__(self, session, *, adb_factory=None, perceptor=analyze_restart_frame, sleep=time.sleep,clock=time.monotonic,
                 on_submitted=None):
        self.session=session;self.adb_factory=adb_factory or (lambda ctx:adb_for_capture(ctx.port))
        self.perceptor,self.sleep=perceptor,sleep
        self.clock=clock
        self.on_submitted=on_submitted
        self.references=None
        if session.config.setup_reference_dir:
            directory=Path(session.config.setup_reference_dir)
            self.references=__import__('json').loads((directory/'selected_settings.json').read_text(encoding='utf-8'))
            self.references={key:{**value,'path':str(directory/value['path'])} for key,value in self.references.items()}

    def __call__(self, ctx, output, *, start_phase='abandon',stop_after_abandon=False,support_start_verified=False):
        if start_phase not in {'abandon','setup'}:raise ValueError('restart_start_phase_invalid')
        if stop_after_abandon and start_phase!='abandon':raise ValueError('abandon_only_requires_abandon_phase')
        if self.session.data['status'] in {'found','unknown','stopped'} or self.session.data.get('pending_action') or self.session.data.get('tp_refill_pending'):
            return {'ok':False,'reason':'restart_not_authorized_by_session'}
        output=Path(output)
        if output.exists() and any(output.iterdir()):return {'ok':False,'reason':'restart_output_already_exists'}
        output.mkdir(parents=True,exist_ok=True)
        frames=ExecutionFrames(self.adb_factory(ctx))
        if not frames.connect():return {'ok':False,'reason':'restart_connect_failed'}
        phase=start_phase;last_action_sha=None;last_action_state=None;ordinary_verified=False;waiting_until=None;selected_resource_kind=None
        refill_selection=None;unavailable_resources=set()
        borrow_target=self.session.data.get('legacy_borrow_target') or target_from_profile(ctx)
        if borrow_target and not self.session.config.borrow_card_id and 'legacy_borrow_target' not in self.session.data:
            self.session.data['legacy_borrow_target']=borrow_target;self.session.save()
        support_preparation=None
        if self.session.config.support_deck or self.session.config.borrow_card_id:
            from core.auto_trainer.cut_support_selection import SupportPreparation
            support_preparation=SupportPreparation(self.session.config,
                filter_cache=self.session.data.setdefault('support_filter_cache',{}))
        friend_scans=0;home_entry_retries=0;abandon_confirmation_retries=0;home_popup_dismissals=0
        transition_budget=TransitionBudget(clock=self.clock);ordinary_reads=0
        setup_navigation_states={'continue_scenario','continue_trainee','continue_inheritance'}
        navigation_budget=TransitionBudget(clock=self.clock,idle_seconds=30.,total_seconds=30.)
        navigation_reads=0;setup_unreadable_reads=0
        for step in range(512):
            source=output/f'frame_{step:02d}.png';image=frames.screenshot(str(source))
            if image is None:return {'ok':False,'reason':'restart_capture_failed'}
            captured_at=self.clock()
            abandon_pending=phase=='setup' and last_action_state=='confirm_abandon'
            retrying_abandon=False
            navigation_pending=phase=='setup' and last_action_state in setup_navigation_states
            # Time belongs to the captured frame, not to when slow OCR finishes.
            # An old frame read before expiry gets a fresh read even if its
            # recognition finishes after the deadline.
            navigation_frame_in_budget=not navigation_budget.expired if navigation_pending else False
            sha=hashlib.sha256(source.read_bytes()).hexdigest()
            if sha==last_action_sha and not navigation_pending and not abandon_pending and last_action_state not in {'inspect_trainee','minimize_tp_refill','select_one_tp_refill'} and not (phase=='opening' and ordinary_verified) and not (support_preparation and support_preparation.pending):
                if waiting_until and self.clock()<waiting_until:self.sleep(.25);continue
                # Pending abandon is re-observed below. Here only the global
                # home entry can retry; unchanged purchase/Start inputs stop.
                if last_action_state!='enter_ordinary_career' or home_entry_retries:
                    return {'ok':False,'reason':'restart_action_no_progress'}
            if navigation_pending and sha==last_action_sha:
                observed={'state':'wait','reason':'setup_navigation_unchanged_frame',
                    'source':str(source),'source_sha256':sha}
            else:
                observed=self.perceptor(source,config=self.session.config,phase=phase,references=self.references,
                    ordinary_start_verified=ordinary_verified,support_start_verified=support_start_verified,
                        remaining_resources=self.session.remaining_resources(),selected_resource_kind=selected_resource_kind,
                        borrow_target=borrow_target,check_start_tp=False,
                        **({'support_preparation':support_preparation} if support_preparation is not None else {}),
                        refill_selection=refill_selection,unavailable_resources=unavailable_resources,
                        **({'abandon_pending':True} if abandon_pending else {}))
            if (phase in {'setup','starting'} and observed.get('state')=='unknown'
                    and observed.get('reason') in SETUP_UNREADABLE_REASONS
                    and last_action_state not in {'confirm_tp_refill','confirm_ordinary_start','confirm_career_start'}):
                setup_unreadable_reads+=1
                observed['read_only_retry']=setup_unreadable_reads<SETUP_UNREADABLE_READ_LIMIT
                observed['read_count']=setup_unreadable_reads
                atomic_json(source.with_suffix('.json'),observed)
                if setup_unreadable_reads>=SETUP_UNREADABLE_READ_LIMIT:
                    return {'ok':False,'reason':observed['reason'],'observation':observed,'readiness_timeout':True}
                self.sleep(.25);continue
            if abandon_pending and observed.get('state')=='confirm_abandon':
                # A transport receipt is not proof that the modal accepted it.
                # Only this exact foreground confirmation can be retried, once,
                # from a frame captured after the original transition window.
                # Unknown/loading/resource screens remain read-only.
                if observed.get('abandon_confirmation_verified') is not True:
                    return {'ok':False,'reason':'abandon_confirmation_unverified','observation':observed}
                if waiting_until and captured_at<waiting_until:
                    atomic_json(source.with_suffix('.json'),{**observed,'state':'wait',
                        'reason':'abandon_confirmation_settling','observed_state':'confirm_abandon'})
                    self.sleep(.25);continue
                if abandon_confirmation_retries:
                    return {'ok':False,'reason':'abandon_confirmation_still_visible','observation':observed}
                abandon_confirmation_retries+=1
                observed['abandon_confirmation_retry']=abandon_confirmation_retries
                retrying_abandon=True
            if navigation_pending:
                transient=(observed.get('state') in {'wait',last_action_state}
                    or observed.get('state')=='unknown' and (
                        observed.get('reason') in {'restart_page_unrecognized',
                            'continue_scenario_control_unverified','continue_trainee_control_unverified',
                            'continue_inheritance_control_unverified'}
                        or last_action_state=='continue_scenario' and observed.get('reason')=='selected_scenario_not_larc'))
                if transient:
                    navigation_reads+=1
                    # Keep one budget across old-page redraws and loading;
                    # neither is permission to repeat the already sent tap.
                    waiting={**observed,'state':'wait','reason':'setup_navigation_transition',
                        'pending_navigation':last_action_state,'observed_state':observed.get('state'),
                        'observed_reason':observed.get('reason'),
                        'support_pending_action':support_preparation.pending[0] if (support_preparation and support_preparation.pending) else None,
                        'frame_in_wait_budget':navigation_frame_in_budget,
                        'recognition_seconds':round(self.clock()-captured_at,4)}
                    atomic_json(source.with_suffix('.json'),waiting)
                    if navigation_reads>=2 and not navigation_frame_in_budget:
                        return {'ok':False,'reason':'restart_navigation_timeout','observation':waiting}
                    self.sleep(.25);continue
                navigation_budget.reset();navigation_reads=0
            if observed.get('normal_stop') is True and not self.session.data.get('tp_refill_pending'):
                atomic_json(source.with_suffix('.json'),observed)
                return {'ok':False,**{key:observed[key] for key in
                    ('reason','normal_stop','resource_status','message') if key in observed},'observation':observed}
            if observed.get('state') == 'existing_career_handoff':
                return {'ok':True, 'reason':'existing_career_handoff', 'source':str(source),
                    'new_career_started':False}
            if observed.get('state')=='wait':
                transition_budget.observe(key='restart_opening' if phase=='opening' and ordinary_verified else 'restart_loading')
                atomic_json(source.with_suffix('.json'),observed)
                if transition_budget.expired:return {'ok':False,'reason':'restart_transition_timeout','observation':observed}
                waiting_until=self.clock()+5
                self.sleep(.25);continue
            if (phase=='starting' and last_action_state=='continue_support'
                    and observed.get('state')=='unknown' and observed.get('reason')=='ordinary_start_tp_equation_unverified'):
                # The support-page tap feedback can cover the TP preview.
                # Wait for a readable equation, never retry Continue or Start.
                transition_budget.observe(key='ordinary_start_equation_ready')
                atomic_json(source.with_suffix('.json'),observed)
                if transition_budget.expired:
                    return {'ok':False,'reason':observed['reason'],'observation':observed,'readiness_timeout':True}
                self.sleep(.25);continue
            if (phase=='setup' and last_action_state=='confirm_abandon'
                    and observed.get('state')=='unknown' and observed.get('reason')=='restart_page_unrecognized'):
                # Abandon is already submitted. The home may still be entering
                # after its loading caption disappears; wait without any input.
                transition_budget.observe(key='abandon_destination_loading')
                atomic_json(source.with_suffix('.json'),{**observed,'state':'wait',
                    'reason':'abandon_destination_loading'})
                if transition_budget.expired:
                    return {'ok':False,'reason':'restart_transition_timeout','observation':observed}
                self.sleep(.25);continue
            opening_unrecognized=(phase=='opening' and ordinary_verified and observed.get('state')=='unknown'
                and observed.get('reason')=='restart_page_unrecognized')
            if not opening_unrecognized:transition_budget.reset()
            # D1 bounds its support search itself (scrolls/refreshes/filters).
            # Do not consume the unrelated ordinary-page routing budget here.
            if not observed.get('support_action') and not observed.get('trainee_action'):ordinary_reads+=1
            if ordinary_reads>64:return {'ok':False,'reason':'restart_step_limit'}
            if self.session.data.get('tp_refill_pending'):
                receipt=(observed.get('state') in {'close_tp_result','close_tp_menu','dismiss_tp_unavailable'}
                    or observed.get('state')=='enter_ordinary_career' and observed.get('global_home_verified') is True)
                # One resource confirmation is already reserved/submitted.
                # Neither a changing old dialog nor a menu can request it twice.
                if not receipt:
                    if waiting_until and self.clock()<waiting_until:self.sleep(.25);continue
                    return {'ok':False,'reason':'tp_refill_result_unverified','observation':observed}
                self.session.data['tp_refill_pending']['receipt']=observed.get('refill_receipt') or observed['state']
                self.session.data.setdefault('tp_refill_receipts',[]).append(self.session.data.pop('tp_refill_pending'))
                self.session.save()
                refill_selection=None
            if phase=='opening' and observed.get('state')=='unknown':
                if observed.get('reason')!='restart_page_unrecognized':
                    return {'ok':False,'reason':observed['reason'],'observation':observed}
                # Once the ordinary start is confirmed, existing L'Arc routing
                # owns opening events/shortening settings and the first home.
                opening=object.__new__(CareerSetupPerceptor)
                opening._screen_text_reader=__import__('core.auto_trainer.frame_safety',fromlist=['LocalRapidOCRScreenTextReader']).LocalRapidOCRScreenTextReader().read_regions
                packet=opening.analyze_career_opening(source.read_bytes())
                if packet.errors:
                    settings=opening.analyze_shorten_settings(source.read_bytes())
                    if not settings.errors:packet=settings
                if packet.errors and analyze_ability_details_button_anchor(source.read_bytes())['abstained']:
                    # Start has already been submitted. Old setup frames and
                    # opening animation share the same read-only budget; slow
                    # recognition cannot consume a five-second input window.
                    transition_budget.observe(key='restart_opening')
                    atomic_json(source.with_suffix('.json'),{**observed,'state':'wait','reason':'ordinary_start_destination_loading'})
                    if transition_budget.expired:
                        return {'ok':False,'reason':'restart_transition_timeout','source':str(source),'observation':observed}
                    self.sleep(.25);continue
                self.session.data['pending_action']=None;self.session.save()
                return {'ok':True,'reason':'ordinary_start_confirmed','source':str(source)}
            atomic_json(source.with_suffix('.json'),observed)
            if observed.get('state')=='unknown':
                scenario_departing = (last_action_state=='continue_scenario'
                    and observed['reason']=='selected_scenario_not_larc')
                if waiting_until and self.clock()<waiting_until and (scenario_departing or observed['reason'] in {'restart_page_unrecognized','refill_page_unrecognized','refill_foreground_boundary_unknown','refill_confirm_disabled_or_unverified'} or observed['reason'].endswith('_control_unverified')):
                    self.sleep(.25);continue
                return {'ok':False,'reason':observed['reason'],'observation':observed}
            if stop_after_abandon:
                if observed['state']=='enter_ordinary_career':
                    if phase!='setup' or last_action_state!='confirm_abandon' or observed.get('global_home_verified') is not True:
                        return {'ok':False,'reason':'abandon_destination_unverified','observation':observed}
                    self.session.data['pending_action']=None;self.session.save()
                    return {'ok':True,'reason':'ordinary_abandon_confirmed','source':str(source)}
                if observed['state'] not in {'open_career_menu','choose_abandon','confirm_abandon'}:
                    return {'ok':False,'reason':'abandon_only_page_out_of_scope','observation':observed}
            if observed['state']=='dismiss_home_popup':
                if home_popup_dismissals>=3:
                    return {'ok':False,'reason':'restart_action_repeated_surface','observation':observed}
                home_popup_dismissals+=1
            elif observed['state']==last_action_state and not retrying_abandon and observed['state']!='scroll_friend_support' and not observed.get('support_action') and not observed.get('trainee_action'):
                if waiting_until and self.clock()<waiting_until:self.sleep(.25);continue
                if (phase=='setup' and observed['state']=='enter_ordinary_career'
                        and observed.get('global_home_verified') is True and not home_entry_retries
                        and not observed.get('resource_kind') and observed.get('center')):
                    home_entry_retries=1
                    observed['navigation_retry']=1
                    atomic_json(source.with_suffix('.json'),observed)
                else:
                    return {'ok':False,'reason':'restart_action_repeated_surface','observation':observed}
            try:
                # Accept pause before reserving a resource or submitting input.
                # If the player changed the page, discard these coordinates and
                # let this existing route read the new page on its next loop.
                with input_guard(frames.adb):
                    self.session.data['pending_action']=None;self.session.save()
                    if observed.get('support_observation'):
                        self.session.data['support_setup_observation']=observed['support_observation']
                        self.session.save()
                    if observed.get('resource_kind'):
                        self.session.reserve_resource(observed['resource_kind'],observed['resource_amount'])
                        self.session.data['tp_refill_pending']={'kind':observed['resource_kind'],
                            'amount':observed['resource_amount'],'source_sha256':sha,'selected_units':1}
                    self.session.data['pending_action']={'state':observed['state'],'source_sha256':sha,'source':str(source)}
                    if observed.get('ordinary_start_verified'):
                        self.session.data['ordinary_start_submitted']=True
                    self.session.save()
                    if (observed.get('support_action') or observed.get('trainee_action')) and observed.get('gesture'):
                        succeeded=frames.swipe(*observed['gesture'],observed.get('duration_ms',1100),scaled=False)
                    elif observed['state']=='scroll_friend_support':
                        if friend_scans>=12:return {'ok':False,'reason':'borrow_support_not_visible_after_bounded_scan'}
                        friend_scans+=1
                        succeeded=frames.swipe(*observed['gesture'],observed.get('duration_ms',400),scaled=False)
                    else:succeeded=frames.tap(*observed['center'],scaled=False)
            except Reobserve:
                if support_preparation is not None:
                    support_preparation.reset_pending(clear_selection_state=True)
                transition_budget.reset();navigation_budget.reset();navigation_reads=0;waiting_until=None
                continue
            if succeeded is not True:
                return {'ok':False,'reason':'restart_input_unconfirmed','observation':observed}
            if support_preparation is not None:
                if observed.get('support_action'):
                    support_preparation.submitted(observed['support_action'])
                else:
                    left_support_flow = observed.get('state') in {
                        'enter_ordinary_career', 'reuse_previous_setup',
                        'continue_scenario', 'next_scenario',
                        'continue_trainee', 'inspect_trainee', 'scroll_trainees', 'ordinary_back_to_trainee',
                        'continue_inheritance', 'confirm_abandon',
                        'open_tp_refill', 'close_tp_result', 'close_tp_menu',
                    }
                    support_preparation.reset_pending(clear_selection_state=left_support_flow)
            if self.on_submitted is not None:
                self.on_submitted(observed)
            last_action_sha=sha;last_action_state=observed['state'];phase=observed['next_phase']
            setup_unreadable_reads=0
            waiting_until=self.clock()+5
            navigation_budget.reset();navigation_reads=0
            if phase=='setup' and last_action_state in setup_navigation_states:
                navigation_budget.observe(key='setup_navigation')
            ordinary_verified=ordinary_verified or observed.get('ordinary_start_verified',False)
            # Low TP can be reported immediately after the support-deck Start,
            # before the final page where ordinary mode is checked separately.
            support_start_verified=support_start_verified or observed['state']=='continue_support'
            if phase=='opening' and ordinary_verified:transition_budget.observe(key='restart_opening')
            if observed['state'] in {'choose_tp_item','choose_tp_carrots'}:
                selected_resource_kind='tp_items' if observed['state']=='choose_tp_item' else 'carrots'
                refill_selection={'kind':selected_resource_kind,'stage':'opened'}
            elif observed['state']=='minimize_tp_refill':
                selected_resource_kind=observed['selected_resource_kind']
                refill_selection={'kind':selected_resource_kind,'stage':'minimum'}
            elif observed['state']=='select_one_tp_refill':
                selected_resource_kind=observed['selected_resource_kind']
                refill_selection={'kind':selected_resource_kind,'stage':'one'}
            elif observed['state']=='confirm_tp_refill':
                selected_resource_kind=observed['resource_kind']
                refill_selection={'kind':selected_resource_kind,'stage':'confirmed'}
            elif observed['state']=='dismiss_tp_unavailable':
                unavailable_resources.add(observed['unavailable_resource_kind'])
                refill_selection=None;selected_resource_kind=None
            elif observed.get('selected_resource_kind'):selected_resource_kind=observed['selected_resource_kind']
            self.sleep(.5)
        return {'ok':False,'reason':'restart_step_limit'}
