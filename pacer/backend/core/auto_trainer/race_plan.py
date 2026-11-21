"""Versioned user race-plan directives with deterministic conflict resolution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping


_RACE_ID_RE = re.compile(r"race\.[a-z0-9][a-z0-9._-]*\Z")
_PLAN_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "plan_id",
        "reserve_opportunities",
        "directives",
    }
)
_PLAN_ALLOWED_KEYS = _PLAN_REQUIRED_KEYS | {"allow_goal_failure"}
_DIRECTIVE_KEYS = frozenset(
    {
        "race_id",
        "directive",
        "priority",
        "source",
        "enabled",
        "outcome_requirement",
    }
)
_DIRECTIVE_VALUES = frozenset({"must_run", "prefer_run", "avoid_run"})
_SOURCE_VALUES = frozenset(
    {"user", "local_policy", "scenario_provider", "ai_shadow"}
)
_OUTCOME_KEYS = frozenset({"rank_at_most", "must_win"})
_DIRECTIVE_RANK = {
    "must_run": 3,
    "avoid_run": 2,
    "prefer_run": 1,
}


class RacePlanError(ValueError):
    """Stable validation failure for race-plan data."""


@dataclass(frozen=True)
class RacePlanDirective:
    race_id: str
    directive: Literal["must_run", "prefer_run", "avoid_run"]
    priority: int
    source: Literal["user", "local_policy", "scenario_provider", "ai_shadow"]
    enabled: bool
    outcome_requirement: Mapping[str, object] | None

    @property
    def executable(self) -> bool:
        return self.enabled and self.source != "ai_shadow"


@dataclass(frozen=True)
class ResolvedRaceDirective:
    race_id: str
    directive: Literal["must_run", "prefer_run", "avoid_run", "none"]
    authority: str
    executable: bool
    selected_directive: RacePlanDirective | None
    shadow_directives: tuple[RacePlanDirective, ...]


@dataclass(frozen=True)
class RacePlan:
    schema_version: str
    plan_id: str
    reserve_opportunities: int
    allow_goal_failure: bool
    directives: tuple[RacePlanDirective, ...]

    @classmethod
    def load(cls, path: str | Path) -> "RacePlan":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RacePlanError("plan_file_invalid") from exc
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: object) -> "RacePlan":
        if type(payload) is not dict:
            raise RacePlanError("plan_root_not_object")
        keys = frozenset(payload)
        if not _PLAN_REQUIRED_KEYS <= keys or not keys <= _PLAN_ALLOWED_KEYS:
            raise RacePlanError("plan_keys_invalid")
        if payload.get("schema_version") != "race_plan.v1":
            raise RacePlanError("plan_schema_version_invalid")
        plan_id = _required_text(payload.get("plan_id"), "plan_id_invalid")
        reserve_opportunities = payload.get("reserve_opportunities")
        if (
            type(reserve_opportunities) is not int
            or reserve_opportunities < 0
        ):
            raise RacePlanError("reserve_opportunities_invalid")
        allow_goal_failure = payload.get("allow_goal_failure", False)
        if type(allow_goal_failure) is not bool:
            raise RacePlanError("allow_goal_failure_invalid")
        rows = payload.get("directives")
        if type(rows) is not list:
            raise RacePlanError("directives_not_array")
        directives = tuple(_parse_directive(row) for row in rows)
        identities = [
            (item.race_id, item.directive, item.source) for item in directives
        ]
        if len(identities) != len(set(identities)):
            raise RacePlanError("duplicate_directive")
        return cls(
            schema_version="race_plan.v1",
            plan_id=plan_id,
            reserve_opportunities=reserve_opportunities,
            allow_goal_failure=allow_goal_failure,
            directives=directives,
        )

    def resolve(
        self,
        race_id: str,
        *,
        hard_goal: bool,
        scenario_mandatory: bool,
    ) -> ResolvedRaceDirective:
        if type(race_id) is not str or _RACE_ID_RE.fullmatch(race_id) is None:
            raise RacePlanError("race_id_invalid")
        if type(hard_goal) is not bool or type(scenario_mandatory) is not bool:
            raise RacePlanError("resolution_flag_invalid")
        matching = tuple(item for item in self.directives if item.race_id == race_id)
        shadow = tuple(
            item for item in matching if item.enabled and item.source == "ai_shadow"
        )
        if hard_goal:
            return ResolvedRaceDirective(
                race_id=race_id,
                directive="must_run",
                authority="career_hard_goal",
                executable=True,
                selected_directive=None,
                shadow_directives=shadow,
            )
        if scenario_mandatory:
            return ResolvedRaceDirective(
                race_id=race_id,
                directive="must_run",
                authority="scenario_mandatory",
                executable=True,
                selected_directive=None,
                shadow_directives=shadow,
            )
        executable = [item for item in matching if item.executable]
        if not executable:
            return ResolvedRaceDirective(
                race_id=race_id,
                directive="none",
                authority="no_executable_directive",
                executable=False,
                selected_directive=None,
                shadow_directives=shadow,
            )
        selected = sorted(
            executable,
            key=lambda item: (
                -_DIRECTIVE_RANK[item.directive],
                -item.priority,
                item.source,
            ),
        )[0]
        authority = (
            f"{selected.source}_{selected.directive.removesuffix('_run')}"
        )
        return ResolvedRaceDirective(
            race_id=race_id,
            directive=selected.directive,
            authority=authority,
            executable=True,
            selected_directive=selected,
            shadow_directives=shadow,
        )


def _parse_directive(row: object) -> RacePlanDirective:
    if type(row) is not dict or frozenset(row) != _DIRECTIVE_KEYS:
        raise RacePlanError("directive_keys_invalid")
    race_id = _required_text(row.get("race_id"), "race_id_invalid")
    if _RACE_ID_RE.fullmatch(race_id) is None:
        raise RacePlanError("race_id_invalid")
    directive = row.get("directive")
    if type(directive) is not str or directive not in _DIRECTIVE_VALUES:
        raise RacePlanError("directive_value_invalid")
    priority = row.get("priority")
    if type(priority) is not int or not 0 <= priority <= 1_000_000:
        raise RacePlanError("priority_invalid")
    source = row.get("source")
    if type(source) is not str or source not in _SOURCE_VALUES:
        raise RacePlanError("directive_source_invalid")
    enabled = row.get("enabled")
    if type(enabled) is not bool:
        raise RacePlanError("directive_enabled_invalid")
    outcome_requirement = _parse_outcome_requirement(
        row.get("outcome_requirement")
    )
    return RacePlanDirective(
        race_id=race_id,
        directive=directive,
        priority=priority,
        source=source,
        enabled=enabled,
        outcome_requirement=outcome_requirement,
    )


def _parse_outcome_requirement(
    value: object,
) -> Mapping[str, object] | None:
    if value is None:
        return None
    if (
        type(value) is not dict
        or not value
        or not frozenset(value) <= _OUTCOME_KEYS
    ):
        raise RacePlanError("outcome_requirement_invalid")
    rank_at_most = value.get("rank_at_most")
    if rank_at_most is not None and (
        type(rank_at_most) is not int or not 1 <= rank_at_most <= 18
    ):
        raise RacePlanError("outcome_requirement_invalid")
    must_win = value.get("must_win")
    if must_win is not None and type(must_win) is not bool:
        raise RacePlanError("outcome_requirement_invalid")
    return MappingProxyType(dict(value))


def _required_text(value: object, reason: str) -> str:
    if type(value) is not str or not value.strip():
        raise RacePlanError(reason)
    return value
