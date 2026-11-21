from __future__ import annotations

import csv
import hashlib
import io
import re
import subprocess
from pathlib import Path

import cv2
import numpy as np

from core.auto_trainer.career_setup_allowlist import (
    CareerSetupAllowlist,
    IdentityEvidence,
)
from core.auto_trainer.career_setup_contracts import CareerSetupPagePacket
from core.auto_trainer.career_setup_friend_support import (
    analyze_friend_support_structure,
)
from core.auto_trainer.career_start_safety import (
    LocalizedOcrToken,
    TesseractTsvTokenReader,
)
from core.auto_trainer.frame_safety import LocalRapidOCRScreenTextReader
from core.auto_trainer.safe_advance import SafeAdvancePlanner
from core.perception import Perception
from core.character_names import character_candidates, match_character_name
from core.name_match import is_confident, match_character
from core.skill_acquisition_parser import _read_name_rapidocr, resolve_tesseract_runtime


TRAINEE_SEARCH_BBOX = (15, 630, 705, 1030)
TRAINEE_NAME_BBOX = (35, 410, 170, 447)
TRAINEE_MATCH_THRESHOLD = 0.94
TRAINEE_MATCH_MARGIN = 0.10
PARENT_MATCH_THRESHOLD = 0.94
SUPPORT_MATCH_THRESHOLD = 0.94

_FORBIDDEN_START_TERMS = (
    "胡萝卜",
    "萝卜",
    "恢复TP",
    "恢复体力",
    "购买",
    "购入",
    "付费",
    "有偿",
    "充值",
    "支付",
    "商店",
    "商城",
)

_AUTO_SELECT_CONFIRM_TERMS = (
    "自动选择确认",
    "是否重置当前选择",
    "统一选择继承优俊少女",
    "包含借用",
    "借用会消耗金币",
)


def _decode_and_normalize(png_bytes: bytes) -> np.ndarray | None:
    frame = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    height, width = frame.shape[:2]
    if (width, height) == (720, 1280):
        return frame
    if (width, height) == (1440, 2560):
        return cv2.resize(frame, (720, 1280), interpolation=cv2.INTER_AREA)
    return None


def _load_identity_template(evidence: IdentityEvidence) -> np.ndarray:
    frame = cv2.imread(evidence.source_path, cv2.IMREAD_GRAYSCALE)
    if frame is None or frame.shape[:2] != (1280, 720):
        raise ValueError(f"identity_template_source_invalid:{evidence.identity_id}")
    x1, y1, x2, y2 = evidence.canonical_bbox
    template = frame[y1:y2, x1:x2]
    if template.size == 0:
        raise ValueError(f"identity_template_empty:{evidence.identity_id}")
    return template


def _nms_two_scores(
    response: np.ndarray,
    template_size: tuple[int, int],
) -> tuple[float, tuple[int, int], float]:
    _minimum, best_score, _minimum_loc, best_loc = cv2.minMaxLoc(response)
    suppressed = response.copy()
    template_width, template_height = template_size
    x, y = best_loc
    suppressed[
        max(0, y - template_height) : min(suppressed.shape[0], y + template_height),
        max(0, x - template_width) : min(suppressed.shape[1], x + template_width),
    ] = -1.0
    _minimum, second_score, _minimum_loc, _second_loc = cv2.minMaxLoc(suppressed)
    return float(best_score), (int(x), int(y)), float(second_score)


def _trainee_green_selection_marker(
    frame: np.ndarray,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
) -> tuple[bool, tuple[float, float, float, float]]:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    regions = (
        hsv[max(0, y - 25) : min(1280, y + height + 25), max(0, x - 30) : x],
        hsv[max(0, y - 25) : min(1280, y + height + 25), x + width : min(720, x + width + 30)],
        hsv[max(0, y - 25) : y, max(0, x - 15) : min(720, x + width + 15)],
        hsv[y + height : min(1280, y + height + 25), max(0, x - 15) : min(720, x + width + 15)],
    )
    ratios: list[float] = []
    for region in regions:
        if region.size == 0:
            ratios.append(0.0)
            continue
        green = (
            (region[:, :, 0] >= 35)
            & (region[:, :, 0] <= 90)
            & (region[:, :, 1] >= 120)
            & (region[:, :, 2] >= 100)
        )
        ratios.append(float(np.mean(green)))
    left, right, top, bottom = ratios
    selected = bool(
        left >= 0.08
        and right >= 0.08
        and max(top, bottom) >= 0.06
    )
    return selected, (left, right, top, bottom)


def _normalize_name(raw_text: str) -> str | None:
    candidates = character_candidates()
    identifier, score, basis = match_character(raw_text, candidates)
    if not is_confident(score, basis):
        return None
    return next((row["name"] for row in candidates if row["character_id"] == identifier), None)


def _read_trainee_name(frame: np.ndarray) -> tuple[str | None, str, str]:
    x1, y1, x2, y2 = TRAINEE_NAME_BBOX
    crop = frame[y1:y2, x1:x2]

    def rapid_fallback(raw_text: str = "") -> tuple[str | None, str, str]:
        try:
            rapid = _read_name_rapidocr(crop)
        except Exception:
            return None, raw_text, "localized_tesseract_alias_v1"
        rapid_text = str(rapid.get("text", "")).strip()
        return _normalize_name(rapid_text), rapid_text, "rapidocr_alias_v1"

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    white_ink = ((hsv[:, :, 1] < 120) & (hsv[:, :, 2] > 130)).astype(np.uint8) * 255
    binary = 255 - cv2.resize(
        white_ink,
        None,
        fx=4,
        fy=4,
        interpolation=cv2.INTER_NEAREST,
    )
    ok, encoded = cv2.imencode(".png", binary)
    if not ok:
        return rapid_fallback()
    runtime = resolve_tesseract_runtime()
    languages = [
        language for language in ("chi_sim", "eng") if language in runtime.available_languages
    ]
    if "chi_sim" not in languages:
        return rapid_fallback()
    completed = subprocess.run(
        [
            str(runtime.executable),
            "stdin",
            "stdout",
            "--tessdata-dir",
            str(runtime.tessdata_dir),
            "-l",
            "+".join(languages),
            "--psm",
            "7",
            "-c",
            "tessedit_create_tsv=1",
        ],
        input=bytes(encoded),
        capture_output=True,
        timeout=10,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        return rapid_fallback()
    rows = csv.DictReader(
        io.StringIO(completed.stdout.decode("utf-8", errors="replace")),
        delimiter="\t",
        quoting=csv.QUOTE_NONE,
    )
    tokens: list[str] = []
    for row in rows:
        text = str(row.get("text", "")).strip()
        try:
            confidence = float(row.get("conf", "-1"))
        except (TypeError, ValueError):
            continue
        if text and confidence >= 0:
            tokens.append(text)
    raw_text = "".join(tokens)
    normalized = _normalize_name(raw_text)
    if normalized is not None:
        return normalized, raw_text, "localized_tesseract_alias_v1"
    return rapid_fallback(raw_text)


class CareerSetupPerceptor:
    def __init__(
        self,
        allowlist: CareerSetupAllowlist,
        token_reader: object | None = None,
        screen_text_reader: object | None = None,
    ) -> None:
        self.allowlist = allowlist
        self._token_reader = token_reader or TesseractTsvTokenReader().read
        self._screen_text_reader = (
            screen_text_reader or LocalRapidOCRScreenTextReader().read_regions
        )
        self._trainee_template = _load_identity_template(allowlist.trainee)
        self._parent_templates = tuple(
            _load_identity_template(parent) for parent in allowlist.inheritance_parents
        )
        self._support_templates = tuple(
            _load_identity_template(card) for card in allowlist.support_cards
        )

    def _read_tokens(self, png_bytes: bytes) -> tuple[LocalizedOcrToken, ...]:
        reader = self._token_reader
        values = reader(png_bytes) if callable(reader) else reader.read(png_bytes)
        return tuple(values)

    def _read_screen_tokens(
        self,
        frame: np.ndarray,
    ) -> tuple[tuple[str, tuple[int, int, int, int]], ...]:
        reader = self._screen_text_reader
        try:
            regions = reader(frame) if callable(reader) else reader.read_regions(frame)
        except Exception:
            return ()
        tokens: list[tuple[str, tuple[int, int, int, int]]] = []
        for item in regions or ():
            try:
                box = item[0]
                text = str(item[1]).strip()
                confidence = float(item[2])
                xs = [float(point[0]) for point in box]
                ys = [float(point[1]) for point in box]
            except (IndexError, TypeError, ValueError):
                continue
            if not text or confidence < 0.9:
                continue
            tokens.append(
                (
                    text,
                    (
                        int(min(xs)),
                        int(min(ys)),
                        int(max(xs)) + 1,
                        int(max(ys)) + 1,
                    ),
                )
            )
        return tuple(tokens)

    def _modal_confirm_button(
        self,
        frame: np.ndarray,
        perception: dict[str, object],
        *,
        required_terms: tuple[str, ...],
    ) -> tuple[int, int, int, int] | None:
        buttons = perception.get("buttons", ())
        modal_buttons = [
            button
            for button in buttons
            if button.get("kind") == "primary_green"
            and button.get("score", 0.0) >= 0.8
            and 330 <= button["bbox"][0] <= 430
            and 740 <= button["bbox"][1] <= 880
            and 220 <= button["bbox"][2] <= 330
            and 50 <= button["bbox"][3] <= 110
        ]
        if len(modal_buttons) != 1:
            return None
        tokens = self._read_screen_tokens(frame)
        visible_text = re.sub(r"\s+", "", "".join(text for text, _bbox in tokens))
        if not all(term in visible_text for term in required_terms):
            return None
        cancel = next((bbox for text, bbox in tokens if text == "取消"), None)
        confirm = next((bbox for text, bbox in tokens if text == "确定"), None)
        if cancel is None or confirm is None:
            return None
        cancel_center = ((cancel[0] + cancel[2]) // 2, (cancel[1] + cancel[3]) // 2)
        confirm_center = ((confirm[0] + confirm[2]) // 2, (confirm[1] + confirm[3]) // 2)
        if not (cancel_center[0] < 360 < confirm_center[0]):
            return None
        button = modal_buttons[0]
        x, y, width, height = button["bbox"]
        if not (
            x <= confirm_center[0] < x + width
            and y <= confirm_center[1] < y + height
            and y <= cancel_center[1] < y + height
        ):
            return None
        return (x, y, x + width, y + height)

    def _auto_select_confirm_button(
        self,
        frame: np.ndarray,
        perception: dict[str, object],
    ) -> tuple[int, int, int, int] | None:
        return self._modal_confirm_button(
            frame,
            perception,
            required_terms=_AUTO_SELECT_CONFIRM_TERMS,
        )

    def _support_auto_formation_confirm_button(
        self,
        frame: np.ndarray,
        perception: dict[str, object],
    ) -> tuple[int, int, int, int] | None:
        return self._modal_confirm_button(
            frame,
            perception,
            required_terms=(
                "自动编成确认",
                "是否重置当前卡组",
                "统一编成协助卡",
                "当前编成卡组会被覆盖",
            ),
        )

    def _auto_selected_inheritance_page(
        self,
        frame: np.ndarray,
        continue_button: tuple[int, int],
    ) -> bool:
        tokens = self._read_screen_tokens(frame)
        visible_text = re.sub(r"\s+", "", "".join(text for text, _bbox in tokens))
        if not all(
            term in visible_text
            for term in ("选择继承优俊少女", "第1人", "第2人", "自动选择", "继续")
        ):
            return False
        first = next((bbox for text, bbox in tokens if text == "第1人"), None)
        second = next((bbox for text, bbox in tokens if text == "第2人"), None)
        continue_text = next((bbox for text, bbox in tokens if text == "继续"), None)
        if first is None or second is None or continue_text is None:
            return False
        first_center = ((first[0] + first[2]) // 2, (first[1] + first[3]) // 2)
        second_center = ((second[0] + second[2]) // 2, (second[1] + second[3]) // 2)
        continue_center = (
            (continue_text[0] + continue_text[2]) // 2,
            (continue_text[1] + continue_text[3]) // 2,
        )
        return bool(
            first_center[0] < 360 < second_center[0]
            and 600 <= first_center[1] <= 760
            and 600 <= second_center[1] <= 760
            and abs(continue_center[0] - continue_button[0]) <= 100
            and abs(continue_center[1] - continue_button[1]) <= 60
        )

    def _filled_inheritance_auto_select_button(
        self,
        frame: np.ndarray,
        continue_button: tuple[int, int],
    ) -> tuple[int, int, int, int] | None:
        if not self._auto_selected_inheritance_page(frame, continue_button):
            return None
        tokens = self._read_screen_tokens(frame)
        if not any(text == "选择继承优俊少女" for text, _bbox in tokens):
            return None
        auto_text = next((bbox for text, bbox in tokens if text == "自动选择"), None)
        if auto_text is None:
            return None
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        origin_x, origin_y = 360, 922
        mask = cv2.inRange(
            hsv[origin_y:1012, origin_x:676],
            np.array([35, 60, 80], dtype=np.uint8),
            np.array([92, 255, 255], dtype=np.uint8),
        )
        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_CLOSE,
            np.ones((7, 7), dtype=np.uint8),
            iterations=2,
        )
        candidates: list[tuple[int, int, int, int]] = []
        contours, _hierarchy = cv2.findContours(
            mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if 0 <= x <= 20 and 15 <= y <= 40 and 280 <= width <= 316 and 35 <= height <= 70:
                candidates.append(
                    (origin_x + x, origin_y + y, origin_x + x + width, origin_y + y + height)
                )
        if len(candidates) != 1:
            return None
        button = candidates[0]
        if not (
            button[0] <= auto_text[0] < auto_text[2] <= button[2]
            and button[1] <= auto_text[1] < auto_text[3] <= button[3]
        ):
            return None
        return button

    def _support_auto_formation_button(
        self,
        frame: np.ndarray,
        *,
        page_match: bool,
        empty_friend: tuple[int, int] | None,
    ) -> tuple[int, int, int, int] | None:
        if not page_match or empty_friend is None:
            return None
        tokens = self._read_screen_tokens(frame)
        visible_text = re.sub(r"\s+", "", "".join(text for text, _bbox in tokens))
        if not all(
            term in visible_text
            for term in ("协助卡编成", "自动编成", "好友", "开始养成")
        ):
            return None
        auto_text = next((bbox for text, bbox in tokens if text == "自动编成"), None)
        if auto_text is None:
            return None
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        origin_x, origin_y = 340, 880
        crop = hsv[origin_y:980, origin_x:700]
        mask = cv2.inRange(
            crop,
            np.array([35, 60, 80], dtype=np.uint8),
            np.array([92, 255, 255], dtype=np.uint8),
        )
        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_CLOSE,
            np.ones((7, 7), dtype=np.uint8),
            iterations=2,
        )
        candidates: list[tuple[int, int, int, int]] = []
        contours, _hierarchy = cv2.findContours(
            mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if 20 <= x <= 60 and 15 <= y <= 55 and 260 <= width <= 330 and 35 <= height <= 70:
                candidates.append(
                    (origin_x + x, origin_y + y, origin_x + x + width, origin_y + y + height)
                )
        if len(candidates) != 1:
            return None
        button = candidates[0]
        text_center = (
            (auto_text[0] + auto_text[2]) // 2,
            (auto_text[1] + auto_text[3]) // 2,
        )
        if not (
            button[0] <= text_center[0] < button[2]
            and button[1] <= text_center[1] < button[3]
        ):
            return None
        return button

    def _auto_formed_support_deck(
        self,
        frame: np.ndarray,
        *,
        page_match: bool,
        empty_friend: tuple[int, int] | None,
    ) -> bool:
        if not page_match or empty_friend is not None:
            return False
        tokens = self._read_screen_tokens(frame)
        visible_text = re.sub(r"\s+", "", "".join(text for text, _bbox in tokens))
        if not all(
            term in visible_text
            for term in ("协助卡编成", "卡组", "好友", "自动编成", "开始养成")
        ):
            return False
        if sum(text.startswith("等级") for text, _bbox in tokens) < 6:
            return False
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        occupied_ratios = []
        for card in self.allowlist.support_cards:
            x1, y1, x2, y2 = card.canonical_bbox
            crop = hsv[y1:y2, x1:x2]
            occupied_ratios.append(
                float(np.mean((crop[:, :, 1] > 55) & (crop[:, :, 2] > 70)))
            )
        return all(ratio >= 0.35 for ratio in occupied_ratios)

    @staticmethod
    def _training_points_equation(
        tokens: tuple[tuple[str, tuple[int, int, int, int]], ...],
    ) -> bool:
        visible_text = re.sub(r"\s+", "", "".join(text for text, _bbox in tokens))
        cost_match = re.search(r"消耗(\d{1,3})训练值开始养成", visible_text)
        if cost_match is None or "最终确认" not in visible_text:
            return False
        cost = int(cost_match.group(1))
        balance_digits = "".join(
            text
            for text, bbox in sorted(tokens, key=lambda item: item[1][0])
            if 1080 <= bbox[1] <= 1140 and re.fullmatch(r"\d{1,6}", text)
        )
        solutions = []
        for split in range(1, len(balance_digits)):
            before = int(balance_digits[:split])
            after = int(balance_digits[split:])
            if before >= cost and after == before - cost:
                solutions.append((before, after))
        return len(solutions) == 1

    @staticmethod
    def _invalid_packet(png_bytes: bytes, family: str) -> CareerSetupPagePacket:
        return CareerSetupPagePacket(
            page_state="invalid_frame",
            source_sha256=hashlib.sha256(png_bytes).hexdigest(),
            page_family=family,
            errors=("image_decode_or_size",),
        )

    def analyze_trainee(self, png_bytes: bytes) -> CareerSetupPagePacket:
        source_sha = hashlib.sha256(png_bytes).hexdigest()
        frame = _decode_and_normalize(png_bytes)
        if frame is None:
            return self._invalid_packet(png_bytes, "trainee")
        perception = Perception().analyze_image(frame)
        button = SafeAdvancePlanner._trainee_select_continue_button(perception, frame)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        x1, y1, x2, y2 = TRAINEE_SEARCH_BBOX
        search = gray[y1:y2, x1:x2]
        response = cv2.matchTemplate(search, self._trainee_template, cv2.TM_CCOEFF_NORMED)
        template_height, template_width = self._trainee_template.shape[:2]
        best, local_point, second = _nms_two_scores(
            response,
            (template_width, template_height),
        )
        match_x = x1 + local_point[0]
        match_y = y1 + local_point[1]
        candidate_match = bool(
            best >= TRAINEE_MATCH_THRESHOLD
            and best - second >= TRAINEE_MATCH_MARGIN
        )
        selected_marker, marker_ratios = _trainee_green_selection_marker(
            frame,
            x=match_x,
            y=match_y,
            width=template_width,
            height=template_height,
        )
        visual_match = bool(
            candidate_match
            and selected_marker
        )
        normalized_name, _raw_name, name_matcher = _read_trainee_name(frame)
        name_match = match_character_name(normalized_name, self.allowlist.trainee.display_name)
        candidate_ready = bool(button is not None and candidate_match and not visual_match)
        errors: list[str] = []
        if button is None:
            errors.append("trainee_page_geometry_mismatch")
        if not visual_match and not candidate_ready:
            errors.append("trainee_visual_mismatch")
        if visual_match and not name_match:
            errors.append("trainee_name_mismatch")
        accepted = button is not None and visual_match and name_match
        if candidate_ready:
            target_bbox = (
                match_x,
                match_y,
                match_x + template_width,
                match_y + template_height,
            )
        elif button is not None:
            target_bbox = (button[0] - 140, button[1] - 48, button[0] + 140, button[1] + 48)
        else:
            target_bbox = None
        actionable = accepted or candidate_ready
        return CareerSetupPagePacket(
            page_state=(
                "trainee_select_ready"
                if accepted
                else "trainee_allowlisted_candidate_ready"
                if candidate_ready
                else "identity_mismatch"
            ),
            source_sha256=source_sha,
            page_family="trainee",
            action_id=(
                "safe_advance.trainee_continue"
                if accepted
                else "career_setup.select_allowlisted_trainee"
                if candidate_ready
                else None
            ),
            canonical_target_bbox=target_bbox,
            identities=(
                self.allowlist.trainee.identity_id if actionable else "unknown",
            ),
            identity_matches=(actionable,),
            matcher_versions=(
                ("name", name_matcher),
                (
                    "visual",
                    "candidate_roi_ncc_green_marker_v2:"
                    f"{best:.6f}:{second:.6f}:"
                    + ",".join(f"{ratio:.4f}" for ratio in marker_ratios),
                ),
            ),
            errors=tuple(errors),
        )

    def analyze_inheritance(
        self,
        png_bytes: bytes,
        *,
        allow_auto_selected: bool = False,
        allow_filled_auto_select: bool = False,
    ) -> CareerSetupPagePacket:
        source_sha = hashlib.sha256(png_bytes).hexdigest()
        frame = _decode_and_normalize(png_bytes)
        if frame is None:
            return self._invalid_packet(png_bytes, "inheritance")
        perception = Perception().analyze_image(frame)
        confirm = self._auto_select_confirm_button(frame, perception)
        if confirm is not None:
            return CareerSetupPagePacket(
                page_state="inheritance_auto_select_borrow_confirm_ready",
                source_sha256=source_sha,
                page_family="inheritance",
                action_id="career_setup.confirm_inheritance_auto_select_borrow",
                canonical_target_bbox=confirm,
                matcher_versions=(
                    ("screen_text", "rapidocr_exact_auto_select_borrow_v1"),
                    ("visual", "modal_confirm_primary_green_bound_v1"),
                ),
                resource_policy="ordinary_game_gold_borrow_allowed",
            )
        auto_select = SafeAdvancePlanner._inheritance_auto_select_button(frame)
        if auto_select is not None:
            return CareerSetupPagePacket(
                page_state="inheritance_auto_select_ready",
                source_sha256=source_sha,
                page_family="inheritance",
                action_id="safe_advance.inheritance_auto_select",
                canonical_target_bbox=(360, 922, 676, 1012),
                matcher_versions=(("visual", "empty_parent_slots_auto_select_v1"),),
            )
        button = SafeAdvancePlanner._inheritance_select_continue_button(perception, frame)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        matches: list[bool] = []
        scores: list[float] = []
        for parent, template in zip(
            self.allowlist.inheritance_parents,
            self._parent_templates,
            strict=True,
        ):
            x1, y1, x2, y2 = parent.canonical_bbox
            observed = gray[y1:y2, x1:x2]
            if observed.shape != template.shape:
                score = -1.0
            else:
                score = float(
                    cv2.matchTemplate(
                        observed,
                        template,
                        cv2.TM_CCOEFF_NORMED,
                    )[0, 0]
                )
            scores.append(score)
            matches.append(score >= PARENT_MATCH_THRESHOLD)
        if (
            allow_filled_auto_select
            and not allow_auto_selected
            and button is not None
            and not all(matches)
        ):
            filled_auto_select = self._filled_inheritance_auto_select_button(frame, button)
            if filled_auto_select is not None:
                return CareerSetupPagePacket(
                    page_state="inheritance_auto_select_ready",
                    source_sha256=source_sha,
                    page_family="inheritance",
                    action_id="safe_advance.inheritance_auto_select",
                    canonical_target_bbox=filled_auto_select,
                    matcher_versions=(
                        ("screen_text", "rapidocr_exact_inheritance_auto_select_v1"),
                        ("visual", "filled_parent_slots_auto_select_bound_v1"),
                    ),
                )
        auto_selected = bool(
            allow_auto_selected
            and button is not None
            and self._auto_selected_inheritance_page(frame, button)
        )
        if auto_selected:
            return CareerSetupPagePacket(
                page_state="inheritance_select_ready",
                source_sha256=source_sha,
                page_family="inheritance",
                action_id="safe_advance.inheritance_continue",
                canonical_target_bbox=(
                    button[0] - 140,
                    button[1] - 48,
                    button[0] + 140,
                    button[1] + 48,
                ),
                identities=("auto_selected_parent_1", "auto_selected_parent_2"),
                identity_matches=(True, True),
                matcher_versions=(
                    ("screen_text", "rapidocr_auto_selected_parent_slots_v1"),
                    ("visual", "inheritance_continue_bound_v1"),
                ),
            )
        accepted = button is not None and all(matches)
        errors: list[str] = []
        if button is None:
            errors.append("inheritance_page_geometry_mismatch")
        if not all(matches):
            errors.append("inheritance_identity_mismatch")
        target_bbox = None
        if button is not None:
            target_bbox = (button[0] - 140, button[1] - 48, button[0] + 140, button[1] + 48)
        identities = tuple(
            parent.identity_id if matched else "unknown"
            for parent, matched in zip(
                self.allowlist.inheritance_parents,
                matches,
                strict=True,
            )
        )
        return CareerSetupPagePacket(
            page_state="inheritance_select_ready" if accepted else "identity_mismatch",
            source_sha256=source_sha,
            page_family="inheritance",
            action_id="safe_advance.inheritance_continue" if accepted else None,
            canonical_target_bbox=target_bbox,
            identities=identities,
            identity_matches=tuple(matches),
            matcher_versions=(
                ("visual", "ordered_parent_roi_ncc_v1:" + ",".join(f"{score:.6f}" for score in scores)),
            ),
            errors=tuple(errors),
        )

    def analyze_support(
        self,
        png_bytes: bytes,
        *,
        allow_auto_formed: bool = False,
    ) -> CareerSetupPagePacket:
        source_sha = hashlib.sha256(png_bytes).hexdigest()
        frame = _decode_and_normalize(png_bytes)
        if frame is None:
            return self._invalid_packet(png_bytes, "support")
        perception = Perception().analyze_image(frame)
        formation_confirm = self._support_auto_formation_confirm_button(
            frame,
            perception,
        )
        if formation_confirm is not None:
            return CareerSetupPagePacket(
                page_state="support_auto_formation_confirm_ready",
                source_sha256=source_sha,
                page_family="support",
                action_id="career_setup.confirm_support_auto_formation",
                canonical_target_bbox=formation_confirm,
                matcher_versions=(
                    ("screen_text", "rapidocr_exact_support_auto_formation_confirm_v1"),
                    ("visual", "modal_confirm_primary_green_bound_v1"),
                ),
                resource_policy="no_resource_spend",
            )
        page_match = SafeAdvancePlanner._looks_like_support_deck_page(frame)
        empty_friend = SafeAdvancePlanner._support_deck_empty_friend_slot_button(frame)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        matches: list[bool] = []
        scores: list[float] = []
        for card, template in zip(
            self.allowlist.support_cards,
            self._support_templates,
            strict=True,
        ):
            x1, y1, x2, y2 = card.canonical_bbox
            observed = gray[y1:y2, x1:x2]
            score = -1.0
            if observed.shape == template.shape:
                score = float(
                    cv2.matchTemplate(observed, template, cv2.TM_CCOEFF_NORMED)[0, 0]
                )
            scores.append(score)
            matches.append(score >= SUPPORT_MATCH_THRESHOLD)
        auto_formation = self._support_auto_formation_button(
            frame,
            page_match=page_match,
            empty_friend=empty_friend,
        )
        if auto_formation is not None and not all(matches[:5]):
            return CareerSetupPagePacket(
                page_state="support_auto_formation_ready",
                source_sha256=source_sha,
                page_family="support",
                action_id="career_setup.auto_form_support_deck",
                canonical_target_bbox=auto_formation,
                matcher_versions=(
                    ("screen_text", "rapidocr_support_auto_formation_v1"),
                    ("visual", "support_page_empty_friend_auto_button_v1"),
                ),
            )
        auto_formed = bool(
            allow_auto_formed
            and self._auto_formed_support_deck(
                frame,
                page_match=page_match,
                empty_friend=empty_friend,
            )
        )
        if auto_formed:
            return CareerSetupPagePacket(
                page_state="support_select_ready",
                source_sha256=source_sha,
                page_family="support",
                action_id="safe_advance.support_deck_start",
                canonical_target_bbox=(250, 1032, 470, 1128),
                identities=tuple(f"auto_formed_support_{index}" for index in range(1, 7)),
                identity_matches=(True,) * 6,
                matcher_versions=(
                    ("screen_text", "rapidocr_auto_formed_six_card_deck_v1"),
                    ("visual", "six_occupied_support_slots_v1"),
                ),
            )
        accepted = bool(page_match and empty_friend is None and all(matches))
        empty_ready = bool(
            page_match
            and empty_friend is not None
            and all(matches[:5])
            and not matches[5]
        )
        matcher_summary = "ordered_support_roi_ncc_v1:" + ",".join(
            f"{score:.6f}" for score in scores
        )
        if empty_ready:
            return CareerSetupPagePacket(
                page_state="support_friend_empty_ready",
                source_sha256=source_sha,
                page_family="support",
                action_id="career_setup.open_friend_support",
                canonical_target_bbox=self.allowlist.support_cards[-1].canonical_bbox,
                identities=tuple(
                    card.identity_id for card in self.allowlist.support_cards[:5]
                ) + ("empty_friend_slot",),
                identity_matches=(True,) * 6,
                matcher_versions=(("visual", matcher_summary),),
            )
        errors: list[str] = []
        if not page_match:
            errors.append("support_page_geometry_mismatch")
        if empty_friend is not None:
            errors.append("support_friend_empty")
        if not all(matches):
            errors.append("support_identity_mismatch")
        identities = tuple(
            card.identity_id if matched else "unknown"
            for card, matched in zip(self.allowlist.support_cards, matches, strict=True)
        )
        return CareerSetupPagePacket(
            page_state="support_select_ready" if accepted else "identity_mismatch",
            source_sha256=source_sha,
            page_family="support",
            action_id="safe_advance.support_deck_start" if accepted else None,
            canonical_target_bbox=(250, 1032, 470, 1128) if accepted else None,
            identities=identities,
            identity_matches=tuple(matches),
            matcher_versions=(("visual", matcher_summary),),
            errors=tuple(errors),
        )

    def analyze_start_confirm(self, png_bytes: bytes) -> CareerSetupPagePacket:
        source_sha = hashlib.sha256(png_bytes).hexdigest()
        frame = _decode_and_normalize(png_bytes)
        if frame is None:
            return self._invalid_packet(png_bytes, "start_confirm")
        perception = Perception().analyze_image(frame)
        button = SafeAdvancePlanner._support_deck_start_confirm_button(perception, frame)
        try:
            encoded_ok, canonical_png = cv2.imencode(".png", frame)
            if not encoded_ok:
                raise RuntimeError("start_confirm_encode_failed")
            tokens = self._read_tokens(bytes(canonical_png))
        except (OSError, RuntimeError, TypeError, AttributeError, ValueError):
            tokens = ()
        visible_text = "".join(token.text for token in tokens)
        screen_tokens = self._read_screen_tokens(frame)
        screen_text = "".join(text for text, _bbox in screen_tokens)
        forbidden = any(
            term in visible_text or term in screen_text
            for term in _FORBIDDEN_START_TERMS
        )
        cost_values: list[int] = []
        balance_values: list[int] = []
        for token in tokens:
            if token.bbox is None or re.fullmatch(r"\d{1,3}", token.text) is None:
                continue
            x, y, _width, _height = token.bbox
            value = int(token.text)
            if 150 <= x <= 300 and 1030 <= y <= 1085:
                cost_values.append(value)
            elif 300 <= x <= 520 and 1080 <= y <= 1135:
                balance_values.append(value)
        tp_equation_valid = bool(
            len(cost_values) == 1
            and len(balance_values) == 2
            and balance_values[0] >= cost_values[0]
            and balance_values[1] == balance_values[0] - cost_values[0]
        )
        training_points_equation_valid = self._training_points_equation(screen_tokens)
        equation_valid = tp_equation_valid or training_points_equation_valid
        resource_policy = (
            "ordinary_tp_only"
            if tp_equation_valid
            else "ordinary_training_points_only"
            if training_points_equation_valid
            else None
        )
        errors: list[str] = []
        if button is None:
            errors.append("start_confirm_geometry_mismatch")
        if forbidden:
            errors.append("forbidden_resource_term")
        if not equation_valid:
            errors.append("ordinary_start_resource_equation_missing_or_invalid")
        accepted = button is not None and not forbidden and equation_valid
        page_state = "start_confirm_ready" if accepted else (
            "resource_forbidden" if forbidden else "resource_ambiguous"
        )
        target_bbox = None
        if button is not None:
            target_bbox = (button[0] - 150, button[1] - 48, button[0] + 150, button[1] + 48)
        return CareerSetupPagePacket(
            page_state=page_state,
            source_sha256=source_sha,
            page_family="start_confirm",
            action_id="safe_advance.support_deck_start_confirm" if accepted else None,
            canonical_target_bbox=target_bbox if accepted else None,
            matcher_versions=((
                "resource",
                "localized_tp_arithmetic_v1"
                if tp_equation_valid
                else "rapidocr_training_points_arithmetic_v1",
            ),),
            resource_policy=resource_policy if accepted else None,
            errors=tuple(errors),
        )

    def analyze_friend_support(self, png_bytes: bytes) -> CareerSetupPagePacket:
        source_sha = hashlib.sha256(png_bytes).hexdigest()
        frame = _decode_and_normalize(png_bytes)
        if frame is None:
            return self._invalid_packet(png_bytes, "friend_support")
        result = analyze_friend_support_structure(frame, self._support_templates[-1])
        unique = result.unique_30137
        if unique is not None:
            return CareerSetupPagePacket(
                page_state="friend_support_30137_ready",
                source_sha256=source_sha,
                page_family="friend_support",
                action_id="career_setup.select_friend_30137",
                canonical_target_bbox=unique.canonical_bbox,
                identities=("30137",),
                identity_matches=(True,),
                matcher_versions=((
                    "visual",
                    "friend_support_30137_orb_homography_v1:"
                    f"{unique.inliers}:{unique.inlier_ratio:.3f}:"
                    f"{unique.projected_area_ratio:.3f}",
                ),),
                errors=(),
            )
        return CareerSetupPagePacket(
            page_state="identity_mismatch",
            source_sha256=source_sha,
            page_family="friend_support",
            identities=("unknown",) if result.page_detected else (),
            identity_matches=(False,) if result.page_detected else (),
            matcher_versions=(("visual", "friend_support_30137_orb_homography_v1"),),
            errors=result.errors,
        )

    def analyze_shorten_settings(self, png_bytes: bytes) -> CareerSetupPagePacket:
        source_sha = hashlib.sha256(png_bytes).hexdigest()
        frame = _decode_and_normalize(png_bytes)
        if frame is None:
            return self._invalid_packet(png_bytes, "shorten_settings")
        result = SafeAdvancePlanner._shortened_event_settings_action(frame)
        if result is None:
            return CareerSetupPagePacket(
                page_state="settings_ambiguous",
                source_sha256=source_sha,
                page_family="shorten_settings",
                errors=("shorten_settings_geometry_mismatch",),
            )
        state, point = result
        mapping = {
            "select_all": ("shorten_select_all", "safe_advance.shorten_all_events"),
            "uncheck_unread": (
                "shorten_uncheck_unread",
                "safe_advance.shorten_unread_toggle",
            ),
            "confirm": (
                "shorten_settings_ready",
                "safe_advance.shorten_events_confirm",
            ),
        }
        if state not in mapping:
            return CareerSetupPagePacket(
                page_state="settings_ambiguous",
                source_sha256=source_sha,
                page_family="shorten_settings",
                errors=("shorten_unread_checkbox_unknown",),
            )
        page_state, action_id = mapping[state]
        return CareerSetupPagePacket(
            page_state=page_state,
            source_sha256=source_sha,
            page_family="shorten_settings",
            action_id=action_id,
            canonical_target_bbox=(point[0] - 40, point[1] - 40, point[0] + 40, point[1] + 40),
            settings=(("keep_unread", False), ("mode", "all")),
            matcher_versions=(("settings", "safe_advance_shorten_state_v1"),),
        )

    def analyze_career_opening(self, png_bytes: bytes) -> CareerSetupPagePacket:
        source_sha = hashlib.sha256(png_bytes).hexdigest()
        frame = _decode_and_normalize(png_bytes)
        if frame is None:
            return self._invalid_packet(png_bytes, "career_opening")
        visible_text = "".join(
            text for text, _bbox in self._read_screen_tokens(frame)
        )
        opening = bool(
            (
                SafeAdvancePlanner._looks_like_training_top_hud(frame)
                and SafeAdvancePlanner._looks_like_large_bottom_dialogue(frame)
                and SafeAdvancePlanner._looks_like_dialogue_continue_arrow(frame)
                and SafeAdvancePlanner._looks_like_bottom_skip_off_pill(frame)
            )
            or SafeAdvancePlanner._current_vr_opening_fast_forward_button(
                frame,
                visible_text,
            )
            is not None
            or SafeAdvancePlanner._career_opening_movie_skip_button(frame)
            is not None
        )
        return CareerSetupPagePacket(
            page_state="career_opening" if opening else "unknown_page",
            source_sha256=source_sha,
            page_family="career_opening",
            matcher_versions=(("visual", "career_opening_handoff_v3"),),
            errors=() if opening else ("career_opening_geometry_mismatch",),
        )

    def analyze_expected(self, png_bytes: bytes, status: str) -> CareerSetupPagePacket:
        if status in {"scenario_verified", "trainee_candidate_selected"}:
            return self.analyze_trainee(png_bytes)
        if status == "trainee_verified":
            return self.analyze_inheritance(png_bytes, allow_filled_auto_select=True)
        if status == "inheritance_auto_selected":
            return self.analyze_inheritance(png_bytes)
        if status == "inheritance_auto_confirmed":
            return self.analyze_inheritance(png_bytes, allow_auto_selected=True)
        if status == "inheritance_verified":
            return self.analyze_support(png_bytes)
        if status == "support_auto_formation_selected":
            return self.analyze_support(png_bytes)
        if status == "support_auto_formation_confirmed":
            return self.analyze_support(png_bytes, allow_auto_formed=True)
        if status == "friend_support_opened":
            friend = self.analyze_friend_support(png_bytes)
            if friend.page_state == "friend_support_30137_ready":
                return friend
            support = self.analyze_support(png_bytes)
            if support.page_state == "support_select_ready":
                return support
            return friend
        if status == "friend_support_selected":
            return self.analyze_support(png_bytes)
        if status == "support_verified":
            return self.analyze_start_confirm(png_bytes)
        if status == "start_confirm_verified":
            opening = self.analyze_career_opening(png_bytes)
            if not opening.errors:
                return opening
            # This page appears only after the opening has finished automatically.
            settings = self.analyze_shorten_settings(png_bytes)
            if settings.page_state == "shorten_settings_ready" and not settings.errors:
                return settings
            return opening
        if status in {
            "career_opening_observed",
            "shorten_mode_verified",
            "shorten_unread_verified",
        }:
            return self.analyze_shorten_settings(png_bytes)
        raise ValueError(f"unsupported_career_setup_status:{status}")


__all__ = ["CareerSetupPerceptor"]
