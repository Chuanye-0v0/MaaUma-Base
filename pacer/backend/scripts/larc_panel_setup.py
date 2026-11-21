"""Local panel preparation. No device connection, input or career creation."""
from datetime import datetime
from copy import deepcopy
import json
import math
from pathlib import Path
import uuid

from core.auto_trainer.config import UGParentGoal, validate_goal_config
from core.auto_trainer.route_transition_session import RouteTransitionSession
from core.auto_trainer.skip_session import RunSkipSession
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json

ROOT = Path(__file__).resolve().parents[1]
STATS = {"speed": "速度", "stamina": "耐力", "power": "力量", "guts": "根性", "wisdom": "智力"}
DEFAULT_TARGETS = dict(speed=1550, stamina=1200, power=1200, guts=1200, wisdom=1200)
DEVICES = {"MuMu 安卓设备": 16384, "MuMu 安卓设备-1": 16416, "MuMu 安卓设备 2": 16448}
STRATEGIES = {"统一基线": "baseline"}
LEGACY_STRATEGIES = {"阿尔法": "alpha", "贝塔": "beta"}
MEI_RULE_SOURCE = "https://umamusu.wiki/Game:SSR_Satake_Mei_(L'aubeは迫りて)"
MEI_EVENT_BONUS = (15, 16, 18, 20, 20)


def save_ramonu_present(run_root, present):
    from core.auto_trainer.run_support_profile import load_profile, save_profile, SCHEMA
    root = Path(run_root)
    profile = load_profile(root, root.name)
    source = {"kind": "user_panel_ramonu_confirmation", "note": "用户确认本局携带智高峰SSR燦爛"}
    if profile is None:
        if not present:
            return
        profile = {"schema_version": SCHEMA, "run_session_id": root.name,
                   "source": source, "cards": [], "max_vitality": None}
    if not present:
        # Unchecking removes a previous manual addition, not imported deck facts.
        cards = [card for card in profile["cards"] if not (
            str(card.get("card_id")) == "30134" and card.get("source", {}).get("kind") == source["kind"])]
        if len(cards) != len(profile["cards"]):
            profile["cards"] = cards
            save_profile(root, root.name, profile)
        return
    card = next((card for card in profile["cards"] if str(card.get("card_id")) == "30134"), None)
    if card is None:
        occupied = {card["slot"] for card in profile["cards"]}
        slot = next((index for index in range(6) if index not in occupied), None)
        if slot is None:
            if profile.get('declared_profile'):
                from core.auto_trainer.run_support_profile import record_legacy_confirmation
                record_legacy_confirmation(root,root.name,profile,'30134',source);return
            raise ValueError("已有完整配卡资料里没有智高峰，请核对配卡。")
        card = {"slot": slot, "slot_basis": "partial_profile_ordinal_not_observed_deck_position",
                "source": source}
        profile["cards"].append(card)
    card.update(card_id="30134", character_id="mejiro_ramonu", rarity="SSR", type="wisdom",
                title="燦爛", presence_confirmation=source)
    # Level, limit breaks and bonus percentages remain unknown unless supplied.
    save_profile(root, root.name, profile)


def parse_mei_breaks(value):
    if value is None or not str(value).strip():
        return None
    try:
        breaks = int(str(value).strip())
    except (ValueError, TypeError):
        raise ValueError("黄帽突破数请填 0～4，或留空。") from None
    if not 0 <= breaks <= 4:
        raise ValueError("黄帽突破数请填 0～4，或留空。")
    return breaks


def save_mei_breaks(run_root, value):
    """User explicitly confirmed max level for this SSR at its supplied LB."""
    breaks = parse_mei_breaks(value)
    if breaks is None:
        return
    from core.auto_trainer.run_support_profile import load_profile, save_profile, SCHEMA
    root = Path(run_root)
    profile = load_profile(root, root.name)
    source = {"kind": "user_panel_input", "note": "用户确认该破数已升到当前等级上限"}
    if profile is None:
        profile = {"schema_version": SCHEMA, "run_session_id": root.name,
                   "source": source, "cards": [], "max_vitality": None}
    card = next((card for card in profile["cards"] if card.get("character_id") == "satake_mei"), None)
    if card is None:
        occupied = {card["slot"] for card in profile["cards"]}
        slot = next((index for index in range(6) if index not in occupied), None)
        if slot is None:
            if profile.get('declared_profile'):
                from core.auto_trainer.run_support_profile import record_legacy_confirmation
                record_legacy_confirmation(root,root.name,profile,'30160',source);return
            raise ValueError("已有完整配卡资料里没有黄帽，请核对配卡。")
        card = {"slot": slot, "slot_basis": "partial_profile_ordinal_not_observed_deck_position"}
        profile["cards"].append(card)
    card.update(card_id="30160", character_id="satake_mei", rarity="SSR", type="friend",
                level=30 + 5 * breaks, limit_breaks=breaks, source=source,
                actual_effects={"event_recovery_percent": 40 + 10 * breaks,
                                "event_effectiveness_percent": MEI_EVENT_BONUS[breaks]},
                actual_effects_source={**source, "rule_source": MEI_RULE_SOURCE})
    save_profile(root, root.name, profile)


def parse_strategy_options(strategy, failure_limit):
    from core.auto_trainer.larc_policy import VARIANT_BRANCHES
    strategy = LEGACY_STRATEGIES.get(strategy, STRATEGIES.get(strategy, strategy))
    if strategy not in VARIANT_BRANCHES:
        raise ValueError("请选择统一基线；旧策略仅用于恢复已有记录。")
    try:
        limit = float(str(failure_limit).strip().removesuffix("%"))
    except (TypeError, ValueError):
        raise ValueError("训练失败率阈值请填写 0～100 的百分比。") from None
    if not math.isfinite(limit) or not 0 <= limit <= 100:
        raise ValueError("训练失败率阈值请填写 0～100 的百分比。")
    return strategy, limit


def prepare_panel_policy(policy=None, *, strategy="baseline", task_mode="career"):
    """An explicit save/new baseline task upgrades; a legacy resume stays old."""
    from core.auto_trainer.larc_policy import normalize_larc_policy, upgrade_larc_policy
    from core.auto_trainer.larc_strategy_versions import PRESET_REVISIONS
    explicit_revision = PRESET_REVISIONS.get(strategy)
    strategy, _ = parse_strategy_options("baseline" if explicit_revision else strategy, 25)
    if policy is not None and not isinstance(policy, dict):
        raise ValueError("策略高级配置应为对象。")
    original = deepcopy(policy)
    if strategy == "baseline":
        from core.auto_trainer.larc_strategy_versions import CURRENT_REVISION, LEGACY_REVISION
        revision = explicit_revision or (original.get('revision', LEGACY_REVISION) if original is not None else CURRENT_REVISION)
        result = upgrade_larc_policy(original, task_mode, revision=revision)
    else:
        previous = {**(original or {}), "variant": strategy}
        result = {"policy": normalize_larc_policy(previous, task_mode), "migration": None}
    default = "junior_cutoff_discard" if task_mode == "cut" else "full_career"
    effective = result["policy"].get("ss_planning_horizon", "full_career")
    explicit = isinstance(original, dict) and "ss_planning_horizon" in original
    result["task_goal"] = {"schema_version": "uma.larc_task_goal.v1", "task_mode": task_mode,
        "default_ss_planning_horizon": default, "effective_ss_planning_horizon": effective,
        "horizon_source": "advanced_explicit" if explicit else
            "task_default" if strategy == "baseline" else "legacy_default",
        "advanced_overrides_task_default": explicit and effective != default,
        "note": "显式高级SS窗口优先于任务默认；不改变目标、失败率或资源授权。"}
    return result


def save_policy_migration(root, prepared, *, label="goal", previous_settings=None):
    """Keep migration outside strict goal.larc_policy; never called by reads."""
    root = Path(root)
    migration = prepared.get("migration")
    if migration is not None and migration.get("changes"):
        directory = root / "policy-migrations"
        name = f"{label}-{datetime.now():%Y%m%d_%H%M%S_%f}-{uuid.uuid4().hex[:6]}.json"
        _atomic_write_json(directory / name, {**deepcopy(migration),
            "task_goal": deepcopy(prepared["task_goal"]),
            **({"previous_settings": deepcopy(previous_settings)} if previous_settings is not None else {})})
    return prepared["policy"]


def upgrade_panel_settings(settings, *, task_mode, previous=None):
    """User save adopts baseline while retaining all explicit advanced values."""
    result = deepcopy(settings)
    old = previous if isinstance(previous, dict) else {}
    policy = result.get("larc_policy", old.get("larc_policy"))
    if policy is None and old:
        policy = {"variant": old.get("strategy", result.get("strategy", "baseline"))}
    prepared = prepare_panel_policy(policy, strategy="baseline", task_mode=task_mode)
    result.update(strategy="baseline", larc_policy=prepared["policy"])
    return result, prepared


def parse_targets(values):
    result = {}
    for key, label in STATS.items():
        try:
            value = int(str(values[key]).strip())
        except (KeyError, ValueError, TypeError):
            raise ValueError(f"{label}目标请填写整数。") from None
        if not 0 <= value <= 3000:
            raise ValueError(f"{label}目标请填写 0～3000，0 表示不特别追求。")
        result[key] = value
    return result


def base_goal():
    return json.loads((ROOT / "config/larc_device0_validation_20260929.goal.json").read_text(encoding="utf-8-sig"))


def parse_early_skill_allowlist(value):
    """One exact name/ID per line; commas remain part of the name."""
    lines=value.splitlines() if isinstance(value,str) else value
    if not isinstance(lines,list) or any(not isinstance(v,str) for v in lines):raise ValueError("高峰技能清单请每行填写一个名称。")
    return list(dict.fromkeys(v.strip() for v in lines if v.strip()))


def parse_arc_skill_policy(value):
    from core.auto_trainer.larc_target_skills import normalize_arc_policy
    from scripts.larc_skill_choices import validate_choices
    policy = normalize_arc_policy(value)
    validate_choices(policy.get("classic_allowlist", []))
    return policy


def update_goal(run_root, targets, *, strategy=None, failure_limit=None, early_skill_allowlist=None,
                arc_policy=None, strategy_refinement=None, training_balance_v2=None,
                larc_policy=None, task_mode="career", mid_skill_learning=None, race_retry=None,
                running_style=None, race_running_styles=None, custom_races=None):
    from core.auto_trainer.larc_strategy_versions import PRESET_REVISIONS
    explicit_revision = PRESET_REVISIONS.get(strategy)
    if explicit_revision is not None:
        strategy = "baseline"
    path = Path(run_root) / "goal.json"
    existing_goal = path.is_file()
    goal = json.loads(path.read_text(encoding="utf-8-sig")) if existing_goal else base_goal()
    previous_goal = deepcopy(goal)
    goal["target_stats"] = parse_targets(targets)
    original_policy = deepcopy(goal.get("larc_policy", {}))
    policy = {**original_policy, **deepcopy(larc_policy or {})}
    if strategy is not None or failure_limit is not None:
        strategy, limit = parse_strategy_options(strategy or policy.get("variant", "alpha"),
            goal.get("training_failure_rate_limit", 25) if failure_limit is None else failure_limit)
        goal["training_failure_rate_limit"] = limit
    if strategy_refinement is not None:
        policy['strategy_refinement'] = strategy_refinement
    if training_balance_v2 is not None:
        policy['training_balance_v2'] = training_balance_v2
    if explicit_revision is not None:
        policy.update(variant="baseline", revision=explicit_revision)
    selected_strategy = strategy or policy.get("variant", "alpha")
    if not existing_goal and larc_policy is None and explicit_revision is None and selected_strategy == 'baseline':
        from core.auto_trainer.larc_policy import normalize_larc_policy
        policy = normalize_larc_policy(None, task_mode)
    if not existing_goal:
        from core.auto_trainer.larc_strategy_versions import new_goal_policy_defaults
        policy = new_goal_policy_defaults(policy)
    prepared = prepare_panel_policy(policy, strategy=selected_strategy, task_mode=task_mode)
    goal["larc_policy"] = prepared["policy"]
    if early_skill_allowlist is not None:
        goal.setdefault("skill_timing",{})["early_skill_allowlist"]=parse_early_skill_allowlist(early_skill_allowlist)
    if mid_skill_learning is not None:
        from core.auto_trainer.larc_support_skill_startup import normalize_mid_skill_learning,new_mid_skill_learning
        settings=normalize_mid_skill_learning(mid_skill_learning) if existing_goal else new_mid_skill_learning(mid_skill_learning)
        if existing_goal:
            settings["mode"]=normalize_mid_skill_learning(previous_goal.get("skill_timing",{}).get("mid_skill_learning"))["mode"]
        goal.setdefault("skill_timing",{})["mid_skill_learning"]=settings
    if arc_policy is not None:
        goal.setdefault("skill_timing", {})["arc_policy"] = parse_arc_skill_policy(arc_policy)
    if race_retry is not None:
        from core.auto_trainer.race_retry_budget import normalize as normalize_retry
        goal['race_retry']=normalize_retry(race_retry)
    if running_style is not None:
        from core.auto_trainer.larc_custom_race_catalog import normalize_running_style
        goal['running_style']=normalize_running_style(running_style)
    if race_running_styles is not None:
        from core.auto_trainer.larc_custom_race_catalog import normalize_race_running_styles
        goal['race_running_styles']=normalize_race_running_styles(race_running_styles)
    if custom_races is not None:
        from core.auto_trainer.larc_custom_race_catalog import normalize_custom_races
        goal['custom_races']=normalize_custom_races(custom_races)
    validate_goal_config(UGParentGoal.from_dict(goal))
    save_policy_migration(Path(run_root) / "session", prepared,
        previous_settings={"goal": previous_goal})
    _atomic_write_json(Path(run_root) / "session/larc_task_goal.json", prepared["task_goal"])
    _atomic_write_json(path, goal)


def create_panel_run(*, device_port, targets, runs_dir=None, strategy="baseline", failure_limit=25, mei_breaks=None, with_ramonu=False, early_skill_allowlist=None, arc_policy=None, strategy_refinement=True, training_balance_v2=None, larc_policy=None, task_mode="career", mid_skill_learning=None,support_profile=None,race_retry=None,running_style=None,race_running_styles=None,custom_races=None):
    from core.cut_portable_runtime import device_ports
    if device_port not in device_ports():
        raise ValueError("请选择面板中的 MuMu 设备。")
    if type(strategy_refinement) is not bool:
        raise ValueError('strategy_refinement_must_be_bool')
    if training_balance_v2 is None:
        training_balance_v2 = strategy_refinement
    if type(training_balance_v2) is not bool:
        raise ValueError('training_balance_v2_must_be_bool')
    from core.auto_trainer.run_support_profile import normalize_declaration,freeze_declaration
    declaration=normalize_declaration(support_profile)
    targets = parse_targets(targets)
    from core.auto_trainer.larc_strategy_versions import PRESET_REVISIONS
    explicit_revision = PRESET_REVISIONS.get(strategy)
    strategy, failure_limit = parse_strategy_options("baseline" if explicit_revision else strategy, failure_limit)
    if explicit_revision is not None:
        larc_policy = {**(larc_policy or {}), "variant": "baseline", "revision": explicit_revision}
    mei_breaks = parse_mei_breaks(mei_breaks)
    if arc_policy is not None:
        arc_policy = parse_arc_skill_policy(arc_policy)
    parent = Path(runs_dir or ROOT / "runs").resolve()
    name = f"larc_panel_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    root = parent / name
    (root / "session").mkdir(parents=True, exist_ok=False)
    update_goal(root, targets, strategy=strategy, failure_limit=failure_limit,early_skill_allowlist=early_skill_allowlist, arc_policy=arc_policy,
        strategy_refinement=strategy_refinement if strategy!='baseline' else None,
        training_balance_v2=training_balance_v2 if strategy!='baseline' else None,
        larc_policy=larc_policy, task_mode=task_mode, mid_skill_learning=mid_skill_learning,race_retry=race_retry,
        running_style=running_style, race_running_styles=race_running_styles, custom_races=custom_races)
    _atomic_write_json(root / "progress.json", {"run_session_id": name,
        "device": f"127.0.0.1:{device_port}", "actions_completed": 0, "next_turn": 1})
    RouteTransitionSession.new(name).save(root / "session/transition_session.json")
    RunSkipSession.new(name).save(root / "session/skip_session.json")
    _atomic_write_json(root / "session/larc_member_roster.json", {
        "schema_version": "larc_member_roster.v1", "run_session_id": name,
        "last_observed_turn": -1, "members": [], "information_scan": {"status": "never_scanned"},
        "ss_executions": [], "last_ss_preview": None, "ss_execution_participants": [],
        "complete_roster_verified": False, "decision_safe": False})
    from core.auto_trainer.larc_skill_driver import prepare_new_run
    if declaration is not None:freeze_declaration(root,name,declaration)
    prepare_new_run(root,f"127.0.0.1:{device_port}")
    save_mei_breaks(root, mei_breaks)
    save_ramonu_present(root, with_ramonu)
    return root
