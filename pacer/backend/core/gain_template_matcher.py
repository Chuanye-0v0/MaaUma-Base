"""增益数字模板匹配模块。

从 assets/digit_templates/ 读取模板图片（支持每 label 多模板）。
预处理：HSV 提取橙色文字 → 二值化 → 3x 放大 → 归一化。
匹配时使用 margin 检查防止近似混淆。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# 模板统一尺寸（预处理后 resize 到此尺寸）
_TEMPLATE_SIZE = (120, 150)  # 3x of original 40x50

# 匹配阈值
_MATCH_THRESHOLD = 0.75

# margin 阈值：top1 - top2 必须大于此值才认为可靠
_MARGIN_THRESHOLD = 0.02

# 模板目录（支持任意工作目录调用）
DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "assets" / "digit_templates"
_TEMPLATE_DIR_CACHE: dict[str, dict[str, list[np.ndarray]]] = {}

_VALUE_TEMPLATE_SIZE = (220, 90)
_VALUE_TEMPLATE_PADDING = 8
_VALUE_MATCH_THRESHOLD = 0.45


def preprocess_glyph(img: np.ndarray, scale: int = 3) -> np.ndarray:
    """统一 glyph 预处理：HSV 提取橙色文字 → 二值化 → 放大 → 归一化。

    输入：BGR 或灰度图像（单个数字或 +N 组的裁剪）
    输出：归一化灰度图，尺寸 _TEMPLATE_SIZE
    """
    if len(img.shape) == 3:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        # 橙色/红色文字区域
        mask = (
            ((hsv[:, :, 0] >= 8) & (hsv[:, :, 0] <= 38))
            & (hsv[:, :, 1] > 80)
            & (hsv[:, :, 2] > 120)
        )
        # 白底黑字二值图
        result = np.full(mask.shape, 255, dtype=np.uint8)
        result[mask] = 0
    else:
        # 灰度图：直接阈值
        _, result = cv2.threshold(img, 128, 255, cv2.THRESH_BINARY_INV)
        result = 255 - result  # 转白底黑字

    # 裁剪到内容区域
    tight = _tight_crop(result)

    # 放大
    h, w = tight.shape[:2]
    scaled = cv2.resize(tight, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)

    # 加白边 padding
    pad = 10
    padded = cv2.copyMakeBorder(scaled, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)

    # 归一化到模板尺寸
    return cv2.resize(padded, _TEMPLATE_SIZE)


def _tight_crop(img: np.ndarray) -> np.ndarray:
    """裁剪到非白色像素区域。"""
    # 找非白色像素（暗像素 = 文字）
    dark = img < 200
    ys, xs = np.where(dark)
    if len(ys) < 3:
        return img
    pad = 3
    y0, y1 = max(0, ys.min() - pad), min(img.shape[0], ys.max() + pad + 1)
    x0, x1 = max(0, xs.min() - pad), min(img.shape[1], xs.max() + pad + 1)
    return img[y0:y1, x0:x1]


class GainTemplateMatcher:
    """增益数字模板匹配器（支持每 label 多模板）。"""

    def __init__(self, template_dir: str | Path = DEFAULT_TEMPLATE_DIR):
        self._template_dir = Path(template_dir)
        # {label: [template_img, ...]}
        self._templates: dict[str, list[np.ndarray]] = {}
        self._loaded = False
        self._load_templates()

    @property
    def available(self) -> bool:
        return self._loaded and len(self._templates) > 0

    @property
    def template_count(self) -> int:
        return sum(len(v) for v in self._templates.values())

    @property
    def loaded_digits(self) -> list[str]:
        return sorted(self._templates.keys())

    def _load_templates(self) -> None:
        """从模板目录加载图片。支持 {label}_{index}.png 和 {label}.png。"""
        if not self._template_dir.exists():
            logger.debug(f"模板目录不存在: {self._template_dir}")
            return

        cache_key = str(self._template_dir.resolve())
        cached = _TEMPLATE_DIR_CACHE.get(cache_key)
        if cached is not None:
            self._templates = {k: list(v) for k, v in cached.items()}
            self._loaded = len(self._templates) > 0
            return

        # 优先读 manifest.json 获取精确映射
        manifest_path = self._template_dir / "manifest.json"
        if manifest_path.exists():
            self._load_from_manifest(manifest_path)
        else:
            self._load_from_filenames()

        self._loaded = len(self._templates) > 0
        if self._loaded:
            _TEMPLATE_DIR_CACHE[cache_key] = {k: list(v) for k, v in self._templates.items()}
        if self._loaded:
            logger.info(f"加载 {self.template_count} 个模板 ({len(self._templates)} label): {self.loaded_digits}")

    def _load_from_manifest(self, manifest_path: Path) -> None:
        """从 manifest.json 加载。"""
        import json
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return
        for entry in manifest.get("templates", []):
            filename = entry.get("file", "")
            label = entry.get("label", "")
            if not filename or not label:
                continue
            # 标准化 label
            label = self._normalize_label(label)
            if label is None:
                continue
            path = self._template_dir / filename
            if path.exists():
                img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    self._templates.setdefault(label, []).append(
                        cv2.resize(img, _TEMPLATE_SIZE)
                    )

    def _load_from_filenames(self) -> None:
        """从文件名模式加载（fallback）。"""
        for path in sorted(self._template_dir.glob("*.png")):
            stem = path.stem
            label = self._stem_to_label(stem)
            if label is None:
                continue
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                self._templates.setdefault(label, []).append(
                    cv2.resize(img, _TEMPLATE_SIZE)
                )

    @staticmethod
    def _stem_to_label(stem: str) -> str | None:
        """从文件名提取 label。plus_0 → '+', 1_0 → '1', 5 → '5'。"""
        if "_" in stem:
            base = stem.rsplit("_", 1)[0]
        else:
            base = stem
        return GainTemplateMatcher._normalize_label(base)

    @staticmethod
    def _normalize_label(label: str) -> str | None:
        """标准化 label。plus → '+', 数字 → 数字。"""
        if label in ("plus", "+"):
            return "+"
        if label.isdigit() and len(label) == 1:
            return label
        return None

    def match_digit(self, crop: np.ndarray) -> dict[str, Any]:
        """匹配单个数字裁剪图。增加 margin 检查。"""
        if not self.available:
            return _unavailable_result()

        if len(crop.shape) == 3:
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        else:
            gray = crop.copy()

        resized = cv2.resize(gray, _TEMPLATE_SIZE)

        all_scores = {}
        for label, templates in self._templates.items():
            best_for_label = 0.0
            for tmpl in templates:
                result = cv2.matchTemplate(resized, tmpl, cv2.TM_CCOEFF_NORMED)
                score = float(result[0, 0])
                if score > best_for_label:
                    best_for_label = score
            all_scores[label] = round(best_for_label, 4)

        if not all_scores:
            return _unavailable_result()

        # 排序取 top2
        sorted_scores = sorted(all_scores.items(), key=lambda x: -x[1])
        best_label = sorted_scores[0][0]
        best_score = sorted_scores[0][1]
        second_label = sorted_scores[1][0] if len(sorted_scores) > 1 else None
        second_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0
        margin = round(best_score - second_score, 4)

        # reliable 条件：分数够高 且 margin 足够（防止 1/2/3 混淆）
        reliable = best_score >= _MATCH_THRESHOLD and margin >= _MARGIN_THRESHOLD

        return {
            "best_digit": best_label,
            "score": round(best_score, 4),
            "second_digit": second_label,
            "second_score": round(second_score, 4),
            "margin": margin,
            "all_scores": all_scores,
            "reliable": reliable,
        }

    @staticmethod
    def _tight_crop(gray: np.ndarray) -> np.ndarray:
        """裁剪到图像中实际非背景像素区域，加小 padding。"""
        # 阈值分离前景（暗像素 = 数字笔画）
        _, thresh = cv2.threshold(gray, 140, 255, cv2.THRESH_BINARY_INV)
        ys, xs = np.where(thresh > 0)
        if len(ys) < 5:
            return gray  # 太少像素，返回原图
        pad = 3
        y0 = max(0, ys.min() - pad)
        y1 = min(gray.shape[0], ys.max() + pad + 1)
        x0 = max(0, xs.min() - pad)
        x1 = min(gray.shape[1], xs.max() + pad + 1)
        return gray[y0:y1, x0:x1]

    def match_group(self, group_crop: np.ndarray, n_components: int) -> dict[str, Any]:
        """匹配一个 +N 数字组。"""
        if not self.available:
            return {"value": None, "digits": [], "scores": [], "reliable": False}

        if len(group_crop.shape) == 3:
            gray = cv2.cvtColor(group_crop, cv2.COLOR_BGR2GRAY)
        else:
            gray = group_crop.copy()

        n_digits = max(1, n_components - 1)
        h, w = gray.shape

        if n_digits == 1:
            digit_crop = gray[:, w // 3:]
            result = self.match_digit(digit_crop)
            if result["reliable"]:
                return {
                    "value": _try_int(result["best_digit"]),
                    "digits": [result["best_digit"]],
                    "scores": [result["score"]],
                    "reliable": True,
                }
        elif n_digits == 2:
            digit_area = gray[:, w // 4:]
            dw = digit_area.shape[1]
            r1 = self.match_digit(digit_area[:, :dw // 2])
            r2 = self.match_digit(digit_area[:, dw // 2:])
            if r1["reliable"] and r2["reliable"]:
                val_str = f"{r1['best_digit']}{r2['best_digit']}"
                return {
                    "value": _try_int(val_str),
                    "digits": [r1["best_digit"], r2["best_digit"]],
                    "scores": [r1["score"], r2["score"]],
                    "reliable": True,
                }

        result = self.match_digit(gray)
        return {
            "value": _try_int(result["best_digit"]) if result["reliable"] else None,
            "digits": [result["best_digit"]] if result["reliable"] else [],
            "scores": [result["score"]] if result["reliable"] else [],
            "reliable": False,
        }

    def match_value_crop(
        self,
        crop: np.ndarray,
        *,
        allowed_values: range | list[int] | tuple[int, ...],
    ) -> dict[str, Any]:
        """Match a whole `+N` crop against synthesized value templates."""
        if not self.available:
            return {
                "value": None,
                "score": 0.0,
                "second_value": None,
                "second_score": 0.0,
                "reliable": False,
            }

        values = [int(value) for value in allowed_values]
        if not values:
            return {
                "value": None,
                "score": 0.0,
                "second_value": None,
                "second_score": 0.0,
                "reliable": False,
            }

        processed = _preprocess_value_crop(crop)
        scored: list[tuple[int, float]] = []
        for value in values:
            template = self._compose_value_template(value)
            score = float(cv2.matchTemplate(processed, template, cv2.TM_CCOEFF_NORMED)[0, 0])
            scored.append((value, score))
        scored.sort(key=lambda item: item[1], reverse=True)

        best_value, best_score = scored[0]
        second_value, second_score = scored[1] if len(scored) > 1 else (None, 0.0)
        reliable = best_score >= _VALUE_MATCH_THRESHOLD and (best_score - second_score) >= _MARGIN_THRESHOLD
        return {
            "value": best_value,
            "score": round(best_score, 4),
            "second_value": second_value,
            "second_score": round(second_score, 4),
            "reliable": reliable,
        }

    def _compose_value_template(self, value: int) -> np.ndarray:
        symbols = ["+"] + list(str(int(value)))
        parts = []
        for symbol in symbols:
            templates = self._templates.get(symbol)
            if not templates:
                raise ValueError(f"missing_template:{symbol}")
            parts.append(_tight_crop(templates[0]))

        target_h = max(part.shape[0] for part in parts)
        rows = []
        for part in parts:
            top = (target_h - part.shape[0]) // 2
            bottom = target_h - part.shape[0] - top
            rows.append(
                cv2.copyMakeBorder(
                    part,
                    top,
                    bottom,
                    0,
                    0,
                    cv2.BORDER_CONSTANT,
                    value=255,
                )
            )

        canvas = rows[0]
        for part in rows[1:]:
            spacer = np.full((target_h, _VALUE_TEMPLATE_PADDING), 255, dtype=np.uint8)
            canvas = np.concatenate([canvas, spacer, part], axis=1)

        canvas = cv2.copyMakeBorder(canvas, 6, 6, 6, 6, cv2.BORDER_CONSTANT, value=255)
        return cv2.resize(canvas, _VALUE_TEMPLATE_SIZE, interpolation=cv2.INTER_NEAREST)

    def match_components(self, component_crops: list[np.ndarray]) -> dict[str, Any]:
        """匹配已分割的单字符 crop 列表。

        规则：
        - 第一个 token 必须是 "+"（否则 reliable=False）
        - 后续 token 必须都是数字
        - 若出现未知/低分/缺模板，reliable=False
        - 没有 8 模板时，8 的分数会低 → reliable=False

        Args:
            component_crops: 按 x 位置排序的单字符 BGR/灰度裁剪列表。

        Returns:
            {
                "value": int | None,
                "tokens": ["+", "1", "3"],
                "scores": [0.98, 0.95, 0.93],
                "reliable": bool,
                "reason": str,
            }
        """
        if not self.available:
            return _components_unavailable("模板未加载")
        if not component_crops:
            return _components_unavailable("无组件输入")

        tokens = []
        scores = []
        component_matches = []
        reasons = []
        unreliable_component = False

        for i, crop in enumerate(component_crops):
            result = self.match_digit(crop)
            component_matches.append(result)
            best = result["best_digit"]
            score = result["score"]

            if best is None or not result["reliable"]:
                reasons.append(f"comp{i}={best}(score={score:.2f})")
                tokens.append(best or "?")
                scores.append(score)
                unreliable_component = True
                continue

            tokens.append(best)
            scores.append(score)

        # 校验 token 结构
        reliable = not unreliable_component

        # 第一个必须是 "+"
        if tokens and tokens[0] != "+":
            reasons.append(f"首token={tokens[0]}(非+)")
            reliable = False

        # 后续必须是数字
        for i, tok in enumerate(tokens[1:], 1):
            if not tok.isdigit():
                reasons.append(f"comp{i}={tok}(非数字)")
                reliable = False

        # 任何低分组件
        for i, sc in enumerate(scores):
            if sc < _MATCH_THRESHOLD:
                reasons.append(f"comp{i}分数={sc:.2f}<{_MATCH_THRESHOLD}")
                reliable = False

        # 拼接数值
        digit_str = "".join(t for t in tokens[1:] if t.isdigit())
        value = _try_int(digit_str) if digit_str else None

        return {
            "value": value,
            "tokens": tokens,
            "scores": [round(s, 4) for s in scores],
            "digit_scores": [round(score, 4) for tok, score in zip(tokens[1:], scores[1:]) if tok.isdigit()],
            "plus_score": round(scores[0], 4) if scores else 0.0,
            "component_matches": component_matches,
            "reliable": reliable,
            "reason": "; ".join(reasons) if reasons else "",
        }


def _preprocess_value_crop(crop: np.ndarray) -> np.ndarray:
    if len(crop.shape) == 3:
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = (
            ((hsv[:, :, 0] >= 8) & (hsv[:, :, 0] <= 38))
            & (hsv[:, :, 1] > 80)
            & (hsv[:, :, 2] > 120)
        )
        normalized = np.full(mask.shape, 255, dtype=np.uint8)
        normalized[mask] = 0
    else:
        normalized = crop.copy()
    normalized = _tight_crop(normalized)
    normalized = cv2.copyMakeBorder(normalized, 6, 6, 6, 6, cv2.BORDER_CONSTANT, value=255)
    return cv2.resize(normalized, _VALUE_TEMPLATE_SIZE, interpolation=cv2.INTER_NEAREST)


def _components_unavailable(reason: str) -> dict[str, Any]:
    return {
        "value": None,
        "tokens": [],
        "scores": [],
        "reliable": False,
        "reason": reason,
    }


def _unavailable_result() -> dict[str, Any]:
    return {
        "best_digit": None,
        "score": 0.0,
        "all_scores": {},
        "reliable": False,
    }


def _try_int(s: str) -> int | None:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None
