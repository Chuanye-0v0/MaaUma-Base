"""Separate execution restrictions from uncertain training estimates."""

from core.auto_trainer.thresholds import SOFT_RISK_DISCOUNT


HARD_REJECT_REASONS = frozenset({
    'failure_rate_exceeds_limit',  # The user's configured authorization limit.
    'paid_action_not_authorized',
    'cross_account_action',
    'action_not_authorized',
})


def is_hard_rejected(row):
    if 'hard_rejected' in row:
        return row['hard_rejected'] is True
    return row.get('rejected') is True


def discount_soft_score(score, soft_risk):
    """Discount positive uncertain value without rewarding negative scores."""
    return round(score * SOFT_RISK_DISCOUNT, 4) if soft_risk and score > 0 else score
