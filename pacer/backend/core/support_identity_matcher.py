from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = ROOT / "config" / "current_support_deck.json"
DEFAULT_TODO_MANIFEST_PATH = ROOT / "config" / "current_support_deck.todo.json"
SUPPORT_DECK_SCHEMA_VERSION = "uma_current_support_deck.v1"
REQUIRED_SUPPORT_CARD_FIELDS = (
    "card_id",
    "card_name",
    "type",
    "avatar_template_path",
    "stat_bonuses",
    "event_names",
    "skill_hints",
)

SUPPORT_CARD_FIELD_ALIASES = {
    "card_name": ("card_name", "display_name", "name"),
    "type": ("type", "card_type"),
    "character_name": ("character_name", "character_display_name"),
    "stat_bonuses": ("stat_bonuses", "training_bonus_summary", "support_bonus_summary"),
    "event_names": ("event_names", "event_hooks", "event_tags"),
    "skill_hints": ("skill_hints", "skill_hint_tags"),
    "evidence_sources": ("evidence_sources", "sources"),
    "data_links": ("data_links", "event_skill_links", "association_links"),
}


@dataclass
class SupportIdentityMatch:
    slot: int | None = None
    character_id: str = "unknown"
    character_name: str = "unknown"
    card_id: str | None = None
    display_name: str = ""
    card_type: str = "unknown"
    support_bonus_summary: dict[str, Any] | list[Any] | str | None = None
    event_hooks: list[str] | None = None
    skill_hint_tags: list[str] | None = None
    template_path: str | None = None
    confidence: float = 0.0
    source: str = "support_identity_matcher"
    reason: str = "identity_unavailable"
    matched_template_path: str | None = None
    identity_candidates: list[dict[str, Any]] | None = None
    evidence_sources: list[dict[str, Any]] | None = None
    data_links: dict[str, Any] | None = None
    event_skill_notes: str = ""
    manual_status: str = "unknown"
    entity_kind: str = "unknown_portrait"

    @property
    def status(self) -> str:
        if (
            self.entity_kind == "non_support_npc"
            and self.character_id != "unknown"
            and self.character_name != "unknown"
        ):
            return "known"
        if self.reason == "below_threshold":
            return "ambiguous"
        if (
            self.card_id
            and not self.card_id.startswith("cluster_")
            and self.card_type != "unknown"
            and self.display_name
            and not self.display_name.startswith("unknown_")
            and not self.display_name.startswith("待标注 ")
        ):
            return "known"
        return "unknown"

    @property
    def matched_card_id(self) -> str | None:
        return self.card_id if self.status == "known" and self.entity_kind == "support_card" else None

    @property
    def identity_state(self) -> str:
        if self.status == "known":
            return "known_identity"
        if self.manual_status == "needs_manual_label":
            return "needs_manual_label"
        return "unknown_identity"

    @staticmethod
    def _unknown_reason_for(*, status: str, identity_state: str, reason: str) -> str:
        if status == "known":
            return ""
        if identity_state == "needs_manual_label" and reason == "matched_template":
            return "needs_manual_label"
        return reason

    def to_dict(self) -> dict[str, Any]:
        bonus_summary = self.support_bonus_summary if self.support_bonus_summary is not None else {}
        evidence_sources = list(self.evidence_sources or [])
        data_links = self._normalized_data_links()
        unknown_reason = self._unknown_reason_for(
            status=self.status,
            identity_state=self.identity_state,
            reason=self.reason,
        )
        support_card = {
            "slot": self.slot,
            "card_id": self.card_id,
            "matched_card_id": self.matched_card_id,
            "character_id": self.character_id,
            "character_name": self.character_name,
            "display_name": self.display_name,
            "card_name": self.display_name,
            "type": self.card_type,
            "card_type": self.card_type,
            "entity_kind": self.entity_kind,
            "is_effective_support": self.entity_kind == "support_card" and self.status == "known",
            "confidence": self.confidence,
            "identity_confidence": self.confidence,
            "status": self.status,
            "identity_state": self.identity_state,
            "reason": self.reason,
            "unknown_reason": unknown_reason,
            "source": self.source,
            "evidence_sources": evidence_sources,
            "template_path": self.template_path,
            "avatar_template_path": self.template_path,
            "matched_template_path": self.matched_template_path,
            "support_bonus_summary": bonus_summary,
            "training_bonus_summary": bonus_summary,
            "stat_bonuses": bonus_summary,
            "event_hooks": list(self.event_hooks or []),
            "event_names": list(self.event_hooks or []),
            "skill_hint_tags": list(self.skill_hint_tags or []),
            "skill_hints": list(self.skill_hint_tags or []),
            "data_links": data_links,
            "event_skill_notes": self.event_skill_notes,
            "manual_status": self.manual_status,
            "needs_manual_label": self.identity_state == "needs_manual_label",
        }
        return {
            "status": self.status,
            "identity_state": self.identity_state,
            "slot": self.slot,
            "matched_card_id": self.matched_card_id,
            "character_id": self.character_id,
            "character_name": self.character_name,
            "card_id": self.card_id,
            "display_name": self.display_name,
            "card_name": self.display_name,
            "card_type": self.card_type,
            "entity_kind": self.entity_kind,
            "is_effective_support": self.entity_kind == "support_card" and self.status == "known",
            "type": self.card_type,
            "support_bonus_summary": bonus_summary,
            "training_bonus_summary": bonus_summary,
            "stat_bonuses": bonus_summary,
            "event_hooks": list(self.event_hooks or []),
            "event_names": list(self.event_hooks or []),
            "skill_hint_tags": list(self.skill_hint_tags or []),
            "skill_hints": list(self.skill_hint_tags or []),
            "template_path": self.template_path,
            "avatar_template_path": self.template_path,
            "confidence": self.confidence,
            "identity_confidence": self.confidence,
            "source": self.source,
            "reason": self.reason,
            "unknown_reason": unknown_reason,
            "matched_template_path": self.matched_template_path,
            "identity_candidates": list(self.identity_candidates or []),
            "evidence_sources": evidence_sources,
            "support_card": support_card,
            "data_links": data_links,
            "event_skill_notes": self.event_skill_notes,
            "manual_status": self.manual_status,
            "needs_manual_label": self.identity_state == "needs_manual_label",
        }

    def _normalized_data_links(self) -> dict[str, list[str]]:
        if not isinstance(self.data_links, dict):
            return {"events": [], "skills": [], "hints": []}
        return {
            "events": [str(item) for item in self.data_links.get("events", []) if item is not None],
            "skills": [str(item) for item in self.data_links.get("skills", []) if item is not None],
            "hints": [str(item) for item in self.data_links.get("hints", []) if item is not None],
        }


@dataclass
class _SupportDeckCard:
    slot: int | None
    card_id: str
    character_id: str
    character_name: str
    display_name: str
    card_type: str
    support_bonus_summary: dict[str, Any] | list[Any] | str
    event_hooks: list[str]
    skill_hint_tags: list[str]
    template_path: Path | None
    template_paths: list[Path]
    evidence_sources: list[dict[str, Any]]
    data_links: dict[str, list[str]]
    notes: str
    event_skill_notes: str
    manual_status: str
    entity_kind: str


@dataclass
class _PreparedTemplate:
    card: _SupportDeckCard
    template_path: Path
    gray: np.ndarray
    edges: np.ndarray
    hist: np.ndarray
    dhash: np.ndarray


class SupportIdentityMatcher:
    def __init__(
        self,
        manifest_path: str | Path | None = None,
        *,
        min_match_score: float = 0.78,
        template_size: int = 64,
    ):
        self._manifest_path = self._resolve_manifest_path(manifest_path)
        self._min_match_score = float(min_match_score)
        self._template_size = int(template_size)
        self._cards: list[_SupportDeckCard] = []
        self._templates: list[_PreparedTemplate] = []
        self._missing_templates: list[dict[str, str]] = []
        self._load_reason = "manifest_missing"
        self._load_manifest()

    def match_slot(
        self,
        *,
        image: np.ndarray,
        bbox: list[int],
        slot_index: int,
        slot_payload: dict[str, Any] | None = None,
    ) -> SupportIdentityMatch:
        _ = slot_index, slot_payload
        if image is None or image.size == 0:
            return SupportIdentityMatch(reason="empty_image")
        if not self._templates:
            return SupportIdentityMatch(reason=self._load_reason, manual_status="needs_manual_label")

        crop = self._crop_bbox(image, bbox)
        if crop is None:
            return SupportIdentityMatch(reason="invalid_bbox")
        prepared = self._prepare_avatar(crop)
        if prepared is None:
            return SupportIdentityMatch(reason="invalid_crop")

        candidate_gray, candidate_hist, candidate_hash = prepared
        best_template: _PreparedTemplate | None = None
        best_score = -1.0
        scored_candidates: list[tuple[float, _PreparedTemplate]] = []
        for template in self._templates:
            score = self._score_template(
                candidate_gray=candidate_gray,
                candidate_hist=candidate_hist,
                candidate_hash=candidate_hash,
                template=template,
            )
            scored_candidates.append((score, template))
            if score > best_score:
                best_score = score
                best_template = template

        # Normalized 1440 captures can shift the Hough avatar box by a few
        # pixels from the live 720 template. Preserve the threshold and only
        # search nearby same-size crops after the original box abstains.
        if best_score < self._min_match_score:
            translated_prepared = self._prepare_translated_avatar_crops(image, bbox)
            if translated_prepared:
                rescored_candidates: list[tuple[float, _PreparedTemplate]] = []
                for original_score, template in scored_candidates:
                    translated_score = max(
                        (
                            self._score_template(
                                candidate_gray=gray,
                                candidate_hist=hist,
                                candidate_hash=dhash,
                                template=template,
                            )
                            for gray, hist, dhash in translated_prepared
                        ),
                        default=original_score,
                    )
                    score = max(original_score, translated_score)
                    rescored_candidates.append((score, template))
                    if score > best_score:
                        best_score = score
                        best_template = template
                scored_candidates = rescored_candidates

        confidence = round(max(best_score, 0.0), 3)
        scored_candidates.sort(key=lambda item: item[0], reverse=True)
        if best_template is None:
            return SupportIdentityMatch(confidence=confidence, reason="no_templates_loaded")
        candidate_reason = "matched_template" if best_score >= self._min_match_score else "below_threshold"
        identity_candidates = [
            self._candidate_payload(template=template, score=score, reason=candidate_reason)
            for score, template in scored_candidates[:5]
        ]
        if best_score < self._min_match_score:
            return SupportIdentityMatch(
                confidence=confidence,
                reason="below_threshold",
                identity_candidates=identity_candidates,
            )

        card = best_template.card
        return SupportIdentityMatch(
            slot=card.slot,
            character_id=card.character_id,
            character_name=card.character_name,
            card_id=card.card_id,
            display_name=card.display_name,
            card_type=card.card_type,
            support_bonus_summary=card.support_bonus_summary,
            event_hooks=card.event_hooks,
            skill_hint_tags=card.skill_hint_tags,
            template_path=str(card.template_path) if card.template_path is not None else None,
            confidence=confidence,
            reason="matched_template",
            matched_template_path=str(best_template.template_path),
            identity_candidates=identity_candidates,
            evidence_sources=self._build_evidence_sources(
                card=card,
                template=best_template,
                confidence=confidence,
            ),
            data_links=card.data_links,
            event_skill_notes=card.event_skill_notes,
            manual_status=card.manual_status,
            entity_kind=card.entity_kind,
        )

    def compare_avatar_images(self, left: np.ndarray, right: np.ndarray) -> float:
        left_prepared = self._prepare_avatar(left)
        right_prepared = self._prepare_avatar(right)
        if left_prepared is None or right_prepared is None:
            return 0.0
        left_gray, left_hist, left_hash = left_prepared
        right_gray, right_hist, right_hash = right_prepared
        template = _PreparedTemplate(
                card=_SupportDeckCard(
                slot=None,
                card_id="",
                character_id="unknown",
                character_name="unknown",
                display_name="",
                card_type="unknown",
                support_bonus_summary={},
                event_hooks=[],
                skill_hint_tags=[],
                template_path=None,
                template_paths=[],
                evidence_sources=[],
                data_links={"events": [], "skills": [], "hints": []},
                notes="",
                event_skill_notes="",
                manual_status="unknown",
                entity_kind="unknown_portrait",
            ),
            template_path=Path(),
            gray=right_gray,
            edges=cv2.Canny(right_gray, 80, 160),
            hist=right_hist,
            dhash=right_hash,
        )
        return round(
            self._score_template(
                candidate_gray=left_gray,
                candidate_hist=left_hist,
                candidate_hash=left_hash,
                template=template,
            ),
            3,
        )

    def find_scaled_template_matches(
        self,
        image: np.ndarray,
        *,
        minimum_score: float = 0.82,
        search_bbox: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Locate verified live-avatar templates at the screenshot's native scale.

        This returns identity evidence only. Callers must still associate every
        match with an independently detected avatar geometry before creating a
        visible slot.
        """
        if image is None or image.size == 0 or not self._templates:
            return []
        threshold = max(float(minimum_score), 0.82)
        search_x1, search_y1 = 0, 0
        search_image = image
        if search_bbox is not None:
            crop = self._crop_bbox(image, search_bbox)
            if crop is None:
                return []
            search_x1, search_y1 = int(search_bbox[0]), int(search_bbox[1])
            search_image = crop

        search_gray = cv2.cvtColor(search_image, cv2.COLOR_BGR2GRAY)
        native_scale = image.shape[1] / 720.0
        scale_factors = (0.65, 0.8, 0.9, 1.0, 1.1, 1.2)
        best_by_entity: dict[tuple[str, str], dict[str, Any]] = {}
        for template in self._templates:
            template_image = cv2.imread(str(template.template_path))
            if template_image is None:
                continue
            template_gray = cv2.cvtColor(template_image, cv2.COLOR_BGR2GRAY)
            base_height, base_width = template_gray.shape[:2]
            for factor in scale_factors:
                scale = native_scale * factor
                width = max(16, int(round(base_width * scale)))
                height = max(16, int(round(base_height * scale)))
                if width > search_gray.shape[1] or height > search_gray.shape[0]:
                    continue
                interpolation = cv2.INTER_AREA if scale <= 1.0 else cv2.INTER_CUBIC
                scaled_template = cv2.resize(template_gray, (width, height), interpolation=interpolation)
                response = cv2.matchTemplate(search_gray, scaled_template, cv2.TM_CCOEFF_NORMED)
                _, score, _, location = cv2.minMaxLoc(response)
                if score < threshold:
                    continue
                entity_key = (template.card.entity_kind, template.card.card_id)
                candidate = {
                    "bbox": [
                        int(search_x1 + location[0]),
                        int(search_y1 + location[1]),
                        int(search_x1 + location[0] + width),
                        int(search_y1 + location[1] + height),
                    ],
                    "identity": self._match_from_prepared_template(template, score),
                    "template_match_confidence": round(float(score), 3),
                    "template_path": str(template.template_path),
                    "template_scale": round(float(scale), 3),
                    "source": "scaled_template_location",
                }
                previous = best_by_entity.get(entity_key)
                if previous is None or candidate["template_match_confidence"] > previous["template_match_confidence"]:
                    best_by_entity[entity_key] = candidate

        return sorted(
            self._spatial_nms_template_matches(list(best_by_entity.values())),
            key=lambda item: (item["bbox"][1], item["bbox"][0]),
        )

    def _match_from_prepared_template(
        self,
        template: _PreparedTemplate,
        score: float,
    ) -> SupportIdentityMatch:
        card = template.card
        confidence = round(max(float(score), 0.0), 3)
        return SupportIdentityMatch(
            slot=card.slot,
            character_id=card.character_id,
            character_name=card.character_name,
            card_id=card.card_id,
            display_name=card.display_name,
            card_type=card.card_type,
            support_bonus_summary=card.support_bonus_summary,
            event_hooks=card.event_hooks,
            skill_hint_tags=card.skill_hint_tags,
            template_path=str(card.template_path) if card.template_path is not None else None,
            confidence=confidence,
            source="scaled_template_location",
            reason="matched_template",
            matched_template_path=str(template.template_path),
            identity_candidates=[
                self._candidate_payload(template=template, score=confidence, reason="matched_template")
            ],
            evidence_sources=self._build_evidence_sources(
                card=card,
                template=template,
                confidence=confidence,
            ),
            data_links=card.data_links,
            event_skill_notes=card.event_skill_notes,
            manual_status=card.manual_status,
            entity_kind=card.entity_kind,
        )

    def find_exact_template_matches(
        self,
        image: np.ndarray,
        *,
        minimum_score: float = 0.98,
        search_bbox: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Find only near-pixel-exact verified templates in a full training image."""
        if image is None or image.size == 0 or not self._templates:
            return []
        threshold = max(float(minimum_score), 0.98)
        search_x1, search_y1 = 0, 0
        search_image = image
        if search_bbox is not None:
            crop = self._crop_bbox(image, search_bbox)
            if crop is None:
                return []
            search_x1, search_y1 = int(search_bbox[0]), int(search_bbox[1])
            search_image = crop
        image_gray = cv2.cvtColor(search_image, cv2.COLOR_BGR2GRAY)
        raw_matches: list[dict[str, Any]] = []
        for template in self._templates:
            template_image = cv2.imread(str(template.template_path))
            if template_image is None:
                continue
            template_height, template_width = template_image.shape[:2]
            if template_height > search_image.shape[0] or template_width > search_image.shape[1]:
                continue
            template_gray = cv2.cvtColor(template_image, cv2.COLOR_BGR2GRAY)
            response = cv2.matchTemplate(image_gray, template_gray, cv2.TM_CCOEFF_NORMED)
            while True:
                _, score, _, location = cv2.minMaxLoc(response)
                if score < threshold:
                    break
                bbox = [
                    int(search_x1 + location[0]),
                    int(search_y1 + location[1]),
                    int(search_x1 + location[0] + template_width),
                    int(search_y1 + location[1] + template_height),
                ]
                identity = self.match_slot(image=image, bbox=bbox, slot_index=0)
                if identity.status == "known":
                    raw_matches.append(
                        {
                            "bbox": bbox,
                            "identity": identity,
                            "template_match_confidence": round(float(score), 3),
                            "template_path": str(template.template_path),
                        }
                    )

                x1 = max(0, int(location[0] - template_width // 2))
                y1 = max(0, int(location[1] - template_height // 2))
                x2 = min(response.shape[1], int(location[0] + template_width // 2 + 1))
                y2 = min(response.shape[0], int(location[1] + template_height // 2 + 1))
                response[y1:y2, x1:x2] = -1.0
        return sorted(
            self._spatial_nms_template_matches(raw_matches),
            key=lambda item: (item["bbox"][1], item["bbox"][0]),
        )

    @staticmethod
    def _spatial_nms_template_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        retained: list[dict[str, Any]] = []
        for match in sorted(matches, key=lambda item: item["template_match_confidence"], reverse=True):
            if any(SupportIdentityMatcher._template_match_boxes_overlap(match["bbox"], kept["bbox"]) for kept in retained):
                continue
            retained.append(match)
        return retained

    @staticmethod
    def _template_match_boxes_overlap(left: list[int], right: list[int]) -> bool:
        left_area = max(0, left[2] - left[0]) * max(0, left[3] - left[1])
        right_area = max(0, right[2] - right[0]) * max(0, right[3] - right[1])
        intersection_width = max(0, min(left[2], right[2]) - max(left[0], right[0]))
        intersection_height = max(0, min(left[3], right[3]) - max(left[1], right[1]))
        intersection_area = intersection_width * intersection_height
        union_area = left_area + right_area - intersection_area
        if union_area and (intersection_area / union_area) >= 0.5:
            return True
        left_center = ((left[0] + left[2]) / 2.0, (left[1] + left[3]) / 2.0)
        right_center = ((right[0] + right[2]) / 2.0, (right[1] + right[3]) / 2.0)
        return (
            abs(left_center[0] - right_center[0]) <= min(left[2] - left[0], right[2] - right[0]) * 0.2
            and abs(left_center[1] - right_center[1]) <= min(left[3] - left[1], right[3] - right[1]) * 0.2
        )

    def manifest_status(self) -> dict[str, Any]:
        manifest_path = str(self._manifest_path) if self._manifest_path is not None else None
        unknown_character_count = sum(1 for card in self._cards if card.character_id == "unknown")
        unlabeled_card_count = sum(
            1
            for card in self._cards
            if (
                card.character_id == "unknown"
                or card.display_name == ""
                or card.display_name.startswith("unknown_")
                or card.display_name.startswith("待标注 ")
                or card.card_type == "unknown"
            )
        )
        return {
            "manifest_path": manifest_path,
            "load_reason": self._load_reason,
            "card_count": len(self._cards),
            "loaded_template_count": len(self._templates),
            "missing_template_count": len(self._missing_templates),
            "missing_templates": list(self._missing_templates),
            "unknown_character_count": unknown_character_count,
            "unlabeled_card_count": unlabeled_card_count,
        }

    @staticmethod
    def _resolve_manifest_path(manifest_path: str | Path | None) -> Path | None:
        if manifest_path is None:
            if DEFAULT_MANIFEST_PATH.is_file():
                return DEFAULT_MANIFEST_PATH
            if DEFAULT_TODO_MANIFEST_PATH.is_file():
                return DEFAULT_TODO_MANIFEST_PATH
            return None
        return Path(manifest_path)

    def _load_manifest(self) -> None:
        if self._manifest_path is None or not self._manifest_path.is_file():
            self._load_reason = "manifest_missing"
            return

        try:
            payload = json.loads(self._manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("failed to read support deck manifest %s: %s", self._manifest_path, exc)
            self._load_reason = "manifest_invalid"
            return

        cards = payload.get("cards", [])
        non_support_entities = payload.get("non_support_entities", [])
        if not isinstance(cards, list):
            cards = []
        if not isinstance(non_support_entities, list):
            non_support_entities = []
        entries = [(entry, "support_card") for entry in cards] + [
            (entry, "non_support_npc") for entry in non_support_entities
        ]
        if not entries:
            self._load_reason = "no_templates_loaded"
            return

        manifest_dir = self._manifest_path.parent
        for entry, default_entity_kind in entries:
            if not isinstance(entry, dict):
                continue
            template_paths = self._resolve_template_paths(manifest_dir, entry)
            template_path = template_paths[0] if template_paths else None
            card_id = str(entry.get("card_id") or entry.get("entity_id") or "")
            display_name = self._display_name_or_unknown_label(
                card_id=card_id,
                display_name=str(self._first_present(entry, "card_name") or ""),
            )
            card_type = str(self._first_present(entry, "type") or "unknown")
            card = _SupportDeckCard(
                slot=self._normalize_optional_int(entry.get("slot")),
                card_id=card_id,
                character_id=str(entry.get("character_id") or "unknown"),
                character_name=str(self._first_present(entry, "character_name") or "unknown"),
                display_name=display_name,
                card_type=card_type,
                support_bonus_summary=self._normalize_support_bonus_summary(
                    self._first_present(entry, "stat_bonuses")
                ),
                event_hooks=self._normalize_string_list(
                    self._first_present(entry, "event_names")
                ),
                skill_hint_tags=self._normalize_string_list(self._first_present(entry, "skill_hints")),
                template_path=template_path,
                template_paths=template_paths,
                evidence_sources=self._normalize_evidence_sources(
                    self._first_present(entry, "evidence_sources")
                ),
                data_links=self._normalize_data_links(self._first_present(entry, "data_links")),
                notes=str(entry.get("notes") or ""),
                event_skill_notes=str(entry.get("event_skill_notes") or entry.get("event_notes") or ""),
                manual_status=self._normalize_manual_status(entry, card_id, display_name, card_type),
                entity_kind=str(entry.get("entity_kind") or default_entity_kind),
            )
            self._cards.append(card)

        for card in self._cards:
            for template_path in card.template_paths:
                prepared = self._load_template(card, template_path)
                if prepared is not None:
                    self._templates.append(prepared)
                else:
                    self._missing_templates.append(
                        {
                            "card_id": card.card_id,
                            "character_id": card.character_id,
                            "display_name": card.display_name,
                            "path": str(template_path),
                        }
                    )

        self._load_reason = "ready" if self._templates else "no_templates_loaded"

    def _load_template(self, card: _SupportDeckCard, template_path: Path) -> _PreparedTemplate | None:
        if not template_path.is_file():
            return None
        image = cv2.imread(str(template_path))
        if image is None:
            return None
        prepared = self._prepare_avatar(image)
        if prepared is None:
            return None
        gray, hist, dhash = prepared
        edges = cv2.Canny(gray, 80, 160)
        return _PreparedTemplate(
            card=card,
            template_path=template_path.resolve(),
            gray=gray,
            edges=edges,
            hist=hist,
            dhash=dhash,
        )

    def _prepare_avatar(self, image: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        if image is None or image.size == 0:
            return None
        if image.ndim != 3 or image.shape[2] != 3:
            return None

        size = self._template_size
        resized = cv2.resize(image, (size, size), interpolation=cv2.INTER_AREA)
        mask = np.zeros((size, size), dtype=np.uint8)
        radius = int(size * 0.46)
        cv2.circle(mask, (size // 2, size // 2), radius, 255, thickness=-1)

        masked = resized.copy()
        masked[mask == 0] = 255
        gray = cv2.cvtColor(masked, cv2.COLOR_BGR2GRAY)

        hsv = cv2.cvtColor(masked, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], mask, [16, 16], [0, 180, 0, 256])
        hist = cv2.normalize(hist, hist).flatten().astype(np.float32)
        dhash = self._compute_dhash(gray, mask)
        return gray, hist, dhash

    @staticmethod
    def _compute_dhash(gray: np.ndarray, mask: np.ndarray) -> np.ndarray:
        resized = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
        mask_small = cv2.resize(mask, (9, 8), interpolation=cv2.INTER_NEAREST) > 0
        diff = resized[:, 1:] > resized[:, :-1]
        usable = mask_small[:, 1:] & mask_small[:, :-1]
        hashed = np.zeros((8, 8), dtype=np.uint8)
        hashed[usable] = diff[usable].astype(np.uint8)
        return hashed.flatten()

    def _score_template(
        self,
        *,
        candidate_gray: np.ndarray,
        candidate_hist: np.ndarray,
        candidate_hash: np.ndarray,
        template: _PreparedTemplate,
    ) -> float:
        template_score = float(
            cv2.matchTemplate(candidate_gray, template.gray, cv2.TM_CCOEFF_NORMED)[0][0]
        )
        template_score = max(0.0, min(1.0, template_score))
        candidate_edges = cv2.Canny(candidate_gray, 80, 160)
        edge_score = float(
            cv2.matchTemplate(candidate_edges, template.edges, cv2.TM_CCOEFF_NORMED)[0][0]
        )
        edge_score = max(0.0, min(1.0, edge_score))

        hist_corr = float(cv2.compareHist(candidate_hist, template.hist, cv2.HISTCMP_CORREL))
        hist_score = max(0.0, min(1.0, (hist_corr + 1.0) / 2.0))

        hash_distance = float(np.count_nonzero(candidate_hash != template.dhash))
        hash_score = 1.0 - (hash_distance / max(float(candidate_hash.size), 1.0))
        hash_score = max(0.0, min(1.0, hash_score))

        weighted_sum = (
            (edge_score * 0.40)
            + (template_score * 0.30)
            + (hist_score * 0.20)
            + (hash_score * 0.10)
        )
        return weighted_sum * edge_score

    @staticmethod
    def _candidate_payload(
        *,
        template: _PreparedTemplate,
        score: float,
        reason: str,
    ) -> dict[str, Any]:
        card = template.card
        is_known = (
            card.card_id
            and not card.card_id.startswith("cluster_")
            and card.card_type != "unknown"
            and card.display_name
            and not card.display_name.startswith("unknown_")
            and not card.display_name.startswith("待标注 ")
        )
        status = "known" if is_known and reason == "matched_template" else "unknown"
        if status == "known":
            identity_state = "known_identity"
        elif card.manual_status == "needs_manual_label":
            identity_state = "needs_manual_label"
        else:
            identity_state = "unknown_identity"
        confidence = round(max(float(score), 0.0), 3)
        unknown_reason = SupportIdentityMatch._unknown_reason_for(
            status=status,
            identity_state=identity_state,
            reason=reason,
        )
        matched_card_id = card.card_id if status == "known" else None
        data_links = dict(card.data_links)
        evidence_sources = list(card.evidence_sources)
        support_card = {
            "card_id": card.card_id,
            "slot": card.slot,
            "matched_card_id": matched_card_id,
            "character_id": card.character_id,
            "character_name": card.character_name,
            "display_name": card.display_name,
            "card_name": card.display_name,
            "type": card.card_type,
            "card_type": card.card_type,
            "confidence": confidence,
            "identity_confidence": confidence,
            "status": status,
            "identity_state": identity_state,
            "reason": reason,
            "unknown_reason": unknown_reason,
            "source": "support_identity_matcher",
            "evidence_sources": evidence_sources,
            "matched_template_path": str(template.template_path),
            "template_path": str(card.template_path) if card.template_path is not None else None,
            "avatar_template_path": str(card.template_path) if card.template_path is not None else None,
            "support_bonus_summary": card.support_bonus_summary,
            "training_bonus_summary": card.support_bonus_summary,
            "stat_bonuses": card.support_bonus_summary,
            "event_hooks": list(card.event_hooks),
            "event_names": list(card.event_hooks),
            "skill_hint_tags": list(card.skill_hint_tags),
            "skill_hints": list(card.skill_hint_tags),
            "data_links": data_links,
            "event_skill_notes": card.event_skill_notes,
            "manual_status": card.manual_status,
            "needs_manual_label": card.manual_status == "needs_manual_label",
        }
        return {
            "card_id": card.card_id,
            "slot": card.slot,
            "matched_card_id": matched_card_id,
            "character_id": card.character_id,
            "character_name": card.character_name,
            "display_name": card.display_name,
            "card_name": card.display_name,
            "type": card.card_type,
            "card_type": card.card_type,
            "confidence": confidence,
            "identity_confidence": confidence,
            "status": status,
            "identity_state": identity_state,
            "reason": reason,
            "unknown_reason": unknown_reason,
            "source": "support_identity_matcher",
            "evidence_sources": evidence_sources,
            "matched_template_path": str(template.template_path),
            "template_path": str(card.template_path) if card.template_path is not None else None,
            "avatar_template_path": str(card.template_path) if card.template_path is not None else None,
            "support_bonus_summary": card.support_bonus_summary,
            "training_bonus_summary": card.support_bonus_summary,
            "stat_bonuses": card.support_bonus_summary,
            "event_hooks": list(card.event_hooks),
            "event_names": list(card.event_hooks),
            "skill_hint_tags": list(card.skill_hint_tags),
            "skill_hints": list(card.skill_hint_tags),
            "support_card": support_card,
            "data_links": data_links,
            "event_skill_notes": card.event_skill_notes,
            "manual_status": card.manual_status,
            "needs_manual_label": card.manual_status == "needs_manual_label",
        }

    def _build_evidence_sources(
        self,
        *,
        card: _SupportDeckCard,
        template: _PreparedTemplate,
        confidence: float,
    ) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        if self._manifest_path is not None:
            sources.append(
                {
                    "source_type": "support_deck_manifest",
                    "path": str(self._manifest_path),
                }
            )
        sources.append(
            {
                "source_type": "avatar_template_match",
                "path": str(template.template_path),
                "confidence": confidence,
            }
        )
        sources.extend(card.evidence_sources)
        return self._dedupe_evidence_sources(sources)

    @staticmethod
    def _crop_bbox(image: np.ndarray, bbox: list[int]) -> np.ndarray | None:
        if not isinstance(bbox, list) or len(bbox) != 4:
            return None
        try:
            x1, y1, x2, y2 = [int(v) for v in bbox]
        except (TypeError, ValueError):
            return None
        x1 = max(0, min(image.shape[1], x1))
        x2 = max(0, min(image.shape[1], x2))
        y1 = max(0, min(image.shape[0], y1))
        y2 = max(0, min(image.shape[0], y2))
        if x2 <= x1 or y2 <= y1:
            return None
        return image[y1:y2, x1:x2]

    def _prepare_translated_avatar_crops(
        self,
        image: np.ndarray,
        bbox: list[int],
    ) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
        if not isinstance(bbox, list) or len(bbox) != 4:
            return []
        try:
            x1, y1, x2, y2 = [int(value) for value in bbox]
        except (TypeError, ValueError):
            return []
        width = x2 - x1
        height = y2 - y1
        if width <= 0 or height <= 0:
            return []

        prepared_crops: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
        for dy in range(-24, 25, 6):
            for dx in range(-24, 25, 6):
                if dx == 0 and dy == 0:
                    continue
                translated_bbox = [x1 + dx, y1 + dy, x2 + dx, y2 + dy]
                crop = self._crop_bbox(image, translated_bbox)
                if crop is None or crop.shape[:2] != (height, width):
                    continue
                prepared = self._prepare_avatar(crop)
                if prepared is not None:
                    prepared_crops.append(prepared)
        return prepared_crops

    @staticmethod
    def _resolve_template_paths(base_dir: Path, entry: dict[str, Any]) -> list[Path]:
        raw_paths: list[Any] = []
        raw_template_path = entry.get("avatar_template_path", entry.get("template_path"))
        if raw_template_path:
            raw_paths.append(raw_template_path)
        legacy_template_paths = entry.get("template_paths")
        if isinstance(legacy_template_paths, list):
            raw_paths.extend(legacy_template_paths)
        resolved: list[Path] = []
        seen: set[Path] = set()
        for raw_path in raw_paths:
            if not raw_path:
                continue
            path = Path(str(raw_path))
            if not path.is_absolute():
                path = (base_dir / path).resolve()
            if path in seen:
                continue
            seen.add(path)
            resolved.append(path)
        return resolved

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if item is not None]

    @staticmethod
    def _normalize_evidence_sources(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        sources: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            normalized = {str(key): item[key] for key in item}
            if "source_type" not in normalized:
                normalized["source_type"] = "external"
            sources.append(normalized)
        return SupportIdentityMatcher._dedupe_evidence_sources(sources)

    @staticmethod
    def _dedupe_evidence_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in sources:
            key = json.dumps(source, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(dict(source))
        return deduped

    @staticmethod
    def _normalize_data_links(value: Any) -> dict[str, list[str]]:
        if not isinstance(value, dict):
            return {"events": [], "skills": [], "hints": []}
        return {
            "events": SupportIdentityMatcher._normalize_string_list(value.get("events")),
            "skills": SupportIdentityMatcher._normalize_string_list(value.get("skills")),
            "hints": SupportIdentityMatcher._normalize_string_list(value.get("hints")),
        }

    @staticmethod
    def _normalize_support_bonus_summary(value: Any) -> dict[str, Any] | list[Any] | str:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, list):
            return list(value)
        if isinstance(value, str):
            return value
        return {}

    @staticmethod
    def _normalize_optional_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_manual_status(
        entry: dict[str, Any],
        card_id: str,
        display_name: str,
        card_type: str,
    ) -> str:
        raw_status = str(entry.get("manual_status") or "").strip()
        if raw_status:
            return raw_status
        if (
            card_id.startswith("cluster_")
            or card_type == "unknown"
            or not display_name
            or display_name.startswith("unknown_")
            or display_name.startswith("待标注 ")
        ):
            return "needs_manual_label"
        return "verified"

    @staticmethod
    def _first_present(entry: dict[str, Any], canonical_field: str) -> Any:
        for field in SUPPORT_CARD_FIELD_ALIASES.get(canonical_field, (canonical_field,)):
            if field in entry:
                return entry.get(field)
        return None

    @staticmethod
    def _display_name_or_unknown_label(*, card_id: str, display_name: str) -> str:
        if display_name and not display_name.startswith("unknown_"):
            return display_name
        if card_id.startswith("cluster_"):
            return f"unknown_{card_id}"
        return display_name


def validate_support_deck_manifest_payload(
    payload: dict[str, Any],
    *,
    manifest_dir: str | Path | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    annotation_todos: list[dict[str, Any]] = []

    if payload.get("schema_version") != SUPPORT_DECK_SCHEMA_VERSION:
        errors.append("schema_version_invalid")

    cards = payload.get("cards")
    if not isinstance(cards, list):
        errors.append("cards_not_list")
        cards = []

    base_dir = Path(manifest_dir) if manifest_dir is not None else Path.cwd()
    card_template_paths: set[Path] = set()
    non_support_template_paths: set[Path] = set()

    def resolve_template_path(template_path: str) -> Path:
        path = Path(template_path)
        if not path.is_absolute():
            path = base_dir / path
        return path.resolve()

    def entry_template_paths(entry: dict[str, Any]) -> list[str]:
        paths: list[str] = []
        primary_path = entry.get("avatar_template_path")
        if isinstance(primary_path, str) and primary_path:
            paths.append(primary_path)
        extra_paths = entry.get("template_paths")
        if isinstance(extra_paths, list):
            paths.extend(path for path in extra_paths if isinstance(path, str) and path)
        return paths

    def is_npc_namespace(template_path: str) -> bool:
        return "npc_live" in {part.lower() for part in Path(template_path).parts}

    seen_card_ids: set[str] = set()
    for index, entry in enumerate(cards):
        prefix = f"cards[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}.not_object")
            continue

        for field in REQUIRED_SUPPORT_CARD_FIELDS:
            aliases = SUPPORT_CARD_FIELD_ALIASES.get(field, (field,))
            if not any(alias in entry for alias in aliases):
                errors.append(f"{prefix}.missing_{field}")

        card_id = str(entry.get("card_id") or "")
        if not card_id:
            errors.append(f"{prefix}.card_id_empty")
        elif card_id in seen_card_ids:
            errors.append(f"{prefix}.duplicate_card_id:{card_id}")
        seen_card_ids.add(card_id)

        template_path = entry.get("avatar_template_path")
        awaiting_avatar_capture = (
            entry.get("manual_status") == "planned_unverified"
            and entry.get("avatar_template_status") == "pending_live_sample"
        )
        if not isinstance(template_path, str) or not template_path:
            if awaiting_avatar_capture:
                warnings.append(f"{prefix}.avatar_template_pending_capture")
            else:
                errors.append(f"{prefix}.avatar_template_path_empty")
        else:
            resolved = resolve_template_path(template_path)
            if not resolved.is_file():
                errors.append(f"{prefix}.avatar_template_path_missing:{template_path}")

        for path in entry_template_paths(entry):
            resolved = resolve_template_path(path)
            if not resolved.is_file():
                errors.append(f"{prefix}.template_path_missing:{path}")
                continue
            card_template_paths.add(resolved)
            if entry.get("manual_status") == "verified" and is_npc_namespace(path):
                errors.append(f"{prefix}.verified_template_in_npc_namespace")

        stat_bonuses = SupportIdentityMatcher._first_present(entry, "stat_bonuses")
        event_names = SupportIdentityMatcher._first_present(entry, "event_names")
        skill_hints = SupportIdentityMatcher._first_present(entry, "skill_hints")
        if not isinstance(stat_bonuses, (dict, list, str)):
            errors.append(f"{prefix}.stat_bonuses_invalid")
        if not isinstance(event_names, list):
            errors.append(f"{prefix}.event_names_not_list")
        if not isinstance(skill_hints, list):
            errors.append(f"{prefix}.skill_hints_not_list")

        character_id = str(entry.get("character_id") or "unknown")
        display_name = str(SupportIdentityMatcher._first_present(entry, "card_name") or "")
        card_type = str(SupportIdentityMatcher._first_present(entry, "type") or "unknown")
        if card_type == "unknown" or display_name == "" or display_name.startswith("unknown_"):
            annotation_todos.append(
                {
                    "card_id": card_id,
                    "display_name": SupportIdentityMatcher._display_name_or_unknown_label(
                        card_id=card_id,
                        display_name=display_name,
                    ),
                    "avatar_template_path": str(template_path or ""),
                    "missing_fields": [
                        field
                        for field, is_missing in (
                            ("card_name", display_name == "" or display_name.startswith("unknown_")),
                            ("type", card_type == "unknown"),
                        )
                        if is_missing
                    ],
                }
            )

    non_support_entities = payload.get("non_support_entities", [])
    if not isinstance(non_support_entities, list):
        errors.append("non_support_entities_not_list")
        non_support_entities = []
    seen_entity_ids: set[str] = set()
    for index, entry in enumerate(non_support_entities):
        prefix = f"non_support_entities[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}.not_object")
            continue

        entity_id = str(entry.get("entity_id") or "")
        if not entity_id:
            errors.append(f"{prefix}.entity_id_empty")
        elif entity_id in seen_entity_ids:
            errors.append(f"{prefix}.duplicate_entity_id:{entity_id}")
        seen_entity_ids.add(entity_id)

        if entry.get("entity_kind") != "non_support_npc":
            errors.append(f"{prefix}.entity_kind_invalid")
        if entry.get("matched_card_id") is not None:
            errors.append(f"{prefix}.matched_card_id_must_be_null")

        template_paths = entry_template_paths(entry)
        if not template_paths:
            errors.append(f"{prefix}.avatar_template_path_empty")
        for path in template_paths:
            resolved = resolve_template_path(path)
            if not resolved.is_file():
                errors.append(f"{prefix}.template_path_missing:{path}")
                continue
            non_support_template_paths.add(resolved)

    if card_template_paths & non_support_template_paths:
        errors.append("template_path_shared_between_card_and_non_support_entity")

    return {
        "schema_version": SUPPORT_DECK_SCHEMA_VERSION,
        "required_card_fields": list(REQUIRED_SUPPORT_CARD_FIELDS),
        "card_count": len(cards),
        "non_support_entity_count": len(non_support_entities),
        "annotation_todo_count": len(annotation_todos),
        "annotation_todos": annotation_todos,
        "errors": errors,
        "warnings": warnings,
        "is_valid": not errors,
    }
