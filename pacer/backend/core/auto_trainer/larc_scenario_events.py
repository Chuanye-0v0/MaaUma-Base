"""Preferences for known scenario choices; future hints are not earned skills.

With/Trust reference: https://gametora.com/umamusume/project-larc
Skill pairs: https://gamewith.jp/uma-musume/article/show/414361
"""
import math

from core.event_knowledge_lookup import normalize_text


# Policy values are editable through goal.event_preferences. The actual five
# options vary by run; their displayed order, not this table, determines taps.
WITH_REWARDS = {
    "天狼星象征": ("sirius_symboli", "瞬息万变", "中距离直线○", "medium"),
    "佐岳鸣": ("satake_mei", "隆尚奇才", "隆尚赛场○", "longchamp"),
    "神鹰": ("el_condor_pasa", "兴奋起来了！", "加快步伐", "general"),
    "曼城茶座": ("manhattan_cafe", "内侧体验", "弯里横", "long"),
    "中山庆典": ("nakayama_festa", "孤注一掷", "可疑的战术", "risk"),
    "黄金船": ("gold_ship", "拼死决心", "不遗余力", "medium"),
}
_WITH_LEGACY_SKILL_ALIASES = {
    "瞬息万变": ("光芒一閃", "光芒一闪"),
    "中距离直线○": ("中距離直線○",),
    "隆尚奇才": ("ロンシャンの申し子",),
    "隆尚赛场○": ("ロンシャンレース場○",),
    "兴奋起来了！": ("アガッてきた！",),
    "加快步伐": ("ペースアップ",),
    "内侧体验": ("内的体験",),
    "弯里横": ("内弁慶",),
    "孤注一掷": ("博打うち",),
    "可疑的战术": ("あやしげな作戦",),
    "拼死决心": ("決死の覚悟",),
    "不遗余力": ("ありったけ",),
}
_WITH_CHARACTER_ALIASES = {
    "佐岳梅": "佐岳鸣",
    "佐岳": "佐岳鸣",
    "友人": "佐岳鸣",
    "天狼星": "天狼星象征",
    "茶座": "曼城茶座",
    "中山": "中山庆典",
    "阿船": "黄金船",
}
LINK_TRAINEES = {row[0] for row in WITH_REWARDS.values()} - {"satake_mei"}
LINK_TRAINEES |= {"satono_diamond", "tap_dance_city", "orfevre"}


def _resolve_with_character(preferred, candidates):
    key = compact(preferred)
    if not key:
        return None
    for cand in candidates:
        if compact(cand) == key:
            return cand
    mapped = _WITH_CHARACTER_ALIASES.get(key)
    if mapped:
        for cand in candidates:
            if compact(cand) == compact(mapped):
                return cand
    from core.character_names import match_character_name

    for cand in candidates:
        if match_character_name(preferred, cand):
            return cand
    return None
NEEDLE_OPTIONS = ("选择能成为强大优俊少女的穴位", "选择能赢下比赛的穴位",
                  "选择能变得健康有活力的穴位", "选择能提升魅力的穴位", "不太放心，还是算了吧")
NEEDLE_PROCEED = ("是的", "还是算了")
NEEDLE_REFUSE = ("我拒绝", "我再想想")


def compact(value):
    return "".join(normalize_text(value).split())


def acupuncture_title(lines):
    return next((line for line in lines if "安心" in line and "针灸师" in line and "登" in line), None)


def acupuncture_decline_binding(expected, index, count):
    """Only the known last, non-risk option may use title + layout binding."""
    if count != 5 or index != count or not acupuncture_title(expected[:1]):
        return False
    return tuple(expected[1:]) == tuple(compact(option) for option in NEEDLE_OPTIONS)


def acupuncture_followup_options(lines):
    # Two visually identical layouts have opposite cancellation positions.
    keys=[compact(line).rstrip('!?.。~～') for line in lines]
    matches=[pair for pair in (NEEDLE_PROCEED,NEEDLE_REFUSE)
             if all(keys.count(option)==1 for option in pair) and keys.index(pair[0])<keys.index(pair[1])]
    return matches[0] if len(matches)==1 else None


def scenario_selection(text, count, context, goal):
    lines = [compact(line) for line in text.splitlines() if line.strip()]
    preferences = context.get('event_preferences') or goal.event_preferences
    if "With" in lines:
        following = lines[lines.index("With") + 1:]
        options = [line for line in following if line.startswith("我的目标是")
                   or line == "我想要得到佐岳鸣的认可"]
        if len(options) != 5 or len(set(options)) != 5:
            return None
        profile = context.get("support_profile") or {}
        identities = {str(card.get(key)) for card in profile.get("cards", [])
                      for key in ("character_id", "character_name", "name") if card.get(key)}
        resolved_identities = set(identities)
        for ident in identities:
            mapped_with = _resolve_with_character(ident, WITH_REWARDS.keys())
            if mapped_with:
                resolved_identities.add(mapped_with)
        trainee = str(context.get("trainee_id"))
        linked_trainee = (context.get("trainee_scenario_link") is True or trainee in LINK_TRAINEES
                          or trainee in set(WITH_REWARDS) - {"佐岳鸣"})
        rows = []
        gold_choice_list = preferences.get("with_gold_choices", [])
        custom = preferences.get("with_skill_values", {})
        for index, option in enumerate(options, 1):
            name = "佐岳鸣" if "佐岳鸣" in option or "佐岳梅" in option else option.removeprefix("我的目标是")
            name = _resolve_with_character(name, WITH_REWARDS.keys()) or name
            reward = WITH_REWARDS.get(name)
            row = {"index": index, "character": name, "score": 0., "future_hint_status": "unknown"}
            if reward:
                identity, gold, white, use = reward
                gold_eligible = (identity in resolved_identities or name in resolved_identities or index == 1 and linked_trainee
                                 or any(_resolve_with_character(choice, [name]) == name for choice in gold_choice_list))
                suitability = {"general": 1.1, "medium": 1., "longchamp": 1.3,
                               "long": .2, "risk": .5 if goal.risk_tolerance == "low" else .9}[use]
                if preferences.get("distance") == "long":
                    suitability = 1.1 if use == "long" else .3 if use == "medium" else suitability
                if preferences.get("course", "longchamp") != "longchamp" and use == "longchamp":
                    suitability = .2
                row.update(score=(20 if gold_eligible else 10) * suitability,
                           gold_skill_reference=gold, normal_skill_reference=white,
                           future_hint_status="gold_eligible" if gold_eligible else "normal_or_unverified_link",
                           future_hint_condition="Trust and expectation condition; not an observed reward")
                target_skill = gold if gold_eligible else white
                skill_keys = (target_skill, *_WITH_LEGACY_SKILL_ALIASES.get(target_skill, ()))
                override = next((custom[key] for key in (name, identity, *skill_keys) if key in custom), None)
                if override is None:
                    override = next(
                        (val for k, val in custom.items() if _resolve_with_character(k, [name]) == name),
                        None,
                    )
                if type(override) in (int, float) and math.isfinite(override):
                    row.update(score=override, preference_basis="configured_skill_value")
            rows.append(row)
        from core.auto_trainer.larc_with_priority import configured_priority
        priority = configured_priority(preferences)
        visible = [row['character'] for row in rows]
        preferred = next((resolved for name in priority if (resolved := _resolve_with_character(name, visible))), None)
        best = next((row for row in rows if row["character"] == preferred),
                    max(rows, key=lambda row: (row["score"], -row["index"])))
        return {"status": "selected", "reason": "larc_with_future_hint_preference",
                "choice_index": best["index"], "expected_event": ("With", *options),
                "effect_basis": "guide_reference_and_deck; future_not_observed", "candidates": rows,
                "with_priority": priority, "selected_character": best['character'],
                "with_choice_basis": "visible_character_priority" if preferred else "existing_automatic_policy"}
    title = acupuncture_title(lines)
    if title:
        if count == 2:
            pending = context.get("acupuncture_pending_choice")
            options=acupuncture_followup_options(lines)
            if options is None:
                return {'status':'blocked','reason':'acupuncture_followup_unreadable','choice_index':None,
                        'expected_event':None,'effect_basis':'two_followup_layouts_require_visible_option_identity','candidates':[]}
            index = 1 if options==NEEDLE_REFUSE or type(pending) is int and 1<=pending<=4 else 2
            return {"status": "selected", "reason": "acupuncture_confirm_refusal" if options==NEEDLE_REFUSE else "acupuncture_follow_selected_risk",
                    "choice_index": index, "expected_event": (title, *options),
                    "effect_basis": "previous_selected_option_or_cancel", "candidates": [],
                    "acupuncture_pending_choice": None}
        options = tuple(compact(option) for option in NEEDLE_OPTIONS)
        complete = count == 5 and all(option in lines for option in options)
        from core.auto_trainer.larc_acupuncture_policy import configured_policy,decide
        policy=configured_policy(preferences);observed=context.get('acupuncture_observation') or {}
        decision=decide(policy,day=observed.get('day'),vitality=observed.get('vitality'),
            date_basis=observed.get('basis'),vitality_basis=observed.get('vitality_basis'))
        index=decision['choice_index'] if complete else 5
        if not complete:decision={**decision,'choice_index':5,'reason':'acupuncture_incomplete_text_decline'}
        # An incomplete known event must not fall back to generic option 1.
        # Unexpected layouts still fail the planner's five-button check.
        return {"status": "selected", "reason": decision['reason'],
                "choice_index": index, "expected_event": (title, *options),
                "effect_basis": "default_decline; configurable_choice; incomplete_text_declines",
                "candidates": [], "acupuncture_pending_choice": index,
                'acupuncture_policy':policy,'acupuncture_decision':decision,'event_policy_version':'larc_acupuncture_v1'}
    return None
