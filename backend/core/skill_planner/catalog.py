from __future__ import annotations
import json
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from core.skill_planner.models import SkillCatalogRow

@dataclass(frozen=True)
class SkillCatalog:
    rows: dict[str, SkillCatalogRow]

    @classmethod
    def load(cls, path: str | Path) -> 'SkillCatalog':
        payload = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        rows: dict[str, SkillCatalogRow] = {}
        raw_by_id: dict[str, dict[str, Any]] = {}
        for item in payload.get('skills', []):
            if not isinstance(item, dict):
                continue
            row = SkillCatalogRow.from_dict(item)
            if row.skill_id:
                rows[row.skill_id] = row
                raw_by_id[row.skill_id] = item
        rows = _infer_verified_external_upgrade_relations(rows, raw_by_id)
        rows = _infer_verified_circle_relations(rows)
        return cls(rows=rows)

    def get(self, skill_id: str) -> SkillCatalogRow:
        return self.rows[skill_id]

    def maybe_get(self, skill_id: str) -> SkillCatalogRow | None:
        return self.rows.get(skill_id)

def _infer_verified_circle_relations(rows: dict[str, SkillCatalogRow]) -> dict[str, SkillCatalogRow]:
    """Link exact ○/◎ pairs without assuming ◎ has no gold successor."""
    by_name: dict[str, list[SkillCatalogRow]] = defaultdict(list)
    for row in rows.values():
        by_name[row.name_zh_cn].append(row)
    inferred = dict(rows)
    for name, members in by_name.items():
        if not name.endswith('○') or len(members) != 1:
            continue
        uppers = by_name.get(name[:-1] + '◎', [])
        if len(uppers) != 1:
            continue
        lower, upper = (members[0], uppers[0])
        if not lower.is_verified or not upper.is_verified or lower.rarity != 'normal' or (upper.rarity != 'normal') or (lower.type != upper.type) or (lower.conditions != upper.conditions) or (lower.relations.get('upper', []) not in ([], [upper.skill_id])) or (upper.relations.get('lower', []) not in ([], [lower.skill_id])):
            continue
        inferred[lower.skill_id] = replace(lower, relations={**lower.relations, 'upper': [upper.skill_id]})
        inferred[upper.skill_id] = replace(upper, relations={**upper.relations, 'lower': [lower.skill_id]})
    return inferred

def _infer_verified_external_upgrade_relations(rows: dict[str, SkillCatalogRow], raw_by_id: dict[str, dict[str, Any]]) -> dict[str, SkillCatalogRow]:
    """Fill only unambiguous two-row external skill upgrade groups.

    The external database encodes an upgrade family as a shared ``group_id``
    and uses ``rate`` 1 for the lower skill and 2 for the gold skill.  Some
    reviewed catalog exports retained those fields but omitted ``relations``.
    We infer a relation only when the group is complete, unique, verified and
    type-consistent; ambiguous or partially reviewed groups remain unchanged.
    """
    grouped: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for skill_id, raw in raw_by_id.items():
        external = raw.get('external_truth')
        if not isinstance(external, dict):
            continue
        group_id = external.get('group_id')
        rate = external.get('rate')
        if type(group_id) is int and type(rate) is int and (rate in {1, 2}) and rows[skill_id].is_verified:
            grouped[group_id].append((skill_id, rate))
    inferred = dict(rows)
    for members in grouped.values():
        if len(members) != 2 or {rate for _, rate in members} != {1, 2}:
            continue
        lower_id = next((skill_id for skill_id, rate in members if rate == 1))
        upper_id = next((skill_id for skill_id, rate in members if rate == 2))
        lower = rows[lower_id]
        upper = rows[upper_id]
        if lower.relations or upper.relations or upper.rarity != 'gold' or (lower.rarity == 'gold') or (lower.type != upper.type):
            continue
        inferred[lower_id] = replace(lower, relations={'upper': [upper_id]})
        inferred[upper_id] = replace(upper, relations={'lower': [lower_id]})
    return inferred
