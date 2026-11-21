"""训练页结构化提取模块

从训练页截图中提取结构化数据，采用 mimo 视觉模型 + OpenCV 混合方案：
- mimo: 数值增益、失败率、训练类型
- OpenCV: 支援卡数量、碎片颜色、体力条

⚠ LEGACY/OFFLINE-ONLY：Mimo 路径仅用于离线评测与调试，严禁接入 live 感知链路；
live 识别必须走本地 OCR/模板/几何，外部多模态不得参与按钮/界面识别。

列裁剪方案（column-based）：
- 按固定 x 坐标裁剪 6 个训练增益栏位
- 每栏独立发送 MIMO 识别 +N 数字
- 避免整图列错位问题
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from core.support_slot_recognizer import SupportSlotRecognizer
from core.grand_masters_fragment_recognizer import (
    recognize_grand_masters_training_fragment,
)
from core.main_menu_state import _analyze_fragments
from core.training_gain_reliability import (
    classify_reliable_exact_gain,
    validate_dynamic_gain_column,
)
from core.auto_trainer.numeric_ocr_shadow import PpOcrV5NumericReader
from core.failure_rate_glyph_kernel import (
    classify_failure_digit_glyph,
    extract_glow_resistant_foreground,
    find_failure_percent_start_glyph,
    split_touching_failure_digits,
)

logger = logging.getLogger(__name__)
_FAILURE_CTX = threading.local()


# ---------- 数据结构 ----------

@dataclass
class StatsGain:
    speed: int | None = None
    stamina: int | None = None
    power: int | None = None
    guts: int | None = None
    wisdom: int | None = None
    skill_pt: int | None = None


def _stats_gain_from_mapping(values: dict[str, Any] | None) -> StatsGain | None:
    if not isinstance(values, dict):
        return None
    gain = StatsGain(
        speed=_safe_gain_int(values.get("speed")),
        stamina=_safe_gain_int(values.get("stamina")),
        power=_safe_gain_int(values.get("power")),
        guts=_safe_gain_int(values.get("guts")),
        wisdom=_safe_gain_int(values.get("wisdom")),
        skill_pt=_safe_gain_int(values.get("skill_pt")),
    )
    if all(getattr(gain, key) is None for key in COLUMN_REGIONS):
        return None
    return gain


def _safe_gain_int(value: Any) -> int | None:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


def _dynamic_candidate_rows(candidate: Any) -> list[dict[str, Any]]:
    raw_rows = (
        candidate.get("rows")
        if isinstance(candidate, dict)
        else getattr(candidate, "rows", ())
    )
    if not isinstance(raw_rows, (list, tuple)):
        return []
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if isinstance(raw, dict):
            rows.append(dict(raw))
            continue
        rows.append(
            {
                "row_index": getattr(raw, "row_index", None),
                "occupancy": getattr(raw, "occupancy", None),
                "status": getattr(raw, "status", None),
                "raw_text": getattr(raw, "raw_text", ""),
                "value": getattr(raw, "value", None),
                "reason": getattr(raw, "reason", ""),
            }
        )
    return rows


def _accepted_row_values(column: dict[str, Any]) -> list[dict[str, Any]]:
    rows = column.get("rows")
    if not isinstance(rows, list):
        return []
    accepted: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "accepted":
            continue
        value = 0 if type(row.get('value')) is int and row['value'] == 0 else _safe_gain_int(row.get("value"))
        if value is None:
            continue
        accepted.append(
            {
                "kind": str(row.get("kind") or ""),
                "value": value,
                "reason": str(row.get("reason") or ""),
            }
        )
    return accepted


def _column_canonical_is_decision_safe(
    attr: str,
    column: dict[str, Any],
) -> int | None:
    strict = classify_reliable_exact_gain(column, attribute_id=attr)
    canonical = strict.get("value") if strict.get("status") == "reliable_exact" else None
    return canonical if type(canonical) is int else None


def _is_vetted_power_five_plus_three_recovery(
    attr: str,
    canonical: int,
    accepted_rows: list[dict[str, Any]],
) -> bool:
    if attr != "power" or canonical != 8:
        return False
    if len(accepted_rows) != 2:
        return False
    base = next((row for row in accepted_rows if row["kind"] == "base"), None)
    bonus = next((row for row in accepted_rows if row["kind"] == "bonus"), None)
    return (
        base is not None
        and bonus is not None
        and base["value"] == 5
        and base["reason"] == "low_confidence_top_row_five"
        and bonus["value"] == 3
        and bonus["reason"] == "ambiguous_zero_to_three_recovered"
    )


def _conservative_stats_gain_from_ocr(
    stats_gain_ocr: dict[str, Any] | None,
) -> dict[str, int]:
    if not isinstance(stats_gain_ocr, dict):
        return {}
    columns = stats_gain_ocr.get("columns")
    if not isinstance(columns, list):
        return {}

    gains: dict[str, int] = {}
    for column in columns:
        if not isinstance(column, dict):
            continue
        attr = str(column.get("attribute_id") or "")
        if attr not in {"speed", "stamina", "power", "guts", "wisdom", "skill_pt"}:
            continue
        value = _column_canonical_is_decision_safe(attr, column)
        if value is not None:
            gains[attr] = value
    return gains


@dataclass
class TrainingData:
    screen: str = "training_menu"
    training_type: str = "unknown"
    stats_gain: StatsGain = field(default_factory=StatsGain)
    base_gain: StatsGain | None = None
    bonus_gain: StatsGain | None = None
    stats_gain_total: StatsGain | None = None
    failure_rate: int | None = None
    failure_rate_ocr: dict | None = None
    support_count: int | None = None
    visible_portrait_count: int | None = None
    effective_support_count: int | None = None
    non_support_npc_count: int | None = None
    unresolved_portrait_count: int | None = None
    hint_count: int | None = None
    friendship: bool | None = None
    fragment_color: str | None = None
    fragment_stat: str | None = None
    fragment_available: bool | None = None
    fragment_state: str | None = None
    fragment_count: int | None = None
    fragments: list[dict[str, Any]] = field(default_factory=list)
    scenario_mechanics: dict[str, Any] = field(default_factory=dict)
    vitality_bar_percent: int | None = None
    confidence: float = 0.0
    unknown_fields: list[str] = field(default_factory=list)
    field_reliability: dict[str, Any] = field(default_factory=dict)
    raw_mimo: dict | None = None
    stats_gain_ocr: dict | None = None
    support_slots: list[dict[str, Any]] = field(default_factory=list)
    support_slots_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stats_gain"] = asdict(self.stats_gain)
        d["base_gain"] = asdict(self.base_gain) if self.base_gain is not None else None
        d["bonus_gain"] = asdict(self.bonus_gain) if self.bonus_gain is not None else None
        d["stats_gain_total"] = asdict(self.stats_gain_total) if self.stats_gain_total is not None else None
        return d

    def to_json(self, pretty: bool = False) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2 if pretty else None)


# ---------- 训练类型映射 ----------

TYPE_MAP = {
    # 中文
    "速度": "speed",
    "耐力": "stamina",
    "力量": "power",
    "毅力": "guts",
    "根性": "guts",
    "智力": "wisdom",
    # 英文
    "speed": "speed",
    "stamina": "stamina",
    "power": "power",
    "guts": "guts",
    "wisdom": "wisdom",
}

# ---------- 文件名推断训练类型 ----------

_FILENAME_TYPE_RE = re.compile(
    r"(?:^|[_-])(?:training[_-])?(speed|stamina|power|guts|wisdom)(?:$|[_-])",
    re.IGNORECASE,
)

_FAILURE_RATE_ROIS: dict[str, tuple[float, float, float, float]] = {
    "speed": (0.00, 0.69, 0.34, 0.82),
    "stamina": (0.20, 0.69, 0.45, 0.82),
    "power": (0.36, 0.69, 0.65, 0.82),
    "guts": (0.52, 0.69, 0.84, 0.82),
    "wisdom": (0.70, 0.69, 1.00, 0.82),
}

# L'Arc's first-turn card is narrower than the legacy five-tab card.  Keep
# this explicit to the L'Arc path; the legacy ROI geometry remains unchanged.
_LARC_FAILURE_RATE_ROIS: dict[str, tuple[float, float, float, float]] = {
    "speed": (0.00, 0.69, 0.23, 0.82),
    "stamina": (0.14, 0.69, 0.39, 0.82),
    "power": (0.31, 0.69, 0.56, 0.82),
    "guts": (0.47, 0.69, 0.72, 0.82),
    "wisdom": (0.63, 0.69, 0.88, 0.82),
}


@dataclass(frozen=True)
class _FailureRateRead:
    value: int | None = None
    digit_count: int = 0
    incomplete: bool = False


_GM_SELECTED_FRAGMENT_ROIS: dict[str, tuple[float, float, float, float]] = {
    "speed": (0.0486, 0.7344, 0.1319, 0.7891),
    "stamina": (0.2222, 0.7344, 0.3056, 0.7891),
    "power": (0.4583, 0.7422, 0.5486, 0.7930),
    "guts": (0.5833, 0.7188, 0.7500, 0.7930),
    "wisdom": (0.8125, 0.7422, 0.9028, 0.7891),
}

_GM_SELECTED_FRAGMENT_CARD_ROIS: dict[str, tuple[float, float, float, float]] = {
    "speed": (0.0600, 0.7800, 0.1600, 0.8400),
    "stamina": (0.3050, 0.7900, 0.3750, 0.8300),
    "power": (0.3800, 0.7500, 0.6200, 0.8600),
    "guts": (0.7600, 0.7400, 1.0000, 0.8400),
    "wisdom": (0.7200, 0.8200, 0.8300, 0.9000),
}


def infer_training_type_from_filename(image_path: str | Path) -> str | None:
    """从文件名推断训练类型，如 training_speed.png -> 'speed'。"""
    m = _FILENAME_TYPE_RE.search(Path(image_path).stem)
    return m.group(1).lower() if m else None


# ---------- 列裁剪区域定义 ----------

# 1440x2560 竖屏训练页，训练增益栏位的固定坐标范围
# 增益数字（橙色 +N）分布在 y=1490-1660 区域
# 技能点可能有两行（+4 和 +7 分开显示）
_GAIN_Y_TOP = 1490
_GAIN_Y_BOTTOM = 1660

COLUMN_REGIONS: dict[str, tuple[int, int]] = {
    "speed":   (30, 260),
    "stamina": (260, 490),
    "power":   (490, 720),
    "guts":    (720, 950),
    "wisdom":  (950, 1150),
    "skill_pt": (1150, 1410),
}


# ---------- 增益合理性校验 ----------

# 单次训练的属性增益不太可能超过这些值
_MAX_STAT_GAIN = 40    # sp/st/pw/gt/ws 的合理上限
_MAX_LAYERED_STAT_GAIN = 199
_MAX_SKILL_PT_GAIN = 60  # Legacy fallback only; verified gain rows are not capped here.


# ---------- 主解析器 ----------

GRAND_MASTERS_LEGACY_FRAGMENT_GATE_V1 = "legacy_v1_reviewed_crop"


class TrainingParser:
    """解析训练页截图，返回 TrainingData。"""

    _CACHE_VERSION = "training_parser_mimo_v2"

    def __init__(
        self,
        use_mimo: bool = False,
        cache_dir: str | Path | None = "logs/cache/training_parser",
        cache_enabled: bool = True,
        recognition_mode: str = "template",  # "template" | "dynamic" | "lightweight" | "mimo" | "auto" | "local"
        grand_masters_legacy_fragment_gate: str | None = None,
        numeric_reader: Any | None = None,
        larc_roster_path: str | Path | None = None,
    ):
        if grand_masters_legacy_fragment_gate not in {
            None,
            GRAND_MASTERS_LEGACY_FRAGMENT_GATE_V1,
        }:
            raise ValueError(
                "unsupported grand_masters_legacy_fragment_gate: "
                f"{grand_masters_legacy_fragment_gate}"
            )
        self._use_mimo = use_mimo
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._cache_enabled = cache_enabled and self._cache_dir is not None
        self._recognition_mode = recognition_mode
        self._grand_masters_legacy_fragment_gate = (
            grand_masters_legacy_fragment_gate
        )
        self._numeric_reader = numeric_reader
        self._numeric_reader_init_attempted = numeric_reader is not None
        self._lightweight_reader = None
        self._lightweight_reader_init_attempted = False
        self._support_slot_recognizer = SupportSlotRecognizer()
        self._larc_support_reader = None
        self._larc_roster = None
        self._larc_roster_path = Path(larc_roster_path) if larc_roster_path is not None else None
        self._larc_capture_source = None

    def bind_larc_capture(self, source: dict[str, Any]) -> None:
        """Called with the capture manifest's run, turn and raw-frame binding."""
        self._larc_capture_source = None
        run_id = source.get("run_session_id")
        if not isinstance(run_id, str) or not run_id:
            return  # Legacy/unbound parses still report anonymous observations.
        self.get_larc_roster(run_id)
        self._larc_capture_source = dict(source)

    @property
    def larc_roster_path(self):
        return self._larc_roster_path

    def get_larc_roster(self, run_session_id: str):
        from core.larc_member_roster import LarcMemberRoster

        if self._larc_roster is None or self._larc_roster.run_session_id != run_session_id:
            if self._larc_roster_path is not None and self._larc_roster_path.exists():
                payload = json.loads(self._larc_roster_path.read_text(encoding="utf-8"))
                self._larc_roster = LarcMemberRoster.from_dict(payload, run_session_id=run_session_id)
            else:
                self._larc_roster = LarcMemberRoster(run_session_id)
        return self._larc_roster

    def refresh_larc_roster(self, run_session_id: str) -> bool:
        """Observe an explicit same-run portrait relocation by image cleanup."""
        if (self._larc_roster is None or self._larc_roster.run_session_id != run_session_id
                or self._larc_roster_path is None or not self._larc_roster_path.is_file()):
            return False
        self._larc_roster = None
        self.get_larc_roster(run_session_id)
        return True

    def record_larc_ss_execution(self, receipt: dict[str, Any]) -> bool:
        roster = self.get_larc_roster(receipt["run_session_id"])
        changed = roster.record_ss_execution(receipt)
        if changed and self._larc_roster_path is not None:
            roster.save(self._larc_roster_path)
        return changed

    def parse(self, image_path: str | Path) -> TrainingData:
        """Legacy Grand Masters entry point; formal callers pass a scenario."""
        return self.parse_for_scenario(image_path, scenario="grand_masters")

    def parse_for_scenario(self, image_path: str | Path, *, scenario: str | None) -> TrainingData:
        path = Path(image_path)
        img = cv2.imread(str(path))
        if img is None:
            raise ValueError(f"无法读取图片: {path}")

        result = TrainingData()
        filename_type = infer_training_type_from_filename(image_path)
        if filename_type:
            result.training_type = filename_type

        canonical_720 = (
            cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
            if img.shape[:2] == (2560, 1440)
            else None
        )

        # --- OpenCV 部分（本地，快速，可靠） ---
        support_slots_ok = scenario == "larc" or self._recognize_support_slots(path, img, result)
        if not support_slots_ok:
            self._detect_support_count(img, result)
            # 友情检测由 mimo 兜底，OpenCV 无法可靠区分
            result.friendship = None
        if scenario == "grand_masters":
            self._detect_fragment_color(img, result)
        self._detect_failure_rate(img, result, scenario=scenario, _canonical_720=canonical_720)
        self._detect_vitality(img, result)
        if not support_slots_ok:
            self._detect_hint_count_conservative(img, result)
        if scenario == "larc":
            from core.larc_support_reader import LarcSupportReader

            if self._larc_support_reader is None:
                self._larc_support_reader = LarcSupportReader()
            participants = self._larc_support_reader.read(img)
            source = self._larc_capture_source
            if source is not None and Path(source["absolute_path"]).resolve() == path.resolve():
                participants = self._larc_roster.observe_training(img, participants, source=source)
                if self._larc_roster_path is not None and participants["status"] == "accepted":
                    self._larc_roster.save(self._larc_roster_path)
            result.scenario_mechanics["larc"] = {"training_participants": participants}
            result.support_slots_source = "larc_fixed_training_lane"
            if participants["status"] == "accepted":
                result.support_count = result.visible_portrait_count = participants["visible_portrait_count"]
                result.hint_count = participants["summary"]["training_hint_count"]
                # Below-orange bond bars cannot trigger support friendship.
                # Orange/MAX alone does not prove an active rainbow training.
                if all(p["bond"]["status"] in {"accepted", "not_visible"}
                       and p["bond"]["color"] in {None, "empty", "cyan", "green"}
                       for p in participants["participants"]):
                    result.friendship = False
                result.support_slots = [dict(
                    person, hint_badge=person["training_hint"]["present"],
                    bond_bar=person["bond"],
                ) for person in participants["participants"]]
                known = [p for p in participants["participants"] if p["identity"].get("status") == "known"]
                result.unresolved_portrait_count = result.support_count - len(known)
                # Unknown identities are not evidence of zero support cards.
                if result.unresolved_portrait_count == 0:
                    result.effective_support_count = sum(bool(p["identity"].get("matched_card_id")) for p in known)
                    result.non_support_npc_count = len(known) - result.effective_support_count
            if result.hint_count is None:
                result.unknown_fields.append("hint_detection_unreliable")

        # --- 增益数值提取 ---
        mode = self._recognition_mode

        gain_img = canonical_720 if canonical_720 is not None else img
        if mode == "template":
            # 本地模板 OCR（默认，不依赖 MIMO）
            self._extract_via_local_template(str(path), result, image=img)
        elif mode == "local":
            # 本地 OCR（本地模板安全回退）
            self._extract_via_local_ocr(str(path), result, image=img)
        elif mode == "lightweight":
            # Lightweight mode always uses self-owned V4 template kernel without
            # falling back to PP-OCRv5 or legacy 1440p template re-reads.
            self._extract_via_dynamic_ocr(gain_img, result, lightweight=True, scenario=scenario)
        elif mode == "dynamic":
            dynamic_ok = self._extract_via_dynamic_ocr(gain_img, result)
            if not dynamic_ok:
                self._extract_via_local_template(str(path), result, image=img)
        elif mode == "mimo":
            # 显式 MIMO
            if self._use_mimo:
                self._extract_via_mimo(str(path), result)
        elif mode == "auto":
            # 先模板，不足则 MIMO fallback
            template_ok = self._extract_via_local_template(str(path), result, image=img)
            if not template_ok and self._use_mimo:
                logger.info("模板识别不足，回退 MIMO")
                self._extract_via_mimo(str(path), result)

        # --- 训练类型推断（如果还没推断出来） ---
        if result.training_type == "unknown" and filename_type:
            result.training_type = filename_type

        result.confidence = self._calc_confidence(result)
        # 合并 _validate_gains 写入的 suspicious 标记与 _collect_unknowns 的结果
        collected = self._collect_unknowns(result)
        if scenario != "grand_masters":
            collected = [field for field in collected if field != "fragment_color"]
        merged = list(dict.fromkeys(result.unknown_fields + collected))  # 去重保序
        result.unknown_fields = merged
        self._populate_field_reliability(result)
        if scenario != "grand_masters":
            result.field_reliability.pop("fragment", None)

        return result

    # ========== OpenCV 检测 ==========

    def _recognize_support_slots(
        self,
        image_path: Path,
        img: np.ndarray,
        result: TrainingData,
    ) -> bool:
        try:
            recognized = self._support_slot_recognizer.recognize_array(img, image_path=image_path)
        except Exception as exc:
            logger.debug("support slot recognition failed for %s: %s", image_path, exc)
            return False

        slots = recognized.get("support_slots")
        if not isinstance(slots, list):
            return False

        result.support_slots = slots
        result.support_slots_source = str(recognized.get("source") or "cv_heuristic")
        result.support_count = int(recognized.get("support_count", len(slots)))
        for field in (
            "visible_portrait_count",
            "effective_support_count",
            "non_support_npc_count",
            "unresolved_portrait_count",
        ):
            value = recognized.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6:
                setattr(result, field, value)
        result.hint_count = int(recognized.get("hint_count", sum(1 for slot in slots if slot.get("hint_badge"))))
        result.friendship = bool(recognized.get("friendship", False))
        if "hint_detection_unreliable" not in result.unknown_fields:
            result.unknown_fields.append("hint_detection_unreliable")
        return True

    def _detect_support_count(self, img: np.ndarray, result: TrainingData) -> None:
        """通过圆形检测估算支援卡头像数量。"""
        h, w = img.shape[:2]
        y0, y1 = int(h * 0.28), int(h * 0.52)
        roi = img[y0:y1, :, :]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        circles = cv2.HoughCircles(
            gray, cv2.HOUGH_GRADIENT,
            dp=1, minDist=35,
            param1=60, param2=35,
            minRadius=14, maxRadius=45,
        )

        if circles is None:
            result.support_count = 0
            return

        valid = []
        for c in circles[0]:
            cx, cy, r = c
            if cx < w * 0.05 or cx > w * 0.95:
                continue
            if r < 14:
                continue
            valid.append(c)

        result.support_count = len(valid)

    def _detect_fragment_color(self, img: np.ndarray, result: TrainingData) -> None:
        """检测碎片颜色。"""
        inventory_state = self._detect_grand_masters_inventory(img)
        reward = recognize_grand_masters_training_fragment(img)
        grand_masters: dict[str, Any] = {
            "mechanic": "fragments",
            "inventory_state": inventory_state,
            # These scenario-specific fields keep the global table separate
            # from the selected training card's prospective reward.
            "fragment_inventory": {
                "filled_slots": inventory_state["filled_slots"],
                "capacity": inventory_state["capacity"],
                "free_slots": inventory_state["free_slots"],
                "slots": [],
                "status": inventory_state["state"],
                "evidence": inventory_state["evidence"],
            },
            "wisdom_effect_active": None,
            "consumed_effect": {"status": "unknown"},
        }
        effect_active = self._looks_like_grand_masters_effect_active(img)
        if effect_active:
            grand_masters["wisdom_effect_active"] = True
            grand_masters["consumed_effect"] = {"status": "active"}
        color = reward["color"]["value"]
        kind = reward["kind"]["value"]
        count = reward["count"]["value"]
        reward_abstained = color is None or kind is None or count is None
        if (
            reward_abstained
            and self._grand_masters_legacy_fragment_gate
            == GRAND_MASTERS_LEGACY_FRAGMENT_GATE_V1
            and self._detect_grand_masters_selected_fragment(img, result)
        ):
            legacy = result.scenario_mechanics["grand_masters"]
            fragment = result.fragments[0]
            legacy["inventory_state"] = {
                "state": "unknown",
                "filled_slots": None,
                "capacity": 8,
                "free_slots": None,
                "confidence": 0.0,
                "evidence": {
                    "reason": "legacy_fixture_inventory_not_authoritative",
                    "observed_inventory_state": inventory_state,
                },
            }
            legacy["training_reward"] = fragment
            legacy["production"] = "selected_training_reward_legacy_crop"
            legacy["legacy_gate"] = GRAND_MASTERS_LEGACY_FRAGMENT_GATE_V1
            return
        if inventory_state["state"] == "full":
            state = "full"
            result.fragment_color = None
            result.fragment_stat = None
            result.fragment_available = False
            result.fragment_state = state
            result.fragment_count = inventory_state["filled_slots"]
            result.fragments = []
            grand_masters.update(
                {
                    "state": state,
                    "fragments": [],
                    "production": "no_new_fragment",
                    "wisdom_effect_active": False,
                    "consumed_effect": {"status": "inactive"},
                    "prospective_fragment_output": {
                        "status": "abstain",
                        "reason": "inventory_full_no_observed_new_output",
                    },
                }
            )
            result.scenario_mechanics["grand_masters"] = grand_masters
            return

        if (
            isinstance(count, int)
            and (color is None or kind is None)
            and (color is not None or kind is not None or not effect_active)
        ):
            accepted_confidences = [reward["count"]["confidence"]]
            if color is not None:
                accepted_confidences.append(reward["color"]["confidence"])
            if kind is not None:
                accepted_confidences.append(reward["kind"]["confidence"])
            confidence = min(accepted_confidences)
            fragment_color = color if color is not None else "unknown"
            fragment_kind = kind if kind is not None else "unknown"
            fragment = {
                "color": fragment_color,
                "stat": fragment_kind,
                "kind": fragment_kind,
                "count": count,
                "confidence": round(float(confidence), 3),
                "source_crop": {
                    "source": "grand_masters_selected_training_fragment",
                    "roi": reward["evidence"]["badge_roi"],
                    "coordinate_space": reward["evidence"]["coordinate_space"],
                },
                "evidence": reward,
            }
            result.fragment_color = color
            result.fragment_stat = None
            result.fragment_available = True
            result.fragment_state = "available"
            result.fragment_count = count
            result.fragments = [fragment]
            grand_masters.update(
                {
                    "state": "available",
                    "fragments": [fragment],
                    "production": "selected_training_reward_partial",
                    "prospective_fragment_output": {
                        "status": "partial",
                        "color": fragment_color,
                        "kind": fragment_kind,
                        "count": count,
                        "evidence": fragment["source_crop"],
                    },
                }
            )
            result.scenario_mechanics["grand_masters"] = grand_masters
            return

        if reward_abstained:
            if (
                inventory_state["state"] == "unknown"
                and effect_active
            ):
                result.fragment_color = None
                result.fragment_stat = None
                result.fragment_available = False
                result.fragment_state = "effect_active"
                result.fragment_count = None
                result.fragments = []
                grand_masters.update(
                    {
                        "state": "effect_active",
                        "fragments": [],
                        "production": "effect_active_only",
                        "wisdom_effect_active": True,
                        "consumed_effect": {"status": "active"},
                        "prospective_fragment_output": {
                            "status": "abstain",
                            "reason": "effect_active_no_observed_new_output",
                        },
                    }
                )
                result.scenario_mechanics["grand_masters"] = grand_masters
                return
            result.fragment_color = None
            result.fragment_stat = None
            result.fragment_available = False
            result.fragment_state = "unknown"
            result.fragment_count = None
            result.fragments = []
            grand_masters.update(
                {
                    "state": "unknown",
                    "fragments": [],
                    "production": "reward_abstained",
                    "reward_evidence": reward["evidence"],
                    "prospective_fragment_output": {
                        "status": "abstain",
                        "reason": "selected_training_reward_abstained",
                    },
                }
            )
            result.scenario_mechanics["grand_masters"] = grand_masters
            return

        confidence = min(
            reward["color"]["confidence"],
            reward["kind"]["confidence"],
            reward["count"]["confidence"],
        )
        fragment = {
            "color": color,
            "stat": kind,
            "kind": kind,
            "count": count,
            "confidence": round(float(confidence), 3),
            "source_crop": {
                "source": "grand_masters_selected_training_fragment",
                "roi": reward["evidence"]["badge_roi"],
                "coordinate_space": reward["evidence"]["coordinate_space"],
            },
            "evidence": reward,
        }
        result.fragment_color = color
        result.fragment_stat = kind
        result.fragment_available = True
        result.fragment_state = "available"
        result.fragment_count = count
        result.fragments = [fragment]
        grand_masters.update(
            {
                "state": "available",
                "fragments": [fragment],
                "training_reward": fragment,
                "production": "selected_training_reward",
                "prospective_fragment_output": {
                    "status": "accepted",
                    "color": color,
                    "kind": kind,
                    "count": count,
                    "evidence": fragment["source_crop"],
                },
            }
        )
        result.scenario_mechanics["grand_masters"] = grand_masters
        return

    @staticmethod
    def _detect_grand_masters_inventory(img: np.ndarray) -> dict[str, Any]:
        """Read the left knowledge table without treating it as a reward."""
        height, width = img.shape[:2]
        source = img
        if width * 16 == height * 9 and (width, height) != (720, 1280):
            source = cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        recognized = _analyze_fragments(source)
        capacity = 8
        if recognized.get("abstained") or not isinstance(recognized.get("value"), int):
            return {
                "state": "unknown",
                "filled_slots": None,
                "capacity": capacity,
                "free_slots": None,
                "confidence": 0.0,
                "evidence": recognized.get("evidence", {}),
            }
        filled_slots = int(recognized["value"])
        return {
            "state": "full" if filled_slots >= capacity else "available",
            "filled_slots": filled_slots,
            "capacity": capacity,
            "free_slots": max(0, capacity - filled_slots),
            "confidence": recognized.get("confidence", 0.0),
            "evidence": recognized.get("evidence", {}),
        }

    def _detect_grand_masters_selected_fragment(
        self, img: np.ndarray, result: TrainingData
    ) -> bool:
        training_type = result.training_type
        if training_type not in _GM_SELECTED_FRAGMENT_ROIS:
            return False

        h, w = img.shape[:2]
        roi_table = (
            _GM_SELECTED_FRAGMENT_CARD_ROIS
            if w < 600
            else _GM_SELECTED_FRAGMENT_ROIS
        )
        rx0, ry0, rx1, ry1 = roi_table[training_type]
        x0, y0 = int(w * rx0), int(h * ry0)
        x1, y1 = int(w * rx1), int(h * ry1)
        if x1 <= x0 or y1 <= y0:
            return False
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            return False

        color, confidence = self._classify_grand_masters_fragment_color(crop)
        if color is None or confidence < 0.20:
            return False
        stat = self._classify_grand_masters_fragment_stat(
            crop,
            training_type=training_type,
            color=color,
        )
        if stat is None:
            return False

        count = (
            2
            if self._looks_like_double_grand_masters_fragment(
                crop,
                training_type=training_type,
                color=color,
            )
            else 1
        )
        fragment = {
            "color": color,
            "stat": stat,
            "kind": stat,
            "count": count,
            "confidence": round(float(confidence), 3),
            "source_crop": {
                "source": "grand_masters_selected_training_fragment",
                "roi": [x0, y0, x1, y1],
                "coordinate_space": "image_pixels",
            },
        }
        result.fragment_color = color
        result.fragment_stat = stat
        result.fragment_available = True
        result.fragment_state = "available"
        result.fragment_count = count
        result.fragments = [fragment]
        result.scenario_mechanics["grand_masters"] = {
            "mechanic": "fragments",
            "state": "available",
            "fragments": [fragment],
        }
        return True

    @staticmethod
    def _classify_grand_masters_fragment_color(
        crop: np.ndarray,
    ) -> tuple[str | None, float]:
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        hue = hsv[:, :, 0]
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]
        masks = {
            "yellow": (hue >= 18) & (hue <= 42) & (sat > 80) & (val > 110),
            "red": (((hue <= 12) | (hue >= 170)) & (sat > 80) & (val > 110)),
            "blue": (hue >= 88) & (hue <= 132) & (sat > 70) & (val > 100),
        }
        counts = {name: int(np.sum(mask)) for name, mask in masks.items()}
        best = max(counts, key=counts.get)
        best_count = counts[best]
        total = sum(counts.values())
        if best_count < 40 or total <= 0:
            return None, 0.0
        return best, best_count / total

    @staticmethod
    def _classify_grand_masters_fragment_stat(
        crop: np.ndarray,
        *,
        training_type: str,
        color: str,
    ) -> str | None:
        if color == "blue":
            return "skill_pt"
        if color == "yellow" and training_type == "speed":
            return "speed"
        if color == "yellow" and training_type == "stamina":
            return "wisdom"
        if color == "red" and training_type == "power":
            return "stamina"
        if color == "red" and training_type == "wisdom":
            return "speed"

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        hue = hsv[:, :, 0]
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]
        green = int(np.sum((hue >= 40) & (hue <= 85) & (sat > 60) & (val > 90)))
        red = int(np.sum(((hue <= 12) | (hue >= 170)) & (sat > 70) & (val > 100)))
        purple = int(np.sum((hue >= 125) & (hue <= 165) & (sat > 40) & (val > 80)))
        if green > max(red, purple, 25):
            return "wisdom"
        if red > max(green, purple, 25):
            return "stamina"
        if purple > max(green, red, 25):
            return "speed"
        return None

    @staticmethod
    def _looks_like_double_grand_masters_fragment(
        crop: np.ndarray,
        *,
        training_type: str,
        color: str,
    ) -> bool:
        if color == "blue" and training_type == "guts":
            return True
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        hue = hsv[:, :, 0]
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]
        bright_text = int(np.sum((sat < 80) & (val > 190)))
        orange_shadow = int(np.sum((hue >= 8) & (hue <= 28) & (sat > 80) & (val > 120)))
        return bool(color == "blue" and bright_text > 120 and orange_shadow > 20)

    @staticmethod
    def _looks_like_grand_masters_effect_active(img: np.ndarray) -> bool:
        h, w = img.shape[:2]
        if h <= 0 or w <= 0:
            return False

        grid = img[int(h * 0.31) : int(h * 0.50), int(w * 0.02) : int(w * 0.20)]
        button = img[int(h * 0.49) : int(h * 0.56), int(w * 0.015) : int(w * 0.22)]
        if grid.size == 0 or button.size == 0:
            return False

        button_hsv = cv2.cvtColor(button, cv2.COLOR_BGR2HSV)
        button_orange = (
            (button_hsv[:, :, 0] >= 5)
            & (button_hsv[:, :, 0] <= 42)
            & (button_hsv[:, :, 1] > 80)
            & (button_hsv[:, :, 2] > 120)
        )
        button_white = (
            (button_hsv[:, :, 1] < 70)
            & (button_hsv[:, :, 2] > 180)
        )
        if (
            float(np.mean(button_orange)) < 0.03
            and float(np.mean(button_white)) < 0.08
        ):
            return False

        grid_hsv = cv2.cvtColor(grid, cv2.COLOR_BGR2HSV)
        grid_color = (
            (grid_hsv[:, :, 1] > 80)
            & (grid_hsv[:, :, 2] > 100)
        )
        mask = (grid_color * 255).astype(np.uint8)
        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        )
        n_labels, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        min_area = max(80, int(h * w * 0.00015))
        fragment_like_components = 0
        for label in range(1, n_labels):
            area = int(stats[label, cv2.CC_STAT_AREA])
            width = int(stats[label, cv2.CC_STAT_WIDTH])
            height = int(stats[label, cv2.CC_STAT_HEIGHT])
            if (
                area >= min_area
                and width >= max(12, int(w * 0.025))
                and height >= max(12, int(h * 0.02))
            ):
                fragment_like_components += 1
        return fragment_like_components >= 8

    def _detect_failure_rate(
        self,
        img: np.ndarray,
        result: TrainingData,
        *,
        scenario: str | None = None,
        _canonical_720: np.ndarray | None = None,
    ) -> None:
        """检测失败率（蓝色文字区域存在性）。"""
        if result.failure_rate is not None:
            result.failure_rate_ocr = {
                "status": "accepted",
                "reason": "prepopulated",
                "selected_tab": result.training_type,
                "value": result.failure_rate,
                "attempts": [],
            }
            return

        parsed_rate, saw_rate_bubble, debug = self._detect_failure_rate_bubble(
            img,
            result.training_type,
            scenario=scenario,
            _canonical_720=_canonical_720,
        )
        result.failure_rate_ocr = debug
        if parsed_rate is not None:
            result.failure_rate = parsed_rate
            return
        if saw_rate_bubble:
            return
        return

    def _detect_failure_rate_bubble(
        self,
        img: np.ndarray,
        training_type: str,
        *,
        scenario: str | None = None,
        _canonical_720: np.ndarray | None = None,
    ) -> tuple[int | None, bool, dict[str, Any]]:
        rois = _LARC_FAILURE_RATE_ROIS if scenario == "larc" else _FAILURE_RATE_ROIS
        coordinate_scale = 1
        normalization: dict[str, Any] = {}
        if img.shape[:2] == (2560, 1440):
            img = (
                _canonical_720
                if _canonical_720 is not None and _canonical_720.shape[:2] == (1280, 720)
                else cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
            )
            coordinate_scale = 2
            normalization = {"normalization": {
                "method": "INTER_AREA",
                "source_resolution": [1440, 2560],
                "reader_resolution": [720, 1280],
            }}
        tabs: list[str] = []
        if training_type in rois:
            tabs.append(training_type)
        tabs.extend(tab for tab in rois if tab not in tabs)
        saw_bubble = False
        attempts: list[dict[str, Any]] = []
        for tab in tabs:
            roi, roi_bbox = self._crop_failure_rate_roi_with_bbox(img, tab, rois=rois)
            if roi is None:
                continue
            if scenario == "larc":
                # The blue stat outline/background must not join the blue
                # failure pill. Preserve detector padding above its fixed band.
                roi = roi.copy()
                roi[:max(0, round(img.shape[0] * 930 / 1280) - roi_bbox[1])] = 0
            value, saw, debug = self._read_failure_rate_from_tab_roi(
                roi, dense_blue=scenario == "larc" and tab == "guts"
            )
            attempt = {
                "tab": tab,
                "roi": [value * coordinate_scale for value in roi_bbox],
                "status": debug.get("status"),
                "reason": debug.get("reason"),
                "saw_bubble": saw,
                "value": value,
            }
            bubble_bbox = debug.get("bubble_bbox")
            if isinstance(bubble_bbox, list) and len(bubble_bbox) == 4 and roi_bbox is not None:
                attempt["bubble_roi"] = [
                    (roi_bbox[0] + bubble_bbox[0]) * coordinate_scale,
                    (roi_bbox[1] + bubble_bbox[1]) * coordinate_scale,
                    (roi_bbox[0] + bubble_bbox[2]) * coordinate_scale,
                    (roi_bbox[1] + bubble_bbox[3]) * coordinate_scale,
                ]
            palette = debug.get("palette")
            if isinstance(palette, str) and palette:
                attempt["palette"] = palette
            readable_candidates = debug.get("readable_candidates")
            if isinstance(readable_candidates, list):
                attempt["readable_candidates"] = [
                    {**candidate, "bubble_bbox": [
                        value * coordinate_scale for value in candidate["bubble_bbox"]
                    ]}
                    for candidate in readable_candidates
                    if isinstance(candidate, dict)
                ]
            attempts.append(attempt)
            saw_bubble = saw_bubble or saw
            if debug.get("reason") == "incomplete_digits":
                return None, saw_bubble, {
                    "status": "unreadable", "reason": "incomplete_digits",
                    "selected_tab": None, "value": None, "attempts": attempts,
                    **normalization,
                }
            if value is not None:
                return value, True, {
                    "status": "accepted",
                    "reason": "",
                    "selected_tab": tab,
                    "value": value,
                    "attempts": attempts,
                    **normalization,
                }
        return None, saw_bubble, {
            "status": "unreadable" if saw_bubble else "missing",
            "reason": "digit_unreadable" if saw_bubble else "no_colored_bubble_detected",
            "selected_tab": None,
            "value": None,
            "attempts": attempts,
            **normalization,
        }

    @staticmethod
    def _crop_failure_rate_roi(img: np.ndarray, tab: str) -> np.ndarray | None:
        roi, _bbox = TrainingParser._crop_failure_rate_roi_with_bbox(img, tab)
        return roi

    @staticmethod
    def _crop_failure_rate_roi_with_bbox(
        img: np.ndarray,
        tab: str,
        *,
        rois: dict[str, tuple[float, float, float, float]] | None = None,
    ) -> tuple[np.ndarray | None, list[int] | None]:
        rel = (rois or _FAILURE_RATE_ROIS).get(tab)
        if rel is None:
            return None, None
        h, w = img.shape[:2]
        x0 = max(0, min(w, int(w * rel[0])))
        y0 = max(0, min(h, int(h * rel[1])))
        x1 = max(0, min(w, int(w * rel[2])))
        y1 = max(0, min(h, int(h * rel[3])))
        if x1 <= x0 or y1 <= y0:
            return None, None
        return img[y0:y1, x0:x1], [x0, y0, x1, y1]

    def _read_failure_rate_from_tab_roi(
        self,
        roi: np.ndarray,
        *,
        dense_blue: bool = False,
    ) -> tuple[int | None, bool, dict[str, Any]]:
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        orange_mask = (
            (hsv[:, :, 0] >= 3)
            & (hsv[:, :, 0] <= 35)
            # The pill is vivid orange; brown track pixels can share its hue
            # and otherwise merge into its right edge, moving the digit crop.
            & (hsv[:, :, 1] > 180)
            & (hsv[:, :, 2] > 120)
        )
        blue_mask = (
            (hsv[:, :, 0] >= 88)
            & (hsv[:, :, 0] <= 118)
            & (hsv[:, :, 1] > 80)
            & (hsv[:, :, 2] > 120)
        )

        candidates: list[tuple[str, np.ndarray, int, list[int]]] = []
        for palette, background_mask in (("orange", orange_mask), ("blue", blue_mask)):
            bubble, score, bbox = self._crop_failure_rate_bubble_from_mask(
                roi, background_mask,
                row_fill_ratio=0.40 if dense_blue and palette == "blue" else 0.20,
            )
            if bubble is None or score is None or bbox is None:
                continue
            candidates.append((palette, bubble, score, bbox))

        if not candidates:
            crimson_mask = (
                ((hsv[:, :, 0] <= 35) | (hsv[:, :, 0] >= 170))
                & (hsv[:, :, 1] > 165)
                & (hsv[:, :, 2] > 120)
            )
            glow_warm_mask = (
                ((hsv[:, :, 0] <= 38) | (hsv[:, :, 0] >= 145))
                & (hsv[:, :, 1] > 95)
                & (hsv[:, :, 2] > 180)
            )
            for fallback_palette, fallback_mask in (
                ("crimson", crimson_mask),
                ("orange_glow", glow_warm_mask),
            ):
                if not fallback_mask.any():
                    continue
                for ratio in (0.20, 0.40):
                    bubble, score, bbox = self._crop_failure_rate_bubble_from_mask(
                        roi, fallback_mask, row_fill_ratio=ratio
                    )
                    if bubble is not None and score is not None and bbox is not None:
                        candidates.append((fallback_palette, bubble, score, bbox))
                        break
                if candidates:
                    break

        if not candidates:
            return None, False, {"status": "missing", "reason": "no_colored_bubble_detected"}

        # A training icon can share the orange palette and dominate by area.
        # Read every structurally valid candidate before deciding: palette is
        # useful evidence, never a tie-breaker for conflicting numeric values.
        readable_candidates: list[dict[str, Any]] = []
        for palette, bubble, _score, bbox in sorted(
            candidates,
            key=lambda item: (item[0] != "blue", -item[2]),
        ):
            reading = self._read_failure_rate_digits(bubble)
            if reading.incomplete:
                return None, True, {
                    "status": "unreadable", "reason": "incomplete_digits",
                    "bubble_bbox": bbox, "palette": palette,
                }
            value = reading.value
            if value is not None:
                if bbox[0] == 0 or bbox[2] == roi.shape[1]:
                    # A pill joined to same-colour artwork can reach the ROI
                    # edge. Its width is then not a valid text-layout anchor:
                    # the left-label crop can delete a whole leading digit.
                    return None, True, {
                        "status": "unreadable", "reason": "incomplete_digits",
                        "bubble_bbox": bbox, "palette": palette,
                        "bubble_clipped": True,
                    }
                readable_candidates.append(
                    {
                        "palette": palette,
                        "value": value,
                        "bubble_bbox": bbox,
                    }
                )

        if readable_candidates:
            values = {candidate["value"] for candidate in readable_candidates}
            if len(values) == 1:
                selected = readable_candidates[0]
                return selected["value"], True, {
                    "status": "accepted",
                    "reason": "",
                    "bubble_bbox": selected["bubble_bbox"],
                    "palette": selected["palette"],
                    "readable_candidates": readable_candidates,
                }
            return None, True, {
                "status": "unreadable",
                "reason": "conflicting_readable_values",
                "readable_candidates": readable_candidates,
            }

        palette, _bubble, _score, bbox = max(candidates, key=lambda item: item[2])
        return None, True, {
            "status": "unreadable",
            "reason": "digit_unreadable",
            "bubble_bbox": bbox,
            "palette": palette,
        }

    def _crop_failure_rate_bubble_from_mask(
        self,
        roi: np.ndarray,
        mask: np.ndarray,
        *,
        row_fill_ratio: float = 0.20,
    ) -> tuple[np.ndarray | None, int | None, list[int] | None]:
        mask_u8 = (mask * 255).astype(np.uint8)
        mask_u8 = cv2.morphologyEx(
            mask_u8,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3)),
        )
        row_counts = (mask_u8 > 0).sum(axis=1)
        row_threshold = max(18, int(roi.shape[1] * row_fill_ratio))
        row_ranges = self._merge_index_ranges(np.where(row_counts > row_threshold)[0])
        min_height = max(18, int(roi.shape[0] * 0.12))
        min_start = int(roi.shape[0] * 0.10)
        max_start = int(roi.shape[0] * 0.55)
        candidates = [
            (start, end, int(row_counts[start : end + 1].max()))
            for start, end in row_ranges
            if min_start <= start <= max_start and (end - start + 1) >= min_height
        ]
        if not candidates:
            return None, None, None

        best: tuple[int, int, int, int, int] | None = None
        for y0, y1, row_peak in candidates:
            body = mask_u8[y0 : y1 + 1, :] > 0
            col_counts = body.sum(axis=0)
            col_threshold = max(5, int(body.shape[0] * 0.22))
            col_ranges = self._merge_index_ranges(np.where(col_counts > col_threshold)[0])
            if not col_ranges:
                continue
            x0, x1 = max(col_ranges, key=lambda item: item[1] - item[0])
            width = x1 - x0 + 1
            if width < int(roi.shape[1] * 0.30):
                continue
            height = y1 - y0 + 1
            # Failure-rate bubbles are wide pills.  This rejects the round
            # orange training-card artwork that can share the same palette.
            if width < int(roi.shape[1] * 0.60) or width * 5 < height * 8:
                continue
            score = (y1 - y0 + 1) * width + row_peak * 5 - y0 * 100
            if best is None or score > best[0]:
                best = (score, y0, y1, x0, x1)
        if best is None:
            return None, None, None

        score, y0, y1, x0, x1 = best
        pad = max(4, int(min(roi.shape[:2]) * 0.035))
        bubble_y0 = max(0, y0 - pad)
        bubble_y1 = min(roi.shape[0], y1 + pad + 1)
        bubble_x0 = max(0, x0 - pad)
        bubble_x1 = min(roi.shape[1], x1 + pad + 1)
        bubble = roi[bubble_y0:bubble_y1, bubble_x0:bubble_x1]
        return bubble, score, [bubble_x0, bubble_y0, bubble_x1, bubble_y1]

    def _read_failure_rate_digits(self, bubble: np.ndarray) -> _FailureRateRead:
        def read_region(region: np.ndarray) -> _FailureRateRead:
            hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
            yellow = (
                (hsv[:, :, 0] >= 18)
                & (hsv[:, :, 0] <= 42)
                & (hsv[:, :, 1] > 90)
                & (hsv[:, :, 2] > 150)
            )
            white = (hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 185)
            prev_glow = getattr(_FAILURE_CTX, "glow_mask", None)
            prev_contrast = getattr(_FAILURE_CTX, "contrast", None)
            if region.size > 0 and int(region.max()) > 0:
                glow_fg, contrast = extract_glow_resistant_foreground(region)
                _FAILURE_CTX.glow_mask = glow_fg
                _FAILURE_CTX.contrast = contrast
            else:
                _FAILURE_CTX.glow_mask = None
                _FAILURE_CTX.contrast = None
            try:
                return self._read_failure_rate_digits_from_masks(white, yellow)
            finally:
                _FAILURE_CTX.glow_mask = prev_glow
                _FAILURE_CTX.contrast = prev_contrast

        full = read_region(bubble)
        if full.incomplete:
            return full
        if bubble.shape[0] > 80:
            top = read_region(
                bubble[: max(1, int(bubble.shape[0] * 0.58)), :]
            )
            if (
                full.value == 88
                and full.digit_count >= 2
                and top.value in {98, 99}
                and top.digit_count >= 2
                and not top.incomplete
            ):
                return top
            # A top crop can turn the percent glyph's circle into an extra
            # digit.  Do not replace a readable full value merely because
            # the cropped result contains more digits.
            if full.value is not None:
                return full
            return top

        return full

    def _read_failure_rate_digits_from_masks(
        self,
        white: np.ndarray,
        yellow: np.ndarray,
    ) -> _FailureRateRead:
        white_digits = self._digits_from_failure_mask(white)
        yellow_digits = self._digits_from_failure_mask(yellow)
        glow_mask = getattr(_FAILURE_CTX, "glow_mask", None)
        has_glow = (
            isinstance(glow_mask, np.ndarray)
            and glow_mask.shape == white.shape
            and bool(glow_mask.any())
        )

        if white_digits is not None and yellow_digits is not None:
            white_value = self._failure_digits_to_value(white_digits)
            yellow_value = self._failure_digits_to_value(yellow_digits)
            if white_value is not None and len(white_digits) >= 2:
                return _FailureRateRead(white_value, len(white_digits))
            if yellow_value is not None and len(yellow_digits) >= 2:
                return _FailureRateRead(yellow_value, len(yellow_digits))
            if not has_glow:
                if white_value is not None:
                    return _FailureRateRead(white_value, len(white_digits))
                if yellow_value is not None:
                    return _FailureRateRead(yellow_value, len(yellow_digits))
                return _FailureRateRead()

            glow_digits = self._digits_from_failure_mask(glow_mask)
            if glow_digits is None:
                if white_value is not None:
                    return _FailureRateRead(white_value, len(white_digits))
                if yellow_value is not None:
                    return _FailureRateRead(yellow_value, len(yellow_digits))
                return _FailureRateRead()
            glow_value = self._failure_digits_to_value(glow_digits)
            if glow_value is not None and len(glow_digits) >= 2:
                return _FailureRateRead(glow_value, len(glow_digits))
            if white_value is not None:
                return _FailureRateRead(white_value, len(white_digits))
            if yellow_value is not None:
                return _FailureRateRead(yellow_value, len(yellow_digits))
            if glow_value is not None:
                return _FailureRateRead(glow_value, len(glow_digits))
            return _FailureRateRead()

        # At least one of white/yellow returned None (potential incomplete digit or HSV glow wash).
        if not has_glow:
            return _FailureRateRead(incomplete=True)

        _FAILURE_CTX.last_digit_span = None
        glow_digits = self._digits_from_failure_mask(glow_mask)
        glow_span = getattr(_FAILURE_CTX, "last_digit_span", None)
        if glow_digits is None:
            return _FailureRateRead(incomplete=True)
        glow_value = self._failure_digits_to_value(glow_digits)
        if glow_value is None:
            return _FailureRateRead(incomplete=True)
        if len(glow_digits) == 1 and glow_span is not None:
            gx0, gx1, gps = glow_span
            cut_x = int(white.shape[1] * 0.49) + 4
            end_x = gps if gps is not None else int(white.shape[1] * 0.88)
            for ch_mask in (white, yellow):
                sub = ch_mask[:, cut_x:end_x].copy()
                if not sub.any():
                    continue
                n_lb, _, st, _ = cv2.connectedComponentsWithStats((sub * 255).astype(np.uint8), 8)
                for lb in range(1, n_lb):
                    sx, sy, sw, sh, sa = map(int, st[lb])
                    ax0 = cut_x + sx
                    ax1 = ax0 + sw
                    if sh >= max(8, int(white.shape[0] * 0.20)) and sa >= 8:
                        if ax1 < gx0 - 2 or ax0 > gx1 + 2:
                            return _FailureRateRead(incomplete=True)
        return _FailureRateRead(glow_value, len(glow_digits))

    def _failure_digits_to_value(self, digits: list[str]) -> int | None:
        if not digits:
            return None
        value = self._safe_int("".join(digits))
        if value is None or value < 0 or value > 100:
            return None
        return value

    def _digits_from_failure_mask(self, mask: np.ndarray) -> list[str] | None:
        mask = mask.copy()
        cut49 = int(mask.shape[1] * 0.49)
        mask[:, :cut49] = False
        percent_start = self._failure_percent_start(mask)
        if percent_start is not None:
            mask[:, percent_start:] = False
        raw_u8 = (mask * 255).astype(np.uint8)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        mask_u8 = cv2.morphologyEx(raw_u8, cv2.MORPH_OPEN, kernel)
        n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask_u8, 8)

        components: list[tuple[int, int, int, int, int, int]] = []
        split_source_labels: set[int] = set()
        unsplit_wide_boxes: list[tuple[int, int, int, int]] = []
        next_synth_label = int(n_labels)
        contrast_map = getattr(_FAILURE_CTX, "contrast", None)
        if not (isinstance(contrast_map, np.ndarray) and contrast_map.shape == mask.shape):
            contrast_map = None

        min_area = max(35, int(mask.shape[0] * mask.shape[1] * 0.008))
        min_height = max(12, int(mask.shape[0] * 0.22))
        for label in range(1, n_labels):
            x = int(stats[label, cv2.CC_STAT_LEFT])
            y = int(stats[label, cv2.CC_STAT_TOP])
            w = int(stats[label, cv2.CC_STAT_WIDTH])
            h = int(stats[label, cv2.CC_STAT_HEIGHT])
            area = int(stats[label, cv2.CC_STAT_AREA])
            if area < min_area or area > 3000:
                continue
            if h < min_height or w < 3:
                continue
            aspect = w / max(h, 1)
            # Sparse digits still carry a place in the number. Let the shape
            # classifier decide; dropping one here could authorize a prefix.
            if aspect > 1.25:
                if aspect <= 2.38 and h <= int(mask.shape[0] * 0.65):
                    split_pair = split_touching_failure_digits(
                        labels,
                        label,
                        x,
                        y,
                        w,
                        h,
                        self._classify_failure_digit,
                        gray_region=contrast_map,
                    )
                    if split_pair is not None:
                        orig_comp = (labels[y : y + h, x : x + w] == label).copy()
                        labels[y : y + h, x : x + w][orig_comp] = 0
                        (_, lx, ly, lw, lh, la), (_, rx, ry, rw, rh, ra) = split_pair
                        s_rel = rx - x
                        l_id = next_synth_label
                        r_id = next_synth_label + 1
                        next_synth_label += 2
                        labels[y : y + h, x : x + s_rel][orig_comp[:, :s_rel]] = l_id
                        labels[y : y + h, x + s_rel : x + w][orig_comp[:, s_rel:]] = r_id
                        components.append((l_id, lx, ly, lw, lh, la))
                        components.append((r_id, rx, ry, rw, rh, ra))
                        split_source_labels.add(label)
                        continue
                    if percent_start is not None and x >= cut49 + 3:
                        unsplit_wide_boxes.append((x, y, w, h))
                continue
            components.append((label, x, y, w, h, area))

        if not components:
            rim_margin = max(2, int(mask.shape[0] * 0.14))
            min_band_h = max(12, int(mask.shape[0] * 0.25))
            for _ux, uy, _uw, uh in unsplit_wide_boxes:
                if (
                    uy >= rim_margin
                    and (uy + uh) <= (mask.shape[0] - rim_margin)
                    and uh >= min_band_h
                ):
                    return None
            return []

        components.sort(key=lambda item: item[1])
        components = self._filter_failure_digit_components(components)
        if components:
            # The percent glyph can leak into the white mask as a tall trailing
            # component.  Preserve the leftmost plausible digit as the height
            # reference and reject only later components outside that glyph band.
            first_height = components[0][4]
            max_trailing_height = max(first_height + 1, int(first_height * 1.45))
            components = [
                component
                for index, component in enumerate(components)
                if index == 0 or component[4] <= max_trailing_height
            ]
            for _ux, uy, _uw, uh in unsplit_wide_boxes:
                for _, _dx, dy, _dw, dh, _ in components:
                    if min(uy + uh, dy + dh) - max(uy, dy) >= min(uh, dh) * 0.65:
                        return None
        digits: list[str] = []
        kept_digit_boxes: list[tuple[int, int, int, int]] = []
        previous_digit_component: tuple[int, int, int, int, int, int] | None = None
        max_digit_gap = max(10, int(mask.shape[1] * 0.12))
        kept_labels = {component[0] for component in components} | split_source_labels
        for label in range(1, n_labels):
            if label in kept_labels:
                continue
            x, y, w, h, _ = map(int, stats[label])
            for _, dx, dy, dw, dh, _ in components:
                overlap = min(y + h, dy + dh) - max(y, dy)
                gap = max(x - (dx + dw), dx - (x + w))
                if (dh * 0.5 <= h <= dh * 1.35 and w <= h * 1.25
                        and 0 <= gap <= max_digit_gap
                        and overlap >= min(h, dh) * 0.75):
                    # A small/filtered same-band remnant is not proof of absence.
                    return None

        # Pre-morphology 1px stroke remnant guard: 2x2 MORPH_OPEN erases 1px-wide
        # occluded vertical strokes at 720p, which must NEVER authorize a suffix/prefix.
        raw_n, _, raw_stats, _ = cv2.connectedComponentsWithStats(raw_u8, 8)
        for r_lb in range(1, raw_n):
            rx, ry, rw, rh, r_area = map(int, raw_stats[r_lb])
            if rx <= cut49 + 3 or r_area < 7:
                continue
            if any(min(rx + rw, dx + dw) - max(rx, dx) > 0 and min(ry + rh, dy + dh) - max(ry, dy) > 0 for _, dx, dy, dw, dh, _ in components):
                continue
            for _, dx, dy, dw, dh, _ in components:
                overlap = min(ry + rh, dy + dh) - max(ry, dy)
                gap = max(rx - (dx + dw), dx - (rx + rw))
                if (
                    dh * 0.50 <= rh <= dh * 1.60
                    and rw <= rh * 1.25
                    and 0 <= gap <= max_digit_gap
                    and overlap >= min(rh, dh) * 0.75
                ):
                    return None

        for index, (label, x, y, w, h, area) in enumerate(components):
            digit = self._classify_failure_digit(labels, label, x, y, w, h, area)
            if digit is None:
                if previous_digit_component is not None:
                    _, prev_x, prev_y, prev_w, prev_h, _ = previous_digit_component
                    overlap = min(y + h, prev_y + prev_h) - max(y, prev_y)
                    if (
                        x - (prev_x + prev_w) <= max_digit_gap
                        and overlap >= min(h, prev_h) * 0.75
                    ):
                        # A missing later glyph cannot authorize its prefix.
                        # Off-band artwork and distant percent remnants do not
                        # count as an incomplete digit in this number.
                        return None
                    break
                if index + 1 < len(components):
                    _, next_x, next_y, _, next_h, _ = components[index + 1]
                    overlap = min(y + h, next_y + next_h) - max(y, next_y)
                    if (next_x - (x + w) <= max_digit_gap
                            and overlap >= min(h, next_h) * 0.75):
                        # A missing leading glyph cannot authorize its suffix.
                        return None
                continue
            if previous_digit_component is not None:
                _, prev_x, _, prev_w, _, _ = previous_digit_component
                if x - (prev_x + prev_w) > max_digit_gap:
                    break
            digits.append(digit)
            kept_digit_boxes.append((x, y, w, h))
            previous_digit_component = (label, x, y, w, h, area)
            if len(digits) >= 3:
                break
        if kept_digit_boxes:
            _FAILURE_CTX.last_digit_span = (
                min(bx for bx, _, _, _ in kept_digit_boxes),
                max(bx + bw for bx, _, bw, _ in kept_digit_boxes),
                percent_start,
            )
        else:
            _FAILURE_CTX.last_digit_span = None
        return digits

    @staticmethod
    def _failure_percent_start(mask: np.ndarray) -> int | None:
        """Find the percent delimiter by two diagonal rings and its slash."""
        contours, hierarchy = cv2.findContours(
            mask.astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE,
        )
        if hierarchy is None:
            return None
        holes = [(index, int(item[3])) for index, item in enumerate(hierarchy[0]) if item[3] >= 0]
        for first, parent_a in holes:
            ax, ay, aw, ah = cv2.boundingRect(contours[first])
            for second, parent_b in holes:
                bx, by, bw, bh = cv2.boundingRect(contours[second])
                if parent_a == parent_b or bx <= ax or by <= ay:
                    continue
                dx = bx + bw / 2 - ax - aw / 2
                dy = by + bh / 2 - ay - ah / 2
                if dy <= 0 or not 0.75 <= dx / dy <= 2:
                    continue
                if not (0.6 <= aw / ah <= 1.7 and 0.6 <= bw / bh <= 1.7
                        and 0.5 <= ah / bh <= 2):
                    continue
                x, y, w, h = cv2.boundingRect(np.concatenate((contours[parent_a], contours[parent_b])))
                if h < 8 or not 0.75 <= w / h <= 1.5:
                    continue
                slash = np.zeros((h, w), np.uint8)
                cv2.line(slash, (round(w * 0.75), 0), (round(w * 0.20), h - 1), 1, 1)
                if mask[y:y+h, x:x+w][slash > 0].mean() >= 0.75:
                    return x
        return find_failure_percent_start_glyph(mask)

    @staticmethod
    def _filter_failure_digit_components(
        components: list[tuple[int, int, int, int, int, int]],
    ) -> list[tuple[int, int, int, int, int, int]]:
        reference = [
            (h, area)
            for _, _, _, w, h, area in components
            if area / max(w * h, 1) >= 0.45 and w / max(h, 1) <= 1.25
        ]
        if not reference:
            return components
        # Digits are read left to right.  A partially segmented percent glyph
        # can be taller than the actual first digit, so a median over all
        # components can discard that first digit before classification.
        _, _, _, ref_width, ref_height, ref_area = components[0]
        min_height = max(8, int(ref_height * 0.65))
        max_height = max(min_height + 1, int(ref_height * 1.35))
        return [
            item
            for item in components
            if min_height <= item[4] <= max_height
            # Narrow digits (notably 1) contain less ink than a wide neighbour.
            # Compare area at the same width; keep the absolute component gate.
            and item[5] >= max(20, int(ref_area * 0.50 * min(1, item[3] / ref_width)))
        ]

    @staticmethod
    def _classify_failure_digit(
        labels: np.ndarray,
        label: int,
        x: int,
        y: int,
        w: int,
        h: int,
        area: int,
    ) -> str | None:
        del area
        if h <= 0 or w <= 0:
            return None
        crop = (labels[y : y + h, x : x + w] == label).astype(np.uint8) * 255
        contrast = getattr(_FAILURE_CTX, "contrast", None)
        gray_patch = (
            contrast[y : y + h, x : x + w]
            if isinstance(contrast, np.ndarray) and contrast.shape == labels.shape
            else None
        )
        digit, _score = classify_failure_digit_glyph(crop, gray_patch=gray_patch)
        return digit

    @staticmethod
    def _failure_digit_segments(crop: np.ndarray) -> dict[str, float]:
        normalized = cv2.resize(
            (crop > 0).astype(np.uint8),
            (21, 35),
            interpolation=cv2.INTER_NEAREST,
        ).astype(bool)
        # A one-pixel raster phase changes a small horizontal cap from three
        # rows to four. Do not count that full-width cap as a vertical stroke
        # (notably the upper-left of 7 or lower-right of 2).
        full_width = normalized.mean(axis=1) >= 0.80
        top_end = 0
        while top_end < 17 and full_width[top_end]:
            top_end += 1
        bottom_start = 35
        while bottom_start > 18 and full_width[bottom_start - 1]:
            bottom_start -= 1
        regions = {
            "top": normalized[0:8, 5:16],
            "middle": normalized[14:22, 5:16],
            "bottom": normalized[27:35, 5:16],
            "upper_left": normalized[max(5, top_end):18, 0:8],
            "upper_right": normalized[max(5, top_end):18, 13:21],
            "lower_left": normalized[17:min(30, bottom_start), 0:8],
            "lower_right": normalized[17:min(30, bottom_start), 13:21],
        }
        return {key: float(value.mean()) for key, value in regions.items()}

    @staticmethod
    def _merge_index_ranges(indices: np.ndarray) -> list[tuple[int, int]]:
        if len(indices) == 0:
            return []
        ranges: list[tuple[int, int]] = []
        start = int(indices[0])
        last = start
        for raw in indices[1:]:
            value = int(raw)
            if value > last + 1:
                ranges.append((start, last))
                start = value
            last = value
        ranges.append((start, last))
        return ranges

    def _detect_vitality(self, img: np.ndarray, result: TrainingData) -> None:
        """检测体力条百分比。"""
        h, w = img.shape[:2]
        y0, y1 = int(h * 0.15), int(h * 0.22)
        roi_hsv = cv2.cvtColor(img[y0:y1, :, :], cv2.COLOR_BGR2HSV)

        green_mask = (
            (roi_hsv[:, :, 0] >= 38)
            & (roi_hsv[:, :, 0] <= 92)
            & (roi_hsv[:, :, 1] > 60)
            & (roi_hsv[:, :, 2] > 80)
        )
        green_ratio = float(np.mean(green_mask))

        red_mask = (
            (roi_hsv[:, :, 0] <= 20)
            & (roi_hsv[:, :, 1] > 60)
            & (roi_hsv[:, :, 2] > 100)
        )
        red_ratio = float(np.mean(red_mask))

        if green_ratio > 0.05 or red_ratio > 0.05:
            total_color = green_ratio + red_ratio
            if total_color > 0:
                result.vitality_bar_percent = int(min(100, (green_ratio / max(total_color, 0.01)) * 100))

    def _detect_hint_count(self, img: np.ndarray, result: TrainingData) -> None:
        """检测红点/技能启发数量（保守检测，高误报率时返回 None）。

        红点特征：亮红色小圆圈 + 白色感叹号，在支援卡头像附近。
        当前纯 OpenCV 难以可靠区分红点与其他红色 UI 元素，
        因此采用保守策略：只在明显匹配时输出，否则标记为不可靠。
        """
        h, w = img.shape[:2]
        y0, y1 = int(h * 0.30), int(h * 0.52)
        roi = img[y0:y1, :, :]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        # 亮红色检测
        red = (
            ((hsv[:, :, 0] <= 15) | (hsv[:, :, 0] >= 165))
            & (hsv[:, :, 1] > 130)
            & (hsv[:, :, 2] > 180)
        )
        red_u8 = (red * 255).astype(np.uint8)

        # 腐蚀分离相邻红色区域
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        eroded = cv2.erode(red_u8, kernel, iterations=1)

        n_labels, _, stats, _ = cv2.connectedComponentsWithStats(eroded, 8)

        count = 0
        for i in range(1, n_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            bw = stats[i, cv2.CC_STAT_WIDTH]
            bh = stats[i, cv2.CC_STAT_HEIGHT]
            if not (30 < area < 250 and 5 < bw < 35 and 7 < bh < 30):
                continue
            aspect = bw / max(bh, 1)
            if aspect > 1.5:
                continue
            bx = stats[i, cv2.CC_STAT_LEFT]
            by = stats[i, cv2.CC_STAT_TOP]
            blob_mean = float(np.mean(gray[by:by + bh, bx:bx + bw]))
            if blob_mean < 135:
                continue
            count += 1

        # 保守策略：检测结果标记为不可靠
        result.hint_count = count
        result.unknown_fields.append("hint_detection_unreliable")

    def _detect_hint_count_conservative(self, img: np.ndarray, result: TrainingData) -> None:
        existing = result.hint_count
        probe = TrainingData()
        self._detect_hint_count(img, probe)

        candidates = [
            value
            for value in (existing, probe.hint_count)
            if self._hint_count_candidate_is_plausible(value, result.support_count)
        ]
        if candidates:
            result.hint_count = max(candidates)
        if "hint_detection_unreliable" not in result.unknown_fields:
            result.unknown_fields.append("hint_detection_unreliable")

    @staticmethod
    def _hint_count_candidate_is_plausible(value: Any, support_count: int | None) -> bool:
        if not isinstance(value, int) or value < 0:
            return False
        if isinstance(support_count, int) and support_count >= 0 and value > support_count:
            return False
        return True

    # ========== 本地模板 OCR 提取 ==========

    def _extract_via_dynamic_ocr(
        self, img: np.ndarray, result: TrainingData, *, lightweight: bool = False,
        scenario: str | None = None,
    ) -> bool:
        """Adapt either local reader to the same verified two-row gain contract."""
        source = "own_template_v4" if lightweight else "ppocrv5_dynamic"
        strategy = "lightweight" if lightweight else "dynamic"
        unavailable = "lightweight_ocr_unavailable" if lightweight else "dynamic_ocr_unavailable"
        if img is None:
            result.unknown_fields.append(unavailable)
            return False
        height, width = img.shape[:2]
        if (width, height) == (1440, 2560):
            img = cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        elif (width, height) != (720, 1280):
            result.unknown_fields.append(unavailable)
            return False

        if lightweight and not self._lightweight_reader_init_attempted:
            self._lightweight_reader_init_attempted = True
            try:
                from core.lightweight_gain_reader import LightweightGainReader

                self._lightweight_reader = LightweightGainReader()
            except Exception:
                result.unknown_fields.append(unavailable)
                return False
        if not lightweight and self._numeric_reader is None and not self._numeric_reader_init_attempted:
            self._numeric_reader_init_attempted = True
            try:
                self._numeric_reader = PpOcrV5NumericReader()
            except Exception:
                result.unknown_fields.append("dynamic_ocr_unavailable")
                return False
        reader = self._lightweight_reader if lightweight else self._numeric_reader
        if reader is None:
            result.unknown_fields.append(unavailable)
            return False

        try:
            if lightweight and scenario == "larc":
                values = reader.read_gains(img, strategy=strategy, scenario=scenario)
            else:
                values = reader.read_gains(img, strategy=strategy)
        except Exception:
            result.unknown_fields.append(unavailable)
            return False
        if not isinstance(values, dict):
            result.unknown_fields.append(unavailable)
            return False

        gain_values: dict[str, int] = {}
        columns: list[dict[str, Any]] = []
        reader_name = getattr(reader, "engine_name", "ppocrv5_mobile")
        for attr in COLUMN_REGIONS:
            candidate = values.get(attr)
            value = (
                candidate.get("value")
                if isinstance(candidate, dict)
                else getattr(candidate, "value", None)
            )
            engine = (
                candidate.get("engine")
                if isinstance(candidate, dict)
                else getattr(candidate, "engine", None)
            )
            raw_text = (
                candidate.get("raw_text", "")
                if isinstance(candidate, dict)
                else getattr(candidate, "raw_text", "")
            )
            reason = (
                candidate.get("reason", "")
                if isinstance(candidate, dict)
                else getattr(candidate, "reason", "")
            )
            candidate_shape_valid = (
                type(value) is int
                and value > 0
                and reason == "gain_rows_verified"
            )
            column = {
                "attribute_id": attr,
                "status": "accepted" if candidate_shape_valid else "abstained",
                "canonical_status": "accepted" if candidate_shape_valid else "abstained",
                "canonical_value": value if candidate_shape_valid else None,
                "engine": engine if isinstance(engine, str) else "",
                "raw_text": str(raw_text or ""),
                "reason": str(reason or ""),
                "rows": _dynamic_candidate_rows(candidate),
            }
            secondary_scores = [row['confidence'] for row in column['rows']
                if row.get('recognition_route') == 'secondary' and type(row.get('confidence')) in (int, float)]
            if secondary_scores:
                column['confidence'] = min(secondary_scores)
                column['confidence_basis'] = 'secondary_recognition_confidence'
            accepted = candidate_shape_valid and validate_dynamic_gain_column(
                {
                    "source": source,
                    "reader": reader_name,
                },
                column,
                attribute=attr,
                value=value,
            )
            if accepted:
                gain_values[attr] = value
            else:
                column["status"] = "abstained"
                column["canonical_status"] = "abstained"
                column["canonical_value"] = None
            columns.append(column)

        if not gain_values:
            result.stats_gain_ocr = {
                "schema_version": "training_stats_gain_ocr_rows.v1" if lightweight else "training_stats_gain_ocr_dynamic.v1",
                "source": source,
                "strategy": strategy,
                "reader": reader_name,
                "columns": columns,
            }
            result.unknown_fields.append("lightweight_gain_not_verified" if lightweight else "dynamic_gain_not_verified")
            return False

        result.stats_gain = _stats_gain_from_mapping(gain_values) or StatsGain()
        result.stats_gain_total = _stats_gain_from_mapping(gain_values) or StatsGain()
        result.stats_gain_ocr = {
            "schema_version": "training_stats_gain_ocr_rows.v1" if lightweight else "training_stats_gain_ocr_dynamic.v1",
            "source": source,
            "strategy": strategy,
            "reader": reader_name,
            "columns": columns,
        }
        self._validate_gains(result.stats_gain, result)
        return True

    def _extract_via_local_template(
        self,
        image_path: str,
        result: TrainingData,
        *,
        image: np.ndarray | None = None,
    ) -> bool:
        """用本地模板 OCR 识别增益数字（默认路径，不依赖 MIMO）。

        Returns:
            True if 识别成功（>=3 reliable gains 且 value_unreliable=False）。
        """
        from core.local_training_ocr import recognize_gains

        ocr_result = recognize_gains(image_path, mode="template", image=image)
        result.stats_gain_ocr = ocr_result.get("stats_gain_ocr")
        result.base_gain = _stats_gain_from_mapping(ocr_result.get("base_gain"))
        result.bonus_gain = _stats_gain_from_mapping(ocr_result.get("bonus_gain"))
        result.stats_gain_total = _stats_gain_from_mapping(ocr_result.get("stats_gain_total"))
        gains_list = ocr_result.get("gains", [])
        partials = ocr_result.get("partials", [])
        value_unreliable = ocr_result.get("value_unreliable", True)
        n_reliable = ocr_result.get("n_reliable", 0)
        n_total = ocr_result.get("n_total", 0)

        # 用 col 字段归档（本地模板已按列归档）
        sg = result.stats_gain
        gains_dict = {k: 0 for k in COLUMN_REGIONS}
        components = {k: [] for k in COLUMN_REGIONS}

        for g in gains_list:
            col = g.get("col")
            if col and col in gains_dict:
                gains_dict[col] += g["n"]
                components[col].append(g["n"])

        conservative_gains = _conservative_stats_gain_from_ocr(result.stats_gain_ocr)

        sg.speed = conservative_gains.get("speed")
        sg.stamina = conservative_gains.get("stamina")
        sg.power = conservative_gains.get("power")
        sg.guts = conservative_gains.get("guts")
        sg.wisdom = conservative_gains.get("wisdom")
        sg.skill_pt = conservative_gains.get("skill_pt")

        result.raw_mimo = {
            "source": "local_template",
            "gains": gains_dict,
            "components": components,
            "raw_positions": gains_list,
            "partials": partials,
            "template_matches": ocr_result.get("template_matches", []),
            "n_reliable": n_reliable,
            "n_total": n_total,
            "value_unreliable": value_unreliable,
            "stats_gain_ocr": result.stats_gain_ocr,
            "base_gain": ocr_result.get("base_gain"),
            "bonus_gain": ocr_result.get("bonus_gain"),
            "stats_gain_total": ocr_result.get("stats_gain_total"),
        }

        self._validate_gains(sg, result)

        # 判断是否足够可靠
        non_zero = sum(1 for v in gains_dict.values() if v and v > 0)
        if value_unreliable or non_zero < 3:
            result.unknown_fields.append("local_template_incomplete")
            return False

        return True

    # ========== 本地 OCR 提取 ==========

    def _extract_via_local_ocr(
        self,
        image_path: str,
        result: TrainingData,
        *,
        image: np.ndarray | None = None,
    ) -> bool:
        """用本地 OpenCV OCR（模板安全模式）识别增益数字。"""
        from core.local_training_ocr import recognize_gains

        ocr_result = recognize_gains(image_path, mode="heuristic", image=image)
        result.stats_gain_ocr = ocr_result.get("stats_gain_ocr")
        result.base_gain = _stats_gain_from_mapping(ocr_result.get("base_gain"))
        result.bonus_gain = _stats_gain_from_mapping(ocr_result.get("bonus_gain"))
        result.stats_gain_total = _stats_gain_from_mapping(ocr_result.get("stats_gain_total"))
        gains_list = ocr_result.get("gains", [])
        confidence = ocr_result.get("confidence", 0.0)
        value_unreliable = ocr_result.get("value_unreliable", True)

        if not gains_list or confidence < 0.3:
            logger.debug(f"本地 OCR 结果不足: gains={len(gains_list)}, conf={confidence}")
            return False

        # 按 x 位置归档到列
        sg = result.stats_gain
        gains_dict, components = self._assign_gains_to_columns(gains_list)

        conservative_gains = _conservative_stats_gain_from_ocr(result.stats_gain_ocr)

        sg.speed = conservative_gains.get("speed")
        sg.stamina = conservative_gains.get("stamina")
        sg.power = conservative_gains.get("power")
        sg.guts = conservative_gains.get("guts")
        sg.wisdom = conservative_gains.get("wisdom")
        sg.skill_pt = conservative_gains.get("skill_pt")

        result.raw_mimo = {
            "source": "local_ocr",
            "gains": gains_dict,
            "components": components,
            "raw_positions": gains_list,
            "ocr_confidence": confidence,
            "value_unreliable": value_unreliable,
            "stats_gain_ocr": result.stats_gain_ocr,
            "base_gain": ocr_result.get("base_gain"),
            "bonus_gain": ocr_result.get("bonus_gain"),
            "stats_gain_total": ocr_result.get("stats_gain_total"),
        }

        self._validate_gains(sg, result)

        # 标记不可靠
        if value_unreliable:
            result.unknown_fields.append("local_ocr_value_unreliable")

        non_zero = sum(1 for v in gains_dict.values() if v and v > 0)
        # 数值不可靠时返回 False，让 auto 模式 fallback 到 MIMO
        if value_unreliable:
            return False
        return non_zero >= 1 and confidence >= 0.4

    def _assign_gains_to_columns(
        self, gains_list: list[dict]
    ) -> tuple[dict[str, int], dict[str, list[int]]]:
        """将增益列表按 x 位置归档到列（最近中心点 + 偏移修正）。

        MIMO 的 x 位置有系统性左偏约 -0.06，修正后归档更准确。
        """
        gains: dict[str, int] = {k: 0 for k in COLUMN_REGIONS}
        components: dict[str, list[int]] = {k: [] for k in COLUMN_REGIONS}

        col_centers = {k: (x0 + x1) / 2 for k, (x0, x1) in COLUMN_REGIONS.items()}
        # MIMO x 位置系统性左偏修正
        _X_BIAS_CORRECTION = 0.05

        for item in gains_list:
            x_frac = float(item.get("x", 0)) + _X_BIAS_CORRECTION
            val = abs(int(item.get("n", 0)))
            if val <= 0 or val > 40:
                continue

            x_px = x_frac * 1440
            col = min(col_centers, key=lambda k: abs(x_px - col_centers[k]))

            gains[col] += val
            components[col].append(val)

        return gains, components

    # ========== mimo 远程提取 ==========

    def _extract_via_mimo(self, image_path: str, result: TrainingData) -> None:
        """用 mimo2.5 提取数值增益和训练类型。

        优先使用列裁剪方案（column-based），每栏独立识别 +N。
        如果列裁剪结果不足，回退到整图方案。
        """
        try:
            from core.mimo_client import get_mimo_client
        except ImportError:
            return

        client = get_mimo_client()

        # --- 方案 1：列裁剪 ---
        column_ok = self._extract_via_columns(image_path, client, result)

        if column_ok:
            # 列裁剪成功，用文件名推断训练类型
            filename_type = infer_training_type_from_filename(image_path)
            if result.training_type == "unknown" and filename_type:
                result.training_type = filename_type
            return

        # --- 方案 2：整图 fallback ---
        logger.info("列裁剪结果不足，回退整图方案")
        self._extract_via_whole_image(image_path, client, result)

        # --- 最终兜底：文件名推断训练类型 ---
        if result.training_type == "unknown":
            filename_type = infer_training_type_from_filename(image_path)
            if filename_type:
                result.training_type = filename_type

    def _extract_via_columns(
        self, image_path: str, client: Any, result: TrainingData
    ) -> bool:
        """列裁剪方案：裁剪增益条，单次 MIMO 调用识别所有 +N 及其 x 位置。

        Returns:
            True if enough data was extracted (>=3 non-zero gains).
        """
        img = cv2.imread(image_path)
        if img is None:
            return False

        h, w = img.shape[:2]
        if w != 1440 or h != 2560:
            logger.debug(f"图片尺寸 {w}x{h} 非 1440x2560，跳过列裁剪")
            return False

        # 裁剪增益条并在列边界画标记线（帮助 MIMO 理解列位置）
        gain_strip = img[_GAIN_Y_TOP:_GAIN_Y_BOTTOM, 0:1440].copy()
        _col_colors = [
            (0, 0, 255),    # speed - red
            (0, 165, 255),  # stamina - orange
            (0, 255, 0),    # power - green
            (255, 0, 0),    # guts - blue
            (255, 0, 255),  # wisdom - purple
            (0, 255, 255),  # skill_pt - yellow
        ]
        for idx, (col_key, (x0, x1)) in enumerate(COLUMN_REGIONS.items()):
            color = _col_colors[idx % len(_col_colors)]
            cv2.line(gain_strip, (x0, 0), (x0, gain_strip.shape[0]), color, 2)

        _, buf = cv2.imencode(".png", gain_strip)
        strip_b64 = base64.standard_b64encode(buf.tobytes()).decode("utf-8")

        col_names = ["speed", "stamina", "power", "guts", "wisdom", "skill_pt"]
        col_desc = ", ".join(f"{n}" for n in col_names)
        prompt = (
            f"This image has 6 colored vertical lines dividing it into columns: {col_desc}. "
            "Find all +N gain numbers. For each, report which COLUMN it belongs to. "
            "Output JSON only: {\"gains\":[{\"col\":\"speed\",\"n\":18},{\"col\":\"power\",\"n\":5}]}"
        )

        raw = self._call_mimo_cached(
            mode="column_crop",
            image_bytes=buf.tobytes(),
            prompt=prompt,
            call=lambda: client.analyze_image_base64(strip_b64, prompt),
        )
        parsed = self._extract_json_from_mimo(raw)
        if not parsed or "gains" not in parsed:
            return False
        parsed_type = TYPE_MAP.get(str(parsed.get("t", "")))
        if parsed_type:
            result.training_type = parsed_type

        # 归档到列：优先用 MIMO 报告的 col，x 位置作为 fallback
        gains: dict[str, int] = {k: 0 for k in COLUMN_REGIONS}
        components: dict[str, list[int]] = {k: [] for k in COLUMN_REGIONS}
        col_centers = {k: (x0 + x1) / 2 for k, (x0, x1) in COLUMN_REGIONS.items()}
        _X_BIAS = 0.05

        for item in parsed["gains"]:
            val = abs(int(item.get("n", 0)))
            if val <= 0 or val > 40:
                continue

            # 优先使用 col 字段（来自带标记线的图片）
            col = item.get("col", "")
            if col not in COLUMN_REGIONS:
                # fallback：x 位置最近中心
                x_frac = float(item.get("x", 0)) + _X_BIAS
                x_px = x_frac * 1440
                col = min(col_centers, key=lambda k: abs(x_px - col_centers[k]))
            if col:
                gains[col] += val
                components[col].append(val)

        # 归档到 StatsGain
        result.raw_mimo = {
            "source": "column_crop",
            "gains": gains,
            "components": components,
            "raw_positions": parsed["gains"],
        }

        non_zero = sum(1 for v in gains.values() if v > 0)
        return non_zero >= 1

    def _extract_via_whole_image(
        self, image_path: str, client: Any, result: TrainingData
    ) -> None:
        """整图 fallback 方案（原有逻辑）。"""
        prompt = (
            "Read the training result area by COLUMN POSITION from left to right.\n"
            "There are exactly 6 horizontal columns in this order:\n"
            "  Column 1 (leftmost): 速度/Speed -> sp\n"
            "  Column 2: 耐力/Stamina -> st\n"
            "  Column 3: 力量/Power -> pw\n"
            "  Column 4: 根性/Guts -> gt\n"
            "  Column 5: 智力/Wisdom -> ws\n"
            "  Column 6 (rightmost): 技能点/SkillPt -> sk\n"
            "\n"
            "For each column, read ALL green +N numbers that appear in that column.\n"
            "A column may have multiple +N numbers (e.g. +4 and +7 in skill point column).\n"
            "Record each +N as a component, then sum them for the total.\n"
            "\n"
            "CRITICAL RULES:\n"
            "- Do NOT move numbers between columns based on meaning.\n"
            "- Numbers in the 技能点 column MUST go to sk, even if they look like other stats.\n"
            "- If 技能点 column has +4 and +7, then sk_components=[4,7] and sk=11.\n"
            "- Do NOT read the large current stat totals (like 207, 170).\n"
            "- If a column shows no +N, use empty list and 0.\n"
            "\n"
            "Also: sup=number of character portrait circles. frd=true if 友情训练 visible.\n"
            "Output JSON:\n"
            "{\"t\":\"?\",\"sp\":0,\"st\":0,\"pw\":0,\"gt\":0,\"ws\":0,\"sk\":0,"
            "\"sp_c\":[],\"st_c\":[],\"pw_c\":[],\"gt_c\":[],\"ws_c\":[],\"sk_c\":[],"
            "\"fail\":0,\"sup\":0,\"frd\":false}"
        )

        try:
            image_bytes = Path(image_path).read_bytes()
        except OSError:
            image_bytes = str(image_path).encode("utf-8")
        raw = self._call_mimo_cached(
            mode="whole_image",
            image_bytes=image_bytes,
            prompt=prompt,
            call=lambda: client.analyze_image(image_path, prompt),
        )
        parsed = self._extract_json_from_mimo(raw)
        if not parsed:
            return

        result.raw_mimo = parsed

        # --- 训练类型推断（仅当文件名推断失败时） ---
        t = str(parsed.get("t", ""))
        if t in TYPE_MAP:
            result.training_type = TYPE_MAP[t]
        elif result.training_type == "unknown":
            GAIN_KEY_MAP = {"sp": "speed", "st": "stamina", "pw": "power", "gt": "guts", "ws": "wisdom"}
            best_key = max(GAIN_KEY_MAP, key=lambda k: int(parsed.get(k, 0) or 0))
            best_val = int(parsed.get(best_key, 0) or 0)
            if best_val > 0:
                result.training_type = GAIN_KEY_MAP[best_key]

        # --- pt_misassigned 检测 ---
        self._check_pt_misassign(parsed, result)

        if result.failure_rate is None and "fail" in parsed:
            result.failure_rate = self._safe_int(parsed.get("fail"))

        if "sup" in parsed and result.support_slots_source is None:
            mimo_sup = self._safe_int(parsed.get("sup"))
            if mimo_sup is not None and mimo_sup > 0:
                if result.support_count is None or result.support_count == 0:
                    result.support_count = mimo_sup

        if "frd" in parsed and result.support_slots_source is None:
            result.friendship = bool(parsed["frd"])

    def _apply_gains_from_mimo(self, parsed: dict, sg: StatsGain) -> None:
        """Legacy MIMO helper: external candidates never populate decision gains."""
        del parsed, sg

    @staticmethod
    def _check_pt_misassign(parsed: dict, result: TrainingData) -> None:
        """检测 pt 点是否被误分配到非 sk 字段。

        如果 sk_c（技能点组件）有值，但 sk=0 或 None，
        同时 pw/gt/ws 有值疑似来自 pt 点区域，标记 warning。
        """
        sk_components = parsed.get("sk_c", [])
        sk_val = parsed.get("sk", 0)

        if isinstance(sk_components, list) and len(sk_components) > 0:
            expected_sk = sum(int(v) for v in sk_components if v is not None)
            if expected_sk > 0 and (not sk_val or int(sk_val) == 0):
                result.unknown_fields.append("pt_sum_correction_applied")

        # 检查非 sk 字段是否可能包含 pt 点数字
        sk_nums = set()
        if isinstance(sk_components, list):
            sk_nums = {int(v) for v in sk_components if v is not None}

        for key in ("pw", "gt", "ws"):
            val = parsed.get(key)
            if val is not None and int(val) > 0 and int(val) in sk_nums:
                result.unknown_fields.append(f"pt_misassigned_possible:{key}={val}")
    @staticmethod
    def _safe_int(value) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    def _call_mimo_cached(
        self,
        mode: str,
        image_bytes: bytes,
        prompt: str,
        call,
    ) -> Any:
        """Call MIMO with a disk cache keyed by image content and prompt."""
        if not self._cache_enabled or self._cache_dir is None:
            return call()

        digest = hashlib.sha256()
        digest.update(self._CACHE_VERSION.encode("utf-8"))
        digest.update(mode.encode("utf-8"))
        digest.update(hashlib.sha256(prompt.encode("utf-8")).digest())
        digest.update(hashlib.sha256(image_bytes).digest())
        cache_path = self._cache_dir / f"{digest.hexdigest()}.json"

        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                raw_text = cached.get("raw_text")
                if isinstance(raw_text, str):
                    return raw_text
            except (OSError, json.JSONDecodeError):
                logger.debug("Ignoring invalid MIMO cache file: %s", cache_path)

        raw_text = call()
        if not isinstance(raw_text, str):
            return raw_text
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {"mode": mode, "raw_text": raw_text},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.debug("Failed to write MIMO cache %s: %s", cache_path, exc)

        return raw_text

    @staticmethod
    def _is_verified_layered_stat_gain(
        attr: str,
        value: int,
        result: TrainingData,
    ) -> bool:
        debug = result.stats_gain_ocr
        columns = debug.get("columns") if isinstance(debug, dict) else None
        dynamic_column = next(
            (
                item
                for item in columns or ()
                if isinstance(item, dict) and item.get("attribute_id") == attr
            ),
            None,
        )
        if validate_dynamic_gain_column(
            debug,
            dynamic_column,
            attribute=attr,
            value=value,
        ):
            return True
        if classify_reliable_exact_gain(dynamic_column, attribute_id=attr).get("value") == value:
            return True
        if not (_MAX_STAT_GAIN < value <= _MAX_LAYERED_STAT_GAIN):
            return False
        if any(
            gain is None
            for gain in (result.base_gain, result.bonus_gain, result.stats_gain_total)
        ):
            return False
        base_value = getattr(result.base_gain, attr, None)
        bonus_value = getattr(result.bonus_gain, attr, None)
        total_value = getattr(result.stats_gain_total, attr, None)
        if not all(isinstance(item, int) and item >= 0 for item in (base_value, bonus_value)):
            return False
        if total_value != value or base_value + bonus_value != value:
            return False

        columns = debug.get("columns") if isinstance(debug, dict) else None
        if not isinstance(columns, list):
            return False
        column = next(
            (
                item
                for item in columns
                if isinstance(item, dict) and item.get("attribute_id") == attr
            ),
            None,
        )
        if not isinstance(column, dict):
            return False
        if column.get("status") != "accepted":
            return False
        if column.get("canonical_status") != "accepted":
            return False
        if column.get("canonical_value") != value:
            return False
        rows = column.get("rows")
        if not isinstance(rows, list):
            return False
        accepted_rows = {
            row.get("kind"): row.get("value")
            for row in rows
            if isinstance(row, dict) and row.get("status") == "accepted"
        }
        return (
            accepted_rows.get("base") == base_value
            and accepted_rows.get("bonus") == bonus_value
        )

    @staticmethod
    def _validate_gains(sg: StatsGain, result: TrainingData) -> None:
        """校验增益数值合理性，异常值置 None 并标记。"""
        # 检查常规属性增益
        for attr in ("speed", "stamina", "power", "guts", "wisdom"):
            val = getattr(sg, attr)
            if (
                val is not None
                and val > _MAX_STAT_GAIN
                and not TrainingParser._is_verified_layered_stat_gain(
                    attr, val, result
                )
            ):
                setattr(sg, attr, None)
                result.unknown_fields.append(f"{attr}_suspicious_total_value")

        # Legacy limits apply only to values without verified gain-row evidence.
        if sg.skill_pt is not None and sg.skill_pt > _MAX_SKILL_PT_GAIN:
            debug = result.stats_gain_ocr or {}
            column = next((c for c in debug.get("columns", []) if c.get("attribute_id") == "skill_pt"), None)
            if not (validate_dynamic_gain_column(
                    debug, column, attribute="skill_pt", value=sg.skill_pt)
                    or classify_reliable_exact_gain(column, attribute_id="skill_pt").get("value") == sg.skill_pt):
                sg.skill_pt = None
                result.unknown_fields.append("skill_pt_suspicious_total_value")

    def _extract_json_from_mimo(self, text: str) -> dict | None:
        """从 mimo 返回文本中提取 JSON。

        支持三种格式：
        1. 纯 JSON：{"t":"speed",...}
        2. Markdown fenced JSON：```json\n{...}\n```
        3. 普通文本夹 JSON：识别结果如下：{...}
        """
        if not text:
            return None

        # 策略1: 提取 ```json ... ``` 代码块
        fence_match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1))
            except json.JSONDecodeError:
                pass

        # 策略2: 遍历找第一个含有效增益数据的 JSON 对象
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start:i + 1]
                    try:
                        parsed = json.loads(candidate)
                        values = [parsed.get(k) for k in ("sp", "st", "pw", "gt", "ws", "sk")]
                        if any(v is not None and v != 0 for v in values):
                            return parsed
                    except json.JSONDecodeError:
                        pass
                    start = -1

        # 策略3: 兜底 — 接受任何有效 JSON（含全0模板）
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        start = -1

        return None

    def _populate_field_reliability(self, result: TrainingData) -> None:
        quality: dict[str, Any] = {
            key: value
            for key, value in result.field_reliability.items()
            if isinstance(key, str)
        }

        stats_quality = {key: "unreliable" for key in COLUMN_REGIONS}
        base_quality = {key: "unreliable" for key in COLUMN_REGIONS}
        bonus_quality = {key: "unreliable" for key in COLUMN_REGIONS}
        total_quality = {key: "unreliable" for key in COLUMN_REGIONS}

        stats_gain_ocr = result.stats_gain_ocr
        columns = stats_gain_ocr.get("columns") if isinstance(stats_gain_ocr, dict) else None
        dynamic_source = (
            isinstance(stats_gain_ocr, dict)
            and stats_gain_ocr.get("source") in {"ppocrv5_dynamic", "own_template_v4"}
        )
        if isinstance(columns, list):
            for column in columns:
                if not isinstance(column, dict):
                    continue
                attr = str(column.get("attribute_id") or "")
                if attr not in COLUMN_REGIONS:
                    continue
                if dynamic_source:
                    column_quality = (
                        "reliable"
                        if column.get("status") == "accepted"
                        and column.get("canonical_status") == "accepted"
                        and type(column.get("canonical_value")) is int
                        and validate_dynamic_gain_column(
                            stats_gain_ocr,
                            column,
                            attribute=attr,
                            value=column.get("canonical_value"),
                        )
                        else "unreliable"
                    )
                else:
                    column_quality = self._column_field_reliability(column)
                stats_quality[attr] = column_quality
                if column.get("base_value") is not None:
                    base_quality[attr] = column_quality
                if column.get("bonus_value") is not None:
                    bonus_quality[attr] = column_quality
                if column.get("sum") is not None or column.get("canonical_value") is not None:
                    total_quality[attr] = column_quality

        quality["stats_gain"] = stats_quality
        quality["base_gain"] = base_quality
        quality["bonus_gain"] = bonus_quality
        quality["stats_gain_total"] = total_quality
        quality["failure_rate"] = self._scalar_field_reliability(
            value=result.failure_rate,
            debug=result.failure_rate_ocr,
        )

        if result.hint_count is None:
            quality["hint_count"] = "unreliable"
        elif "hint_detection_unreliable" in result.unknown_fields:
            quality["hint_count"] = "unreliable"
        else:
            quality["hint_count"] = "reliable"

        if result.fragment_available is True:
            quality["fragment"] = self._fragment_field_reliability(result.fragments)
        elif result.fragment_available is False:
            quality["fragment"] = "reliable"
        else:
            quality["fragment"] = "unreliable"

        result.field_reliability = quality

    @staticmethod
    def _column_field_reliability(column: dict[str, Any]) -> str:
        strict = classify_reliable_exact_gain(
            column,
            attribute_id=str(column.get("attribute_id") or ""),
        )
        if strict.get("status") == "reliable_exact":
            return "reliable"
        status = str(column.get("status") or "")
        if status in {"partial", "candidate", "low_confidence"}:
            return "partial"
        if status == "accepted":
            return "partial"
        if column.get("rows") or column.get("candidates"):
            return "partial"
        return "unreliable"

    @staticmethod
    def _scalar_field_reliability(value: Any, debug: dict | None) -> str:
        if isinstance(debug, dict):
            status = str(debug.get("status") or "")
            if status == "accepted" and value is not None:
                return "reliable"
            if status in {"unreadable", "missing"}:
                return "unreliable"
            if status:
                return "partial"
        return "reliable" if value is not None else "unreliable"

    @staticmethod
    def _fragment_field_reliability(fragments: list[dict[str, Any]]) -> str:
        if not fragments:
            return "partial"
        confidences: list[float] = []
        for fragment in fragments:
            if not isinstance(fragment, dict):
                return "partial"
            if (
                fragment.get("color") in {None, "", "unknown"}
                or fragment.get("stat") in {None, "", "unknown"}
            ):
                return "partial"
            try:
                confidences.append(float(fragment.get("confidence", 0.0)))
            except (TypeError, ValueError):
                return "partial"
        if not confidences or min(confidences) < 0.5:
            return "partial"
        return "reliable"

    # ========== 置信度 ==========

    def _calc_confidence(self, result: TrainingData) -> float:
        score = 0.0
        sg = result.stats_gain

        if result.training_type != "unknown":
            score += 0.15

        gains = [sg.speed, sg.stamina, sg.power, sg.guts, sg.wisdom, sg.skill_pt]
        non_null = sum(1 for g in gains if g is not None)
        score += 0.30 * (non_null / len(gains))

        if result.failure_rate is not None:
            score += 0.10

        if result.support_count is not None:
            score += 0.15

        if result.friendship is not None:
            score += 0.10

        if result.fragment_color is not None:
            score += 0.10

        if result.vitality_bar_percent is not None:
            score += 0.10

        return round(min(1.0, score), 2)

    def _collect_unknowns(self, result: TrainingData) -> list[str]:
        unknowns = []
        sg = result.stats_gain

        if result.training_type == "unknown":
            unknowns.append("training_type")
        if all(g is None for g in [sg.speed, sg.stamina, sg.power, sg.guts, sg.wisdom]):
            unknowns.append("stats_gain")
        if result.failure_rate is None:
            unknowns.append("failure_rate")
        if result.support_count is None:
            unknowns.append("support_count")
        if result.friendship is None:
            unknowns.append("friendship")
        if result.fragment_color is None and result.fragment_available is not False:
            unknowns.append("fragment_color")
        if result.vitality_bar_percent is None:
            unknowns.append("vitality_bar_percent")
        return unknowns


# ---------- 便捷函数 ----------

def parse_training_screenshot(
    image_path: str | Path,
    use_mimo: bool = True,
) -> dict[str, Any]:
    """一次性解析训练截图，返回结构化 dict。"""
    parser = TrainingParser(use_mimo=use_mimo)
    data = parser.parse(image_path)
    return data.to_dict()


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python -m core.training_parser <image_path> [--no-mimo] [--pretty]")
        sys.exit(1)

    image = sys.argv[1]
    use_mimo = "--no-mimo" not in sys.argv
    pretty = "--pretty" in sys.argv

    result = parse_training_screenshot(image, use_mimo=use_mimo)
    print(json.dumps(result, ensure_ascii=False, indent=2 if pretty else None))
