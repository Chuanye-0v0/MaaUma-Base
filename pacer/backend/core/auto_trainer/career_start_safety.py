from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
import hashlib
import io
from pathlib import Path
import subprocess
from typing import Callable, Sequence

from core.auto_trainer.career_start_contracts import canonical_sha256
from core.auto_trainer.frame_safety import (
    FRAME_SAFETY_SCHEMA_VERSION,
    FrameSafetyPacket,
)
from core.screen_text import analyze_screen_text
from core.skill_acquisition_parser import resolve_tesseract_runtime


NAV_TERM_ROIS = {
    "商店": (0.25, 0.80, 0.43, 0.91),
    "招募": (0.82, 0.91, 1.00, 1.00),
}
ROUTE_FORBIDDEN_TERMS = ("好友刷新", "刷新好友", "更新列表")


PASSIVE_STATUS_TERM_ROIS = {
    "TP": (0.20, 0.01, 0.55, 0.10),
    "RP": (0.55, 0.01, 0.85, 0.10),
}


@dataclass(frozen=True)
class LocalizedOcrToken:
    text: str
    bbox: tuple[int, int, int, int] | None
    confidence: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class NavigationExclusion:
    term: str
    bbox: tuple[int, int, int, int]
    expected_slot: str
    reason: str = "persistent_nav_only"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CareerStartSafetyScope:
    top_status_bar: bool
    global_home_nav: bool
    home_tab_selected: bool
    career_button_count: int
    blocking_modal: bool
    trusted_navigation_context: str = "none"
    trusted_navigation_slots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.trusted_navigation_context not in {
            "none",
            "grand_masters_destination",
            "scenario_select_loading",
        }:
            raise ValueError("trusted_navigation_context")
        if (
            not isinstance(self.trusted_navigation_slots, tuple)
            or not set(self.trusted_navigation_slots).issubset(NAV_TERM_ROIS)
        ):
            raise ValueError("trusted_navigation_slots")

    @property
    def complete_home(self) -> bool:
        return bool(
            self.top_status_bar
            and self.global_home_nav
            and self.home_tab_selected
            and self.career_button_count == 1
            and not self.blocking_modal
        )

    @property
    def navigation_exclusion_allowed(self) -> bool:
        return bool(
            self.complete_home
            or (
                self.trusted_navigation_context
                in {"grand_masters_destination", "scenario_select_loading"}
                and not self.blocking_modal
            )
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CareerStartSafetyBundle:
    source_image_sha256: str
    raw_packet: FrameSafetyPacket
    scoped_packet: FrameSafetyPacket
    tokens: Sequence[LocalizedOcrToken]
    exclusions: Sequence[NavigationExclusion]
    derivation_digest: str
    scope: CareerStartSafetyScope
    source_size: tuple[int, int]


class TesseractTsvTokenReader:
    def read(self, png_bytes: bytes) -> tuple[LocalizedOcrToken, ...]:
        runtime = resolve_tesseract_runtime()
        languages = [
            language
            for language in ("chi_sim", "eng")
            if language in runtime.available_languages
        ]
        if not languages:
            raise RuntimeError("career_start_ocr_language_missing")
        completed = subprocess.run(
            [
                str(runtime.executable),
                "stdin",
                "stdout",
                "--tessdata-dir",
                ".",
                "-l",
                "+".join(languages),
                "--psm",
                "11",
                "-c",
                "tessedit_create_tsv=1",
            ],
            cwd=runtime.tessdata_dir,
            input=png_bytes,
            capture_output=True,
            text=False,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise RuntimeError(f"career_start_tesseract_exit:{completed.returncode}")
        text = completed.stdout.decode("utf-8", errors="replace")
        tokens: list[LocalizedOcrToken] = []
        for row in csv.DictReader(
            io.StringIO(text),
            delimiter="\t",
            quoting=csv.QUOTE_NONE,
        ):
            token_text = str(row.get("text", "")).strip()
            if not token_text:
                continue
            try:
                confidence = float(row.get("conf", "-1"))
                bbox = (
                    int(row["left"]),
                    int(row["top"]),
                    int(row["width"]),
                    int(row["height"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("career_start_tesseract_tsv_invalid") from exc
            if confidence < 0:
                continue
            tokens.append(
                LocalizedOcrToken(
                    text=token_text,
                    bbox=bbox,
                    confidence=confidence,
                )
            )
        return tuple(tokens)


class CareerStartLocalizedSafetyAnalyzer:
    def __init__(
        self,
        *,
        token_reader: TesseractTsvTokenReader
        | Callable[[bytes], Sequence[LocalizedOcrToken]]
        | object
        | None = None,
    ) -> None:
        self.token_reader = token_reader or TesseractTsvTokenReader()

    def analyze(
        self,
        png_bytes: bytes,
        *,
        source_size: tuple[int, int],
        scope: CareerStartSafetyScope,
    ) -> CareerStartSafetyBundle:
        source_sha256 = hashlib.sha256(png_bytes).hexdigest()
        errors: tuple[str, ...] = ()
        try:
            tokens = tuple(self.token_reader.read(png_bytes))
        except Exception as exc:
            tokens = ()
            errors = (f"localized_ocr:{type(exc).__name__}",)

        canonical_tokens = tuple(
            self._canonical_token(token, source_size) for token in tokens
        )
        exclusions, excluded_indexes = self._navigation_exclusions(
            canonical_tokens,
            scope,
        )
        raw_text = " ".join(token.text for token in canonical_tokens)
        scoped_text = " ".join(
            token.text
            for index, token in enumerate(canonical_tokens)
            if index not in excluded_indexes
        )
        raw_packet = self._packet(
            source_sha256,
            raw_text,
            errors=errors,
            forced_resource_terms=self._forced_resource_terms(canonical_tokens),
        )
        scoped_packet = self._packet(
            source_sha256,
            scoped_text,
            errors=errors,
            forced_resource_terms=self._forced_resource_terms(
                canonical_tokens,
                excluded_indexes=excluded_indexes,
            ),
        )
        derivation_digest = canonical_sha256(
            {
                "source_image_sha256": source_sha256,
                "source_size": source_size,
                "tokens": [token.to_dict() for token in canonical_tokens],
                "scope": scope.to_dict(),
                "exclusions": [item.to_dict() for item in exclusions],
                "raw_packet": raw_packet.to_dict(),
                "scoped_packet": scoped_packet.to_dict(),
            }
        )
        return CareerStartSafetyBundle(
            source_image_sha256=source_sha256,
            raw_packet=raw_packet,
            scoped_packet=scoped_packet,
            tokens=canonical_tokens,
            exclusions=exclusions,
            derivation_digest=derivation_digest,
            scope=scope,
            source_size=source_size,
        )

    @staticmethod
    def _canonical_token(
        token: LocalizedOcrToken,
        source_size: tuple[int, int],
    ) -> LocalizedOcrToken:
        if token.bbox is None or source_size == (720, 1280):
            return token
        if source_size != (1440, 2560) or any(value % 2 for value in token.bbox):
            return LocalizedOcrToken(
                text=token.text,
                bbox=None,
                confidence=token.confidence,
            )
        return LocalizedOcrToken(
            text=token.text,
            bbox=tuple(value // 2 for value in token.bbox),
            confidence=token.confidence,
        )

    @staticmethod
    def _navigation_exclusions(
        tokens: Sequence[LocalizedOcrToken],
        scope: CareerStartSafetyScope,
    ) -> tuple[tuple[NavigationExclusion, ...], frozenset[int]]:
        if not scope.navigation_exclusion_allowed:
            return (), frozenset()
        exclusions: list[NavigationExclusion] = []
        excluded_indexes: set[int] = set()
        for term, roi in NAV_TERM_ROIS.items():
            if term not in scope.trusted_navigation_slots:
                continue
            indexes = [index for index, token in enumerate(tokens) if token.text == term]
            if len(indexes) != 1:
                continue
            index = indexes[0]
            bbox = tokens[index].bbox
            if bbox is None or not _bbox_inside_normalized_roi(bbox, roi):
                continue
            exclusions.append(
                NavigationExclusion(
                    term=term,
                    bbox=bbox,
                    expected_slot=f"persistent_nav.{term}",
                )
            )
            excluded_indexes.add(index)
        if scope.navigation_exclusion_allowed:
            for term, roi in PASSIVE_STATUS_TERM_ROIS.items():
                indexes = [
                    index for index, token in enumerate(tokens) if token.text == term
                ]
                if len(indexes) != 1:
                    continue
                index = indexes[0]
                bbox = tokens[index].bbox
                if bbox is None or not _bbox_inside_normalized_roi(bbox, roi):
                    continue
                exclusions.append(
                    NavigationExclusion(
                        term=term,
                        bbox=bbox,
                        expected_slot=f"persistent_status.{term}",
                        reason="persistent_status_only",
                    )
                )
                excluded_indexes.add(index)
        return tuple(exclusions), frozenset(excluded_indexes)

    @staticmethod
    def _forced_resource_terms(
        tokens: Sequence[LocalizedOcrToken],
        *,
        excluded_indexes: frozenset[int] = frozenset(),
    ) -> tuple[str, ...]:
        resource_vocabulary = (
            frozenset(NAV_TERM_ROIS)
            | frozenset(PASSIVE_STATUS_TERM_ROIS)
            | frozenset(ROUTE_FORBIDDEN_TERMS)
        )
        return tuple(
            token.text
            for index, token in enumerate(tokens)
            if index not in excluded_indexes and token.text in resource_vocabulary
        )

    @staticmethod
    def _packet(
        source_sha256: str,
        visible_text: str,
        *,
        errors: tuple[str, ...],
        forced_resource_terms: Sequence[str] = (),
    ) -> FrameSafetyPacket:
        text_status = "ok" if visible_text.strip() and not errors else "abstained"
        normalized = "".join(visible_text.split())
        analysis = analyze_screen_text(normalized)
        resource_terms = list(analysis.get("resource_terms", []))
        for term in ROUTE_FORBIDDEN_TERMS:
            if term in normalized and term not in resource_terms:
                resource_terms.append(term)
        for term in forced_resource_terms:
            if term not in resource_terms:
                resource_terms.append(term)
        dangerous = bool(resource_terms) or text_status != "ok"
        return FrameSafetyPacket(
            schema_version=FRAME_SAFETY_SCHEMA_VERSION,
            source_image_sha256=source_sha256,
            visible_text=visible_text,
            text_source="localized_tesseract",
            text_status=text_status,
            resource_terms=tuple(resource_terms),
            dangerous=dangerous,
            negative_resource_evidence_trusted=False,
            errors=errors,
        )


def _bbox_inside_normalized_roi(
    bbox: tuple[int, int, int, int],
    roi: tuple[float, float, float, float],
) -> bool:
    x, y, width, height = bbox
    if width <= 0 or height <= 0:
        return False
    x0, y0, x1, y1 = roi
    return bool(
        x >= round(720 * x0)
        and y >= round(1280 * y0)
        and x + width <= round(720 * x1)
        and y + height <= round(1280 * y1)
    )


def validate_career_start_safety_bundle(
    bundle: CareerStartSafetyBundle,
    *,
    source_size: tuple[int, int],
) -> bool:
    if type(bundle) is not CareerStartSafetyBundle:
        return False
    if type(bundle.scope) is not CareerStartSafetyScope:
        return False
    if source_size not in {(720, 1280), (1440, 2560)}:
        return False
    if tuple(bundle.source_size) != tuple(source_size):
        return False
    tokens = tuple(bundle.tokens)
    if not all(type(token) is LocalizedOcrToken for token in tokens):
        return False
    analyzer = CareerStartLocalizedSafetyAnalyzer()
    exclusions, excluded_indexes = analyzer._navigation_exclusions(
        tokens,
        bundle.scope,
    )
    raw_text = " ".join(token.text for token in tokens)
    scoped_text = " ".join(
        token.text
        for index, token in enumerate(tokens)
        if index not in excluded_indexes
    )
    errors = tuple(bundle.raw_packet.errors)
    if errors != tuple(bundle.scoped_packet.errors):
        return False
    raw_packet = analyzer._packet(
        bundle.source_image_sha256,
        raw_text,
        errors=errors,
        forced_resource_terms=analyzer._forced_resource_terms(tokens),
    )
    scoped_packet = analyzer._packet(
        bundle.source_image_sha256,
        scoped_text,
        errors=errors,
        forced_resource_terms=analyzer._forced_resource_terms(
            tokens,
            excluded_indexes=excluded_indexes,
        ),
    )
    derivation_digest = canonical_sha256(
        {
            "source_image_sha256": bundle.source_image_sha256,
            "source_size": source_size,
            "tokens": [token.to_dict() for token in tokens],
            "scope": bundle.scope.to_dict(),
            "exclusions": [item.to_dict() for item in exclusions],
            "raw_packet": raw_packet.to_dict(),
            "scoped_packet": scoped_packet.to_dict(),
        }
    )
    return bool(
        tuple(bundle.exclusions) == tuple(exclusions)
        and bundle.raw_packet == raw_packet
        and bundle.scoped_packet == scoped_packet
        and bundle.derivation_digest == derivation_digest
    )


__all__ = [
    "CareerStartLocalizedSafetyAnalyzer",
    "CareerStartSafetyBundle",
    "CareerStartSafetyScope",
    "LocalizedOcrToken",
    "NavigationExclusion",
    "TesseractTsvTokenReader",
    "validate_career_start_safety_bundle",
]
