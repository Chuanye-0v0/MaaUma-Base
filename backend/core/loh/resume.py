"""Atomic, same-run LOH admission journal; no ticket-count arithmetic."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import uuid

from .policy import (ENTRY_PAGES, PROGRESS_PAGES, RESULT_PAGES, authorize,
                     entry_continuation_available, has_loh_context)


_BINDING_KEYS = ("action_id", "source_page", "source_frame_sha256", "dispatch_id",
                 "input_status", "effect", "consuming", "entry_continuation")


def _small_observation(observed):
    # Keep page/control/receipt evidence, not ndarray frame buffers or OCR engines.
    return {key: deepcopy(value) for key, value in observed.items()
            if key not in {"frame", "image"}}


def _unconfirmed_matching(entry):
    return bool(entry and not entry.get('adopted')
        and entry.get('action_id') == 'choose_opponent'
        and entry.get('source_page') in {'loh_hub', 'loh_result'}
        and entry.get('input_status') == 'submitted'
        and (entry.get('entry_confirmation') or {}).get('status') not in {'submitted', 'reserved', 'uncertain'})


class StateBook:
    def __init__(self, output, endpoint, resume_from=None):
        output = Path(output).resolve()
        self.path = Path(resume_from).resolve() if resume_from else output / "loh-state.json"
        self.root = self.path.parent
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("LOH endpoint is required")
        self.endpoint = endpoint
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("schema") != "uma.loh.state.v1":
                raise ValueError("LOH resume schema mismatch")
            if self.data.get("endpoint") != endpoint or self.data.get("root") != str(self.root):
                raise ValueError("LOH resume scope or endpoint mismatch")
            # An original bound admission is already LOH-owned. This preserves
            # pre-owner-field journals without inventing an account proof chain.
            self.data.setdefault("loh_context_verified", False)
            if self.data.get("pending"):
                self.data["loh_context_verified"] = True
        elif resume_from:
            raise ValueError("LOH resume state does not exist")
        else:
            self.data = {"schema": "uma.loh.state.v1", "scope_id": uuid.uuid4().hex,
                "root": str(self.root), "endpoint": endpoint, "entries_started": 0,
                "entries_completed": 0, "adopted_entries": 0, "pending": None,
                "records": [], "last_observation": {}, "last_action": None,
                "cold_adoption_allowed": True, "result_boundary_ready": False,
                "loh_context_verified": False}
            self.save()

    def save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _complete(self, page):
        pending = self.data.get("pending")
        if not pending or not pending.get("result_seen"):
            return
        self.data["entries_completed"] += 1
        entry = {**deepcopy(pending), "status": "completed", "completed_page": page}
        self.data["last_completed_entry"] = entry
        self.data["records"].append({"kind": "entry_completed", **entry})
        self.data["pending"] = None
        self.data["cold_adoption_allowed"] = False

    def _restore_unconfirmed_matching(self, observed):
        """Repair the old transient-summary classification using its own ACKs."""
        alias = self.data.get('pending') or {}
        completed = self.data.get('last_completed_entry') or {}
        if (observed.get('page') != 'loh_entry_confirm'
                or observed.get('reason') != 'native_auto_hero_items_entry'
                or alias.get('input_status') != 'adopted' or alias.get('result_seen')
                or not _unconfirmed_matching(completed) or completed.get('completed_page') != 'loh_result'):
            return
        records = self.data['records']
        indexes = [i for i,r in enumerate(records) if r.get('kind') == 'entry_completed'
                   and r.get('entry_id') == completed['entry_id']]
        if not indexes:
            return
        index = indexes[-1]
        receipts = {r.get('dispatch_id'): r['receipt'] for r in records
                    if r.get('kind') == 'action_receipt'}
        matching = receipts.get(completed.get('dispatch_id'), {})
        outcome = matching.get('action_outcome') or {}
        if (matching.get('execution_status') != 'executed'
                or outcome.get('input_attempts') != 1 or outcome.get('acknowledged_inputs') != 1):
            return
        # A later consuming intent must have an explicit zero-input receipt.
        # Submitted/uncertain final confirmations are never reclassified.
        for record in records[index+1:]:
            if record.get('kind') == 'action_reserved' and record.get('consuming'):
                receipt = receipts.get(record.get('dispatch_id'), {})
                outcome = receipt.get('action_outcome') or {}
                if (receipt.get('execution_status') != 'not_executed'
                        or outcome.get('input_attempts') != 0 or outcome.get('acknowledged_inputs') != 0):
                    return
        restored = deepcopy(completed)
        for key in ('status', 'completed_page', 'result_frame_sha256'):
            restored.pop(key, None)
        restored['result_seen'] = False
        self.data['records'].append(dict(kind='entry_completion_reclassified',
            reason='old_summary_during_unconfirmed_matching', entry_id=completed['entry_id'],
            original_completed_record=deepcopy(completed), adopted_alias=deepcopy(alias),
            confirmation_frame_sha256=observed.get('frame_sha256')))
        self.data['entries_completed'] -= 1
        self.data['last_completed_entry'] = next((deepcopy(r) for r in reversed(records[:index])
            if r.get('kind') == 'entry_completed'), None)
        self.data['pending'] = restored
        self.data['result_boundary_ready'] = False

    def observe(self, observed):
        if observed.get("endpoint", self.endpoint) != self.endpoint:
            raise ValueError("LOH observation endpoint mismatch")
        page = observed.get("page", "unknown")
        if (observed.get('spectator_playback_cancel') is True and self.data.get('final_watch_auto')
                and has_loh_context(self.data) and not self.data.get('pending') and not self.data.get('pending_like')):
            self.data.setdefault('final_watch_requirement',dict(enabled=True,groups=['A','B'],reward_each=30,mode='current_two'))
        from .final_watch import observe_state as observe_watch, requirement as watch_requirement
        observe_watch(self.data,observed)
        if watch_requirement(self.data) and not self.data.get('pending') and page in PROGRESS_PAGES|RESULT_PAGES:
            self.data['last_observation']=_small_observation(observed)
            self.save()
            return self.data
        self._restore_unconfirmed_matching(observed)
        pending = self.data.get("pending")
        if (page=='events' and observed.get('spectator_playback_period') is True
                and self.data.get('spectator_playback_cancel_verified') and not pending
                and not self.data.get('pending_like') and has_loh_context(self.data)):
            self.data['ranking_period_verified']=True
            self.data['ranking_period_basis']='native_replay_card_after_verified_cancel'
        if (page == 'loh_closed' and observed.get('unavailable_verified') is True
                and observed.get('entry_disabled') is True and not pending
                and has_loh_context(self.data)):
            self.data['ranking_period_verified'] = True
        if isinstance(page, str) and page.startswith("loh_") and observed.get("requires_loh_context") is not True:
            self.data["loh_context_verified"] = True
        if observed.get("requires_loh_context") is True and not has_loh_context(self.data):
            # Generic race/reward controls also occur in other events. A cold
            # page alone cannot establish that this task owns those controls.
            self.data["last_observation"] = _small_observation(observed)
            self.save()
            return self.data
        from .spectator import observe_state
        observe_state(self.data, observed)
        if (page == 'loh_team_confirm' and observed.get('team_complete') is True
                and observed.get('team_confirmation_modal') is False
                and not self.data.get('empty_team_auto_requested') and not self.data.get('team_ready')):
            # A pre-existing full team needs no editor/save round-trip. Keep
            # its three selections and return to the native opponent button.
            self.data['team_ready'] = True
            self.data['records'].append(dict(kind='existing_team_retained',
                source_frame_sha256=observed.get('frame_sha256')))
        if (page == 'loh_result' and observed.get('reason') == 'settled_points_summary'
                and _unconfirmed_matching(pending)):
            # Team preview can flash the preceding group's summary while the
            # original opponents are being matched. It is not this entry's result.
            self.data['last_observation'] = _small_observation(observed)
            self.save()
            return self.data
        if (pending and pending.get('adopted') is True and not pending.get('result_seen')
                and self.data.get('entries_started', 0) == 0 and page == 'loh_hub'
                and (observed.get('actions') or {}).get('enter_loh', {}).get('effect') == 'navigation'):
            # A legacy observer mistook the hub's "主要赛事进行中" banner for
            # playback. This was an adoption, never an admission input. Retain
            # its history while correcting it at the explicit pre-entry hub;
            # submitted/uncertain ticket entries cannot take this path.
            self.data['records'].append(dict(kind='adoption_reclassified',
                entry_id=pending['entry_id'], reason='explicit_pre_entry_hub_without_submission',
                source_frame_sha256=observed.get('frame_sha256')))
            self.data['pending'] = None
            pending = None
        if page == 'loh_no_tickets':
            self.data['tickets_exhausted'] = True
            if (observed.get('exhaustion_verified') is True and observed.get('entry_disabled') is True
                    and pending and not pending.get('adopted')
                    and pending.get('action_id') == 'choose_opponent'
                    and pending.get('source_page') in {'loh_hub', 'loh_result'}
                    and pending.get('input_status') == 'submitted'
                    and not pending.get('progress_seen') and not pending.get('result_seen')):
                # A tap on the now-disabled matching control never reached the
                # final ticket confirmation. Keep its ACK/history as not started.
                self.data['records'].append(dict(kind='entry_not_started',
                    reason='native_zero_tickets_disabled_entry',**deepcopy(pending)))
                self.data['last_unstarted_entry'] = deepcopy(pending)
                self.data['entries_started'] -= 1
                self.data['pending'] = None
                pending = None
        if page == 'loh_hub':
            self.data['team_config_reward_pending'] = False
        config_reward = page == 'loh_rewards' and self.data.get('team_config_reward_pending')
        if pending is None and not config_reward and (page in {"loh_matching", "loh_preview", "loh_race"} or
                (page in RESULT_PAGES and self.data.get("cold_adoption_allowed"))):
            pending = {"entry_id": uuid.uuid4().hex, "adopted": True,
                "input_status": "adopted", "progress_seen": True, "result_seen": False,
                "source_page": page, "source_frame_sha256": observed.get("frame_sha256")}
            self.data["pending"] = pending
            self.data["adopted_entries"] += 1
            self.data["cold_adoption_allowed"] = False
            self.data["records"].append({"kind": "entry_adopted", "entry_id": pending["entry_id"], "page": page})
        if pending:
            confirmation = pending.get("entry_confirmation") or {}
            beyond_confirmation = (page in PROGRESS_PAGES or page == "loh_hub"
                or page in RESULT_PAGES and pending.get("progress_seen"))
            if beyond_confirmation and confirmation.get("status") in {None, "available"}:
                pending["entry_confirmation"] = {**confirmation, "status": "expired", "expired_page": page}
            if page in PROGRESS_PAGES:
                pending["progress_seen"] = True
            if page in RESULT_PAGES and pending.get("progress_seen"):
                pending["result_seen"] = True
                pending["result_frame_sha256"] = observed.get("frame_sha256")
            direct_next = any(isinstance(action, dict) and action.get("enabled") is not False
                              and action.get("effect") == "admission"
                              for action in (observed.get("actions") or {}).values())
            if pending.get("result_seen") and (page in ENTRY_PAGES or
                    page in {"loh_no_tickets", "loh_closed"} or
                    (page in RESULT_PAGES and direct_next)):
                self._complete(page)
                self.data["result_boundary_ready"] = page in RESULT_PAGES and direct_next
        if page not in RESULT_PAGES:
            self.data["result_boundary_ready"] = False
        self.data["last_observation"] = _small_observation(observed)
        self.save()
        return self.data

    def reserve(self, action, observed):
        action = {"action_id": action} if isinstance(action, str) else action
        action_id = action["action_id"]
        verdict = authorize(action_id, observed, self.data)
        if not verdict["ok"]:
            raise ValueError(verdict["reason"])
        pending = self.data.get("pending")
        continuation = bool(verdict.get("entry_continuation") and
            entry_continuation_available(pending, action_id, observed.get("page")))
        if verdict["consuming"] and pending and not continuation:
            raise ValueError("loh_pending_admission_unresolved")
        reserved = {"dispatch_id": uuid.uuid4().hex, "action_id": action_id,
            "consuming": verdict["consuming"], "source_page": observed.get("page"),
            "source_frame_sha256": observed.get("frame_sha256"),
            "effect": (observed.get("actions") or {}).get(action_id, {}).get("effect"),
            "entry_continuation": continuation}
        if action_id == 'confirm_team':
            reserved['team_confirmation_final'] = observed.get('team_confirmation_modal') is True
        if action_id=='close_notice' and observed.get('spectator_playback_cancel') is True:
            reserved['spectator_playback_cancel']=True
        if continuation:
            prior_confirmation = deepcopy(pending["entry_confirmation"])
            prior_binding = {key: deepcopy(pending.get(key)) for key in _BINDING_KEYS}
            pending.update(deepcopy(reserved), input_status="reserved")
            pending["entry_confirmation"] = {**prior_confirmation, "status": "reserved",
                "prior_binding": prior_binding, "prior_confirmation": prior_confirmation}
            reserved["entry_id"] = pending["entry_id"]
        elif verdict["consuming"]:
            entry_id = uuid.uuid4().hex
            self.data["entries_started"] += 1
            self.data["pending"] = {"entry_id": entry_id, "adopted": False,
                "input_status": "reserved", "progress_seen": False, "result_seen": False,
                "prior_result_boundary_ready": self.data.get("result_boundary_ready", False),
                "prior_cold_adoption_allowed": self.data.get("cold_adoption_allowed", False),
                **deepcopy(reserved)}
            self.data["cold_adoption_allowed"] = False
            self.data["result_boundary_ready"] = False
            reserved["entry_id"] = entry_id
        from .spectator import reserve as reserve_spectator
        reserve_spectator(self.data, action_id, observed, reserved)
        from .final_watch import reserve as reserve_watch
        reserve_watch(self.data,action_id,observed,reserved)
        self.data["last_action"] = reserved
        self.data["records"].append({"kind": "action_reserved", **deepcopy(reserved)})
        # This completes before device.step: a crash at the input boundary leaves
        # an unresolved reservation, never a fresh permission to spend again.
        self.save()
        return reserved

    def apply_receipt(self, receipt):
        if not isinstance(receipt, dict):
            raise ValueError("LOH receipt must be an object")
        if receipt.get("endpoint", self.endpoint) != self.endpoint:
            raise ValueError("LOH receipt endpoint mismatch")
        action = self.data.get("last_action") or {}
        if receipt.get("action_id", action.get("action_id")) != action.get("action_id"):
            raise ValueError("LOH receipt action mismatch")
        outcome = receipt.get("action_outcome") or {}
        uncertain = bool(receipt.get("uncertain") or outcome.get("status") in {"uncertain", "pending"}
                         or receipt.get("execution_status") in {"uncertain", "pending"})
        attempts = outcome.get("input_attempts")
        acknowledged = outcome.get("acknowledged_inputs")
        zero_input = (not uncertain and receipt.get("execution_status") == "not_executed"
                      and attempts == 0 and acknowledged == 0)
        performed = (receipt.get("execution_status") == "executed"
                     and isinstance(attempts, int) and attempts > 0
                     and isinstance(acknowledged, int) and acknowledged > 0)
        if action.get("action_id") == "open_loh" and performed and not uncertain:
            self.data["loh_context_verified"] = True
        if (action.get('spectator_playback_cancel') and performed and not uncertain
                and outcome.get('execution_status')=='executed'
                and type(attempts) is int and type(acknowledged) is int
                and attempts==acknowledged and not self.data.get('pending')
                and not self.data.get('pending_like')):
            self.data['spectator_playback_cancel_verified']=True
        if action.get('action_id') == 'auto_team' and performed and not uncertain:
            self.data['empty_team_auto_requested'] = True
        if (action.get('action_id') == 'confirm_team' and action.get('team_confirmation_final')
                and performed and not uncertain):
            self.data['team_ready'] = True
            self.data['team_config_reward_pending'] = True
        pending = self.data.get("pending")
        if pending and pending.get("dispatch_id") == action.get("dispatch_id"):
            confirmation = pending.get("entry_confirmation") or {}
            if action.get("entry_continuation"):
                if zero_input:
                    pending.update(deepcopy(confirmation["prior_binding"]))
                    pending["entry_confirmation"] = deepcopy(confirmation["prior_confirmation"])
                else:
                    pending["input_status"] = "submitted" if performed and not uncertain else "uncertain"
                    confirmation["status"] = pending["input_status"]
            elif zero_input:
                self.data["entries_started"] -= 1
                self.data["result_boundary_ready"] = pending.get("prior_result_boundary_ready", False)
                self.data["cold_adoption_allowed"] = pending.get("prior_cold_adoption_allowed", False)
                self.data["pending"] = None
            else:
                pending["input_status"] = "submitted" if performed and not uncertain else "uncertain"
                after_page = (receipt.get("observation") or {}).get("page")
                if (performed and not uncertain and action.get("source_page") not in RESULT_PAGES
                        and after_page in RESULT_PAGES):
                    # A confirmed admission from a non-result entrance can go
                    # straight to results. Reselecting ON an old result cannot
                    # use that same residual surface as its own completion.
                    pending["progress_seen"] = True
        elif pending and performed and not uncertain and not action.get("consuming"):
            if action.get("effect") == "progress":
                pending["progress_seen"] = True
            if (action.get("source_page") in {"loh_opponents", "loh_team_confirm"}
                    and pending.get("input_status") == "submitted" and not pending.get("adopted")
                    and not pending.get("result_seen")
                    and ((pending.get("entry_confirmation") or {}).get("status") in {None, "available"}
                         or ((receipt.get('observation') or {}).get('page') == 'loh_entry_confirm'
                             and (pending.get('entry_confirmation') or {}).get('expired_page') == 'loh_hub'))):
                pending["progress_seen"] = True
                pending["entry_confirmation"] = {"status": "available",
                    "from_dispatch_id": action.get("dispatch_id"),
                    "from_page": action.get("source_page"), "from_action_id": action.get("action_id")}
        from .final_watch import receipt as watch_receipt
        watch_receipt(self.data,action,zero_input,performed,uncertain,outcome)
        from .spectator import receipt as spectator_receipt
        spectator_receipt(self.data, action, zero_input, performed, uncertain)
        self.data["records"].append({"kind": "action_receipt", "dispatch_id": action.get("dispatch_id"),
            "action_id": action.get("action_id"), "receipt": _small_observation(receipt)})
        self.save()
        if isinstance(receipt.get("observation"), dict):
            self.observe(receipt["observation"])
        return self.data

    def record_failure(self, error, action=None):
        outcome = getattr(error, "action_outcome", None)
        if isinstance(outcome, dict):
            self.apply_receipt({"action_id": (action.get("action_id") if isinstance(action, dict) else action),
                "performed": False, "execution_status": outcome.get("execution_status"),
                "action_outcome": outcome})
        self.data["records"].append({"kind": "failure", "error": str(error),
            "action_id": action.get("action_id") if isinstance(action, dict) else action})
        self.save()
