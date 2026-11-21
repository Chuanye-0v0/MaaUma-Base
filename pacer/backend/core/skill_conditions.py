"""Parse game condition data without executing source text or simulating a race."""
from __future__ import annotations

import operator
import re
from typing import Mapping


COMPARISON = re.compile(r"([A-Za-z_]\w*)\s*(==|!=|>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\Z")
OPS = {"==": operator.eq, "!=": operator.ne, ">=": operator.ge,
       "<=": operator.le, ">": operator.gt, "<": operator.lt}
PHASE_NAMES = {0: "序盘", 1: "中盘", 2: "终盘前半", 3: "终盘后半"}
PHASE_RANGES = {0: (0, 1 / 6), 1: (1 / 6, 2 / 3), 2: (2 / 3, 5 / 6), 3: (5 / 6, 1)}
VARIABLE_NAMES = {
    "running_style": "跑法（1领跑、2跟前、3居中、4后追）",
    "distance_type": "距离类型（1短距离、2英里、3中距离、4长距离）",
    "ground_type": "场地（1草地、2泥地）", "course_distance": "比赛距离（米）",
    "phase": "比赛阶段", "phase_random": "阶段内随机位置",
    "phase_firsthalf_random": "阶段前半的随机位置",
    "phase_laterhalf_random": "阶段后半的随机位置",
    "phase_firstquarter_random": "阶段前四分之一的随机位置",
    "distance_rate": "已跑距离占比（%）", "remain_distance": "剩余距离（米）",
    "distance_rate_after_random": "指定进度之后的随机位置",
    "order": "名次", "order_rate": "名次占比（%）",
    "is_finalcorner": "处于最终弯道范围", "corner": "当前弯道编号（0为直线）",
    "is_last_straight": "处于最终直线", "all_corner_random": "全程弯道随机位置",
    "corner_random": "指定弯道随机位置", "straight_random": "直线随机位置",
    "is_finalcorner_random": "最终弯道随机位置",
    "is_finalcorner_laterhalf": "最终弯道后半",
    "is_lastspurt": "最终冲刺状态", "hp_per": "剩余耐力占比（%）",
    "accumulatetime": "比赛累计时间（秒）", "is_overtake": "正在尝试赶超",
    "overtake_target_time": "存在赶超目标的持续时间（秒）",
    "distance_diff_top": "与第一名的距离（米）", "track_id": "赛场 ID",
    "slope": "坡道类型", "season": "季节枚举", "weather": "天气枚举",
    "ground_condition": "场地状态枚举", "rotation": "赛道方向枚举",
}


def parse_condition(code: str) -> dict:
    code = code.strip()
    if not code or code in {"0", "1"}:
        return {"constant": code != "0"}
    alternatives = []
    for term in code.split("@"):
        clauses = []
        for part in term.split("&"):
            match = COMPARISON.fullmatch(part.strip())
            if match is None:
                raise ValueError(f"unsupported_skill_condition:{part}")
            variable, op, literal = match.groups()
            value = float(literal) if "." in literal else int(literal)
            clauses.append({"variable": variable, "operator": op, "value": value})
        alternatives.append({"all": clauses})
    return {"any": alternatives}


def atoms(tree: dict) -> list[dict]:
    if "variable" in tree:
        return [tree]
    return [atom for child in tree.get("any", tree.get("all", [])) for atom in atoms(child)]


def evaluate_condition(tree: dict, inputs: Mapping[str, int | float]) -> bool | None:
    """Three-valued evaluation: missing race state stays unknown.

    This compares supplied game variables. It does not choose random positions,
    latch preconditions, model overtaking, or predict activation probabilities.
    """
    if "constant" in tree:
        return tree["constant"]
    if "variable" in tree:
        value = inputs.get(tree["variable"])
        if value is None:
            return None
        return OPS[tree["operator"]](value, tree["value"])
    values = [evaluate_condition(child, inputs) for child in tree.get("all", tree.get("any", []))]
    if "all" in tree:
        return False if False in values else (None if None in values else True)
    return True if True in values else (None if None in values else False)


def describe_condition(tree: dict, course_distance: int | None = None) -> list[dict]:
    alternatives = tree.get("any", [{"all": atoms(tree)}])
    result = []
    for alternative in alternatives:
        clauses = []
        for atom in atoms(alternative):
            var, op, value = atom["variable"], atom["operator"], atom["value"]
            clause = {**atom, "label_zh_cn": VARIABLE_NAMES.get(var, var),
                      "random_position": "random" in var,
                      "description_verified": var in VARIABLE_NAMES}
            if var.startswith("phase") and op == "==" and value in PHASE_NAMES:
                lo, hi = PHASE_RANGES[value]
                if "firsthalf" in var:
                    hi = (lo + hi) / 2
                elif "laterhalf" in var:
                    lo = (lo + hi) / 2
                elif "firstquarter" in var:
                    hi = lo + (hi - lo) / 4
                clause["phase_name"] = PHASE_NAMES[value]
                if var in {"phase", "phase_random", "phase_firsthalf_random", "phase_laterhalf_random", "phase_firstquarter_random"}:
                    clause["track_fraction_range"] = [lo, hi]
                    if course_distance:
                        clause["track_meter_range"] = [round(lo * course_distance, 3), round(hi * course_distance, 3)]
            if course_distance and var == "distance_rate":
                clause["threshold_meters_from_start"] = course_distance * value / 100
            if course_distance and var == "remain_distance":
                clause["threshold_meters_from_start"] = course_distance - value
            clauses.append(clause)
        result.append({"all": clauses})
    return result
