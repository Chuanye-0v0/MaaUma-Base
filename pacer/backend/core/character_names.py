"""Read-only roster adapter for character-name callers.

The projected Pacer roster omits character_id; the shared matcher derives it
from the six-digit card id.  Importing this module does not read any file.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.name_match import (
    character_candidates as _character_candidates,
    is_confident,
    match_character,
    normalize_name,
)


def character_candidates(records_or_roster: object = None) -> list[dict]:
    """Load the adjacent roster or aggregate explicitly provided records."""
    if records_or_roster is None:
        try:
            path = Path(__file__).with_name("autonomous_training") / "plant_roster.json"
            records_or_roster = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            return []
    return _character_candidates(records_or_roster)


def match_character_name(observed: object, expected: object, candidates: object = None) -> bool:
    """Compare resolved identities without narrowing the observed candidate set.

Unknown custom labels retain their previous normalized exact-name behavior;
they are not made similar to a known, different character by this fallback.
"""
    candidates = character_candidates() if candidates is None else character_candidates(candidates)
    observed_id, observed_score, observed_basis = match_character(observed, candidates)
    expected_id, expected_score, expected_basis = match_character(expected, candidates)
    observed_confident = is_confident(observed_score, observed_basis)
    expected_confident = is_confident(expected_score, expected_basis)
    if observed_confident or expected_confident:
        return observed_confident and expected_confident and observed_id == expected_id
    return bool(normalize_name(observed)) and normalize_name(observed) == normalize_name(expected)
