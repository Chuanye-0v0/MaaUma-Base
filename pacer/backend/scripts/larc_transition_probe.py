"""Cheap, read-only career transition polling. It never chooses or taps an action."""
from __future__ import annotations

import time
import cv2
import numpy as np
from core.auto_trainer.transition_wait import ControlReadiness, TransitionBudget, connecting_overlay_visible

from core.auto_trainer.event_options import detect_event_option_layout, help_carousel_visible, event_option_rows_ready
from core.auto_trainer.larc_pages import larc_race_day_controls
from core.auto_trainer.safe_advance import SafeAdvanceExecutor, SafeAdvancePlanner
from core.auto_trainer.speed_toggle import SpeedTogglePlanner
from core.execution_frames import wait_for_frame


def normalized_frame(image):
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[1::-1] not in {(720, 1280), (1440, 2560)}:
        return None
    return image if image.shape[1::-1] == (720, 1280) else cv2.resize(image, (720, 1280))


def event_title_crop(image, *, include_pink=False):
    """A candidate title band, not proof of an event identity or reward."""
    image = normalized_frame(image)
    if image is None:
        return None
    band = image[195:310, 115:625]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    cyan = (hsv[:, :, 0] >= 85) & (hsv[:, :, 0] <= 115) & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 110)
    if include_pink:
        pink=((hsv[:,:,0]>=140)|(hsv[:,:,0]<=8))&(hsv[:,:,1]>90)&(hsv[:,:,2]>110)
        cyan=cyan|pink
    white = (hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 175)
    if float(cyan.mean()) < .22 or float(white.mean()) < .025:
        return None
    return image[185:325, 8:660].copy()


def event_options_ready(image):
    """Real option rows share a column and regular spacing, unlike scenery."""
    return event_option_rows_ready(image)


def actionable_surface(image, *, skip_enabled=False):
    image = normalized_frame(image)
    if image is None:
        return None
    from core.auto_trainer.transition_wait import career_comic_loading_visible
    if career_comic_loading_visible(image):
        return None
    if connecting_overlay_visible(image):
        return None
    if help_carousel_visible(image):
        return "modal"  # Existing explanation owner closes it, never event selection.
    if SafeAdvancePlanner._career_opening_movie_skip_button(image) is not None:
        return "skip" # UI-only wakeup; the opening owner still gates the tap.
    from core.auto_trainer.larc_control_wait import modal_content_pending, modal_controls
    if modal_content_pending(image):
        return None
    # This is only a wakeup signal. Existing handlers still verify and execute.
    if SafeAdvancePlanner._looks_like_confirm_modal_panel(image) or modal_controls(image):
        return "modal"
    # Readiness hint only; the page owner still requires the inheritance title.
    if SafeAdvancePlanner._inheritance_factor_confirm_button(image,named_scene=True) is not None:
        return "inheritance"  # Wake the existing handler, not a direct tap.
    # Diagonal goal-completion bands and the white chibi card can imitate
    # regularly spaced event rows. Wake the normal result handler instead.
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    green = ((hsv[:, :, 0] >= 38) & (hsv[:, :, 0] <= 92)
             & (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 80))
    white = (hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 170)
    if (float(white[360:790, 72:648].mean()) > .70
            and float(green[128:384].mean()) > .18
            and float(green[832:1126].mean()) > .14):
        return "modal"
    if event_options_ready(image):
        return "event_choice"
    if SafeAdvanceExecutor._career_home_controls_ready(image):
        return "main_menu"
    if larc_race_day_controls(image):
        return "race_menu"
    level = SpeedTogglePlanner._bottom_skip_level(image)
    if level in {"off", "single"}:
        return "skip_control"
    if (not skip_enabled and level != "double"
            and SafeAdvancePlanner._looks_like_dialogue_continue_arrow(image)):
        return "dialogue"
    return None


class CareerTransitionProbe:
    def __init__(self, adb, *, journal=None, clock=time.monotonic, sleep=time.sleep,cut_sampling=False):
        self.adb = adb
        self.journal = journal
        self.cut_sampling=cut_sampling
        self.clock = clock
        self.sleep = sleep
        self._selected_options = None
        self._readiness = ControlReadiness()
        self._progress = TransitionBudget(clock=clock)

    @staticmethod
    def _option_text_masks(image):
        image = normalized_frame(image)
        if image is None:
            return None
        points = detect_event_option_layout(image).get("ordered_points", [])
        if not points:
            return None
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        masks = []
        for _, y in points:
            crop = hsv[max(0, y-23):y+23, 50:670]
            masks.append(((crop[:, :, 0] < 35) & (crop[:, :, 1] > 65)
                          & (crop[:, :, 2] > 35) & (crop[:, :, 2] < 210)))
        return masks

    def mark_event_selected(self, image):
        self._selected_options = self._option_text_masks(image)
        self._readiness = ControlReadiness()

    def _surface(self, image, skip_enabled):
        surface = actionable_surface(image, skip_enabled=skip_enabled)
        if self._selected_options is not None:
            current = self._option_text_masks(image)
            if surface == "event_choice" and current is not None:
                unchanged = (len(current) == len(self._selected_options)
                    and all(a.shape == b.shape and float(np.mean(a != b)) < .015
                            for a, b in zip(current, self._selected_options)))
                if unchanged:
                    return None  # Input has happened; wait for the option pane to leave.
                self._selected_options = None
            elif current is None or surface in {"main_menu", "race_menu", "modal"}:
                self._selected_options = None
        if surface is None and self._selected_options is None:
            frame = normalized_frame(image)
            if frame is not None and not connecting_overlay_visible(frame):
                from core.auto_trainer.race_route import RaceRoutePlanner
                preview = RaceRoutePlanner._race_preview_action_points(frame)
                if (preview is not None and preview.get("view_result") is not None
                        and not preview.get("view_result_locked")):
                    # A read-only wakeup, not a route state or permission to tap.
                    # Keep pending event selection and existing page owners first.
                    return self._readiness.observe("race_preview_wakeup", frame)
        return self._readiness.observe(surface, image)

    def observe(self, *, action_turn, skip_enabled, timeout=2.0):
        version = self._progress.progress_version
        phase_frame = None
        def observe(image, surface):
            nonlocal phase_frame
            self._progress.observe(image=image, key=surface)
            phase_frame = image
            if self.journal is not None and (self.cut_sampling or surface != "main_menu"):
                crop = event_title_crop(image,include_pink=self.cut_sampling)
                if crop is not None:
                    self.journal.offer(crop, action_turn)
        observation = wait_for_frame(self.adb,
            lambda image: self._surface(image, skip_enabled),
            timeout=timeout, interval=.15, max_polls=20, observe=observe,
            clock=self.clock, sleep=self.sleep)
        preview_wakeup = observation.value == "race_preview_wakeup"
        result = {"surface": None if preview_wakeup else observation.value,
                  "polls": observation.polls, "seconds": observation.seconds,
                  "progressed": self._progress.progress_version != version,
                  "skip_level": None,
                  "wait_phase": self._progress.phase
                    if observation.frame is not None and phase_frame is observation.frame else None}
        # Diagnostic facts only, from the frame already polled above. An absent
        # or occluded control is unknown, not proof that fast-forward is off.
        frame = normalized_frame(observation.frame)
        if frame is not None:
            try:
                level = SpeedTogglePlanner._bottom_skip_level(frame)
            except (cv2.error, ValueError, TypeError):
                level = None  # Diagnostic reading must not become a new stop.
            if level in {"off", "single", "double"}:
                result["skip_level"] = level
        if observation.frame is not None:
            result["progress_frame"] = observation.frame
        if self._selected_options is not None:
            result["event_selection_pending"] = True
        if preview_wakeup:
            result["race_preview_wakeup"] = "free_view_result_control_candidate"
        if observation.error:
            result["fallback"] = observation.error
            if self.cut_sampling:result['cut_sampling_error']=observation.error
        if observation.value:
            # The owner still classifies this frame before any input. Handoff
            # is synchronous and single-use, just as for the ordinary home.
            result["frame"] = observation.frame
        return result

    def close(self):
        self.adb.close()
