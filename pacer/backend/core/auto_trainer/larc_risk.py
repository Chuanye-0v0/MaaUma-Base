"""One training-risk scale; a planning preference is not a game probability."""
import math


def strategy_v4_enabled(state=None, goal=None):
    from core.auto_trainer.larc_policy import strategy_v4_enabled as policy_enabled
    return (policy_enabled(goal)
            or getattr(state, 'scenario_state', {}).get('larc_strategy_v4') is True)


def risk_value(success_value, failure_rate, *, basis='observed_current', include_preference=True):
    """Price known failure once, with the legacy aversion shown separately.

    Unknown probabilities keep a conditional-success opportunity value. They
    are never represented as measured zero-percent future failure.
    """
    known = (type(failure_rate) in {int, float} and math.isfinite(failure_rate)
             and 0 <= failure_rate <= 100)
    success = max(0., float(success_value))
    loss = success * failure_rate / 100 if known else 0.
    preference = .5 * failure_rate if known and include_preference else 0.
    return {'value': success - loss - preference,
            'conditional_success_value': success,
            'expected_value': success - loss if known else None,
            'expected_loss': loss, 'preference_cost': preference,
            'failure_rate': failure_rate if known else None,
            'success_probability': 1. - failure_rate / 100 if known else None,
            'probability_status': 'known' if known else 'unknown_conditional_success_only',
            'basis': basis, 'preference_basis': 'legacy_aversion_0.5_per_failure_percentage_point',
            'failure_energy_effect': 'unknown_not_invented'}


def expected_success_value(success_value, failure_rate, **kwargs):
    return risk_value(success_value, failure_rate, **kwargs)['value']


def risk_at_energy(success_value, metadata, energy):
    """An observed rate applies at its observed energy, not every future bar."""
    metadata = metadata if isinstance(metadata, dict) else {}
    observed = metadata.get('vitality_percent')
    same = (type(observed) in {int, float} and math.isfinite(observed)
            and abs(observed - energy) < 1e-6)
    return risk_value(success_value, metadata.get('failure_rate') if same else None,
        basis='sampled_layout_at_observed_energy' if same else 'future_energy_failure_rate_unknown')
