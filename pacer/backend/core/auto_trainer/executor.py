from __future__ import annotations

import hashlib
import tempfile
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.auto_trainer.state import TRAINING_TYPES
from core.auto_trainer.frame_safety import (
    FormalActionSafetyGate,
    FrameSafetyAnalyzer,
)
from core.auto_trainer.dashboard_runner import (
    ControlledStop,
    CooperativeRunnerDriver,
    FreshFrameEvidence,
)
from core.coordinates import CoordinateConfig, CoordinateScaler
from core.main_menu_state import analyze_main_menu_state
from core.training_menu_precheck import TrainingMenuPrecheckResult
from core.execution_frames import tap_without_settle
from core.auto_trainer.runtime_control import turn_action, controlled_sleep
from core.auto_trainer.transition_wait import precheck_page_handoff, main_menu_page_handoff


REQUIRED_EXECUTION_TOKEN = "UG_PARENT_AUTOTRAINER_SANDBOX"
SKIP_BUTTON_POINT = (514, 2430)
SUPPORTED_EXECUTION_SCREEN_SIZES = {(720, 1280), (1440, 2560)}
PRECHECK_REFERENCE_SIZE = (1440, 2560)
CLINIC_POST_VERIFY_ATTEMPTS = 3
CLINIC_CONFIRM_TEMPLATE_SHA256 = (
    "e3b2850450ebeb78c0c9f0721df8b7437ed22831da2b740e7c0c44403e0cc554"
)
CLINIC_CONFIRM_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "auto_trainer"
    / "clinic_confirm"
    / "clinic_confirm.png"
)
# These fixed regions bind the confirmation to the medical-room dialog rather
# than to the generic green confirmation button shared by other modal flows.
CLINIC_CONFIRM_TEMPLATE_ANCHORS = (
    ("clinic_title", (20, 370, 700, 455), 0.92),
    ("clinic_illustration", (220, 470, 500, 725), 0.90),
    ("confirm_button", (370, 790, 665, 880), 0.985),
)
CLINIC_CONFIRM_TEMPLATE_SIZE = (720, 1280)


def validate_training_execution_gate(
    *,
    mode: str,
    execute_training_confirm: bool,
    confirm_token: str | None,
    max_turns: int | None,
) -> tuple[bool, str]:
    if mode == "dry-run":
        return True, "dry_run"
    if mode != "live":
        return False, "mode"
    if not execute_training_confirm:
        return False, "execute_training_confirm"
    if confirm_token != REQUIRED_EXECUTION_TOKEN:
        return False, "confirm_token"
    if max_turns is None or max_turns <= 0:
        return False, "max_turns"
    return True, "live_gate_ok"


def analyze_clinic_confirm_modal(image_path: str | Path) -> dict[str, Any]:
    """Accept only the provenance-bound medical-room confirmation modal."""

    try:
        template_bytes = CLINIC_CONFIRM_TEMPLATE_PATH.read_bytes()
        if hashlib.sha256(template_bytes).hexdigest() != CLINIC_CONFIRM_TEMPLATE_SHA256:
            return {
                "accepted": False,
                "reason": "clinic_confirm_template_provenance_mismatch",
            }
        image_bytes = Path(image_path).read_bytes()
    except OSError:
        return {"accepted": False, "reason": "clinic_confirm_image_unreadable"}

    try:
        import cv2
        import numpy as np

        template = cv2.imdecode(
            np.frombuffer(template_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
        )
        image = cv2.imdecode(
            np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
        )
    except (ImportError, ValueError):
        return {"accepted": False, "reason": "clinic_confirm_image_decode_failed"}

    if template is None or image is None:
        return {"accepted": False, "reason": "clinic_confirm_image_decode_failed"}
    if (template.shape[1], template.shape[0]) != CLINIC_CONFIRM_TEMPLATE_SIZE:
        return {
            "accepted": False,
            "reason": "clinic_confirm_template_size_mismatch",
        }
    image_size = (image.shape[1], image.shape[0])
    if image_size == (1440, 2560):
        image = cv2.resize(image, CLINIC_CONFIRM_TEMPLATE_SIZE)
    elif image_size != CLINIC_CONFIRM_TEMPLATE_SIZE:
        return {
            "accepted": False,
            "reason": "clinic_confirm_unsupported_image_size",
            "image_size": {"width": image_size[0], "height": image_size[1]},
        }

    anchors: dict[str, dict[str, float | bool]] = {}
    for name, (left, top, right, bottom), minimum_score in CLINIC_CONFIRM_TEMPLATE_ANCHORS:
        template_anchor = template[top:bottom, left:right]
        image_anchor = image[top:bottom, left:right]
        score = float(
            cv2.matchTemplate(
                image_anchor,
                template_anchor,
                cv2.TM_CCOEFF_NORMED,
            )[0, 0]
        )
        anchors[name] = {
            "score": score,
            "minimum_score": minimum_score,
            "accepted": score >= minimum_score,
        }

    accepted = all(anchor["accepted"] for anchor in anchors.values())
    return {
        "accepted": accepted,
        "reason": (
            "clinic_confirm_template_verified"
            if accepted
            else "clinic_confirm_template_mismatch"
        ),
        "template": {
            "path": str(CLINIC_CONFIRM_TEMPLATE_PATH),
            "sha256": CLINIC_CONFIRM_TEMPLATE_SHA256,
            "source_size": {"width": 720, "height": 1280},
        },
        "anchors": anchors,
    }


class DefaultTrainingExecutionVerifier:
    """Fail closed before clicking the final training confirmation button."""

    def __init__(self, *, scenario: str = "grand_masters") -> None:
        self.scenario = scenario

    def precheck(self, image_path: str | Path) -> Any:
        return self._check(image_path, expected_tab=None)

    def verify_target_tab(self, target: str, image_path: str | Path) -> Any:
        return self._check(image_path, expected_tab=target)

    def _check(self, image_path: str | Path, *, expected_tab: str | None) -> Any:
        from core.training_menu_precheck import TrainingMenuPrechecker

        image = Path(image_path)
        if not image.is_file():
            return TrainingMenuPrecheckResult(
                accepted=False,
                reason="cannot read image",
                selected_tab=None,
                evidence={"image": str(image)},
                failures=["image_read"],
            )
        return TrainingMenuPrechecker(scenario=self.scenario).check(
            self._precheck_image_path(image),
            expected_tab=expected_tab,
        )

    def _precheck_image_path(self, image_path: Path) -> Path:
        return image_path


def _precheck_requires_target_tab_normalization(
    precheck: Any,
    *,
    target: str,
) -> bool:
    """Repeat the harmless tab tap when independent selectors disagree."""
    evidence = getattr(precheck, "evidence", {})
    if not isinstance(evidence, dict):
        return False
    detection = evidence.get("selected_tab_detection", {})
    if not isinstance(detection, dict):
        return False
    ribbon_tab = detection.get("decision_ribbon_best_tab")
    try:
        ribbon_score = float(detection.get("decision_ribbon_best_score", 0.0))
        ribbon_margin = float(detection.get("decision_ribbon_margin", 0.0))
    except (TypeError, ValueError):
        return False
    return (
        ribbon_tab in {"speed", "stamina", "power", "guts", "wisdom"}
        and ribbon_tab != target
        and ribbon_score >= 0.15
        and ribbon_margin >= 0.035
    )


def _ordinary_larc_training_visual_check(check, *, expected_tab=None):
    if (not isinstance(check, TrainingMenuPrecheckResult)
            or not check.failures
            or not set(check.failures) <= {'failure_rate_region'}
            or check.selected_tab not in (*TRAINING_TYPES, 'ss')
            or expected_tab is not None and check.selected_tab != expected_tab
            or check.evidence.get('selected_tab_detection', {}).get('mode') != 'larc_decide_ribbon'):
        return check
    return replace(check, accepted=True, reason='training_menu_without_failure_region',
                   evidence={**check.evidence, 'visual_warning': 'failure_rate_region'}, failures=[])


class TrainingActionExecutor:
    def __init__(
        self,
        *,
        scenario: str = "grand_masters",
        adb_factory: Callable[[], Any] | None = None,
        outing_adb_factory: Callable[[], Any] | None = None,
        coordinate_config: CoordinateConfig | None = None,
        execution_verifier: Any | None = None,
        main_menu_analyzer: Callable[[bytes], dict[str, Any]] | None = None,
        frame_safety_analyzer: Any | None = None,
        action_safety_gate: Any | None = None,
        clinic_confirm_verifier: Callable[[str | Path], dict[str, Any]] | None = None,
        rest_confirm_planner: Callable[..., dict[str, Any]] | None = None,
        screenshot_dir: str | Path | None = None,
        sleep_fn: Callable[[float], None] = controlled_sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if scenario not in {"grand_masters", "larc"}:
            raise ValueError(f"unsupported_training_layout:{scenario}")
        self.scenario = scenario
        self.training_coordinate_section = "larc_training_menu" if scenario == "larc" else "training_menu"
        self.adb_factory = adb_factory or self._default_adb_factory
        self.outing_adb_factory = outing_adb_factory
        self.coordinates = coordinate_config or CoordinateConfig()
        self.execution_verifier = execution_verifier or DefaultTrainingExecutionVerifier(scenario=scenario)
        self.main_menu_analyzer = main_menu_analyzer or (
            lambda png: analyze_main_menu_state(png, scenario=scenario)
        )
        self.frame_safety_analyzer = frame_safety_analyzer or FrameSafetyAnalyzer()
        self.action_safety_gate = action_safety_gate or FormalActionSafetyGate()
        self.clinic_confirm_verifier = (
            clinic_confirm_verifier or analyze_clinic_confirm_modal
        )
        self.rest_confirm_planner = (
            rest_confirm_planner or self._default_rest_confirm_planner
        )
        self.screenshot_dir = (
            Path(screenshot_dir)
            if screenshot_dir is not None
            else Path(tempfile.gettempdir()) / "uma_auto_trainer_executor"
        )
        self.sleep_fn = sleep_fn
        self.wall_clock = wall_clock

    def _resolve_screen_size(
        self,
        adb: Any,
        *,
        max_retries: int = 2,
        retry_delay: float = 0.6,
    ) -> tuple[Any, ...]:
        """Resolve device screen size, retrying transient (None, None) ADB timeouts."""
        screen_size = adb.get_screen_size()
        attempts = 0
        while attempts < max_retries and (
            not isinstance(screen_size, tuple)
            or len(screen_size) != 2
            or screen_size[0] is None
            or screen_size[1] is None
        ):
            attempts += 1
            if retry_delay > 0:
                if self.sleep_fn is time.sleep:
                    controlled_sleep(retry_delay)
                else:
                    self.sleep_fn(retry_delay)
            connect_fn = getattr(adb, "connect", None)
            if callable(connect_fn):
                connect_fn()
            screen_size = adb.get_screen_size()
        return screen_size

    @turn_action
    def execute_training(
        self,
        *,
        target: str,
        mode: str,
        execute_training_confirm: bool,
        confirm_token: str | None,
        max_turns: int | None,
        post_action_skip_taps: int = 0,
        settle_seconds: float = 0.7,
        safety_screenshot_dir: str | Path | None = None,
        control_driver: CooperativeRunnerDriver | None = None,
        structured_action_guard: Any | None = None,
    ) -> dict[str, Any]:
        if target not in TRAINING_TYPES:
            return {
                "performed": False,
                "target": target,
                "reason": "invalid_training_target",
                "tap_plan": [],
            }

        tap_plan = self._tap_plan(target, post_action_skip_taps)
        ok, reason = validate_training_execution_gate(
            mode=mode,
            execute_training_confirm=execute_training_confirm,
            confirm_token=confirm_token,
            max_turns=max_turns,
        )
        if not ok:
            return {
                "performed": False,
                "target": target,
                "reason": reason,
                "tap_plan": tap_plan,
            }
        if mode == "dry-run":
            return {
                "performed": False,
                "target": target,
                "reason": "dry_run",
                "tap_plan": tap_plan,
            }

        adb = self.adb_factory()
        if not adb.connect():
            return {
                "performed": False,
                "target": target,
                "reason": "adb_connect_failed",
                "tap_plan": tap_plan,
            }
        screen_size = self._resolve_screen_size(adb)
        if screen_size not in SUPPORTED_EXECUTION_SCREEN_SIZES:
            return {
                "performed": False,
                "target": target,
                "reason": f"unsupported_screen_size:{screen_size}",
                "tap_plan": tap_plan,
                **self._execution_context(screen_size),
            }
        tap_plan = self._tap_plan_with_device_points(tap_plan, screen_size)

        safety_checks: list[dict[str, Any]] = []
        fresh_frames: list[FreshFrameEvidence] = []
        structured_post_action_frame: dict[str, Any] | None = None
        precheck_path = self._capture_safety_frame(
            adb,
            "precheck",
            target,
            safety_screenshot_dir=safety_screenshot_dir,
        )
        precheck = self.execution_verifier.precheck(precheck_path)
        ordinary_larc = (self.scenario == 'larc'
                         and type(self.execution_verifier) is DefaultTrainingExecutionVerifier)
        if ordinary_larc:
            precheck = _ordinary_larc_training_visual_check(precheck)
        safety_checks.append(self._verification_record("precheck", precheck, precheck_path))
        if not getattr(precheck, "accepted", False):
            return {
                "performed": False,
                "target": target,
                "reason": f"precheck_rejected:{getattr(precheck, 'reason', 'unknown')}",
                **precheck_page_handoff(precheck, precheck_path),
                "tap_plan": tap_plan,
                "safety_checks": safety_checks,
                **self._execution_context(screen_size),
            }

        target_path = precheck_path
        if (
            getattr(precheck, "selected_tab", None) != target
            or _precheck_requires_target_tab_normalization(
                precheck,
                target=target,
            )
        ):
            tab_tap = tap_plan[0]
            outcome = self._perform_controlled_tap(
                adb=adb,
                tap=tab_tap,
                target=target,
                control_driver=control_driver,
                safety_screenshot_dir=safety_screenshot_dir,
                settle_seconds=settle_seconds,
            )
            if outcome is not None and outcome.get("continue") is not True:
                return self._tap_outcome_result(
                    outcome,
                    target=target,
                    tap_plan=tap_plan,
                    fresh_frames=fresh_frames,
                    safety_checks=safety_checks,
                    **self._execution_context(screen_size),
                )
            self._append_fresh_frame(fresh_frames, outcome)
            self._append_structured_action_safety(
                safety_checks,
                outcome,
            )
            target_path = self._capture_safety_frame(
                adb,
                "target_tab",
                target,
                safety_screenshot_dir=safety_screenshot_dir,
            )
        target_check = self.execution_verifier.verify_target_tab(target, target_path)
        if ordinary_larc:
            target_check = _ordinary_larc_training_visual_check(target_check, expected_tab=target)
        safety_checks.append(
            self._verification_record("target_tab", target_check, target_path)
        )
        if not getattr(target_check, "accepted", False):
            return {
                "performed": False,
                "target": target,
                "reason": (
                "target_tab_precheck_rejected:"
                    f"{getattr(target_check, 'reason', 'unknown')}"
                ),
                **precheck_page_handoff(target_check, target_path, navigation_attempted=bool(fresh_frames)),
                "tap_plan": tap_plan,
                "safety_checks": safety_checks,
                **self._execution_context(screen_size),
            }

        target_bytes = target_path.read_bytes()
        if not target_bytes:
            return {
                "performed": False,
                "target": target,
                "reason": "target_tab_source_empty",
                "tap_plan": tap_plan,
                "safety_checks": safety_checks,
                **self._execution_context(screen_size),
            }
        decide_receipt_source = {
            "action_id": "training.execute",
            "action_state": "training_menu",
            "coordinate_name": f"training_menu.{target}_decide",
            "source_path": str(target_path.resolve()),
            "source_sha256": hashlib.sha256(target_bytes).hexdigest(),
        }

        for tap_index, tap in enumerate(tap_plan[1:], start=1):
            outcome = self._perform_controlled_tap(
                adb=adb,
                tap=tap,
                target=target,
                control_driver=control_driver,
                safety_screenshot_dir=safety_screenshot_dir,
                settle_seconds=settle_seconds,
                structured_action_guard=(
                    structured_action_guard
                    if tap_index == 1
                    else None
                ),
            )
            if outcome is not None and outcome.get("continue") is not True:
                return self._tap_outcome_result(
                    outcome,
                    target=target,
                    tap_plan=tap_plan,
                    fresh_frames=fresh_frames,
                    safety_checks=safety_checks,
                    **self._execution_context(screen_size),
                )
            self._append_fresh_frame(fresh_frames, outcome)
            self._append_structured_action_safety(
                safety_checks,
                outcome,
            )
            if tap_index == 1 and isinstance(outcome, dict):
                pre_action = outcome.get("pre_action_frame")
                if (
                    isinstance(pre_action, dict)
                    and pre_action.get("coordinate_name")
                    == f"training_menu.{target}_decide"
                ):
                    decide_receipt_source["source_path"] = pre_action[
                        "source_path"
                    ]
                    decide_receipt_source["source_sha256"] = pre_action[
                        "source_sha256"
                    ]
            if structured_action_guard is not None and tap_index == 1:
                structured_post_action_frame = (
                    self._structured_post_action_frame_record(outcome)
                )
        return {
            "performed": True,
            "target": target,
            "reason": "executed",
            "tap_plan": tap_plan,
            "safety_checks": safety_checks,
            "route_action_receipt_source": decide_receipt_source,
            **self._fresh_frames_report(fresh_frames),
            **(
                {
                    "structured_post_action_frame": (
                        structured_post_action_frame
                    )
                }
                if structured_post_action_frame is not None
                else {}
            ),
            **self._execution_context(screen_size),
        }

    @turn_action
    def execute_mei_outing(self, *, packet, mode, execute_training_confirm, confirm_token,
                           max_turns, safety_screenshot_dir, control_driver=None,
                           structured_action_guard=None):
        if self.scenario != "larc" or structured_action_guard is not None:
            return {"performed": False, "reason": "mei_outing_requires_larc_local_policy", "tap_plan": []}
        from core.auto_trainer.larc_outing_route import MeiOutingRoute
        adb = (self.outing_adb_factory or self.adb_factory)()
        try:
            return MeiOutingRoute(adb=adb, safety=self.frame_safety_analyzer,
                gate=self.action_safety_gate, sleep_fn=self.sleep_fn, control_driver=control_driver).run(
                    run_id=packet["source"]["run_session_id"], turn=packet["source"]["turn_index"],
                    session_path=packet["session_path"], transition_path=packet["transition_session_path"],
                    output_dir=safety_screenshot_dir, mode=mode, execute_training_confirm=execute_training_confirm,
                    confirm_token=confirm_token, max_turns=max_turns, candidate=packet)
        finally:
            if self.outing_adb_factory is not None and getattr(adb, "supports_in_memory_capture", False) is True:
                adb.close()

    @turn_action
    def execute_rest(
        self,
        *,
        mode: str,
        execute_training_confirm: bool,
        confirm_token: str | None,
        max_turns: int | None,
        settle_seconds: float = 0.7,
        safety_screenshot_dir: str | Path | None = None,
        control_driver: CooperativeRunnerDriver | None = None,
        structured_action_guard: Any | None = None,
        recovery_kind: str = "rest",
    ) -> dict[str, Any]:
        if recovery_kind not in {"rest","outing"} or (recovery_kind=="outing" and self.scenario!='larc'):
            return {'performed':False,'reason':'ordinary_recovery_kind_unverified','tap_plan':[]}
        tap_plan = self._rest_tap_plan() if recovery_kind=="rest" else self._rest_tap_plan("outing")
        ok, reason = validate_training_execution_gate(
            mode=mode,
            execute_training_confirm=execute_training_confirm,
            confirm_token=confirm_token,
            max_turns=max_turns,
        )
        if not ok:
            return {
                "performed": False,
                "target": None,
                "reason": reason,
                "tap_plan": tap_plan,
            }
        if mode == "dry-run":
            return {
                "performed": False,
                "target": None,
                "reason": "dry_run",
                "tap_plan": tap_plan,
            }

        adb = self.adb_factory()
        if not adb.connect():
            return {
                "performed": False,
                "target": None,
                "reason": "adb_connect_failed",
                "tap_plan": tap_plan,
            }
        screen_size = self._resolve_screen_size(adb)
        if screen_size not in SUPPORTED_EXECUTION_SCREEN_SIZES:
            return {
                "performed": False,
                "target": None,
                "reason": f"unsupported_screen_size:{screen_size}",
                "tap_plan": tap_plan,
                **self._execution_context(screen_size),
            }
        tap_plan = self._tap_plan_with_device_points(tap_plan, screen_size)

        safety_checks: list[dict[str, Any]] = []
        fresh_frames: list[FreshFrameEvidence] = []
        precheck_path = self._capture_safety_frame(
            adb,
            "precheck",
            "rest",
            safety_screenshot_dir=safety_screenshot_dir,
        )
        precheck = self.execution_verifier.precheck(precheck_path)
        safety_checks.append(self._verification_record("precheck", precheck, precheck_path))
        if not getattr(precheck, "accepted", False):
            return {
                "performed": False,
                "target": None,
                "reason": f"precheck_rejected:{getattr(precheck, 'reason', 'unknown')}",
                **precheck_page_handoff(precheck, precheck_path),
                "tap_plan": tap_plan,
                "safety_checks": safety_checks,
                **self._execution_context(screen_size),
            }

        back_tap = tap_plan[0]
        outcome = self._perform_controlled_tap(
            adb=adb,
            tap=back_tap,
            target="rest",
            control_driver=control_driver,
            safety_screenshot_dir=safety_screenshot_dir,
            settle_seconds=settle_seconds,
        )
        if outcome is not None and outcome.get("continue") is not True:
            return self._tap_outcome_result(
                outcome,
                target=None,
                tap_plan=tap_plan,
                fresh_frames=fresh_frames,
                safety_checks=safety_checks,
                **self._execution_context(screen_size),
            )
        self._append_fresh_frame(fresh_frames, outcome)

        main_menu_path = self._capture_safety_frame(
            adb,
            "main_menu",
            "rest",
            safety_screenshot_dir=safety_screenshot_dir,
        )
        main_menu_check = self._main_menu_verification_record(main_menu_path)
        safety_checks.append(main_menu_check)
        if not main_menu_check["accepted"]:
            return {
                "performed": False,
                "target": None,
                "reason": "main_menu_verification_rejected",
                **main_menu_page_handoff(main_menu_path),
                "tap_plan": tap_plan,
                "safety_checks": safety_checks,
                **self._fresh_frames_report(fresh_frames),
                **self._execution_context(screen_size),
            }

        if recovery_kind=='outing':
            import cv2
            from core.larc_outing_reader import MeiOutingReader
            home=MeiOutingReader().read(cv2.imread(str(main_menu_path)))
            point=home.get('open')
            if home.get('status')!='accepted' or home.get('screen')!='main_menu' or not point:
                return {'performed':False,'reason':'normal_outing_button_unverified','tap_plan':tap_plan,
                        'safety_checks':safety_checks,**self._execution_context(screen_size)}
            tap_plan.extend(self._tap_plan_with_device_points([{'coordinate_name':'main_menu.outing',
                'x':point[0],'y':point[1],'scaled':False}],screen_size))
        rest_tap = tap_plan[1]
        outcome = self._perform_controlled_tap(
            adb=adb,
            tap=rest_tap,
            target="rest",
            control_driver=control_driver,
            safety_screenshot_dir=safety_screenshot_dir,
            settle_seconds=settle_seconds,
            structured_action_guard=structured_action_guard,
        )
        if outcome is not None and outcome.get("continue") is not True:
            return self._tap_outcome_result(
                outcome,
                target=None,
                tap_plan=tap_plan,
                fresh_frames=fresh_frames,
                safety_checks=safety_checks,
                **self._execution_context(screen_size),
            )
        self._append_fresh_frame(fresh_frames, outcome)
        self._append_structured_action_safety(safety_checks, outcome)

        return self._confirm_open_rest(
            adb=adb, screen_size=screen_size, tap_plan=tap_plan,
            safety_checks=safety_checks, fresh_frames=fresh_frames,
            control_driver=control_driver, safety_screenshot_dir=safety_screenshot_dir,
            settle_seconds=settle_seconds,
            opening_source=main_menu_path,
            recovery_kind=recovery_kind,
            expected_confirmation_pair=("confirm_normal_outing","safe_advance.normal_outing_confirm") if recovery_kind=="outing" else None,
        )

    def _confirm_open_rest(
        self, *, adb: Any, screen_size: tuple[int, int],
        tap_plan: list[dict[str, Any]], safety_checks: list[dict[str, Any]],
        fresh_frames: list[FreshFrameEvidence],
        control_driver: CooperativeRunnerDriver | None,
        safety_screenshot_dir: str | Path | None, settle_seconds: float,
        expected_confirmation_pair: tuple[str, str] | None = None,
        opening_source: Path | None = None,
        _read_attempt: int = 0,
        recovery_kind: str = "rest",
    ) -> dict[str, Any]:
        """Confirm the rest modal after its caller has verified the opening stage."""
        confirm_path = self._capture_safety_frame(
            adb,
            "confirm_modal" if not _read_attempt else f"confirm_modal_retry{_read_attempt}",
            "rest",
            safety_screenshot_dir=safety_screenshot_dir,
        )
        packet = self.frame_safety_analyzer.analyze(confirm_path)
        frame_safety = packet.to_dict()
        confirm_sha256 = hashlib.sha256(confirm_path.read_bytes()).hexdigest()
        if packet.source_image_sha256 != confirm_sha256:
            return {
                "performed": False,
                "target": None,
                "reason": "frame_sha_mismatch",
                "tap_plan": tap_plan,
                "safety_checks": safety_checks,
                "frame_safety": frame_safety,
                **self._fresh_frames_report(fresh_frames),
                **self._execution_context(screen_size),
            }
        try:
            if recovery_kind=='outing':
                import cv2
                from core.larc_outing_reader import MeiOutingReader
                observed=MeiOutingReader().read(cv2.imread(str(confirm_path)))
                point=observed.get('confirm')
                decision=({'action':'tap','reason':'confirm_normal_outing',
                    'tap':{'coordinate_name':'safe_advance.normal_outing_confirm',
                           'x':point[0],'y':point[1],'scaled':False}}
                    if observed.get('screen')=='mei_outing_confirm' and observed.get('status')=='accepted' and point else None)
            else:
                decision = self.rest_confirm_planner(
                    confirm_path,
                    visible_text=packet.visible_text,
                )
        except Exception:
            decision = None
        tap = decision.get("tap") if isinstance(decision, dict) else None
        confirmation_pairs=({('confirm_normal_outing','safe_advance.normal_outing_confirm')}
            if recovery_kind=='outing' else {('confirm_rest','safe_advance.rest_confirm'),
                ('confirm_rest_outing','safe_advance.rest_outing_confirm')})
        confirm_verified = (
            isinstance(decision, dict)
            and decision.get("action") == "tap"
            and isinstance(tap, dict)
            and (decision.get("reason"), tap.get("coordinate_name")) in confirmation_pairs
            and (expected_confirmation_pair is None
                 or (decision.get("reason"), tap.get("coordinate_name")) == expected_confirmation_pair)
        )
        safety_checks.append(
            {
                "label": "rest_confirm",
                "accepted": confirm_verified,
                "reason": (
                    "rest_confirm_verified"
                    if confirm_verified
                    else "rest_confirm_modal_unverified"
                ),
                "image_path": str(confirm_path),
                "decision": decision,
            }
        )
        if not confirm_verified:
            if self.scenario == 'larc' and opening_source is not None:
                from core.auto_trainer.optional_confirmation import direct_action_started
                started = direct_action_started(confirm_path, before_path=opening_source)
                if started is not None:
                    return {
                        'performed':True, 'target':'normal' if recovery_kind=='outing' else None,
                        'reason':'executed_normal_outing' if recovery_kind=='outing' else 'executed_rest_confirmed',
                        'confirmation_mode':'disabled', 'direct_start_observation':started,
                        'tap_plan':tap_plan, 'safety_checks':safety_checks, 'frame_safety':frame_safety,
                        'route_action_receipt_source':{
                            'action_id':recovery_kind+'.execute', 'action_state':'main_menu',
                            'coordinate_name':'main_menu.'+recovery_kind,
                            'source_path':str(Path(opening_source).resolve()),
                            'source_sha256':hashlib.sha256(Path(opening_source).read_bytes()).hexdigest()},
                        **self._fresh_frames_report(fresh_frames), **self._execution_context(screen_size)}
                if _read_attempt < 2:
                    self.sleep_fn(.15)
                    return self._confirm_open_rest(adb=adb,screen_size=screen_size,tap_plan=tap_plan,
                        safety_checks=safety_checks,fresh_frames=fresh_frames,control_driver=control_driver,
                        safety_screenshot_dir=safety_screenshot_dir,settle_seconds=settle_seconds,
                        expected_confirmation_pair=expected_confirmation_pair,opening_source=opening_source,
                        _read_attempt=_read_attempt+1,recovery_kind=recovery_kind)
            return {
                "performed": False,
                "target": None,
                "reason": "rest_confirm_modal_unverified",
                "tap_plan": tap_plan,
                "safety_checks": safety_checks,
                "frame_safety": frame_safety,
                **self._fresh_frames_report(fresh_frames),
                **self._execution_context(screen_size),
            }

        verdict = self.action_safety_gate.evaluate(packet, decision)
        action_safety = verdict.to_dict()
        if not verdict.allowed:
            return {
                "performed": False,
                "target": None,
                "reason": verdict.reason,
                "tap_plan": tap_plan,
                "safety_checks": safety_checks,
                "frame_safety": frame_safety,
                "action_safety": action_safety,
                "decision": decision,
                **self._fresh_frames_report(fresh_frames),
                **self._execution_context(screen_size),
            }

        annotated_confirm_tap = self._tap_plan_with_device_points([tap], screen_size)[0]
        tap_plan.append(annotated_confirm_tap)
        outcome = self._perform_controlled_tap(
            adb=adb,
            tap={
                **annotated_confirm_tap,
                "x": annotated_confirm_tap["device_x"],
                "y": annotated_confirm_tap["device_y"],
                "scaled": False,
            },
            target="rest",
            control_driver=control_driver,
            safety_screenshot_dir=safety_screenshot_dir,
            settle_seconds=settle_seconds,
        )
        if outcome is not None and outcome.get("continue") is not True:
            return self._tap_outcome_result(
                outcome,
                target=None,
                tap_plan=tap_plan,
                fresh_frames=fresh_frames,
                safety_checks=safety_checks,
                frame_safety=frame_safety,
                action_safety=action_safety,
                decision=decision,
                **self._execution_context(screen_size),
            )
        self._append_fresh_frame(fresh_frames, outcome)
        return {
            "performed": True,
            "target": "normal" if recovery_kind=="outing" else None,
            "reason": "executed_normal_outing" if recovery_kind=="outing" else "executed_rest_confirmed",
            "tap_plan": tap_plan,
            "safety_checks": safety_checks,
            "frame_safety": frame_safety,
            "action_safety": action_safety,
            "decision": decision,
            "route_action_receipt_source": {
                "action_id": tap["coordinate_name"],
                "action_state": "modal",
                "coordinate_name": tap["coordinate_name"],
                "source_path": str(confirm_path.resolve()),
                "source_sha256": confirm_sha256,
            },
            **self._fresh_frames_report(fresh_frames),
            **self._execution_context(screen_size),
        }

    @turn_action
    def execute_low_vitality_rest_from_main_menu(
        self,
        *,
        mode: str,
        execute_training_confirm: bool,
        confirm_token: str | None,
        max_turns: int | None,
        vitality_threshold: float = 35.0,
        capture_preflight: bool = False,
        settle_seconds: float = 0.7,
        safety_screenshot_dir: str | Path | None = None,
        control_driver: CooperativeRunnerDriver | None = None,
    ) -> dict[str, Any]:
        """Atomically rest from a freshly verified low-vitality main menu.

        This path deliberately has no training-menu back tap.  The main-menu
        analyzer, resource-text packet, action verdict, and executed tap are all
        derived from the same freshly captured frame.
        """

        rest_x, rest_y = self.coordinates.point("main_menu", "rest")
        tap_plan = [
            {
                "coordinate_name": "main_menu.rest",
                "x": rest_x,
                "y": rest_y,
                "scaled": True,
            }
        ]
        ok, reason = validate_training_execution_gate(
            mode=mode,
            execute_training_confirm=execute_training_confirm,
            confirm_token=confirm_token,
            max_turns=max_turns,
        )
        if not ok:
            return {
                "performed": False,
                "target": None,
                "reason": reason,
                "tap_plan": tap_plan,
            }
        if mode == "dry-run" and not capture_preflight:
            return {
                "performed": False,
                "target": None,
                "reason": "dry_run",
                "tap_plan": tap_plan,
            }

        adb = self.adb_factory()
        if not adb.connect():
            return {
                "performed": False,
                "target": None,
                "reason": "adb_connect_failed",
                "tap_plan": tap_plan,
            }
        screen_size = self._resolve_screen_size(adb)
        if screen_size not in SUPPORTED_EXECUTION_SCREEN_SIZES:
            return {
                "performed": False,
                "target": None,
                "reason": f"unsupported_screen_size:{screen_size}",
                "tap_plan": tap_plan,
                **self._execution_context(screen_size),
            }
        tap_plan = self._tap_plan_with_device_points(tap_plan, screen_size)

        frame_path = self._capture_safety_frame(
            adb,
            "main_menu_precheck",
            "low_vitality_rest",
            safety_screenshot_dir=safety_screenshot_dir,
        )
        main_menu_check = self._main_menu_verification_record(frame_path)
        if not main_menu_check["accepted"]:
            return {
                "performed": False,
                "target": None,
                "reason": "main_menu_verification_rejected",
                **main_menu_page_handoff(frame_path, navigation_attempted=False),
                "tap_plan": tap_plan,
                "safety_checks": [main_menu_check],
                **self._execution_context(screen_size),
            }

        vitality_percent = self._main_menu_vitality_percent(
            main_menu_check.get("evidence")
        )
        if vitality_percent is None or vitality_percent >= float(vitality_threshold):
            return {
                "performed": False,
                "target": None,
                "reason": "low_vitality_policy_not_satisfied",
                "tap_plan": tap_plan,
                "safety_checks": [main_menu_check],
                "policy_evidence": {
                    "vitality_percent": vitality_percent,
                    "threshold": float(vitality_threshold),
                },
                **self._execution_context(screen_size),
            }

        packet = self.frame_safety_analyzer.analyze(frame_path)
        frame_safety = packet.to_dict()
        actual_sha256 = hashlib.sha256(frame_path.read_bytes()).hexdigest()
        if packet.source_image_sha256 != actual_sha256:
            return {
                "performed": False,
                "target": None,
                "reason": "frame_sha_mismatch",
                "tap_plan": tap_plan,
                "safety_checks": [main_menu_check],
                "frame_safety": frame_safety,
                **self._execution_context(screen_size),
            }
        if packet.text_status != "ok":
            return {
                "performed": False,
                "target": None,
                "reason": "frame_text_untrusted",
                "tap_plan": tap_plan,
                "safety_checks": [main_menu_check],
                "frame_safety": frame_safety,
                **self._execution_context(screen_size),
            }

        decision = {
            "action": "tap",
            "reason": "low_vitality_rest",
            "tap": tap_plan[0],
            "evidence": {
                "vitality_percent": vitality_percent,
                "threshold": float(vitality_threshold),
                "source_image_sha256": actual_sha256,
            },
        }
        verdict = self.action_safety_gate.evaluate(packet, decision)
        action_safety = verdict.to_dict()
        if not verdict.allowed:
            return {
                "performed": False,
                "target": None,
                "reason": verdict.reason,
                "tap_plan": tap_plan,
                "safety_checks": [main_menu_check],
                "frame_safety": frame_safety,
                "action_safety": action_safety,
                "decision": decision,
                **self._execution_context(screen_size),
            }

        if mode == "dry-run":
            return {
                "performed": False,
                "target": None,
                "reason": "preflight_ready",
                "tap_plan": tap_plan,
                "safety_checks": [main_menu_check],
                "frame_safety": frame_safety,
                "action_safety": action_safety,
                "decision": decision,
                **self._execution_context(screen_size),
            }

        tap = tap_plan[0]
        outcome = self._perform_controlled_tap(
            adb=adb,
            tap=tap,
            target="low_vitality_rest",
            control_driver=control_driver,
            safety_screenshot_dir=safety_screenshot_dir,
            settle_seconds=settle_seconds,
        )
        if outcome is not None and outcome.get("continue") is not True:
            return self._tap_outcome_result(
                outcome,
                target=None,
                tap_plan=tap_plan,
                fresh_frames=[],
                safety_checks=[main_menu_check],
                frame_safety=frame_safety,
                action_safety=action_safety,
                decision=decision,
                **self._execution_context(screen_size),
            )
        fresh_frames: list[FreshFrameEvidence] = []
        self._append_fresh_frame(fresh_frames, outcome)
        return {
            "performed": True,
            "target": None,
            "reason": "executed_low_vitality_rest",
            "tap_plan": tap_plan,
            "safety_checks": [main_menu_check],
            "frame_safety": frame_safety,
            "action_safety": action_safety,
            "decision": decision,
            **self._fresh_frames_report(fresh_frames),
            **self._execution_context(screen_size),
        }

    @turn_action
    def execute_clinic(
        self,
        *,
        mode: str,
        execute_training_confirm: bool,
        confirm_token: str | None,
        max_turns: int | None,
        allow_clinic: bool,
        settle_seconds: float = 0.7,
        safety_screenshot_dir: str | Path | None = None,
        control_driver: CooperativeRunnerDriver | None = None,
        structured_action_guard: Any | None = None,
    ) -> dict[str, Any]:
        tap_plan = self._clinic_tap_plan()
        ok, reason = validate_training_execution_gate(
            mode=mode,
            execute_training_confirm=execute_training_confirm,
            confirm_token=confirm_token,
            max_turns=max_turns,
        )
        if not ok:
            return {
                "performed": False,
                "target": None,
                "reason": reason,
                "tap_plan": tap_plan,
            }
        if mode == "dry-run":
            return {
                "performed": False,
                "target": None,
                "reason": "dry_run",
                "tap_plan": tap_plan,
            }
        if not allow_clinic:
            return {
                "performed": False,
                "target": None,
                "reason": "goal_disallows_clinic",
                "tap_plan": tap_plan,
            }

        try:
            tap_plan = self._clinic_tap_plan(verified=True)
        except ValueError as exc:
            return {
                "performed": False,
                "target": None,
                "reason": str(exc),
                "tap_plan": tap_plan,
            }

        adb = self.adb_factory()
        if not adb.connect():
            return {
                "performed": False,
                "target": None,
                "reason": "adb_connect_failed",
                "tap_plan": tap_plan,
            }
        screen_size = self._resolve_screen_size(adb)
        if screen_size not in SUPPORTED_EXECUTION_SCREEN_SIZES:
            return {
                "performed": False,
                "target": None,
                "reason": f"unsupported_screen_size:{screen_size}",
                "tap_plan": tap_plan,
                **self._execution_context(screen_size),
            }
        tap_plan = self._tap_plan_with_device_points(tap_plan, screen_size)

        safety_checks: list[dict[str, Any]] = []
        fresh_frames: list[FreshFrameEvidence] = []
        precheck_path = self._capture_safety_frame(
            adb,
            "precheck",
            "clinic",
            safety_screenshot_dir=safety_screenshot_dir,
        )
        precheck = self.execution_verifier.precheck(precheck_path)
        safety_checks.append(
            {
                **self._verification_record("precheck", precheck, precheck_path),
                "screen": "training_menu",
            }
        )
        if not getattr(precheck, "accepted", False):
            return {
                "performed": False,
                "target": None,
                "reason": f"precheck_rejected:{getattr(precheck, 'reason', 'unknown')}",
                **precheck_page_handoff(precheck, precheck_path),
                "tap_plan": tap_plan,
                "safety_checks": safety_checks,
                **self._execution_context(screen_size),
            }

        back_tap = tap_plan[0]
        outcome = self._perform_controlled_tap(
            adb=adb,
            tap=back_tap,
            target="clinic",
            control_driver=control_driver,
            safety_screenshot_dir=safety_screenshot_dir,
            settle_seconds=settle_seconds,
        )
        if outcome is not None and outcome.get("continue") is not True:
            return self._tap_outcome_result(
                outcome,
                target=None,
                tap_plan=tap_plan,
                fresh_frames=fresh_frames,
                safety_checks=safety_checks,
                **self._execution_context(screen_size),
            )
        self._append_fresh_frame(fresh_frames, outcome)

        main_menu_path = self._capture_safety_frame(
            adb,
            "main_menu",
            "clinic",
            safety_screenshot_dir=safety_screenshot_dir,
        )
        main_menu_check = self._main_menu_verification_record(main_menu_path)
        safety_checks.append(main_menu_check)
        if not main_menu_check["accepted"]:
            return {
                "performed": False,
                "target": None,
                "reason": "main_menu_verification_rejected",
                **main_menu_page_handoff(main_menu_path),
                "tap_plan": tap_plan,
                "safety_checks": safety_checks,
                **self._execution_context(screen_size),
            }

        clinic_tap = tap_plan[1]
        outcome = self._perform_controlled_tap(
            adb=adb,
            tap=clinic_tap,
            target="clinic",
            control_driver=control_driver,
            safety_screenshot_dir=safety_screenshot_dir,
            settle_seconds=settle_seconds,
            structured_action_guard=structured_action_guard,
        )
        if outcome is not None and outcome.get("continue") is not True:
            return self._tap_outcome_result(
                outcome,
                target=None,
                tap_plan=tap_plan,
                fresh_frames=fresh_frames,
                safety_checks=safety_checks,
                **self._execution_context(screen_size),
            )
        self._append_fresh_frame(fresh_frames, outcome)
        self._append_structured_action_safety(
            safety_checks,
            outcome,
        )

        confirm_path = self._capture_safety_frame(
            adb,
            "confirm_modal",
            "clinic",
            safety_screenshot_dir=safety_screenshot_dir,
        )
        confirm_verified, confirm_decision = self._verified_clinic_confirm(confirm_path)
        safety_checks.append(
            {
                "label": "clinic_confirm",
                "accepted": confirm_verified,
                "reason": (
                    "clinic_confirm_verified"
                    if confirm_verified
                    else "clinic_confirm_modal_unverified"
                ),
                "image_path": str(confirm_path),
                "decision": confirm_decision,
            }
        )
        if not confirm_verified:
            return {
                "performed": False,
                "target": None,
                "reason": "clinic_confirm_modal_unverified",
                "tap_plan": tap_plan,
                "safety_checks": safety_checks,
                **self._execution_context(screen_size),
            }

        try:
            clinic_confirm_coordinate = self.coordinates.require_verified(
                "clinic_confirm", "confirm"
            )
            if (
                clinic_confirm_coordinate["verification"].get("source_sha256")
                != CLINIC_CONFIRM_TEMPLATE_SHA256
            ):
                raise ValueError("clinic_confirm_fixture_sha_mismatch")
            confirm_x = int(clinic_confirm_coordinate["x"])
            confirm_y = int(clinic_confirm_coordinate["y"])
        except ValueError as exc:
            return {
                "performed": False,
                "target": None,
                "reason": str(exc),
                "tap_plan": tap_plan,
                "safety_checks": safety_checks,
                **self._fresh_frames_report(fresh_frames),
                **self._execution_context(screen_size),
            }
        annotated_confirm_tap = self._tap_plan_with_device_points(
            [
                {
                    "coordinate_name": "clinic_confirm.confirm",
                    "x": confirm_x,
                    "y": confirm_y,
                    "scaled": True,
                }
            ],
            screen_size,
        )[0]
        tap_plan.append(annotated_confirm_tap)
        outcome = self._perform_controlled_tap(
            adb=adb,
            tap={
                **annotated_confirm_tap,
                "x": annotated_confirm_tap["device_x"],
                "y": annotated_confirm_tap["device_y"],
                "scaled": False,
            },
            target="clinic",
            control_driver=control_driver,
            safety_screenshot_dir=safety_screenshot_dir,
            settle_seconds=settle_seconds,
        )
        if outcome is not None and outcome.get("continue") is not True:
            return self._tap_outcome_result(
                outcome,
                target=None,
                tap_plan=tap_plan,
                fresh_frames=fresh_frames,
                safety_checks=safety_checks,
                **self._execution_context(screen_size),
            )
        self._append_fresh_frame(fresh_frames, outcome)

        for attempt in range(CLINIC_POST_VERIFY_ATTEMPTS):
            post_path = self._capture_safety_frame(
                adb,
                f"post_clinic_{attempt + 1}",
                "clinic",
                safety_screenshot_dir=safety_screenshot_dir,
            )
            post_check = self._main_menu_verification_record(post_path)
            post_check["label"] = "clinic_post_main_menu"
            post_check["attempt"] = attempt + 1
            safety_checks.append(post_check)
            if post_check["accepted"]:
                return {
                    "performed": True,
                    "target": None,
                    "reason": "executed",
                    "tap_plan": tap_plan,
                    "safety_checks": safety_checks,
                    **self._fresh_frames_report(fresh_frames),
                    **self._execution_context(screen_size),
                }
            if attempt + 1 < CLINIC_POST_VERIFY_ATTEMPTS:
                self.sleep_fn(settle_seconds)
        return {
            "performed": False,
            "target": None,
            "reason": "clinic_post_verification_rejected",
            "tap_plan": tap_plan,
            "safety_checks": safety_checks,
            **self._execution_context(screen_size),
        }

    def _tap_plan(self, target: str, post_action_skip_taps: int) -> list[dict[str, Any]]:
        section = self.training_coordinate_section
        tab_x, tab_y = self.coordinates.point(section, target)
        decide_x, decide_y = self.coordinates.point(section, f"{target}_decide")
        plan = [
            {
                "coordinate_name": f"training_menu.{target}",
                "coordinate_config_name": f"{section}.{target}",
                "x": tab_x,
                "y": tab_y,
                "scaled": True,
            },
            {
                "coordinate_name": f"training_menu.{target}_decide",
                "coordinate_config_name": f"{section}.{target}_decide",
                "x": decide_x,
                "y": decide_y,
                "scaled": True,
            },
        ]
        for index in range(max(0, int(post_action_skip_taps))):
            plan.append(
                {
                    "coordinate_name": f"post_action.skip:{index + 1}",
                    "x": SKIP_BUTTON_POINT[0],
                    "y": SKIP_BUTTON_POINT[1],
                    "scaled": True,
                }
            )
        return plan

    def _rest_tap_plan(self, recovery_kind="rest") -> list[dict[str, Any]]:
        back_x, back_y = self.coordinates.point("training_menu", "back")
        if recovery_kind=='outing':
            # The existing outing reader locates the live orange button only
            # after returning home; there is no invented coordinate entry.
            return [{'coordinate_name':'training_menu.back','x':back_x,'y':back_y,'scaled':True}]
        rest_x, rest_y = self.coordinates.point("main_menu", recovery_kind)
        return [
            {
                "coordinate_name": "training_menu.back",
                "x": back_x,
                "y": back_y,
                "scaled": True,
            },
            {
                "coordinate_name": "main_menu."+recovery_kind,
                "x": rest_x,
                "y": rest_y,
                "scaled": True,
            },
        ]

    def _clinic_tap_plan(self, *, verified: bool = False) -> list[dict[str, Any]]:
        back_x, back_y = self.coordinates.point("training_menu", "back")
        if verified:
            clinic_x, clinic_y = self.coordinates.verified_point(
                "main_menu", "clinic"
            )
        else:
            clinic_x, clinic_y = self.coordinates.point("main_menu", "clinic")
        return [
            {
                "coordinate_name": "training_menu.back",
                "x": back_x,
                "y": back_y,
                "scaled": True,
            },
            {
                "coordinate_name": "main_menu.clinic",
                "x": clinic_x,
                "y": clinic_y,
                "scaled": True,
            },
        ]

    def _tap_plan_with_device_points(
        self,
        tap_plan: list[dict[str, Any]],
        screen_size: tuple[int, int],
    ) -> list[dict[str, Any]]:
        scaler = CoordinateScaler(self.coordinates.reference_size, screen_size)
        annotated: list[dict[str, Any]] = []
        for tap in tap_plan:
            reference_x = int(tap["x"])
            reference_y = int(tap["y"])
            if tap.get("scaled", False):
                device_x, device_y = scaler.scale_point(reference_x, reference_y)
            else:
                device_x, device_y = reference_x, reference_y
            annotated.append(
                {
                    **tap,
                    "reference_x": reference_x,
                    "reference_y": reference_y,
                    "device_x": device_x,
                    "device_y": device_y,
                    "device_size": self._size_record(screen_size),
                    "reference_size": self._size_record(self.coordinates.reference_size),
                }
            )
        return annotated

    def _execution_context(self, screen_size: tuple[int, int]) -> dict[str, Any]:
        return {
            "screen_size": self._size_record(screen_size),
            "coordinate_reference_size": self._size_record(self.coordinates.reference_size),
        }

    @staticmethod
    def _size_record(size: tuple[int, int]) -> dict[str, int]:
        return {
            "width": int(size[0]) if size and size[0] is not None else 0,
            "height": int(size[1]) if size and size[1] is not None else 0,
        }

    @staticmethod
    def _default_adb_factory() -> Any:
        from core.adb import ADB

        return ADB()

    def _default_rest_confirm_planner(
        self,
        image_path: str | Path,
        *,
        visible_text: str | None = None,
    ) -> dict[str, Any]:
        from core.auto_trainer.safe_advance import SafeAdvancePlanner

        return SafeAdvancePlanner().plan(
            image_path,
            visible_text=visible_text,
            scenario=self.scenario,
        )

    def _verified_clinic_confirm(
        self, image_path: Path
    ) -> tuple[bool, dict[str, Any] | None]:
        try:
            decision = self.clinic_confirm_verifier(image_path)
        except Exception:
            return False, None
        if not isinstance(decision, dict):
            return False, None
        accepted = decision.get("accepted") is True
        reason = decision.get("reason")
        return accepted and reason == "clinic_confirm_template_verified", decision

    def _perform_controlled_tap(
        self,
        *,
        adb: Any,
        tap: dict[str, Any],
        target: str,
        control_driver: CooperativeRunnerDriver | None,
        safety_screenshot_dir: str | Path | None,
        settle_seconds: float,
        structured_action_guard: Any | None = None,
    ) -> dict[str, Any] | None:
        structured_safety: dict[str, Any] | None = None
        pre_action_frame: dict[str, Any] | None = None
        if control_driver is not None:
            try:
                control_driver.reach("before_tap")
            except ControlledStop:
                return {
                    "continue": False,
                    "performed": False,
                    "reason": "control_stop_before_tap",
                }

        if structured_action_guard is not None:
            try:
                frame_path = self._capture_safety_frame(
                    adb,
                    "structured_pre_tap",
                    target,
                    safety_screenshot_dir=safety_screenshot_dir,
                )
                frame_bytes = frame_path.read_bytes()
                if not frame_bytes:
                    return {
                        "continue": False,
                        "performed": False,
                        "reason": "structured_execution_frame_empty",
                    }
                structured_safety = structured_action_guard.authorize(
                    frame_path=frame_path,
                    coordinate_name=tap.get("coordinate_name"),
                )
                if (
                    isinstance(structured_safety, dict)
                    and structured_safety.get("allowed") is True
                ):
                    pre_action_frame = {
                        "coordinate_name": tap.get("coordinate_name"),
                        "source_path": str(frame_path.resolve()),
                        "source_sha256": hashlib.sha256(frame_bytes).hexdigest(),
                    }
            except Exception:
                structured_safety = {
                    "allowed": False,
                    "reason": "structured_execution_guard_failed",
                }
            if (
                not isinstance(structured_safety, dict)
                or structured_safety.get("allowed") is not True
            ):
                reason = (
                    str(structured_safety.get("reason"))
                    if isinstance(structured_safety, dict)
                    and structured_safety.get("reason")
                    else "structured_execution_guard_rejected"
                )
                return {
                    "continue": False,
                    "performed": False,
                    "reason": reason,
                    "structured_action_safety": structured_safety,
                }

        tap_ok = tap_without_settle(adb, tap["x"], tap["y"], scaled=tap.get("scaled", False))
        if tap_ok is not True:
            return {
                "continue": False,
                "performed": False,
                "reason": "adb_tap_outcome_unknown",
            }
        self.sleep_fn(settle_seconds)
        if control_driver is None and structured_safety is None:
            return None

        try:
            frame_path = self._capture_safety_frame(
                adb,
                "post_tap_fresh",
                target,
                safety_screenshot_dir=safety_screenshot_dir,
            )
            frame_bytes = frame_path.read_bytes()
        except Exception:
            return {
                "continue": False,
                "performed": True,
                "reason": "post_tap_fresh_frame_capture_failed",
            }
        if not frame_bytes:
            return {
                "continue": False,
                "performed": True,
                "reason": "post_tap_fresh_frame_empty",
            }
        evidence = FreshFrameEvidence(
            path=str(frame_path),
            sha256=hashlib.sha256(frame_bytes).hexdigest(),
            captured_at=self.wall_clock(),
        )
        if control_driver is not None:
            try:
                control_driver.after_fresh_frame(evidence)
            except ControlledStop:
                return {
                    "continue": False,
                    "performed": True,
                    "reason": "control_stop_after_fresh_frame",
                    "fresh_frame": evidence,
                }
        return {
            "continue": True,
            "performed": True,
            "fresh_frame": evidence,
            **(
                {"pre_action_frame": pre_action_frame}
                if pre_action_frame is not None
                else {}
            ),
            **(
                {"structured_action_safety": structured_safety}
                if structured_safety is not None
                else {}
            ),
        }

    @classmethod
    def _tap_outcome_result(
        cls,
        outcome: dict[str, Any],
        *,
        target: str | None,
        tap_plan: list[dict[str, Any]],
        fresh_frames: list[FreshFrameEvidence],
        **extra: Any,
    ) -> dict[str, Any]:
        all_frames = list(fresh_frames)
        latest = outcome.get("fresh_frame")
        if isinstance(latest, FreshFrameEvidence):
            all_frames.append(latest)
        result = {
            "performed": (
                False
                if outcome.get("reason") == "adb_tap_outcome_unknown"
                else bool(all_frames) or outcome.get("performed") is True
            ),
            "target": target,
            "reason": str(outcome["reason"]),
            "tap_plan": tap_plan,
            **extra,
        }
        structured_action_safety = outcome.get(
            "structured_action_safety"
        )
        if isinstance(structured_action_safety, dict):
            result["structured_action_safety"] = (
                structured_action_safety
            )
        if all_frames:
            result["fresh_frames"] = [cls._fresh_frame_record(item) for item in all_frames]
        return result

    @staticmethod
    def _append_structured_action_safety(
        safety_checks: list[dict[str, Any]],
        outcome: dict[str, Any] | None,
    ) -> None:
        if not isinstance(outcome, dict):
            return
        structured = outcome.get("structured_action_safety")
        if isinstance(structured, dict):
            safety_checks.append(
                {
                    "label": "structured_action_safety",
                    **structured,
                }
            )

    @staticmethod
    def _fresh_frame_record(evidence: FreshFrameEvidence) -> dict[str, Any]:
        return {
            "path": evidence.path,
            "sha256": evidence.sha256,
            "captured_at": evidence.captured_at,
        }

    @classmethod
    def _structured_post_action_frame_record(
        cls,
        outcome: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(outcome, dict):
            return None
        evidence = outcome.get("fresh_frame")
        safety = outcome.get("structured_action_safety")
        if (
            not isinstance(evidence, FreshFrameEvidence)
            or not isinstance(safety, dict)
        ):
            return None
        return {
            **cls._fresh_frame_record(evidence),
            "structured_action_safety": dict(safety),
        }

    @staticmethod
    def _append_fresh_frame(
        fresh_frames: list[FreshFrameEvidence],
        outcome: dict[str, Any] | None,
    ) -> None:
        if outcome is None:
            return
        evidence = outcome.get("fresh_frame")
        if isinstance(evidence, FreshFrameEvidence) and outcome.get("continue") is True:
            fresh_frames.append(evidence)

    @classmethod
    def _fresh_frames_report(
        cls, fresh_frames: list[FreshFrameEvidence]
    ) -> dict[str, Any]:
        if not fresh_frames:
            return {}
        return {
            "fresh_frames": [cls._fresh_frame_record(item) for item in fresh_frames]
        }

    def _capture_safety_frame(
        self,
        adb: Any,
        label: str,
        target: str,
        *,
        safety_screenshot_dir: str | Path | None,
    ) -> Path:
        screenshot_dir = (
            Path(safety_screenshot_dir)
            if safety_screenshot_dir is not None
            else self.screenshot_dir
        )
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000)
        stem = f"ug_parent_{stamp}_{target}_{label}"
        if len(str((screenshot_dir / f"{stem}.png").absolute())) >= 240:
            # Leave room for the verifier's normalized image suffix on Windows.
            # Target and phase remain recorded in the execution report.
            stem = f"{stamp:x}"
        path = screenshot_dir / f"{stem}.png"
        suffix = 1
        while path.exists():
            path = screenshot_dir / f"{stem}_{suffix}.png"
            suffix += 1
        adb.screenshot(str(path))
        return path

    @staticmethod
    def _verification_record(label: str, result: Any, image_path: Path) -> dict[str, Any]:
        return {
            "label": label,
            "accepted": bool(getattr(result, "accepted", False)),
            "reason": str(getattr(result, "reason", "unknown")),
            "selected_tab": getattr(result, "selected_tab", None),
            "failures": list(getattr(result, "failures", []) or []),
            "image_path": str(image_path),
            "evidence": getattr(result, "evidence", {}) or {},
        }

    def _main_menu_verification_record(self, image_path: Path) -> dict[str, Any]:
        try:
            observation = self.main_menu_analyzer(image_path.read_bytes())
        except (OSError, ValueError, TypeError):
            observation = {}
        fields = observation.get("fields", {}) if isinstance(observation, dict) else {}
        screen_field = fields.get("screen", {}) if isinstance(fields, dict) else {}
        screen = screen_field.get("value") if isinstance(screen_field, dict) else None
        valid_for_training = bool(
            observation.get("numeric_state_complete", observation.get("valid_for_training", False))
            if isinstance(observation, dict)
            else False
        )
        accepted = screen == "main_menu" and valid_for_training
        return {
            "label": "main_menu",
            "accepted": accepted,
            "reason": "main_menu_verified" if accepted else "main_menu_not_verified",
            "screen": screen,
            "valid_for_training": valid_for_training,
            "image_path": str(image_path),
            "evidence": observation if isinstance(observation, dict) else {},
        }

    @staticmethod
    def _main_menu_vitality_percent(observation: Any) -> float | None:
        if not isinstance(observation, dict):
            return None
        fields = observation.get("fields")
        if not isinstance(fields, dict):
            return None
        vitality = fields.get("vitality")
        if not isinstance(vitality, dict) or vitality.get("abstained") is True:
            return None
        value = vitality.get("value")
        if isinstance(value, dict):
            percent = value.get("percent")
            if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                value = float(percent)
            else:
                ratio = value.get("ratio")
                if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
                    value = float(ratio) * 100.0
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        percent = float(value)
        if not 0.0 <= percent <= 100.0:
            return None
        return round(percent, 4)
