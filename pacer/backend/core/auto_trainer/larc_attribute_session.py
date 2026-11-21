"""Optional local forecast inputs from the normal collection path; no new OCR."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from core.auto_trainer.larc_attribute_plan import history_sample

HISTORY_FILE = "larc_attribute_history.json"
REWARDS_FILE = "larc_remaining_rewards.json"
EVENT_FILE = "event_progress_summary.json"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_attribute_context(session_dir, run_id, turn, *, first_expedition_memory=False, departure18_memory=False):
    """Missing optional files leave forecasts usable and rewards unknown."""
    root = Path(session_dir)
    inputs = {"run_session_id": run_id, "through_turn": turn, "training_history": [],
              "remaining_rewards": [], "source_notes": []}
    from core.auto_trainer.larc_policy import load_skill_context
    result = {"larc_attribute_inputs": inputs, **load_skill_context(root, run_id, turn)}
    from core.auto_trainer.larc_mei_contact import load_contact
    result['larc_mei_contact']=load_contact(root,run_id,turn)
    from core.auto_trainer.larc_expedition_forecast import load_expedition_context
    expedition = load_expedition_context(root, run_id, turn)
    if expedition:
        result["larc_expedition_context"] = expedition
    for name in (HISTORY_FILE, REWARDS_FILE, EVENT_FILE):
        path = root / name
        if not path.exists():
            continue
        try:
            data = _read(path)
            if not isinstance(data, dict) or data.get("run_session_id", data.get("run_id")) != run_id:
                raise ValueError("run_mismatch")
            through = data.get("through_turn", data.get("current_turn_index"))
            if type(through) is int and through > turn:
                raise ValueError("future_turn")
            if name == HISTORY_FILE:
                inputs["training_history"] = data.get("samples", [])
                if departure18_memory:
                    from core.auto_trainer.larc_phase_memory_io import _planned
                    inputs['training_history'] = [deepcopy(row) for row in inputs['training_history']
                        if isinstance(row, dict) and type(row.get('turn_index')) is int
                        and row['turn_index'] < turn and row.get('run_session_id', run_id) == run_id
                        and not _planned(row) and not row.get('simulation') and 'simulation_truth' not in row
                        and not any(_planned((m.get('energy_observation') or {}).get('source') or {})
                            for m in (row.get('forecast_metadata') or {}).values() if isinstance(m, dict))]
            elif name == EVENT_FILE:
                result["larc_event_progress"] = data
            else:
                # Remaining-window overrides are specific to an observation;
                # don't keep subtracting the same count on later turns.
                for key in ("remaining_rewards", "fixed_race_reward_coverage"):
                    if key in data:
                        inputs[key] = data[key]
                if through == turn:
                    for key in ("remaining_free_actions", "reserved_recovery_actions"):
                        if key in data:
                            inputs[key] = data[key]
            inputs["source_notes"].append({"path": str(path), "status": "loaded"})
        except (OSError, ValueError, TypeError) as exc:
            inputs["source_notes"].append({"path": str(path), "status": "unavailable", "reason": str(exc)})
    support_path = root / "support_chain_context.json"
    if support_path.is_file():
        try:
            support = _read(support_path)
            if (support.get("schema_version") != "uma.support_chain_context.v1"
                    or support.get("run_session_id") != run_id):
                raise ValueError("support_chain_run_or_schema_mismatch")
            # This history uses pacer action turns, not the calendar turn above.
            # Keep it as separate reference knowledge; do not credit its rewards.
            result["larc_support_chain_context"] = support
            inputs["source_notes"].append({"path": str(support_path), "status": "loaded"})
        except (OSError, ValueError, TypeError) as exc:
            inputs["source_notes"].append({"path": str(support_path), "status": "unavailable", "reason": str(exc)})
    profile_path = root / "support_card_profile.json"
    loaded_profile = None
    if profile_path.is_file():
        try:
            candidate_profile = _read(profile_path)
            if isinstance(candidate_profile, dict) and candidate_profile.get("run_session_id") == run_id:
                loaded_profile = candidate_profile
                if candidate_profile.get("schema_version") == "uma_run_support_profile.v1":
                    result["larc_support_card_profile"] = deepcopy(candidate_profile)
        except (OSError, ValueError, TypeError):
            loaded_profile = None
    from core.auto_trainer.larc_action_history import action_prefix, ss_cycle_history, ss_deadline_history
    # Missing optional history cannot silently grant another full-group
    # deferral after a process restart: expose known/unknown on every load.
    result['larc_ss_deadline_history']=ss_deadline_history(root,run_id,turn)
    try:
        prefix = action_prefix(root, run_id, turn, inputs['training_history'])
        if prefix is not None:
            result['larc_expectation_action_prefix'] = prefix
        cycle = ss_cycle_history(root, run_id, turn)
        if cycle['ordinary_offers'] or cycle['readiness_intervals']:
            result['larc_ss_cycle_history'] = cycle
    except (OSError, ValueError, TypeError, KeyError) as exc:
        inputs['source_notes'].append({'path': 'confirmed_strategy_history', 'status': 'unavailable',
                                      'reason': type(exc).__name__})
    if first_expedition_memory is True:
        try:
            from core.auto_trainer.larc_phase_memory_io import load_optional
            packet,diagnostic=load_optional(root,run_id,turn)
        except Exception as exc:
            packet={'status':'unavailable','run_session_id':run_id,'through_turn':turn,'records':[]}
            diagnostic={'status':'unavailable','reason':'optional_memory_bridge_unavailable','error_type':type(exc).__name__}
        if loaded_profile is not None:
            try:
                result['larc_tail263_support_profile']={k:deepcopy(loaded_profile[k])for k in ('run_session_id','race_bonus_percent','event_multiplier')if k in loaded_profile}
                from core.auto_trainer.run_support_profile import _aggregate_effects
                nested=_aggregate_effects({'terminal_deck_effects':loaded_profile.get('terminal_deck_effects')},loaded_profile.get('cards'))
                if nested:
                    result['larc_last_training_deck_effects']={'run_session_id':run_id,'terminal_deck_effects':deepcopy(nested)}
            except (OSError,ValueError,TypeError):
                pass  # Unknown declared effects keep a named conservative scenario.
        result['larc_first_expedition_reference']=packet
        result['larc_first_expedition_reference_diagnostic']=diagnostic
    return result


def record_attribute_sample(session_dir, run_id, state, *, first_expedition_memory=False, departure18_memory=False):
    """Replace a recollected turn, retain twelve recent layouts, isolate runs."""
    if not getattr(state, "training_options", None) or type(getattr(state, "turn", None)) is not int:
        return None
    path = Path(session_dir) / HISTORY_FILE
    samples = []
    if path.exists():
        data = _read(path)
        if data.get("run_session_id") != run_id:
            raise ValueError("attribute_history_run_mismatch")
        if type(data.get("through_turn")) is int and data["through_turn"] > state.turn:
            raise ValueError("attribute_history_newer_than_observation")
        samples = data.get("samples", [])
    samples = [row for row in samples if type(row.get("turn_index")) is int
               and row["turn_index"] < state.turn]
    sample = history_sample(state)
    if departure18_memory:
        from core.auto_trainer.larc_phase_memory_io import observed_layout_sample
        accepted = observed_layout_sample(state, run_id, sample)
        if accepted is None:
            return None  # Optional memory never promotes a modeled/foreign layout.
        sample = accepted
    from core.auto_trainer.larc_action_history import action_prefix, expectation_transition
    try:
        prefix = action_prefix(Path(session_dir), run_id, state.turn, samples)
        transition = expectation_transition(state, prefix)
        if transition is not None:
            sample['expectation_transition'] = deepcopy(transition)
            for metadata in sample.get('forecast_metadata', {}).values():
                metadata['expectation_transition'] = deepcopy(transition)
    except (OSError, ValueError, TypeError, KeyError):
        # Optional source attribution is unknown; never retry an action for it.
        pass
    samples.append(sample)
    data = {"schema_version": "larc_attribute_history.v1", "run_session_id": run_id,
            "through_turn": state.turn, "samples": sorted(samples, key=lambda row: row["turn_index"])[-12:]}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    if first_expedition_memory is True:
        try:
            from core.auto_trainer.larc_phase_memory_io import record_optional
            diagnostic=record_optional(session_dir,run_id,state,sample)
            if diagnostic.get('reason')=='optional_file_unavailable':
                import logging
                logging.getLogger(__name__).debug('Optional first-expedition memory unchanged: %s',diagnostic)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).debug('Optional first-expedition memory unavailable: %s',type(exc).__name__)
    return path
