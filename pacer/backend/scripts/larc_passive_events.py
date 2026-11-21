"""Bounded title-only background OCR; gameplay never waits for each OCR result."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor,wait
import json
from pathlib import Path
import cv2
import numpy as np


class PassiveEventJournal:
    def __init__(self, root, *, reader=None, executor=None):
        self.root = Path(root) / "passive_events"
        self.root.mkdir(parents=True, exist_ok=True)
        self.reader = reader
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="event-title")
        self.pending = []
        self.last_signature = None
        self.sequence = len(list(self.root.glob("title_*.png")))
        self.seen = set()
        self.dropped = 0
        self.last_error = None

    def offer(self, crop, action_turn):
        signature = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (96, 24)).astype(np.float32)
        if self.last_signature is not None:
            turn, previous = self.last_signature
            if turn == action_turn and float(np.mean(np.abs(signature - previous))) < 7:
                return
        self.last_signature = (action_turn, signature)
        if len(self.pending) >= 8:
            self.dropped += 1
            return
        self.sequence += 1
        path = self.root / f"title_{self.sequence:04d}.png"
        # Small title crop only; no full-frame recording or network dependency.
        ok, encoded = cv2.imencode(".png", crop)
        if not ok:
            self.dropped += 1
            return
        try:
            path.write_bytes(encoded.tobytes())
            self.pending.append((self.executor.submit(self._read, crop), action_turn, path))
        except (OSError, RuntimeError) as exc:
            self.dropped += 1
            self.last_error = type(exc).__name__

    def _read(self, crop):
        if self.reader is None:
            from core.auto_trainer.frame_safety import LocalRapidOCRScreenTextReader, _join_high_confidence_rapidocr_text
            reader = LocalRapidOCRScreenTextReader()
            self.reader = lambda image: _join_high_confidence_rapidocr_text(reader.read_regions(image))
        return self.reader(crop)

    def settle(self, timeout=1.0):
        """Bounded wait for already-captured titles; never captures a new frame."""
        futures=[future for future,_,_ in self.pending if not future.done()]
        if futures:wait(futures,timeout=timeout)
        return all(future.done() for future,_,_ in self.pending)

    def drain(self, *, include_health=False):
        completed, remaining = [], []
        for future, turn, path in self.pending:
            if not future.done():
                remaining.append((future, turn, path))
                continue
            try:
                text = future.result()
                error = None
            except Exception as exc:
                text, error = "", type(exc).__name__
            key = (turn, "".join(str(text).split()))
            status = "observed" if text else "unknown"
            if text and key in self.seen:
                status = "duplicate"
            self.seen.add(key)
            report = {"status": status, "action_turn": turn, "source": str(path),
                      "sample_sequence":int(path.stem.rsplit('_',1)[-1]),
                      "error": error, "scope": "title_only_not_reward_confirmation",
                      "step_reports": [{"step": 1, "screenshot_path": str(path), "result": {
                          "performed": False, "frame_safety": {"text_status": "ok" if text else "unknown",
                          "visible_text": text}, "decision": {"action": "observe", "reason": "passive_event_title"}}}]}
            report_path = path.with_suffix(".json")
            try:
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                self.dropped += 1
                self.last_error = type(exc).__name__
                continue
            if status == "observed" or (include_health and (status=='duplicate' or error is not None)):
                completed.append((report, report_path, turn))
        self.pending = remaining
        return completed

    def close(self):
        # Only flush at run exit, never between turns. Queue is bounded to eight.
        self.executor.shutdown(wait=True)
