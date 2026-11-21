"""Run-local Mei outing ledger. A tap is pending until fresh progress confirms it."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
import os
import re
from pathlib import Path
import tempfile

from core.auto_trainer.larc_friend_outing import CHARACTER, SCHEMA, verified_source


@lru_cache(maxsize=1)
def _mei_unlock_event():
    from core.event_knowledge_lookup import REVIEWED_PATHS, load_reviewed_events
    return next(event for event in load_reviewed_events([REVIEWED_PATHS[0]])
                if event["event_id"] == "jp.single_mode_story:809043004")


def _compact(text):
    from core.event_knowledge_lookup import normalize_text
    return "".join(normalize_text(str(text)).split())


class MeiOutingSession:
    def __init__(self, run_id, data=None):
        self.data = data or {"schema_version": "larc_outing_session.v1", "run_session_id": run_id,
            "unlocked": False, "unlock_source": None, "completed": None,
            "source": None, "pending": None, "receipts": []}
        if (self.data.get("schema_version") != "larc_outing_session.v1"
                or self.data.get("run_session_id") != run_id
                or type(self.data.get("unlocked")) is not bool
                or self.data.get("completed") is not None and
                   (type(self.data["completed"]) is not int or not 0 <= self.data["completed"] <= 5)):
            raise ValueError("mei_outing_session_invalid")

    @classmethod
    def load(cls, path, run_id):
        path = Path(path)
        return cls(run_id, json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else cls(run_id)

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def verified_historical_source(self, source, *, run_root, current_turn):
        """The saved run ledger retains observed facts after image release.

        Only its own recorded unlock/progress source is historical. New menu,
        chooser and confirmation inputs still require verified_source pixels.
        """
        if (self.data.get('schema_version')!='larc_outing_session.v1'
                or self.data.get('unlocked') is not True or type(self.data.get('completed')) is not int
                or not 0<=self.data['completed']<=5 or not isinstance(source,dict)
                or source not in (self.data.get('source'),self.data.get('unlock_source'))
                or source.get('run_session_id')!=self.data['run_session_id']
                or type(current_turn) is not int or type(source.get('turn_index')) is not int
                or not 1<=source['turn_index']<=current_turn
                or not isinstance(source.get('sha256'),str) or re.fullmatch(r'[0-9a-f]{64}',source['sha256']) is None):
            return False
        try:
            path=Path(source['path']);root=Path(run_root).resolve()
            return path.is_absolute() and path.resolve().is_relative_to(root)
        except (KeyError,TypeError,OSError,ValueError):
            return False

    def observe_unlock(self, text, source):
        text = "".join(str(text).split()).replace("鳴", "鸣")
        if not any(term in text for term in ("可以和佐岳鸣一起外出了", "可以与佐岳鸣一起外出了")):
            return False
        if not verified_source(source, run_id=self.data["run_session_id"]):
            return False
        if not self.data["unlocked"]:
            self.data.update(unlocked=True, completed=0, unlock_source=deepcopy(source))
        if self.data.get("unlock_event", {}).get("status") in {"awaiting_result","result_unknown"}:
            self.data["unlock_event"].update(status="confirmed_by_result_message", result_source=deepcopy(source))
        return True  # Re-observing the unlock message must never reset used outings.

    def observe_unlock_choice(self, text, action_id, source, *, selection=None):
        """Use an executed choice, not merely a visible unlock-event title.

        The user confirmed option 1's guaranteed unlock on 2026-09-27. The
        local library supplies its identity/options and separate random branch.
        This does not make every reference-library effect a confirmed result.
        """
        if not any(title in str(text) for title in ("有时也要忘记工作", "時には仕事を忘れて")):
            return False
        event = _mei_unlock_event()
        lines = {_compact(line) for line in str(text).splitlines() if line.strip()}
        titles = {_compact(value) for value in event["titles"].values() if isinstance(value, str)}
        if not titles.intersection(lines):
            return False
        index = {"event_choice.top": 1, "event_choice.second": 2,
                 "event_choice.middle": 2, "event_choice.bottom": len(event["options"])}.get(action_id)
        option = next((row for row in event["options"] if row["index"] == index), None)
        if option is None or not any(_compact(value) in lines for value in option["texts"].values()):
            return False
        if not verified_source(source, run_id=self.data["run_session_id"]):
            return False
        previous=self.data.get('unlock_event') or {}
        if previous.get('source')==source and previous.get('selected_option_index')==index:
            return False  # A replay must not turn a settled gamble back into pending.
        fixed = {effect["kind"] for effect in option["effects"]}
        status = ("guaranteed_unlock" if "outing_unlock" in fixed else
                  "unavailable" if "outing_unavailable" in fixed else "awaiting_result")
        self.data["unlock_event"] = {"event_id": event["event_id"], "option_id": option["option_id"],
            "selected_option_index": index, "status": status, "source": deepcopy(source),
            "rule_basis": "local_event_library_and_user_confirmation_2026-09-27",'tap_performed':True}
        from core.auto_trainer.larc_mei_unlock_policy import gamble_selection
        if index==2 and gamble_selection(selection):
            self.data['unlock_event'].update(gamble_armed=True,
                gamble_policy=deepcopy(selection['mei_unlock_policy']),
                gamble_decision=deepcopy(selection['mei_unlock_decision']))
        if status == "guaranteed_unlock" and not self.data["unlocked"]:
            self.data.update(unlocked=True, completed=0, unlock_source=deepcopy(source))
        # Option 2 remains unknown until the result text or actual chooser is
        # observed. Its larger possible recovery is not proof of an unlock.
        return True

    def observe_unlock_result(self, observation, source):
        """Accept an explicit observed unavailability, never absent chooser text.

        Existing success-banner and real chooser confirmation remain sufficient
        for success. Failure requires a fresh, positively identified result;
        reference branch effects or an energy change are not result evidence.
        """
        event=self.data.get('unlock_event') or {}
        choice=event.get('source') or {}
        if (self.data['unlocked'] or event.get('status')!='awaiting_result'
                or event.get('selected_option_index')!=2 or event.get('tap_performed') is not True
                or not isinstance(observation,dict) or observation.get('status')!='accepted'
                or observation.get('result_observed') is not True
                or str(observation.get('story_id'))!='809043004'
                or observation.get('character')!='satake_mei' or observation.get('available') is not False
                or observation.get('basis') not in {'explicit_outing_unavailable_observation',
                    'user_confirmed_energy_loss_and_absent_friend_badge'}
                or not verified_source(source,run_id=self.data['run_session_id'])
                or source['turn_index']<choice.get('turn_index',source['turn_index'])
                or source.get('sha256')==choice.get('sha256')):
            return False
        event.update(status='confirmed_failed',result_source=deepcopy(source),
            result_basis=observation['basis'],result_observation=deepcopy(observation))
        return True

    def observe_chooser(self, observation, source, *, menu_snapshot=None,
                        recover_interrupted_selection=False):
        if (observation.get("status") != "accepted" or observation.get("character") != CHARACTER
                or type(observation.get("completed")) is not int or not 0 <= observation["completed"] <= 5
                or not verified_source(source, run_id=self.data["run_session_id"])):
            raise ValueError("mei_outing_chooser_unverified")
        completed = observation["completed"]
        pending = self.data["pending"]
        if (pending and recover_interrupted_selection
                and pending.get("tap_performed") is not True):
            # A cancelled/failed selection can return home without ever
            # producing a final-confirm receipt. Inspecting the real chooser
            # establishes current progress; it does not replay that input or
            # invent a successful receipt for the interrupted selection.
            if (source["turn_index"] < pending["turn_index"]
                    or source["sha256"] == pending["source"]["sha256"]
                    or not isinstance(menu_snapshot, dict)
                    or not isinstance(menu_snapshot.get("period"), dict)
                    or not menu_snapshot["period"]
                    or completed not in {self.data["completed"], pending["stage"]}):
                raise ValueError("mei_outing_interrupted_selection_unverified")
            self.data.setdefault("reconciliations", []).append({
                "status": "current_chooser_after_interrupted_selection",
                "previous_pending": deepcopy(pending),
                "observed_turn": source["turn_index"], "completed": completed,
                "source": deepcopy(source), "menu_snapshot": deepcopy(menu_snapshot),
                "input_replayed": False})
            self.data["pending"] = None
            # This count came from the current chooser, not from record_tap.
            self.data.update(unlocked=True, completed=completed, source=deepcopy(source))
            return
        if pending:
            if source["turn_index"] <= pending["turn_index"]:
                raise ValueError("mei_outing_result_unconfirmed")
            if source["sha256"] == pending["source"]["sha256"]:
                raise ValueError("mei_outing_result_frame_not_fresh")
            if pending.get("tap_performed") is not True:
                raise ValueError("mei_outing_ambiguous_tap_requires_review")
            if completed != pending["stage"]:
                before = pending.get("before_menu")
                unchanged_position = (isinstance(before, dict) and isinstance(menu_snapshot, dict)
                    and isinstance(before.get("period"), dict) and bool(before["period"])
                    and before["period"] == menu_snapshot.get("period")
                    and type(before.get("remaining_turns")) is int
                    and before["remaining_turns"] == menu_snapshot.get("remaining_turns"))
                if not (completed == self.data["completed"] == pending["stage"] - 1
                        and source["turn_index"] == pending["turn_index"] + 1
                        and unchanged_position):
                    raise ValueError("mei_outing_result_unconfirmed")
                # A transport tap is not a server commitment. After an
                # interruption, the fresh chooser and unchanged calendar can
                # prove that this outing was not applied. Keep the old receipt.
                self.data.setdefault("reconciliations", []).append({
                    "status": "confirmed_not_applied", "action_id": pending["action_id"],
                    "recorded_turn": pending["turn_index"], "observed_turn": source["turn_index"],
                    "completed": completed, "source": deepcopy(source)})
            self.data["pending"] = None
        elif self.data["completed"] is not None and completed != self.data["completed"]:
            raise ValueError("mei_outing_progress_disagrees")
        self.data.update(unlocked=True, completed=completed, source=deepcopy(source))
        if self.data.get("unlock_event", {}).get("status") in {"awaiting_result", "result_unknown"}:
            self.data["unlock_event"].update(status="confirmed_on_chooser", result_source=deepcopy(source))

    def reserve(self, stage, source, *, menu_snapshot=None):
        if (not self.data["unlocked"] or self.data["pending"] is not None
                or type(stage) is not int or stage != self.data["completed"] + 1 or not 1 <= stage <= 5
                or source != self.data["source"] or not verified_source(source, run_id=self.data["run_session_id"])):
            raise ValueError("mei_outing_not_executable")
        self.data["pending"] = {"action_id": f"outing.satake_mei.{stage}", "stage": stage,
            "turn_index": source["turn_index"], "source": deepcopy(source), "tap_performed": None,
            "before_menu": deepcopy(menu_snapshot)}

    def confirm_return(self, menu_snapshot, source):
        pending = self.data["pending"]
        if pending is None:
            return False
        before = pending.get("before_menu")
        if (pending.get("tap_performed") is not True or not isinstance(before, dict)
                or not verified_source(source, run_id=self.data["run_session_id"], turn=pending["turn_index"] + 1)):
            raise ValueError("mei_outing_return_receipt_unverified")
        old_period, new_period = before.get("period", {}), menu_snapshot.get("period", {})
        def calendar(period):
            if (period.get("year_index") in {1, 2, 3} and type(period.get("month")) is int
                    and period.get("half") in {"first", "second"}):
                return period["year_index"] * 24 + (period["month"] - 1) * 2 + (period["half"] == "second")
            return None
        old_day, new_day = calendar(old_period), calendar(new_period)
        # Fixed goal races can advance the calendar further before the next
        # ordinary main menu. There is still exactly one logged outing action.
        advanced = old_day is not None and new_day is not None and new_day > old_day
        if old_period.get("pre_debut") is True and new_period.get("pre_debut") is True:
            old_count, new_count = before.get("remaining_turns"), menu_snapshot.get("remaining_turns")
            advanced = type(old_count) is int and type(new_count) is int and new_count == old_count - 1
        elif old_period.get("pre_debut") is True and before.get("remaining_turns") == 1:
            advanced = new_period.get("year_index") == 1 and new_period.get("month") == 7
        if source["sha256"] == pending["source"]["sha256"]:
            advanced = False
        if not advanced:
            raise ValueError("mei_outing_return_did_not_advance")
        self.data.update(completed=pending["stage"], pending=None, result_source=deepcopy(source))
        return True

    def record_tap(self, performed):
        pending = self.data["pending"]
        if pending is None or pending["tap_performed"] is not None:
            raise ValueError("mei_outing_receipt_duplicate_or_missing")
        pending["tap_performed"] = performed
        if performed is True:
            receipt = {"run_session_id": self.data["run_session_id"], **deepcopy(pending)}
            self.data["receipts"].append(receipt)
            return receipt
        return None  # An uncertain input is never retried automatically.

    def packet(self, *, turn, session_path, transition_path):
        source = self.data["source"]
        if (self.data["pending"] is not None or not verified_source(source, run_id=self.data["run_session_id"], turn=turn)):
            return None
        return {"schema_version": SCHEMA, "status": "accepted", "character": CHARACTER,
            "unlocked": self.data["unlocked"], "completed": self.data["completed"],
            "pending": None, "source": deepcopy(source),
            "session_path": str(Path(session_path).resolve()),
            "transition_session_path": str(Path(transition_path).resolve())}


def observe_unlock_report(session, report, *, turn):
    changed = False
    if report.get("route_transition", {}).get("run_session_id") not in (None, session.data["run_session_id"]):
        return False
    for row in report.get("step_reports", []):
        result = row.get("result", {})
        if result.get("route_transition", {}).get("run_session_id") not in (None, session.data["run_session_id"]):
            continue
        frame = result.get("frame_safety", {})
        if frame.get("text_status") != "ok":
            continue
        decision = result.get("decision", {})
        receipt = result.get("route_action_receipt_source", {})
        source = {"run_session_id": session.data["run_session_id"], "turn_index": turn,
            "path": row.get("screenshot_path") or decision.get("evidence", {}).get("image") or receipt.get("source_path"),
            "sha256": frame.get("source_image_sha256")}
        changed = session.observe_unlock(frame.get("visible_text", ""), source) or changed
        changed = session.observe_unlock_result(result.get('mei_unlock_result_observation'),source) or changed
        if result.get("performed") is True and decision.get("action") == "tap":
            action_id = decision.get("tap", {}).get("coordinate_name")
            selection=decision.get('evidence',{}).get('event_selection')
            changed = session.observe_unlock_choice(frame.get("visible_text", ""), action_id, source,selection=selection) or changed
    return changed
