"""Nonblocking per-host display state for Pacer realtime luck and daily extrema."""

from __future__ import annotations

from datetime import datetime
from itertools import islice
import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import tempfile
from threading import Thread
from typing import Any, Callable

from .pacer_luck_records import read_current_review
from .pacer_realtime_luck import summarize_realtime_luck

DAILY_STATE_FILE = "_realtime_luck_daily.json"
DAILY_SCHEMA = "uma.pacer_realtime_luck_daily.v2"
MIN_FORMAL_DATES = 3
_LIVE_TURN_FILES = (
    "decision/turns/turn_000/state.json",
    "capture/ss_comparison_snapshot.json",
    "capture/training_options.generated.json",
    "action_receipt.json",
)


def _default_today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _live_turn_stamp(run_path: Path, next_turn: Any) -> tuple[Any, ...]:
    """O(1) current-turn file stamp so Turn 1 & mid-turn captures invalidate marker immediately."""
    if type(next_turn) is not int or next_turn < 1:
        return ()
    try:
        newest_seg: Path | None = None
        for item in islice(run_path.iterdir(), 64):
            if item.name.startswith("autonomous_") and item.is_dir():
                if newest_seg is None or item.name > newest_seg.name:
                    newest_seg = item
        if newest_seg is None:
            return ()
        prefix = f"turn_{next_turn:03d}"
        candidates: list[Path] = []
        exact = newest_seg / prefix
        if exact.is_dir():
            candidates.append(exact)
        for item in islice(newest_seg.iterdir(), 96):
            if item.name != prefix and item.name.startswith(prefix + "_") and item.is_dir():
                candidates.append(item)
        if not candidates:
            return (newest_seg.name,)
        latest_dir = max(candidates, key=lambda d: (0 if d.name == prefix else 1, d.name))
        mtimes = []
        for rel in _LIVE_TURN_FILES:
            target = latest_dir / rel
            try:
                st = target.stat()
                mtimes.append((rel, st.st_mtime_ns, st.st_size))
            except OSError:
                continue
        return (newest_seg.name, latest_dir.name, tuple(mtimes))
    except OSError:
        return ()


class RealtimeLuckSession:
    def __init__(
        self,
        runs_root,
        *,
        reader=read_current_review,
        scorer=summarize_realtime_luck,
        today_fn: Callable[[], str] = _default_today,
    ):
        self.runs_root = Path(runs_root).resolve()
        self.state_path = self.runs_root / DAILY_STATE_FILE
        self.reader = reader
        self.scorer = scorer
        self.today_fn = today_fn
        self.identity: tuple[Path, str, str] | None = None
        self.generation = 0
        self.marker = None
        self.scheduled = None
        self.result: dict[str, Any] | None = None
        self.worker: Thread | None = None
        self.results: SimpleQueue = SimpleQueue()
        self.cache: dict[str, Any] = {}
        self.daily_date: str = self.today_fn()
        self.daily_runs: dict[str, dict[str, Any]] = {}
        self.last_recorded: dict[str, Any] | None = None
        self._load_persisted()

    def _load_persisted(self) -> None:
        try:
            if not self.state_path.is_file():
                return
            raw = self.state_path.read_text(encoding="utf-8-sig")
            if len(raw) > 512 * 1024:
                return
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("schema_version") != DAILY_SCHEMA:
                return
            last = data.get("last_recorded")
            if (
                isinstance(last, dict)
                and last.get("status") == "recorded"
                and type(last.get("score")) in (int, float)
                and type(last.get("scored_dates")) is int
                and last["scored_dates"] > 0
            ):
                self.last_recorded = last
            today = self.today_fn()
            if data.get("date") == today and isinstance(data.get("runs"), dict):
                self.daily_date = today
                cleaned: dict[str, dict[str, Any]] = {}
                for run_id, entry in list(data["runs"].items())[:256]:
                    if (
                        isinstance(run_id, str)
                        and isinstance(entry, dict)
                        and type(entry.get("score")) in (int, float)
                        and type(entry.get("scored_dates")) is int
                        and entry["scored_dates"] > 0
                    ):
                        cleaned[run_id] = {
                            "run_id": run_id,
                            "score": round(float(entry["score"]), 1),
                            "scored_dates": int(entry["scored_dates"]),
                        }
                self.daily_runs = cleaned
        except (OSError, ValueError, TypeError):
            return

    def _persist(self) -> None:
        try:
            self.runs_root.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": DAILY_SCHEMA,
                "date": self.daily_date,
                "runs": self.daily_runs,
                "last_recorded": self.last_recorded,
            }
            fd, tmp_name = tempfile.mkstemp(
                prefix=".luck_daily_", suffix=".tmp", dir=str(self.runs_root)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(tmp_name, self.state_path)
            finally:
                if os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
        except OSError:
            pass

    def _ensure_today(self) -> None:
        today = self.today_fn()
        if today != self.daily_date:
            self.daily_date = today
            self.daily_runs = {}
            self._persist()

    def _record_scored_run(self, run_id: str, result: dict[str, Any]) -> None:
        if (
            not isinstance(run_id, str)
            or not run_id
            or result.get("status") != "recorded"
            or type(result.get("score")) not in (int, float)
            or type(result.get("scored_dates")) is not int
            or result["scored_dates"] <= 0
        ):
            return
        self._ensure_today()
        score = round(float(result["score"]), 1)
        dates = int(result["scored_dates"])
        self.last_recorded = {**result, "run_id": run_id}
        if len(self.daily_runs) >= 256 and run_id not in self.daily_runs:
            oldest_key = next(iter(self.daily_runs))
            self.daily_runs.pop(oldest_key, None)
        self.daily_runs[run_id] = {
            "run_id": run_id,
            "score": score,
            "scored_dates": dates,
        }
        self._persist()

    def _daily_extrema(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        self._ensure_today()
        valid = [
            r for r in self.daily_runs.values()
            if type(r.get("score")) in (int, float)
            and type(r.get("scored_dates")) is int
            and r["scored_dates"] > 0
        ]
        if not valid:
            return None, None
        formal = [r for r in valid if r["scored_dates"] >= MIN_FORMAL_DATES]
        pool = formal if formal else valid
        if len(pool) == 1:
            only = pool[0]
            item = {
                "score": round(float(only["score"]), 1),
                "scored_dates": int(only["scored_dates"]),
                "run_id": str(only["run_id"]),
            }
            if item["score"] >= 0.0:
                return item, None
            return None, item

        best = max(pool, key=lambda r: (float(r["score"]), int(r["scored_dates"]), str(r["run_id"])))
        worst = min(pool, key=lambda r: (float(r["score"]), -int(r["scored_dates"]), str(r["run_id"])))
        return (
            {
                "score": round(float(best["score"]), 1),
                "scored_dates": int(best["scored_dates"]),
                "run_id": str(best["run_id"]),
            },
            {
                "score": round(float(worst["score"]), 1),
                "scored_dates": int(worst["scored_dates"]),
                "run_id": str(worst["run_id"]),
            },
        )

    def _current_identity(self, run_root, record, mode):
        if not isinstance(record, dict):
            return None
        run_id = record.get("run_session_id")
        if not run_root or not isinstance(run_id, str) or mode not in ("career", "cut"):
            return None
        try:
            path = Path(run_root).resolve()
        except OSError:
            return None
        if path.name != run_id or path == self.runs_root or not path.is_relative_to(self.runs_root):
            return None
        return path, run_id, mode

    def _switch_identity(self, identity):
        self.identity = identity
        self.generation += 1
        self.marker = self.scheduled = self.result = None
        self.cache = {}

    def _collect_results(self):
        while True:
            try:
                generation, run_id, result = self.results.get_nowait()
            except Empty:
                return
            if isinstance(result, dict):
                self._record_scored_run(run_id, result)
            if generation == self.generation:
                self.result = result

    def _score(self, generation: int, path: Path, run_id: str, cache: dict[str, Any]):
        try:
            result = self.scorer(self.reader(path, cache=cache))
        except Exception:
            result = {"status": "unavailable"}
        self.results.put((generation, run_id, result))

    def _schedule(self):
        key = self.generation, self.marker
        if self.marker is None or key == self.scheduled or (self.worker and self.worker.is_alive()):
            return
        self.scheduled = key
        self.worker = Thread(
            target=self._score,
            args=(self.generation, self.identity[0], self.identity[1], self.cache),
            daemon=True,
        )
        self.worker.start()

    def snapshot(self, run_root, record, mode, *, live: bool | None = None) -> dict[str, Any]:
        record = record if isinstance(record, dict) else {}
        identity = self._current_identity(run_root, record, mode)
        if identity != self.identity:
            self._switch_identity(identity)
        self._collect_results()
        if identity is not None:
            latest = record.get("last_turn") or {}
            next_turn = record.get("next_turn")
            if isinstance(latest, dict) and (latest or next_turn):
                self.marker = (
                    latest.get("turn_index"),
                    latest.get("output"),
                    next_turn,
                    record.get("actions_completed"),
                    record.get("summary_status"),
                    _live_turn_stamp(identity[0], next_turn),
                )
            self._schedule()
            if self.worker is not None and self.worker.is_alive() and (
                self.result is None
                or self.result.get("status") != "recorded"
                or live is False
            ):
                self.worker.join(timeout=0.08)
            self._collect_results()

        daily_best, daily_worst = self._daily_extrema()
        extrema = {"daily_best": daily_best, "daily_worst": daily_worst}

        if identity is not None and isinstance(self.result, dict) and self.result.get("status") == "recorded":
            finished = (live is False) and (
                record.get("summary_status") in ("completed", "career_complete")
                or self.result.get("career_complete") is True
            )
            scope = "current" if live is not False else ("finished" if finished else "last_run")
            return {
                **self.result,
                "run_id": identity[1],
                "display_scope": scope,
                **extrema,
            }

        if live is True and identity is not None:
            cur_status = (self.result or {}).get("status", "waiting_for_current")
            return {
                "status": cur_status,
                "run_id": identity[1],
                "display_scope": "current",
                **extrema,
            }

        if self.last_recorded is not None:
            scope = "finished" if self.last_recorded.get("career_complete") is True else "last_run"
            return {
                **self.last_recorded,
                "display_scope": scope,
                **extrema,
            }

        return {
            "status": "waiting_run",
            "display_scope": "empty",
            **extrema,
        }
