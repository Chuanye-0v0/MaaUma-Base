"""Offline identity matching for a bounded, full support-card rectangle.

No OCR, device calls, clicks, deck changes, or selectability decisions live here.
The caller must locate the card rectangle for its page layout separately.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import cv2
import numpy as np

@dataclass(frozen=True)
class CardArtMatch:
    status: str
    card_id: int | None
    reason: str
    score: float = 0.0
    margin: float = 0.0
    candidates: tuple[tuple[int, float], ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)

def read_image(path: Path) -> np.ndarray:
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f'Cannot decode card image: {path}')
    return image

class CardArtMatcher:
    """Compare central artwork while ignoring rarity/type/level/status overlays."""

    def __init__(self, references: dict[int, np.ndarray], *, min_score: float=0.8, min_margin: float=0.1):
        if not references:
            raise ValueError('No support-card reference art loaded')
        self.min_score = min_score
        self.min_margin = min_margin
        self.templates = {}
        for card_id, image in references.items():
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            self.templates[card_id] = [cv2.resize(gray, (round(96 * scale), round(128 * scale)), interpolation=cv2.INTER_AREA) for scale in (0.94, 1.0, 1.06)]

    @classmethod
    def from_catalog(cls, path: Path, **kwargs) -> CardArtMatcher:
        catalog = json.loads(path.read_text(encoding='utf-8'))
        if catalog.get('schema') != 'uma.support_card_art.v1':
            raise ValueError('Unsupported support-card catalog schema')
        references = {}
        for card in catalog['cards']:
            if card['image_status'] != 'ready':
                continue
            card_id = int(card['card_id'])
            if card_id in references:
                raise ValueError(f'Duplicate support card ID: {card_id}')
            references[card_id] = read_image(path.parent / card['image_path'])
        return cls(references, **kwargs)

    def match(self, crop: np.ndarray) -> CardArtMatch:
        if crop is None or crop.size == 0:
            return CardArtMatch('unknown', None, 'empty_crop')
        height, width = crop.shape[:2]
        if min(height, width) < 40 or not 0.65 <= width / height <= 0.85:
            return CardArtMatch('unknown', None, 'not_full_portrait_card')
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        normalized = cv2.resize(gray, (96, 128), interpolation=cv2.INTER_AREA)
        artwork = normalized[36:98, 18:78]
        if float(artwork.std()) < 8:
            return CardArtMatch('unknown', None, 'no_artwork_detail')
        scores = []
        for card_id, variants in self.templates.items():
            best = -1.0
            for reference in variants:
                x = (reference.shape[1] - 60) // 2
                y = round(reference.shape[0] * (67 / 128)) - 31
                window = reference[y - 6:y + 68, x - 6:x + 66]
                score = float(cv2.matchTemplate(window, artwork, cv2.TM_CCOEFF_NORMED).max())
                best = max(best, score)
            scores.append((card_id, best))
        scores.sort(key=lambda row: row[1], reverse=True)
        best_id, best_score = scores[0]
        margin = best_score - (scores[1][1] if len(scores) > 1 else 0.0)
        reason = 'low_similarity' if best_score < self.min_score else 'ambiguous_artwork' if margin < self.min_margin else 'matched_artwork'
        accepted = reason == 'matched_artwork'
        return CardArtMatch('matched' if accepted else 'unknown', best_id if accepted else None, reason, round(best_score, 4), round(margin, 4), tuple(((card_id, round(score, 4)) for card_id, score in scores[:3])))
