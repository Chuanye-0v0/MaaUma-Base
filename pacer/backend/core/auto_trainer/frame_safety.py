from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import math
from pathlib import Path
import subprocess
from typing import Any, Callable

import cv2

from core.frame_io import io_path, read_image_compat
from core.screen_text import analyze_screen_text
from core.skill_acquisition_parser import resolve_tesseract_runtime


FRAME_SAFETY_SCHEMA_VERSION = "ug_parent.frame_safety.v1"

EXPLICIT_ESCAPE_ACTION_IDS = frozenset(
    {
        "safe_advance.clock_retry_cancel",
        "safe_advance.bottom_right_skip",
        "safe_advance.announcement_close",
        "safe_advance.trophy_reward_close",
        "safe_advance.training_pass_reset_close",
        "safe_advance.pass_renewal_cancel",
        "safe_advance.exchange_points_expiry_close",
        "safe_advance.recruit_return_home",
        "global_hub.center_home",
        "race_list.back",
        "race_participant_list.back",
        "race_detail.back",
        "race_confirm.grand_masters_final_cancel",
    }
)

GENERIC_ACTION_IDS = frozenset(
    {
        "safe_advance.safe_confirm",
        "safe_advance.fullscreen_continue",
    }
)

GENERIC_REASONS = frozenset(
    {
        "confirm_safe_modal",
        "continue_fullscreen_result",
    }
)

IMAGE_BOUND_ACTION_REASONS = {
    "safe_advance.larc_opening_movie_skip": "skip_larc_opening_movie",
    "safe_advance.inheritance_factor_confirm": "inheritance_factor_confirm",
    "race_list.scan_for_visible_g1": "scan_race_list_for_visible_g1",
}
_RAPIDOCR_MIN_CONFIDENCE = 0.85
_RAPIDOCR_FAILURE_REASONS = frozenset(
    {
        "rapidocr_module_unavailable",
        "rapidocr_initialization_failed",
        "rapidocr_inference_failed",
    }
)


@dataclass(frozen=True)
class FrameSafetyPacket:
    schema_version: str
    source_image_sha256: str
    visible_text: str
    text_source: str
    text_status: str
    resource_terms: tuple[str, ...]
    dangerous: bool
    negative_resource_evidence_trusted: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ActionSafetyVerdict:
    allowed: bool
    reason: str
    action_id: str | None
    source_image_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LocalTesseractScreenTextReader:
    def read(self, image_path: Path) -> str:
        runtime = resolve_tesseract_runtime()
        languages = (
            "chi_sim+eng"
            if "chi_sim" in runtime.available_languages
            else "eng"
        )
        completed = subprocess.run(
            [
                str(runtime.executable),
                str(image_path),
                "stdout",
                "--tessdata-dir",
                str(runtime.tessdata_dir),
                "-l",
                languages,
                "--psm",
                "6",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise RuntimeError(f"tesseract_exit:{completed.returncode}")
        return completed.stdout


class RapidOCRTextReaderError(RuntimeError):
    """Fixed, non-textual failure category from the local RapidOCR reader."""


class LocalRapidOCRScreenTextReader:
    def __init__(
        self,
        *,
        engine_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._engine_factory = engine_factory
        self._engine: Any | None = None

    def read(self, image_path: Path) -> str:
        image = read_image_compat(image_path)
        if image is None:
            raise RapidOCRTextReaderError("rapidocr_inference_failed")
        return _join_high_confidence_rapidocr_text(self.read_regions(image))

    def read_regions(self, image: Any) -> Any:
        """Read OCR boxes from an already-decoded, caller-bound image."""
        engine = self._get_engine()
        try:
            response = engine(image)
            result = response[0]
        except Exception as exc:
            raise RapidOCRTextReaderError("rapidocr_inference_failed") from exc
        return result or ()

    def read_line(self, image: Any) -> tuple[str, float]:
        """Recognize one caller-cropped text line without box detection."""
        engine = self._get_engine()
        try:
            result = engine(image, use_det=False, use_cls=False)[0]
            if len(result) != 1 or len(result[0]) != 2:
                raise ValueError("single_line_result_required")
            return str(result[0][0]), float(result[0][1])
        except Exception as exc:
            raise RapidOCRTextReaderError("rapidocr_inference_failed") from exc

    def _get_engine(self) -> Any:
        if self._engine_factory is None:
            try:
                from core.local_ocr_runtime import default_rapidocr_engine
                return default_rapidocr_engine()
            except ImportError as exc:
                raise RapidOCRTextReaderError(
                    "rapidocr_module_unavailable"
                ) from exc
            except Exception as exc:
                raise RapidOCRTextReaderError("rapidocr_initialization_failed") from exc
        if self._engine is not None:
            return self._engine
        try:
            self._engine = self._engine_factory()
        except Exception as exc:
            raise RapidOCRTextReaderError(
                "rapidocr_initialization_failed"
            ) from exc
        return self._engine


def _join_high_confidence_rapidocr_text(items: Any) -> str:
    ordered: list[tuple[float, float, str]] = []
    try:
        iterator = iter(items)
    except TypeError:
        return ""
    for item in iterator:
        try:
            box = item[0]
            text = str(item[1]).strip()
            confidence = float(item[2])
            position = _rapidocr_visual_position(box)
        except (IndexError, TypeError, ValueError):
            continue
        if not text or position is None or not math.isfinite(confidence):
            continue
        if confidence < _RAPIDOCR_MIN_CONFIDENCE:
            # Low-confidence status text is excluded, but payment/resource
            # language remains a conservative safety signal rather than being
            # silently converted into a safe frame.
            if not bool(analyze_screen_text(text).get("dangerous")):
                continue
        ordered.append((position[0], position[1], text))
    ordered.sort(key=lambda item: (item[0], item[1]))
    return "\n".join(item[2] for item in ordered)


def _rapidocr_visual_position(box: Any) -> tuple[float, float] | None:
    try:
        points = [(float(point[0]), float(point[1])) for point in box]
    except (IndexError, TypeError, ValueError):
        return None
    if not points or not all(
        math.isfinite(coordinate) for point in points for coordinate in point
    ):
        return None
    return (
        sum(point[1] for point in points) / len(points),
        sum(point[0] for point in points) / len(points),
    )


class FrameSafetyAnalyzer:
    def __init__(
        self,
        text_reader: Callable[[Path], str] | None = None,
        rapidocr_reader: Callable[[Path], str] | None = None,
    ) -> None:
        self.text_reader = text_reader or LocalTesseractScreenTextReader().read
        self.rapidocr_reader: Callable[[Path], str] | None = (
            None
            if text_reader is not None and rapidocr_reader is None
            else rapidocr_reader or LocalRapidOCRScreenTextReader().read
        )

    def analyze(
        self,
        image_path: str | Path,
        *,
        supplied_text: str | None = None,
        _decoded_frame: tuple[bytes, Any] | None = None,
    ) -> FrameSafetyPacket:
        path = Path(image_path)
        frame_bytes = io_path(path).read_bytes()
        digest = hashlib.sha256(frame_bytes).hexdigest()
        if supplied_text is not None:
            visible_text = supplied_text
            text_source = "supplied"
            errors: tuple[str, ...] = ()
        elif self.rapidocr_reader is None:
            text_source = "local_tesseract"
            try:
                visible_text = self.text_reader(path)
            except Exception:
                visible_text = ""
                errors = ("tesseract_reader_failed",)
            else:
                errors = ()
        else:
            rapidocr_failure: str | None = None
            try:
                # Race preview already decoded this frame. Only the standard
                # full reader may reuse those pixels, and only for the same file.
                reader = getattr(self.rapidocr_reader, "__self__", None)
                if (_decoded_frame is not None and frame_bytes == _decoded_frame[0]
                        and type(reader) is LocalRapidOCRScreenTextReader
                        and getattr(self.rapidocr_reader, "__func__", None) is LocalRapidOCRScreenTextReader.read
                        and reader._engine_factory is None):
                    visible_text = _join_high_confidence_rapidocr_text(reader.read_regions(_decoded_frame[1]))
                else:
                    visible_text = self.rapidocr_reader(path)
            except Exception as exc:
                visible_text = ""
                reason = str(exc)
                rapidocr_failure = (
                    reason
                    if reason in _RAPIDOCR_FAILURE_REASONS
                    else "rapidocr_inference_failed"
                )
            if visible_text.strip():
                text_source = "local_rapidocr"
                errors = ()
            else:
                if rapidocr_failure is None:
                    rapidocr_failure = "rapidocr_no_text"
                text_source = "local_tesseract_fallback"
                try:
                    visible_text = self.text_reader(path)
                except Exception:
                    visible_text = ""
                    errors = (
                        rapidocr_failure,
                        "tesseract_fallback_failed",
                    )
                else:
                    errors = () if visible_text.strip() else (
                        rapidocr_failure,
                        "tesseract_fallback_no_text",
                    )
        text_status = "ok" if visible_text.strip() else "abstained"

        analysis = analyze_screen_text(visible_text)
        resource_terms = tuple(
            str(value) for value in analysis.get("resource_terms", [])
        )
        return FrameSafetyPacket(
            schema_version=FRAME_SAFETY_SCHEMA_VERSION,
            source_image_sha256=digest,
            visible_text=visible_text,
            text_source=text_source,
            text_status=text_status,
            resource_terms=resource_terms,
            dangerous=bool(analysis.get("dangerous")),
            negative_resource_evidence_trusted=False,
            errors=errors,
        )


class FormalActionSafetyGate:
    def evaluate(
        self,
        packet: FrameSafetyPacket,
        decision: dict[str, Any],
        *,
        authorized_resource_action: str | None = None,
    ) -> ActionSafetyVerdict:
        tap = decision.get("tap")
        gesture = decision.get("gesture")
        keyevent = decision.get("keyevent")
        action = decision.get("action")
        if action == "back":
            action_id = (
                keyevent.get("coordinate_name") if isinstance(keyevent, dict) else None
            )
        else:
            action_id = (
                tap.get("coordinate_name")
                if isinstance(tap, dict)
                else gesture.get("coordinate_name")
                if isinstance(gesture, dict)
                else None
            )
        reason = str(decision.get("reason", ""))

        def verdict(allowed: bool, why: str) -> ActionSafetyVerdict:
            return ActionSafetyVerdict(
                allowed=allowed,
                reason=why,
                action_id=str(action_id) if action_id is not None else None,
                source_image_sha256=packet.source_image_sha256,
            )

        if action == "back":
            if not (
                isinstance(keyevent, dict)
                and decision.get("reason") == "leave_race_participant_list"
                and action_id == "race_participant_list.back"
                and type(keyevent.get("keycode")) is int
                and keyevent["keycode"] == 4
            ):
                return verdict(False, "invalid_back_action")
        elif action not in {"tap", "wait", "swipe"}:
            return verdict(True, "non_executing_decision")
        if action == "wait":
            return verdict(True, "wait")
        image_bound_action = (
            isinstance(action_id, str)
            and IMAGE_BOUND_ACTION_REASONS.get(action_id) == reason
        )
        if (
            packet.text_status != "ok"
            and action_id not in EXPLICIT_ESCAPE_ACTION_IDS
            and not image_bound_action
        ):
            return verdict(False, "frame_text_safety_abstained")
        permitted_clock_retry = (action == "tap" and action_id == authorized_resource_action
            and (action_id, reason) in {("race.clock_retry", "retry_lost_race"),
                                       ("race.clock_retry_open", "open_lost_race_retry")})
        # A recovery skill's description is not a TP purchase. The caller must
        # have recognized the skill list and its exact Back on this same frame;
        # neither an arbitrary escape ID nor a resource-keyword edit is enough.
        skill_return = (decision.get("evidence") or {}).get("skill_page_return", {})
        permitted_skill_return = (
            action == "tap"
            and (action_id, reason, decision.get("route_state")) in {
                ("startup.skills.back", "startup.skills.back", "skill_acquisition"),
                ("arc.skills.back", "arc.skills.back", "skill_acquisition"),
            }
            and packet.text_status == "ok" and not packet.errors
            and set(packet.resource_terms) == {"恢复体力"}
            and isinstance(skill_return, dict)
            and skill_return.get("source_image_sha256") == packet.source_image_sha256
            and skill_return.get("skill_page_accepted") is True
            and skill_return.get("confirm_modal_visible") is False
            and skill_return.get("back_point") == [tap.get("x"), tap.get("y")]
        )
        if (packet.dangerous and action_id not in EXPLICIT_ESCAPE_ACTION_IDS
                and not permitted_clock_retry and not permitted_skill_return):
            return verdict(False, "dangerous_frame_action_blocked")
        if permitted_skill_return:
            return verdict(True, "identified_free_skill_return_allowed")
        if packet.text_status != "ok" and image_bound_action:
            return verdict(True, "image_bound_action_allowed")
        if action_id in GENERIC_ACTION_IDS and reason in GENERIC_REASONS:
            return verdict(False, "generic_action_semantics_required")
        return verdict(True, "specific_action_allowed")


__all__ = [
    "ActionSafetyVerdict",
    "EXPLICIT_ESCAPE_ACTION_IDS",
    "FormalActionSafetyGate",
    "FRAME_SAFETY_SCHEMA_VERSION",
    "FrameSafetyAnalyzer",
    "FrameSafetyPacket",
    "GENERIC_ACTION_IDS",
    "GENERIC_REASONS",
    "IMAGE_BOUND_ACTION_REASONS",
    "LocalRapidOCRScreenTextReader",
    "LocalTesseractScreenTextReader",
    "RapidOCRTextReaderError",
]
