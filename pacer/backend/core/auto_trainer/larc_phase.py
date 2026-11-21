"""Shared numerical priorities by observed L'Arc phase."""
from core.auto_trainer.larc_recovery import period_value


# Initial policy preferences, not fixed game rules or fitted optimal weights.
PHASE_WEIGHTS = {
    "junior_early": dict(stats=.65, skill_pt=.45, effective_bond=18., net_charge=5., newly_full=8., ss_preparation=8.),
    "junior_late": dict(stats=.8, skill_pt=.55, effective_bond=16., net_charge=6., newly_full=10., ss_preparation=10.),
    "junior_unspecified": dict(stats=.75, skill_pt=.5, effective_bond=17., net_charge=5., newly_full=9., ss_preparation=9.),
    "classic_domestic": dict(stats=.9, skill_pt=.7, effective_bond=11., net_charge=4., newly_full=6., ss_preparation=8.),
    "classic_expedition": dict(stats=1., skill_pt=.85, effective_bond=6., net_charge=0., newly_full=0., ss_preparation=0.),
    "after_first_arc": dict(stats=1., skill_pt=.8, effective_bond=5., net_charge=1.5, newly_full=2., ss_preparation=3.),
    "senior_domestic": dict(stats=1., skill_pt=.85, effective_bond=3., net_charge=1., newly_full=2., ss_preparation=2.),
    "senior_expedition": dict(stats=1., skill_pt=.85, effective_bond=1., net_charge=0., newly_full=0., ss_preparation=0.),
    "baseline": dict(stats=1., skill_pt=.5, effective_bond=0., net_charge=0., newly_full=0., ss_preparation=0.),
}


def phase(state):
    return phase_for_period(period_value(state))


def phase_weights(state,goal=None):
    from core.auto_trainer.larc_user_preferences import weighted_phase
    return weighted_phase(PHASE_WEIGHTS[phase(state)],goal)


def phase_for_period(value):
    year = value.get("year_index")
    month = value.get("month")
    if year in {2, 3} and type(month) is int and 1 <= month <= 12:
        if 7 <= month <= 9:
            return "classic_expedition" if year == 2 else "senior_expedition"
        if year == 3:
            return "senior_domestic"
        return "after_first_arc" if month >= 10 else "classic_domestic"
    if year != 1:
        return "baseline"
    if value.get("pre_debut") is True or value.get("stage") == "pre_debut" or type(month) is int and 1 <= month <= 6:
        return "junior_early"
    if type(month) is int and 7 <= month <= 12:
        return "junior_late"
    return "junior_unspecified"

