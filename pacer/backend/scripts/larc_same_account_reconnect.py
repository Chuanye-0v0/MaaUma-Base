"""Bounded same-account navigation back to the existing career, never a start."""
from pathlib import Path
import json
import math
import time
import cv2
import numpy as np

from core.auto_trainer.safe_advance import SafeAdvancePlanner, SafeAdvanceExecutor, ADVANCE_CONFIRM_TOKEN
from core.native_capture_adb import adb_for_capture
from core.auto_trainer.frame_safety import FrameSafetyAnalyzer
from core.auto_trainer.global_home_layout import home_layout_anchors
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json


NAVIGATION = frozenset({"return_title_from_connection_conflict", "tap_title_start",
    "enter_training_from_home", "continue_training_modal", "skip_received_login_reward",
    "close_announcement", "close_exchange_points_expiry", "dismiss_global_home_greeting"})
NAVIGATION = NAVIGATION | {"accept_authorized_bilibili_sdk_terms"}
CAREER_READY = frozenset({"ready_main_menu", "ready_training_menu", "ready_larc_ss_preview",
    "race_day_main_menu",
    "event_choice", "skill_acquisition_handoff", "career_end_confirmation",
    "enable_shorten_settings_skip_toggle", "select_shorten_all_events",
    "uncheck_do_not_shorten_unread", "confirm_shorten_all_events",
    "skip_bottom_dialogue", "advance_dialogue", "enable_bottom_skip_toggle"})


def bilibili_sdk_terms_point(image, text):
    compact = ''.join(str(text).split())
    if ('用户协议与隐私政策' not in compact
            or not any(provider in compact.lower() for provider in ('bilibili','哔哩哔哩'))
            or '同意' not in compact or image is None or image.shape[1]>=image.shape[0]):
        return None
    height,width=image.shape[:2]
    hsv=cv2.cvtColor(cv2.resize(image,(720,1280)),cv2.COLOR_BGR2HSV)
    body=hsv[210:950,85:635];button=hsv[1008:1048,390:540]
    white=(body[:,:,1]<35)&(body[:,:,2]>200)
    blue=(button[:,:,0]>=85)&(button[:,:,0]<=110)&(button[:,:,1]>90)&(button[:,:,2]>150)
    if float(white.mean())<.65 or float(blue.mean())<.70:return None
    return round(468*width/720),round(1028*height/1280)


class ReconnectPlanner(SafeAdvancePlanner):
    def __init__(self, *, require_existing_career=False, allow_bilibili_sdk_terms=False):
        super().__init__()
        self.require_existing_career = require_existing_career
        self.allow_bilibili_sdk_terms = allow_bilibili_sdk_terms
        self.return_submitted = False
        self.title_start_submitted = False
        self.submitted_navigation = set()

    def plan(self, image_path, **kwargs):
        decision = super().plan(image_path, **kwargs)
        text = str(decision.get("evidence", {}).get("screen_text", {}).get("normalized_text", ""))
        if any(term in text for term in ("选择账号", "更换账号", "输入密码", "手机号登录")):
            return self._stop("reconnect_manual_account_login_required", evidence=decision.get("evidence", {}))
        if decision.get("reason") == "resource_prompt":
            terms=set(decision.get('evidence',{}).get('screen_text',{}).get('resource_terms',()))
            anchors=home_layout_anchors(cv2.imread(str(image_path)))
            fading_home=(all(anchors) or (anchors[1] and anchors[2]
                and all(label in text for label in ('主页','强化编成','剧情'))))
            if ('continue_training_modal' in self.submitted_navigation
                    and terms and terms.issubset({'TP','RP','商店','招募'})
                    and fading_home):
                # The acknowledged free Continue can fade over the home HUD.
                # Only observe; never submit Continue again or authorize cost.
                return {'action':'wait','reason':'reconnect_navigation_transition','route_state':'loading',
                    'evidence':decision.get('evidence',{})}
            if ('enter_training_from_home' in self.submitted_navigation
                    and terms.issubset({'TP','RP','商店','招募'})):
                probe_kwargs={**kwargs,'visible_text':None,'allow_enter_training_from_global_home':True}
                home=super().plan(image_path,**probe_kwargs)
                if home.get('reason')=='enter_training_from_home':
                    return {'action':'wait','reason':'reconnect_navigation_transition','route_state':'loading',
                        'evidence':decision.get('evidence',{})}
            return decision
        image = cv2.imread(str(image_path))
        if self.allow_bilibili_sdk_terms:
            terms = bilibili_sdk_terms_point(image,text)
            if terms is not None:
                return self._tap_decision('accept_authorized_bilibili_sdk_terms',
                    'safe_advance.bilibili_sdk_terms_accept',terms,decision.get('evidence',{}))
        point = self._connection_conflict_return_title_button(image) if image is not None else None
        if point is not None:
            if self.title_start_submitted:
                return self._stop("reconnect_repeated_account_conflict", evidence=decision.get("evidence", {}))
            if self.return_submitted:
                return {"action":"wait","reason":"reconnect_title_transition","route_state":"loading",
                    "evidence":decision.get("evidence", {})}
            return self._tap_decision("return_title_from_connection_conflict", "safe_advance.return_title",
                point, decision.get("evidence", {}))
        if self.title_start_submitted and '正在启动游戏' in ''.join(text.split()):
            return {'action':'wait','reason':'reconnect_cached_login_loading','route_state':'loading',
                'evidence':decision.get('evidence',{})}
        if decision.get("reason") in CAREER_READY:
            return self._stop("reconnect_existing_career_ready", evidence=decision.get("evidence", {}))
        if decision.get("reason") in self.submitted_navigation and decision.get("action") == "tap":
            return {"action":"wait","reason":"reconnect_navigation_transition","route_state":"loading",
                "evidence":decision.get("evidence", {})}
        if decision.get("action") == "wait" or decision.get("reason") in NAVIGATION:
            return decision
        return self._stop("reconnect_existing_career_not_visible", evidence=decision.get("evidence", {}))


def _app_startup_wait_allowed(preview, packet, planner, *, require_existing_career,
                              wait_seconds, elapsed):
    # A fresh force-start can show the intro before title controls. Waiting
    # here is bounded and never grants an unknown page any input action.
    return bool(require_existing_career and wait_seconds > 0 and 0 <= elapsed < wait_seconds
        and not planner.title_start_submitted and not planner.submitted_navigation
        and preview.get('action') == 'stop'
        and preview.get('reason') == 'reconnect_existing_career_not_visible'
        and not packet.dangerous)


def reconnect_existing_career(ctx, output, *, executor_factory=SafeAdvanceExecutor,
        title_start_submitted=False, require_existing_career=False, allow_bilibili_sdk_terms=None,
        continue_training_submitted=False, adb=None, sleep_fn=None, clock_fn=None,
        app_startup_wait_seconds=0):
    if (type(app_startup_wait_seconds) not in {int,float}
            or not math.isfinite(app_startup_wait_seconds)
            or not 0 <= app_startup_wait_seconds <= 20):
        raise ValueError('reconnect_app_startup_wait_invalid')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    report = {"run_session_id":ctx.run_id,"device":ctx.progress["device"],"ok":False,
        "new_career_started":False,"tp_start_submitted":False,"records":[]}
    if allow_bilibili_sdk_terms is None:
        permission=Path(ctx.root)/'session/ordinary_navigation_authorization.json'
        allowed=json.loads(permission.read_text(encoding='utf-8')) if permission.is_file() else {}
        allow_bilibili_sdk_terms=allowed.get('allow_bilibili_sdk_terms') is True
    planner = ReconnectPlanner(require_existing_career=require_existing_career,
        allow_bilibili_sdk_terms=allow_bilibili_sdk_terms)
    if title_start_submitted:
        planner.title_start_submitted = True
        planner.submitted_navigation.add("tap_title_start")
    if continue_training_submitted:
        planner.submitted_navigation.update({'enter_training_from_home', 'continue_training_modal'})
    adb = adb or adb_for_capture(ctx.port)
    sleep = sleep_fn or time.sleep
    clock = clock_fn or time.monotonic
    if not adb.connect():
        report["reason"] = "reconnect_capture_connection_failed"
        _atomic_write_json(output/"report.json",report)
        return report
    safety = getattr(ctx, "screen_reader", None) or FrameSafetyAnalyzer()
    executor = executor_factory(planner=planner, adb_factory=lambda:adb, safety_analyzer=safety)
    submitted = set()
    started = clock()
    deadline = started + 75
    # Opted-in startup waiting must not be truncated to the old 30 x .3s
    # poll count. Both the startup duration and absolute deadline stay bounded.
    max_observations = 30 + math.ceil(app_startup_wait_seconds / .3)
    for index in range(max_observations):
        path = output/f"step_{index:02d}.png"
        image = adb.screenshot(str(path))
        if image is None:
            report["reason"] = "reconnect_capture_failed"
            break
        packet = safety.analyze(path)
        report['current_source'] = str(path)
        if require_existing_career and not packet.visible_text.strip() and (image.max(axis=2)<32).mean()>.98:
            report['records'].append({'performed':False,'reason':'reconnect_startup_black_wait',
                'decision':{'action':'wait'},'source':str(path)})
            _atomic_write_json(output/'report.json',report)
            if clock()>=deadline:
                report['reason']='reconnect_timeout';break
            sleep(.3);continue
        preview = planner.plan(path, scenario="larc", visible_text=packet.visible_text,
            allow_enter_training_from_global_home=True)
        if preview.get("reason") in {"global_home", "resource_prompt"}:
            preview = planner.plan(path, scenario="larc", allow_enter_training_from_global_home=True)
        if _app_startup_wait_allowed(preview,packet,planner,
                require_existing_career=require_existing_career,
                wait_seconds=app_startup_wait_seconds,elapsed=clock()-started):
            report['records'].append({'performed':False,'reason':'reconnect_app_startup_wait',
                'decision':{'action':'wait'},'source':str(path)})
            _atomic_write_json(output/'report.json',report)
            sleep(.3)
            continue
        if (preview.get('reason') == 'reconnect_existing_career_not_visible'
                and require_existing_career and planner.title_start_submitted
                and ('enter_training_from_home' not in planner.submitted_navigation
                     or 'continue_training_modal' in planner.submitted_navigation)
                and not packet.dangerous):
            # A cached login can fade from its comic into the home greeting
            # before any home controls render. Keep this transition read-only;
            # unknown UI must never authorize login, a new start, or an input.
            report['records'].append({'performed':False,'reason':'reconnect_post_login_home_loading',
                'decision':{'action':'wait'},'source':str(path)})
            _atomic_write_json(output/'report.json',report)
            if clock() >= deadline:
                report['reason'] = 'reconnect_timeout'; break
            sleep(.3); continue
        if (preview.get('reason')=='reconnect_existing_career_not_visible'
                and 'enter_training_from_home' in planner.submitted_navigation and not packet.dangerous):
            from core.auto_trainer.larc_control_wait import modal_content_pending
            if modal_content_pending(image) or '继续养成' in ''.join(packet.visible_text.split()):
                report['records'].append({'performed':False,'reason':'reconnect_continue_modal_loading',
                    'decision':{'action':'wait'},'source':str(path)})
                _atomic_write_json(output/'report.json',report)
                if clock()>=deadline:
                    report['reason']='reconnect_timeout';break
                sleep(.3);continue
        if (preview.get('reason')=='reconnect_existing_career_not_visible'
                and 'continue_training_modal' in planner.submitted_navigation and not packet.dangerous
                and all(term in packet.visible_text for term in ('目标','体力','还剩'))):
            report['records'].append({'performed':False,'reason':'reconnect_owned_career_loading',
                'decision':{'action':'wait'},'source':str(path)})
            _atomic_write_json(output/'report.json',report)
            if clock()>=deadline:
                report['reason']='reconnect_timeout';break
            sleep(.3);continue
        enter_home = preview.get("reason") == "enter_training_from_home"
        if (enter_home and require_existing_career
                and not (any(term in packet.visible_text for term in ("初级", "经典级", "高级"))
                         and any(term in packet.visible_text for term in ("出道前", "月", "远征")))):
            report["reason"] = "reconnect_existing_career_not_visible"
            break
        result = executor.run_once(mode="live",execute=True,confirm_token=ADVANCE_CONFIRM_TOKEN,
            scenario="larc",goal_completion_observe_only=True,screenshot_path=path, initial_frame=image,
            allow_enter_training_from_global_home=enter_home,
            expected_action_id="safe_advance.home_training" if enter_home else None,post_wait_freezes=False)
        report["records"].append(result)
        reason = result.get("reason")
        if reason == "reconnect_existing_career_ready":
            report.update(ok=True,reason=reason)
            break
        if result.get("performed") is not True:
            report["reason"] = reason
            break
        if result.get("tap_performed") or result.get("decision", {}).get("action") == "tap":
            if reason in submitted:
                report["reason"] = "reconnect_navigation_repeated"
                break
            submitted.add(reason)
            planner.submitted_navigation.add(reason)
            if reason == "return_title_from_connection_conflict": planner.return_submitted = True
            if reason == "tap_title_start": planner.title_start_submitted = True
        if clock() >= deadline:
            report["reason"] = "reconnect_timeout"
            break
        _atomic_write_json(output/"report.json",report)
        sleep(.3)
    else:
        report["reason"] = "reconnect_observation_limit"
    _atomic_write_json(output/"report.json",report)
    return report
