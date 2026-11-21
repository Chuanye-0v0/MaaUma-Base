"""Read the visible supporter-point shortfall from an already observed main menu."""
from __future__ import annotations

import re


def read_larc_supporter_goal(text, *, remaining_turns, source_path, source_sha256,
                             run_session_id, turn_index):
    text = "".join(str(text).split())
    target = re.search(r"获得([\d,，]+)点协助者积分", text)
    # The nearby blue Pt icon can be joined to the numeric word as P/Pt.
    # Keep both textual anchors; do not strip arbitrary non-numeric text.
    gap = re.search(r"达成条件还需(?:[Pp][Tt]?)?([\d,，]+)积分", text)
    reversed_gap = re.search(r"([\d,，]+)积分达成条件还需", text)
    if gap is not None and reversed_gap is not None:
        return None  # Values on both sides are ambiguous; do not choose one.
    gap = gap if gap is not None else reversed_gap
    current = re.search(r"协助者积分([\d,，]+)(?:pt|积分)", text, re.IGNORECASE)
    if (target is None or gap is None or type(remaining_turns) is not int
            or remaining_turns < 0 or not source_path or not source_sha256):
        return None

    def number(match):
        return int(match.group(1).replace(",", "").replace("，", ""))

    return {"schema_version": "larc_supporter_goal.v1", "status": "accepted",
            "target_points": number(target), "remaining_points": number(gap),
            "current_points": number(current) if current else None,
            "remaining_turns": remaining_turns,
            "source": {"path": str(source_path), "sha256": source_sha256,
                       "run_session_id": run_session_id, "turn_index": turn_index}}


def urgent_larc_supporter_goal(goal):
    return (isinstance(goal, dict) and goal.get("status") == "accepted"
            and goal.get("schema_version") == "larc_supporter_goal.v1"
            and type(goal.get("remaining_points")) is int and goal["remaining_points"] > 0
            and type(goal.get("remaining_turns")) is int and goal["remaining_turns"] in {1, 2})


def deadline_larc_supporter_goal(goal):
    return urgent_larc_supporter_goal(goal) and goal["remaining_turns"] == 1
