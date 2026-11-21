"""Read-only game navigation: information, bounded scroll, close, same tab.

No training/SS confirmation or Android BACK action exists in this executor.
The existing route session and formal frame-safety gate retain authority.
"""
from dataclasses import asdict, replace
import hashlib
from pathlib import Path
import json
import time

import cv2
import numpy as np

from core.auto_trainer.frame_safety import FrameSafetyAnalyzer, FormalActionSafetyGate
from core.auto_trainer.route_transition_session import RouteTransitionSession
from core.larc_ss_information import LarcSSInformationReader, _runs
from core.training_menu_precheck import TrainingMenuPrechecker
from core.execution_frames import tap_without_settle


PROBE_CONFIRM_TOKEN = "CAPTURE_LARC_SS_INFORMATION"
MAX_SCROLLS = 6
ASSETS = Path(__file__).resolve().parents[2] / "assets/larc_support"


def _navigation_template(kind):
    """Decode the existing tiny control assets without Windows narrow paths."""
    path = ASSETS / f"ss_information_{kind}.png"
    try:
        raw = path.read_bytes()
        template = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    except (OSError, cv2.error) as error:
        raise ValueError("information_template_unavailable") from error
    if template is None:
        raise ValueError("information_template_unavailable")
    return template


def _close_only_navigation(image, *, reader=None):
    """Verify only this frame's exact information title and Close control.

    Unreadable reward rows stay unknown. This is not an information scan and
    does not establish participant rewards, numeric values or a completed scan.
    """
    from core.auto_trainer.larc_preview_text import _frame, _reader, _line
    from core.auto_trainer.transition_wait import connecting_overlay_visible
    from core.auto_trainer.larc_control_wait import modal_content_pending
    from core.auto_trainer.safe_advance import SafeAdvancePlanner
    frame = _frame(image)
    if (frame is None or connecting_overlay_visible(frame) or modal_content_pending(frame)
            or SafeAdvancePlanner._looks_like_confirm_modal_panel(frame)):
        return None
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    if ((hsv[40:85,120:580,1]>100)&(hsv[40:85,120:580,2]>190)).mean()<.55:
        return None
    try:
        read = _reader(reader)
        title = _line(frame, (170,30,550,97), read)
        if title != "群星赛信息":
            return None
        # These are the established navigation ROI/template, not desktop
        # coordinates and not a general modal-close heuristic.
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        template = _navigation_template("close")
        x1,y1,x2,y2 = 215,1138,510,1234
        _,score,_,loc = cv2.minMaxLoc(cv2.matchTemplate(
            grey[y1:y2,x1:x2], template, cv2.TM_CCOEFF_NORMED))
        if score < .85:
            return None
        label = frame[1148:1228,250:470]
        colors = cv2.cvtColor(label, cv2.COLOR_BGR2HSV)
        points = cv2.findNonZero(((colors[:,:,0]>=8)&(colors[:,:,0]<40)
            &(colors[:,:,1]>45)&(colors[:,:,2]<200)).astype('uint8'))
        if points is None:
            return None
        x,y,w,h = cv2.boundingRect(points)
        if w<25 or h<15:
            return None
        close = _line(label, (max(0,x-2),max(0,y-2),
            min(label.shape[1],x+w+2),min(label.shape[0],y+h+2)), read)
        if close != "关闭":
            return None
        width,height = image.shape[1],image.shape[0]
        point = [round((x1+loc[0]+template.shape[1]/2)*width/720),
                 round((y1+loc[1]+template.shape[0]/2)*height/1280)]
        return {"page_verified":True, "close":point,
                "verified_text":title+"\n"+close,
                "verification_scope":"current_frame_title_and_close_only"}
    except (OSError,RuntimeError,ValueError,TypeError,cv2.error):
        return None


def _same_training_hud(before, after):
    return _training_hud_comparison(before, after)["same"]


def _training_hud_comparison(before, after):
    """Preserve the pixel fast path; supplement only already verified tabs.

    Navigation can alter a stat panel's shading without changing its numbers.
    The existing numeric glyph reader supplies a second description of those
    same fixed slots. A complete matching countdown, five stats and Pt plus
    the unchanged calendar header can establish the same neutral-navigation
    return. Unreadable or changed values never turn into invented rewards.
    """
    old=cv2.resize(before,(720,1280));new=cv2.resize(after,(720,1280))
    rois=[(35,67,119,120),(165,44,370,70)]
    rois.extend((x1,856,x2,906) for x1,x2 in
                ((74,137),(187,249),(299,363),(411,477),(525,589),(612,695)))
    names=('remaining_turns','calendar','speed','stamina','power','guts','wisdom','skill_pt')
    scores={name:float(cv2.matchTemplate(old[y1:y2,x1:x2],new[y1:y2,x1:x2],
        cv2.TM_CCOEFF_NORMED).item()) for name,(x1,y1,x2,y2) in zip(names,rois)}
    result={"same":all(score>=.985 for score in scores.values()),
            "method":"pixels", "pixel_scores":scores, "semantic_fields":{},
            "conflict_fields":[]}
    if result["same"]:
        return result
    try:
        from core.main_menu_state import NUMERIC_FIELDS, _analyze_numeric_field, numeric_field_accepted
        for name in NUMERIC_FIELDS:
            left=_analyze_numeric_field(name,old,scenario="larc")
            right=_analyze_numeric_field(name,new,scenario="larc")
            accepted_left=numeric_field_accepted(left)
            accepted_right=numeric_field_accepted(right)
            result["semantic_fields"][name]={"before":left.get("value"),"after":right.get("value"),
                "before_accepted":accepted_left,"after_accepted":accepted_right}
            if accepted_left and accepted_right and left["value"]!=right["value"]:
                result["conflict_fields"].append(name)
        complete=all(row["before_accepted"] and row["after_accepted"]
                     for row in result["semantic_fields"].values())
        result["same"]=(complete and not result["conflict_fields"] and scores["calendar"]>=.985)
        result["method"]="numeric_semantics" if result["same"] else "unconfirmed"
    except (ValueError,OSError,RuntimeError,cv2.error):
        # This supplements an old pixel rejection, never weakens its fast path
        # or creates a new failure condition when a local template is absent.
        result["method"]="semantic_reader_unavailable"
    return result


def _read_locked_ss_slot(canonical, width, height):
    """Own pre-debut layout: disabled grey sixth button plus gold padlock/keyhole."""
    hsv = cv2.cvtColor(canonical, cv2.COLOR_BGR2HSV)
    body = hsv[1055:1105, 627:681]
    disabled = float(((body[:, :, 1] < 55) & (body[:, :, 2] >= 60)
                      & (body[:, :, 2] <= 185)).mean())
    lock = hsv[1022:1052, 599:624]
    gold = float(((lock[:, :, 0] >= 12) & (lock[:, :, 0] <= 35)
                  & (lock[:, :, 1] > 100) & (lock[:, :, 2] > 160)).mean())
    key = hsv[1036:1048, 605:620]
    dark = ((key[:, :, 0] >= 8) & (key[:, :, 0] <= 35)
            & (key[:, :, 1] > 90) & (key[:, :, 2] < 150)).astype(np.uint8)
    components = cv2.connectedComponentsWithStats(dark, 8)[2][1:]
    keyhole = any(5 <= w <= 9 and 9 <= h <= 12 and 35 <= area <= 75
                  for _, _, w, h, area in components)
    if disabled < .90 or gold < .55 or not keyhole:
        return None
    return {"status": "accepted", "reason": "gold_padlock_on_disabled_ss_slot",
            "bbox": [round(598*width/720), round(1020*height/1280),
                     round(709*width/720), round(1135*height/1280)],
            "disabled_ratio": round(disabled, 4), "gold_ratio": round(gold, 4)}


def read_navigation(image, *, reader=None):
    reader = reader or LarcSSInformationReader()
    info = reader.read(image)
    result = {"page_verified": info["page_verified"], "open": None, "close": None,
              "scroll": None, "information": info, "ss_locked": None}
    h, w = image.shape[:2]
    if not 0.55 <= w / h <= 0.575:
        return result
    canonical = cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
    grey = cv2.cvtColor(canonical, cv2.COLOR_BGR2GRAY)
    kind = "close" if info["page_verified"] else "open"
    roi = (215, 1138, 510, 1234) if kind == "close" else (10, 295, 185, 389)
    template = _navigation_template(kind)
    x1, y1, x2, y2 = roi
    scores = cv2.matchTemplate(grey[y1:y2, x1:x2], template, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(scores)
    if score >= 0.85:
        result[kind] = [round((x1 + loc[0] + template.shape[1]/2)*w/720),
                        round((y1 + loc[1] + template.shape[0]/2)*h/1280)]
    if not info["page_verified"] and result["open"] is None:
        result["ss_locked"] = _read_locked_ss_slot(canonical, w, h)
    if info["page_verified"]:
        hsv = cv2.cvtColor(canonical, cv2.COLOR_BGR2HSV)
        # The grey thumb is inside a fixed narrow track, not a person/card edge.
        candidates = []
        for x in range(690, 699):
            stripe = hsv[160:1110, x]
            for start, end in _runs((stripe[:, 1] < 50) & (stripe[:, 2] > 65) & (stripe[:, 2] < 190)):
                if 150 <= end-start <= 750:
                    candidates.append((start, end))
        if len(candidates) >= 3:
            start, end = np.median(candidates, axis=0).astype(int).tolist()
            agreeing = sum(abs(a-start) <= 4 and abs(b-end) <= 4 for a,b in candidates)
            if agreeing >= 3:
                result["scroll"] = {"at_top": start <= 12, "at_bottom": end >= 930,
                                    "thumb": [start+160, end+160]}
    return result


class LarcSSInformationProbe:
    def __init__(self, *, adb, safety_analyzer=None, sleep_fn=time.sleep):
        self.adb = adb
        self.safety = safety_analyzer or FrameSafetyAnalyzer()
        self.gate = FormalActionSafetyGate()
        self.sleep = sleep_fn
        self.reader = LarcSSInformationReader()
        self.prechecker = TrainingMenuPrechecker(scenario="larc")

    def run(self, *, run_session_id, turn_index, roster, roster_path,
            transition_session_path, output_dir, execute, confirm_token):
        report = {"schema_version": "larc_ss_information_probe.v1", "status": "stopped",
                  "run_session_id": run_session_id, "turn_index": turn_index,
                  "stage": "entry",
                  "returned_to_training": False, "turn_consumed": None,
                  "turn_consuming_action_executed": False,
                  "steps": [], "observations": [], "decision_safe": False}
        if not execute or confirm_token != PROBE_CONFIRM_TOKEN:
            return dict(report, reason="execution_gate_required")
        route = RouteTransitionSession.load(transition_session_path, expected_run_session_id=run_session_id)
        route_save_path = transition_session_path
        roster._check_source({"run_session_id": run_session_id, "turn_index": turn_index})
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=False)  # Never overwrite earlier evidence.
        sequence = 0
        timings={key:0. for key in ('capture_seconds','information_read_seconds','safety_text_seconds','input_seconds','wait_seconds')}
        report['timings']=timings
        report['navigation_recoveries']=[]
        safety_cache={}
        before = initial = None
        close_attempted = False
        close_confirmed = False

        def sleep(seconds):
            started=time.monotonic();self.sleep(seconds)
            timings['wait_seconds']+=time.monotonic()-started

        def safety_packet(path,image,nav=None,*,close_only_text=None):
            started=time.monotonic()
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            if close_only_text is not None:
                digest += ':verified_information_close_only'
            if digest in safety_cache:
                report['safety_cache_hits']=report.get('safety_cache_hits',0)+1
                return safety_cache[digest]
            from core.auto_trainer.larc_preview_text import default_local_reader
            from core.auto_trainer.larc_ss_information_text import information_text
            text=(close_only_text or information_text(image,nav or {})) if default_local_reader(self.safety) else None
            packet=self.safety.analyze(path, supplied_text=text) if text else self.safety.analyze(path)
            if text:packet=replace(packet,text_source='larc_ss_information_regions')
            safety_cache[digest]=packet
            timings['safety_text_seconds']+=time.monotonic()-started
            return packet

        def capture_frame():
            nonlocal sequence
            # An unsuccessful new capture does not own the preceding frame.
            report["last_source"]=None
            path = root / f"frame_{sequence:02d}.png"
            sequence += 1
            started=time.monotonic()
            captured=self.adb.screenshot(str(path))
            if captured is None or not path.is_file():
                raise ValueError("capture_failed")
            image = captured if isinstance(captured,np.ndarray) else cv2.imdecode(
                np.frombuffer(path.read_bytes(),dtype=np.uint8),cv2.IMREAD_COLOR)
            timings['capture_seconds']+=time.monotonic()-started
            if image is None or (image.shape[1], image.shape[0]) not in {(720,1280), (1440,2560)}:
                raise ValueError("unsupported_capture")
            report["last_source"]={"run_session_id":run_session_id,"turn_index":turn_index,
                "path":str(path.resolve()),"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
            return path, image

        def capture():
            path,image = capture_frame()
            started=time.monotonic()
            try:
                nav=read_navigation(image, reader=self.reader)
            except (ValueError,OSError,RuntimeError,cv2.error) as read_error:
                # Reward rows are optional, but the current title and exact
                # free Close control must still be recognized independently.
                close_only=_close_only_navigation(image)
                if close_only is None:
                    raise
                report.setdefault("read_failures",[]).append({
                    "reason":str(read_error),"source":dict(report["last_source"]),
                    "verification_scope":"current_frame_title_and_close_only"})
                nav={**close_only,"open":None,"scroll":None,"ss_locked":None,
                    "information":{"page_verified":True,"status":"abstained",
                        "reason":"reward_rows_unavailable","members":[],"decision_safe":False}}
            timings['information_read_seconds']+=time.monotonic()-started
            return path, image, nav

        def source(path):
            return {"run_session_id": run_session_id, "turn_index": turn_index,
                    "absolute_path": str(path.resolve())}

        def observe_route(state, path, *, resync=False):
            # Reuse the established provenance hash, required by the route gate.
            import hashlib
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            try:
                route.observe(state, source_sha256=sha, resync=resync)
            finally:
                route.save(route_save_path)
            return sha

        def act(kind, point, path, state, next_state, image, nav=None, *, close_only_text=None, cleanup=False):
            nonlocal close_attempted, close_confirmed
            report["stage"]=kind
            action_id = f"larc_ss_information.{kind}"
            decision = {"action": "swipe" if kind.startswith("scroll") else "tap",
                        "reason": action_id, "route_state": state}
            if decision["action"] == "tap":
                decision["tap"] = {"coordinate_name": action_id, "x": point[0], "y": point[1], "scaled": False}
            else:
                w,h = image.shape[1],image.shape[0]
                y1,y2 = (950,410) if kind == "scroll_down" else (410,950)
                point = [round(580*w/720),round(y1*h/1280),round(580*w/720),round(y2*h/1280)]
                decision["gesture"] = {"coordinate_name": action_id, "points": point, "duration_ms": 450}
            packet = safety_packet(path,image,nav,close_only_text=close_only_text)
            verdict = self.gate.evaluate(packet, decision)
            step = {"source": source(path), "source_sha256": packet.source_image_sha256,
                    "decision": decision, "safety_verdict": verdict.to_dict(), "input_performed": False}
            report["steps"].append(step)
            step['text_source']=packet.text_source
            if not verdict.allowed:
                raise ValueError(verdict.reason)
            sha = observe_route(state, path, resync=cleanup)
            if sha != packet.source_image_sha256:
                raise ValueError("action_source_changed")
            started=time.monotonic()
            if decision["action"] == "tap":
                if kind == "close":
                    close_attempted = True
                if cleanup:
                    from core.auto_trainer.runtime_control import input_guard
                    with input_guard(self.adb):
                        ok = tap_without_settle(self.adb,*point,scaled=False)
                else:
                    ok = tap_without_settle(self.adb,*point,scaled=False)
            else:
                ok = self.adb.swipe(*point, duration_ms=450, scaled=False)
            timings['input_seconds']+=time.monotonic()-started
            if ok is not True:
                step['input_error']='input_not_confirmed'
                if kind.startswith('scroll') and len(report['navigation_recoveries'])<1:
                    # A swipe may have happened even when its transport failed.
                    # Observe the current information page before choosing a new
                    # direction; never replay training/SS/resource confirmation.
                    fresh_path,fresh_image,fresh_nav=capture()
                    fresh_packet=safety_packet(fresh_path,fresh_image,fresh_nav)
                    if (fresh_nav['page_verified'] and fresh_nav['close'] is not None
                            and fresh_packet.text_status=='ok' and not fresh_packet.dangerous):
                        observe_route('larc_ss_information',fresh_path,resync=True)
                        recovery={'reason':'scroll_reobserved_after_unconfirmed_input','step':len(report['steps']),
                                  'source':source(fresh_path),'input_replayed':False}
                        report['navigation_recoveries'].append(recovery)
                        step['reobserved']=True
                        return fresh_path,fresh_image,fresh_nav
                raise ValueError("input_not_confirmed")
            step["input_performed"] = True
            if kind == "close":
                close_confirmed = True
            route.commit_action(action_id, expected_next_states=(next_state,), source_sha256=sha)
            route.save(route_save_path)
            # adb.swipe already settles for SWIPE_DELAY. Do not add a second
            # fixed delay; tap transitions retain their observed readiness wait.
            if decision['action']=='tap':sleep(0.15)

        def wait_for_training_return(path,image, *, close_recovery=True):
            started=time.monotonic()
            returned=self.prechecker.check(path,expected_tab=before.selected_tab)
            comparison=_training_hud_comparison(initial,image) if returned.accepted and initial is not None else None
            reads=1
            for _ in range(20):
                if ((returned.accepted and (initial is None or comparison["same"]))
                        or (comparison is not None and comparison["conflict_fields"])
                        or time.monotonic()-started>=4.):
                    break
                # A structurally ready tab can still be fading its number row.
                # Read only; neither the successful nor uncertain Close is sent
                # again. A confidently changed number retains the old stop.
                sleep(.15)
                path,image=capture_frame()
                reads+=1
                returned=self.prechecker.check(path,expected_tab=before.selected_tab)
                comparison=_training_hud_comparison(initial,image) if returned.accepted and initial is not None else None
            readiness={"reads":reads,"seconds":round(time.monotonic()-started,3)}
            if not returned.accepted and close_confirmed and close_recovery:
                # A confirmed transport tap can land during the final scroll
                # animation and leave this free information card open. After
                # the normal return wait, verify the current title and exact
                # Close again before one bounded retry. An uncertain input,
                # another page or a changed HUD never enters this branch.
                navigation=_close_only_navigation(image)
                if navigation is not None:
                    recovery={"reason":"confirmed_close_left_information_open",
                        "source":source(path),"source_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
                        "previous_close_input_confirmed":True,"retry_limit":1}
                    report.setdefault("close_recoveries",[]).append(recovery)
                    act("close",navigation["close"],path,"larc_ss_information","training_menu",
                        image,close_only_text=navigation["verified_text"],cleanup=True)
                    report["stage"]="return"
                    path,image=capture_frame()
                    path,image,returned,comparison,retry_ready=wait_for_training_return(
                        path,image,close_recovery=False)
                    recovery["return_readiness"]=retry_ready
                    readiness["reads"]+=retry_ready["reads"]
                    readiness["seconds"]=round(time.monotonic()-started,3)
                    readiness["confirmed_close_retry"]=True
            return path,image,returned,comparison,readiness

        def return_after_read_failure():
            from core.auto_trainer.runtime_control import device_control, current, RunStopped
            control = device_control(self.adb) or current()
            if control is not None and control.stopping:
                raise RunStopped()
            report["stage"]="cleanup"
            path,image = capture_frame()  # No reward-row reader for cleanup.
            returned = self.prechecker.check(path,expected_tab=before.selected_tab)
            cleanup_report = report["failure_cleanup"]
            if not returned.accepted:
                navigation = _close_only_navigation(image)
                if navigation is None:
                    raise ValueError("information_cleanup_page_unverified")
                if close_attempted:
                    # An uncertain close is not replayed. The fresh training
                    # check above may confirm it; otherwise retain the stop.
                    raise ValueError("information_cleanup_close_already_attempted")
                act("close",navigation["close"],path,"larc_ss_information","training_menu",
                    image,close_only_text=navigation["verified_text"],cleanup=True)
                cleanup_report["close_input_performed"] = True
                path,image = capture_frame()
            report["stage"]="return"
            path,image,returned,comparison,readiness=wait_for_training_return(path,image)
            cleanup_report["return_readiness"]=readiness
            cleanup_report["return_precheck"] = asdict(returned)
            if not returned.accepted:
                raise ValueError("return_training_unverified")
            same_hud = comparison["same"]
            report["hud_comparison"]=comparison
            cleanup_report["same_training_hud"] = same_hud
            if not same_hud:
                raise ValueError("training_hud_changed")
            observe_route("training_menu",path,resync=True)
            report.update(returned_to_training=True,turn_consumed=False,same_training_hud=True)
            report["return_source"]=dict(report["last_source"])
            cleanup_report.update(status="returned_to_training",source=source(path),
                information_scan_completed=False)

        try:
            if not self.adb.connect():
                raise ValueError("adb_connect_failed")
            path, image, nav = capture()
            before = self.prechecker.check(path)
            entry_started=time.monotonic()
            entry_reads=1
            for _ in range(20):
                if before.accepted or nav["page_verified"] or time.monotonic()-entry_started>=4.:
                    break
                sleep(.15)
                path,image,nav=capture()
                entry_reads+=1
                before=self.prechecker.check(path)
            report["entry_readiness"]={"reads":entry_reads,
                "seconds":round(time.monotonic()-entry_started,3)}
            report["entry_precheck"] = asdict(before)
            if nav["page_verified"] and nav["close"] is not None:
                # A prior interrupted scan (or manual opening) can leave this
                # free information card visible. Closing it needs no old HUD.
                # Keep the main route and any unresolved consuming action
                # intact; the caller must collect fresh training state next.
                navigation=_close_only_navigation(image)
                if navigation is None:
                    raise ValueError("information_cleanup_page_unverified")
                report["recovered_existing_information"]=True
                route=RouteTransitionSession.new(run_session_id)
                route_save_path=root/"close_only_navigation.json"
                act("close",navigation["close"],path,"larc_ss_information","training_menu",
                    image,close_only_text=navigation["verified_text"],cleanup=True)
                report["stage"]="return"
                path,image=capture_frame()
                path,image,returned,comparison,readiness=wait_for_training_return(path,image)
                report["return_readiness"]=readiness
                report["return_precheck"]=asdict(returned)
                if not returned.accepted:
                    raise ValueError("return_training_unverified")
                observe_route("training_menu",path)
                report.update(status="partial",reason="existing_information_closed",
                    returned_to_training=True,turn_consumed=False,
                    fresh_training_capture_required=True,main_transition_session_preserved=True)
                report["return_source"]=dict(report["last_source"])
                return report
            if not before.accepted or before.selected_tab == "ss":
                raise ValueError("information_entry_unverified")
            if nav["open"] is None and nav["ss_locked"] is not None:
                sha = observe_route("training_menu", path)
                report.update(status="completed", reason="ss_not_unlocked",
                              returned_to_training=True, turn_consumed=False,
                              ss_availability={"status": "locked", "source": dict(source(path), sha256=sha),
                                               "evidence": nav["ss_locked"]})
                # No navigation occurred. Do not cache an information scan:
                # a later unlocked frame must still be collected normally.
                return report
            if nav["open"] is None:
                raise ValueError("information_entry_unverified")
            initial = image.copy()
            report["entry_source"]=dict(report["last_source"])
            act("open", nav["open"], path, "training_menu", "larc_ss_information", image,nav)
            path, image, nav = capture()
            open_frames=[(path,image.copy())]
            open_started=time.monotonic()
            for _ in range(20):
                if (nav["page_verified"] or time.monotonic()-open_started>=4.
                        or (len(open_frames)>=3
                            and self.prechecker.check(path,expected_tab=before.selected_tab).accepted)):
                    break
                sleep(.2)
                path,image,nav=capture()
                open_frames.append((path,image.copy()))
            report["open_readiness"]={"reads":len(open_frames),
                "seconds":round(time.monotonic()-open_started,3)}
            if not nav["page_verified"] and self.prechecker.check(path,expected_tab=before.selected_tab).accepted:
                report["hud_comparison"]=_training_hud_comparison(initial,image)
                report["same_training_hud"]=report["hud_comparison"]["same"]
                if not report["same_training_hud"]:
                    raise ValueError("training_hud_changed")
                if len(open_frames)>=3 and all(np.array_equal(open_frames[-1][1],item[1]) for item in open_frames[-3:]):
                    report['freeze_frames']=[str(item[0]) for item in open_frames[-3:]]
                    raise ValueError('information_open_frozen')
                observe_route("training_menu",path,resync=True)
                report.update(status="partial",reason="information_open_deferred",
                              returned_to_training=True,turn_consumed=False)
                report["return_source"]=dict(report["last_source"])
                return report
            reached_top = False
            reached_bottom = False
            up_count = down_count = 0
            report["stage"]="collect"
            while nav["page_verified"] and nav["close"] is not None:
                frame_sha = observe_route("larc_ss_information", path)
                scroll = nav["scroll"]
                reached_top = reached_top or bool(scroll and scroll["at_top"])
                if reached_top and nav["information"]["status"] == "accepted":
                    report["observations"].append({"source": dict(source(path), sha256=frame_sha),
                                                   "result": nav["information"]})
                if scroll is None:
                    break
                if not reached_top:
                    if up_count >= 3:
                        break
                    kind = "scroll_up"
                    up_count += 1
                elif scroll["at_bottom"]:
                    reached_bottom = True
                    break
                else:
                    if down_count >= MAX_SCROLLS:
                        break
                    kind = "scroll_down"
                    down_count += 1
                recovered=act(kind, None, path, "larc_ss_information", "larc_ss_information", image,nav)
                path, image, nav = recovered or capture()
            if not nav["page_verified"] or nav["close"] is None:
                raise ValueError("information_page_lost")
            act("close", nav["close"], path, "larc_ss_information", "training_menu", image,nav,
                close_only_text=nav.get("verified_text"))
            report["stage"]="return"
            path,image=capture_frame()
            path,image,returned,comparison,readiness=wait_for_training_return(path,image)
            report['return_readiness']=readiness
            report["return_precheck"] = asdict(returned)
            if not returned.accepted:
                raise ValueError("return_training_unverified")
            observe_route("training_menu", path)
            report["returned_to_training"] = True
            # Compare each numeric/date field, not a whole-header mean: one
            # changed countdown digit would be diluted by the white background.
            report["hud_comparison"]=comparison
            report["same_training_hud"] = comparison["same"]
            if not report["same_training_hud"]:
                raise ValueError("training_hud_changed")
            report["turn_consumed"] = False
            report["return_source"]=dict(report["last_source"])
            # Keep uncommitted observations in the report if return verification
            # fails; never associate their rewards with an unverified turn.
            for item in report["observations"]:
                frame = cv2.imdecode(np.frombuffer(
                    Path(item["source"]["absolute_path"]).read_bytes(),dtype=np.uint8),cv2.IMREAD_COLOR)
                item["result"] = roster.observe_information(frame, item["result"], source=item["source"])
            complete = reached_top and reached_bottom and bool(report["observations"])
            if complete:
                roster.mark_information_scan_complete(source=source(path))
            report.update(status="completed" if complete else "partial",
                          reason="returned_after_collection" if complete else "scroll_coverage_incomplete")
        except (ValueError, OSError, RuntimeError, cv2.error) as exc:
            report["reason"] = str(exc)
            report["failure_source"]=(dict(report["last_source"])
                if report.get("last_source") else None)
            report["failure_stage"]=report["stage"]
            if initial is not None and before is not None and not report["returned_to_training"]:
                report["failure_cleanup"] = {"status":"not_returned",
                    "original_reason":str(exc),"close_input_performed":False}
                try:
                    return_after_read_failure()
                except (ValueError,OSError,RuntimeError) as cleanup_error:
                    report["failure_cleanup"]["reason"] = str(cleanup_error)
            # Keep the original failure/status. Only the existing caller may
            # downgrade optional initialization after a verified safe return.

        finally:
            roster.save(roster_path)
            (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
