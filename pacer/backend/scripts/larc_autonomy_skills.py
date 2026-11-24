"""Own isolated D1 skill batches; finishing remains the L'Arc route's job."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from core.skill_acquisition_parser import resolve_tesseract_runtime
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json
from scripts.larc_d1_prerace_skill_worker import skill_ocr_environment, valid_endpoint
from core.auto_trainer.controlled_process import run_controlled_process
from scripts.larc_d1_skill_worker import confirmed_return_state, readonly_prepare_failure_state


def run_larc_terminal_skills(*, run_root, output_dir, d1_root, external_skill_db_path, adb_port):
    root, output, d1 = (Path(path).resolve() for path in (run_root, output_dir, d1_root))
    report = {"schema_version": "larc_terminal_skills.v1", "run_session_id": root.name,
              "device": f"127.0.0.1:{adb_port}", "ready_to_finish": False,
              "stop_reason": "terminal_skills_not_started", "batches": []}
    destination = None
    pending_return = None
    completed_worker = None
    readonly_recovery = None
    try:
        progress = json.loads((root / "progress.json").read_text(encoding="utf-8"))
        if (type(adb_port) is not int or not valid_endpoint(f"127.0.0.1:{adb_port}") or progress.get("run_session_id") != root.name
                or progress.get("device") != report["device"]):
            raise ValueError("terminal_skill_run_device_mismatch")
        report["turn_index"] = progress["next_turn"]
        state_path = root / "session/larc_terminal_skills.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if (state.get("run_session_id") != root.name or state.get("device") != report["device"]
                    or state.get("turn_index") != report["turn_index"]):
                raise ValueError("terminal_skill_session_mismatch")
            if state.get("status") != "ready":
                worker_path = Path(state.get("worker_report_path", "")).resolve()
                expected_turn_id = f"{root.name}_turn{report['turn_index']}_terminal_{worker_path.parent.name}"
                candidate = json.loads(worker_path.read_text(encoding="utf-8")) if worker_path.is_file() else {}
                if (candidate.get("ok") is True and candidate.get("stage") == "complete"
                        and worker_path.is_relative_to(root)
                        and candidate.get("run_session_id") == root.name
                        and candidate.get("endpoint") == report["device"]
                        and candidate.get("turn_id") == expected_turn_id
                        and candidate.get("output_dir") == str(worker_path.parent)):
                    completed_worker = candidate
                elif confirmed_return_state(worker_path, run_root=root, endpoint=report["device"],
                        turn_id=expected_turn_id) is None:
                    if readonly_prepare_failure_state(worker_path,run_root=root,endpoint=report['device'],
                            turn_id=expected_turn_id) is None:
                        report["stop_reason"] = "terminal_skill_pending_requires_review"
                        return report, 2
                    readonly_recovery=worker_path
                if readonly_recovery is None:pending_return = worker_path.parent
            original = Path(state["report_path"]).resolve(strict=True)
            if not original.is_relative_to(root):
                raise ValueError("terminal_skill_report_outside_run")
            saved = json.loads(original.read_text(encoding="utf-8"))
            if ((pending_return is None and readonly_recovery is None and saved.get("ready_to_finish") is not True)
                    or saved.get("run_session_id") != root.name
                    or saved.get("device") != report["device"] or saved.get("turn_index") != report["turn_index"]):
                raise ValueError("terminal_skill_ready_report_mismatch")
            if pending_return is None and readonly_recovery is None:
                return saved, 0
            report=saved
            if readonly_recovery is not None:
                output=root/f"s{len(report['batches'])+1}"
                report.setdefault('readonly_prepare_recoveries',[]).append({
                    'prior_worker':str(readonly_recovery),'prior_report':str(original),'purchase_replayed':False})
            else:output=original.parent
        if os.name=='nt' and len(str(output))>180 and pending_return is None:
            output=root/'s'
        if output == root or not output.is_relative_to(root) or output.is_relative_to(d1):
            raise ValueError("terminal_skill_output_outside_run")
        catalogs = d1 / "data/skills/cn-20260916"
        from core.auto_trainer.larc_target_skills import policy_from_run, summarize_run_skill_pt
        arc_policy = policy_from_run(root)
        cm_spenddown = arc_policy["enabled"] and (arc_policy["auto_cm"] or arc_policy.get("mode") == "tiered_ladder")
        if arc_policy["enabled"]:
            catalogs = Path(__file__).resolve().parents[1] / "data/skills/cn_larc_end"
        resolver, planner = (catalogs / name for name in (
            "cn_skill_resolver_catalog.json", "cn_skill_planner_catalog.json"))
        database = Path(external_skill_db_path).resolve(strict=True)
        if not all(path.is_file() for path in (resolver, planner, database)):
            raise ValueError("terminal_skill_catalog_missing")
        runtime = resolve_tesseract_runtime()
        child_env = skill_ocr_environment(os.environ, executable=runtime.executable)
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        if pending_return is None:
            output.mkdir(parents=True, exist_ok=False)
        destination = output / "report.json"
        report["report_path"] = str(destination)
        previous_points, identity = report.get("remaining_skill_points"), report.get("finish_identity")
        while True:
            batch = pending_return or output / f"batch_{len(report['batches']) + 1:03d}"
            turn_id = f"{root.name}_turn{report['turn_index']}_terminal_{batch.name}"
            state = {"run_session_id": root.name, "device": report["device"],
                     "turn_index": report["turn_index"], "status": "pending",
                     "report_path": str(destination), "worker_report_path": str(batch / "worker_report.json")}
            _atomic_write_json(state_path, state)
            _atomic_write_json(destination, report)
            argv = [sys.executable, "-B", str(Path(__file__).with_name("larc_d1_skill_worker.py").resolve()),
                "--d1-root", str(d1), "--run-root", str(root), "--output-dir", str(batch),
                "--turn-id", turn_id, "--resolver-catalog", str(resolver), "--planner-catalog", str(planner),
                "--external-skill-db-path", str(database), "--endpoint", report["device"], "--execute"]
            if pending_return is not None:
                argv.append("--resume-confirmed-return")
            if readonly_recovery is not None:
                argv[-1:-1]=['--resume-readonly-prepare',str(readonly_recovery)]
            worker_path = batch / "worker_report.json"
            if completed_worker is not None:
                worker, child_code = completed_worker, 0
                completed_worker = None
                report["completed_pending_worker_reused"] = str(worker_path)
            else:
                process_path = output / f"{batch.name}_process.json"
                if pending_return is not None and process_path.is_file():
                    _atomic_write_json(output / f"{batch.name}_process.before_resume_{time.time_ns()}.json",
                        json.loads(process_path.read_text(encoding="utf-8")))
                child = run_controlled_process(argv, cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", env=child_env)
                child_code = child.returncode
                _atomic_write_json(output / f"{batch.name}_process.json", {
                    "returncode": child.returncode, "stdout": child.stdout, "stderr": child.stderr})
                worker = json.loads(worker_path.read_text(encoding="utf-8"))
            if str(worker_path) not in report["batches"]:
                report["batches"].append(str(worker_path))
            if (child_code or worker.get("ok") is not True or worker.get("stage") != "complete"):
                raise ValueError("terminal_skill_worker_failed:" + str(worker.get("reason")))
            readonly_recovery=None
            proof = Path(worker.get("return_proof_path", "")).resolve()
            if (worker.get("run_session_id") != root.name or worker.get("endpoint") != report["device"]
                    or worker.get("turn_id") != turn_id or worker.get("output_dir") != str(batch)
                    or not proof.is_relative_to(batch) or not proof.is_file()):
                raise ValueError("terminal_skill_worker_context_mismatch")
            before = worker.get("before", {}).get("current_skill_points")
            after = worker.get("after", {}).get("current_skill_points")
            no_purchase = worker.get("reason") == "no_purchase_return_verified"
            if (type(before) is not int or type(after) is not int or after < 0
                    or not worker.get("finish_identity")
                    or worker.get("reason") not in {"no_purchase_return_verified",
                        "harvest_skill_learning_verified", "race_skill_learning_verified"}):
                raise ValueError("terminal_skill_points_or_identity_mismatch")
            diagnostics = {}
            if previous_points is not None and before != previous_points:
                diagnostics["previous_points_reading"] = {"previous":previous_points,"current":before}
            if identity is not None and worker["finish_identity"] != identity:
                diagnostics["finish_identity_reading_changed"] = True
            if (after != before if no_purchase else after >= before):
                diagnostics["points_reading_did_not_decrease"] = {"before":before,"after":after}
            if diagnostics:
                report.setdefault("diagnostics", []).append(diagnostics)
            report.update(remaining_skill_points=after, finish_identity=worker["finish_identity"],
                          final_worker_report_path=str(worker_path), after=worker["after"])
            pending_return = None
            small_remainder = after <= 20 and not cm_spenddown
            if no_purchase or after == 0 or small_remainder or "points_reading_did_not_decrease" in diagnostics:
                report.update(ready_to_finish=True, stop_reason="terminal_skills_ready",
                              ready_reason=("no_purchase" if no_purchase else
                                  "zero_points" if after == 0 else "confirmed_learning_remainder_retained"
                                  if "points_reading_did_not_decrease" in diagnostics else "small_remainder_retained"))
                _atomic_write_json(destination, report)
                report["skill_pt_ledger"] = summarize_run_skill_pt(root, current_pt=after)
                _atomic_write_json(destination, report)
                _atomic_write_json(state_path, dict(state, status="ready"))
                return report, 0
            previous_points, identity = after, worker["finish_identity"]
    except Exception as exc:
        report["stop_reason"] = f"terminal_skill_stopped:{type(exc).__name__}:{exc}"
    if destination is not None:
        _atomic_write_json(destination, report)
    return report, 2


def run_larc_terminal_settlement(*, run_root, output_dir, d1_root, adb_port):
    """Only normal result pages; the child cannot learn, start, or reroll."""
    root, output = Path(run_root).resolve(), Path(output_dir).resolve()
    if os.name=='nt' and len(str(output))>180:
        output=root/'z'
    progress = json.loads((root / "progress.json").read_text(encoding="utf-8"))
    endpoint = f"127.0.0.1:{adb_port}"
    if type(adb_port) is not int or not valid_endpoint(f"127.0.0.1:{adb_port}") or progress.get("device") != endpoint or progress.get("run_session_id") != root.name:
        return {"ok":False,"reason":"terminal_settlement_device_mismatch"}, 2
    state_path = root / "session/larc_terminal_settlement.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        original = Path(state["report_path"]).resolve()
        if (state.get("run_session_id") != root.name or state.get("device") != endpoint
                or state.get("turn_index") != progress["next_turn"] or not original.is_relative_to(root)):
            return {"ok":False,"reason":"terminal_settlement_context_mismatch"}, 2
        output = original.parent
        if state.get("status") == "complete":
            saved = json.loads(original.read_text(encoding="utf-8"))
            if (saved.get("ok") is True and saved.get("final_page") == "home"
                    and saved.get("run_session_id") == root.name and saved.get("endpoint") == endpoint
                    and saved.get("turn_index") == progress["next_turn"]):
                return {**saved,"report_path":str(original)}, 0
            return {"ok":False,"reason":"terminal_settlement_completed_report_mismatch"}, 2
    runtime = resolve_tesseract_runtime()
    env = skill_ocr_environment(os.environ, executable=runtime.executable)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [sys.executable, "-B", str(Path(__file__).with_name("larc_d1_settlement_worker.py")),
        "--d1-root", str(d1_root), "--run-root", str(root), "--output-dir", str(output),
        "--endpoint", f"127.0.0.1:{adb_port}", "--execute"]
    process_path = output / "process.json"
    if state_path.exists() and process_path.is_file():
        _atomic_write_json(output / f"process.before_resume_{time.time_ns()}.json",
            json.loads(process_path.read_text(encoding="utf-8")))
    child = run_controlled_process(argv, cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env)
    _atomic_write_json(output / "process.json", {"returncode": child.returncode,
        "stdout": child.stdout, "stderr": child.stderr})
    path = output / "report.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["report_path"] = str(path)
    valid = (child.returncode == 0 and report.get("ok") is True
        and report.get("run_session_id") == root.name
        and report.get("endpoint") == f"127.0.0.1:{adb_port}"
        and report.get("final_page") == "home")
    return report, 0 if valid else 2
