"""One completed capture scan may hand its verified observation back to Safe."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import cv2

from core.auto_trainer.route_handoff import is_route_handoff
from core.main_menu_state import _analyze_numeric_field
from core.training_menu_precheck import TrainingMenuPrechecker
from .capture_adapter import load_capture_group_raws
from .route_transition_session import RouteTransitionError, RouteTransitionStatus
from .state import TRAINING_TYPES
from .training_current_stats import read_training_current_stats


def prepare_capture_observation_handoff(*, report_path, report_sha256,
        invocation_path, invocation_sha256, session, image_path, packet):
    """Validate immutable failed-turn evidence; return an unpublished observation."""
    sources = {}
    def bound(path, sha):
        path = Path(path).resolve()
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise RouteTransitionError("capture_handoff_source_mismatch")
        sources[str(path)] = sha
        return raw

    wrapper = json.loads(bound(report_path, report_sha256))
    report = wrapper["report"]
    invocation = json.loads(bound(invocation_path, invocation_sha256))
    argv = invocation["argv"]
    def option(name):
        if argv.count(name) != 1:
            raise RouteTransitionError("capture_handoff_invocation_invalid")
        return argv[argv.index(name) + 1]

    turn = report["turn_reports"][-1]
    prior = report["turn_reports"][-2]
    stages = turn["report"]["stages"]
    capture, provider = stages["capture"], stages["state_context_provider"]
    baseline = prior["advance"]["report"]["route_transition"]
    group_id = f"{session.run_session_id}_turn_{turn['turn_index']:03d}"
    if (wrapper.get("code") != 2 or report.get("status") != "stopped"
        or report.get("stop_reason") != "training_page_observation_failed"
        or report.get("run_session_id") != session.run_session_id
        or turn.get("code") != 2 or turn["report"].get("is_valid") is not False
        or turn["report"].get("status") != "training_page_observation_failed"
        or set(stages) != {"capture", "state_context_provider", "sidecar", "training_page_observation"}
        or any(key in turn for key in ("training_route_receipt", "rest_route_receipt", "race_route"))
        or capture.get("is_valid") is not True or provider.get("is_valid") is not True
        or stages["training_page_observation"].get("is_valid") is not False
        or turn.get("turn_id") != group_id or capture.get("actual_group_id") != group_id
        or prior.get("turn_index") != turn["turn_index"] - 1
        or session.to_dict() != baseline or session.status != RouteTransitionStatus.TRACKING
        or session.current_state != "main_menu" or session.expected_next_states
        or session.action_source_sha256 is not None or session.mismatch_state is not None
        or option("--mode") != "live" or option("--start-screen") != "main-menu"
        or option("--run-session-id") != session.run_session_id
        or option("--capture-confirm-token") != "CAPTURE_UG_PARENT_LIVE_TABS"
        or "--capture-live" not in argv or "--capture-execute" not in argv
        or not int(option("--start-turn-index")) <= turn["turn_index"] < int(option("--start-turn-index")) + int(option("--max-training-turns"))
        or Path(capture["output_root"]).resolve() != Path(option("--output-root")).resolve() / group_id / "capture"):
        raise RouteTransitionError("capture_handoff_history_invalid")
    manifest_path = Path(capture["manifest_path"])
    manifest = json.loads(bound(manifest_path, stages["training_page_observation"]["source_manifest_sha256"]))
    groups = manifest["groups"]
    if len(groups) != 1:
        raise RouteTransitionError("capture_handoff_group_invalid")
    group = groups[0]
    if (group.get("run_session_id") != session.run_session_id or group.get("actual_group_id") != group_id
        or group.get("source_state_key") != group_id or group.get("turn_index") != turn["turn_index"]):
        raise RouteTransitionError("capture_handoff_group_invalid")
    raws = load_capture_group_raws(manifest_path=manifest_path, execution_root=capture["output_root"],
        actual_group_id=group_id, formal_capture=True)
    if set(raws) != set(TRAINING_TYPES) or len(group["raw_files"]) != len(TRAINING_TYPES):
        raise RouteTransitionError("capture_handoff_scan_incomplete")
    initial = []
    for raw in group["raw_files"]:
        tab = raw["source_screen"].removesuffix("_tab")
        bound(Path(capture["output_root"]) / raw["relative_path"], raw["sha256"])
        bound(raw["source_image_path"], raw["source_image_sha256"])
        if Path(raw["source_image_path"]).resolve() == Path(capture["output_root"]).resolve() / "precheck" / "training_menu.png":
            initial.append(tab)
    if len(initial) != 1:
        raise RouteTransitionError("capture_handoff_initial_tab_unknown")
    last_tab = [tab for tab in TRAINING_TYPES if tab != initial[0]][-1]
    context = manifest["state_context"]
    snapshot = json.loads(bound(context["snapshot_path"], context["snapshot_sha256"]))
    bound(context["source_path"], context["source_sha256"])
    source_sha = context["source_screenshot_sha256"]
    bound(context["source_screenshot_path"], source_sha)
    prior_report = prior["advance"]["report"]
    prior_step = prior_report["step_reports"][-1]
    prior_result = prior_step["result"]
    bound(prior_step["screenshot_path"], source_sha)
    if (prior["advance"].get("code") != 0 or prior_report.get("is_valid") is not True
        or prior_report.get("stop_reason") != "ready_main_menu"
        or prior_result.get("performed") is not False or prior_result.get("tap_performed") is True
        or prior_result.get("reason") != "ready_main_menu"
        or not is_route_handoff(prior_result.get("decision"), reason="ready_main_menu")
        or prior_result["frame_safety"].get("source_image_sha256") != source_sha
        or prior_result["safety_verdict"].get("allowed") is not True
        or prior_result["safety_verdict"].get("action_id") is not None
        or prior_result["safety_verdict"].get("source_image_sha256") != source_sha
        or prior_result.get("route_transition") != baseline):
        raise RouteTransitionError("capture_handoff_prior_main_unverified")
    provider_report = json.loads(Path(context["provider_path"]).read_text(encoding="utf-8"))
    sources[str(Path(context["provider_path"]).resolve())] = hashlib.sha256(Path(context["provider_path"]).read_bytes()).hexdigest()
    if (source_sha != session.observation_sha256 or source_sha != provider["source_screenshot_sha256"]
        or source_sha != provider_report["source"]["sha256"] or provider_report.get("valid_for_training") is not True
        or snapshot.get("turn_index") != turn["turn_index"]
        or snapshot["current_stats"] != provider["report"]["state_context"]["current_stats"]
        or snapshot["remaining_turns"] != provider["report"]["state_context"]["remaining_turns"]
        or snapshot["current_stats"] != provider_report["state_context"]["current_stats"]
        or snapshot["remaining_turns"] != provider_report["state_context"]["remaining_turns"]):
        raise RouteTransitionError("capture_handoff_provider_mismatch")
    image = cv2.imread(str(image_path))
    check = TrainingMenuPrechecker().check(image_path)
    if (packet.text_status != "ok" or packet.errors or packet.dangerous or packet.resource_terms
        or hashlib.sha256(Path(image_path).read_bytes()).hexdigest() != packet.source_image_sha256
        or packet.source_image_sha256 == source_sha or not check.accepted or check.selected_tab != last_tab):
        raise RouteTransitionError("capture_handoff_fresh_training_unverified")
    current = read_training_current_stats(image)
    remaining = _analyze_numeric_field("remaining_turns", image)
    if (current["status"] != "ok" or current["values"] != snapshot["current_stats"]
        or remaining.get("abstained") is not False or type(remaining.get("value")) is not int
        or remaining["value"] != snapshot["remaining_turns"]):
        raise RouteTransitionError("capture_handoff_current_values_changed")
    candidate = replace(session, current_state="training_menu", observation_sha256=packet.source_image_sha256,
        transition_version=session.transition_version + 1)
    candidate._validate_invariants()
    return candidate, {"kind": "capture_observation_handoff", "new_tap": False, "new_wait": False,
        "capture_tap_receipt": "unavailable", "source_files": sources,
        "source_frame_sha256": packet.source_image_sha256, "run_session_id": session.run_session_id,
        "turn_index": turn["turn_index"], "last_scanned_tab": last_tab,
        "old_session": session.to_dict(), "observed_session": candidate.to_dict(),
        "current_stats": current, "remaining_turns": remaining}
