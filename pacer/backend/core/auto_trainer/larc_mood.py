"""Soft recovery preference; these scores are not game reward amounts."""
import math

MOODS = {"极佳": 2, "上佳": 1, "良好": 1, "普通": 0, "不佳": -1, "极差": -2,
         "great": 2, "very_good": 2, "excellent": 2, "good": 1, "normal": 0,
         "bad": -1, "poor": -1, "very_bad": -2, "awful": -2}
MOOD_LEVEL_CREDIT = 9.


def mood_recovery_value(mood, change):
    before = MOODS.get(mood) if isinstance(mood, str) else mood
    if type(before) not in {int, float} or before not in range(-2, 3):
        return {"score": 0., "before": None, "after": None, "effective_levels": 0,
                "basis": "mood_unknown"}
    after = max(-2, min(2, before + change))
    # Normal +1 retains the old 18-point preference. Repairing worse mood is
    # more urgent (27/36); topping off good mood is less urgent (9).
    # Price only the levels actually gained, never all missing mood at once.
    def potential(level):
        lower = math.floor(level)
        return (sum(MOOD_LEVEL_CREDIT * (2 - step) for step in range(lower, 2))
                - (level - lower) * MOOD_LEVEL_CREDIT * (2 - lower))
    score = potential(before) - potential(after)
    return {"score": round(score, 4), "before": before, "after": after,
            "effective_levels": after - before,
            "basis": "bounded severity preference; not extra observed training yield or forced outing"}
