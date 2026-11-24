"""One pre-Arc skill batch from an already-open skill page, using D1 modules.

The caller owns entering/leaving the game flow and supplies the current race
style. This worker never pretends that a race-day page is a finish page.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

ENDPOINT = "127.0.0.1:16448"
def valid_endpoint(endpoint):
    """The caller binds this selected loopback endpoint to its saved run."""
    match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", endpoint) if isinstance(endpoint, str) else None
    return match is not None and 1 <= int(match.group(1)) <= 65535

SCROLL_ACTIONS = frozenset({"scroll_skill_list", "scroll_skill_reveal_bottom",
                            "scroll_skill_overlap", "scroll_skill_up"})
STYLES = ("leader", "front", "betweener", "chaser")


_TESSERACT_HEALTH_CACHE: dict[str, bool] = {}


def _tesseract_executable_healthy(path: Path) -> bool:
    key = str(path)
    cached = _TESSERACT_HEALTH_CACHE.get(key)
    if cached is not None:
        return cached
    ok = False
    if path.is_file() and path.stat().st_size > 4096:
        try:
            import subprocess
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            res = subprocess.run([str(path), "--version"], capture_output=True, timeout=5, creationflags=flags)
            ok = res.returncode == 0
        except Exception:
            ok = False
    _TESSERACT_HEALTH_CACHE[key] = ok
    return ok


def skill_ocr_environment(base_env, *, executable=None):
    """Pass an installed Chinese OCR dataset to the isolated D1 process."""
    env = dict(base_env)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env['UMA_ISOLATED_SKILL_WORKER'] = '1'
    bundled = Path(__file__).resolve().parents[2] / 'ocr/tesseract.exe'
    explicit_executable = executable is not None
    if explicit_executable:
        located = Path(executable)
    else:
        exe_candidates: list[Path] = []
        if bundled.is_file():
            exe_candidates.append(bundled)
        which_hit = shutil.which("tesseract", path=env.get("PATH"))
        if which_hit:
            exe_candidates.append(Path(which_hit))
        installed = Path(env.get("ProgramFiles", "C:/Program Files")) / "Tesseract-OCR/tesseract.exe"
        if installed.is_file():
            exe_candidates.append(installed)
        located = next((c for c in exe_candidates if _tesseract_executable_healthy(c)), None)
        if located is None:
            located = next((c for c in exe_candidates if c.is_file()), None)
    if located is None or not Path(located).is_file():
        raise ValueError("skill_tesseract_executable_missing")
    resolved_exe = Path(located).resolve()
    candidates = [resolved_exe.parent / "tessdata", resolved_exe.parents[1] / "share/tessdata"]
    if bundled.parent.joinpath("tessdata").is_dir():
        candidates.append((bundled.parent / "tessdata").resolve())
    if env.get("TESSDATA_PREFIX"):
        configured = Path(env["TESSDATA_PREFIX"])
        candidates.extend((configured, configured / "tessdata"))
    prefix = Path(sys.prefix)
    candidates.extend(path / "share/tessdata" for path in (prefix, *list(prefix.parents)[:2]))
    seen_dirs: set[Path] = set()
    for candidate in candidates:
        try:
            norm = candidate.resolve()
        except OSError:
            continue
        if norm in seen_dirs:
            continue
        seen_dirs.add(norm)
        if all((norm / (language + ".traineddata")).is_file() for language in ("eng", "chi_sim")):
            env["TESSDATA_PREFIX"] = str(norm)
            env["PATH"] = str(resolved_exe.parent) + os.pathsep + env.get("PATH", "")
            return env
    raise ValueError("skill_chinese_tessdata_missing")


def _load_d1(root, *, endpoint=None):
    if not (root / "core/autonomous_training/takeover.py").is_file():
        raise ValueError("d1_module_root_invalid")
    if any(name.split(".")[0] in {"core", "scripts", "desktop"} for name in sys.modules):
        raise RuntimeError("project_modules_already_loaded_requires_isolated_process")
    os.environ.update(skill_ocr_environment(os.environ))
    local = Path(__file__).resolve().parents[1]
    sys.dont_write_bytecode = True
    sys.path[:] = [str(root)] + [p for p in sys.path
        if p and Path(p).resolve() not in {local, local / "scripts", root}]
    if (root/'desktop/umaa_public_context.py').is_file():
        selected=endpoint or os.environ.get('UMA_PUBLIC_ENDPOINT') or ('127.0.0.1:'+os.environ.get('UMA_ADB_PORT',''))
        if not valid_endpoint(selected):
            raise ValueError('packaged_skill_endpoint_required')
        # Public bindings are consulted at import time; the caller has already
        # matched this explicit endpoint to the current run's progress.
        os.environ['UMA_PUBLIC_ENDPOINT']=selected
    install_d1_skill_points_consensus()
    from core.autonomous_training.takeover import SkillTakeoverDevice
    from core.autonomous_training.skills import prepare_skills
    from core.autonomous_training.purchase import (
        select_and_open_purchase_confirmation, SKILL_BATCH_PURCHASE_SAFETY_TOKEN,
    )
    from core.autonomous_training.harvest_skills import (
        complete_harvest_selection, close_and_return_harvest_selection,
    )
    install_d1_skill_compatibility()
    from core.skill_batch_purchase import _primary_evidence, validate_skill_batch_plan
    from core.skill_planner.preferences import load_skill_preference_profile
    from scripts import plan_fresh_skill_acquisition as planner_api
    from core.autonomous_training.skills import publish_skill_plan
    from core.skill_ocr_performance import harvest_skill_ocr
    spec = importlib.util.spec_from_file_location("u1_skill_completion_frames",
        Path(__file__).with_name("larc_skill_completion_frames.py"))
    frames = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(frames)
    return SimpleNamespace(Device=frames.completion_device_class(SkillTakeoverDevice),
        prepare=lambda device, output, context, **kwargs: prepare_for_arc_preference(
            prepare_skills, planner_api, device, output, context, **kwargs),
        select=select_and_open_purchase_confirmation, complete=complete_harvest_selection,
        close=close_and_return_harvest_selection,
        validate=validate_skill_batch_plan, primary_evidence=_primary_evidence,
        token=SKILL_BATCH_PURCHASE_SAFETY_TOKEN,
        load_preference=load_skill_preference_profile,
        replan=lambda **kwargs: replan_with_startup_policy(planner_api, **kwargs),
        publish=publish_skill_plan, ocr_session=harvest_skill_ocr)


def install_d1_skill_points_consensus():
    """Install before transport imports capture the external parser alias."""
    from core import skill_acquisition_parser
    spec = importlib.util.spec_from_file_location("u1_skill_points_consensus",
        Path(__file__).with_name("larc_d1_skill_points_consensus.py"))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.install_skill_points_consensus(skill_acquisition_parser)


def install_d1_skill_compatibility():
    """Bind local compatibility hooks to this isolated D1 namespace only."""
    from core.autonomous_training import harvest_skills, success_close
    from core.skill_planner import global_optimizer
    for name, target in (("larc_d1_optimizer_search", global_optimizer),
                         ("larc_d1_success_auto_lower", (harvest_skills, success_close))):
        spec = importlib.util.spec_from_file_location("u1_" + name,
            Path(__file__).with_name(name + ".py"))
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        if name == "larc_d1_optimizer_search":
            helper.install_small_batch_exact_search(target)
        else:
            helper.install_success_auto_lower(*target)


def prepare_for_arc_preference(prepare, planner_api, device, output, context, **kwargs):
    """Capture the full inventory before computing an arc-target purchase plan."""
    if context.get("larc_skill_purpose") != "arc_target":
        return prepare(device, output, context, **kwargs)
    original = planner_api._planner_available_skills
    original_verdict = planner_api.build_replay_verdict

    def capture_only(observation, build_context, catalog=None):
        available, _ = original(observation, build_context, catalog)
        return [], {"mode": "arc_target_capture_only", "observed_candidate_count": len(available)}

    def capture_verdict(**arguments):
        verdict = original_verdict(**arguments)
        if (verdict["parser_verdict"] == "PARSER_READY" and verdict["capture_binding_accepted"]
                and not arguments["planner_observation"].get("available_skills")):
            # An identified owned-only scan can still support a no-buy plan.
            verdict["planner_verdict"] = "PLANNER_READY"
        return verdict

    planner_api._planner_available_skills = capture_only
    planner_api.build_replay_verdict = capture_verdict
    try:
        return prepare(device, output, context, **kwargs)
    finally:
        planner_api._planner_available_skills = original
        planner_api.build_replay_verdict = original_verdict


def replan_with_startup_policy(planner_api, **kwargs):
    """Honor the existing condition-policy switch in this isolated worker.

    Older D1 adapters reset that switch during context conversion. Only the
    startup or final-target replan, constrained by its own candidate filter
    and purchase postcheck, can use tri-state conditions; other fields stay intact.
    """
    context = kwargs.get("build_context", {})
    purpose = context.get("larc_skill_purpose")
    if (purpose not in {"support_startup", "arc_target"}
            or context.get("policy", {}).get("require_condition_match") is not False):
        return planner_api.replan_fresh_report(**kwargs)
    original = planner_api._planner_policy_from_build_context
    optimizer = sys.modules.get("core.skill_planner.global_optimizer")
    classifier = getattr(getattr(optimizer, "_search_options", None), "_pacer_cm_core_candidate", None)
    cm_profile = getattr(kwargs.get("preference_profile"), "profile_id", "").endswith("-arc-target-cm")
    original_core = optimizer._is_core_candidate if cm_profile and classifier else None

    def startup_policy(build_context):
        policy = original(build_context)
        if (build_context.get("larc_skill_purpose") in {"support_startup", "arc_target"}
                and build_context.get("policy", {}).get("require_condition_match") is False):
            policy["require_condition_match"] = False
        if build_context.get("larc_skill_purpose") == "arc_target":
            for key in ("reserve_skill_pt", "trigger_budget", "max_purchase_count"):
                value = build_context["policy"].get(key)
                if type(value) is int and value >= 0:
                    policy[key] = value
        return policy

    planner_api._planner_policy_from_build_context = startup_policy
    if original_core:
        optimizer._is_core_candidate = classifier
    try:
        report = planner_api.replan_fresh_report(**kwargs)
        plan = report.get("planner_dry_run", {})
        if (purpose == "arc_target" and cm_profile
                and plan.get("status") == "partial" and plan.get("optimizer_status") == "ok"
                and plan.get("execution_allowed") is False and plan.get("unmet_required_skill_ids")):
            # Required targets are aspirational. D1 still validates the selected
            # identities, inventory binding and costs for this affordable subset.
            report["verdict"]["planner_verdict"] = "PLANNER_READY"
            planner_api._write_json(kwargs["output_dir"] / "planner/fresh_skill_dry_run.report.json", report)
        return report
    finally:
        planner_api._planner_policy_from_build_context = original
        if original_core:
            optimizer._is_core_candidate = original_core


def _read(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("object_required")
    return value


def _confirmation_base(count=0):
    return {"ok": False, "device_action_count": count, "device_action_count_complete": True,
            "device_action_count_scope": "confirmation_close_return"}


def _target_policy():
    path = Path(__file__).resolve().parents[1] / "core/auto_trainer/larc_target_skills.py"
    spec = importlib.util.spec_from_file_location("_larc_target_skills", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def _selectable_target_inventory(inventory, primary_evidence):
    """Keep captured rows for diagnostics and qualify only clickable rows."""
    allowed = set(inventory.get("allowed_action_ids", []))
    selectable = []
    for row in inventory.get("candidates", []):
        if row["skill_id"] in allowed:
            try:
                primary_evidence(row)
            except ValueError:
                continue
            selectable.append(row["skill_id"])
    return {**inventory, "allowed_action_ids": selectable}


def apply_race_preference(api, prepared, output, context, preference_path):
    """Reuse the captured inventory; changing preferences must not scan again."""
    manifest = _read(prepared["capture_manifest_path"])
    output.mkdir(parents=True, exist_ok=False)
    (output / "capture_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    timing = None
    target_diagnostic = None
    target_enabled = context.get("arc_policy", {}).get("enabled") is True
    arc_policy = context.get("arc_policy", {})
    classic = context.get("larc_year") == 2 and not context.get("terminal_build")
    derived = target_enabled and (arc_policy.get("mode") == "tiered_ladder"
                                  or (not arc_policy.get("classic_allowlist") and arc_policy.get("auto_cm", True)))
    legacy_timing = classic and not derived
    if target_enabled or legacy_timing:
        inventory = _read(prepared["inventory_path"])
        catalog = _read(context["planner_catalog"])
        base = _read(preference_path) if preference_path is not None else {
            "schema_version": "uma.skill_preference_profile.v1", "profile_id": "larc-race",
            "base_profile": "race", "reserve_skill_pt": 0, "filler_policy": "safe_useful_only",
            "max_batch_size": min(48, context["policy"]["max_purchase_count"]), "rules": []}
    if legacy_timing:
        path = Path(__file__).resolve().parents[1] / "core/auto_trainer/larc_prerace_skill_policy.py"
        spec = importlib.util.spec_from_file_location("_larc_prerace_skill_policy", path)
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        profile, timing = policy.classic_skill_preference(base, inventory, catalog, context=context)
        preference_path = output / "classic_preference.json"
        preference_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "classic_policy.json").write_text(json.dumps(timing, ensure_ascii=False, indent=2), encoding="utf-8")
        if target_enabled:
            base = profile
    if target_enabled:
        primary_evidence = getattr(api, "primary_evidence", None)
        if primary_evidence is not None:
            inventory = _selectable_target_inventory(inventory, primary_evidence)
        profile, target_diagnostic = _target_policy().stage_skill_preference(base, inventory, catalog,
            context=context, year=context.get("larc_year", 3))
        preference_path = output / "target_preference.json"
        preference_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "target_policy.json").write_text(json.dumps(target_diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
    report = api.replan(source_report_path=Path(prepared["planner_report_path"]),
        planner_catalog=Path(context["planner_catalog"]), output_dir=output,
        build_context=context, preference_profile=api.load_preference(preference_path))
    result = api.publish(output, report, manifest)
    if timing is not None:
        result["classic_policy_path"] = str(output / "classic_policy.json")
    if target_diagnostic is not None:
        result["target_diagnostic"] = target_diagnostic
    return result


def _confirmed_purchase(source, root, progress, context):
    source = Path(source).resolve()
    if source == root or not source.is_relative_to(root):
        raise ValueError("prerace_resume_outside_run")
    original = _read(source / "worker_report.json")
    final_path = source / "confirmation/final_confirmation_report.json"
    final = _read(final_path)
    child_path = Path(final.get("promotion_plan_path", "")).resolve()
    if not child_path.is_relative_to(source / "selection") or not child_path.is_file():
        raise ValueError("prerace_confirmed_child_missing")
    child = _read(child_path)
    if (original.get("run_session_id") != root.name or original.get("endpoint") != progress.get("device")
            or original.get("turn_index") != progress["next_turn"]
            or original.get("context", {}).get("race_profile") != context["race_profile"]
            or original.get("year") != context["year"]
            or original.get("stage") != "confirmation_pending" or original.get("returned") is not False
            or final.get("final_confirmation_performed") is not True
            or final.get("stop_reason") not in {"final_confirmation_probe_complete", "batch_final_confirmation_probe_complete"}
            or final.get("actions") != ["skill_purchase.final_confirm"] or final.get("device_action_count") != 1
            or final.get("promotion_plan_sha256") != hashlib.sha256(child_path.read_bytes()).hexdigest()
            or child.get("expected_remaining_skill_points") != original.get("remaining_skill_points")
            or type(original.get("remaining_skill_points")) is not int):
        raise ValueError("prerace_final_confirmation_unverified")
    close_path = source / "success_close/success_close_report.json"
    if close_path.exists():
        close = _read(close_path)
        ret_path = source / "return_after_learning/return_after_learning_report.json"
        ret = _read(ret_path) if ret_path.is_file() else {}
        if not (
            close.get("close_success_performed") is True
            and close.get("stop_reason") in {"close_success_probe_complete", "batch_close_success_probe_complete"}
            and close.get("device_action_count") == 1
            and close.get("final_confirmation_report_sha256") == hashlib.sha256(final_path.read_bytes()).hexdigest()
            and close.get("child_plan_sha256") == hashlib.sha256(child_path.read_bytes()).hexdigest()
            and ret.get("return_performed") is not True
            and not ret.get("actions")
            and ret.get("device_action_count") in (None, 0)
        ):
            raise ValueError("prerace_close_already_attempted_use_its_receipt")
    return source, original, final_path, child_path


def _startup_policy():
    path=Path(__file__).resolve().parents[1]/"core/auto_trainer/larc_support_skill_startup.py"
    spec=importlib.util.spec_from_file_location("_larc_support_startup",path)
    policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
    return policy


def bind_startup_transport(endpoint,modules=None):
    """Configure only this isolated run-bound D1 process, never another account.

    Some vendor helpers keep old U1 transport constants. Bind those explicit
    transport parameters after checking progress.device, without endpoint
    spoofing, extra actions or modifying vendor source files.
    """
    for name,module in tuple((modules or sys.modules).items()):
        if name.startswith("core."):
            for key in ("U1_ENDPOINT","_ALLOWED_ENDPOINT","_ALLOWED_DEVICE_ENDPOINT"):
                if getattr(module,key,None) in {"127.0.0.1:16416","127.0.0.1:16448"}:setattr(module,key,endpoint)


def apply_startup_preference(api,prepared,output,context,*,required,extra_owned=(),native_known=False,options=None,allowlist=None,mode="legacy_speed_startup"):
    policy=_startup_policy();inventory=_read(prepared["inventory_path"]);catalog=_read(context["planner_catalog"])
    manifest=prepared.get("capture_manifest") or _read(prepared["capture_manifest_path"])
    observation=policy.owned_parser_observation(_read(prepared["planner_report_path"]),inventory,catalog,extra_owned=extra_owned)
    unknown=observation["unknown_owned"]
    native_known=native_known or observation["native_observation_covered"]
    merged_extra_owned=list(extra_owned)+list(observation.get("resolved_owned_ids",[]))
    profile,diagnostic=policy.startup_preference(inventory,catalog,required=required,
        race_profile=context["race_profile"],extra_owned=merged_extra_owned,unknown_owned=unknown,native_known=native_known,options=options,allowlist=allowlist,mode=mode)
    output.mkdir(parents=True,exist_ok=False)
    (output/'capture_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    profile_path=output/"startup_preference.json";profile_path.write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding="utf-8")
    authorized_context={**context,"larc_skill_purpose":"support_startup",
        "policy":{**context["policy"],"require_condition_match":False}}
    report=api.replan(source_report_path=Path(prepared["planner_report_path"]),planner_catalog=Path(context["planner_catalog"]),
        output_dir=output,build_context=authorized_context,preference_profile=api.load_preference(profile_path))
    result=api.publish(output,report,manifest)
    result["startup_diagnostic"]=diagnostic
    return result


def run_skill_batch(*, d1_root, run_root, output_dir, run_style, year,
                    resolver_catalog, planner_catalog, external_skill_db_path,
                    endpoint=ENDPOINT, adb_path=None, max_purchase_count=5,
                    execute=False, resume_confirmed_from=None, current_speed=None, current_stamina=None,
                    purpose="prerace", startup_required_count=5, startup_owned_json=None):
    report = {"schema_version": "larc_prerace_skill_batch.v1", "ok": False,
              "reason": "dry_run_no_device_actions", "stage": "not_started",
              "endpoint": endpoint, "returned": False, "purchase_may_have_occurred": False}
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
        root, output, d1 = (Path(p).resolve() for p in (run_root, output_dir, d1_root))
        progress = _read(root / "progress.json")
        arc_policy = _target_policy().policy_from_run(root) if purpose == "prerace" else {"enabled": False}
        if (not valid_endpoint(endpoint) or progress.get("device") != endpoint
                or progress.get("run_session_id") != root.name
                or type(progress.get("next_turn")) is not int):
            raise ValueError("prerace_run_device_mismatch")
        if (purpose not in {"prerace","support_startup"}
                or (run_style not in ((None, *STYLES) if arc_policy["enabled"] else STYLES) or year not in {2,3} if purpose=="prerace" else run_style not in (None,*STYLES) or year not in {1,2,3})
                or type(startup_required_count) is not int or startup_required_count<1 or type(year) is not int
                or type(max_purchase_count) is not int or max_purchase_count < 1):
            raise ValueError("prerace_context_invalid")
        if output == root or not output.is_relative_to(root) or output.is_relative_to(d1):
            raise ValueError("prerace_output_outside_run")
        catalogs = {k: str(Path(v).resolve(strict=True)) for k, v in dict(
            resolver_catalog=resolver_catalog, planner_catalog=planner_catalog,
            external_skill_db_path=external_skill_db_path).items()}
        if not all(Path(p).is_file() for p in catalogs.values()):
            raise ValueError("prerace_catalog_missing")
        output.mkdir(parents=True, exist_ok=False)
        destination = output
        turn_id = f"{root.name}_turn{progress['next_turn']}_arc{year}" if purpose=="prerace" else f"{root.name}_turn{progress['next_turn']}_support_startup"
        context = {"turn_id": turn_id, "target_mode": "race" if purpose=="prerace" else "parent",
                   "larc_year": year, "terminal_build": False,
                   "race_profile": {"distance_type": "middle" if purpose=="prerace" else "any", "style": run_style or "any"},
                   "policy": {"max_purchase_count": max_purchase_count,
                              "allow_gold_without_lower": True}, **catalogs}
        if arc_policy["enabled"]:
            context["current_race_profile"] = {
                **context["race_profile"], "track_id": 10201, "distance_m": 2400,
                "style": run_style or arc_policy["final_target"].get("style", "any")}
        preference_path = None
        policy_dir = root / "skill_policy"
        if purpose=="prerace" and (policy_dir / "race_build_context.json").is_file():
            configured = _read(policy_dir / "race_build_context.json")
            if (configured.get("target_mode") != "race"
                    or (not arc_policy["enabled"] and configured.get("race_profile", {}).get("style") != run_style)):
                raise ValueError("prerace_skill_policy_style_mismatch")
            context["race_profile"].update(configured["race_profile"])
            context["current_stats"] = configured.get("current_stats")
            context["policy"].update(configured.get("policy", {}))
            context["policy"]["max_purchase_count"] = max_purchase_count
            if isinstance(configured.get("larc_prerace_policy"), dict):
                context["larc_prerace_policy"] = configured["larc_prerace_policy"]
            speed_ready = type(current_speed) is int and current_speed >= 1200
            preference_path = policy_dir / ("speed_ge1200.preference.json" if speed_ready
                                             else "speed_lt1200.preference.json")
            if not preference_path.is_file():
                if not arc_policy["enabled"]:
                    raise ValueError("prerace_skill_preference_missing")
                preference_path = None
            if preference_path is not None:
                report["preference_path"] = str(preference_path)
        if arc_policy["enabled"]:
            context = _target_policy().target_context(context, arc_policy)
            if (year == 2 and arc_policy.get("mode") != "tiered_ladder"
                    and not arc_policy["classic_allowlist"] and not arc_policy["auto_cm"]
                    and resume_confirmed_from is None):
                report.update(ok=True, stage="skipped", reason="classic_allowlist_empty", context=context)
                return report
        extra_owned=[];native_known=False;startup_settings={};startup_allowlist=[];startup_mode="legacy_speed_startup"
        if purpose=="support_startup":
            race_context=policy_dir/"race_build_context.json"
            if race_context.is_file():
                supplied=_read(race_context)
                if supplied.get("run_session_id") in (None,root.name):context["race_profile"]=supplied.get("race_profile",context["race_profile"])
            if startup_owned_json is not None:
                ownership=_read(startup_owned_json)
                if ownership.get("run_session_id")!=root.name:raise ValueError("startup_owned_run_mismatch")
                extra_owned=ownership.get("owned_skill_ids",[]);native_known=ownership.get("native_observation_covered") is True
                startup_settings=ownership.get("options",{})
                startup_allowlist=ownership.get("early_skill_allowlist",[])
                startup_mode=ownership.get("target",{}).get("mode","legacy_speed_startup")
        if purpose=="support_startup":
            if not isinstance(startup_allowlist,list) or any(not isinstance(v,str) or not v.strip() for v in startup_allowlist):
                raise ValueError("startup_allowlist_invalid")
            if not startup_allowlist:
                report.update(ok=True,stage="skipped",reason="startup_allowlist_empty")
                return report
            context["policy"]["require_condition_match"]=False #user explicitly authorized listed purposes
        if purpose == "prerace":
            context["current_stats"] = _target_policy().read_current_stats(root, progress,
                context.get("current_stats"), {"speed": current_speed, "stamina": current_stamina})
        report.update(run_session_id=root.name, turn_index=progress["next_turn"], year=year, purpose=purpose,
                      context=context, context_source="caller_current_run_race_plan",
                      stage="loading", reason="in_progress")
        save()
        confirmed = (_confirmed_purchase(resume_confirmed_from, root, progress,
                     {"race_profile": context["race_profile"], "year": year})
                     if resume_confirmed_from is not None else None)
        api = _load_d1(d1,endpoint=endpoint)
        bind_startup_transport(endpoint)
        ocr_metrics = ocr_stack.enter_context(api.ocr_session())

        class SkillsOnlyDevice(api.Device):
            def step(self, action_id, stop_event=None):
                if action_id not in SCROLL_ACTIONS and action_id != "return_harvest_skills":
                    raise RuntimeError("prerace_worker_action_forbidden:" + str(action_id))
                return super().step(action_id, stop_event)

            def close_completed(self, *args, **kwargs):
                raise RuntimeError("prerace_worker_action_forbidden:close_completed")

        device = SkillsOnlyDevice(endpoint, adb_path, output / "capture", expected_endpoint=endpoint)
        device.connect()
        if confirmed is not None:
            source, original, final_path, child_path = confirmed
            remaining = original["remaining_skill_points"]
            report.update(stage="close_return_pending", recovery_of=str(source),
                          remaining_skill_points=remaining, purchase_may_have_occurred=True,
                          purchase_confirmed_before_resume=True)
            save()
            prev_close = source / "success_close" / "success_close_report.json"
            learned = api.close(device, output, source / "selection", child_path,
                                final_path, _confirmation_base(1), remaining, None, endpoint,
                                previous_close_report=(str(prev_close) if prev_close.is_file() else None))
            report["learning_result"] = learned
            if learned.get("ok") is not True or learned.get("remaining_skill_points") != remaining:
                raise ValueError("prerace_close_return_failed:" + str(learned.get("reason")))
            proof = Path(learned.get("return_after_learning_report_path", "")).resolve()
            if not proof.is_relative_to(output) or not proof.is_file():
                raise ValueError("prerace_return_report_missing")
            report.update(ok=True, stage="complete", reason="confirmed_purchase_returned",
                          returned=True, return_report_path=str(proof), requires_menu_observation=True)
            save()
            return report
        before = device.capture()
        if (before.get("endpoint") != endpoint or before.get("page") != "skills"
                or type(before.get("current_skill_points")) is not int
                or before["current_skill_points"] < 0
                or before.get("skill_cart_disabled") is not True):
            raise ValueError("fresh_empty_skill_page_required")
        report.update(before={k: before.get(k) for k in (
            "page", "current_skill_points", "screenshot_path", "frame_sha256")}, stage="preparing")
        save()
        prepared = api.prepare(device, output / "prepare", context, expected_endpoint=endpoint)
        report["preparation"] = prepared
        if prepared.get("status") != "ready":
            raise ValueError("prerace_prepare_failed:" + str(prepared.get("reason")))
        if purpose=="support_startup":
            prepared=apply_startup_preference(api,prepared,output/"support_preference",context,
                required=startup_required_count,extra_owned=extra_owned,native_known=native_known,options=startup_settings,allowlist=startup_allowlist,mode=startup_mode)
            report["startup"]=prepared.get("startup_diagnostic",{})
            report["preference_preparation"]=prepared
        elif preference_path is not None or year == 2 or arc_policy["enabled"]:
            prepared = apply_race_preference(api, prepared, output / "race_preference",
                                             context, preference_path)
            report["preference_preparation"] = prepared
        inventory = _read(prepared["inventory_path"])
        plan = _read(prepared["batch_plan_path"])
        valid, reason = api.validate(plan, inventory)
        if not valid:
            raise ValueError("prerace_plan_invalid:" + str(reason))
        actions = plan.get("selected_action_ids")
        remaining, initial = plan.get("expected_remaining_skill_points"), plan.get("initial_skill_points")
        if (not isinstance(actions, list) or len(actions) > max_purchase_count
                or initial != before["current_skill_points"] or type(remaining) is not int
                or not 0 <= remaining <= initial
                or (not actions and remaining != initial) or (actions and remaining >= initial)):
            raise ValueError("prerace_plan_points_or_count_invalid")
        if purpose=="support_startup" and (not set(actions)<=set(report["startup"].get("selected_skill_ids",[])) or len(actions)>report["startup"].get("safe_remaining",0)):
            raise ValueError("startup_plan_not_bound_to_qualified_candidates")
        if arc_policy["enabled"]:
            _target_policy().validate_target_plan(actions, prepared["target_diagnostic"],
                                                 remaining_skill_points=remaining)
        report.update(selected_action_ids=actions, selected_skills=plan.get("items", []),
                      remaining_skill_points=remaining)
        if not actions:
            # No cart was selected. The caller can use its normal observed
            # skill-page back action; no terminal-only no-purchase proof is used.
            report.update(ok=True, stage="complete", reason="no_suitable_skills",
                          requires_menu_return=True)
            save()
            return report
        selection_root = output / "selection"
        report.update(stage="selection_pending", purchase_may_have_occurred=True)
        save()
        nonce = f"{turn_id}-{time.time_ns()}"
        selection = api.select(device=device, inventory_path=prepared["inventory_path"],
            plan_path=prepared["batch_plan_path"], review_path=None, output_dir=selection_root,
            mode="live", execute=True, expected_inventory_sha256=inventory["packet_sha256"],
            expected_plan_sha256=plan["plan_sha256"], planner_report_path=prepared["planner_report_path"],
            capture_manifest_path=prepared["capture_manifest_path"],
            external_skill_db_path=catalogs["external_skill_db_path"],
            session_id=f"prerace-session-{nonce}", capture_batch_id=f"prerace-capture-{nonce}",
            safety_token=api.token, expected_endpoint=endpoint)
        report["selection"] = selection
        if selection.get("stop_reason") != "confirmation_opened" or selection.get("confirmation_opened") is not True:
            if (selection.get("schema_version")=="uma.autonomous_skill_purchase_preflight.v1"
                    and type(selection.get("device_action_count")) is int and selection["device_action_count"]==0
                    and selection.get("confirmation_opened") is False and selection.get("execution") is None
                    and not selection.get("input_may_have_occurred") and not selection.get("purchase_may_have_occurred")):
                report.update(stage="selection_rejected",purchase_may_have_occurred=False,remaining_skill_points=initial)
            raise ValueError("prerace_selection_not_confirmed")
        report["stage"] = "confirmation_pending"
        save()
        # Despite its historical name this helper only confirms the selected
        # skills, closes success, and taps the skill-page back control.
        learned = api.complete(device, output, selection_root, selection, _confirmation_base(),
                               remaining, len(actions), None, endpoint)
        report["learning_result"] = learned
        owned_remaining = learned.get("remaining_skill_points")
        if learned.get("ok") is not True or type(owned_remaining) is not int or owned_remaining < 0:
            raise ValueError("prerace_learning_failed:" + str(learned.get("reason")))
        if owned_remaining != remaining:
            report["skill_pt_reading_discrepancy"] = {"planned":remaining,"verified_owner":owned_remaining}
        report["remaining_skill_points"] = owned_remaining
        proof = Path(learned.get("return_after_learning_report_path", "")).resolve()
        if not proof.is_relative_to(output) or not proof.is_file():
            raise ValueError("prerace_return_report_missing")
        if purpose=="support_startup":
            policy=_startup_policy();ids=report["startup"]["held"]["owned_skill_ids"]+[row["skill_id"] for row in plan.get("items",[]) if row.get("skill_id")]
            report["startup"]["after"]=policy.held_speed_skills(ids,_read(catalogs["planner_catalog"]),
                unknown_owned=max(0,report["startup"]["held"]["unknown_count"]-len(report["startup"]["held"]["unknown_skill_ids"])-(0 if report["startup"]["held"]["native_observation_covered"] else 1)),
                native_known=report["startup"]["held"]["native_observation_covered"])
        report.update(ok=True, stage="complete", reason="prerace_skills_learned",
                      returned=True, return_report_path=str(proof), requires_menu_observation=True)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("d1-root", "run-root", "output-dir", "resolver-catalog", "planner-catalog", "external-skill-db-path"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--run-style", choices=STYLES)
    parser.add_argument("--year", type=int, choices=(1,2,3), default=1)
    parser.add_argument("--purpose", choices=("prerace","support_startup"), default="prerace")
    parser.add_argument("--startup-required-count",type=int,default=5)
    parser.add_argument("--startup-owned-json",type=Path)
    parser.add_argument("--endpoint", default=ENDPOINT)
    parser.add_argument("--adb-path")
    parser.add_argument("--max-purchase-count", type=int, default=5)
    parser.add_argument("--current-speed", type=int)
    parser.add_argument("--current-stamina", type=int)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume-confirmed-from", type=Path)
    report = run_skill_batch(**vars(parser.parse_args()))
    print(json.dumps({k: report.get(k) for k in ("ok", "reason", "stage", "returned", "remaining_skill_points")}, ensure_ascii=False))
    return 0 if report["ok"] or report["reason"] == "dry_run_no_device_actions" else 2


if __name__ == "__main__":
    raise SystemExit(main())
