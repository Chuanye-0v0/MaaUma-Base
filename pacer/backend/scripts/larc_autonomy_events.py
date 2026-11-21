"""Persist natural event observations from an already saved L'Arc route report."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from core.event_progress import (
    adapt_report_observations,
    load_progress,
    record_event_observation,
    save_progress,
    summarize_event_progress,
)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _check_run(report: Mapping[str, Any], run_id: str) -> None:
    transitions = [report.get("route_transition") or {}]
    transitions.extend((row.get("result") or {}).get("route_transition") or {}
                       for row in report.get("step_reports", []))
    if any(item.get("run_session_id") not in (None, run_id) for item in transitions):
        raise ValueError("event_report_run_mismatch")


def observe_event_report(ctx: Any, report: Mapping[str, Any], report_path: str | Path,
                         action_turn: int, phase: str) -> dict[str, Any]:
    """Update per-run event files; return a note instead of interrupting play."""
    try:
        root = Path(ctx.root)
    except (AttributeError, TypeError):
        return {"status": "error", "observations": 0, "new_observations": 0,
                "note": "event_progress:ValueError:context_root_invalid",
                "progress_path": None, "summary_path": None}
    progress_path = root / "session/event_progress.json"
    summary_path = root / "session/event_progress_summary.json"
    report_path = Path(report_path)
    result = {"status": "unchanged", "observations": 0, "new_observations": 0,
              "note": None, "progress_path": str(progress_path), "summary_path": str(summary_path)}
    try:
        if type(action_turn) is not int or action_turn < 0 or not isinstance(phase, str) or not phase:
            raise ValueError("event_report_turn_or_phase_invalid")
        path_turn = re.search(r"(?:^|[\\/])turn_(\d+)(?:[\\/]|$)", str(report_path))
        if path_turn and int(path_turn[1]) != action_turn:
            raise ValueError("event_report_action_turn_mismatch")
        _check_run(report, ctx.run_id)
        state = load_progress(progress_path, ctx.run_id)
        observations = adapt_report_observations(
            report, report_path, run_session_id=ctx.run_id, action_turn=action_turn,
            trainee_id=(getattr(ctx, "trainee_id", None)
                        or (getattr(ctx, "progress", None) or {}).get("trainee_id")),
        )
        result["observations"] = len(observations)
        before = len(state["seen_observation_ids"])
        for observation in observations:
            observation["phase"] = phase
            state = record_event_observation(state, observation)
        result["new_observations"] = len(state["seen_observation_ids"]) - before
        if result["new_observations"] or not progress_path.exists():
            save_progress(progress_path, state)
        if result["new_observations"] or not summary_path.exists():
            _write_json(summary_path, summarize_event_progress(state))
        result["status"] = "updated" if result["new_observations"] else "unchanged"
        # Enrich the next decision from the run's saved deck/history, without
        # capture, OCR, network access, or treating reference rewards as earned.
        support_path = root / "session/support_chain_context.json"
        if (root / "session/support_card_profile.json").is_file() and (result["new_observations"] or not support_path.exists()):
            try:
                from core.support_card_knowledge import prepare_pacer_support_context
                knowledge = prepare_pacer_support_context(root / "session", run_id=ctx.run_id, through_turn=action_turn)
                _write_json(support_path, knowledge)
                result["support_chain_context"] = {"status": "updated", "path": str(support_path)}
            except (OSError, ValueError, TypeError, KeyError) as exc:
                result["support_chain_context"] = {"status": "unavailable", "reason": str(exc)}
    except Exception as error:
        note = f"event_progress:{type(error).__name__}:{error}"
        result.update(status="error", note=note)
        try:
            note_path = root / "session/event_progress_notes.jsonl"
            note_path.parent.mkdir(parents=True, exist_ok=True)
            with note_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps({"run_id": getattr(ctx, "run_id", None),
                                         "report_path": str(report_path), "action_turn": action_turn,
                                         "phase": phase, "note": note}, ensure_ascii=False) + "\n")
        except OSError:
            pass
    return result
