from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.auto_trainer.config import UGParentGoal


class RunLogger:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)

    def _write_json(self, path: Path, data: dict[str, Any] | list[Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(',', ':')),
            encoding="utf-8",
        )

    def write_run_config(self, goal: UGParentGoal, mode: str) -> None:
        self._write_json(
            self.run_dir / "run_config.json",
            {
                "schema_version": "ug_parent_auto_run_config.v1",
                "mode": mode,
                "goal": goal.to_dict(),
            },
        )

    def write_turn(
        self,
        turn_index: int,
        state: dict[str, Any],
        candidates: list[dict[str, Any]],
        decision: dict[str, Any],
        execution: dict[str, Any],
        ai_review: dict[str, Any] | None = None,
    ) -> None:
        turn_dir = self.run_dir / "turns" / f"turn_{turn_index:03d}"
        self._write_json(turn_dir / "state.json", state)
        self._write_json(turn_dir / "candidates.json", candidates)
        self._write_json(turn_dir / "decision.json", decision)
        self._write_json(turn_dir / "execution.json", execution)
        if ai_review is not None:
            self._write_json(turn_dir / "ai_review.json", ai_review)

    def write_summary(self, summary: dict[str, Any]) -> None:
        self._write_json(self.run_dir / "run_summary.json", summary)
