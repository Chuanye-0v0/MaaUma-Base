from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable

from config.settings import ADB_ENDPOINT
from core.auto_trainer.career_start_contracts import (
    ACTION_ID,
    ACTION_REASON,
    ALLOWED_SOURCE_SIZES,
    DESTINATION_STATE,
    POST_MAX_CAPTURES,
    POST_MAX_SECONDS,
    POST_POLL_SECONDS,
    SOURCE_STATE,
    SOURCE_MAX_AGE_NS,
    CareerStartDryRunPlan,
    CareerStartDestinationPacket,
    CareerStartPlan,
    CareerStartSourcePacket,
    canonical_bytes,
    canonical_sha256,
)
from core.auto_trainer.career_start_perception import (
    DESTINATION_REQUIRED_NAVIGATION_SLOTS,
    HOME_REQUIRED_NAVIGATION_SLOTS,
    CareerStartPerceptor,
)
from core.auto_trainer.career_start_safety import (
    CareerStartSafetyBundle,
    validate_career_start_safety_bundle,
)
from core.auto_trainer.frame_safety import FormalActionSafetyGate


EXPECTED_ENDPOINT = ADB_ENDPOINT
EXECUTION_SCHEMA = "uma.career_start_execution.v1"
DEFAULT_MARKER_ROOT = (
    Path(__file__).resolve().parents[2] / "runs" / "career_start_consumed"
)
_AUTH_SENTINEL = object()


class CareerStartSafetyError(RuntimeError):
    pass


class CareerStartPlanner:
    def __init__(self, *, action_table_path: str | Path | None = None) -> None:
        self.action_table_path = Path(
            action_table_path
            or Path(__file__).resolve().parents[2]
            / "config"
            / "auto_trainer_actions.json"
        )

    def plan(
        self,
        packet: CareerStartSourcePacket,
        safety: CareerStartSafetyBundle,
        *,
        endpoint: str,
        now_ns: int,
    ) -> CareerStartPlan:
        source_sha = getattr(packet, "source_image_sha256", "0" * 64)

        def stop(reason: str) -> CareerStartPlan:
            return CareerStartPlan.stop(reason, source_image_sha256=source_sha)

        if type(packet) is not CareerStartSourcePacket:
            return stop("source_packet_type")
        if type(now_ns) is not int or now_ns < packet.captured_monotonic_ns:
            return stop("source_clock_invalid")
        if endpoint != EXPECTED_ENDPOINT:
            return stop("endpoint_mismatch")
        try:
            action = self._load_action()
        except Exception:
            return stop("action_metadata_invalid")
        if not _source_packet_contract_valid(packet):
            return stop("source_contract_invalid")
        if now_ns > packet.expires_monotonic_ns:
            return stop("source_stale")
        if type(safety) is not CareerStartSafetyBundle:
            return stop("safety_bundle_type")
        if not validate_career_start_safety_bundle(
            safety,
            source_size=packet.source_image_size,
        ):
            return stop("safety_derivation_invalid")
        if not _source_scope_matches_packet(packet, safety):
            return stop("source_scope_mismatch")
        if (
            safety.source_image_sha256 != packet.source_image_sha256
            or safety.raw_packet.source_image_sha256 != packet.source_image_sha256
            or safety.scoped_packet.source_image_sha256 != packet.source_image_sha256
            or safety.derivation_digest != packet.scoped_safety_digest
        ):
            return stop("safety_digest_mismatch")
        if safety.scoped_packet.text_status != "ok" or safety.scoped_packet.dangerous:
            return stop("frame_safety_rejected")

        return CareerStartPlan(
            action="tap",
            reason=ACTION_REASON,
            action_id=ACTION_ID,
            endpoint=endpoint,
            source_image_sha256=packet.source_image_sha256,
            packet_digest=canonical_sha256(packet),
            canonical_bbox=packet.career_button.canonical_bbox,
            canonical_center=packet.career_button.canonical_center,
            device_bbox=packet.career_button.device_bbox,
            device_center=packet.career_button.device_center,
            source_state=str(action["expected_source_state"]),
            destination_state=str(action["expected_destination_state"]),
        )

    def _load_action(self) -> dict[str, Any]:
        payload = json.loads(self.action_table_path.read_text(encoding="utf-8"))
        action = payload["career_start_route"]["global_home_enter_career"]
        if not isinstance(action, dict):
            raise ValueError("action")
        expected = {
            "action": "tap",
            "reason": ACTION_REASON,
            "coordinate_name": ACTION_ID,
            "expected_source_state": SOURCE_STATE,
            "expected_destination_state": DESTINATION_STATE,
        }
        if action != expected or any(key in action for key in ("point", "x", "y")):
            raise ValueError("action_metadata")
        return action


def execution_fingerprint(plan: CareerStartPlan) -> str:
    return canonical_sha256(
        {
            "schema_version": EXECUTION_SCHEMA,
            "endpoint": plan.endpoint,
            "source_image_sha256": plan.source_image_sha256,
            "packet_digest": plan.packet_digest,
            "plan_digest": canonical_sha256(plan),
            "action_id": plan.action_id,
            "source_state": plan.source_state,
            "destination_state": plan.destination_state,
        }
    )


def _source_packet_contract_valid(packet: CareerStartSourcePacket) -> bool:
    if type(packet) is not CareerStartSourcePacket:
        return False
    if not (
        packet.source_image_size in ALLOWED_SOURCE_SIZES
        and packet.canonical_size == (720, 1280)
        and packet.orientation == "portrait"
        and packet.page_state == SOURCE_STATE
        and packet.page_confidence == 1.0
        and packet.top_status_bar
        and packet.global_home_nav
        and packet.home_tab_selected
        and packet.career_button_count == 1
        and packet.career_button is not None
        and packet.active_forbidden_page is None
        and not packet.blocking_modal
        and not packet.errors
        and packet.expires_monotonic_ns - packet.captured_monotonic_ns
        == SOURCE_MAX_AGE_NS
    ):
        return False
    button = packet.career_button
    if button.action_id != ACTION_ID:
        return False
    x, y, width, height = button.canonical_bbox
    cx, cy = button.canonical_center
    if not (
        width > 0
        and height > 0
        and 0 <= x < 720
        and 0 <= y < 1280
        and x + width <= 720
        and y + height <= 1280
        and x <= cx < x + width
        and y <= cy < y + height
        and 720 * 0.62 <= cx <= 720 * 0.98
        and 1280 * 0.78 <= cy <= 1280 * 0.92
        and width >= 720 * 0.18
        and height >= 1280 * 0.035
    ):
        return False
    scale = packet.source_image_size[0] // 720
    if packet.source_image_size != (720 * scale, 1280 * scale):
        return False
    if button.device_bbox != tuple(value * scale for value in button.canonical_bbox):
        return False
    if button.device_center != tuple(
        value * scale for value in button.canonical_center
    ):
        return False
    dx, dy, dwidth, dheight = button.device_bbox
    dcx, dcy = button.device_center
    return bool(
        dx >= 0
        and dy >= 0
        and dx + dwidth <= packet.source_image_size[0]
        and dy + dheight <= packet.source_image_size[1]
        and dx <= dcx < dx + dwidth
        and dy <= dcy < dy + dheight
    )


def _plan_matches_source(
    plan: CareerStartPlan,
    packet: CareerStartSourcePacket,
    *,
    endpoint: str,
) -> bool:
    button = packet.career_button
    if type(plan) is not CareerStartPlan or button is None:
        return False
    return bool(
        plan.action == "tap"
        and plan.reason == ACTION_REASON
        and plan.action_id == ACTION_ID
        and plan.endpoint == EXPECTED_ENDPOINT
        and plan.endpoint == endpoint
        and plan.source_image_sha256 == packet.source_image_sha256
        and plan.packet_digest == canonical_sha256(packet)
        and plan.source_state == SOURCE_STATE
        and plan.destination_state == DESTINATION_STATE
        and plan.canonical_bbox == button.canonical_bbox
        and plan.canonical_center == button.canonical_center
        and plan.device_bbox == button.device_bbox
        and plan.device_center == button.device_center
    )


def _source_scope_matches_packet(
    packet: CareerStartSourcePacket,
    safety: CareerStartSafetyBundle,
) -> bool:
    scope = safety.scope
    return bool(
        scope.top_status_bar is packet.top_status_bar
        and scope.global_home_nav is packet.global_home_nav
        and scope.home_tab_selected is packet.home_tab_selected
        and scope.career_button_count == packet.career_button_count
        and scope.blocking_modal is packet.blocking_modal
        and scope.trusted_navigation_context == "none"
        and scope.trusted_navigation_slots == HOME_REQUIRED_NAVIGATION_SLOTS
    )


def _post_binding_valid(
    packet: CareerStartDestinationPacket,
    safety: CareerStartSafetyBundle,
    actual_sha256: str,
) -> bool:
    if type(packet) is not CareerStartDestinationPacket:
        return False
    if type(safety) is not CareerStartSafetyBundle:
        return False
    if not validate_career_start_safety_bundle(
        safety,
        source_size=safety.source_size,
    ):
        return False
    if not (
        packet.source_image_sha256 == actual_sha256
        and safety.source_image_sha256 == actual_sha256
        and safety.raw_packet.source_image_sha256 == actual_sha256
        and safety.scoped_packet.source_image_sha256 == actual_sha256
        and packet.raw_safety_digest == canonical_sha256(safety.raw_packet.to_dict())
        and packet.scoped_safety_digest
        == canonical_sha256(safety.scoped_packet.to_dict())
    ):
        return False
    if packet.page_state == "known_loading":
        common_loading = bool(
            not safety.scope.top_status_bar
            and not safety.scope.global_home_nav
            and safety.scope.career_button_count == 0
            and not safety.scope.blocking_modal
            and not safety.scoped_packet.resource_terms
            and not safety.scoped_packet.errors
            and packet.scenario_id is None
            and not packet.selected
            and not packet.anchors
            and packet.active_forbidden_page is None
            and not packet.blocking_modal
            and not packet.errors
        )
        strict_spinner_scope = bool(
            not safety.scope.home_tab_selected
            and safety.scope.trusted_navigation_context == "none"
            and not safety.scope.trusted_navigation_slots
        )
        scenario_skeleton_scope = bool(
            safety.scope.home_tab_selected
            and safety.scope.trusted_navigation_context == "scenario_select_loading"
            and safety.scope.trusted_navigation_slots
            == DESTINATION_REQUIRED_NAVIGATION_SLOTS
        )
        return common_loading and (strict_spinner_scope or scenario_skeleton_scope)
    if safety.scoped_packet.text_status != "ok" or safety.scoped_packet.dangerous:
        return False
    if packet.page_state == DESTINATION_STATE:
        expected_bool_anchors = {
            "portrait_status_bar",
            "scenario_title_region",
            "grand_masters_hero_signature",
            "scenario_carousel",
            "continue_button_enabled",
            "bottom_home_nav",
        }
        if set(packet.anchors) != expected_bool_anchors | {
            "continue_button_count"
        }:
            return False
        return bool(
            packet.scenario_id == "grand_masters"
            and packet.selected
            and not safety.scope.top_status_bar
            and not safety.scope.global_home_nav
            and safety.scope.home_tab_selected
            and safety.scope.career_button_count == 0
            and safety.scope.blocking_modal is packet.blocking_modal
            and safety.scope.trusted_navigation_context
            == "grand_masters_destination"
            and safety.scope.trusted_navigation_slots
            == DESTINATION_REQUIRED_NAVIGATION_SLOTS
            and packet.active_forbidden_page is None
            and not packet.blocking_modal
            and not packet.errors
            and type(packet.anchors["continue_button_count"]) is int
            and packet.anchors["continue_button_count"] == 1
            and all(
                type(packet.anchors[key]) is bool and packet.anchors[key]
                for key in expected_bool_anchors
            )
        )
    if packet.page_state == "global_home_still_visible":
        return bool(
            safety.scope.top_status_bar
            and safety.scope.global_home_nav
            and safety.scope.home_tab_selected
            and safety.scope.career_button_count == 1
            and not safety.scope.blocking_modal
            and safety.scope.trusted_navigation_context == "none"
            and safety.scope.trusted_navigation_slots
            == HOME_REQUIRED_NAVIGATION_SLOTS
            and packet.scenario_id is None
            and not packet.selected
            and not packet.anchors
            and packet.active_forbidden_page is None
            and not packet.blocking_modal
            and not packet.errors
        )
    return True


class LiveCareerStartAuthorization:
    __slots__ = (
        "_fingerprint",
        "_nonce",
        "_plan_digest",
        "_state",
        "_lock",
        "_tap_callable",
    )

    def __init__(
        self,
        sentinel: object,
        plan: CareerStartPlan,
        tap_callable: Callable[[], bool | None],
        nonce: str,
    ) -> None:
        if sentinel is not _AUTH_SENTINEL:
            raise CareerStartSafetyError("live_authorization_private")
        if type(plan) is not CareerStartPlan or plan.action != "tap":
            raise CareerStartSafetyError("live_authorization_plan")
        if not callable(tap_callable) or not isinstance(nonce, str) or not nonce:
            raise CareerStartSafetyError("live_authorization_input")
        self._fingerprint = execution_fingerprint(plan)
        self._plan_digest = canonical_sha256(plan)
        self._nonce = nonce
        self._state = "fresh"
        self._lock = threading.Lock()
        self._tap_callable = tap_callable

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def plan_digest(self) -> str:
        return self._plan_digest

    def tap_enter_career(self) -> bool | None:
        with self._lock:
            if self._state != "fresh":
                raise CareerStartSafetyError("tap_capability_consumed")
            self._state = "consumed"
        return self._tap_callable()


def _create_live_authorization(
    plan: CareerStartPlan,
    adb: object,
    *,
    expires_monotonic_ns: int,
    monotonic_ns: Callable[[], int],
    nonce: str | None = None,
) -> LiveCareerStartAuthorization:
    if type(plan) is not CareerStartPlan or plan.action != "tap":
        raise CareerStartSafetyError("authorization_plan")
    if plan.device_center is None:
        raise CareerStartSafetyError("authorization_center")
    if (
        type(expires_monotonic_ns) is not int
        or expires_monotonic_ns <= 0
        or not callable(monotonic_ns)
    ):
        raise CareerStartSafetyError("authorization_expiry")
    device_center = tuple(plan.device_center)

    def tap_bound_coordinate() -> bool | None:
        if monotonic_ns() > expires_monotonic_ns:
            raise CareerStartSafetyError("authorization_expired")
        return adb.tap(device_center[0], device_center[1], scaled=False)

    return LiveCareerStartAuthorization(
        _AUTH_SENTINEL,
        plan,
        tap_bound_coordinate,
        nonce or secrets.token_hex(16),
    )


def _flush_marker(handle: Any) -> None:
    handle.flush()


def _write_durable_exclusive(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _persist_pre_action_evidence(
    evidence_root: str | Path,
    source_png_bytes: bytes,
    packet: CareerStartSourcePacket,
    safety: CareerStartSafetyBundle,
    plan: CareerStartPlan,
) -> dict[str, object]:
    root = Path(evidence_root)
    source_path = root / f"source_{packet.source_image_sha256}.png"
    evidence_path = root / f"pre_action_{canonical_sha256(plan)}.json"
    try:
        _write_durable_exclusive(source_path, source_png_bytes)
    except FileExistsError:
        if source_path.read_bytes() != source_png_bytes:
            raise CareerStartSafetyError("persisted_source_content_mismatch")
    persisted_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if persisted_sha != packet.source_image_sha256:
        raise CareerStartSafetyError("persisted_source_sha_mismatch")
    evidence = {
        "schema_version": EXECUTION_SCHEMA,
        "source_packet": packet.to_dict(),
        "safety": {
            "source_image_sha256": safety.source_image_sha256,
            "raw_packet": safety.raw_packet.to_dict(),
            "scoped_packet": safety.scoped_packet.to_dict(),
            "tokens": [token.to_dict() for token in safety.tokens],
            "exclusions": [item.to_dict() for item in safety.exclusions],
            "derivation_digest": safety.derivation_digest,
            "scope": safety.scope.to_dict(),
            "source_size": safety.source_size,
        },
        "plan": plan.to_dict(),
        "persisted_source_sha256": persisted_sha,
    }
    _write_durable_exclusive(evidence_path, canonical_bytes(evidence))
    return {
        "source_path": str(source_path),
        "evidence_path": str(evidence_path),
        "source_image_sha256": persisted_sha,
        "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
    }


def _consume_authorization(
    authorization: object,
    *,
    expected_plan: CareerStartPlan,
) -> dict[str, object]:
    if type(authorization) is not LiveCareerStartAuthorization:
        return {"reason": "live_authorization_type", "tap_count": 0}
    if (
        type(expected_plan) is not CareerStartPlan
        or authorization.plan_digest != canonical_sha256(expected_plan)
        or authorization.fingerprint != execution_fingerprint(expected_plan)
    ):
        return {"reason": "live_authorization_binding", "tap_count": 0}

    root = Path(DEFAULT_MARKER_ROOT)
    marker = root / f"{authorization.fingerprint}.json"
    try:
        root.mkdir(parents=True, exist_ok=True)
        with marker.open("xb") as handle:
            handle.write(
                canonical_bytes(
                    {
                        "schema_version": EXECUTION_SCHEMA,
                        "execution_fingerprint": authorization.fingerprint,
                        "plan_digest": authorization.plan_digest,
                        "state": "consumed_before_transport",
                    }
                )
            )
            _flush_marker(handle)
            os.fsync(handle.fileno())
    except FileExistsError:
        return {
            "reason": "execution_already_consumed",
            "tap_count": 0,
            "execution_fingerprint": authorization.fingerprint,
        }
    except Exception as exc:
        return {
            "reason": f"consumption_marker_failed:{type(exc).__name__}",
            "tap_count": 0,
            "execution_fingerprint": authorization.fingerprint,
        }

    try:
        transported = authorization.tap_enter_career()
    except CareerStartSafetyError as exc:
        if str(exc) == "authorization_expired":
            return {
                "reason": "authorization_expired",
                "tap_count": 0,
                "execution_fingerprint": authorization.fingerprint,
            }
        transported = None
    except Exception:
        transported = None
    if transported is True:
        return {
            "reason": "tap_transport_confirmed",
            "tap_count": 1,
            "execution_fingerprint": authorization.fingerprint,
        }
    return {
        "reason": "tap_transport_unknown",
        "tap_count": "unknown",
        "execution_fingerprint": authorization.fingerprint,
    }


class ScreenshotOnlyADBFacade:
    __slots__ = ("_capture",)

    def __init__(self, adb: object, *, capture_root: str | Path) -> None:
        direct = getattr(adb, "capture_png_bytes", None)
        root = Path(capture_root)

        if callable(direct):
            def capture(deadline: float | None = None) -> bytes:
                return direct(deadline=deadline)

            self._capture = capture
        else:
            counter = iter(range(1_000_000))

            def capture(deadline: float | None = None) -> bytes:
                root.mkdir(parents=True, exist_ok=True)
                path = root / f"career_start_capture_{next(counter):06d}.png"
                image = adb.screenshot(str(path), deadline=deadline)
                if image is None or not path.exists():
                    raise RuntimeError("screenshot_failed")
                return path.read_bytes()

            self._capture = capture

    def capture_png_bytes(self, *, deadline: float | None = None) -> bytes:
        value = self._capture(deadline)
        if not isinstance(value, bytes) or not value:
            raise RuntimeError("screenshot_bytes_invalid")
        return value


class CareerStartRouteExecutor:
    def __init__(
        self,
        *,
        perceptor: CareerStartPerceptor | None = None,
        planner: CareerStartPlanner | None = None,
        safety_gate: FormalActionSafetyGate | None = None,
        capture_root: str | Path | None = None,
        evidence_root: str | Path | None = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.perceptor = perceptor or CareerStartPerceptor()
        self.planner = planner or CareerStartPlanner()
        self.safety_gate = safety_gate or FormalActionSafetyGate()
        marker_root = Path(DEFAULT_MARKER_ROOT)
        self.capture_root = Path(capture_root or marker_root / "captures")
        self.evidence_root = Path(
            evidence_root or marker_root / "evidence"
        )
        self.monotonic_ns = monotonic_ns
        self.monotonic = monotonic
        self.sleep = sleep

    def run_dry_run(
        self,
        source_png_bytes: bytes,
        post_png_bytes: bytes | list[bytes] | tuple[bytes, ...],
        *,
        now_ns: int,
    ) -> dict[str, object]:
        packet, safety = self.perceptor.analyze_source(
            source_png_bytes,
            captured_monotonic_ns=now_ns,
        )
        plan = self.planner.plan(
            packet,
            safety,
            endpoint=EXPECTED_ENDPOINT,
            now_ns=now_ns,
        )
        post_frames = (
            (post_png_bytes,)
            if isinstance(post_png_bytes, bytes)
            else tuple(post_png_bytes)
        )
        post_results = [
            self.perceptor.analyze_destination(frame) for frame in post_frames
        ]
        post_packets = [packet.to_dict() for packet, _safety in post_results]
        post_safety_digests = [
            safety.derivation_digest for _packet, safety in post_results
        ]
        return {
            "mode": "dry-run",
            "tap_count": 0,
            "source_packet": packet.to_dict(),
            "source_safety_digest": safety.derivation_digest,
            "dry_run_plan": CareerStartDryRunPlan(plan=plan).to_dict(),
            "post_packet": post_packets[-1] if post_packets else None,
            "post_packets": post_packets,
            "post_safety_digests": post_safety_digests,
        }

    def run_live(self, adb: object, *, endpoint: str) -> dict[str, object]:
        screenshots = ScreenshotOnlyADBFacade(adb, capture_root=self.capture_root)
        freshness_recaptures = 0
        while True:
            try:
                source_png_bytes, packet, safety = self._capture_source(screenshots)
            except Exception as exc:
                label = "source_recapture_failed" if freshness_recaptures else "source_capture_failed"
                return self._stop(f"{label}:{type(exc).__name__}", 0)
            plan = self.planner.plan(
                packet,
                safety,
                endpoint=endpoint,
                now_ns=self.monotonic_ns(),
            )
            if plan.reason == "source_stale" and freshness_recaptures == 0:
                freshness_recaptures += 1
                continue
            if plan.action != "tap":
                return self._stop(plan.reason, 0, plan=plan)

            decision = {
                "action": "tap",
                "reason": plan.reason,
                "tap": {
                    "coordinate_name": plan.action_id,
                    "point": plan.device_center,
                },
            }
            gate = self.safety_gate.evaluate(safety.scoped_packet, decision)
            if (
                not gate.allowed
                or gate.action_id != ACTION_ID
                or gate.source_image_sha256 != packet.source_image_sha256
                or not _plan_matches_source(plan, packet, endpoint=endpoint)
            ):
                return self._stop("formal_safety_gate_rejected", 0, plan=plan)

            try:
                pre_action_evidence = _persist_pre_action_evidence(
                    self.evidence_root,
                    source_png_bytes,
                    packet,
                    safety,
                    plan,
                )
            except Exception as exc:
                return self._stop(
                    f"pre_action_evidence_failed:{type(exc).__name__}",
                    0,
                    plan=plan,
                )
            if self.monotonic_ns() > packet.expires_monotonic_ns:
                if freshness_recaptures == 0:
                    freshness_recaptures += 1
                    continue
                return self._stop("source_stale", 0, plan=plan)
            break

        authorization = _create_live_authorization(
            plan,
            adb,
            expires_monotonic_ns=packet.expires_monotonic_ns,
            monotonic_ns=self.monotonic_ns,
        )
        transport = _consume_authorization(
            authorization,
            expected_plan=plan,
        )
        del authorization
        transport["pre_action_evidence"] = pre_action_evidence
        if transport["tap_count"] != 1:
            return {**transport, "plan": plan.to_dict()}

        deadline = self.monotonic() + POST_MAX_SECONDS
        post_evidence: list[dict[str, object]] = []
        self.sleep(POST_POLL_SECONDS)
        for capture_index in range(POST_MAX_CAPTURES):
            if self.monotonic() > deadline:
                break
            try:
                png_bytes = screenshots.capture_png_bytes(deadline=deadline)
                if self.monotonic() > deadline:
                    break
                post_packet, post_safety = self.perceptor.analyze_destination(png_bytes)
            except Exception as exc:
                return {
                    **transport,
                    "reason": f"post_capture_failed:{type(exc).__name__}",
                    "plan": plan.to_dict(),
                    "post_evidence": post_evidence,
                }
            actual_sha = hashlib.sha256(png_bytes).hexdigest()
            if (
                post_packet.source_image_sha256 != actual_sha
                or post_safety.source_image_sha256 != actual_sha
                or not _post_binding_valid(post_packet, post_safety, actual_sha)
            ):
                return {
                    **transport,
                    "reason": "post_frame_binding_mismatch",
                    "plan": plan.to_dict(),
                    "post_evidence": post_evidence,
                }
            post_evidence.append(
                {
                    "packet": post_packet.to_dict(),
                    "safety_digest": post_safety.derivation_digest,
                }
            )
            if post_packet.page_state == DESTINATION_STATE:
                return {
                    **transport,
                    "reason": "verified_destination",
                    "plan": plan.to_dict(),
                    "post_evidence": post_evidence,
                }
            if post_packet.page_state not in {
                "global_home_still_visible",
                "known_loading",
            }:
                return {
                    **transport,
                    "reason": post_packet.page_state,
                    "plan": plan.to_dict(),
                    "post_evidence": post_evidence,
                }
            if capture_index + 1 < POST_MAX_CAPTURES:
                self.sleep(POST_POLL_SECONDS)

        return {
            **transport,
            "reason": "post_verification_timeout",
            "plan": plan.to_dict(),
            "post_evidence": post_evidence,
        }

    def _capture_source(self, screenshots: ScreenshotOnlyADBFacade):
        png_bytes = screenshots.capture_png_bytes()
        captured_ns = self.monotonic_ns()
        packet, safety = self.perceptor.analyze_source(
            png_bytes,
            captured_monotonic_ns=captured_ns,
        )
        actual_sha = hashlib.sha256(png_bytes).hexdigest()
        if (
            packet.source_image_sha256 != actual_sha
            or safety.source_image_sha256 != actual_sha
        ):
            raise CareerStartSafetyError("source_frame_binding_mismatch")
        return png_bytes, packet, safety

    @staticmethod
    def _stop(
        reason: str,
        tap_count: int | str,
        *,
        plan: CareerStartPlan | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {"reason": reason, "tap_count": tap_count}
        if plan is not None:
            result["plan"] = plan.to_dict()
        return result


__all__ = [
    "CareerStartPlanner",
    "CareerStartRouteExecutor",
    "CareerStartSafetyError",
    "EXPECTED_ENDPOINT",
    "LiveCareerStartAuthorization",
    "ScreenshotOnlyADBFacade",
    "execution_fingerprint",
]
