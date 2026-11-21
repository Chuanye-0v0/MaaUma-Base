"""Known L'Arc action dates shared by charge and recovery planning.

Race dates come from the existing scenario flow. This calendar does not
invent race energy changes, random events, or a date for pre-debut turns.
"""

FORCED_RACES = {33: "derby", 40: "niel", 42: "classic_arc",
                59: "takarazuka", 64: "foy", 66: "senior_arc"}
LAST_DOMESTIC_DAY = 58
LAST_TRAINING_DAY = 65


def period_ordinal(period):
    year, month, half = period.get("year_index"), period.get("month"), period.get("half")
    if (year not in {1, 2, 3} or type(month) is not int or not 1 <= month <= 12
            or half not in {"first", "second"}):
        return None
    return (year - 1) * 24 + (month - 1) * 2 + (half == "second")


def period_at(day):
    return {"year_index": day // 24 + 1, "month": day % 24 // 2 + 1,
            "half": "second" if day % 2 else "first"}


def is_overseas(day):
    return day >= 24 and 7 <= day % 24 // 2 + 1 <= 9


def free_action_days(start, stop=LAST_TRAINING_DAY + 1):
    return [day for day in range(max(0, start), min(stop, LAST_TRAINING_DAY + 1))
            if day not in FORCED_RACES]


def domestic_action_days(start, stop=LAST_DOMESTIC_DAY + 1):
    return [day for day in free_action_days(start, stop) if not is_overseas(day)]
