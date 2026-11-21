"""Dynamic facade-symbol lookup so unittest.mock.patch on safe_advance works seamlessly."""
from __future__ import annotations

import importlib
import sys
from typing import Any

from core.auto_trainer.career_goal_perception import perceive_goal_page as _orig_perceive_goal_page
from core.auto_trainer.career_result_layouts import rating_continue_point as _orig_rating_continue_point
from core.auto_trainer.career_start_safety import TesseractTsvTokenReader as _OrigTesseractTsvTokenReader
from core.auto_trainer.event_options import detect_event_option_layout as _orig_detect_event_option_layout
from core.auto_trainer.frame_safety import FrameSafetyAnalyzer as _OrigFrameSafetyAnalyzer
from core.auto_trainer.larc_pages import is_larc_race_day as _orig_is_larc_race_day
from core.auto_trainer.speed_toggle import SpeedTogglePlanner as _OrigSpeedTogglePlanner
from core.main_menu_state import analyze_main_menu_state as _orig_analyze_main_menu_state
from core.perception import Perception as _OrigPerception
from core.screen_text import (
    analyze_screen_text as _orig_analyze_screen_text,
    fuse_perception_with_text as _orig_fuse_perception_with_text,
)
from core.skill_acquisition_parser import inspect_skill_acquisition_page as _orig_inspect_skill_acquisition_page
from core.training_menu_precheck import TrainingMenuPrechecker as _OrigTrainingMenuPrechecker


def sa_attr(name: str, default: Any) -> Any:
    mod = sys.modules.get("core.auto_trainer.safe_advance")
    if mod is not None and name in mod.__dict__:
        return mod.__dict__[name]
    return default


class _FacadeSymbol:
    def __init__(self, mod_name: str, attr: str, default: Any) -> None:
        self._mod_name = mod_name
        self._attr = attr
        self._default = default

    def _target(self) -> Any:
        mod = sys.modules.get(self._mod_name)
        if mod is not None and self._attr in mod.__dict__:
            return mod.__dict__[self._attr]
        return self._default

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._target()(*args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._target(), item)


class _LazySymbol:
    def __init__(self, facade_mod: str, attr: str, fallback_mod: str) -> None:
        self._facade_mod = facade_mod
        self._attr = attr
        self._fallback_mod = fallback_mod

    def _target(self) -> Any:
        mod = sys.modules.get(self._facade_mod)
        if mod is not None and self._attr in mod.__dict__:
            return mod.__dict__[self._attr]
        fb = sys.modules.get(self._fallback_mod)
        if fb is None:
            fb = importlib.import_module(self._fallback_mod)
        return getattr(fb, self._attr)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._target()(*args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._target(), item)


SafeAdvancePlanner: Any = _LazySymbol(
    "core.auto_trainer.safe_advance", "SafeAdvancePlanner", "core.auto_trainer.advance.planner"
)
SafeAdvanceExecutor: Any = _LazySymbol(
    "core.auto_trainer.safe_advance", "SafeAdvanceExecutor", "core.auto_trainer.advance.executor"
)
RaceRoutePlanner: Any = _LazySymbol(
    "core.auto_trainer.race_route", "RaceRoutePlanner", "core.auto_trainer.racing.planner"
)
FrameSafetyAnalyzer: Any = _FacadeSymbol(
    "core.auto_trainer.safe_advance", "FrameSafetyAnalyzer", _OrigFrameSafetyAnalyzer
)
SpeedTogglePlanner: Any = _FacadeSymbol(
    "core.auto_trainer.safe_advance", "SpeedTogglePlanner", _OrigSpeedTogglePlanner
)
TesseractTsvTokenReader: Any = _FacadeSymbol(
    "core.auto_trainer.safe_advance", "TesseractTsvTokenReader", _OrigTesseractTsvTokenReader
)
TrainingMenuPrechecker: Any = _FacadeSymbol(
    "core.auto_trainer.safe_advance", "TrainingMenuPrechecker", _OrigTrainingMenuPrechecker
)
Perception: Any = _FacadeSymbol(
    "core.auto_trainer.safe_advance", "Perception", _OrigPerception
)


def perceive_goal_page(*args: Any, **kwargs: Any) -> Any:
    return sa_attr("perceive_goal_page", _orig_perceive_goal_page)(*args, **kwargs)


def rating_continue_point(*args: Any, **kwargs: Any) -> Any:
    return sa_attr("rating_continue_point", _orig_rating_continue_point)(*args, **kwargs)


def detect_event_option_layout(*args: Any, **kwargs: Any) -> Any:
    return sa_attr("detect_event_option_layout", _orig_detect_event_option_layout)(*args, **kwargs)


def is_larc_race_day(*args: Any, **kwargs: Any) -> Any:
    return sa_attr("is_larc_race_day", _orig_is_larc_race_day)(*args, **kwargs)


def analyze_main_menu_state(*args: Any, **kwargs: Any) -> Any:
    return sa_attr("analyze_main_menu_state", _orig_analyze_main_menu_state)(*args, **kwargs)


def analyze_screen_text(*args: Any, **kwargs: Any) -> Any:
    return sa_attr("analyze_screen_text", _orig_analyze_screen_text)(*args, **kwargs)


def fuse_perception_with_text(*args: Any, **kwargs: Any) -> Any:
    return sa_attr("fuse_perception_with_text", _orig_fuse_perception_with_text)(*args, **kwargs)


def inspect_skill_acquisition_page(*args: Any, **kwargs: Any) -> Any:
    return sa_attr("inspect_skill_acquisition_page", _orig_inspect_skill_acquisition_page)(*args, **kwargs)
