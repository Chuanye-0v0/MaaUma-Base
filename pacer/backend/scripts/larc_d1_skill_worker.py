"""One isolated D1 skill batch; never finishes training or resumes a transaction."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace


ENDPOINT = "127.0.0.1:16448"
def valid_endpoint(endpoint):
    return _prerace_helper().valid_endpoint(endpoint)

SKILL_STEPS = frozenset({
    "open_skills", "return_harvest_skills", "scroll_skill_list",
    "scroll_skill_reveal_bottom", "scroll_skill_overlap", "scroll_skill_up",
})


def readonly_prepare_failure_state(worker_path, *, run_root, endpoint, turn_id):
    """Admit only a failed preparation with no selection/purchase stage."""
    root=Path(run_root).resolve();path=Path(worker_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():return None
    data=json.loads(path.read_text(encoding='utf-8'))
    learning=data.get('learning_result') or {}
    output=path.parent
    if (data.get('run_session_id')!=root.name or data.get('endpoint')!=endpoint
            or data.get('turn_id')!=turn_id or data.get('output_dir')!=str(output)
            or data.get('ok') is not False or data.get('stage')!='learning_pending'
            or learning.get('reason')!='skill_prepare_not_ready'
            or learning.get('child_plan_path') is not None
            or learning.get('device_action_count_complete') is not True):return None
    prepare=output/'transaction/prepare/skill_prepare.report.json'
    if not prepare.is_file():return None
    prepared=json.loads(prepare.read_text(encoding='utf-8'))
    if (prepared.get('purchase_executed') is not False or prepared.get('tap_count')!=0
            or prepared.get('status')!='blocked' or prepared.get('reason')!='skill_planner_failed'
            or any((output/'transaction'/name).exists() for name in ('selection','confirmation','return'))):return None
    points=(data.get('before') or {}).get('current_skill_points')
    receipts=list((output/'capture').glob('*-receipt.json'))
    if type(points) is not int or points<0 or not receipts:return None
    for receipt_path in receipts:
        receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
        if (receipt.get('endpoint')!=endpoint or receipt.get('performed') is not True
                or receipt.get('action_id') not in {'open_skills','scroll_skill_overlap',
                    'scroll_skill_list','scroll_skill_reveal_bottom','scroll_skill_up'}):return None
    return data


def _load_d1(root: Path, *, endpoint=None):
    """Only call inside a dedicated process, before importing any project code."""
    if not (root / "core/autonomous_training/takeover.py").is_file():
        raise ValueError("d1_module_root_invalid")
    if any(name.split(".")[0] in {"core", "scripts", "desktop"} for name in sys.modules):
        raise RuntimeError("project_modules_already_loaded_requires_isolated_process")
    if endpoint is not None and not valid_endpoint(endpoint):
        raise ValueError("skill_worker_endpoint_not_authorized")
    if (root / "desktop/umaa_public_context.py").is_file():
        selected = endpoint or os.environ.get("UMA_PUBLIC_ENDPOINT")
        if not valid_endpoint(selected):
            raise ValueError("packaged_skill_endpoint_required")
        os.environ["UMA_PUBLIC_ENDPOINT"] = selected
    sys.dont_write_bytecode = True
    u1 = Path(__file__).resolve().parents[1]
    sys.path[:] = [str(root)] + [p for p in sys.path
        if p and Path(p).resolve() not in {u1, u1 / "scripts", root}]
    compatibility = _prerace_helper()
    os.environ.update(compatibility.skill_ocr_environment(os.environ))
    compatibility.install_d1_skill_points_consensus()
    from core.autonomous_training.takeover import SkillTakeoverDevice
    from core.autonomous_training.finish import canonical_finish_evidence
    from core.autonomous_training.harvest_skills import (
        build_harvest_context, learn_harvest_skills, return_after_no_purchase_skills,
    )
    compatibility.install_d1_skill_compatibility()
    from core.skill_ocr_performance import harvest_skill_ocr
    spec = importlib.util.spec_from_file_location("u1_skill_completion_frames",
        Path(__file__).with_name("larc_skill_completion_frames.py"))
    frames = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(frames)
    return SimpleNamespace(Device=frames.completion_device_class(SkillTakeoverDevice),
        canonical_finish_evidence=canonical_finish_evidence,
        build_harvest_context=build_harvest_context,
        learn_harvest_skills=learn_harvest_skills,
        return_after_no_purchase_skills=return_after_no_purchase_skills,
        ocr_session=harvest_skill_ocr,
        bind_transport=_prerace_helper().bind_startup_transport)


def _prerace_helper():
    # The helper is stdlib-only; vendor transport changes stay in this isolated
    # worker and occur only after progress.device has matched the requested run.
    spec = importlib.util.spec_from_file_location("isolated_prerace_worker",
        Path(__file__).with_name("larc_d1_prerace_skill_worker.py"))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def _load_race_api(api):
    helper = _prerace_helper()
    from core.autonomous_training.skills import prepare_skills, publish_skill_plan
    from core.autonomous_training.purchase import select_and_open_purchase_confirmation, SKILL_BATCH_PURCHASE_SAFETY_TOKEN
    from core.autonomous_training.harvest_skills import complete_harvest_selection
    from core.skill_batch_purchase import _primary_evidence, validate_skill_batch_plan
    from core.skill_planner.preferences import load_skill_preference_profile
    from scripts import plan_fresh_skill_acquisition as planner_api
    api.prepare = lambda device, output, context, **kwargs: helper.prepare_for_arc_preference(
        prepare_skills, planner_api, device, output, context, **kwargs)
    api.publish = publish_skill_plan
    api.select, api.token = select_and_open_purchase_confirmation, SKILL_BATCH_PURCHASE_SAFETY_TOKEN
    api.complete, api.validate = complete_harvest_selection, validate_skill_batch_plan
    api.primary_evidence = _primary_evidence
    api.load_preference = load_skill_preference_profile
    api.replan = lambda **kwargs: helper.replan_with_startup_policy(planner_api, **kwargs)
    api.apply_race_preference = helper.apply_race_preference


def _learn_race_skills(api, device, output, context, preference, initial_points,
                       *, expected_endpoint=ENDPOINT):
    """Use the race planner while retaining D1's existing purchase/return chain."""
    if not hasattr(api, "prepare"):
        _load_race_api(api)
    if hasattr(api, "bind_transport"):
        api.bind_transport(expected_endpoint)
    prepared = api.prepare(device, output / "prepare", context, expected_endpoint=expected_endpoint)
    if prepared.get("status") != "ready":
        raise ValueError("race_skill_prepare_failed:" + str(prepared.get("reason")))
    prepared = api.apply_race_preference(api, prepared, output / "race_preference", context, preference)
    inventory = json.loads(Path(prepared["inventory_path"]).read_text(encoding="utf-8"))
    plan = json.loads(Path(prepared["batch_plan_path"]).read_text(encoding="utf-8"))
    valid, reason = api.validate(plan, inventory)
    if not valid:
        raise ValueError("race_skill_plan_invalid:" + str(reason))
    actions, remaining = plan.get("selected_action_ids"), plan.get("expected_remaining_skill_points")
    if (not isinstance(actions, list) or len(actions) > context["policy"]["max_purchase_count"]
            or plan.get("initial_skill_points") != initial_points or type(remaining) is not int
            or not 0 <= remaining <= initial_points
            or (not actions and remaining != initial_points) or (actions and remaining >= initial_points)):
        raise ValueError("race_skill_plan_points_or_count_invalid")
    if context.get("arc_policy", {}).get("enabled"):
        _prerace_helper()._target_policy().validate_target_plan(
            actions, prepared["target_diagnostic"], remaining_skill_points=remaining)
    if not actions:
        # No cart was selected. This is free navigation, not the harvest-only
        # empty-plan transaction whose pending evidence a race planner lacks.
        proof = output / "race_no_purchase_return.json"
        navigation = {"preparation": prepared, "selected_action_ids": [],
                      "purchase_executed": False, "return_navigation": []}
        def save_return():
            proof.write_text(json.dumps(navigation, ensure_ascii=False, indent=2), encoding="utf-8")
        navigation["after"] = _settle_learning_return(device, remaining, navigation, save_return,
            expected_endpoint=expected_endpoint, empty_plan=True)
        save_return()
        return {"ok": True, "reason": "no_purchase_return_verified", "remaining_skill_points": remaining,
                "return_after_learning_report_path": str(proof)}
    selection_root = output / "selection"
    nonce = f"{context['turn_id']}-{time.time_ns()}"
    selected = api.select(device=device, inventory_path=prepared["inventory_path"],
        plan_path=prepared["batch_plan_path"], review_path=None, output_dir=selection_root,
        mode="live", execute=True, expected_inventory_sha256=inventory["packet_sha256"],
        expected_plan_sha256=plan["plan_sha256"], planner_report_path=prepared["planner_report_path"],
        capture_manifest_path=prepared["capture_manifest_path"],
        external_skill_db_path=context["external_skill_db_path"],
        session_id=f"race-terminal-{nonce}", capture_batch_id=f"race-terminal-capture-{nonce}",
        safety_token=api.token, expected_endpoint=expected_endpoint)
    if selected.get("confirmation_opened") is not True or selected.get("stop_reason") != "confirmation_opened":
        raise ValueError("race_skill_confirmation_not_opened")
    learned = api.complete(device, output, selection_root, selected,
        {"ok": False, "device_action_count": 0, "device_action_count_complete": True,
         "device_action_count_scope": "confirmation_close_return"}, remaining, len(actions), None, expected_endpoint)
    owned_remaining = learned.get("remaining_skill_points")
    if learned.get("ok") is not True or type(owned_remaining) is not int or owned_remaining < 0:
        raise ValueError("race_skill_learning_failed:" + str(learned.get("reason")))
    return {**learned, "reason": "race_skill_learning_verified",
        **({"skill_pt_reading_discrepancy":{"planned":remaining,"verified_owner":owned_remaining}}
            if owned_remaining != remaining else {})}


def _merge_prior_finish_identity(observed, prior_identity):
    """Reuse prior verified identity only on finish_ready with >=3 agreeing styles and no conflict."""
    if not isinstance(prior_identity, dict):
        return None
    name = prior_identity.get("name")
    prior_styles = prior_identity.get("run_styles")
    if not isinstance(name, str) or not name or not isinstance(prior_styles, dict):
        return None
    expected_styles = {"领跑", "跟前", "居中", "后追"}
    valid_grades = {"G", "F", "E", "D", "C", "B", "A", "S", "SS"}
    if set(prior_styles) != expected_styles or any(
        not isinstance(prior_styles.get(k), str) or prior_styles.get(k) not in valid_grades
        for k in expected_styles
    ):
        return None
    if observed.get("page") != "finish_ready":
        return None
    actions = observed.get("actions")
    control = actions.get("finish_training") if isinstance(actions, dict) else None
    point = control.get("point") if isinstance(control, dict) else None
    if not (
        isinstance(point, list) and len(point) == 2
        and all(type(item) is int for item in point)
        and (0 <= point[0] < 720) and (0 <= point[1] < 1280)
        and control.get("enabled") is not False
    ):
        return None
    summary = observed.get("finish_summary")
    if not isinstance(summary, dict) or summary.get("name") != name:
        return None
    obs_styles = summary.get("run_styles")
    if not isinstance(obs_styles, dict) or not (set(obs_styles) <= expected_styles):
        return None
    non_null = {k: v for k, v in obs_styles.items() if v is not None}
    if len(non_null) < 3:
        return None
    if any(v != prior_styles.get(k) for k, v in non_null.items()):
        return None
    return {"name": name, "run_styles": dict(prior_styles)}


def _terminal(device, api, *, expected_endpoint=ENDPOINT, prior_identity=None,
              max_attempts=6, retry_delay=0.35):
    observed = None
    for attempt in range(max_attempts):
        observed = device.capture()
        if observed.get("endpoint") != expected_endpoint:
            raise ValueError("fresh_terminal_not_verified")
        if (observed.get("page") != "finish_ready"
                or type(observed.get("current_skill_points")) is not int
                or observed["current_skill_points"] < 0):
            if attempt + 1 < max_attempts and observed.get("page") in {"finish_ready", "unknown", "loading", None}:
                time.sleep(retry_delay)
                continue
            raise ValueError("fresh_terminal_not_verified")
        captured = observed.get("captured_at_unix_ms")
        if type(captured) is not int or not 0 <= time.time()*1000-captured <= 10000:
            raise ValueError("terminal_capture_not_fresh")
        frame = Path(observed["screenshot_path"])
        if hashlib.sha256(frame.read_bytes()).hexdigest() != observed.get("frame_sha256"):
            raise ValueError("terminal_source_sha_mismatch")
        identity = api.canonical_finish_evidence(observed)
        if identity:
            before = {key: observed[key] for key in (
                "endpoint", "page", "current_skill_points", "screenshot_path", "frame_sha256",
                "captured_at_unix_ms")}
            if isinstance(observed.get("current_stats"), dict):
                before["current_stats"] = observed["current_stats"]
            if attempt > 0:
                before["terminal_capture_attempts"] = attempt + 1
            return before, identity
        if attempt + 1 < max_attempts:
            time.sleep(retry_delay)
            continue
        fallback = _merge_prior_finish_identity(observed, prior_identity)
        if fallback:
            synthetic = {**observed, "finish_summary": {**(observed.get("finish_summary") or {}), "run_styles": fallback["run_styles"]}}
            verified_fallback = api.canonical_finish_evidence(synthetic)
            if verified_fallback:
                before = {key: observed[key] for key in (
                    "endpoint", "page", "current_skill_points", "screenshot_path", "frame_sha256",
                    "captured_at_unix_ms")}
                if isinstance(observed.get("current_stats"), dict):
                    before["current_stats"] = observed["current_stats"]
                before["terminal_capture_attempts"] = attempt + 1
                before["identity_fallback"] = "prior_batch_verified_partial_match"
                return before, verified_fallback
    raise ValueError("terminal_identity_unverified")


def confirmed_return_state(worker_path: Path, *, run_root: Path, endpoint: str,
                           turn_id: str | None = None) -> dict | None:
    """An already successful purchase/return may resume navigation, never learn."""
    try:
        root, path = Path(run_root).resolve(), Path(worker_path).resolve()
        saved = json.loads(path.read_text(encoding="utf-8"))
        learned = saved.get("learning_result", {})
        if (not path.is_relative_to(root) or saved.get("output_dir") != str(path.parent)
                or saved.get("run_session_id") != root.name or saved.get("endpoint") != endpoint
                or (turn_id is not None and saved.get("turn_id") != turn_id)
                or saved.get("stage") != "verify_terminal"
                or learned.get("ok") is not True or learned.get("reason") not in {
                    "harvest_skill_learning_verified", "race_skill_learning_verified"}
                or type(learned.get("remaining_skill_points")) is not int
                or learned["remaining_skill_points"] < 0):
            return None
        proof_path = Path(saved["return_proof_path"]).resolve()
        if not proof_path.is_relative_to(path.parent):
            return None
        returned = json.loads(proof_path.read_text(encoding="utf-8"))
        final_path = path.parent / "transaction/confirmation/final_confirmation_report.json"
        final = json.loads(final_path.read_text(encoding="utf-8"))
        if (returned.get("return_performed") is not True
                or returned.get("device_endpoint") != endpoint
                or returned.get("verified_current_skill_points") != learned["remaining_skill_points"]
                or final.get("final_confirmation_performed") is not True
                or final.get("actions") != ["skill_purchase.final_confirm"]
                or final.get("mode") != "live" or final.get("execute") is not True):
            return None
        return saved
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _settle_learning_return(device, points: int, report: dict, save,
                            *, expected_endpoint=ENDPOINT, empty_plan=False):
    """Follow the visible free Back control; do not reconcile learned effects."""
    back_submitted = False
    navigation = report.setdefault("return_navigation", [])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        observed = device.capture()
        if observed.get("endpoint") != expected_endpoint:
            raise ValueError("terminal_return_endpoint_mismatch")
        if observed.get("page") == "finish_ready":
            after = {key: observed.get(key) for key in (
                "endpoint", "page", "screenshot_path", "frame_sha256", "captured_at_unix_ms")}
            after.update(current_skill_points=points,
                points_basis="owned_learning_return_receipt",
                observed_current_skill_points=observed.get("current_skill_points"))
            return after
        point = observed.get("actions", {}).get("return_harvest_skills", {}).get("point")
        if (not back_submitted and observed.get("page") == "skills"
                and observed.get("skill_cart_disabled") is True
                and "open_skill_confirmation" not in observed.get("actions", {})
                and isinstance(point, (list, tuple)) and len(point) == 2):
            if empty_plan:
                if observed.get("current_skill_points") != points:
                    raise ValueError("empty_plan_return_points_changed")
                frame = Path(observed.get("screenshot_path", ""))
                if (not frame.is_file()
                        or hashlib.sha256(frame.read_bytes()).hexdigest() != observed.get("frame_sha256")
                        or type(observed.get("captured_at_unix_ms")) is not int
                        or not 0 <= time.time()*1000-observed["captured_at_unix_ms"] <= 10000):
                    raise ValueError("empty_plan_return_source_unverified")
            back_submitted = True
            receipt = {"action": "return_unselected_terminal_skills" if empty_plan else "return_confirmed_terminal_skills", "point": list(point),
                "source_frame": observed.get("screenshot_path"), "status": "pending"}
            navigation.append(receipt)
            save()
            # Only a currently visible, disabled-cart Back; the confirmed
            # learning chain is never reopened or submitted again.
            device.command("-s", expected_endpoint, "shell", "input", "tap",
                           str(point[0]), str(point[1]))
            receipt["status"] = "submitted"
            save()
        time.sleep(.15)
    raise ValueError("terminal_return_page_not_settled")


def run_skill_batch(*, d1_root: Path, run_root: Path, output_dir: Path, turn_id: str,
                    resolver_catalog: Path, planner_catalog: Path,
                    external_skill_db_path: Path, endpoint: str = ENDPOINT,
                    adb_path: str | None = None, execute: bool = False,
                    current_speed: int | None = None,
                    prior_identity: dict | None = None,
                    resume_confirmed_return: bool = False, resume_readonly_prepare: Path | None = None) -> dict:
    report = {"schema_version": "larc_d1_skill_batch.v1", "ok": False,
              "reason": "dry_run_no_device_actions", "endpoint": endpoint,
              "turn_id": turn_id, "input_may_have_occurred": False, "stage": "not_started"}
    if execute is not True:
        return report
    destination = None
    ocr_stack = ExitStack()
    ocr_metrics = None
    device = None

    def save():
        if destination is not None:
            (destination / "worker_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        root, output, source = Path(run_root).resolve(), Path(output_dir).resolve(), Path(d1_root).resolve()
        if not valid_endpoint(endpoint):
            raise ValueError("skill_worker_endpoint_not_authorized")
        progress = json.loads((root / "progress.json").read_text(encoding="utf-8"))
        if progress.get("run_session_id") != root.name or progress.get("device") != endpoint:
            raise ValueError("run_device_binding_mismatch")
        if output == root or not output.is_relative_to(root) or output.is_relative_to(source):
            raise ValueError("output_must_be_new_u1_run_subdirectory")
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("turn_id_required")
        catalogs = {"resolver_catalog": resolver_catalog, "planner_catalog": planner_catalog,
                    "external_skill_db_path": external_skill_db_path}
        catalogs = {key: str(Path(value).resolve()) for key, value in catalogs.items()}
        if any(not Path(value).is_file() for value in catalogs.values()):
            raise ValueError("explicit_skill_data_missing")
        readonly_saved=None
        if resume_readonly_prepare is not None:
            prior_path=Path(resume_readonly_prepare).resolve()
            prior_turn=f"{root.name}_turn{progress['next_turn']}_terminal_{prior_path.parent.name}"
            readonly_saved=readonly_prepare_failure_state(prior_path,run_root=root,endpoint=endpoint,turn_id=prior_turn)
            if readonly_saved is None:raise ValueError('readonly_skill_prepare_recovery_unverified')
        saved_return = (confirmed_return_state(output / "worker_report.json", run_root=root,
            endpoint=endpoint, turn_id=turn_id) if resume_confirmed_return else None)
        if resume_confirmed_return and saved_return is None:
            raise ValueError("confirmed_terminal_return_missing")
        if saved_return is None:
            output.mkdir(parents=True, exist_ok=False)
        else:
            original = output / "worker_report.before_return_recovery.json"
            if not original.exists():
                original.write_text(json.dumps(saved_return, ensure_ascii=False, indent=2), encoding="utf-8")
            report = dict(saved_return)
            report.update(ok=False, return_recovery={"original_report": str(original),
                "original_reason": saved_return.get("reason"), "purchase_replayed": False})
        destination = output
        report.update(run_session_id=root.name, turn_index=progress.get("next_turn"), run_root=str(root), output_dir=str(output),
                      d1_root=str(source), stage="loading", reason="in_progress")
        save()
        api = _load_d1(source, endpoint=endpoint)
        if hasattr(api, "bind_transport"):
            api.bind_transport(endpoint)
        ocr_metrics = ocr_stack.enter_context(api.ocr_session())

        class SkillsOnlyDevice(api.Device):
            def step(self, action_id, stop_event=None):
                if action_id not in SKILL_STEPS:
                    raise RuntimeError("skill_worker_action_forbidden:" + str(action_id))
                return super().step(action_id, stop_event)

            def close_completed(self, *args, **kwargs):
                raise RuntimeError("skill_worker_action_forbidden:close_completed")

        device = SkillsOnlyDevice(endpoint, adb_path, output / "capture", expected_endpoint=endpoint)
        device.connect()
        if readonly_saved is not None:
            current=device.capture()
            points=readonly_saved['before']['current_skill_points']
            if current.get('current_skill_points')!=points:
                raise ValueError('readonly_prepare_points_changed')
            if current.get('page')=='skills':
                report['readonly_prepare_recovery']={'prior_worker':str(resume_readonly_prepare),
                    'purchase_replayed':False,'identity_return':device.inspect_finish_identity()}
                save()
            elif current.get('page')!='finish_ready':
                raise ValueError('readonly_prepare_current_page_unverified')
        if saved_return is not None:
            report.update(stage="verify_terminal", reason="confirmed_return_in_progress")
            save()
            points = report["learning_result"]["remaining_skill_points"]
            report["after"] = _settle_learning_return(device, points, report, save,
                expected_endpoint=endpoint)
            report.update(ok=True, stage="complete", reason=report["learning_result"]["reason"])
            save()
            return report
        before, identity = _terminal(
            device, api, expected_endpoint=endpoint, prior_identity=prior_identity,
        )
        report.update(before=before, finish_identity=identity)
        policy_dir = root / "skill_policy"
        race_policy = policy_dir / "race_build_context.json"
        target_helper = _prerace_helper()._target_policy()
        arc_policy = target_helper.policy_from_run(root)
        preference = None
        if race_policy.is_file():
            context = json.loads(race_policy.read_text(encoding="utf-8-sig"))
            if (context.get("target_mode") != "race" or context.get("race_profile", {}).get("style")
                    not in ({None, "any", "leader", "front", "betweener", "chaser"} if arc_policy["enabled"] else {"leader", "front", "betweener", "chaser"})):
                raise ValueError("terminal_race_policy_invalid")
            context["turn_id"] = turn_id
            context.setdefault("policy", {})["max_purchase_count"] = 20
            preference = policy_dir / ("speed_ge1200.preference.json"
                if type(current_speed) is int and current_speed >= 1200 else "speed_lt1200.preference.json")
            if not preference.is_file():
                if not arc_policy["enabled"]:
                    raise ValueError("terminal_race_preference_missing")
                preference = None
        else:
            context = api.build_harvest_context(identity, turn_id)
            if context.get("reason") or context.get("finish_identity") != identity:
                raise ValueError("terminal_build_context_unverified")
        if arc_policy["enabled"]:
            context.setdefault("current_race_profile", {"distance_type": "middle",
                "style": context.get("race_profile", {}).get("style", "any")})
            context = target_helper.target_context(context, arc_policy)
            context["larc_year"] = 3
            context["terminal_build"] = True
            context["policy"]["max_purchase_count"] = 20
            if arc_policy["auto_cm"]:
                context["policy"].pop("reserve_skill_pt", None)
                context["policy"].pop("trigger_budget", None)
            context["current_stats"] = target_helper.read_current_stats(root, progress,
                context.get("current_stats"), before.get("current_stats"), {"speed": current_speed})
        elif type(current_speed) is int:
            context["current_stats"] = {**(context.get("current_stats") or {}), "speed": current_speed}
        use_race_policy = preference is not None or arc_policy["enabled"]
        context.update(catalogs)
        report.update(context=context, preference_path=str(preference) if preference else None)
        device.context.update(mode="harvest", harvest_active=True, harvest_summary=identity)
        report.update(stage="open_skills_pending", input_may_have_occurred=True)
        save()
        opened = device.step("open_skills")
        report["open_skills_receipt"] = opened
        if (opened.get("performed") is not True or opened.get("action_id") != "open_skills"
                or opened.get("observation", {}).get("page") != "skills"):
            raise ValueError("skill_entry_not_verified")
        report["stage"] = "learning_pending"
        save()
        learned = (_learn_race_skills(api, device, output / "transaction", context, preference,
                                    before["current_skill_points"], expected_endpoint=endpoint) if use_race_policy else
            api.learn_harvest_skills(device, output / "transaction", context,
                execute=True, purchase_allowed=True, expected_endpoint=endpoint))
        report["learning_result"] = learned
        empty = learned.get("reason") in {"no_harvest_eligible_skills_return_required", "no_purchase_return_verified"}
        if use_race_policy:
            if learned.get("ok") is not True or learned.get("reason") not in {
                    "no_purchase_return_verified", "race_skill_learning_verified"}:
                raise ValueError("race_learning_failed:" + str(learned.get("reason")))
            returned = learned
            proof = learned.get("return_after_learning_report_path")
        elif empty:
            report["stage"] = "no_purchase_return_pending"
            save()
            returned = api.return_after_no_purchase_skills(device, output / "no_purchase_return",
                learned["no_purchase_evidence_path"], learned["no_purchase_evidence_sha256"],
                execute=True, expected_endpoint=endpoint)
            report["no_purchase_return_result"] = returned
            if (returned.get("ok") is not True or returned.get("return_performed") is not True
                    or returned.get("reason") != "no_purchase_return_verified"):
                raise ValueError("no_purchase_return_not_verified")
            proof = returned.get("no_purchase_return_report_path")
        else:
            if learned.get("ok") is not True or learned.get("reason") != "harvest_skill_learning_verified":
                raise ValueError("learning_failed:" + str(learned.get("reason")))
            returned = learned
            proof = learned.get("return_after_learning_report_path")
        proof = Path(proof).resolve()
        if not proof.is_relative_to(output) or not proof.is_file():
            raise ValueError("return_proof_missing_or_outside_batch")
        report.update(return_proof_path=str(proof),
                      return_proof_sha256=hashlib.sha256(proof.read_bytes()).hexdigest(),
                      stage="verify_terminal")
        save()
        points = returned.get("remaining_skill_points")
        if type(points) is not int or points < 0:
            raise ValueError("learning_return_points_missing")
        after = _settle_learning_return(device, points, report, save, expected_endpoint=endpoint)
        report["after"] = after
        report.update(ok=True, stage="complete", reason=returned["reason"])
    except Exception as exc:
        report.update(ok=False, reason=f"{type(exc).__name__}:{exc}")
    finally:
        if isinstance(ocr_metrics, dict):
            report["ocr_metrics"] = {key: ocr_metrics.get(key, 0)
                for key in ("rapid_calls", "cache_hits", "tesseract_calls")}
        if device is not None:
            report["completion_frame_waits"] = getattr(device, "completion_frame_waits", [])
        ocr_stack.close()
        if report.get("ok") is True and report.get("stage") == "complete":
            try:
                import importlib.util
                cache_path = Path(__file__).resolve().parents[1] / "core/auto_trainer/larc_policy.py"
                spec = importlib.util.spec_from_file_location("_larc_skill_opportunity_cache", cache_path)
                cache = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cache)
                report["opportunity_cache"] = cache.cache_worker_skill_opportunities(root, report)
                save()
            except Exception as exc:
                report["opportunity_cache"] = {"status": "unknown", "reason": type(exc).__name__}
        save()
    save()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("d1-root", "run-root", "output-dir", "resolver-catalog", "planner-catalog", "external-skill-db-path"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--turn-id", required=True)
    parser.add_argument("--endpoint", default=ENDPOINT)
    parser.add_argument("--adb-path")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--current-speed", type=int)
    parser.add_argument("--prior-identity-json")
    parser.add_argument("--resume-confirmed-return", action="store_true")
    parser.add_argument("--resume-readonly-prepare", type=Path)
    parsed = vars(parser.parse_args())
    prior_raw = parsed.pop("prior_identity_json", None)
    parsed["prior_identity"] = json.loads(prior_raw) if prior_raw else None
    report = run_skill_batch(**parsed)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] or report["reason"] == "dry_run_no_device_actions" else 2


if __name__ == "__main__":
    raise SystemExit(main())
