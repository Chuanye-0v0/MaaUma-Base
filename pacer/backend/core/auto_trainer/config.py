from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any


REQUIRED_SCHEMA_VERSION = "ug_parent_goal.v1"
REQUIRED_LIVE_CONFIRM_TOKEN = "UG_PARENT_AUTOTRAINER_SANDBOX"
STAT_KEYS = ("speed", "stamina", "power", "guts", "wisdom")
SUPPORTED_SCENARIOS = {"grand_masters", "larc"}
SUPPORTED_GOALS = {"ug_parent"}
SUPPORTED_RISK_TOLERANCE = {"low", "medium", "high"}
SUPPORTED_SKILL_TIMING_MODES = {
    "terminal",
    "scenario_required_first",
    "interval",
    "immediate",
}


@dataclass(frozen=True)
class SkillTiming:
    """When the loop should visit the skill page and buy skills.

    Modes:
    - terminal: learn all affordable skills shortly before the final race.
    - scenario_required_first: learn required skill counts early (e.g. 5 speed
      skills for a support-card training bonus), then bulk-learn at the end.
    - interval: visit the skill page every ``interval_turns`` turns.
    - immediate: buy a skill as soon as the inventory shows an affordable one.
    """

    mode: str = "terminal"
    early_required: dict[str, int] = field(default_factory=dict)
    interval_turns: int = 6
    min_pt: int = 60
    final_lead_turns: int = 2
    early_options: dict[str, Any] = field(default_factory=dict)
    early_skill_allowlist: list[str] = field(default_factory=list)
    # None preserves legacy normal-career behavior; cut requires explicit enable.
    mid_skill_learning: dict[str, Any] | None = None
    # Final build purpose and the optional first-Arc list are independent of
    # the support-card startup list above. Missing configuration stays opt-in.
    arc_policy: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SkillTiming":
        data = data or {}
        mode = str(data.get("mode", "terminal"))
        if mode not in SUPPORTED_SKILL_TIMING_MODES:
            raise ValueError(f"skill_timing.mode_must_be_one_of:{mode}")
        early_required = data.get("early_required", {})
        if not isinstance(early_required, dict):
            raise ValueError("skill_timing.early_required_must_be_object")
        parsed_early: dict[str, int] = {}
        for key, value in early_required.items():
            if key not in STAT_KEYS:
                raise ValueError(f"skill_timing.early_required.unknown_key:{key}")
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    f"skill_timing.early_required.invalid_count:{key}"
                )
            parsed_early[key] = value
        interval_turns = data.get("interval_turns", 6)
        if (
            isinstance(interval_turns, bool)
            or not isinstance(interval_turns, int)
            or interval_turns < 1
        ):
            raise ValueError("skill_timing.interval_turns_must_be_positive_integer")
        min_pt = data.get("min_pt", 60)
        if isinstance(min_pt, bool) or not isinstance(min_pt, int) or min_pt < 0:
            raise ValueError("skill_timing.min_pt_must_be_non_negative_integer")
        final_lead_turns = data.get("final_lead_turns", 2)
        if (
            isinstance(final_lead_turns, bool)
            or not isinstance(final_lead_turns, int)
            or final_lead_turns < 0
        ):
            raise ValueError(
                "skill_timing.final_lead_turns_must_be_non_negative_integer"
            )
        from core.auto_trainer.larc_support_skill_startup import startup_options,normalize_mid_skill_learning
        allowlist=data.get("early_skill_allowlist",[])
        if not isinstance(allowlist,list) or any(not isinstance(v,str) or not v.strip() for v in allowlist):
            raise ValueError("skill_timing.early_skill_allowlist_must_be_names_or_ids")
        allowlist=list(dict.fromkeys(v.strip() for v in allowlist))
        early_options=data.get("early_options",{})
        startup_options(early_options)
        mid_skill_learning=data.get("mid_skill_learning")
        if mid_skill_learning is not None:
            mid_skill_learning=normalize_mid_skill_learning(mid_skill_learning)
        from core.auto_trainer.larc_target_skills import normalize_arc_policy
        arc_policy = normalize_arc_policy(data.get("arc_policy"))
        return cls(
            mode=mode,
            early_required=parsed_early,
            interval_turns=interval_turns,
            min_pt=min_pt,
            final_lead_turns=final_lead_turns,
            early_options=dict(early_options),
            early_skill_allowlist=allowlist,
            mid_skill_learning=mid_skill_learning,
            arc_policy=arc_policy,
        )


@dataclass(frozen=True)
class ExecutionPolicy:
    allow_training_confirm: bool = False
    allow_clinic: bool = False
    allow_race: bool = False
    allow_event_choice: bool = False
    stop_on_paid_resource_prompt: bool = True
    stop_on_unknown_screen: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ExecutionPolicy":
        data = data or {}
        allow_clinic = data.get("allow_clinic", False)
        if not isinstance(allow_clinic, bool):
            raise ValueError("execution.allow_clinic_must_be_boolean")
        return cls(
            allow_training_confirm=bool(data.get("allow_training_confirm", False)),
            allow_clinic=allow_clinic,
            allow_race=bool(data.get("allow_race", False)),
            allow_event_choice=bool(data.get("allow_event_choice", False)),
            stop_on_paid_resource_prompt=bool(
                data.get("stop_on_paid_resource_prompt", True)
            ),
            stop_on_unknown_screen=bool(data.get("stop_on_unknown_screen", True)),
        )


@dataclass(frozen=True)
class UGParentGoal:
    schema_version: str
    scenario: str
    goal: str
    target_rank: str
    target_stats: dict[str, int]
    total_turns: int = 72
    factor_priority: list[str] = field(default_factory=list)
    skill_priority: list[str] = field(default_factory=list)
    risk_tolerance: str = "medium"
    execution: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    skill_timing: SkillTiming = field(default_factory=SkillTiming)
    # L'Arc eligibility override, in percent. None uses its policy default;
    # risk_tolerance still controls soft recovery/risk preferences separately.
    training_failure_rate_limit: float | None = None
    event_preferences: dict[str, Any] = field(default_factory=dict)
    larc_policy: dict[str, Any] = field(default_factory=dict)
    race_retry: dict[str, Any] | None = None
    running_style: str | None = None
    race_running_styles: dict[str, str] = field(default_factory=dict)
    custom_races: list[str] = field(default_factory=list)

    @classmethod
    def default(cls) -> "UGParentGoal":
        return cls(
            schema_version=REQUIRED_SCHEMA_VERSION,
            scenario="grand_masters",
            goal="ug_parent",
            total_turns=72,
            target_rank="UG",
            target_stats={
                "speed": 1200,
                "stamina": 600,
                "power": 1000,
                "guts": 600,
                "wisdom": 900,
            },
            factor_priority=["speed", "power", "distance", "turf"],
            skill_priority=["scenario_gold", "distance_core", "recovery"],
            risk_tolerance="medium",
            execution=ExecutionPolicy(
                allow_training_confirm=True,
                allow_race=False,
                allow_event_choice=False,
                stop_on_paid_resource_prompt=True,
                stop_on_unknown_screen=True,
            ),
            skill_timing=SkillTiming(),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UGParentGoal":
        from core.auto_trainer.race_retry_budget import normalize as normalize_retry
        from core.auto_trainer.larc_custom_race_catalog import (
            normalize_custom_races,
            normalize_race_running_styles,
            normalize_running_style,
        )
        policy = data.get("larc_policy", {})
        # Only explicitly new unified goals acquire the unified defaults.
        # Old empty/alpha/beta saves keep their exact previous semantics.
        if (data.get('scenario') == 'larc' and isinstance(policy, dict)
                and policy.get('variant') == 'baseline'):
            from core.auto_trainer.larc_baseline import normalize_larc_policy
            policy = normalize_larc_policy(policy)
        return cls(
            schema_version=str(data.get("schema_version", "")),
            scenario=str(data.get("scenario", "")),
            goal=str(data.get("goal", "")),
            total_turns=int(data.get("total_turns", 72)),
            target_rank=str(data.get("target_rank", "")),
            target_stats={
                key: int(value) for key, value in data.get("target_stats", {}).items()
            },
            factor_priority=list(data.get("factor_priority", [])),
            skill_priority=list(data.get("skill_priority", [])),
            risk_tolerance=str(data.get("risk_tolerance", "medium")),
            execution=ExecutionPolicy.from_dict(data.get("execution")),
            skill_timing=SkillTiming.from_dict(data.get("skill_timing")),
            training_failure_rate_limit=data.get("training_failure_rate_limit"),
            event_preferences=dict(data.get("event_preferences") or {}),
            larc_policy=policy,
            race_retry=normalize_retry(data.get("race_retry")),
            running_style=normalize_running_style(data.get("running_style")),
            race_running_styles=normalize_race_running_styles(data.get("race_running_styles")),
            custom_races=normalize_custom_races(data.get("custom_races")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SUPPORT_TYPE_ALIASES = {
    "speed": "speed",
    "stamina": "stamina",
    "power": "power",
    "guts": "guts",
    "wisdom": "wisdom",
    "intelligence": "wisdom",
    "friend": "friend",
    "group": "group",
    "速度": "speed",
    "耐力": "stamina",
    "力量": "power",
    "根性": "guts",
    "毅力": "guts",
    "智力": "wisdom",
    "友人": "friend",
    "团队": "group",
}


def copy_goal_runtime_metadata(src: Any, dst: UGParentGoal) -> UGParentGoal:
    deck_types = getattr(src, "_support_deck_types", None)
    if deck_types:
        object.__setattr__(dst, "_support_deck_types", tuple(deck_types))
    return dst


def replace_goal(goal: UGParentGoal, **changes: Any) -> UGParentGoal:
    return copy_goal_runtime_metadata(goal, replace(goal, **changes))


def _load_support_deck_types(goal_path: str | Path) -> tuple[str, ...]:
    prof_path = Path(goal_path).resolve().parent / "session" / "support_card_profile.json"
    if not prof_path.is_file():
        return ()
    try:
        prof = json.loads(prof_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ()
    if not isinstance(prof, dict):
        return ()
    cards = prof.get("cards")
    if not (isinstance(cards, list) and cards):
        obs = prof.get("observed_profile")
        decl = prof.get("declared_profile")
        cards = (obs.get("cards") if isinstance(obs, dict) and obs.get("cards")
                 else decl.get("cards") if isinstance(decl, dict) and decl.get("cards")
                 else [])
    if not isinstance(cards, list):
        return ()
    types: list[str] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        raw_type = card.get("type")
        if not isinstance(raw_type, str):
            continue
        norm = _SUPPORT_TYPE_ALIASES.get(raw_type.strip().lower()) or _SUPPORT_TYPE_ALIASES.get(raw_type.strip())
        if norm:
            types.append(norm)
    return tuple(types)


def load_goal_config(path: str | Path) -> UGParentGoal:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("goal_config_must_be_object")
    goal = UGParentGoal.from_dict(data)
    validate_goal_config(goal)
    deck_types = _load_support_deck_types(path)
    if deck_types:
        object.__setattr__(goal, "_support_deck_types", deck_types)
    return goal


def validate_goal_config(goal: UGParentGoal) -> None:
    from core.auto_trainer.larc_policy import validate_policy
    validate_policy(goal.larc_policy)
    errors: list[str] = []
    if goal.schema_version != REQUIRED_SCHEMA_VERSION:
        errors.append("schema_version")
    if goal.scenario not in SUPPORTED_SCENARIOS:
        errors.append("scenario")
    if goal.goal not in SUPPORTED_GOALS:
        errors.append("goal")
    if (
        isinstance(goal.total_turns, bool)
        or not isinstance(goal.total_turns, int)
        or goal.total_turns < 1
    ):
        errors.append("total_turns")
    if not goal.target_rank:
        errors.append("target_rank")
    missing_stats = [key for key in STAT_KEYS if key not in goal.target_stats]
    if missing_stats:
        errors.append(f"missing_target_stats:{','.join(missing_stats)}")
    invalid_stats = [
        key
        for key, value in goal.target_stats.items()
        if key not in STAT_KEYS or not isinstance(value, int) or value < 0
    ]
    if invalid_stats:
        errors.append(f"invalid_target_stats:{','.join(invalid_stats)}")
    if goal.risk_tolerance not in SUPPORTED_RISK_TOLERANCE:
        errors.append("risk_tolerance")
    limit = goal.training_failure_rate_limit
    if limit is not None and (type(limit) not in {int, float}
                              or not math.isfinite(limit) or not 0 <= limit <= 100):
        errors.append("training_failure_rate_limit_must_be_percent_0_to_100")
    if goal.execution.allow_event_choice:
        errors.append("event_choice_execution_not_supported_in_mvp")
    if goal.skill_timing.mode not in SUPPORTED_SKILL_TIMING_MODES:
        errors.append("skill_timing.mode")
    from core.auto_trainer.larc_custom_race_catalog import (
        normalize_custom_races,
        normalize_race_running_styles,
        normalize_running_style,
    )
    if goal.running_style is not None:
        try:
            normalize_running_style(goal.running_style)
        except ValueError:
            errors.append("running_style")
    if goal.race_running_styles:
        try:
            normalize_race_running_styles(goal.race_running_styles)
        except ValueError:
            errors.append("race_running_styles")
    if goal.custom_races:
        try:
            normalize_custom_races(goal.custom_races)
        except ValueError:
            errors.append("custom_races")
    if errors:
        raise ValueError(";".join(errors))


def validate_live_execution_gate(
    *,
    goal: UGParentGoal,
    mode: str,
    execute_training_confirm: bool,
    confirm_token: str | None,
    max_turns: int | None,
) -> tuple[bool, str]:
    validate_goal_config(goal)
    if mode == "dry-run":
        return True, "dry_run"
    if mode != "live":
        return False, "mode"
    if not goal.execution.allow_training_confirm:
        return False, "goal_disallows_training_confirm"
    if not execute_training_confirm:
        return False, "execute_training_confirm"
    if confirm_token != REQUIRED_LIVE_CONFIRM_TOKEN:
        return False, "confirm_token"
    if max_turns is None or max_turns <= 0:
        return False, "max_turns"
    return True, "live_gate_ok"
