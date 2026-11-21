"""Local catalog search for the user-selected first-Arc list; no game actions."""
from functools import lru_cache

from core.skill_knowledge import SkillKnowledgeLibrary, race_inputs


@lru_cache(maxsize=1)
def library():
    return SkillKnowledgeLibrary.load()


def validate_choices(choices):
    for identity in choices:
        try:
            row = library().get(identity)
        except ValueError as exc:
            raise ValueError(f"技能名称无法唯一匹配，请从技能库选择：{identity}") from exc
        if row["acquisition"]["kind"] != "pt_purchase":
            raise ValueError(f"该技能不能直接花 Pt 学习：{row['name_zh_cn']}")


def search_choices(query="", *, target=None, limit=100):
    """Catalog choices are not an assertion of this run's available inventory."""
    if not isinstance(query, str) or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("技能搜索参数无效。")
    target = target or {}
    race_inputs(target)
    results = []
    for row in library().search(query.strip()):
        if row["acquisition"]["kind"] != "pt_purchase":
            continue
        activation = library().explain_activation(row["skill_id"], target)
        results.append({"skill_id": row["skill_id"], "name": row["name_zh_cn"],
            "kind": row["skill_kind"], "target_compatible": activation["target_compatible"],
            "conditions": [branch["condition_code"] for branch in activation["branches"]],
            "description": row.get("description_zh_cn") or row.get("description") or "",
            "base_cost": row.get("base_cost"), "hint_level": None, "actual_chain_price": None})
    return {"source": "local_cn_larc_end", "total": len(results), "items": results[:limit],
            "note": "这是可选技能库；本局是否可学、启发等级和剩余下位链价格在赛前读取。"}
