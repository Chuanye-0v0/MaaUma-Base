"""Constants, wait budgets, and template metadata for SafeAdvance."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
from ._compat import analyze_screen_text

ADVANCE_CONFIRM_TOKEN = "UG_PARENT_SAFE_ADVANCE_SANDBOX"
_UNBOUND_GOAL_SUMMARY_ACTION_PAIRS = frozenset(
    {
        (
            "safe_advance.goal_achievement_continue",
            "continue_goal_achievement_result",
        ),
        (
            "safe_advance.goal_remaining_continue",
            "continue_goal_remaining_result",
        ),
    }
)
_UNBOUND_GOAL_SUMMARY_ACTION_IDS = frozenset(
    action_id for action_id, _reason in _UNBOUND_GOAL_SUMMARY_ACTION_PAIRS
)
_GLOBAL_HOME_PASSIVE_RESOURCE_TERMS = frozenset({"TP", "RP", "招募", "商店"})
DIALOGUE_NEXT_POINT = (1248, 2280)
SUPPORTED_ADVANCE_SCREEN_SIZES = {(720, 1280), (1440, 2560)}
START_TRAINING_SETTLE_SECONDS = 1.2
POST_WAIT_FREEZE_POLL_SECONDS = 0.15
POST_WAIT_FREEZE_TIMEOUT_SECONDS = 8.0
POST_WAIT_FREEZE_STABLE_FRAMES = 2
POST_WAIT_FREEZE_MAX_FRAME_DIFF = 0.004
POST_WAIT_FREEZE_DIFF_HISTORY_LIMIT = 32
_OPTIONAL_NAVIGATION_PAIRS = frozenset({
    ("safe_advance.dialogue_next", "advance_dialogue"),
    ("safe_advance.dialogue_box", "advance_dialogue"),
    ("safe_advance.bottom_skip_toggle", "enable_bottom_skip_toggle"),
    ("safe_advance.bottom_skip_toggle", "enable_shorten_settings_skip_toggle"),
    ("safe_advance.larc_opening_movie_skip", "skip_larc_opening_movie"),
    ("safe_advance.larc_opening_skip_confirm", "confirm_larc_opening_skip"),
    ("safe_advance.larc_ss_open", "larc_ss_open"),
    ("safe_advance.larc_adaptation_return", "larc_adaptation_return"),
})


def post_wait_navigation_reobserve(result):
    """Only known non-consuming navigation can reobserve after a submitted tap."""
    decision=result.get('decision') or {};tap=decision.get('tap') or {}
    return (result.get('performed') is False and result.get('tap_performed') is True
        and result.get('input_may_have_occurred') is not True
        and result.get('tap_outcome') not in {'unknown','failed','pending'}
        and (tap.get('coordinate_name'),decision.get('reason')) in _OPTIONAL_NAVIGATION_PAIRS
        and not (result.get('frame_safety') or {}).get('dangerous')
        and (result.get('reason','').startswith('post_wait_freeze_')
             or result.get('reason')=='optional_skip_unconfirmed'))

SPECIAL_PAGE_WAIT_DEFAULT_LIMIT = 2
SPECIAL_PAGE_WAIT_MAX_LIMIT = 3
SPECIAL_PAGE_WAIT_REASONS = frozenset(
    {
        "grand_masters_crystal_reward_unconfirmed",
        "training_menu_transition",
    }
)
POST_WAIT_TRAINING_HUD_REFERENCE_SIZE = (720, 1280)
POST_WAIT_TRAINING_HUD_STABILITY_ROIS = (
    (0, 0, 350, 210),
    (160, 40, 710, 130),
    (25, 830, 700, 900),
)
LONG_SETTLE_REASONS = {
    "support_deck_start_training",
    "support_deck_start_confirm",
    "continue_fullscreen_result",
}
WISDOM_FRAGMENT_DIALOG_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[3]
    / "assets"
    / "safe_advance_templates"
    / "wisdom_fragment_unavailable_dialog_720.png"
)
WISDOM_FRAGMENT_DIALOG_MANIFEST_PATH = WISDOM_FRAGMENT_DIALOG_TEMPLATE_PATH.parent / "manifest.json"
WISDOM_FRAGMENT_DIALOG_TEMPLATE_SHA256 = "B133B6ECBC069B18DEC2FF88DB735C8399DEE2B0E6FFFDBBD058B519AAC76794"
WISDOM_FRAGMENT_DIALOG_SOURCE_PATH = (
    r"E:\uma\runs\ug_parent_live\u1_packet_local_turn010_011_retry2_20260711"
    r"\u1_packet_local_turn010_011_retry2_20260711_turn_010\advance\advance_step_007.png"
)
WISDOM_FRAGMENT_DIALOG_SOURCE_SHA256 = "0C531F2E9CCF317E5D469E50CFA31C02AAE81EE5079087E7A86C698FAFBCDB07"
WISDOM_FRAGMENT_DIALOG_ROI = (4, 377, 712, 530)
RECRUIT_HOME_NAV_REFERENCE_PATH = (
    Path(__file__).resolve().parents[3]
    / "runs"
    / "ug_parent_live"
    / "u1_new_run_20260715"
    / "setup_probe_20260715_112556"
    / "advance_step_001.png"
)
RECRUIT_HOME_NAV_REFERENCE_SHA256 = (
    "6654a3945c88624dfe3dad676b4940b92c58a70febc223c69ffb8085a61c3be1"
)
WISDOM_FRAGMENT_DIALOG_ANCHORS = {
    "panel_geometry": ((0, 0, 712, 530), 0.995, 0.85, 0.006),
    "header_green_geometry": ((0, 0, 712, 62), 0.994, 0.86, 0.012),
    "exact_red_warning_text": ((154, 369, 412, 34), 0.994, 0.90, 0.030),
    "left_close_button": ((51, 416, 289, 79), 0.985, 0.80, 0.012),
    "right_confirm_button": ((372, 416, 288, 79), 0.982, 0.79, 0.012),
}


class WisdomFragmentDialogState(str, Enum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    ASSET_ERROR = "ASSET_ERROR"


@dataclass(frozen=True)
class WisdomFragmentDialogResult:
    state: WisdomFragmentDialogState
    tap: tuple[int, int] | None = None
    asset_error: str | None = None


@dataclass(frozen=True)
class _WisdomFragmentTemplateLoad:
    template: np.ndarray | None
    error: str | None = None



@dataclass
class SpecialPageWaitBudget:
    """Bound consecutive waits on known transitional special pages."""

    limit: int = SPECIAL_PAGE_WAIT_DEFAULT_LIMIT
    reason: str | None = None
    route_state: str | None = None
    count: int = 0
    last_source_image_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.limit) is not int or not 1 <= self.limit <= SPECIAL_PAGE_WAIT_MAX_LIMIT:
            raise ValueError("special_page_wait_limit")

    def observe(
        self,
        decision: dict[str, Any],
        *,
        source_image_sha256: str,
    ) -> dict[str, Any] | None:
        reason = str(decision.get("reason", ""))
        route_state = str(decision.get("route_state", ""))
        is_special_wait = (
            decision.get("action") == "wait"
            and reason in SPECIAL_PAGE_WAIT_REASONS
        )
        if not is_special_wait:
            self.reset()
            return None
        if (reason, route_state) == (self.reason, self.route_state):
            self.count += 1
        else:
            self.reason = reason
            self.route_state = route_state
            self.count = 1
        # A repeated source frame is evidence of no progress, never a reset.
        self.last_source_image_sha256 = source_image_sha256
        return self.to_dict()

    @property
    def exhausted(self) -> bool:
        return self.count > self.limit

    def reset(self) -> None:
        self.reason = None
        self.route_state = None
        self.count = 0
        self.last_source_image_sha256 = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "route_state": self.route_state,
            "count": self.count,
            "limit": self.limit,
            "last_source_image_sha256": self.last_source_image_sha256,
            "exhausted": self.exhausted,
        }


def _career_end_skill_points_from_visible_text(text: str) -> int | None:
    lines = [re.sub(r"\s+", "", line) for line in text.splitlines() if line.strip()]
    candidates: list[int] = []
    for index, line in enumerate(lines):
        inline = re.fullmatch(r"(?:技能点数[:：]?(\d{1,4})|(\d{1,4})技能点数)", line)
        if inline is not None:
            candidates.append(int(inline.group(1) or inline.group(2)))
            continue
        if line != "技能点数":
            continue
        for adjacent in (index - 1, index + 1):
            if 0 <= adjacent < len(lines) and re.fullmatch(r"\d{1,4}", lines[adjacent]):
                candidates.append(int(lines[adjacent]))
    return candidates[0] if len(candidates) == 1 else None


def _unused_skill_points_from_visible_text(text: str) -> int | None:
    lines = [re.sub(r"\s+", "", line) for line in text.splitlines() if line.strip()]
    candidates: list[int] = []
    for index, line in enumerate(lines):
        inline = re.fullmatch(
            r"(?:剩余技能点数[:：]?(\d{1,4})|(\d{1,4})剩余技能点数)",
            line,
        )
        if inline is not None:
            candidates.append(int(inline.group(1) or inline.group(2)))
            continue
        if line != "剩余技能点数":
            continue
        for adjacent in (index - 1, index + 1):
            if 0 <= adjacent < len(lines) and re.fullmatch(r"\d{1,4}", lines[adjacent]):
                candidates.append(int(lines[adjacent]))
    return candidates[0] if len(candidates) == 1 else None
