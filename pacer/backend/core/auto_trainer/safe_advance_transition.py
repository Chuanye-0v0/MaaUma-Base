from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from core.auto_trainer.route_handoff import is_route_handoff
from core.auto_trainer.route_transition_session import POST_ACTION_ROUTE_SUCCESSORS


_PROGRESS_STATES = (
    "dialogue",
    "event_choice",
    "loading",
    "main_menu",
    "modal",
    "race_list",
    "race_scene",
    "skill_acquisition",
    "training_menu",
)
_DIALOGUE_ACTION_PARTS = (
    "continue",
    "dialogue",
    "fullscreen",
    "goal_achievement",
    "inheritance_factor_confirm",
    "next_page",
    "result",
    "reward",
    "single_event_option",
    "story_fast_forward",
)
_CLOSE_ACTION_PARTS = (
    "close",
    "finish_training",
    "menu_close",
)
_SPECIAL_LOADING_WAIT_REASONS = frozenset(
    {
        "grand_masters_crystal_reward_unconfirmed",
        "training_menu_transition",
    }
)


_ROUTE_STATE_ALIASES = {
    "training-menu": "training_menu",
    "main-menu": "main_menu",
    "event-choice": "event_choice",
}


def normalize_route_state(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("route_state")
    normalized = value.strip().lower()
    return _ROUTE_STATE_ALIASES.get(normalized, normalized.replace("-", "_"))


def _normalized_states(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        state = normalize_route_state(value)
        if state not in result:
            result.append(state)
    return tuple(result)


@dataclass(frozen=True)
class SafeAdvanceRoutePolicy:
    allowed_observed_states: tuple[str, ...]
    source: str
    constrained: bool = True
    pending_action_id: str | None = None
    action_source_sha256: str | None = None
    opening_context: bool = False

    @classmethod
    def resolve(
        cls,
        *,
        current_state: str | None,
        expected_next_states: Iterable[str],
        bootstrap_state: str | None,
        last_action_id: str | None = None,
        action_source_sha256: str | None = None,
    ) -> "SafeAdvanceRoutePolicy":
        if bootstrap_state in {'live-career', 'live-career-opening'}:
            return cls(_PROGRESS_STATES, 'live_current_screen', opening_context=bootstrap_state=='live-career-opening')
        pending = _normalized_states(expected_next_states)
        if pending:
            return cls(
                pending,
                "pending",
                pending_action_id=last_action_id,
                action_source_sha256=action_source_sha256,
                opening_context=bootstrap_state=='career-opening',
            )
        if current_state is not None:
            return cls((normalize_route_state(current_state),), "current", opening_context=bootstrap_state=='career-opening')
        if bootstrap_state is not None:
            if bootstrap_state=='career-opening':
                return cls(('dialogue','modal','loading','main_menu'),'career_opening_bootstrap',opening_context=True)
            return cls((normalize_route_state(bootstrap_state),), "bootstrap")
        return cls((), "missing")

    def allows_observed_state(self, state: str) -> bool:
        return normalize_route_state(state) in self.allowed_observed_states


# Shared career navigation: one exact identity defines its source and successors.
# These contracts do not assume Grand Masters mechanics or a scenario calendar.
_COMMON_CAREER_DECISIONS = {
    ("tap", "cancel_unconsumed_outing_chooser", "safe_advance.outing_chooser_cancel"):
        ("mei_outing_chooser", ("main_menu", "loading")),
    ("tap", "continue_goal_failure_summary", "safe_advance.goal_failure_summary_continue"):
        ("modal", ("dialogue", "loading", "modal", "skill_acquisition", "main_menu")),
    ("tap", "larc_date_change_confirm", "safe_advance.date_change_confirm"):
        ("main_menu", ("main_menu", "loading")),
    ("tap", "cancel_larc_fan_warning", "safe_advance.larc_fan_warning_cancel"):
        ("main_menu", ("main_menu", "loading")),
    ("tap", "cancel_larc_adaptation_race_warning", "safe_advance.larc_adaptation_warning_cancel"):
        ("modal", ("modal", "race_list", "main_menu", "loading")),
    ("tap", "larc_adaptation_open", "safe_advance.larc_adaptation_open"):
        ("main_menu", ("modal", "loading")),
    ("tap", "open_larc_first_arc_skills", "safe_advance.larc_first_arc_skill_open"):
        ("main_menu", ("skill_acquisition", "loading")),
    ("tap", "open_larc_final_arc_skills", "safe_advance.larc_final_arc_skill_open"):
        ("main_menu", ("skill_acquisition", "loading")),
    ("tap", "larc_adaptation_return", "safe_advance.larc_adaptation_return"):
        ("modal", ("main_menu", "loading")),
    ("wait", "larc_adaptation_return_loading", ""):
        ("loading", ("main_menu", "loading")),
    ("tap", "larc_adaptation_select", "safe_advance.larc_adaptation_select"):
        ("modal", ("modal", "loading")),
    ("tap", "larc_adaptation_level_up", "safe_advance.larc_adaptation_level_up"):
        ("modal", ("modal", "loading")),
    ("tap", "larc_adaptation_review", "safe_advance.larc_adaptation_review"):
        ("modal", ("modal", "loading")),
    ("tap", "larc_adaptation_upgrade", "safe_advance.larc_adaptation_upgrade"):
        ("modal", ("modal", "loading")),
    ("tap", "larc_adaptation_result_close", "safe_advance.larc_adaptation_result_close"):
        ("modal", ("modal", "loading")),
    ("wait", "larc_adaptation_grid_loading", ""):
        ("loading", ("modal", "loading")),
    ("wait", "larc_adaptation_awaiting_result", ""):
        ("modal", ("modal", "loading")),
    ("wait", "larc_adaptation_selected_level_loading", ""):
        ("modal", ("modal", "loading")),
    ("wait", "larc_adaptation_modal_loading", ""):
        ("modal", ("modal", "loading")),
    ("tap", "larc_ss_open", "safe_advance.larc_ss_open"):
        ("training_menu", ("training_menu", "modal", "loading")),
    ("tap", "larc_ss_confirm", "safe_advance.larc_ss_confirm"):
        ("training_menu", ("dialogue", "loading", "modal", "event_choice", "main_menu")),
    ("tap", "larc_explanation_continue", "safe_advance.larc_explanation_continue"):
        ("modal", ("dialogue", "event_choice", "loading", "main_menu", "modal", "training_menu")),
    ("tap", "confirm_race_recommendation_settings", "safe_advance.race_recommendation_top_confirm"):
        ("modal", ("race_list", "loading", "main_menu", "dialogue", "modal")),
    ("tap", "go_to_race_entry_requirement", "safe_advance.go_to_race"):
        ("modal", ("race_list", "modal", "loading")),
    ("tap", "larc_tutorial_skip", "safe_advance.larc_tutorial_skip"):
        ("event_choice", ("dialogue", "event_choice", "loading", "main_menu", "modal")),
    ("tap", "larc_tutorial_decline", "safe_advance.larc_tutorial_decline"):
        ("event_choice", ("dialogue", "event_choice", "loading", "main_menu", "modal")),
    ("tap", "larc_ss_information_close", "larc_ss_information.close"):
        ("larc_ss_information", ("training_menu",)),
    ("tap", "select_shorten_all_events", "safe_advance.shorten_all_events"):
        ("modal", ("modal",)),
    ("tap", "skip_larc_opening_movie", "safe_advance.larc_opening_movie_skip"):
        ("dialogue", ("dialogue","loading","modal","main_menu")),
    ("tap", "confirm_larc_opening_skip", "safe_advance.larc_opening_skip_confirm"):
        ("modal", ("dialogue","loading","modal","main_menu")),
    ("wait", "larc_opening_movie_pending", ""):
        ("dialogue", ("dialogue","loading","modal","main_menu")),
    ("wait", "larc_goal_start_animation", ""):
        ("loading", POST_ACTION_ROUTE_SUCCESSORS),
    ("tap", "enable_shorten_settings_skip_toggle", "safe_advance.bottom_skip_toggle"):
        ("modal", ("modal",)),
    ("wait", "shorten_settings_skip_loading", ""):
        ("modal", ("modal",)),
    ("tap", "uncheck_do_not_shorten_unread", "safe_advance.shorten_unread_toggle"):
        ("modal", ("modal",)),
    ("tap", "confirm_shorten_all_events", "safe_advance.shorten_events_confirm"):
        ("modal", ("dialogue", "loading", "main_menu", "modal")),
    ("tap", "return_main_menu_from_training", "safe_advance.training_menu_back"):
        ("training_menu", ("loading", "main_menu")),
    ("tap", "confirm_rest", "safe_advance.rest_confirm"):
        ("modal", ("dialogue", "event_choice", "loading", "main_menu", "training_menu")),
    ("tap", "confirm_rest_outing", "safe_advance.rest_outing_confirm"):
        ("modal", ("dialogue", "event_choice", "loading", "main_menu", "training_menu")),
    ("tap", "cancel_clinic_confirmation", "safe_advance.clinic_confirm_cancel"):
        ("modal", ("main_menu",)),
}


_SCENARIO_SETUP_DECISIONS = {
    ("tap", "scenario_select_continue", "safe_advance.scenario_continue"):
        ("scenario_select", ("dialogue", "loading", "modal", "training_menu")),
}


_TERMINAL_DECISIONS = {
    ("tap", "open_career_end_skill_acquisition", "safe_advance.career_end_skill_acquisition"):
        ("modal", ("skill_acquisition", "loading")),
    ("tap", "open_career_end_finish_confirmation", "safe_advance.career_end_finish"):
        ("modal", ("modal", "loading")),
    ("tap", "cancel_career_end_finish", "safe_advance.career_end_finish_cancel"):
        ("modal", ("modal", "loading")),
    ("tap", "finish_training_confirm", "safe_advance.finish_training"):
        ("modal", ("dialogue", "loading", "modal")),
    ("tap", "continue_final_training_rating", "safe_advance.final_training_rating_continue"):
        ("dialogue", ("dialogue", "loading", "modal")),
    ("tap", "confirm_inheritance_factors", "safe_advance.confirm_inheritance_factors"):
        ("dialogue", ("dialogue", "loading", "modal")),
    ("tap", "close_trainee_detail", "safe_advance.trainee_detail_close"):
        ("dialogue", ("dialogue", "loading", "modal")),
    ("tap", "close_honor_title", "safe_advance.honor_title_confirm"):
        ("dialogue", ("dialogue", "loading", "modal")),
    ("tap", "continue_final_career_summary", "safe_advance.final_career_summary_continue"):
        ("dialogue", ("dialogue", "loading", "modal")),
    ("tap", "continue_final_career_reward", "safe_advance.final_career_reward_continue"):
        ("dialogue", ("dialogue", "loading", "modal")),
    ("tap", "continue_scenario_score_record", "safe_advance.scenario_score_record_continue"):
        ("dialogue", ("dialogue", "loading", "modal")),
    ("tap", "next_support_reward_page", "safe_advance.support_reward_next_page"):
        ("dialogue", ("loading", "main_menu")),
}


_ALLOWED_V1_DECISIONS: dict[str, frozenset[tuple[str, str, str]]] = {
    "resource_prompt": frozenset({
        ("tap", "cancel_clock_retry", "safe_advance.clock_retry_cancel"),
    }),
    "training_menu": frozenset(
        {
            ("stop", "ready_training_menu", ""),
        }
    ),
    "main_menu": frozenset(
        {
            ("stop", "ready_main_menu", ""),
            ("stop", "global_home", ""),
            ("tap", "enter_training_from_home", "safe_advance.home_training"),
            (
                "tap",
                "enable_bottom_skip_toggle",
                "safe_advance.bottom_skip_toggle",
            ),
        }
    ),
    "dialogue": frozenset(
        {
            ("tap", "continue_race_result", "safe_advance.race_result_continue"),
            ("tap", "skip_race_story_scene", "safe_advance.race_story_fast_forward"),
            ("tap", "continue_final_loss_result", "safe_advance.final_loss_result_continue"),
            (
                "tap",
                "skip_received_login_reward",
                "safe_advance.bottom_right_skip",
            ),
            (
                "tap",
                "continue_login_walk_reward",
                "safe_advance.login_walk_reward_continue",
            ),
            ("tap", "advance_dialogue", "safe_advance.dialogue_box"),
            ("tap", "advance_dialogue", "safe_advance.dialogue_next"),
            (
                "tap",
                "continue_fullscreen_result",
                "safe_advance.fullscreen_continue",
            ),
            ("tap", "story_fast_forward", "safe_advance.story_fast_forward"),
            (
                "tap",
                "enable_bottom_skip_toggle",
                "safe_advance.bottom_skip_toggle",
            ),
            ("wait", "bottom_skip_double_enabled", ""),
            (
                "tap",
                "continue_goal_remaining_result",
                "safe_advance.goal_remaining_continue",
            ),
            (
                "tap",
                "continue_goal_achievement_result",
                "safe_advance.goal_achievement_continue",
            ),
            (
                "tap",
                "continue_goal_progress_result",
                "safe_advance.goal_progress_continue",
            ),
            (
                "tap",
                "inheritance_factor_confirm",
                "safe_advance.inheritance_factor_confirm",
            ),
            (
                "tap",
                "continue_swbc_growth_summary",
                "safe_advance.swbc_growth_summary_continue",
            ),
            (
                "tap",
                "grand_masters_crystal_reward",
                "safe_advance.dialogue_box",
            ),
        }
    ),
    "loading": frozenset(
        {
            ("wait", "loading", ""),
            ("wait", "transition", ""),
            ("wait", "connection_overlay_wait", ""),
            ("wait", "training_action_animation_wait", ""),
            ("wait", "grand_masters_crystal_reward_unconfirmed", ""),
            ("wait", "training_menu_transition", ""),
        }
    ),
    "modal": frozenset(
        {
            (
                "tap",
                "continue_training_modal",
                "safe_advance.continue_training",
            ),
            (
                "tap",
                "close_training_pass_reset",
                "safe_advance.training_pass_reset_close",
            ),
            (
                "tap",
                "close_exchange_points_expiry",
                "safe_advance.exchange_points_expiry_close",
            ),
            (
                "tap",
                "confirm_wisdom_learning_prompt",
                "safe_advance.wisdom_learning_prompt_confirm",
            ),
            ("tap", "learn_wisdom", "safe_advance.learn_wisdom"),
            ("tap", "learn_wisdom_modal", "safe_advance.learn_wisdom"),
            (
                "tap",
                "confirm_race_recommendation_top",
                "safe_advance.race_recommendation_top_confirm",
            ),
            ("tap", "confirm_race_recommendation_settings", "safe_advance.race_recommendation_top_confirm"),
            (
                "tap",
                "decline_grand_masters_tutorial",
                "safe_advance.grand_masters_tutorial_decline",
            ),
            (
                "tap",
                "confirm_additional_data_download",
                "safe_advance.additional_data_download_confirm",
            ),
            (
                "tap",
                "close_announcement",
                "safe_advance.announcement_close",
            ),
            ("tap", "close_knowledge_table", "safe_advance.knowledge_close"),
        }
    ),
}


def _decision_identity(decision: dict[str, Any]) -> tuple[str, str, str]:
    tap = decision.get("tap") if isinstance(decision.get("tap"), dict) else {}
    return (
        str(decision.get("action") or "").strip().lower(),
        str(decision.get("reason") or "").strip().lower(),
        str(tap.get("coordinate_name") or "").strip().lower(),
    )


_EVENT_SUCCESSORS = ("dialogue", "event_choice", "loading", "main_menu", "modal", "training_menu")


def event_choice_option_index(choice: str, option_count: int) -> int | None:
    """Resolve a displayed slot against this frame, including numbered slots."""
    index = {"top": 1, "bottom": option_count, "second": 2, "third": 3, "fourth": 4}.get(choice)
    if index is None and isinstance(choice, str) and choice.startswith("option_"):
        number = choice.removeprefix("option_")
        if number.isdecimal() and number == str(int(number)):
            index = int(number)
    return index if type(index) is int and 1 <= index <= option_count else None


def _event_action_candidate(decision: dict[str, Any]) -> bool:
    _, reason, coordinate = _decision_identity(decision)
    return (coordinate.startswith("event_choice.") or reason.startswith("choose_event_")
            or reason in {"choose_single_event_option", "advance_event_dialogue"}
            or coordinate == "safe_advance.single_event_option")


def _event_action_matches_frame(decision: dict[str, Any]) -> bool:
    """Pair action/reason/slot with the existing detector's current geometry.

    Both event planners already carry their source image. Reading it here covers
    their real outputs without a parallel action enumeration or new producer
    metadata. The executor and route session retain their source-SHA checks.
    """
    import cv2
    from core.auto_trainer.event_choice_route import EventChoicePlanner

    action, reason, coordinate = _decision_identity(decision)
    tap = decision.get("tap")
    if (action != "tap" or not isinstance(tap, dict)
            or set(tap) != {"coordinate_name", "x", "y", "scaled"}
            or tap["scaled"] is not False
            or type(tap["x"]) is not int or type(tap["y"]) is not int
            or any(decision.get(key) is not None for key in ("gesture", "keyevent", "swipe"))):
        return False
    single = (reason, coordinate) == ("choose_single_event_option", "safe_advance.single_event_option")
    if decision.get("route_state", "event_choice" if single else None) != "event_choice":
        return False
    evidence = decision.get("evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("image"), str):
        return False
    image = cv2.imread(evidence["image"])
    if image is None:
        return False
    point = (tap["x"], tap["y"])
    if (reason, coordinate) == ("advance_event_dialogue", "event_choice.dialogue_next"):
        return point in (EventChoicePlanner._event_summary_continue_point(image),
                         EventChoicePlanner._event_dialogue_advance_point(image))
    text = evidence.get("screen_text") or {}
    layout = EventChoicePlanner._event_option_layout(image, text.get("visible_text"))
    if single:
        return layout["option_count"] == 1 and layout["ordered_points"] == [point]
    slot = coordinate.removeprefix("event_choice.")
    if coordinate != f"event_choice.{slot}" or reason != f"choose_event_{slot}":
        return False
    index = event_choice_option_index(slot, layout["option_count"])
    return index is not None and point == tuple(layout["ordered_points"][index - 1])


def action_allowed_from_state(
    current_state: str,
    decision: dict[str, Any],
) -> bool:
    state = normalize_route_state(current_state)
    if decision.get("action") == "handoff":
        return is_route_handoff(decision) and decision["target"] == state
    identity = _decision_identity(decision)
    if _event_action_candidate(decision):
        return state == "event_choice" and _event_action_matches_frame(decision)
    if identity == (
        "tap",
        "enable_bottom_skip_toggle",
        "safe_advance.bottom_skip_toggle",
    ) and decision.get("route_state"):
        try:
            routed_state = normalize_route_state(str(decision["route_state"]))
            if routed_state not in {"main_menu", "dialogue"}:
                return False
            return state == routed_state
        except ValueError:
            return False
    common = _COMMON_CAREER_DECISIONS.get(identity)
    if common is not None:
        return state == common[0]
    setup = _SCENARIO_SETUP_DECISIONS.get(_decision_identity(decision))
    if setup is not None:
        return state == setup[0]
    terminal = _TERMINAL_DECISIONS.get(_decision_identity(decision))
    if terminal is not None:
        return state == terminal[0]
    allowed = _ALLOWED_V1_DECISIONS.get(state)
    if allowed is None:
        return decision.get("action") == "stop"
    return _decision_identity(decision) in allowed


def is_global_route_interrupt(decision: dict[str, Any]) -> bool:
    identity = _decision_identity(decision)
    if identity in {
        ("stop", "resource_prompt", ""),
        ("stop", "resource_prompt_unreadable", ""),
        ("stop", "connection_conflict", ""),
        (
            "tap",
            "return_title_from_connection_conflict",
            "safe_advance.return_title",
        ),
        ("tap", "tap_title_start", "safe_advance.title_start"),
    }:
        return True
    return (
        identity == ("wait", "loading", "")
        and str(decision.get("state") or "").strip().lower()
        == "connection_overlay_wait"
        and str(decision.get("route_state") or "").strip().lower()
        == "connection_overlay"
    )


def observed_route_state(decision: dict[str, Any]) -> str:
    if decision.get("action") == "handoff" and is_route_handoff(decision):
        return decision["target"]
    reason = str(decision.get("reason") or "").strip().lower()
    state = str(decision.get("state") or "").strip().lower()
    route_state = str(decision.get("route_state") or "").strip().lower()
    identity = _decision_identity(decision)
    if identity == ("tap", "cancel_clock_retry", "safe_advance.clock_retry_cancel"):
        return "resource_prompt"
    if identity == (
        "tap",
        "enter_training_from_home",
        "safe_advance.home_training",
    ):
        return "main_menu"
    if (
        identity[0] == "wait"
        and reason in _SPECIAL_LOADING_WAIT_REASONS
        and route_state == "loading"
    ):
        return "loading"
    combined = " ".join((reason, state, route_state))
    if any(token in combined for token in ("resource_prompt", "purchase", "shop", "currency")):
        return "resource_prompt"
    common = _COMMON_CAREER_DECISIONS.get(identity)
    if common is not None:
        return common[0]
    setup = _SCENARIO_SETUP_DECISIONS.get(identity)
    if setup is not None:
        return setup[0]
    terminal = _TERMINAL_DECISIONS.get(identity)
    if terminal is not None:
        return terminal[0]
    if identity == (
        "tap", "continue_swbc_growth_summary",
        "safe_advance.swbc_growth_summary_continue",
    ):
        return "dialogue"
    if (
        decision.get("action") == "stop"
        and decision.get("reason") == "wbc_race_hub"
        and decision.get("tap") is None
    ):
        return "race_list"
    if (decision.get("action") == "wait" and decision.get("reason") == "transition"
        and decision.get("route_state") == "transition" and decision.get("tap") is None):
        return "loading"
    if "skill_acquisition" in combined:
        return "skill_acquisition"
    if "training_menu" in combined or "ready_training" in combined:
        return "training_menu"
    if "global_home" in combined or "main_menu" in combined or "ready_main" in combined:
        return "main_menu"
    if (
        "enable_bottom_skip_toggle" in combined
        or "bottom_skip_double_enabled" in combined
        or "auto_skip_wait" in combined
    ):
        return "dialogue"
    if "event_choice" in combined or "single_event_option" in combined:
        return "event_choice"
    if "race_list" in combined or "race_detail" in combined or "race_menu" in combined:
        return "race_list"
    if "race_scene" in combined or "race_title" in combined or "race_running" in combined:
        return "race_scene"
    if "loading" in combined or "transition_wipe" in combined or "connection_overlay_wait" in combined:
        return "loading"
    if any(
        token in combined
        for token in (
            "dialogue",
            "story",
            "result",
            "reward",
            "goal_achieved",
            "goal_achievement",
            "inheritance_factor",
        )
    ):
        return "dialogue"
    if any(
        token in combined
        for token in (
            "announcement",
            "confirm",
            "knowledge_table",
            "modal",
            "tutorial",
            "formation",
        )
    ):
        return "modal"
    return route_state or state or reason or "unknown"


def expected_next_states_for_decision(decision: dict[str, Any]) -> tuple[str, ...]:
    if str(decision.get("action") or "") not in {"tap", "wait"}:
        return ()
    if _event_action_candidate(decision):
        return _EVENT_SUCCESSORS if _event_action_matches_frame(decision) else ()
    common = _COMMON_CAREER_DECISIONS.get(_decision_identity(decision))
    if common is not None:
        return common[1]
    setup = _SCENARIO_SETUP_DECISIONS.get(_decision_identity(decision))
    if setup is not None:
        return setup[1]
    if _decision_identity(decision) == ("tap", "cancel_clock_retry", "safe_advance.clock_retry_cancel"):
        return ("dialogue", "loading", "modal")
    if _decision_identity(decision) == ("tap", "continue_final_loss_result", "safe_advance.final_loss_result_continue"):
        return ("dialogue", "loading", "modal")
    terminal = _TERMINAL_DECISIONS.get(_decision_identity(decision))
    if terminal is not None:
        return terminal[1]
    tap = decision.get("tap") if isinstance(decision.get("tap"), dict) else {}
    action_id = str(tap.get("coordinate_name") or "").strip().lower()
    reason = str(decision.get("reason") or "").strip().lower()
    route_state = str(decision.get("route_state") or "").strip().lower()
    identity = f"{action_id} {reason}"
    if str(decision.get("action")) == "wait":
        if reason == "training_action_animation_wait" and not action_id:
            return POST_ACTION_ROUTE_SUCCESSORS
        if ("loading" in identity or reason in _SPECIAL_LOADING_WAIT_REASONS
            or (decision.get("reason") == "transition" and decision.get("route_state") == "transition"
                and decision.get("tap") is None)):
            return (
                "dialogue",
                "event_choice",
                "loading",
                "main_menu",
                "modal",
                "race_list",
                "race_scene",
                "skill_acquisition",
                "training_menu",
                "wbc_race_hub",
            )
        if "bottom_skip_double_enabled" in identity:
            return (
                "dialogue",
                "event_choice",
                "loading",
                "main_menu",
                "modal",
                "training_menu",
            )
        return ()
    if (
        action_id == "safe_advance.home_training"
        and reason == "enter_training_from_home"
    ):
        return ("dialogue", "loading", "modal", "scenario_select", "training_menu")
    if "additional_data_download" in identity:
        return ("dialogue", "loading", "main_menu", "modal")
    if "title_start" in identity:
        return ("dialogue", "loading", "main_menu", "modal", "skip_startup_promo")
    if action_id == "safe_advance.bottom_right_skip" and reason == "skip_startup_promo":
        return ("dialogue", "loading", "main_menu", "modal", "skip_startup_promo")
    if (
        action_id == "safe_advance.career_end_skill_acquisition"
        and reason == "open_career_end_skill_acquisition"
    ):
        return ("skill_acquisition", "loading")
    if (
        action_id == "safe_advance.career_end_finish_cancel"
        and reason == "cancel_career_end_finish"
    ):
        return ("modal", "loading")
    if "home_training" in identity:
        return ("dialogue", "loading", "main_menu", "modal", "training_menu")
    if "tutorial_decline" in identity:
        return (
            "dialogue",
            "event_choice",
            "loading",
            "main_menu",
            "modal",
            "training_menu",
        )
    if "bottom_skip_toggle" in identity:
        if route_state == "main_menu":
            return ("main_menu",)
        return ("dialogue", "event_choice", "loading", "main_menu", "training_menu")
    if "go_to_race" in identity or "race_story_fast_forward" in identity:
        return ("dialogue", "loading", "modal", "race_list", "race_scene")
    if "support_deck_start" in identity or "support_deck_friend_slot" in identity:
        return ("dialogue", "loading", "modal", "training_menu")
    if "learn_wisdom" in identity or "_confirm" in identity:
        return _PROGRESS_STATES
    if any(part in identity for part in ("_back", "return_home", "return_title")):
        return ("dialogue", "loading", "main_menu", "modal", "training_menu")
    if any(part in identity for part in _DIALOGUE_ACTION_PARTS):
        return _PROGRESS_STATES
    if any(part in identity for part in _CLOSE_ACTION_PARTS):
        return ("dialogue", "loading", "main_menu", "modal", "training_menu")
    return ()
