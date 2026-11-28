"""Regression tests for CN official character, outfit, support card, and With naming alignment."""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = REPO_ROOT / "pacer" / "backend"


def _with_isolated_core(backend_dir: Path, callback):
    prefixes = ("core", "desktop", "app")
    saved_modules = {
        k: v
        for k, v in sys.modules.items()
        if any(k == p or k.startswith(f"{p}.") for p in prefixes)
    }
    saved_path = list(sys.path)
    saved_endpoint = os.environ.get("UMA_PUBLIC_ENDPOINT")
    try:
        for key in list(saved_modules):
            sys.modules.pop(key, None)
        sys.path.insert(0, str(REPO_ROOT / "backend"))
        if backend_dir != REPO_ROOT / "backend":
            sys.path.insert(0, str(backend_dir))
        os.environ["UMA_PUBLIC_ENDPOINT"] = "127.0.0.1:16384"
        return callback()
    finally:
        for key in [
            k
            for k in sys.modules
            if any(k == p or k.startswith(f"{p}.") for p in prefixes)
        ]:
            sys.modules.pop(key, None)
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path
        if saved_endpoint is None:
            os.environ.pop("UMA_PUBLIC_ENDPOINT", None)
        else:
            os.environ["UMA_PUBLIC_ENDPOINT"] = saved_endpoint


# ---------------------------------------------------------------------------
# 1. Roster parity, CN names, community aliases, and 157 official outfits
# ---------------------------------------------------------------------------


def test_rosters_aligned_with_skill_sources_and_cn_official_names() -> None:
    main_roster = json.loads(
        (REPO_ROOT / "backend/core/autonomous_training/plant_roster.json").read_text(encoding="utf-8-sig")
    )
    bridge_roster = json.loads(
        (PACER_BACKEND / "bridges/skills/backend/core/autonomous_training/plant_roster.json").read_text(
            encoding="utf-8-sig"
        )
    )
    pacer_roster = json.loads(
        (PACER_BACKEND / "core/autonomous_training/plant_roster.json").read_text(encoding="utf-8-sig")
    )
    assert main_roster == bridge_roster

    projected_expected = {
        "records": [{"id": r["id"], "name": r["name"], "aliases": r["aliases"]} for r in main_roster["records"]]
    }
    assert pacer_roster == projected_expected
    assert len(main_roster["records"]) == 157

    ss = json.loads((REPO_ROOT / "backend/data/parent_library/skill_sources.json").read_text(encoding="utf-8-sig"))
    ss_trainees = {}
    for sk in ss["skills"].values():
        if isinstance(sk, dict):
            for it in sk.get("trainees") or []:
                if isinstance(it, dict) and it.get("card_id"):
                    ss_trainees.setdefault(str(it["card_id"]), it)
    assert len(ss_trainees) == 157

    by_id = {r["id"]: r for r in main_roster["records"]}
    for cid, r in by_id.items():
        assert "待核" not in str(r.get("name_source", "")), f"Unverified note left on {cid}"
        assert "待核" not in str(r.get("outfit", "")), f"Placeholder outfit left on {cid}"
        raw_outfit = (ss_trainees[cid].get("outfit") or "").strip()
        expected_outfit = raw_outfit[1:-1] if (raw_outfit.startswith("[") and raw_outfit.endswith("]")) else raw_outfit
        assert r["outfit"] == expected_outfit, f"Outfit mismatch on {cid}: {r['outfit']} != {expected_outfit}"
        assert r["name"] not in r["aliases"]

    # Six official CN renames & preserved aliases
    assert by_id["102701"]["name"] == "目白莱恩" and "目白赖恩" in by_id["102701"]["aliases"]
    assert by_id["103102"]["name"] == "艾尼风神" and "艾尼斯风神" in by_id["103102"]["aliases"]
    assert by_id["105201"]["name"] == "春乌菈菈" and {"乌拉拉", "春乌拉拉"} <= set(by_id["105201"]["aliases"])
    assert by_id["106201"]["name"] == "待兼唐怀瑟" and "待兼诗歌剧" in by_id["106201"]["aliases"]
    assert by_id["109101"]["name"] == "维普露斯" and {"比布鲁斯", "强击", "维普罗斯"} <= set(by_id["109101"]["aliases"])
    assert by_id["110701"]["name"] == "跳舞城" and {"踢踏城市", "踢踏舞城"} <= set(by_id["110701"]["aliases"])

    # Highlighted official outfits + 100602 legacy outfit alias
    assert by_id["100102"]["outfit"] == "雀跃♪维他命心拍"
    assert by_id["101501"]["outfit"] == "O sole suo！"
    assert by_id["102901"]["outfit"] == "可可爱爱的六出冰花"
    assert by_id["108601"]["outfit"] == "墨瑙魅线"
    assert by_id["109801"]["outfit"] == "阴阳八卦☆开运服"
    assert "奇迹的白星" in by_id["100602"].get("outfit_aliases", [])


# ---------------------------------------------------------------------------
# 2. name_match bidirectional resolution without polluting skill similarity
# ---------------------------------------------------------------------------


def test_name_match_parity_and_bidirectional_aliases() -> None:
    main_text = (REPO_ROOT / "backend/core/name_match.py").read_text(encoding="utf-8")
    pacer_text = (PACER_BACKEND / "core/name_match.py").read_text(encoding="utf-8")
    assert main_text == pacer_text

    def _run():
        nm = importlib.import_module("core.name_match")
        cn = importlib.import_module("core.character_names")
        # 菈->拉 is intentionally NOT in global _TRADITIONAL / name_similarity
        assert nm.name_similarity("菈", "拉") == 0.0

        pairs = [
            ("目白莱恩", "目白赖恩", "1027"),
            ("艾尼风神", "艾尼斯风神", "1031"),
            ("春乌菈菈", "春乌拉拉", "1052"),
            ("春乌菈菈", "乌拉拉", "1052"),
            ("待兼唐怀瑟", "待兼诗歌剧", "1062"),
            ("维普露斯", "比布鲁斯", "1091"),
            ("维普露斯", "维普罗斯", "1091"),
            ("跳舞城", "踢踏城市", "1107"),
            ("跳舞城", "踢踏舞城", "1107"),
            ("里见皇冠", "里见皇冠", "1088"),
            ("东海帝王", "东海帝皇", "1003"),
            ("黄金城", "黄金城市", "1040"),
            ("克里斯象征", "吉兆", "1083"),
            ("北幸樽前", "北港火山", "1099"),
        ]
        for official, legacy, expected_cid in pairs:
            assert cn.match_character_name(legacy, official)
            assert cn.match_character_name(official, legacy)
            cid, score, basis = nm.match_character(legacy, cn.character_candidates())
            assert cid == expected_cid
            assert nm.is_confident(score, basis)

    _with_isolated_core(PACER_BACKEND, _run)
    _with_isolated_core(REPO_ROOT / "backend", _run)


# ---------------------------------------------------------------------------
# 3. plant_catalog, observer, daily_legend, and interface.json
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend_dir",
    [REPO_ROOT / "backend", PACER_BACKEND / "bridges/skills/backend"],
)
def test_daily_legend_plant_catalog_and_observer_compat(backend_dir: Path) -> None:
    def _run():
        dl_cat = importlib.import_module("core.daily_legend.catalog")
        assert dl_cat.opponent(15)["name"] == "目白莱恩"

        pc = importlib.import_module("core.autonomous_training.plant_catalog")
        assert pc.TRAINEES["urara"] == "春乌菈菈"
        if hasattr(pc, "trainee_identity"):
            ident = pc.trainee_identity("urara")
            assert ident["name"] == "春乌菈菈"
            assert ident["outfit"] == "乌菈菈一等奖♪"

        obs_mod = importlib.import_module("core.autonomous_training.observer")
        import cv2
        import unittest.mock as mock

        img = np.full((1280, 720, 3), 240, dtype=np.uint8)
        ok, buf = cv2.imencode(".png", img)
        assert ok
        for screen_text in ("春乌菈菈", "春乌拉拉"):
            tokens = [
                {"text": "选择养成优俊少女", "bbox": [100, 30, 340, 70], "confidence": 0.99},
                {"text": screen_text, "bbox": [50, 415, 210, 445], "confidence": 0.98},
            ]
            with (
                mock.patch("core.autonomous_training.trainee.bakushin_candidate", return_value=None),
                mock.patch("core.autonomous_training.trainee.scarlet_candidate", return_value=None),
                mock.patch("core.autonomous_training.trainee.initial_candidate", return_value=None),
            ):
                obs = obs_mod.observe(buf.tobytes(), tokens)
            assert obs["page"] == "trainee"
            assert obs["selected_trainee_name"] == "春乌菈菈"

    _with_isolated_core(backend_dir, _run)


def test_interface_json_preserves_saved_keys_and_updates_display_labels() -> None:
    iface = json.loads((REPO_ROOT / "interface.json").read_text(encoding="utf-8-sig"))
    raw = json.dumps(iface, ensure_ascii=False)
    assert "国服译名待核实" not in raw
    assert "国服衣装名待核对" not in raw

    cases = iface["option"]["育成马娘"]["cases"]
    urara_case = next(c for c in cases if c.get("name") == "乌拉拉（初始）")
    assert urara_case["label"] == "春乌菈菈（初始）"

    by_name = {c.get("name"): c for c in cases if isinstance(c, dict)}
    assert by_name["preview_102701"]["label"].startswith("目白莱恩 · ")
    assert "目白赖恩" in by_name["preview_102701"]["description"]
    assert by_name["preview_110701"]["label"] == "跳舞城 · GLITTER！"


# ---------------------------------------------------------------------------
# 4. cut_farming bidirectional identity & validation_mode compat
# ---------------------------------------------------------------------------


def test_cut_farming_bidirectional_legacy_and_official_names() -> None:
    if str(PACER_BACKEND) not in sys.path:
        sys.path.insert(0, str(PACER_BACKEND))
    from core.auto_trainer import cut_farming as cf

    cf._character_identities.cache_clear()
    cf._canonical_character_names.cache_clear()

    # Fixed preset 103102 now uses CN official '艾尼风神' and accepts both observed forms
    cfg_1031 = cf.CutFarmingConfig(character_id="103102")
    assert cf.expected_character_name(cfg_1031) == "艾尼风神"
    assert cf.character_identity_evidence(cfg_1031, "艾尼斯风神")["status"] == "matched"
    assert cf.character_identity_evidence(cfg_1031, "艾尼风神")["status"] == "matched"

    # Saved legacy validation_mode names resolve seamlessly to canonical IDs
    legacy_to_official = [
        ("目白赖恩", "目白莱恩", 1027),
        ("艾尼斯风神", "艾尼风神", 1031),
        ("乌拉拉", "春乌菈菈", 1052),
        ("待兼诗歌剧", "待兼唐怀瑟", 1062),
        ("比布鲁斯", "维普露斯", 1091),
        ("踢踏城市", "跳舞城", 1107),
    ]
    for legacy, official, cid in legacy_to_official:
        cfg = cf.CutFarmingConfig(
            character_id="validation",
            validation_mode=True,
            validation_character_name=legacy,
        )
        assert cf.expected_character_name(cfg) == official
        ev = cf.character_identity_evidence(cfg, official)
        assert ev["status"] == "matched"
        assert ev["expected_character_id"] == cid
        assert ev["observed_character_id"] == cid


# ---------------------------------------------------------------------------
# 5. support_library aliases & larc_scenario_events With resolution
# ---------------------------------------------------------------------------


def test_support_library_search_finds_community_and_legacy_aliases() -> None:
    if str(PACER_BACKEND) not in sys.path:
        sys.path.insert(0, str(PACER_BACKEND))
    from core.support_card_knowledge import SupportCardKnowledgeLibrary

    lib = SupportCardKnowledgeLibrary.load()
    assert lib.cards["10022"]["character_name_zh_cn"] == "桐生院葵"
    assert lib.cards["20021"]["character_name_zh_cn"] == "桐生院葵"

    expected_hits = [
        ("大鸣大放", "1108"),
        ("丛林口袋", "1094"),
        ("维露仙娜", "1090"),
        ("罗伊斯罗伊斯", "1103"),
        ("佐岳梅", "9043"),
        ("佐岳", "9043"),
        ("桐生院葵", "9004"),
        ("桐生院训练员", "9004"),
        ("待兼诗歌剧", "1062"),
        ("比布鲁斯", "1091"),
    ]
    for query, expected_cid in expected_hits:
        found = lib.search_cards(query)
        assert found, f"No support cards matched query {query!r}"
        assert any(str(c["character_id"]) == expected_cid for c in found)


def test_larc_with_priority_resolves_mei_and_legacy_aliases_and_cn_skills() -> None:
    if str(PACER_BACKEND) not in sys.path:
        sys.path.insert(0, str(PACER_BACKEND))
    from core.auto_trainer import larc_scenario_events as lse

    # WITH_REWARDS uses CN official skill references
    assert lse.WITH_REWARDS["佐岳鸣"][1:3] == ("隆尚奇才", "隆尚赛场○")
    assert lse.WITH_REWARDS["天狼星象征"][1:3] == ("瞬息万变", "中距离直线○")
    assert lse.WITH_REWARDS["黄金船"][1:3] == ("拼死决心", "不遗余力")

    with_screen = "\n".join(
        [
            "With",
            "我的目标是天狼星象征",
            "我的目标是黄金船",
            "我的目标是中山庆典",
            "我的目标是曼城茶座",
            "我想要得到佐岳鸣的认可",
        ]
    )
    goal = SimpleNamespace(event_preferences={}, risk_tolerance="low")

    for alias in ("佐岳梅", "佐岳"):
        res = lse.scenario_selection(
            with_screen,
            5,
            {"event_preferences": {"with_priority": [alias]}},
            goal,
        )
        assert res is not None
        assert res["choice_index"] == 5
        assert res["selected_character"] == "佐岳鸣"
        assert res["with_choice_basis"] == "visible_character_priority"

    # Legacy JP skill key in with_skill_values still overrides candidate score
    res_legacy_skill = lse.scenario_selection(
        with_screen,
        5,
        {
            "event_preferences": {
                "with_priority": [],
                "with_skill_values": {"博打うち": 999},
                "with_gold_choices": ["中山庆典"],
            }
        },
        goal,
    )
    assert res_legacy_skill is not None
    assert res_legacy_skill["choice_index"] == 3
    assert res_legacy_skill["selected_character"] == "中山庆典"
