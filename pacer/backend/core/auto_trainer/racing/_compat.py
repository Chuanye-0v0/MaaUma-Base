"""Dynamic facade-symbol lookup so unittest.mock.patch on race_route works seamlessly."""
from __future__ import annotations

import importlib
import sys
from typing import Any

from core.auto_trainer.career_goal_perception import match_main_menu_named_goal as _orig_match_main_menu_named_goal
from core.auto_trainer.frame_safety import (
    FrameSafetyAnalyzer as _OrigFrameSafetyAnalyzer,
    LocalRapidOCRScreenTextReader as _OrigLocalRapidOCRScreenTextReader,
)
from core.auto_trainer.screen_graph import classify_landscape_race_screen as _orig_classify_landscape_race_screen
from core.perception import Perception as _OrigPerception
from core.screen_text import (
    analyze_screen_text as _orig_analyze_screen_text,
    fuse_perception_with_text as _orig_fuse_perception_with_text,
)
from core.training_menu_precheck import TrainingMenuPrechecker as _OrigTrainingMenuPrechecker
from .models import _current_clock_retry as _orig_current_clock_retry


def rr_attr(name: str, default: Any) -> Any:
    mod = sys.modules.get("core.auto_trainer.race_route")
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


RaceRoutePlanner: Any = _LazySymbol(
    "core.auto_trainer.race_route", "RaceRoutePlanner", "core.auto_trainer.racing.planner"
)
RaceRouteExecutor: Any = _LazySymbol(
    "core.auto_trainer.race_route", "RaceRouteExecutor", "core.auto_trainer.racing.executor"
)
SafeAdvancePlanner: Any = _LazySymbol(
    "core.auto_trainer.safe_advance", "SafeAdvancePlanner", "core.auto_trainer.advance.planner"
)
FrameSafetyAnalyzer: Any = _FacadeSymbol(
    "core.auto_trainer.race_route", "FrameSafetyAnalyzer", _OrigFrameSafetyAnalyzer
)
LocalRapidOCRScreenTextReader: Any = _FacadeSymbol(
    "core.auto_trainer.race_route", "LocalRapidOCRScreenTextReader", _OrigLocalRapidOCRScreenTextReader
)
Perception: Any = _FacadeSymbol(
    "core.auto_trainer.race_route", "Perception", _OrigPerception
)
TrainingMenuPrechecker: Any = _FacadeSymbol(
    "core.auto_trainer.race_route", "TrainingMenuPrechecker", _OrigTrainingMenuPrechecker
)


def classify_landscape_race_screen(*args: Any, **kwargs: Any) -> Any:
    return rr_attr("classify_landscape_race_screen", _orig_classify_landscape_race_screen)(*args, **kwargs)


def match_main_menu_named_goal(*args: Any, **kwargs: Any) -> Any:
    return rr_attr("match_main_menu_named_goal", _orig_match_main_menu_named_goal)(*args, **kwargs)


def analyze_screen_text(*args: Any, **kwargs: Any) -> Any:
    return rr_attr("analyze_screen_text", _orig_analyze_screen_text)(*args, **kwargs)


def fuse_perception_with_text(*args: Any, **kwargs: Any) -> Any:
    return rr_attr("fuse_perception_with_text", _orig_fuse_perception_with_text)(*args, **kwargs)


def _current_clock_retry(*args: Any, **kwargs: Any) -> Any:
    return rr_attr("_current_clock_retry", _orig_current_clock_retry)(*args, **kwargs)
