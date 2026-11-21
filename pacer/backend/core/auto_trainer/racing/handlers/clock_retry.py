"""Clock retry candidate and prompt handlers for RaceRoute."""
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
from .._compat import (
    FrameSafetyAnalyzer,
    LocalRapidOCRScreenTextReader,
    Perception,
    RaceRouteExecutor,
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
from ..models import (
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


class ClockRetryHandlerMixin:
    """Lost-race clock retry detection and candidate evaluation."""

    @staticmethod
    def _result_retry_page_candidate(img: np.ndarray, visible_text: str | None) -> dict[str, Any] | None:
        """Observe a requested retry choice without authorizing its controls.

        Reuse the existing paired controls/course/standings identity. Unlike
        strict entry evidence, this page remains a candidate when its outcome
        cannot be read. A first place read only rules retry out; other reads
        must still pass the existing entry reader on a later observation.
        """
        from core.auto_trainer.race_clock_retry import (
            _result_controls, _ordinary_result_blocked, _header_text_rank,
            _line, _compact, _confident, RESULT_HEADER_ROI, RESULT_RANK_ROI,
        )
        if img.shape[0] <= img.shape[1] or _ordinary_result_blocked(visible_text):
            return None
        image = img if img.shape[:2] == (1280, 720) else cv2.resize(img, (720, 1280))
        controls = _result_controls(image)
        if controls is None:
            return None
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        for x1, y1, x2, y2 in ((15, 505, 705, 595), (25, 620, 695, 1070)):
            band = hsv[y1:y2, x1:x2]
            if float(np.mean((band[:, :, 1] < 65) & (band[:, :, 2] > 185))) < .45:
                return None
        retry_box, enabled, next_box = controls
        ranks = _header_text_rank(visible_text)
        rank = next(iter(ranks)) if len(ranks) == 1 else None
        if enabled:
            for roi in (RESULT_HEADER_ROI, RESULT_RANK_ROI):
                label, confidence = _line(image, roi)
                match = re.fullmatch(r'(?:第)?([1-9][0-9]*)(?:名)?', _compact(label))
                if match and _confident(confidence):
                    observed = int(match[1])
                    rank = observed if not ranks or ranks == {observed} else None
                    break
        return {"kind": "current_result_retry_candidate", "source": "paired_result_page_without_required_outcome",
                "rank": rank, "retry_enabled": enabled, "retry_box": retry_box, "next_box": next_box,
                "screen_size": [img.shape[1], img.shape[0]], "authorizes_input": False}
