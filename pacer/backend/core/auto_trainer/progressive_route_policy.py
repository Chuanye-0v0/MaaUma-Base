from __future__ import annotations

from typing import Mapping

from core.auto_trainer.grand_masters_scenario import (
    GRAND_MASTERS_ACTION_IDS,
    PlayerScenarioPolicy,
)
from core.auto_trainer.progressive_route_session import ProgressiveRouteError


_HOLD_ACTION_ID = GRAND_MASTERS_ACTION_IDS[0]
_GODDESS_ACTION_IDS = GRAND_MASTERS_ACTION_IDS[1:]


def choose_hold(packet: dict[str, object]) -> str:
    _allowed_actions(packet)
    return _HOLD_ACTION_ID


def choose_first_usable_goddess(packet: dict[str, object]) -> str:
    allowed = _allowed_actions(packet)
    critical = packet.get("critical_unknown_fields")
    if not isinstance(critical, (tuple, list)) or critical:
        return _HOLD_ACTION_ID
    for action_id in _GODDESS_ACTION_IDS:
        if action_id in allowed:
            return action_id
    return _HOLD_ACTION_ID


PLAYER_SCENARIO_POLICIES: dict[str, PlayerScenarioPolicy] = {
    "hold": choose_hold,
    "first_usable_goddess": choose_first_usable_goddess,
}


def build_player_scenario_policy(policy_id: str) -> PlayerScenarioPolicy:
    if type(policy_id) is not str or policy_id not in PLAYER_SCENARIO_POLICIES:
        raise ProgressiveRouteError("scenario policy_id is not allowlisted")
    return PLAYER_SCENARIO_POLICIES[policy_id]


def _allowed_actions(packet: object) -> tuple[str, ...]:
    if not isinstance(packet, Mapping):
        raise ProgressiveRouteError("scenario policy packet must be an object")
    raw = packet.get("allowed_action_ids")
    if not isinstance(raw, (tuple, list)) or any(
        type(action_id) is not str or not action_id for action_id in raw
    ):
        raise ProgressiveRouteError("scenario policy packet allowlist is invalid")
    allowed = tuple(raw)
    if len(set(allowed)) != len(allowed) or _HOLD_ACTION_ID not in allowed:
        raise ProgressiveRouteError("scenario policy packet allowlist is invalid")
    return allowed
