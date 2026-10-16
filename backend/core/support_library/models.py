from __future__ import annotations
from dataclasses import dataclass
RARITIES = frozenset({'SSR', 'SR', 'R'})

@dataclass(frozen=True)
class SupportCardRecord:
    card_id: str
    display_name: str
    rarity: str
    level: int | None
    level_cap: int | None
    breakthrough_stage: int | None
    breakthrough_cap: int | None
    free_breakthrough_steps: int | None
    duplicate_count: int | None
    confidence: float

    def __post_init__(self) -> None:
        if not self.card_id.strip() or not self.display_name.strip():
            raise ValueError('support_card_identity_required')
        if self.rarity not in RARITIES:
            raise ValueError('support_card_rarity_invalid')
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError('support_card_confidence_invalid')

    @property
    def breakthrough_steps_available(self) -> int:
        if self.breakthrough_stage is None or self.breakthrough_cap is None:
            return 0
        if self.free_breakthrough_steps is None:
            return 0
        return max(0, min(self.breakthrough_cap - self.breakthrough_stage, self.free_breakthrough_steps))

    @property
    def can_upgrade_to_max(self) -> bool:
        return self.level is not None and self.level_cap is not None and (self.level < self.level_cap)

@dataclass(frozen=True)
class UmaRecord:
    uma_id: str
    display_name: str
    rarity: str | None
    level: int | None
    confidence: float

    def __post_init__(self) -> None:
        if not self.uma_id.strip() or not self.display_name.strip():
            raise ValueError('uma_identity_required')
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError('uma_confidence_invalid')

@dataclass(frozen=True)
class LibrarySnapshot:
    account_id: str
    captured_at: str
    support_cards: tuple[SupportCardRecord, ...]
    umas: tuple[UmaRecord, ...]
    pending_review: tuple[str, ...] = ()

@dataclass(frozen=True)
class SupportMaintenancePolicy:
    whitelist: tuple[str, ...] = ()
    min_confidence: float = 0.9
    allow_paid_resources: bool = False

    def __post_init__(self) -> None:
        normalized = tuple(dict.fromkeys((card_id.strip() for card_id in self.whitelist if card_id.strip())))
        object.__setattr__(self, 'whitelist', normalized)
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError('maintenance_confidence_invalid')
        if self.allow_paid_resources:
            raise ValueError('paid_resources_not_supported')

@dataclass(frozen=True)
class MaintenanceAction:
    action_id: str
    card_id: str
    kind: str
    target: int
    uses_paid_resource: bool
    reason: str

@dataclass(frozen=True)
class MaintenancePlan:
    account_id: str
    actions: tuple[MaintenanceAction, ...]
    pending_review: tuple[str, ...]
    policy_sha256: str
