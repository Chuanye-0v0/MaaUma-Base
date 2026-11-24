"""RaceRouteExecutor with staged capture, scope checks, swipe/tap execution, and postconditions."""
from __future__ import annotations

import hashlib
import re
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from core.frame_io import read_image_with_retry
from core.auto_trainer.action_table import ActionTable
from core.auto_trainer.career_goal_perception import is_goal_achievement_title
from core.auto_trainer.larc_pages import (
    LARC_SYSTEM_GOAL_IDS,
    annotate_larc_race_page,
    larc_race_transition_kind,
    plan_larc_explanation,
)
from core.auto_trainer.frame_safety import FormalActionSafetyGate
from core.auto_trainer.runtime_control import reobserving_step, controlled_sleep
from core.auto_trainer.race_clock_retry import _ordinary_result_blocked, retry_prompt_visible
from core.auto_trainer.route_handoff import handoff, is_route_handoff
from types import SimpleNamespace
from core.auto_trainer.transition_wait import TransitionBudget
from ._compat import (
    FrameSafetyAnalyzer,
    LocalRapidOCRScreenTextReader,
    Perception,
    RaceRoutePlanner,
    SafeAdvancePlanner,
    TrainingMenuPrechecker,
    _current_clock_retry,
    analyze_screen_text,
    classify_landscape_race_screen,
    fuse_perception_with_text,
    match_main_menu_named_goal,
    rr_attr,
)
from .models import (
    RACE_ENTRY_POST_OBSERVATION_RETRIES,
    RACE_LIST_GRADE_REOBSERVATION_RETRIES,
    RACE_READ_ONLY_REOBSERVATION_DELAY_SECONDS,
    RACE_ROUTE_CONFIRM_TOKEN,
    RACE_SCENE_WAIT_REASONS,
    RESULT_TRANSITION_RECAPTURE_DELAY_SECONDS,
    RESULT_TRANSITION_RECAPTURE_LIMIT,
    SUPPORTED_RACE_ROUTE_SCREEN_SIZES,
    RaceRoutePolicy,
    RaceRouteSession,
    _RACE_ACCOUNT_CONFLICT_TEXT,
    is_race_scene_wait_result,
    larc_exchange_entry_released,
)


RACE_ENTRY_LOADING_EXTRA_RETRIES = 8
RACE_ENTRY_LOADING_REOBSERVATION_DELAY_SECONDS = 0.6
_RACE_ENTRY_LOADING_TEXT = ("连接中", "通信中", "加载中", "读取中", "nowloading")
_RACE_ENTRY_STILL_ON_HUB_TEXT = ("代表交流赛", "目标赛事", "凯旋门代表交流赛")


class RaceRouteExecutor:
    def __init__(
        self,
        *,
        adb_factory: Callable[[], Any] | None = None,
        planner: RaceRoutePlanner | None = None,
        safety_analyzer: FrameSafetyAnalyzer | None = None,
        safety_gate: FormalActionSafetyGate | None = None,
        sleep_fn: Callable[[float], None] = controlled_sleep,
    ) -> None:
        self.adb_factory = adb_factory or self._default_adb_factory
        self.planner = planner or RaceRoutePlanner()
        self.safety_analyzer = safety_analyzer or FrameSafetyAnalyzer()
        self.safety_gate = safety_gate or FormalActionSafetyGate()
        self.sleep_fn = sleep_fn
        self._default_gentle_budget = TransitionBudget()

    def _observe_race_day_entry(self,adb,source_path,*,pre_sha256,decision,policy,session):
        """Observe the submitted navigation once, then bounded retry reads.

        Pre-frame text proves the entry input only. Destination identity always
        uses a separately analyzed current post frame; this method never taps.
        When transient loading text (e.g. 连接中) or the unchanged hub screen
        appears, extend read-only re-observations up to RACE_ENTRY_LOADING_EXTRA_RETRIES.
        """
        observations=[]
        final={'path':'','sha256':None,'screen':'unknown'}
        normal_used=0
        extra_loading_used=0
        max_normal=RACE_ENTRY_POST_OBSERVATION_RETRIES+1
        index=0
        while normal_used<max_normal:
            if index:
                prev_reason=(observations[-1] or {}).get('reason')
                delay=(RACE_ENTRY_LOADING_REOBSERVATION_DELAY_SECONDS
                    if prev_reason in {'entry_frame_unchanged','entry_transition_loading'}
                    else RACE_READ_ONLY_REOBSERVATION_DELAY_SECONDS)
                self.sleep_fn(delay)
            suffix='.post_tap' if index==0 else f'.post_tap_read_{index:03d}'
            index+=1
            path=source_path.with_name(f'{source_path.stem}{suffix}{source_path.suffix or ".png"}')
            current={'path':str(path),'sha256':None,'screen':'unknown','input_performed':False}
            observations.append(current);final=current
            try:
                captured=adb.screenshot(str(path))
                if captured is None or not path.is_file():raise ValueError('capture_unavailable')
                image=cv2.imread(str(path),cv2.IMREAD_COLOR)
                if image is None:raise ValueError('image_unreadable')
                sha=hashlib.sha256(path.read_bytes()).hexdigest();current['sha256']=sha
                post_packet=self.safety_analyzer.analyze(path)
                current.update(text_status=post_packet.text_status,text_source=post_packet.text_source,
                    source_image_sha256=post_packet.source_image_sha256)
                if (post_packet.source_image_sha256!=sha
                        or hashlib.sha256(path.read_bytes()).hexdigest()!=sha):
                    current['reason']='post_source_mismatch';break
                if post_packet.dangerous or any(term in ''.join(post_packet.visible_text.split()) for term in _RACE_ACCOUNT_CONFLICT_TEXT):
                    current['reason']='unsafe_post_page';break
                if post_packet.text_status!='ok' or post_packet.errors:
                    current['reason']='post_text_unreadable';normal_used+=1;continue
                if sha==pre_sha256:
                    current['reason']='entry_frame_unchanged'
                    if extra_loading_used<RACE_ENTRY_LOADING_EXTRA_RETRIES:
                        extra_loading_used+=1;continue
                    normal_used+=1;continue
                if RaceRoutePlanner._looks_like_race_list_postcondition(image,post_packet.visible_text):
                    current['screen']='race_list';current['reason']='current_post_list_verified';break
                if policy.scenario=='larc' and session is not None:
                    successor=RaceRoutePlanner().larc_exchange_entry_postcondition(path,
                        entry_decision=decision,session=session,visible_text=post_packet.visible_text)
                    if successor is not None:
                        current['screen']=str(successor['route_state']);current['reason']='current_exchange_successor_verified';break
                compact=''.join(post_packet.visible_text.split()).lower()
                loading_transition=(
                    any(marker in compact for marker in _RACE_ENTRY_LOADING_TEXT)
                    or (decision.get('route_state')=='race_day_main_menu'
                        and any(marker in compact for marker in _RACE_ENTRY_STILL_ON_HUB_TEXT)
                        and '是否参加比赛' not in compact)
                )
                if loading_transition:
                    current['reason']='entry_transition_loading'
                    if extra_loading_used<RACE_ENTRY_LOADING_EXTRA_RETRIES:
                        extra_loading_used+=1;continue
                else:
                    current['reason']='post_page_unrecognized'
            except (OSError,ValueError,RuntimeError,cv2.error) as exc:
                current['reason']='post_read_unavailable:'+type(exc).__name__
            normal_used+=1
        return dict(final),observations

    @reobserving_step
    def run_once(
        self,
        *,
        mode: str,
        execute: bool,
        confirm_token: str | None,
        policy: RaceRoutePolicy | None = None,
        session: RaceRouteSession | None = None,
        screenshot_path: str | Path | None = None,
        wait_seconds: float = 0.0,
        settle_seconds: float = 0.0,
        transition_preflight: Callable[[dict[str, Any]], None] | None = None,
        allow_gentle_transition_wait: bool = False,
        gentle_transition_budget: TransitionBudget | None = None,
    ) -> dict[str, Any]:
        _ctx = SimpleNamespace(**{k: v for k, v in locals().items() if k != 'self'})
        for step in (
            self._run_stage_capture,
            self._run_stage_read_and_plan,
            self._run_stage_safety_and_scope_checks,
        ):
            early = step(_ctx)
            if early is not None:
                return early
        return self._run_stage_execute_and_postconditions(_ctx)

    def _run_stage_capture(self, _ctx: SimpleNamespace) -> dict[str, Any] | None:
        mode = _ctx.mode
        execute = _ctx.execute
        confirm_token = _ctx.confirm_token
        policy = _ctx.policy
        screenshot_path = _ctx.screenshot_path
        screenshot_path = Path(screenshot_path) if screenshot_path is not None else (
            Path(tempfile.gettempdir()) / "uma_race_route" / "screen.png"
        )

        capture_started=time.monotonic()
        timings={}
        adb = self.adb_factory()
        if mode == "live":
            if not adb.connect():
                return {"performed": False, "reason": "adb_connect_failed", "decision": {}}
            size = tuple(adb.get_screen_size())
            if size not in SUPPORTED_RACE_ROUTE_SCREEN_SIZES:
                return {"performed": False, "reason": f"unsupported_screen_size:{size}", "decision": {}}
            timings['connection_size_seconds']=round(time.monotonic()-capture_started,4)
            capture_started=time.monotonic()
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            adb.screenshot(str(screenshot_path))
        elif mode != "dry-run":
            return {"performed": False, "reason": "mode", "decision": {}}

        timings['capture_seconds']=round(time.monotonic()-capture_started,4) if mode=='live' else 0.
        requested_policy = policy or RaceRoutePolicy()
        base_policy = requested_policy.for_authorized_continuation(
            mode=mode, execute=execute, confirm_token=confirm_token)
        _ctx.screenshot_path = screenshot_path
        _ctx.timings = timings
        _ctx.adb = adb
        _ctx.requested_policy = requested_policy
        _ctx.base_policy = base_policy
        return None

    def _run_stage_read_and_plan(self, _ctx: SimpleNamespace) -> dict[str, Any] | None:
        mode = _ctx.mode
        session = _ctx.session
        screenshot_path = _ctx.screenshot_path
        timings = _ctx.timings
        adb = _ctx.adb
        requested_policy = _ctx.requested_policy
        base_policy = _ctx.base_policy
        read_started=time.monotonic()
        preview_text=None
        regional_source=None
        decoded_frame=None
        if mode=='live':
            from core.auto_trainer.larc_preview_text import default_local_reader,race_preview_text,race_navigation_text
            if default_local_reader(self.safety_analyzer):
                # This local observation cannot survive another run_once/frame.
                try:
                    frame_bytes=screenshot_path.read_bytes()
                    current_frame=cv2.imdecode(np.frombuffer(frame_bytes,dtype=np.uint8),cv2.IMREAD_COLOR)
                except OSError:
                    frame_bytes=None
                    current_frame=cv2.imread(str(screenshot_path))
                if frame_bytes is not None and current_frame is not None:
                    decoded_frame=(frame_bytes,current_frame)
                full_frame_observation={}
                preview_text=race_preview_text(current_frame,base_policy)
                if preview_text is not None:
                    regional_source='larc_race_preview_regions'
                else:
                    local_reader=getattr(self.safety_analyzer.rapidocr_reader,'__self__',None)
                    reusable_reader=getattr(local_reader,'_engine_factory',None) is None
                    navigation_text=race_navigation_text(current_frame,base_policy,session,
                        _full_frame_observation=full_frame_observation if reusable_reader else None)
                    if navigation_text is not None:
                        preview_text,regional_source=navigation_text
                    elif (frame_bytes is not None and full_frame_observation.get('image') is current_frame
                            and full_frame_observation.get('text') and
                            hashlib.sha256(screenshot_path.read_bytes()).digest()==hashlib.sha256(frame_bytes).digest()):
                        # Exactly the default full-reader join from these bytes;
                        # the analyzer still computes the normal safety flags.
                        preview_text=full_frame_observation['text']
                        regional_source='local_rapidocr'
                    elif (current_frame is not None
                            and (base_policy.post_entry_only or (session is not None and session.result_transition_pending))
                            and (RaceRoutePlanner._looks_like_blue_white_transition(current_frame)
                                 or (float(np.std(current_frame)) < 8.0 and RaceRoutePlanner._looks_like_loading(current_frame)))):
                        # Blank uniform fade or blue-white wipe cannot contain a
                        # prompt or control; skip full-frame OCR and wait safely.
                        preview_text = ''
                        regional_source = 'fast_loading_visual'
        packet = self.safety_analyzer.analyze(
            screenshot_path,
            supplied_text=(
                base_policy.visible_text if mode == "dry-run" else preview_text
            ),
            **({"_decoded_frame": decoded_frame} if preview_text is None and decoded_frame is not None else {}),
        )
        if preview_text is not None:packet=replace(packet,text_source=regional_source)
        timings['text_source']=packet.text_source
        effective_policy = replace(
            base_policy, visible_text=packet.visible_text,
            allow_locked_race_entry=(base_policy.allow_locked_race_entry is True and (
                not base_policy.wbc_only
                or (session is not None and session.wbc_entry_confirmed is True)
            )),
        )
        timings['text_seconds']=round(time.monotonic()-read_started,4)
        plan_started=time.monotonic()
        _extra_plan_kwargs: dict[str, Any] = {}
        if not packet.dangerous and not packet.resource_terms and (_ctx.allow_gentle_transition_wait or _ctx.gentle_transition_budget is not None):
            _extra_plan_kwargs['allow_gentle_transition_wait'] = True
            _extra_plan_kwargs['gentle_transition_budget'] = (
                _ctx.gentle_transition_budget
                if _ctx.gentle_transition_budget is not None
                else self._default_gentle_budget
            )
        decision = self.planner.plan(
            screenshot_path,
            policy=effective_policy,
            session=session,
            **_extra_plan_kwargs,
        )
        if (
            _ctx.gentle_transition_budget is None
            and (
                decision.get("action") in {"tap", "swipe", "handoff"}
                or decision.get("handoff") is True
                or (
                    decision.get("action") == "stop"
                    and decision.get("reason")
                    in {
                        "ready_main_menu",
                        "ready_training_menu",
                        "ready_larc_ss_preview",
                        "race_day_main_menu",
                        "global_home",
                        "race_inspection_complete",
                    }
                )
            )
        ):
            self._default_gentle_budget.reset()
        timings['planning_seconds']=round(time.monotonic()-plan_started,4)
        if base_policy != requested_policy:
            decision.setdefault("evidence", {})["ordinary_race_continuation"] = {
                "basis": "authorized_live_race_route", "entry_authority_added": False,
                "resource_authority_added": False,
            }
        grade_observations=[]
        if (mode=='live' and decision.get('reason')=='race_list:selected_grade_not_g1'
                and (decision.get('evidence',{}).get('selected_race') or {}).get('grade') in (None,'unknown')
                and packet.text_status=='ok' and not packet.dangerous
                and RaceRoutePlanner._looks_like_race_list_postcondition(cv2.imread(str(screenshot_path)),packet.visible_text)):
            # Missing grade is a read failure, not G1 authorization. Re-read a
            # currently identified list; never enter a race on an unknown grade.
            original_path=screenshot_path
            for index in range(1,RACE_LIST_GRADE_REOBSERVATION_RETRIES+1):
                self.sleep_fn(RACE_READ_ONLY_REOBSERVATION_DELAY_SECONDS)
                fresh=original_path.with_name(f'{original_path.stem}.grade_read_{index:03d}{original_path.suffix or ".png"}')
                observation={'path':str(fresh),'input_performed':False}
                grade_observations.append(observation)
                try:
                    captured=adb.screenshot(str(fresh))
                    if captured is None or not fresh.is_file():raise ValueError('capture_unavailable')
                    fresh_sha=hashlib.sha256(fresh.read_bytes()).hexdigest()
                    fresh_packet=self.safety_analyzer.analyze(fresh)
                    observation.update(sha256=fresh_sha,text_source=fresh_packet.text_source,
                        text_status=fresh_packet.text_status)
                    if (fresh_packet.source_image_sha256!=fresh_sha
                            or hashlib.sha256(fresh.read_bytes()).hexdigest()!=fresh_sha):
                        observation['reason']='source_mismatch';break
                    if fresh_packet.dangerous or any(term in ''.join(fresh_packet.visible_text.split()) for term in _RACE_ACCOUNT_CONFLICT_TEXT):
                        observation['reason']='unsafe_current_page';break
                    if (fresh_packet.text_status!='ok' or not RaceRoutePlanner._looks_like_race_list_postcondition(
                            cv2.imread(str(fresh)),fresh_packet.visible_text)):
                        observation['reason']='current_list_unreadable';continue
                    fresh_policy=replace(effective_policy,visible_text=fresh_packet.visible_text)
                    fresh_decision=self.planner.plan(fresh,policy=fresh_policy,session=session)
                    screenshot_path,packet,effective_policy,decision=fresh,fresh_packet,fresh_policy,fresh_decision
                    observation['reason']=decision.get('reason')
                    if (decision.get('reason')!='race_list:selected_grade_not_g1'
                            or (decision.get('evidence',{}).get('selected_race') or {}).get('grade') not in (None,'unknown')):break
                except (OSError,ValueError,RuntimeError,cv2.error) as exc:
                    observation['reason']='read_unavailable:'+type(exc).__name__
        if grade_observations:timings['grade_observations']=grade_observations
        # A result page can briefly expose a blank/blurred transition frame.
        # Do not weaken the same-frame Safety Gate; recapture a bounded number
        # of times before deciding whether the already-planned result tap is
        # safe.  No tap is issued during this recovery window.
        _ctx.screenshot_path = screenshot_path
        _ctx.packet = packet
        _ctx.effective_policy = effective_policy
        _ctx.decision = decision
        return None

    def _run_stage_safety_and_scope_checks(self, _ctx: SimpleNamespace) -> dict[str, Any] | None:
        mode = _ctx.mode
        execute = _ctx.execute
        session = _ctx.session
        screenshot_path = _ctx.screenshot_path
        timings = _ctx.timings
        adb = _ctx.adb
        base_policy = _ctx.base_policy
        packet = _ctx.packet
        effective_policy = _ctx.effective_policy
        decision = _ctx.decision
        if (
            mode == "live"
            and decision.get("reason") == "race_result_continue"
            and packet.text_status != "ok"
        ):
            for refresh_index in range(1, RESULT_TRANSITION_RECAPTURE_LIMIT + 1):
                self.sleep_fn(RESULT_TRANSITION_RECAPTURE_DELAY_SECONDS)
                refresh_path = screenshot_path.with_name(
                    f"{screenshot_path.stem}.result_refresh_{refresh_index:03d}"
                    f"{screenshot_path.suffix or '.png'}"
                )
                try:
                    adb.screenshot(str(refresh_path))
                    refreshed_packet = self.safety_analyzer.analyze(refresh_path)
                except Exception:
                    continue
                if refreshed_packet.text_status != "ok" or refreshed_packet.errors:
                    continue
                screenshot_path = refresh_path
                packet = refreshed_packet
                effective_policy = replace(effective_policy, visible_text=packet.visible_text)
                decision = self.planner.plan(
                    screenshot_path,
                    policy=effective_policy,
                    session=session,
                )
                break
        if session is not None and larc_exchange_entry_released(decision):
            # No input is needed to know that the recognized preview/scene/home
            # is past this confirmation. Do not retain stale ownership on wait.
            session.pending_larc_exchange = None
        if decision.get('reason') == 'cancel_clock_retry':
            from core.auto_trainer.race_clock_retry import authorized_decline
            current_frame = cv2.imread(str(screenshot_path))
            decline_policy = effective_policy
            if effective_policy.clock_retry_remaining is None and not effective_policy.allow_clock_retry:
                # A missing legacy budget cannot authorize spending. It also
                # must not prevent closing this verified existing retry page.
                decline_policy = replace(effective_policy, clock_retry_remaining=0)
            if not authorized_decline(decline_policy, decision, packet.visible_text, current_frame):
                return {'performed':False,'reason':'clock_retry_cancel_unverified','decision':decision}
            safety_verdict = self.safety_gate.evaluate(packet, decision)
        elif decision.get('reason')=='open_lost_race_retry':
            from core.auto_trainer.race_clock_retry import ENTRY_ACTION_ID, authorized_entry
            try:
                current_frame = cv2.imdecode(np.frombuffer(screenshot_path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
            except (OSError, cv2.error):
                current_frame = None
            if not authorized_entry(effective_policy,session,decision,packet.visible_text,
                    image=current_frame):
                return {'performed':False,'reason':'clock_retry_entry_not_authorized','decision':decision}
            safety_verdict=self.safety_gate.evaluate(packet,decision,authorized_resource_action=ENTRY_ACTION_ID)
        elif decision.get("reason") == "retry_lost_race":
            from core.auto_trainer.race_clock_retry import ACTION_ID, authorized_retry
            if not authorized_retry(effective_policy, session, decision, packet.visible_text):
                return {"performed": False, "reason": "clock_retry_not_authorized", "decision": decision}
            safety_verdict = self.safety_gate.evaluate(packet, decision, authorized_resource_action=ACTION_ID)
        else:
            safety_verdict = self.safety_gate.evaluate(packet, decision)
        safety_evidence = {
            "frame_safety": packet.to_dict(),
            "safety_verdict": safety_verdict.to_dict(),
            "timing":timings,
        }
        if not safety_verdict.allowed:
            return {
                "performed": False,
                "reason": "action_safety_gate",
                "decision": decision,
                **safety_evidence,
            }
        observed_control = decision.get("evidence", {}).get("observed_free_race_control")
        if observed_control is not None and (
            observed_control.get("source_image_sha256") != packet.source_image_sha256
            or observed_control.get("action_id") != (decision.get("tap") or {}).get("coordinate_name")
            or (decision.get("reason"), decision.get("route_state"), observed_control.get("action_id")) not in {
                ("race_result_continue", "race_result", "race_result.continue"),
                ("close_trophy_reward", "race_result", "safe_advance.trophy_reward_close"),
                ("larc_explanation_continue", "modal", "safe_advance.larc_explanation_continue"),
                ("continue_scenario_score_record", "race_result", "safe_advance.scenario_score_record_continue"),
                ("next_support_reward_page", "race_result", "safe_advance.support_reward_next_page"),
                ("skip_or_advance_race_scene", "race_scene", "race_scene.skip_or_advance"),
            }
        ):
            return {"performed": False, "reason": "observed_race_control_source_mismatch",
                    "decision": decision, **safety_evidence}
        rank_points_overlay = decision.get("evidence", {}).get("larc_rank_points_overlay", {})
        compact_rank = (decision.get("evidence", {}).get("compact_rank_evidence")
                        or decision.get("evidence", {}).get("compact_brown_overlay_evidence")
                        or (rank_points_overlay if rank_points_overlay.get("compact_award") else None))
        if compact_rank is not None and not (
            compact_rank.get("source_image_sha256") == packet.source_image_sha256
            == hashlib.sha256(screenshot_path.read_bytes()).hexdigest()
        ):
            return {"performed": False, "reason": "compact_rank_source_mismatch", "decision": decision, **safety_evidence}
        if base_policy.count_reserved_only:
            error = self._count_scope_error(decision, session, effective_policy, packet.source_image_sha256)
            if error is not None:
                return {"performed": False, "reason": error, "decision": decision, **safety_evidence}
        if base_policy.inspection_only:
            error = RaceRoutePlanner._inspection_context_error(effective_policy, session)
            phase = session.inspection_phase if session is not None else None
            identity = (decision.get("action"), decision.get("reason"), decision.get("route_state"),
                        (decision.get("tap") or {}).get("coordinate_name"))
            allowed = {
                "new": {("tap", "enter_race_from_main_menu", "ready_main_menu", "main_menu.race")},
                "opened": {("tap", "leave_inspected_race_list", "race_list", "race_list.back"),
                           ("tap", "confirm_race_recommendation_top", "fan_goal_recommendation", "safe_advance.race_recommendation_top_confirm"),
                           ("wait", "loading", "loading", None),
                           ("wait", "transition", "transition", None)},
                "confirmed": {("tap", "leave_inspected_race_list", "race_list", "race_list.back"),
                           ("wait", "loading", "loading", None),
                           ("wait", "transition", "transition", None)},
                "returned": {("stop", "race_inspection_complete", "ready_main_menu", None),
                             ("wait", "loading", "loading", None),
                             ("wait", "transition", "transition", None)},
            }
            if error is None and identity not in allowed.get(phase, set()):
                error = "race_inspection:action_out_of_scope"
            if error is None and (hashlib.sha256(screenshot_path.read_bytes()).hexdigest() != packet.source_image_sha256
                                  or packet.source_image_sha256 == session.inspection_source_sha256):
                error = "race_inspection:frame_not_fresh"
            if error is not None:
                return {"performed": False, "reason": error, "decision": decision, **safety_evidence}
            if not execute or mode != "live":
                return {"performed": False, "reason": "execute" if mode == "live" else "dry_run",
                        "decision": decision, **safety_evidence}
        if base_policy.wbc_only:
            if hashlib.sha256(screenshot_path.read_bytes()).hexdigest() != packet.source_image_sha256:
                scope_error = "frame_safety_sha_mismatch"
            else:
                scope_error = self._wbc_scope_error(
                    decision, session, effective_policy, screenshot_path, packet.visible_text,
                )
            if scope_error is not None:
                return {
                    "performed": False, "reason": scope_error,
                    "decision": decision, **safety_evidence,
                }
        _ctx.screenshot_path = screenshot_path
        _ctx.packet = packet
        _ctx.effective_policy = effective_policy
        _ctx.decision = decision
        _ctx.safety_evidence = safety_evidence
        _ctx.observed_control = observed_control
        _ctx.rank_points_overlay = rank_points_overlay
        return None

    def _run_stage_execute_and_postconditions(self, _ctx: SimpleNamespace) -> dict[str, Any]:
        mode = _ctx.mode
        execute = _ctx.execute
        confirm_token = _ctx.confirm_token
        session = _ctx.session
        screenshot_path = _ctx.screenshot_path
        wait_seconds = _ctx.wait_seconds
        settle_seconds = _ctx.settle_seconds
        transition_preflight = _ctx.transition_preflight
        adb = _ctx.adb
        base_policy = _ctx.base_policy
        packet = _ctx.packet
        decision = _ctx.decision
        safety_evidence = _ctx.safety_evidence
        observed_control = _ctx.observed_control
        rank_points_overlay = _ctx.rank_points_overlay
        if decision.get("action") == "wait":
            if mode == "dry-run":
                return {
                    "performed": False,
                    "reason": "dry_run",
                    "decision": decision,
                    **safety_evidence,
                }
            self.sleep_fn(max(0.0, float(wait_seconds)))
            return {
                "performed": True,
                "reason": str(decision.get("reason", "wait")),
                "decision": decision,
                **safety_evidence,
            }
        if decision.get("action") == "handoff":
            if (not is_route_handoff(decision) or packet.dangerous
                    or hashlib.sha256(screenshot_path.read_bytes()).hexdigest() != packet.source_image_sha256):
                return {"performed": False, "reason": "race_handoff_unverified",
                        "decision": decision, **safety_evidence}
            return {"performed": False, "reason": decision["reason"], "decision": decision,
                    **safety_evidence}
        if decision.get("action") not in {"tap", "swipe"}:
            return {
                "performed": False,
                "reason": str(decision.get("reason", "stop")),
                "decision": decision,
                **safety_evidence,
            }
        if mode == "dry-run":
            return {
                "performed": False,
                "reason": "dry_run",
                "decision": decision,
                **safety_evidence,
            }
        if not execute:
            return {
                "performed": False,
                "reason": "execute",
                "decision": decision,
                **safety_evidence,
            }
        if confirm_token != RACE_ROUTE_CONFIRM_TOKEN:
            return {
                "performed": False,
                "reason": "confirm_token",
                "decision": decision,
                **safety_evidence,
            }
        if (
            decision.get("larc_exchange_verification") is not None
            or decision.get("target_race_verification") is not None
            or decision.get("locked_target_verification") is not None
            or decision.get("consume_target_race_verification")
            or decision.get("scenario_final_verification") is not None
            or decision.get("consume_scenario_final_verification")
            or decision.get("fan_goal_recommendation_verification") is not None
            or decision.get("consume_fan_goal_recommendation_verification")
            or decision.get("fan_goal_shortfall_selected_verification") is not None
            or decision.get("consume_fan_goal_shortfall_selected_verification")
            or decision.get("wbc_hub_verification") is not None
            or decision.get("consume_wbc_hub_verification")
            or decision.get("gur_race_verification") is not None
            or decision.get("consume_gur_race_verification")
        ) and session is None:
            return {
                "performed": False,
                "reason": "race_session_required",
                "decision": decision,
                **safety_evidence,
            }
        if decision.get("consume_fan_goal_shortfall_selected_verification"):
            assert session is not None
            if session.fan_goal_shortfall_selected_verification() is None:
                return {
                    "performed": False,
                    "reason": "fan_goal_shortfall_selected_verification_required",
                    "decision": decision,
                    **safety_evidence,
                }

        if hashlib.sha256(screenshot_path.read_bytes()).hexdigest() != packet.source_image_sha256:
            return {
                "performed": False,
                "reason": "frame_safety_sha_mismatch",
                "decision": decision,
                **safety_evidence,
            }

        if base_policy.inspection_only:
            tap = decision["tap"]
            source = {"action_id": tap["coordinate_name"], "coordinate_name": tap["coordinate_name"],
                      "action_state": ("main_menu" if session.inspection_phase == "new" else
                                       "modal" if tap["coordinate_name"] == "safe_advance.race_recommendation_top_confirm" else "race_list"),
                      "source_path": str(screenshot_path.resolve(strict=True)),
                      "source_sha256": packet.source_image_sha256}
            try:
                if transition_preflight is None:
                    raise ValueError("transition_preflight_required")
                transition_preflight(source)
            except Exception as exc:
                return {"performed": False, "reason": f"race_inspection:preflight:{exc}",
                        "decision": decision, **safety_evidence}
            try:
                outcome = adb.tap(tap["x"], tap["y"], scaled=bool(tap.get("scaled", False)))
            except Exception as exc:
                return {"performed": False, "reason": "race_inspection:tap_outcome_unknown",
                        "error": f"{type(exc).__name__}: {exc}", "decision": decision, **safety_evidence}
            if outcome is not True:
                return {"performed": False, "reason": "race_inspection:tap_outcome_failed" if outcome is False else "race_inspection:tap_outcome_unknown",
                        "decision": decision, **safety_evidence}
            session.inspection_phase = ("opened" if session.inspection_phase == "new" else
                                        "confirmed" if tap["coordinate_name"] == "safe_advance.race_recommendation_top_confirm" else "returned")
            session.inspection_source_sha256 = packet.source_image_sha256
            self.sleep_fn(max(0.0, float(settle_seconds)))
            return {"performed": True, "tap_performed": True, "reason": decision["reason"],
                    "decision": decision, "route_action_receipt_source": source, **safety_evidence}

        if decision.get("action") == "swipe":
            if session is None:
                return {
                    "performed": False,
                    "reason": "race_session_required",
                    "decision": decision,
                    **safety_evidence,
                }
            gesture = decision.get("gesture")
            if not isinstance(gesture, dict):
                return {
                    "performed": False,
                    "reason": "race_list_scan_gesture_required",
                    "decision": decision,
                    **safety_evidence,
                }
            scan_image = cv2.imread(str(screenshot_path), cv2.IMREAD_COLOR)
            expected_gesture = (
                {
                    "x1": int(round(scan_image.shape[1] * 0.50)),
                    "y1": int(round(scan_image.shape[0] * 0.76)),
                    "x2": int(round(scan_image.shape[1] * 0.50)),
                    "y2": int(round(scan_image.shape[0] * 0.55)),
                    "duration_ms": 450,
                }
                if scan_image is not None
                else None
            )
            if (
                expected_gesture is None
                or gesture.get("coordinate_name")
                != "race_list.scan_for_visible_g1"
                or gesture.get("direction") != "up"
                or gesture.get("scaled") is not False
                or any(
                    type(gesture.get(key)) is not int
                    or gesture.get(key) != value
                    for key, value in expected_gesture.items()
                )
            ):
                return {
                    "performed": False,
                    "reason": "race_list_scan_gesture_invalid",
                    "decision": decision,
                    **safety_evidence,
                }
            scan_evidence = (
                decision.get("evidence", {}).get("race_list_scan")
                if isinstance(decision.get("evidence"), dict)
                else None
            )
            if (
                not isinstance(scan_evidence, dict)
                or scan_evidence.get("source")
                != "race_list.viewport_pixels"
                or scan_evidence.get("max_scans") != 2
                or scan_evidence.get("viewport_sha256")
                != RaceRoutePlanner._race_list_viewport_sha256(scan_image)
            ):
                return {
                    "performed": False,
                    "reason": "race_list_scan_viewport_sha_mismatch",
                    "decision": decision,
                    **safety_evidence,
                }
            try:
                session.stage_race_list_scan(decision)
            except ValueError as exc:
                return {
                    "performed": False,
                    "reason": str(exc),
                    "decision": decision,
                    **safety_evidence,
                }
            adb.swipe(
                int(gesture["x1"]),
                int(gesture["y1"]),
                int(gesture["x2"]),
                int(gesture["y2"]),
                duration_ms=int(gesture["duration_ms"]),
                scaled=bool(gesture.get("scaled", False)),
            )
            if settle_seconds > 0:
                self.sleep_fn(max(0.0, float(settle_seconds)))
            return {
                "performed": True,
                "reason": str(decision["reason"]),
                "decision": decision,
                **safety_evidence,
            }

        if (
            session is not None
            and session.fan_goal_shortfall_selected_verification() is not None
            and any(
                decision.get(key) is not None
                for key in (
                    "target_race_verification",
                    "locked_target_verification",
                    "scenario_final_verification",
                    "fan_goal_recommendation_verification",
                    "wbc_hub_verification",
                    "gur_race_verification",
                )
            )
        ):
            return {
                "performed": False,
                "reason": "ambiguous_race_verification",
                "decision": decision,
                **safety_evidence,
            }
        if decision.get("fan_goal_shortfall_selected_verification") is not None:
            assert session is not None
            try:
                session.stage_fan_goal_shortfall_selected_entry(decision)
            except ValueError as exc:
                return {
                    "performed": False,
                    "reason": str(exc),
                    "decision": decision,
                    **safety_evidence,
                }
        if decision.get("custom_scheduled_race_verification") is not None:
            assert session is not None
            try:
                session.stage_custom_scheduled_race_entry(decision)
            except ValueError as exc:
                return {
                    "performed": False,
                    "reason": str(exc),
                    "decision": decision,
                    **safety_evidence,
                }
        if decision.get("fan_goal_recommendation_verification") is not None:
            assert session is not None
            try:
                session.stage_fan_goal_recommendation_entry(decision)
            except ValueError as exc:
                return {
                    "performed": False,
                    "reason": str(exc),
                    "decision": decision,
                    **safety_evidence,
                }
        staged_wbc_session = None
        if decision.get("wbc_hub_verification") is not None:
            assert session is not None
            try:
                staged_wbc_session = replace(session)
                staged_wbc_session.stage_wbc_hub_race_entry(decision)
            except ValueError as exc:
                return {
                    "performed": False,
                    "reason": str(exc),
                    "decision": decision,
                    **safety_evidence,
                }
        if decision.get("gur_race_verification") is not None:
            assert session is not None
            try:
                session.stage_gur_race_entry(decision)
            except ValueError as exc:
                return {
                    "performed": False,
                    "reason": str(exc),
                    "decision": decision,
                    **safety_evidence,
                }
        if decision.get("larc_exchange_verification") is not None:
            staged_exchange = replace(session)
            if decision.get("consume_target_race_verification"):
                staged_exchange.consume_target_race_entry()
            try:
                staged_exchange.stage_larc_exchange_entry(decision)
            except ValueError as exc:
                return {"performed": False, "reason": str(exc), "decision": decision, **safety_evidence}
        tap = decision["tap"]
        if base_policy.count_reserved_only:
            staged = replace(session)
            try:
                if decision.get("target_race_verification") is not None:
                    staged.stage_target_race_entry(decision)
                if decision.get("consume_target_race_verification"):
                    staged.consume_target_race_entry()
                if transition_preflight is not None:
                    transition_preflight({"action_id": tap["coordinate_name"],
                        "source_path": str(screenshot_path.resolve(strict=True)),
                        "source_sha256": packet.source_image_sha256, **safety_evidence})
            except (OSError, ValueError) as exc:
                return {"performed": False, "reason": f"count_race:preflight:{exc}", "decision": decision, **safety_evidence}
        strict_wbc_tap = bool(
            base_policy.wbc_only
            or decision.get("reason") in {"enter_race_from_wbc_hub", "confirm_wbc_hub_race"}
        )
        strict_final_list_tap = decision.get("reason") == "enter_grand_masters_final_from_list"
        strict_final_tap = bool(strict_final_list_tap or decision.get("scenario_final_verification")
                                or decision.get("consume_scenario_final_verification"))
        strict_compact_tap = (decision.get("evidence", {}).get("compact_brown_overlay_evidence") is not None
                              or rank_points_overlay.get("compact_award") is True)
        strict_reward_tap = decision.get("evidence", {}).get("larc_reward_footer") is not None
        strict_clock_tap = decision.get("reason") in {"retry_lost_race", "open_lost_race_retry", "cancel_clock_retry"}
        strict_local_tap = (strict_final_tap or strict_compact_tap or strict_clock_tap
                            or strict_reward_tap or observed_control is not None)
        strict_fan_shortfall_tap = (
            decision.get("consume_fan_goal_shortfall_selected_verification") is True
        )
        local_tap_prefix = ("clock_retry" if strict_clock_tap else "compact_result" if strict_compact_tap
                            else "race_reward" if strict_reward_tap else "observed_race_control"
                            if observed_control is not None else "scenario_final")
        try:
            tap_ok = adb.tap(tap["x"], tap["y"], scaled=bool(tap.get("scaled", False)))
        except Exception as exc:
            if (
                not strict_wbc_tap
                and not strict_local_tap
                and not strict_fan_shortfall_tap
                and not base_policy.count_reserved_only
            ):
                raise
            return {
                "performed": False, "reason": (f"{local_tap_prefix}_tap_outcome_unknown" if strict_local_tap else
                    "fan_goal_shortfall_tap_outcome_unknown" if strict_fan_shortfall_tap else
                    "count_race:tap_outcome_unknown" if base_policy.count_reserved_only else "wbc_tap_outcome_unknown"),
                "tap_outcome": "unknown", "error": f"{type(exc).__name__}: {exc}",
                "decision": decision, **safety_evidence,
            }
        if (
            strict_wbc_tap
            or strict_local_tap
            or strict_fan_shortfall_tap
            or base_policy.count_reserved_only
        ) and tap_ok is not True:
            outcome = "failed" if tap_ok is False else "unknown"
            return {
                "performed": False, "reason": (f"{local_tap_prefix}_tap_outcome_{outcome}" if strict_local_tap else
                    f"fan_goal_shortfall_tap_outcome_{outcome}" if strict_fan_shortfall_tap else
                    f"count_race:tap_outcome_{outcome}" if base_policy.count_reserved_only else f"wbc_tap_outcome_{outcome}"),
                "tap_outcome": outcome, "decision": decision, **safety_evidence,
            }
        if decision.get("larc_exchange_verification") is not None and tap_ok is not True:
            return {"performed": False, "reason": "larc_exchange_tap_outcome_unknown",
                    "tap_outcome": "failed" if tap_ok is False else "unknown",
                    "decision": decision, **safety_evidence}
        if decision.get('post_tap_confirmation')=='race_day_entry_race_list' and tap_ok is not True:
            outcome='failed' if tap_ok is False else 'unknown'
            return {'performed':False,'reason':'race_day_entry_tap_outcome_'+outcome,
                'tap_outcome':outcome,'input_may_have_occurred':outcome=='unknown',
                'decision':decision,**safety_evidence}
        if staged_wbc_session is not None:
            session.pending_wbc_hub_race = staged_wbc_session.pending_wbc_hub_race
        if strict_final_list_tap:
            session.pending_scenario_final_race = dict(decision["scenario_final_list_entry_verification"])
        if (
            decision.get("action") == "tap"
            and decision.get("reason") in {
                "leave_selected_non_g1_race_list",
                "leave_unverified_g1_race_list",
                "leave_fan_goal_shortfall_race_list",
                "leave_unavailable_custom_scheduled_race_list",
            }
            and decision.get("route_state") == "race_list"
            and tap.get("coordinate_name") == "race_list.back"
        ):
            if tap_ok is not True:
                return {
                    "performed": False, "reason": "race_list_back_tap_outcome_unknown",
                    "decision": decision, **safety_evidence,
                }
            if decision.get("reason") in {
                "leave_selected_non_g1_race_list",
                "leave_unverified_g1_race_list",
                "leave_fan_goal_shortfall_race_list",
                "leave_unavailable_custom_scheduled_race_list",
            } and session is not None:
                # Opening a fixed-race list only stages its target; a confirmed
                # Back cancels that unsubmitted intent, never a race receipt.
                session.pending_locked_target_race = None
                session.pending_custom_scheduled_race = None
            if settle_seconds > 0:
                self.sleep_fn(max(0.0, float(settle_seconds)))
            return {
                "performed": True, "tap_performed": True,
                "reason": str(decision["reason"]), "decision": decision,
                "route_action_receipt_source": {
                    "action_id": "race_list.back", "action_state": "race_list",
                    "coordinate_name": "race_list.back",
                    "source_path": str(screenshot_path.resolve(strict=True)),
                    "source_sha256": packet.source_image_sha256,
                },
                **safety_evidence,
            }
        if decision.get("target_race_verification") is not None:
            assert session is not None
            session.stage_target_race_entry(decision)
        if decision.get("locked_target_verification") is not None:
            assert session is not None
            session.stage_locked_target_race_entry(decision)
        if decision.get("scenario_final_verification") is not None:
            assert session is not None
            session.stage_scenario_final_race_entry(decision)
        if decision.get("consume_target_race_verification"):
            assert session is not None
            session.consume_target_race_entry()
        if decision.get("larc_exchange_verification") is not None:
            assert session is not None
            session.stage_larc_exchange_entry(decision)
        if session is not None and decision.get("reason") in {
            "view_race_result", "go_to_race_from_detail", "race_scene_start_or_continue",
            "skip_or_advance_race_scene", "confirm_race_playback", "race_result_continue",
            "cancel_unverified_race_confirm", "leave_race_detail",
        }:
            session.pending_larc_exchange = None
        if decision.get("consume_scenario_final_verification"):
            assert session is not None
            session.consume_scenario_final_race_entry()
        if decision.get("consume_fan_goal_recommendation_verification"):
            assert session is not None
            session.consume_fan_goal_recommendation_entry()
        if decision.get("consume_fan_goal_shortfall_selected_verification"):
            assert session is not None
            session.consume_fan_goal_shortfall_selected_entry()
        if decision.get("consume_custom_scheduled_race_verification"):
            assert session is not None
            session.consume_custom_scheduled_race_entry()
        if decision.get("consume_wbc_hub_verification"):
            assert session is not None
            session.consume_wbc_hub_race_entry()
            session.wbc_entry_confirmed = True
        if decision.get("consume_gur_race_verification"):
            assert session is not None
            session.consume_gur_race_entry()
        if session is not None and decision.get("reason") == "view_race_result":
            session.result_transition_pending = True
        elif session is not None and decision.get("reason") == "race_result_continue":
            session.result_transition_pending = False
        elif session is not None and decision.get("reason") == "retry_lost_race":
            session.clock_retries_used += 1
            session.last_clock_retry_remaining = decision["evidence"]["clock_retry"]["remaining"]
            session.clock_retry_entry_pending = False
            session.clock_retry_declined = False
            session.result_transition_pending = False
        elif session is not None and decision.get('reason') == 'cancel_clock_retry':
            session.clock_retry_entry_pending = False
            session.clock_retry_declined = True
            session.result_transition_pending = True
        elif session is not None and decision.get('reason')=='open_lost_race_retry':
            session.clock_retry_entry_pending = True
        elif session is not None and decision.get("reason") in {"race_tactic_open", "race_tactic_confirm", "race_tactic_cancel"}:
            tactic_ev = (decision.get("evidence") or {}).get("race_tactic") if isinstance(decision.get("evidence"), dict) else None
            if decision.get("reason") == "race_tactic_open" and isinstance(tactic_ev, dict):
                session.pending_tactic_target = tactic_ev.get("target_style")
                session.pending_tactic_solo_fallback = bool(tactic_ev.get("solo_leader_fallback"))
                session.tactic_modal_closing = False
            elif decision.get("reason") == "race_tactic_confirm":
                session.pending_tactic_target = None
                session.pending_tactic_solo_fallback = False
                session.tactic_modal_closing = True
            elif decision.get("reason") == "race_tactic_cancel":
                session.pending_tactic_target = None
                session.pending_tactic_solo_fallback = False
                session.tactic_modal_closing = True
                if isinstance(tactic_ev, dict) and tactic_ev.get("reason") == "solo_leader_front_aptitude_blocked":
                    session.solo_leader_front_aptitude_rejected = True
        elif session is not None and session.tactic_modal_closing and decision.get("reason") != "race_tactic_select":
            session.tactic_modal_closing = False
        if base_policy.count_reserved_only:
            session.count_phase = {"main_menu.race": "opened", "race_list.primary_entry": "entered",
                                   "race_confirm.primary": "confirmed"}.get(tap["coordinate_name"], session.count_phase)
            session.count_resume_preview_title = None
        if settle_seconds > 0:
            self.sleep_fn(max(0.0, float(settle_seconds)))
        if decision.get("post_tap_confirmation") == "race_day_entry_race_list":
            post_frame,post_reads=self._observe_race_day_entry(adb,screenshot_path,
                pre_sha256=packet.source_image_sha256,decision=decision,policy=base_policy,session=session)
            if post_frame['screen'] == "unknown":
                return {
                    "performed": False,
                    "tap_performed": True,
                    "reason": "race_day_entry_postcondition_failed",
                    "decision": decision,
                    "post_tap_frame": post_frame,
                    "postcondition_observations": post_reads,
                    **safety_evidence,
                }
            return {
                "performed": True,
                "reason": str(decision["reason"]),
                "decision": decision,
                "post_tap_frame": post_frame,
                "postcondition_observations": post_reads,
                **({"tap_performed": True} if strict_final_tap else {}),
                **safety_evidence,
            }
        post_tap_confirmation = decision.get("post_tap_confirmation")
        if post_tap_confirmation == "career_complete_global_home":
            adb.screenshot(str(screenshot_path))
            post_tap_image = cv2.imread(str(screenshot_path))
            reason = (
                "career_complete"
                if SafeAdvancePlanner.is_post_career_global_home(post_tap_image)
                else "terminal_confirmation_unknown"
            )
        else:
            reason = str(decision["reason"])
        return {
            "performed": True,
            "reason": reason,
            "decision": decision,
            **(
                {"tap_performed": True}
                if strict_wbc_tap
                or strict_local_tap
                or strict_fan_shortfall_tap
                or base_policy.count_reserved_only
                or decision.get("larc_exchange_verification") is not None
                else {}
            ),
            **({"route_action_receipt_source": {"action_id": tap["coordinate_name"],
                "coordinate_name": tap["coordinate_name"], "action_state": decision.get("route_state"),
                "source_path": str(screenshot_path.resolve(strict=True)), "source_sha256": packet.source_image_sha256}}
               if base_policy.count_reserved_only else {}),
            **safety_evidence,
        }

    @staticmethod
    def _count_scope_error(decision, session, policy, source_sha):
        if (session is None or len(session.planned_race_ids) != 1 or not session.schedule_packet_sha256
            or policy.wbc_only or policy.inspection_only or any(getattr(policy, name) for name in (
                "allow_fan_goal_recommended_race", "allow_select_visible_g1", "allow_scan_race_list_for_g1",
                "allow_leave_race_list", "allow_leave_unverified_g1_race_list", "allow_leave_race_detail",
                "allow_locked_race_entry", "allow_grand_masters_final_race_confirm", "allow_cancel_unverified_grand_masters_final"))):
            return "count_race:binding_or_capability_invalid"
        if any(getattr(session, name) is not None for name in ("pending_locked_target_race", "pending_scenario_final_race",
            "pending_fan_goal_recommendation", "pending_fan_goal_shortfall_selected", "pending_wbc_hub_race", "pending_gur_race")):
            return "count_race:foreign_proof"
        if session.count_phase in {"confirmed", "watching"} and session.scheduled_entry_consumed is not True:
            return "count_race:confirmation_required"
        if session.count_resume_restart_title is not None:
            source = Path((decision.get("evidence") or {}).get("image", ""))
            img = cv2.imread(str(source))
            lines = tuple(line.strip() for line in analyze_screen_text(policy.visible_text or "")["normalized_text"].splitlines() if line.strip())
            point = RaceRoutePlanner._bottom_center_green_region(img, min_y_ratio=0.82) if img is not None else None
            tap = decision.get("tap") or {}
            if (session.count_phase != "watching" or decision.get("action") != "tap"
                or decision.get("reason") != "race_scene_start_or_continue" or decision.get("route_state") != "race_scene"
                or tap.get("coordinate_name") != "race_scene.start_or_continue" or (tap.get("x"), tap.get("y")) != point
                or lines[:2] != session.count_resume_restart_title or lines[-1:] != ("参赛!",)
                or (decision.get("evidence") or {}).get("perception", {}).get("screen") != "race_participant_list"
                or img is None or img.shape[:2] != (1280, 720) or point is None
                or SafeAdvancePlanner._looks_like_race_list(img) or RaceRoutePlanner._race_preview_action_points(img) is not None
                or hashlib.sha256(source.read_bytes()).hexdigest() != source_sha):
                return "count_race:restart_start_unverified"
        elif session.count_resume_watch_pending:
            source = Path((decision.get("evidence") or {}).get("image", ""))
            img = cv2.imread(str(source))
            rank_stop = (decision.get("action") == "stop" and decision.get("reason") == "race_result:continue_not_allowed"
                         and img is not None and RaceRoutePlanner._looks_like_ranking_list_result(img))
            controls = RaceRoutePlanner._count_portrait_watch_controls(img, policy.visible_text) if img is not None else None
            scene = (controls is not None and decision.get("route_state") == "race_scene"
                     and ((decision.get("action"), decision.get("reason")) == ("wait", "race_scene_running")
                          or (decision.get("action") == "tap" and decision.get("reason") == "skip_or_advance_race_scene"
                              and [(decision.get("tap") or {}).get("x"), (decision.get("tap") or {}).get("y")] == controls["skip_point"])))
            if (session.count_phase != "watching" or not (rank_stop or scene)
                or hashlib.sha256(source.read_bytes()).hexdigest() != source_sha):
                return "count_race:resume_watch_changed"
        if session.count_resume_preview_title is not None:
            lines = tuple(re.sub(r"[^\S\r\n]+", "", line) for line in (policy.visible_text or "").splitlines() if line.strip())
            grade, name = session.count_resume_preview_title
            if (decision.get("action") != "tap" or decision.get("reason") not in {"go_to_race_from_detail", "view_race_result"}
                or grade != "GI" or not (lines[:2] == (grade, name) or lines[:1] == (name,))):
                return "count_race:resume_preview_changed"
            source = Path((decision.get("evidence") or {}).get("image", ""))
            img = cv2.imread(str(source))
            points = RaceRoutePlanner._race_preview_action_points(img) if img is not None else None
            locked = decision.get("reason") == "go_to_race_from_detail"
            tap = decision.get("tap") or {}
            if (img is None or img.shape[:2] != (1280, 720) or points is None
                or points.get("view_result_locked") is not locked
                or (decision.get("route_state"), tap.get("coordinate_name")) != (
                    ("race_detail", "race_detail.go_to_race") if locked
                    else ("race_preview", "race_detail.view_result"))
                or (tap.get("x"), tap.get("y")) != points["go_to_race" if locked else "view_result"]
                or hashlib.sha256(source.read_bytes()).hexdigest() != source_sha):
                return "count_race:resume_preview_unverified"
            # Preview header ROI, not the selected-list-card ROI. Reuse the
            # existing G1 badge palette; full-text OCR and its cutoff stay intact.
            badge = cv2.cvtColor(img[31:58, 48:110], cv2.COLOR_BGR2HSV)
            blue = float(np.mean((badge[:, :, 0] >= 85) & (badge[:, :, 0] <= 125)
                                 & (badge[:, :, 1] > 60) & (badge[:, :, 2] > 90)))
            pink = float(np.mean(((badge[:, :, 0] <= 8) | (badge[:, :, 0] >= 165))
                                 & (badge[:, :, 1] > 55) & (badge[:, :, 2] > 130)))
            orange = float(np.mean((badge[:, :, 0] >= 5) & (badge[:, :, 0] <= 35)
                                   & (badge[:, :, 1] > 60) & (badge[:, :, 2] > 100)))
            green = float(np.mean((badge[:, :, 0] >= 38) & (badge[:, :, 0] <= 92)
                                  & (badge[:, :, 1] > 70) & (badge[:, :, 2] > 80)))
            if not (blue > 0.40 and pink < 0.10 and orange < 0.10 and green < 0.10):
                return "count_race:resume_grade_unverified"
            decision["evidence"]["count_resume_preview_identity"] = {
                "source_frame_sha256": source_sha, "race_name": name, "grade": "G1",
                "grade_source": "race_preview.header_grade_badge", "grade_roi": [48, 31, 110, 58],
                "grade_scores": {"blue": round(blue, 4), "pink": round(pink, 4),
                                 "orange": round(orange, 4), "green": round(green, 4)}}
        if decision.get("action") == "stop" or is_route_handoff(decision):
            return None
        identity = (decision.get("action"), decision.get("reason"), decision.get("route_state"),
                    (decision.get("tap") or {}).get("coordinate_name"))
        if identity == ("wait", "count_race_post_confirm_wait", "race_list", None):
            selected = (decision.get("evidence") or {}).get("selected_race") or {}
            if (session.count_phase == "confirmed" and session.scheduled_entry_consumed is True
                and decision.get("tap") is None and selected.get("grade") == "G1"
                and selected.get("race_id") == session.planned_race_ids[0]
                and selected.get("source_image_sha256") == source_sha):
                return None
            return "count_race:post_confirm_wait_unverified"
        allowed = {
            "new": {("tap", "enter_race_from_main_menu", "ready_main_menu", "main_menu.race")},
            "opened": {("tap", "enter_selected_target_race", "race_list", "race_list.primary_entry")},
            "entered": {("tap", "confirm_race_entry", "race_entry_confirm", "race_confirm.primary"),
                        ("tap", "confirm_race_entry", "race_detail_confirm", "race_confirm.primary")},
            "confirmed": {("tap", "view_race_result", "race_preview", "race_detail.view_result"),
                          ("tap", "go_to_race_from_detail", "race_detail", "race_detail.go_to_race"),
                          ("tap", "race_scene_start_or_continue", "race_scene", "race_scene.start_or_continue"),
                          ("tap", "skip_or_advance_race_scene", "race_scene", "race_scene.skip_or_advance"),
                          ("tap", "confirm_race_playback", "race_playback_confirm", "race_confirm.primary"),
                          ("tap", "race_result_continue", "race_result", "race_result.continue")},
            "watching": {("tap", "race_scene_start_or_continue", "race_scene", "race_scene.start_or_continue"),
                         ("tap", "skip_or_advance_race_scene", "race_scene", "race_scene.skip_or_advance"),
                         ("tap", "race_result_continue", "race_result", "race_result.continue")},
        }
        if decision.get("action") == "wait" and decision.get("route_state") in {"loading", "race_scene"}:
            return None
        if identity not in allowed.get(session.count_phase, set()):
            return "count_race:action_out_of_scope"
        if decision.get("reason") in {"race_scene_start_or_continue", "skip_or_advance_race_scene"} and policy.allow_skip_race_scene is not True:
            return "count_race:scene_permission_required"
        if decision.get("reason") == "confirm_race_playback":
            img = cv2.imread(str((decision.get("evidence") or {}).get("image", "")))
            if (policy.allow_confirm_race is not True or decision.get("consume_target_race_verification")
                or decision.get("target_race_verification") is not None
                or img is None or not RaceRoutePlanner._looks_like_race_playback_modal(img)):
                return "count_race:playback_unverified"
        if decision.get("reason") == "go_to_race_from_detail":
            source = Path((decision.get("evidence") or {}).get("image", ""))
            img = cv2.imread(str(source))
            points = RaceRoutePlanner._race_preview_action_points(img) if img is not None else None
            tap = decision.get("tap") or {}
            if (not session.scheduled_entry_consumed or policy.allow_confirm_race is not True
                or points is None or points.get("view_result_locked") is not True
                or (tap.get("x"), tap.get("y")) != points["go_to_race"]
                or hashlib.sha256(source.read_bytes()).hexdigest() != source_sha):
                return "count_race:locked_preview_unverified"
        if session.count_phase == "opened":
            proof = decision.get("target_race_verification") or {}
            if (proof.get("race_id") != session.planned_race_ids[0] or proof.get("grade") != "G1"
                or proof.get("schedule_packet_sha256") != session.schedule_packet_sha256
                or proof.get("source_image_sha256") != source_sha):
                return "count_race:selected_proof_invalid"
        if session.count_phase == "entered" and (decision.get("consume_target_race_verification") is not True
            or (decision.get("evidence") or {}).get("prior_target_race_verification") != session.target_race_verification()):
            return "count_race:confirmation_proof_invalid"
        return None

    @staticmethod
    def _wbc_scope_error(
        decision: dict[str, Any], session: RaceRouteSession | None,
        policy: RaceRoutePolicy, source_path: Path, visible_text: str | None,
    ) -> str | None:
        if session is None:
            return "wbc_only:session_required"
        if session.planned_race_ids or session.schedule_packet_sha256 is not None:
            return "wbc_only:scheduled_binding_forbidden"
        if any(value is not None for value in (
            session.pending_target_race, session.pending_locked_target_race,
            session.pending_scenario_final_race, session.pending_fan_goal_recommendation,
            session.pending_fan_goal_shortfall_selected, session.pending_gur_race,
        )):
            return "wbc_only:other_capability_pending"
        pending = session.pending_wbc_hub_race
        confirmed = session.wbc_entry_confirmed
        if type(confirmed) is not bool or (pending is not None and confirmed):
            return "wbc_only:phase_conflict"
        action, reason, state = (decision.get(key) for key in ("action", "reason", "route_state"))
        tap = decision.get("tap")
        action_id = tap.get("coordinate_name") if isinstance(tap, dict) else None
        triplet = (reason, state, action_id)
        evidence = decision.get("evidence") or {}
        if not evidence.get("image") or Path(evidence["image"]).resolve() != source_path.resolve():
            return "wbc_only:decision_source_mismatch"
        if any(decision.get(key) is not None for key in (
            "target_race_verification", "locked_target_verification", "scenario_final_verification",
            "fan_goal_recommendation_verification", "fan_goal_shortfall_selected_verification",
            "gur_race_verification", "consume_target_race_verification",
            "consume_scenario_final_verification", "consume_fan_goal_recommendation_verification",
            "consume_fan_goal_shortfall_selected_verification", "consume_gur_race_verification",
        )):
            return "wbc_only:other_action_capability"
        if (
            decision.get("wbc_hub_verification") is not None
            and triplet != ("enter_race_from_wbc_hub", "wbc_race_hub", "wbc_hub.race")
        ) or (
            decision.get("consume_wbc_hub_verification") is not None
            and triplet != ("confirm_wbc_hub_race", "race_detail_confirm", "race_confirm.wbc_hub")
        ):
            return "wbc_only:phase_capability_mismatch"
        if action == "stop" or is_route_handoff(decision):
            if tap is not None:
                return "wbc_only:action_out_of_scope"
            if reason in {"post_race_dialogue", "event_choice", "ready_main_menu", "ready_training_menu",
                          "goal_achievement_loading", "goal_achievement_result"} and not confirmed:
                return "wbc_only:confirmation_required"
            return None  # Other stops are diagnostic; they do not grant a handoff.
        if action == "wait":
            waits = {("loading", "loading"), ("loading", "race_detail_loading"),
                     ("transition", "transition")}
            if confirmed:
                waits |= {(reason, "race_scene") for reason in RACE_SCENE_WAIT_REASONS}
            if (pending is not None or confirmed) and (reason, state) in waits and tap is None:
                return None
            return "wbc_only:action_out_of_scope"
        if action != "tap" or not isinstance(tap, dict):
            return "wbc_only:action_out_of_scope"
        if pending is None and not confirmed:
            if triplet != ("enter_race_from_wbc_hub", "wbc_race_hub", "wbc_hub.race"):
                return "wbc_only:entry_required"
            image = cv2.imread(str(source_path))
            if policy.allow_enter_race is not True or image is None or not RaceRoutePlanner._looks_like_wbc_race_hub(image):
                return "wbc_only:hub_verification_required"
            try:
                replace(session).stage_wbc_hub_race_entry(decision)
            except ValueError as exc:
                return f"wbc_only:{exc}"
            return None
        if pending is not None:
            if triplet != ("confirm_wbc_hub_race", "race_detail_confirm", "race_confirm.wbc_hub"):
                return "wbc_only:confirmation_required"
            verification = evidence.get("wbc_hub_verification")
            confirmation_img = cv2.imread(str(source_path))
            confirmation_text = analyze_screen_text(visible_text or "")
            confirmation_perception = (
                Perception().analyze_image(confirmation_img)
                if confirmation_img is not None
                else {}
            )
            if confirmation_img is not None:
                confirmation_perception = fuse_perception_with_text(
                    confirmation_perception,
                    confirmation_text,
                )
            confirmation = RaceRoutePlanner._wbc_race_confirmation(
                {
                    "screen_text": confirmation_text,
                    "perception": confirmation_perception,
                },
                confirmation_img,
            )
            if (
                policy.allow_confirm_race is not True
                or decision.get("consume_wbc_hub_verification") is not True
                or not isinstance(pending, dict) or not isinstance(verification, dict)
                or not all(verification.get(key) == value for key, value in pending.items())
                or pending.get("source") != "wbc_race_hub" or pending.get("scenario") != "grand_masters"
                or pending.get("action_id") != "wbc_hub.race" or pending.get("single_use") is not True
                or re.fullmatch(r"[0-9a-f]{64}", str(pending.get("source_image_sha256", ""))) is None
                or pending.get("source_image_sha256") == hashlib.sha256(source_path.read_bytes()).hexdigest()
                or confirmation is None
                or verification.get("confirmation_source") != confirmation["source"]
            ):
                return "wbc_only:confirmation_verification_required"
            return None
        permissions = {
            ("view_race_result", "race_preview", "race_detail.view_result"): policy.allow_quick_result is True,
            ("go_to_race_from_detail", "race_detail", "race_detail.go_to_race"):
                policy.allow_locked_race_entry is True and (policy.allow_confirm_race is True or policy.allow_skip_race_scene is True),
            ("race_scene_start_or_continue", "race_scene", "race_scene.start_or_continue"):
                policy.allow_enter_race is True or policy.allow_confirm_race is True,
            ("skip_or_advance_race_scene", "race_scene", "race_scene.skip_or_advance"): policy.allow_skip_race_scene is True,
            ("race_result_continue", "race_result", "race_result.continue"): policy.allow_result_continue is True,
            ("close_landscape_race_menu", "race_menu", "race_menu.close"): True,
        }
        if permissions.get(triplet) is not True:
            return "wbc_only:action_out_of_scope"
        if reason == "go_to_race_from_detail":
            image = cv2.imread(str(source_path))
            preview = RaceRoutePlanner._race_preview_action_points(image) if image is not None else None
            if preview is None or preview.get("view_result_locked") is not True:
                return "wbc_only:locked_preview_required"
        return None

    @staticmethod
    def _default_adb_factory() -> Any:
        from core.adb import ADB

        return ADB()
