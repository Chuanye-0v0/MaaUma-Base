"""Isolated, result-only D1 settlement after L'Arc's confirmed finish input."""
import argparse
import importlib.util
import json
from pathlib import Path
import time
import cv2
from copy import deepcopy


SETTLEMENT_ACTIONS = frozenset({
    "select_skill_evolution", "confirm_skill_evolution_selection", "confirm_skill_evolution", "advance_skill_evolution",
    "confirm_training_rating", "confirm_factors", "confirm_factor_choice",
    "close_trained_detail", "continue_score_record", "continue_scenario_record",
    "confirm_title_notice", "next_training_summary", "next_affinity_result",
    "next_training_rewards", "cancel_follow_trainer", "advance_settlement",
    "close_activity_story_unlock", "advance_activity_reward", "next_activity_points",
    "close_activity_points_claim", "close_completed_training", "close_character_story_unlock",
})

# These are new, free tail-page steps, not permission to repeat a dispatched
# close or revisit factor/skill transactions. The current capture must expose
# the exact action for its recognized page; the device revalidates it at input.
FREE_TAIL_ACTIONS = {
    "trained_detail": "close_trained_detail", "score_record": "continue_score_record",
    "scenario_record": "continue_scenario_record", "title_notice": "confirm_title_notice",
    "training_summary": "next_training_summary", "affinity_result": "next_affinity_result",
    "training_rewards": "next_training_rewards", "follow_trainer": "cancel_follow_trainer",
    "settlement_navigation": "advance_settlement",
    "character_story_unlock": "close_character_story_unlock",
    "activity_story_unlock": "close_activity_story_unlock",
    "activity_reward_animation": "advance_activity_reward",
    "activity_points_result": "next_activity_points",
    "activity_points_claim": "close_activity_points_claim",
}


def _settlement_action(observed, result_actions):
    page = observed.get("page")
    if page == "skill_evolution_select":
        actions = observed.get("actions") or {}
        if "confirm_skill_evolution_selection" in actions:
            return "confirm_skill_evolution_selection"
        return "select_skill_evolution" if "select_skill_evolution" in actions else None
    return {
        "training_complete": "close_completed_training",
        "character_story_unlock": "close_character_story_unlock",
        "skill_evolution_confirm": "confirm_skill_evolution",
        "skill_evolution_animation": "advance_skill_evolution",
        "training_rating": "confirm_training_rating", "factor_result": "confirm_factors",
        "factor_confirm": "confirm_factor_choice",
    }.get(page) or result_actions.get(page)


def _confirmed_close_destination(record, observed, action):
    if observed.get("page") == "home":
        return True
    previous = record.get("action_id")
    return (previous in {"close_completed_training", "close_character_story_unlock"}
        and action != previous
        and FREE_TAIL_ACTIONS.get(observed.get("page")) == action
        and action in observed.get("actions", {}))


def _zero_input_reobserve(outcome, action):
    return (isinstance(outcome, dict) and outcome.get('action_id') == action
        and outcome.get('execution_status') == 'not_executed'
        and outcome.get('input_attempts') == 0 and outcome.get('acknowledged_inputs') == 0
        and outcome.get('decision') in {'reobserve','wait_exhausted'})


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(4):
        try:
            temp.replace(path)
            break
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(.05)


def _dispatched_close_receipt(output, action, endpoint):
    if action not in {"close_completed_training", "close_character_story_unlock"}:
        return None
    path = output / "capture" / (action + ".receipt.json")
    try:
        receipt = _read(path)
    except (OSError, ValueError):
        return None
    if (receipt.get("action_id") != action or receipt.get("endpoint") != endpoint
            or receipt.get("performed") is not True
            or receipt.get("status") not in {"dispatched", "observed"}):
        return None
    return receipt


def _skill_helper():
    spec = importlib.util.spec_from_file_location("isolated_larc_skill_helpers",
        Path(__file__).with_name("larc_d1_skill_worker.py"))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def _transition_policy():
    # Load only after the isolated D1 API. Importing Pacer's core beforehand
    # would defeat _load_d1's clean namespace and mix the two source trees.
    spec = importlib.util.spec_from_file_location("isolated_result_transition_wait",
        Path(__file__).resolve().parents[1] / "core/auto_trainer/transition_wait.py")
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    return policy


def _settlement_wait_phase(policy, observed):
    phase = policy.transition_phase(key=observed.get("page"))
    if phase != "unknown" or observed.get("actions"):
        return phase
    path = observed.get("screenshot_path")
    image = cv2.imread(str(path)) if path and Path(path).is_file() else None
    if image is not None:
        if policy.connecting_overlay_visible(image):
            return "connection"
        if policy.career_comic_loading_visible(image):
            return "loading"
        # A truly blank white fade is read-only, not a result or click target.
        # Avoid importing Pacer's SafeAdvance into the vendor core namespace.
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        if ((hsv[:, :, 1] < 45) & (hsv[:, :, 2] > 210)).mean() > .985:
            return "loading"
    return "unknown"


def _load_api(d1_root, *, endpoint=None):
    api = _skill_helper()._load_d1(d1_root, endpoint=endpoint)
    from core.autonomous_training.harvest import RESULT_ACTIONS
    api.result_actions = dict(RESULT_ACTIONS)
    spec = importlib.util.spec_from_file_location("isolated_result_layouts",
        Path(__file__).resolve().parents[1] / "core/auto_trainer/career_result_layouts.py")
    layouts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(layouts)
    api.rating_continue_point = layouts.rating_continue_point
    api.title_notice_confirm_point = layouts.title_notice_confirm_point
    spec = importlib.util.spec_from_file_location("isolated_global_home_layout",
        Path(__file__).resolve().parents[1] / "core/auto_trainer/global_home_layout.py")
    home_layout = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(home_layout)
    api.is_global_home = home_layout.is_global_home
    return api


def run_settlement(*, d1_root, run_root, output_dir, endpoint, execute=False):
    report = {"schema_version": "larc_terminal_settlement.v1", "ok": False,
        "reason": "dry_run_no_device_actions", "endpoint": endpoint, "records": []}
    if not execute:
        return report
    destination = None
    readonly_postcondition = False
    def save():
        if destination is not None:
            _write(destination, report)
    try:
        root, output, d1 = (Path(p).resolve() for p in (run_root, output_dir, d1_root))
        progress = _read(root / "progress.json")
        finish = _read(root / "session/larc_terminal_finish.json")
        if (not _skill_helper().valid_endpoint(endpoint)
                or progress.get("run_session_id") != root.name or progress.get("device") != endpoint
                or finish.get("run_session_id") != root.name or finish.get("device") != endpoint
                or finish.get("turn_index") != progress.get("next_turn")
                or finish.get("status") != "submitted"):
            raise ValueError("settlement_finish_or_device_not_bound")
        if output == root or not output.is_relative_to(root) or output.is_relative_to(d1):
            raise ValueError("settlement_output_outside_run")
        state_path = root / "session/larc_terminal_settlement.json"
        if state_path.exists():
            state = _read(state_path)
            original = Path(state["report_path"]).resolve()
            if (state.get("run_session_id") != root.name or state.get("device") != endpoint
                    or state.get("turn_index") != progress["next_turn"] or not original.is_relative_to(root)):
                raise ValueError("settlement_checkpoint_context_mismatch")
            saved = _read(original)
            prior_saved = deepcopy(saved)
            if (saved.get("run_session_id") != root.name or saved.get("endpoint") != endpoint
                    or saved.get("turn_index") != progress["next_turn"]):
                raise ValueError("settlement_report_context_mismatch")
            if state.get("status") == "complete" and saved.get("ok") is True and saved.get("final_page") == "home":
                return saved
            boundary_path = original.parent/'capture/recovery/last-action.json'
            boundary = _read(boundary_path) if boundary_path.is_file() else {}
            for row in saved.get('records', []):
                if (row.get('status') == 'pending'
                        and saved.get('reason') in {'ReobserveRequired:action_page_changed:' + row['action_id'],
                            'ObservationTimeout:action_not_allowed:' + row['action_id']}
                        and _zero_input_reobserve(boundary, row['action_id'])):
                    row.update(status='not_performed_reobserve', action_outcome=boundary)
            unresolved = [row for row in saved.get("records", []) if not (
                    row.get('status') == 'completed' and row.get('receipt', {}).get('performed') is True
                    or row.get('status') == 'not_performed_reobserve'
                    and _zero_input_reobserve(row.get('action_outcome'), row['action_id']))]
            if (len(unresolved) == 1 and unresolved[0] is saved["records"][-1]
                    and (unresolved[0].get("status") == "submitted_destination_pending"
                         or saved.get("reason") == "RuntimeError:completion_home_destination_unverified")):
                receipt = _dispatched_close_receipt(original.parent, unresolved[0]["action_id"], endpoint)
                if receipt is not None:
                    unresolved[0].update(status="submitted_destination_pending", receipt=receipt)
                    readonly_postcondition = True
            if state.get("status") != "pending" or (unresolved and not readonly_postcondition):
                raise ValueError("settlement_unknown_input_not_replayed")
            backup = original.with_name(f"report.before_resume_{len(saved.get('resume_history', [])) + 1:03d}.json")
            _write(backup, prior_saved)
            report, output = saved, original.parent
            report.setdefault("resume_history", []).append({"prior_reason":saved.get("reason"),
                "original_report":str(backup),"completed_actions_replayed":False})
        else:
            output.mkdir(parents=True, exist_ok=False)
        destination = output / "report.json"
        report.update(run_session_id=root.name, turn_index=progress["next_turn"], reason="in_progress")
        save()
        state = {"run_session_id": root.name, "device": endpoint, "turn_index": progress["next_turn"],
            "status": "pending", "report_path": str(destination)}
        _write(state_path, state)
        api = _load_api(d1, endpoint=endpoint)
        wait_policy = _transition_policy()
        api.bind_transport(endpoint)
        class ResultOnlyDevice(api.Device):
            def capture(self, **kwargs):
                observed = super().capture(**kwargs)
                home_layout = getattr(api, "is_global_home", None)
                image = None
                if home_layout is not None:
                    image = cv2.imread(observed.get("screenshot_path", ""))
                    if home_layout(image):
                        return {**observed, "raw_page": observed.get("page"),
                            "page": "home", "layout_basis": "global_home_controls",
                            "actions": {}}
                layout = getattr(api, "rating_continue_point", None)
                if layout is not None and observed.get("page") in {"unknown","loading","training_rating"}:
                    point = layout(cv2.imread(observed.get("screenshot_path", "")))
                    if point is not None:
                        observed = {**observed, "raw_page":observed.get("page"),
                            "page":"training_rating", "layout_basis":"rating_page_controls",
                            "actions":{"confirm_training_rating":{"point":list(point)}}}
                title_layout = getattr(api,'title_notice_confirm_point',None)
                if (title_layout is not None and observed.get('page')=='title_notice'
                        and 'confirm_title_notice' not in observed.get('actions',{})):
                    point=title_layout(cv2.imread(observed.get('screenshot_path','')))
                    if point is not None:
                        observed={**observed,'layout_basis':'earned_title_confirm_controls',
                            'actions':{'confirm_title_notice':{'point':list(point)}}}
                return observed
            def step(self, action, stop_event=None):
                if action not in SETTLEMENT_ACTIONS:
                    raise ValueError("settlement_action_forbidden:" + action)
                return super().step(action, stop_event)
        device = ResultOnlyDevice(endpoint, None, output / "capture", expected_endpoint=endpoint)
        device.connect()
        device.context.update(mode="harvest", harvest_active=True,
            completion_submitted=True, harvest_update_team=False)
        prior_actions = {row["action_id"] for row in report["records"]
                         if row.get('status') != 'not_performed_reobserve'}
        if "confirm_factors" in prior_actions: device.context["factors_requested"] = True
        if "confirm_factor_choice" in prior_actions: device.context["factor_choice_submitted"] = True
        if "close_completed_training" in prior_actions:
            device.context.update(completion_home_submitted=True, harvest_home_submitted=True)
        wait_budget = wait_policy.TransitionBudget(clock=lambda: time.monotonic())
        last_source = None
        while len(report["records"]) < 40:
            observed = device.capture()
            if observed.get("endpoint") != endpoint:
                raise ValueError("settlement_capture_device_mismatch")
            page = observed.get("page")
            report["last_observation"] = observed
            controls = page, tuple(sorted(observed.get("actions", {})))
            wait_budget.observe(key=controls, phase=_settlement_wait_phase(wait_policy, observed))
            action = _settlement_action(observed, api.result_actions)
            if readonly_postcondition and _confirmed_close_destination(
                    report["records"][-1], observed, action):
                # The old dispatch is observed, never repeated. A recognized
                # new free tail page has its own action below; only Home ends
                # the settlement, not a title/story/activity notice.
                last = report["records"][-1]
                last.update(status="completed", destination_verified=True,
                    receipt={**last["receipt"], "status":"observed", "observation":observed})
                readonly_postcondition = False
                # The last dispatched input was this close, not the older
                # navigation row that preceded its postcondition exception.
                last_source = last["action_id"], last["receipt"].get("source", {}).get("frame_sha256")
            save()
            if page == "home":
                report.update(ok=True, reason="normal_settlement_complete", final_page="home")
                save()
                state["status"] = "complete"
                _write(state_path, state)
                break
            if readonly_postcondition:
                if wait_budget.expired:
                    report.setdefault("technical_wait_stops", []).append(wait_policy.technical_wait_stop(
                        wait_budget, reason="completion_home_destination_unverified", owner="settlement",
                        surface=page, run_session_id=root.name, turn_index=progress["next_turn"]))
                    raise RuntimeError("completion_home_destination_unverified")
                time.sleep(.2)
                continue
            if (not action or action == "open_completed_formation"
                    or action not in observed.get("actions", {})
                    or (action, observed.get("frame_sha256")) == last_source
                    or (action == "confirm_factors" and device.context.get("factors_requested"))
                    or (action == "confirm_factor_choice" and device.context.get("factor_choice_submitted"))):
                if wait_budget.expired:
                    report.setdefault("technical_wait_stops", []).append(wait_policy.technical_wait_stop(
                        wait_budget, reason="settlement_page_not_ready:" + str(page), owner="settlement",
                        surface=page, run_session_id=root.name, turn_index=progress["next_turn"]))
                    raise ValueError("settlement_page_not_ready:" + str(page))
                time.sleep(.2)
                continue
            record = {"action_id": action, "source_frame": observed.get("screenshot_path"),
                "source_page": page, "status": "pending"}
            report["records"].append(record)
            save()
            try:
                if action in {"close_completed_training", "close_character_story_unlock"}:
                    receipt = device.close_completed(action)
                    device.context["harvest_home_submitted"] = True
                else:
                    receipt = device.step(action)
            except Exception as error:
                if (type(error).__name__ in {'ReobserveRequired','ObservationTimeout'}
                        and _zero_input_reobserve(getattr(error, 'action_outcome', None), action)):
                    record.update(status='not_performed_reobserve', action_outcome=error.action_outcome)
                    last_source=action,observed.get('frame_sha256')
                    save()
                    continue  # Capture the actual next page; never replay the old point.
                if str(error) == "completion_home_destination_unverified":
                    dispatched = _dispatched_close_receipt(output, action, endpoint)
                    if dispatched is not None:
                        record.update(status="submitted_destination_pending", receipt=dispatched)
                        report.setdefault("postcondition_history", []).append({
                            "reason":f"{type(error).__name__}:{error}", "action_id":action,
                            "recovery":"read_only_destination", "input_repeated":False})
                        readonly_postcondition = True
                        # close_completed already spent its own probing time.
                        # Begin a fresh read-only destination stage after that
                        # acknowledged input; never submit the close again.
                        wait_budget.reset()
                        save()
                        continue
                raise
            record.update(status="completed" if receipt.get("performed") is True else "unconfirmed",
                receipt=receipt)
            save()
            if receipt.get("performed") is not True:
                raise ValueError("settlement_input_not_confirmed")
            last_source = action, observed.get("frame_sha256")
            wait_budget.reset()
        else:
            raise ValueError("settlement_step_limit")
    except Exception as exc:
        report.update(ok=False, reason=f"{type(exc).__name__}:{exc}")
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("d1-root", "run-root", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--execute", action="store_true")
    report = run_settlement(**vars(parser.parse_args()))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] or report["reason"] == "dry_run_no_device_actions" else 2


if __name__ == "__main__":
    raise SystemExit(main())
