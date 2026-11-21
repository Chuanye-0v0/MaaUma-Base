"""Target-aware pre-Arc learning using the existing isolated D1 skill owner."""
import json
import os
from pathlib import Path
import sys
from time import monotonic

import cv2

from core.auto_trainer.config import load_goal_config
from core.auto_trainer.controlled_process import run_controlled_process
from core.auto_trainer.larc_custom_race_catalog import resolve_configured_running_style
from core.auto_trainer.larc_pages import is_larc_race_day, larc_race_day_controls
from core.auto_trainer.main_menu_state_provider import _CALENDAR_REGION_READER
from core.auto_trainer.safe_advance import SafeAdvancePlanner
from core.skill_acquisition_parser import inspect_skill_acquisition_page, resolve_tesseract_runtime
from scripts.larc_autonomy_support_skills import StartupNavigation
from scripts.larc_d1_prerace_skill_worker import SCROLL_ACTIONS, skill_ocr_environment
from scripts.larc_skill_completion_frames import success_close_box
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def arc_year(context):
    date = context.get("period", {}).get("value") or {}
    year = date.get("year_index")
    target = {2: "larc_arc_participation", 3: "larc_final_arc_top5"}.get(year)
    return year if (context.get("status") == "verified" and target is not None
                    and context.get("target_id") == target
                    and date.get("month") == 10 and date.get("half") == "first") else None


class ArcSkillNavigation(StartupNavigation):
    def open(self, source, folder):
        image = cv2.imread(str(source))
        if image is None or image.shape[:2] != (1280, 720):
            raise ValueError("arc_skill_source_unreadable")
        packet = self.safety.analyze(source)
        if not is_larc_race_day(image, {"screen_text": {"normalized_text": packet.visible_text}}):
            raise ValueError("arc_skill_not_race_day")
        label, confidence = _CALENDAR_REGION_READER.read_line(image[1080:1130, 60:155])
        if label != "技能" or confidence < .95:
            raise ValueError("arc_skill_entry_not_found")
        self.tap(source, (108, 1095), "main_menu", "arc.skills.open", ("skill_acquisition", "loading"))
        for _ in range(12):
            path, frame = self.capture(folder)
            if inspect_skill_acquisition_page(frame).get("accepted"):
                sha = self.observe(path, "skill_acquisition")
                self.route.commit_action("arc.skills.worker_handoff",
                    expected_next_states=("main_menu", "skill_acquisition", "loading", "modal"), source_sha256=sha)
                self.route.save(self.ctx.route)
                return True
            self.sleep(.2)
        raise ValueError("arc_skill_entry_not_settled")

    def return_race_day(self, folder, *, worker_returned=False):
        """Close/back only; never selects or confirms a skill purchase."""
        closed = False
        backed = worker_returned
        for _ in range(20):
            path, frame = self.capture(folder)
            if larc_race_day_controls(frame):
                packet = self.safety.analyze(path)
                if is_larc_race_day(frame, {"screen_text": {"normalized_text": packet.visible_text}}):
                    self.observe(path, "main_menu", resync=True)
                    return str(path)
            box = success_close_box(frame)
            if box is not None and not closed:
                self.observe(path, "modal", resync=True)
                self.tap(path, ((box[0]+box[2])//2, (box[1]+box[3])//2), "modal",
                    "arc.skills.close_success", ("skill_acquisition", "main_menu", "loading", "modal"))
                closed = True
            elif inspect_skill_acquisition_page(frame).get("accepted") and not backed:
                point = SafeAdvancePlanner._skill_acquisition_back_button(frame) or SafeAdvancePlanner._skill_acquisition_back_control(frame)
                if point is None:
                    raise ValueError("arc_skill_back_control_missing")
                self.observe(path, "skill_acquisition", resync=True)
                self.tap(path, point, "skill_acquisition", "arc.skills.back", ("main_menu", "loading"))
                backed = True
            self.sleep(.2)
        raise ValueError("arc_skill_return_not_settled")


def launch_arc_worker(ctx, output, request):
    catalogs = Path(__file__).resolve().parents[1]/"data/skills/cn_larc_end"
    runtime = resolve_tesseract_runtime()
    env = skill_ocr_environment(os.environ, executable=runtime.executable)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [sys.executable, "-B", str(Path(__file__).with_name("larc_d1_prerace_skill_worker.py")),
        "--purpose", "prerace", "--year", str(request["year"]), "--d1-root", str(ctx.d1_skill_root),
        "--run-root", str(ctx.root), "--output-dir", str(output),
        "--resolver-catalog", str(catalogs/"cn_skill_resolver_catalog.json"),
        "--planner-catalog", str(catalogs/"cn_skill_planner_catalog.json"),
        "--external-skill-db-path", str(ctx.external_skill_db_path), "--endpoint", ctx.progress["device"], "--execute"]
    if request.get("style"):
        argv += ["--run-style", request["style"]]
    if request.get("resume_confirmed_from"):
        argv += ["--resume-confirmed-from", request["resume_confirmed_from"]]
    child = run_controlled_process(argv, cwd=str(ctx.root), capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", env=env)
    _atomic_write_json(Path(output).parent/(Path(output).name+"_process.json"),
        {"returncode": child.returncode, "stdout": child.stdout, "stderr": child.stderr})
    path = Path(output)/"worker_report.json"
    return read(path) if path.is_file() else {"ok": False, "reason": "arc_worker_report_missing"}


def _worker_bound(ctx, result, year):
    return (result.get("run_session_id") == ctx.run_id and result.get("endpoint") == ctx.progress["device"]
            and result.get("turn_index") == ctx.progress["next_turn"] and result.get("year") == year
            and result.get("purpose") == "prerace")


def _zero_purchase_preparation(ctx, pending, result, year):
    """A failed read-only scan is not an uncertain purchase transaction."""
    prepared = result.get("preparation") or {}
    if (not _worker_bound(ctx, result, year) or result.get("ok") is not False
            or result.get("stage") != "preparing" or result.get("returned") is not False
            or result.get("purchase_may_have_occurred") is not False
            or prepared.get("execution_allowed") is not False
            or prepared.get("purchase_executed") is not False or prepared.get("tap_count") != 0
            or type(result.get("before", {}).get("current_skill_points")) is not int
            or result["before"]["current_skill_points"] < 0
            or any(key in result for key in ("selected_action_ids", "selection", "learning_result"))
            or any((pending/name).exists() for name in
                ("selection", "confirmation", "success_close", "return_after_learning"))):
        return None
    manifest = prepared.get("capture_manifest") or {}
    if manifest.get("purchase_executed") is not False or manifest.get("tap_count") != 0:
        return None
    receipts = list(pending.rglob("*receipt*.json"))
    for path in receipts:
        receipt = read(path)
        if (receipt.get("action_id") not in SCROLL_ACTIONS
                or receipt.get("endpoint") != ctx.progress["device"]
                or receipt.get("performed") is not True or receipt.get("status") != "observed"
                or receipt.get("input_may_have_occurred") or receipt.get("purchase_may_have_occurred")):
            return None
    return {"previous_worker": str(pending), "scroll_receipts_checked": len(receipts),
            "expected_skill_points": result["before"]["current_skill_points"]}


def _observe_preparation_retry(ctx, pending, result, year, output, navigation):
    proof = _zero_purchase_preparation(ctx, pending, result, year)
    if proof is None:
        return None
    if f"127.0.0.1:{getattr(ctx, 'port', None)}" != ctx.progress["device"]:
        raise ValueError("arc_prepare_retry_endpoint_mismatch")
    output.mkdir(parents=True, exist_ok=True)
    path, frame = navigation.capture(output)
    page = inspect_skill_acquisition_page(frame)
    if (page.get("accepted") is not True
            or SafeAdvancePlanner._looks_like_confirm_modal_panel(frame) or success_close_box(frame) is not None):
        raise ValueError("arc_prepare_retry_current_skill_page_unverified")
    if page.get("current_skill_points") != proof["expected_skill_points"]:
        raise ValueError("arc_prepare_retry_points_mismatch")
    small = cv2.resize(frame, (720, 1280))
    hsv = cv2.cvtColor(small[1050:1115, 230:490], cv2.COLOR_BGR2HSV)
    disabled = float(((hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] >= 130)
        & (hsv[:, :, 2] >= 90) & (hsv[:, :, 2] < 165)).mean()) > .7
    if not disabled:
        raise ValueError("arc_prepare_retry_cart_not_empty")
    navigation.observe(path, "skill_acquisition", resync=True)
    proof.update(source=str(path), observed_skill_points=page["current_skill_points"],
        skill_cart_disabled=True, purchase_replayed=False, old_selected_actions_reused=False, turn_consumed=False)
    _atomic_write_json(output/"preparation_retry_observation.json", proof)
    return proof


def _record_arc_call_timing(ctx, output_dir, started, report, *, entrypoint, raised):
    """Pacer-call duration only; missing worker-stage clocks stay unknown."""
    if report is None and not raised:
        return  # Not an applicable skill call; do not manufacture an episode.
    timing = {"elapsed_seconds": max(0., monotonic() - started),
        "timing_scope": "pacer_call_inclusive", "stage_elapsed_seconds": None}
    if isinstance(report, dict):
        report.update(timing)
    try:
        root = Path(ctx.root).resolve(strict=True)
        output = Path(output_dir).resolve()
        if (ctx.run_id != root.name or output == root or not output.is_relative_to(root)):
            return
        _atomic_write_json(output / (entrypoint + "_timing.json"), {
            "schema_version": "uma.pacer_skill_call_timing.v1",
            "run_session_id": ctx.run_id, "device": ctx.progress.get("device"),
            "entrypoint": entrypoint, "outcome": "raised" if raised else "returned",
            **timing})
    except Exception:
        # Telemetry cannot change a return, mask an original exception, or
        # create a new execution/recovery gate when an output cannot be saved.
        pass


def prepare_arc_skills(ctx, race_context, source, output_dir, *, navigation_factory=ArcSkillNavigation,
                       worker=launch_arc_worker):
    started = monotonic()
    report = None
    raised = False
    try:
        report = _prepare_arc_skills(ctx, race_context, source, output_dir,
            navigation_factory=navigation_factory, worker=worker)
        return report
    except BaseException:
        raised = True
        raise
    finally:
        _record_arc_call_timing(ctx, output_dir, started, report,
            entrypoint="prepare_arc_skills", raised=raised)


def _prepare_arc_skills(ctx, race_context, source, output_dir, *, navigation_factory,
                        worker):
    goal = load_goal_config(ctx.root/"goal.json")
    policy = goal.skill_timing.arc_policy
    year = arc_year(race_context)
    if not policy.get("enabled") or year is None or getattr(ctx, "cut_farming_hook", None) is not None:
        return None
    if not getattr(ctx, "terminal_skill_enabled", True):
        return {"ok": True, "status": "disabled_by_execution_options", "year": year}
    if year == 2 and not policy.get("classic_allowlist") and not policy.get("auto_cm", True):
        return {"ok": True, "status": "classic_allowlist_empty", "year": year, "turn_consumed": False}
    if not getattr(ctx, "d1_skill_root", None) or not getattr(ctx, "external_skill_db_path", None):
        return {"ok": False, "status": "skill_driver_not_configured", "year": year}
    ledger_path = ctx.root/"session/larc_arc_skills.json"
    ledger = read(ledger_path) if ledger_path.is_file() else {
        "schema_version": "larc_arc_skills.v1", "run_session_id": ctx.run_id, "device": ctx.progress["device"], "stages": {}}
    if ledger.get("run_session_id") != ctx.run_id or ledger.get("device") != ctx.progress["device"]:
        raise ValueError("arc_skill_session_mismatch")
    key = str(year)
    stage = ledger["stages"].get(key)
    if stage and stage.get("status") == "complete":
        return {"ok": True, "status": "already_complete", "year": year, "turn_consumed": False}
    if stage and stage.get("turn_index") != ctx.progress["next_turn"]:
        raise ValueError("arc_skill_pending_turn_mismatch")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    if stage is None:
        style, _ = resolve_configured_running_style(goal, visible_text="凯旋门")
        request = {"year": year}
        if style in {"leader", "front", "betweener", "chaser"}:
            request["style"] = style
        stage = {"status": "ready", "turn_index": ctx.progress["next_turn"], "year": year,
                 "request": request,
                 "race_context": race_context, "batches": [], "turn_consumed": False}
        ledger["stages"][key] = stage
        _atomic_write_json(ledger_path, ledger)
    # Classic retains its one-batch budget for later discounts. Senior may use
    # several existing D1 batches, each with the newly observed remaining Pt.
    while year != 2 or not stage["batches"]:
        navigation = navigation_factory(ctx)
        batch = output/f"batch_{len(stage['batches'])+1:02d}"
        pending = stage.get("pending_worker")
        if pending:
            path = Path(pending).resolve()
            if not path.is_relative_to(ctx.root.resolve()):
                raise ValueError("arc_worker_outside_run")
            result = read(path/"worker_report.json") if (path/"worker_report.json").is_file() else {}
            if not _worker_bound(ctx, result, year):
                raise ValueError("arc_skill_worker_context_mismatch")
            if stage.get("preparation_restarts") and _zero_purchase_preparation(ctx, path, result, year) is not None:
                return {"ok": False, "status": "arc_skill_preparation_failed", "reason": result.get("reason"),
                        "year": year, "purchase_may_have_occurred": False}
            retry = _observe_preparation_retry(ctx, path, result, year,
                batch.with_name(batch.name+"_prepare_observe"), navigation)
            if retry is not None:
                fresh = batch.with_name(batch.name+"_reprepare")
                retry["new_worker"] = str(fresh)
                stage.setdefault("preparation_restarts", []).append(retry)
                stage["pending_worker"] = str(fresh)
                _atomic_write_json(ledger_path, ledger)
                result = worker(ctx, fresh, {"year": year,
                    **({"style": stage["request"]["style"]} if stage["request"].get("style") else {})})
                path = fresh
            elif result.get("ok") is not True or result.get("stage") != "complete":
                # The worker verifies the existing final-confirmation receipt;
                # recovery only closes/returns and never selects a new cart.
                recovered = batch.with_name(batch.name+"_return")
                result = {**result, **worker(ctx, recovered,
                    {**stage["request"], "resume_confirmed_from": str(path)})}
            batch = path
        else:
            nav_output = output/f"navigation_{len(stage['batches'])+1:02d}"
            nav_output.mkdir(parents=True, exist_ok=True)
            navigation.open(source, nav_output)
            stage.update(status="pending", pending_worker=str(batch))
            _atomic_write_json(ledger_path, ledger)
            result = worker(ctx, batch, stage["request"])
            retry = _observe_preparation_retry(ctx, batch, result, year,
                batch.with_name(batch.name+"_prepare_observe"), navigation)
            if retry is not None:
                fresh = batch.with_name(batch.name+"_reprepare")
                retry["new_worker"] = str(fresh)
                stage.setdefault("preparation_restarts", []).append(retry)
                stage["pending_worker"] = str(fresh)
                _atomic_write_json(ledger_path, ledger)
                result = worker(ctx, fresh, dict(stage["request"]))
                batch = fresh
        if not _worker_bound(ctx, result, year) or result.get("ok") is not True or result.get("stage") != "complete":
            if _zero_purchase_preparation(ctx, batch, result, year) is not None:
                return {"ok": False, "status": "arc_skill_preparation_failed", "reason": result.get("reason"),
                        "year": year, "purchase_may_have_occurred": False}
            return {"ok": False, "status": "arc_skill_worker_pending", "reason": result.get("reason"), "year": year}
        return_dir = output/f"return_{len(stage['batches'])+1:02d}"
        return_dir.mkdir(parents=True, exist_ok=True)
        source = navigation.return_race_day(return_dir, worker_returned=result.get("returned") is True)
        stage["batches"].append(str(batch/"worker_report.json"))
        stage.pop("pending_worker", None)
        stage.update(status="ready", remaining_skill_points=result.get("remaining_skill_points"))
        _atomic_write_json(ledger_path, ledger)
        points = result.get("remaining_skill_points")
        before = result.get("before", {}).get("current_skill_points")
        if (not result.get("selected_action_ids") or type(points) is not int or type(before) is not int
                or points <= 20 or points >= before):
            break
    stage.update(status="complete", source_after=source)
    _atomic_write_json(ledger_path, ledger)
    return {"ok": True, "status": "complete", "year": year, "batches": stage["batches"],
            "remaining_skill_points": stage.get("remaining_skill_points"), "turn_consumed": False}


def resume_arc_skill_learning(ctx, output_dir):
    started = monotonic()
    report = None
    raised = False
    try:
        path = ctx.root/"session/larc_arc_skills.json"
        if not path.is_file():
            return None
        ledger = read(path)
        for stage in ledger.get("stages", {}).values():
            if stage.get("status") == "pending":
                report = prepare_arc_skills(ctx, stage["race_context"], None, output_dir)
                return report
        return None
    except BaseException:
        raised = True
        raise
    finally:
        _record_arc_call_timing(ctx, output_dir, started, report,
            entrypoint="resume_arc_skill_learning", raised=raised)
