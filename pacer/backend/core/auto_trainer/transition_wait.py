"""Shared read-only transition handling; never retries or authorizes input."""
import time

import cv2
import numpy as np

if __package__:
    from .thresholds import UNKNOWN_TRANSITION_IDLE_SECONDS, UNKNOWN_TRANSITION_TOTAL_SECONDS
else:
    # D1 result settlement loads this file after swapping in its own core package.
    # Read the adjacent Pacer constants by path without importing that package.
    from pathlib import Path
    from runpy import run_path

    _thresholds = run_path(str(Path(__file__).with_name("thresholds.py")))
    UNKNOWN_TRANSITION_IDLE_SECONDS = _thresholds["UNKNOWN_TRANSITION_IDLE_SECONDS"]
    UNKNOWN_TRANSITION_TOTAL_SECONDS = _thresholds["UNKNOWN_TRANSITION_TOTAL_SECONDS"]

try:
    from core.auto_trainer.runtime_control import current
except ModuleNotFoundError as error:
    if __package__ or error.name not in {"core", "core.auto_trainer", "core.auto_trainer.runtime_control"}:
        raise
    # Isolated D1 workers load this read-only policy by path. Their device
    # transport retains its separate parent-control pipe; this fallback only
    # means no local pause-duration adjustment is available for the budget.
    def current():
        return None


def career_comic_loading_visible(image):
    """White comic splash with the lower-right loading caption and mascot."""
    if not isinstance(image, np.ndarray) or image.size == 0 or image.shape[1] >= image.shape[0]:
        return False
    if image.shape[:2] != (1280, 720):
        image = cv2.resize(image, (720, 1280))

    def white_fraction(region):
        hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
        return float(((hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 235)).mean())

    # Most frames fail at the top band. Convert the remaining regions only
    # when the same white-space checks still admit a loading frame.
    if white_fraction(image[:325]) < .85 or white_fraction(image[955:1150, :630]) < .8:
        return False
    footer = cv2.cvtColor(image[1207:1260, 510:646], cv2.COLOR_BGR2HSV)
    brown = ((footer[:, :, 0] >= 5) & (footer[:, :, 0] <= 30)
             & (footer[:, :, 1] > 80) & (footer[:, :, 2] > 65) & (footer[:, :, 2] < 220))
    if float(brown.mean()) <= .055:
        return False
    mascot = cv2.cvtColor(image[1165:1265, 649:713], cv2.COLOR_BGR2HSV)
    return (float(((mascot[:, :, 1] > 70) & (mascot[:, :, 2] < 245)).mean()) > .10
            and white_fraction(image[440:900, 40:680]) < .7)


def connecting_overlay_visible(image):
    """The white, brown-outlined connecting caption above the career HUD."""
    if image is None or not isinstance(image, np.ndarray) or image.size == 0:
        return False
    canonical = image if image.shape[:2] == (1280, 720) else cv2.resize(image, (720, 1280))
    hsv = cv2.cvtColor(canonical[50:125, 480:705], cv2.COLOR_BGR2HSV)
    white = ((hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 235)).astype(np.uint8)
    _,labels,stats,_ = cv2.connectedComponentsWithStats(white)
    brown = ((hsv[:, :, 0] >= 5) & (hsv[:, :, 0] <= 30) & (hsv[:, :, 1] > 50)
        & (hsv[:, :, 2] > 80) & (hsv[:, :, 2] < 215))
    glyphs=[]
    for index,row in enumerate(stats[1:],1):
        if not (5 <= row[2] <= 40 and 16 <= row[3] <= 42 and 50 <= row[4] <= 700):continue
        glyph=(labels==index).astype(np.uint8)
        ring=(cv2.dilate(glyph,np.ones((3,3),np.uint8))>0)&(glyph==0)
        # Scenery windows can align like white glyphs, with brown walls nearby.
        # The connecting caption has a dark brown outline directly around each
        # white component, rather than brown somewhere inside a broad group box.
        if ring.any() and float(brown[ring].mean())>=.2:glyphs.append(row)
    for anchor in glyphs:
        center = anchor[1] + anchor[3] / 2
        group = [row for row in glyphs if abs(row[1] + row[3] / 2 - center) <= 12]
        if len(group) < 3:
            continue
        x1, x2 = min(row[0] for row in group), max(row[0] + row[2] for row in group)
        y1, y2 = min(row[1] for row in group), max(row[1] + row[3] for row in group)
        if not 65 <= x2 - x1 <= 160:
            continue
        area = hsv[max(0, y1-3):y2+3, max(0, x1-3):x2+3]
        group_brown = (area[:, :, 0] >= 5) & (area[:, :, 0] <= 30) & (area[:, :, 1] > 50) & (area[:, :, 2] > 80)
        if float(group_brown.mean()) > .03:
            return True
    return False


def entry_page_handoff(image, source_path):
    """Only visual page changes before the capture entry input, not OCR failure."""
    from scripts.larc_transition_probe import actionable_surface
    if not isinstance(image, np.ndarray):
        return None
    surface = actionable_surface(image, skip_enabled=True)
    if surface == "main_menu":
        return None
    return {"phase": "capture_entry", "before_consuming_input": True,
            "consuming_input_attempted": False, "input_attempted": False, "surface": surface or "transition",
            "source_path": str(source_path)}


def precheck_page_handoff(check, path, *, navigation_attempted=False):
    changes = {"main_menu", "blocking_screen", "layout", "tab_presence", "race_entry",
               "race_hub", "settings_overlay", "forbidden_intersection"}
    if not changes.intersection(getattr(check, "failures", ())):
        return {}
    return {"page_handoff": {"phase": "execution_precheck", "before_consuming_input": True,
                             "consuming_input_attempted": False,
                             "input_attempted": navigation_attempted, "source_path": str(path)}}


def main_menu_page_handoff(path, *, navigation_attempted=True):
    image = cv2.imread(str(path))
    if image is None:
        return {}
    handoff = entry_page_handoff(image, path)
    if handoff is None:
        return {}
    handoff.update(phase="execution_precheck", input_attempted=navigation_attempted)
    return {"page_handoff": handoff}


def pipeline_page_handoff(report):
    stages = report.get("stages", {})
    capture = stages.get("capture", {})
    execution = stages.get("live_loop", {}).get("execution", {})
    handoff = capture.get("page_handoff") or execution.get("page_handoff")
    if not isinstance(handoff, dict) or handoff.get("before_consuming_input") is not True:
        return None
    if handoff.get("consuming_input_attempted") is not False:
        return None
    if handoff.get("phase") not in {"capture_entry", "execution_precheck"}:
        return None
    if handoff.get("phase") == "capture_entry" and (handoff.get("input_attempted") is not False
            or capture.get("transition_events", []) != []):
        return None
    if any(execution.get(key) for key in ("performed", "tap_performed", "input_may_have_occurred", "input_receipts")):
        return None
    return handoff


def control_signature(image, surface=None):
    """Small opaque control samples; omit the animated character/background."""
    if image is None or not isinstance(image, np.ndarray) or image.size == 0:
        return None
    image = cv2.resize(image, (90, 160), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # White/green UI occupancy is enough to notice a modal/control appearing;
    # RGB sparkle and character animation are not progress evidence.
    mask = (((hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 175)).astype(np.uint8)
            + 2 * ((hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 95)
                   & (hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 100)))
    rows = [mask[0:22], mask[139:158]]
    if surface in {"modal", "event_choice"}:
        rows += [mask[45:136, 3:7], mask[45:136, 83:87]]
    return np.concatenate([row.ravel() for row in rows])


def signature_changed(before, after, *, threshold=.05):
    return (after is not None and (before is None or before.shape != after.shape
            or float(np.mean(before != after)) > threshold))


class ControlReadiness:
    """Confirm ambiguous shells/option panes, while ready home wakes at once."""
    def __init__(self):
        self.value = None
        self.signature = None

    def observe(self, value, image):
        if value is None:
            self.value = self.signature = None
            return None
        kind = value[1] if isinstance(value, tuple) and len(value) > 1 else value
        if kind in {"main_menu", "race_menu", "inheritance", "preview", "result", "skip"}:
            self.value = self.signature = None
            return value
        signature = control_signature(image, kind)
        ready = value == self.value and not signature_changed(self.signature, signature)
        self.value, self.signature = value, signature
        return value if ready else None


def transition_phase(image=None, key=None):
    """Use existing loading detectors, not movement, to select a wait policy."""
    name = str(key or "").lower()
    if name in {"connecting", "connecting_overlay", "connection_overlay"}:
        return "connection"
    if name in {"loading", "comic_loading", "career_comic_loading", "blank_white_loading"}:
        return "loading"
    if isinstance(image, np.ndarray) and image.size:
        if connecting_overlay_visible(image):
            return "connection"
        if career_comic_loading_visible(image):
            return "loading"
        if __package__:
            from core.auto_trainer.safe_advance import SafeAdvancePlanner
            if SafeAdvancePlanner._looks_like_blank_white_loading(image):
                return "loading"
        elif blank_white_loading_visible(image):
            return "loading"
    if name in {"transition", "main_menu_transition", "event_selection_pending"}:
        return "transition"
    return "unknown"


class TransitionBudget:
    """Phase-aware waits with an absolute cap; animation never resets a phase."""
    PHASE_LIMITS = {
        "unknown": (UNKNOWN_TRANSITION_IDLE_SECONDS, 120.),
        "transition": (60., 120.),
        "connection": (60., 120.),
        # A confirmed loading splash can be static. Its stage deadline, not
        # the generic idle timer, bounds it. These are wait budgets, not a
        # claim that a loading image proves the game is still progressing.
        "loading": (180., 180.),
    }

    def __init__(self, *, clock=time.monotonic, idle_seconds=UNKNOWN_TRANSITION_IDLE_SECONDS,
                 total_seconds=None, unknown_total_seconds=None):
        self.clock, self.idle_seconds = clock, idle_seconds
        self.total_seconds = 240. if total_seconds is None else total_seconds
        default_unknown_total = UNKNOWN_TRANSITION_TOTAL_SECONDS if total_seconds is None else total_seconds
        self.unknown_total_seconds = (default_unknown_total if unknown_total_seconds is None
                                      else unknown_total_seconds)
        self.reset()

    def reset(self):
        self.started = self.last_progress = None
        self.key = self.signature = None
        self.progress_version = 0
        self.phase = None
        self.phase_started = None
        self.progress_token = None
        self._gentle_consecutive_frames = 0
        self._gentle_window_started = None

    def _now(self):
        control = current()
        return self.clock() - (control.paused_seconds if control else 0.)

    def progress(self):
        now = self._now()
        if self.started is None:
            self.started = now
        self.last_progress = now
        self.progress_version += 1

    def observe(self, *, image=None, key=None, phase=None, progressed=False, progress_token=None):
        phase = phase or transition_phase(image, key)
        if phase not in self.PHASE_LIMITS:
            phase = "unknown"
        phase_changed = phase != self.phase
        if phase_changed:
            self.phase, self.phase_started = phase, self._now()
        signature = control_signature(image, key)
        changed = ((key is not None and key != self.key)
                   or (phase == "unknown" and signature_changed(self.signature, signature)))
        semantic_progress = (progressed is True or progress_token is not None
            and progress_token != self.progress_token)
        changed = changed or phase_changed or semantic_progress
        if self.started is None or changed:
            self.progress()
        if progress_token is not None:
            self.progress_token = progress_token
        if key is not None:
            self.key = key
        if signature is not None and (changed or self.signature is None):
            self.signature = signature
        return changed

    def _limits(self):
        idle, phase_total = self.PHASE_LIMITS.get(self.phase, self.PHASE_LIMITS["unknown"])
        if self.phase in {None, "unknown"}:
            return self.idle_seconds, phase_total, self.unknown_total_seconds
        return idle, phase_total, self.total_seconds

    @property
    def expired(self):
        if self.started is None:
            self.progress()
        now = self._now()
        idle, phase_total, total = self._limits()
        phase_started = self.started if self.phase_started is None else self.phase_started
        return (now - self.last_progress >= idle
                or now - phase_started >= phase_total
                or now - self.started >= total)

    def snapshot(self):
        """Read diagnostic timing without starting or advancing the budget."""
        idle, phase_total, total = self._limits()
        started = self.started is not None
        idle_elapsed = phase_elapsed = total_elapsed = 0.
        expired_by = []
        if started:
            now = self._now()
            phase_started = self.started if self.phase_started is None else self.phase_started
            idle_elapsed = max(0., now - self.last_progress)
            phase_elapsed = max(0., now - phase_started)
            total_elapsed = max(0., now - self.started)
            expired_by = [name for name, elapsed, limit in (
                ("idle", idle_elapsed, idle), ("phase", phase_elapsed, phase_total),
                ("total", total_elapsed, total)) if elapsed >= limit]
        return {"phase": self.phase, "last_key": self.key, "started": started,
            "idle_elapsed_seconds": idle_elapsed, "phase_elapsed_seconds": phase_elapsed,
            "total_elapsed_seconds": total_elapsed, "idle_limit_seconds": idle,
            "phase_limit_seconds": phase_total, "total_limit_seconds": total,
            "expired_by": expired_by, "progress_version": self.progress_version}


def technical_wait_stop(budget, *, reason, owner, surface=None,
                        run_session_id=None, turn_index=None):
    """Describe one actual wait stop; sampling alone never creates a record."""
    return {"schema_version": "uma.technical_wait_stop.v1", "owner": owner,
        "reason": reason, "surface": surface, "run_session_id": run_session_id,
        "turn_index": turn_index, "wait": budget.snapshot(),
        "recovery": {"status": "not_attempted"}}


def transient_observation(result, *, reason=None, allow_home_guess=False):
    """A fresh read may resolve this failure; any actual/uncertain input stops."""
    if not isinstance(result, dict) or any(result.get(key) for key in (
            "tap_performed", "input_may_have_occurred", "input_receipts")):
        return False
    if result.get("tap_outcome") in {"failed", "unknown", "pending"}:
        return False
    decision = result.get("decision", {})
    frame = result.get("frame_safety", {})
    if decision.get("action") == "wait" and not decision.get("tap"):
        # Read-only loading waits spend nothing, even when a comic mentions
        # currency. The planner still owns identification of the loading page.
        return True
    if frame.get("dangerous") or decision.get("evidence", {}).get("screen_text", {}).get("dangerous"):
        return False
    if result.get("performed") and decision.get("action") != "wait":
        return False
    reason = reason or result.get("reason") or decision.get("reason")
    if reason == "action_safety_gate":
        return result.get("safety_verdict", {}).get("reason") == "frame_text_safety_abstained"
    if reason in {"resource_prompt", "paid_resource_confirmation", "connection_conflict",
                  "frame_safety_sha_mismatch", "race_executor_error"}:
        return False
    if reason == "route_state_out_of_scope":
        return (decision.get("action") == "wait" or decision.get("reason") in {
            "common_career_page_unrecognized", "bottom_skip_double_enabled"})
    if reason == "global_home":
        return allow_home_guess
    return (reason in {"common_career_page_unrecognized", "bottom_skip_state_unconfirmed",
                       "event_choice:no_option_points", "event_choice:identity_mismatch",
                       "event_choice:options_mismatch"}
            or reason in {f"unsupported_screen:{screen}" for screen in (
                "event_choice", "portrait_ui", "game_home", "training_menu", "unknown", "bottom_action")}
            or reason == "unsupported_screen:race_participant_list"
            or (reason == "modal_confirm:unsupported" and
                "".join(str(frame.get("visible_text", "")).split()) in {"取消确定", "确定取消"}))


# ---------------------------------------------------------------------------
# Unified Visual Transition / Wipe / Fade Classifiers & Gentle Wait Policy
# ---------------------------------------------------------------------------

_GENTLE_SPEND_OR_CONFLICT_TERMS = (
    "购买",
    "宝石",
    "充值",
    "有偿",
    "无偿",
    "消耗",
    "不足",
    "闹钟",
    "其他设备",
    "别的设备",
    "其他终端",
    "别处登录",
    "连接已断开",
    "返回标题",
)


def blank_white_loading_visible(image):
    """Pure/near-pure white loading or scene-flash frame without UI buttons."""
    if not isinstance(image, np.ndarray) or image.size == 0:
        return False
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    white = np.mean((hsv[:, :, 1] < 45) & (hsv[:, :, 2] > 210))
    saturated = np.mean((hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 100))
    if white > 0.985 and saturated < 0.01:
        return True
    height, width = image.shape[:2]
    if width >= height:
        return False
    body = hsv[: int(height * 0.88), :]
    if body.size == 0:
        return False
    body_white = np.mean((body[:, :, 1] < 45) & (body[:, :, 2] > 210))
    body_saturated = np.mean((body[:, :, 1] > 80) & (body[:, :, 2] > 100))
    return bool(body_white > 0.995 and body_saturated < 0.002)


def orange_wipe_visible(image):
    """Shared portrait orange full-screen wipe mask used across career & race routes."""
    if not isinstance(image, np.ndarray) or image.size == 0:
        return False
    height, width = image.shape[:2]
    if width > height:
        return False
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    scan = hsv[int(height * 0.25) : int(height * 0.92), :]
    if scan.size == 0:
        return False
    orange = np.mean(
        (scan[:, :, 0] >= 5)
        & (scan[:, :, 0] <= 25)
        & (scan[:, :, 1] > 30)
        & (scan[:, :, 2] > 150)
    )
    white = np.mean((scan[:, :, 1] < 60) & (scan[:, :, 2] > 175))
    dark = np.mean(scan[:, :, 2] < 100)
    return bool(orange > 0.80 and white < 0.05 and dark < 0.02)


def pastel_diagonal_wipe_visible(image):
    """Pastel blue/pink diagonal scene wipe."""
    if not isinstance(image, np.ndarray) or image.size == 0:
        return False
    height, width = image.shape[:2]
    if width > height:
        return False
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    scan = hsv[int(height * 0.25) : int(height * 0.92), :]
    if scan.size == 0:
        return False
    dark = np.mean(scan[:, :, 2] < 100)
    pastel_blue = np.mean(
        (scan[:, :, 0] >= 95)
        & (scan[:, :, 0] <= 115)
        & (scan[:, :, 1] > 25)
        & (scan[:, :, 1] < 95)
        & (scan[:, :, 2] > 170)
    )
    pastel_pink = np.mean(
        ((scan[:, :, 0] <= 5) | (scan[:, :, 0] >= 170))
        & (scan[:, :, 1] > 20)
        & (scan[:, :, 1] < 95)
        & (scan[:, :, 2] > 175)
    )
    bright = np.mean(scan[:, :, 2] > 160)
    return bool(pastel_blue > 0.45 and pastel_pink > 0.20 and bright > 0.95 and dark < 0.02)


def blue_white_transition_visible(image):
    """Striped blue/white race transition curtain."""
    if not isinstance(image, np.ndarray) or image.size == 0:
        return False
    height, width = image.shape[:2]
    if width > height:
        return False
    hsv = cv2.cvtColor(cv2.resize(image, (180, 320)), cv2.COLOR_BGR2HSV)
    white = (hsv[:, :, 1] < 45) & (hsv[:, :, 2] > 210)
    blue = (
        (hsv[:, :, 0] >= 85)
        & (hsv[:, :, 0] <= 115)
        & (hsv[:, :, 1] >= 45)
        & (hsv[:, :, 1] < 110)
        & (hsv[:, :, 2] > 210)
    )
    return bool(
        np.mean(white | blue) > 0.995
        and np.mean(blue) > 0.20
        and np.mean(white) > 0.05
        and np.mean(hsv[:, :, 2] < 150) < 0.001
    )


def gentle_fade_or_blur_kind(image):
    """Identify transient mid-animation fade or motion-blur frames with zero controls."""
    if not isinstance(image, np.ndarray) or image.size == 0:
        return None
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2]
    s = hsv[:, :, 1]
    saturated = float(np.mean((s > 80) & (v > 95)))
    if float(np.mean(v < 18)) > 0.985 and saturated < 0.01:
        return "pure_black_fade"
    if blank_white_loading_visible(image) or (
        float(np.mean((s < 55) & (v > 200))) > 0.94 and saturated < 0.02
    ):
        return "white_flash"
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edge_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if float(np.mean(v < 55)) > 0.92 and saturated < 0.03 and edge_variance < 28.0:
        return "dark_fade"
    if edge_variance < 12.0:
        return "blurred_afterimage"
    return None


def _has_blocking_safety_signal(*, visible_text=None, screen_text=None, frame_safety=None):
    if isinstance(frame_safety, dict):
        if frame_safety.get("dangerous") or frame_safety.get("resource_terms"):
            return True
        if visible_text is None and isinstance(frame_safety.get("visible_text"), str):
            visible_text = frame_safety["visible_text"]
    elif frame_safety is not None:
        if getattr(frame_safety, "dangerous", False) or getattr(frame_safety, "resource_terms", ()):
            return True
        if visible_text is None and isinstance(getattr(frame_safety, "visible_text", None), str):
            visible_text = frame_safety.visible_text
    if isinstance(screen_text, dict):
        if screen_text.get("dangerous") or screen_text.get("resource_terms"):
            return True
        if visible_text is None and isinstance(screen_text.get("normalized_text"), str):
            visible_text = screen_text["normalized_text"]
    compact = "".join(str(visible_text or "").split())
    if compact and any(term in compact for term in _GENTLE_SPEND_OR_CONFLICT_TERMS):
        return True
    return False


def classify_gentle_transition_frame(
    image,
    *,
    perception=None,
    visible_text=None,
    screen_text=None,
    frame_safety=None,
):
    """Classify any transient wipe/loading/fade/blur frame when safety signals are clean."""
    if not isinstance(image, np.ndarray) or image.size == 0:
        return None
    if _has_blocking_safety_signal(
        visible_text=visible_text,
        screen_text=screen_text,
        frame_safety=frame_safety,
    ):
        return None
    blur_kind = gentle_fade_or_blur_kind(image)
    if isinstance(perception, dict) and perception.get("buttons") and blur_kind is None:
        return None
    if connecting_overlay_visible(image):
        return "connecting_overlay"
    if career_comic_loading_visible(image):
        return "career_comic_loading"
    if orange_wipe_visible(image):
        return "orange_wipe"
    if pastel_diagonal_wipe_visible(image):
        return "pastel_diagonal_wipe"
    if blue_white_transition_visible(image):
        return "blue_white_transition"
    if blank_white_loading_visible(image):
        return "blank_white_loading"
    return blur_kind


def _eligible_unrecognized_stop_reason(reason):
    if not isinstance(reason, str):
        return False
    return (
        reason in {
            "common_career_page_unrecognized",
            "race_inspection:screen_unverified",
            "race_list:no_primary_entry",
        }
        or reason.startswith("unsupported_screen:")
    )


GENTLE_MAX_CONSECUTIVE_FRAMES = 4
GENTLE_MAX_WINDOW_SECONDS = 6.0

_GENTLE_READY_STOP_REASONS = frozenset(
    {
        "ready_main_menu",
        "ready_training_menu",
        "ready_larc_ss_preview",
        "race_day_main_menu",
        "global_home",
        "race_inspection_complete",
    }
)


def is_gentle_reset_decision(decision):
    if not isinstance(decision, dict):
        return False
    action = decision.get("action")
    if action in {"tap", "swipe", "handoff"} or decision.get("handoff") is True:
        return True
    return action == "stop" and str(decision.get("reason") or "") in _GENTLE_READY_STOP_REASONS


class GentleTransitionWaitPolicy:
    """Bounded read-only wait policy for mid-animation frames; never masks dangerous modals."""

    def __init__(
        self,
        budget=None,
        *,
        reset_on_actionable=True,
        max_consecutive_frames=GENTLE_MAX_CONSECUTIVE_FRAMES,
        max_window_seconds=GENTLE_MAX_WINDOW_SECONDS,
    ):
        self.budget = budget if budget is not None else TransitionBudget()
        self.reset_on_actionable = reset_on_actionable
        self.max_consecutive_frames = int(max_consecutive_frames)
        self.max_window_seconds = float(max_window_seconds)

    def evaluate(
        self,
        decision,
        image,
        *,
        frame_safety=None,
        visible_text=None,
        owner="safe_advance",
    ):
        if not isinstance(decision, dict):
            return decision
        action = decision.get("action")
        reason = str(decision.get("reason") or "")
        evidence = dict(decision.get("evidence") or {})
        screen_text = evidence.get("screen_text")

        if _has_blocking_safety_signal(
            visible_text=visible_text,
            screen_text=screen_text,
            frame_safety=frame_safety,
        ):
            return decision

        if action in {"tap", "swipe", "handoff"} or decision.get("handoff") is True:
            if self.reset_on_actionable:
                self.budget.reset()
            return decision

        if action == "wait" and not decision.get("tap"):
            key = str(decision.get("route_state") or reason or "transition")
            self.budget.observe(image=image, key=key)
            if self.budget.expired:
                return {
                    "action": "stop",
                    "reason": "no_progress_timeout",
                    "route_state": decision.get("route_state", "loading"),
                    "evidence": {
                        **evidence,
                        "gentle_transition": {
                            "kind": key,
                            "expired": True,
                            "original_stop_reason": reason,
                            "wait_budget": self.budget.snapshot(),
                        },
                    },
                    "technical_wait_stop": technical_wait_stop(
                        self.budget,
                        reason="no_progress_timeout",
                        owner=owner,
                        surface=key,
                    ),
                }
            return decision

        if action != "stop":
            return decision

        if reason in _GENTLE_READY_STOP_REASONS:
            if self.reset_on_actionable:
                self.budget.reset()
            return decision

        if not _eligible_unrecognized_stop_reason(reason):
            return decision

        kind = classify_gentle_transition_frame(
            image,
            perception=evidence.get("perception"),
            visible_text=visible_text,
            screen_text=screen_text,
            frame_safety=frame_safety,
        )
        if kind is None:
            return decision

        now_fn = getattr(self.budget, "_now", time.monotonic)
        now = float(now_fn())
        window_started = getattr(self.budget, "_gentle_window_started", None)
        if window_started is None:
            window_started = now
            self.budget._gentle_window_started = window_started
        consecutive = int(getattr(self.budget, "_gentle_consecutive_frames", 0)) + 1
        self.budget._gentle_consecutive_frames = consecutive
        window_elapsed = max(0.0, now - float(window_started))

        phase = (
            "connection"
            if kind == "connecting_overlay"
            else "loading"
            if "loading" in kind
            else "transition"
        )
        self.budget.observe(image=image, key=f"gentle_{kind}", phase=phase)
        gentle_expired_by = []
        if consecutive > self.max_consecutive_frames:
            gentle_expired_by.append("max_consecutive_frames")
        if window_elapsed >= self.max_window_seconds:
            gentle_expired_by.append("max_window_seconds")
        if self.budget.expired:
            gentle_expired_by.append("transition_budget")
        if gentle_expired_by:
            return {
                "action": "stop",
                "reason": "no_progress_timeout",
                "route_state": decision.get("route_state", "loading"),
                "evidence": {
                    **evidence,
                    "gentle_transition": {
                        "kind": kind,
                        "expired": True,
                        "expired_by": gentle_expired_by,
                        "consecutive_frames": consecutive,
                        "max_consecutive_frames": self.max_consecutive_frames,
                        "window_elapsed_seconds": round(window_elapsed, 4),
                        "max_window_seconds": self.max_window_seconds,
                        "original_stop_reason": reason,
                        "wait_budget": self.budget.snapshot(),
                    },
                },
                "technical_wait_stop": technical_wait_stop(
                    self.budget,
                    reason="no_progress_timeout",
                    owner=owner,
                    surface=kind,
                ),
            }
        return {
            "action": "wait",
            "reason": "transition",
            "route_state": "transition",
            "evidence": {
                **evidence,
                "gentle_transition": {
                    "kind": kind,
                    "consecutive_frames": consecutive,
                    "max_consecutive_frames": self.max_consecutive_frames,
                    "window_elapsed_seconds": round(window_elapsed, 4),
                    "max_window_seconds": self.max_window_seconds,
                    "original_stop_reason": reason,
                    "wait_budget": self.budget.snapshot(),
                },
            },
        }


_DEFAULT_GENTLE_BUDGET = TransitionBudget()


def evaluate_gentle_transition_wait(
    decision,
    image,
    *,
    budget=None,
    frame_safety=None,
    visible_text=None,
    owner="safe_advance",
    max_consecutive_frames=GENTLE_MAX_CONSECUTIVE_FRAMES,
    max_window_seconds=GENTLE_MAX_WINDOW_SECONDS,
):
    """Evaluate one planner decision under GentleTransitionWaitPolicy."""
    effective_budget = budget if budget is not None else _DEFAULT_GENTLE_BUDGET
    policy = GentleTransitionWaitPolicy(
        budget=effective_budget,
        reset_on_actionable=True,
        max_consecutive_frames=max_consecutive_frames,
        max_window_seconds=max_window_seconds,
    )
    return policy.evaluate(
        decision,
        image,
        frame_safety=frame_safety,
        visible_text=visible_text,
        owner=owner,
    )
