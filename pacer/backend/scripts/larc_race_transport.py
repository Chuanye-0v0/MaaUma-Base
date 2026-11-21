"""Race-scoped native frames and visual settling; no race action decisions."""
import time
import cv2

from core.auto_trainer.race_route import RaceRoutePlanner
from core.auto_trainer.safe_advance import SafeAdvancePlanner
from core.execution_frames import ExecutionFrames, wait_for_frame
from core.auto_trainer.larc_control_wait import modal_content_pending
from core.auto_trainer.transition_wait import ControlReadiness


def race_controls(image):
    """Cheap wakeup signals only; the existing planner still authorizes taps."""
    height, width = image.shape[:2]
    shape = (width, height)
    if width > height:
        point = RaceRoutePlanner._right_bottom_race_skip_to_end_point(image)
        return (shape, "skip", point) if point else None
    if modal_content_pending(image):
        return None
    preview = RaceRoutePlanner._race_preview_action_points(image)
    if preview:
        return (shape, "preview", preview["view_result_locked"])
    point = RaceRoutePlanner._race_result_next_button_from_image(image)
    if point:
        return (shape, "result", point)
    if SafeAdvancePlanner._looks_like_confirm_modal_panel(image):
        return (shape, "modal")
    from scripts.larc_transition_probe import actionable_surface
    surface = actionable_surface(image, skip_enabled=True)
    if surface in {"main_menu", "race_menu", "event_choice", "inheritance", "modal"}:
        return (shape, surface)
    return None


class RaceNativeTransport(ExecutionFrames):
    def __init__(self, adb, *, clock=time.monotonic, sleep=time.sleep, controls=race_controls):
        super().__init__(adb)
        self.clock, self.sleep, self.controls = clock, sleep, controls
        self.last_wait = None

    def tap(self, x, y, *, scaled=False):
        result = super().tap(x, y, scaled=scaled)
        if result is True:
            # Input succeeded even if the optional native observation fails.
            if getattr(self.adb,'supports_in_memory_capture',False) is True:
                self._settle()
            else:
                # Portable capture may fall back to ordinary ADB. Keep its
                # established settling delay, but reuse the route connection.
                self.sleep(1.2)
                self.last_wait={'reason':'adb_fixed_settle','seconds':1.2,'polls':0}
        return result

    def _settle(self):
        started = self.clock()
        deadline = started + 1.2
        disappeared = False
        try:
            previous = self.controls(self.last_frame) if self.last_frame is not None else None
            readiness = ControlReadiness()
            def ready(image):
                nonlocal disappeared
                current = self.controls(image)
                if current is None:
                    disappeared = True
                    readiness.observe(None, image)
                    return False
                if current == previous and not disappeared:
                    return False
                # Tapping view_race_result dims/locks the left button while the
                # preview screen starts fading; wait for exit instead of
                # waking on the newly locked button.
                if (not disappeared
                        and isinstance(previous, tuple) and isinstance(current, tuple)
                        and len(previous) >= 3 and len(current) >= 3
                        and previous[1] == "preview" and previous[2] is False
                        and current[1] == "preview" and current[2] is True):
                    return False
                return readiness.observe(current, image) is not None
            result = wait_for_frame(self.adb, ready, timeout=1.2, max_polls=12,
                initial_delay=True, clock=self.clock, sleep=self.sleep)
            self.last_wait = {"reason": "controls_ready" if result.value else
                "native_fallback" if result.error else "bounded_wait",
                "polls": result.polls, "seconds": result.seconds}
            if result.value:
                self.pending_frame = result.frame
            elif result.error:
                self.last_wait["error"] = result.error
                self.sleep(max(0., deadline - self.clock()))
        except (OSError, RuntimeError, AttributeError, cv2.error) as exc:
            # Let NativeCaptureADB.screenshot use its existing ADB fallback.
            self.last_wait = {"reason": "native_fallback", "error": type(exc).__name__,
                              "polls": 0, "seconds": self.clock() - started}
            self.sleep(max(0., deadline - self.clock()))

    def wait_transition(self):
        """Poll unreadable race transitions without more OCR or any input."""
        readiness = ControlReadiness()
        result = wait_for_frame(self.adb,
            lambda image: readiness.observe(self.controls(image), image),
            timeout=2., interval=.1, clock=self.clock, sleep=self.sleep)
        if result.frame is not None:
            self.last_frame = result.frame
        if result.value:
            self.pending_frame = result.frame
        return {"controls": result.value, "polls": result.polls,
                "seconds": result.seconds, "error": result.error}


def race_timing_summary(report):
    """Small text-only breakdown for the existing one-career review export."""
    rows=[]
    for step in report.get('step_reports',[]):
        result=step.get('result') or {};timing=result.get('timing')
        if not isinstance(timing,dict):continue
        rows.append({'step':step.get('step'),'reason':result.get('reason'),**timing,
            'settle':result.get('native_settle'),'transition_poll':result.get('transition_poll')})
    return {'timed_steps':len(rows),'steps':rows}
