"""Regression tests for CN Duramente (30178) official titles & anti-sink target-skill chain break."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))
if str(PACER_BACKEND.resolve()) not in sys.path:
    sys.path.insert(0, str(PACER_BACKEND.resolve()))


def _build_duramente_library():
    from core.skill_knowledge import SkillKnowledgeLibrary
    from core.support_card_knowledge import SupportCardKnowledgeLibrary

    skills = SkillKnowledgeLibrary(
        {
            "schema_version": "uma.cn_larc_skill_library.v1",
            "skills": [
                {
                    "skill_id": "cn:永不言弃",
                    "db_id": "202721",
                    "name_zh_cn": "永不言弃",
                    "name_ja": "ネバーギブアップ",
                    "rarity": "gold",
                    "skill_kind": "purchasable",
                    "relations": {},
                    "evolution": [],
                    "character_sources": [],
                },
                {
                    "skill_id": "cn:点火",
                    "db_id": "202602",
                    "name_zh_cn": "点火",
                    "name_ja": "イグニッション",
                    "rarity": "normal",
                    "skill_kind": "purchasable",
                    "relations": {},
                    "evolution": [],
                    "character_sources": [],
                },
            ],
        }
    )

    hint_never_give_up_1 = {
        "db_id": "202721",
        "skill_id": "cn:永不言弃",
        "name_zh_cn": "永不言弃",
        "name_ja": "ネバーギブアップ",
        "operation": "hint",
        "hint_level_gain": 1,
    }
    hint_never_give_up_3 = {**hint_never_give_up_1, "hint_level_gain": 3}

    payload = {
        "schema_version": "uma.support_knowledge.v1",
        "dataset_version": "test_068",
        "skill_library_version": "test_068",
        "cards": [
            {
                "card_id": "30178",
                "character_id": "1108",
                "name_zh_cn": "[血脉初现]鸣声雷动",
                "character_name_zh_cn": "鸣声雷动",
                "name_ja": "[血脈の胎動]ドゥラメンテ",
                "rarity": "SSR",
                "type": "speed",
                "aliases": [],
            }
        ],
        "events": [
            {
                "event_id": "support:30178:story:801108001",
                "story_id": "801108001",
                "support_card_id": "30178",
                "owner_character_id": "1108",
                "kind": "random",
                "stage": None,
                "chain_id": None,
                "is_choice": True,
                "titles": {"ja": "流すべきこの汗", "zh_cn": "应该流的这滴汗"},
                "next_event_ids": [],
                "options": [
                    {
                        "index": 1,
                        "texts": {"zh_cn": ""},
                        "chain_transition": None,
                        "opens_chain_ids": [],
                        "closes_chain_ids": [],
                        "conditional_closes_chain_ids": [],
                        "branches": [
                            {
                                "branch_id": "option_1:branch_1",
                                "probability": 1.0,
                                "skills": [],
                                "unknown_rewards": [],
                                "chain_end": False,
                                "outing_unlock": False,
                                "effects": [
                                    {"kind": "speed", "op": "add", "value": 10},
                                    {"kind": "guts", "op": "add", "value": 10},
                                    {"kind": "bond", "op": "add", "value": 5, "owner_id": "30178"},
                                ],
                            }
                        ],
                    },
                    {
                        "index": 2,
                        "texts": {"zh_cn": ""},
                        "chain_transition": None,
                        "opens_chain_ids": [],
                        "closes_chain_ids": [],
                        "conditional_closes_chain_ids": [],
                        "branches": [
                            {
                                "branch_id": "option_2:branch_1",
                                "probability": 1.0,
                                "skills": [],
                                "unknown_rewards": [],
                                "chain_end": False,
                                "outing_unlock": False,
                                "effects": [
                                    {"kind": "energy", "op": "add", "value": 25},
                                    {"kind": "bond", "op": "add", "value": 5, "owner_id": "30178"},
                                ],
                            }
                        ],
                    },
                ],
            },
            {
                "event_id": "support:30178:story:801108002",
                "story_id": "801108002",
                "support_card_id": "30178",
                "owner_character_id": "1108",
                "kind": "random",
                "stage": None,
                "chain_id": None,
                "is_choice": True,
                "titles": {"ja": "追うべき背中", "zh_cn": "应该追逐的背影"},
                "next_event_ids": [],
                "options": [
                    {
                        "index": 1,
                        "texts": {"zh_cn": ""},
                        "chain_transition": None,
                        "opens_chain_ids": [],
                        "closes_chain_ids": [],
                        "conditional_closes_chain_ids": [],
                        "branches": [
                            {
                                "branch_id": "option_1:branch_1",
                                "probability": 1.0,
                                "skills": [],
                                "unknown_rewards": [],
                                "chain_end": False,
                                "outing_unlock": False,
                                "effects": [{"kind": "stamina", "op": "add", "value": 15}],
                            }
                        ],
                    },
                    {
                        "index": 2,
                        "texts": {"zh_cn": ""},
                        "chain_transition": None,
                        "opens_chain_ids": [],
                        "closes_chain_ids": [],
                        "conditional_closes_chain_ids": [],
                        "branches": [
                            {
                                "branch_id": "option_2:branch_1",
                                "probability": 1.0,
                                "skills": [{"db_id": "202602", "skill_id": "cn:点火", "name_zh_cn": "点火", "operation": "hint", "hint_level_gain": 1}],
                                "unknown_rewards": [],
                                "chain_end": False,
                                "outing_unlock": False,
                                "effects": [{"kind": "skill_hint", "op": "add", "value": 1, "skill_id": "cn:点火", "db_id": "202602", "name_zh_cn": "点火"}],
                            }
                        ],
                    },
                ],
            },
            {
                "event_id": "support:30178:story:830178001",
                "story_id": "830178001",
                "support_card_id": "30178",
                "owner_character_id": "1108",
                "kind": "continuous",
                "stage": 1,
                "chain_id": "support:30178:continuous",
                "is_choice": False,
                "titles": {"ja": "Vol.1 『ストイックの向こう側』", "zh_cn": "Vol.1 《克己的彼岸》"},
                "next_event_ids": ["support:30178:story:830178002"],
                "options": [
                    {
                        "index": 1,
                        "texts": {"zh_cn": ""},
                        "chain_transition": "continue",
                        "opens_chain_ids": [],
                        "closes_chain_ids": [],
                        "conditional_closes_chain_ids": [],
                        "branches": [
                            {
                                "branch_id": "option_1:branch_1",
                                "probability": 1.0,
                                "skills": [],
                                "unknown_rewards": [],
                                "chain_end": False,
                                "outing_unlock": False,
                                "effects": [{"kind": "speed", "op": "add", "value": 5}],
                            }
                        ],
                    }
                ],
            },
            {
                "event_id": "support:30178:story:830178002",
                "story_id": "830178002",
                "support_card_id": "30178",
                "owner_character_id": "1108",
                "kind": "continuous",
                "stage": 2,
                "chain_id": "support:30178:continuous",
                "is_choice": True,
                "titles": {"ja": "Vol.2 『脈々と』", "zh_cn": "Vol.2 《连绵不绝》"},
                "next_event_ids": ["support:30178:story:830178003"],
                "options": [
                    {
                        "index": 1,
                        "texts": {"zh_cn": ""},
                        "chain_transition": "continue",
                        "opens_chain_ids": [],
                        "closes_chain_ids": [],
                        "conditional_closes_chain_ids": [],
                        "branches": [
                            {
                                "branch_id": "option_1:branch_1",
                                "probability": 1.0,
                                "skills": [],
                                "unknown_rewards": [],
                                "chain_end": False,
                                "outing_unlock": False,
                                "effects": [
                                    {"kind": "energy", "op": "add", "value": 15},
                                    {"kind": "speed", "op": "add", "value": 15},
                                    {"kind": "bond", "op": "add", "value": 5, "owner_id": "30178"},
                                ],
                            }
                        ],
                    },
                    {
                        "index": 2,
                        "texts": {"zh_cn": ""},
                        "chain_transition": "end",
                        "opens_chain_ids": [],
                        "closes_chain_ids": ["support:30178:continuous"],
                        "conditional_closes_chain_ids": [],
                        "branches": [
                            {
                                "branch_id": "option_2:branch_1",
                                "probability": 1.0,
                                "skills": [hint_never_give_up_1],
                                "unknown_rewards": [],
                                "chain_end": True,
                                "outing_unlock": False,
                                "effects": [
                                    {"kind": "speed", "op": "add", "value": 7},
                                    {"kind": "power", "op": "add", "value": 7},
                                    {"kind": "skill_hint", "op": "add", "value": 1, "skill_id": "cn:永不言弃", "db_id": "202721", "name_zh_cn": "永不言弃"},
                                    {"kind": "bond", "op": "add", "value": 5, "owner_id": "30178"},
                                    {"kind": "chain_end", "op": "set", "value": True},
                                ],
                            }
                        ],
                    },
                ],
            },
            {
                "event_id": "support:30178:story:830178003",
                "story_id": "830178003",
                "support_card_id": "30178",
                "owner_character_id": "1108",
                "kind": "continuous",
                "stage": 3,
                "chain_id": "support:30178:continuous",
                "is_choice": False,
                "titles": {"ja": "Vol.3 『その名ドゥラメンテ』", "zh_cn": "Vol.3 《其名为鸣声雷动》"},
                "next_event_ids": [],
                "options": [
                    {
                        "index": 1,
                        "texts": {"zh_cn": ""},
                        "chain_transition": "complete",
                        "opens_chain_ids": [],
                        "closes_chain_ids": [],
                        "conditional_closes_chain_ids": [],
                        "branches": [
                            {
                                "branch_id": "option_1:branch_1",
                                "probability": 1.0,
                                "skills": [hint_never_give_up_3],
                                "unknown_rewards": [],
                                "chain_end": False,
                                "outing_unlock": False,
                                "effects": [
                                    {"kind": "energy", "op": "add", "value": 15},
                                    {"kind": "speed", "op": "add", "value": 20},
                                    {"kind": "power", "op": "add", "value": 20},
                                    {"kind": "skill_hint", "op": "add", "value": 3, "skill_id": "cn:永不言弃", "db_id": "202721", "name_zh_cn": "永不言弃"},
                                ],
                            }
                        ],
                    }
                ],
            },
        ],
        "chains": [
            {
                "chain_id": "support:30178:continuous",
                "card_id": "30178",
                "kind": "continuous",
                "event_ids": [
                    "support:30178:story:830178001",
                    "support:30178:story:830178002",
                    "support:30178:story:830178003",
                ],
                "stage_count": 3,
            }
        ],
    }
    for ev in payload["events"]:
        for opt in ev["options"]:
            opt.setdefault("option_id", f"{ev['event_id']}:option:{opt['index']}")
    return SupportCardKnowledgeLibrary(payload, skills=skills)


class Test068EventChainBreakAndCnTitles(unittest.TestCase):
    def setUp(self) -> None:
        from core.auto_trainer import larc_event_catalog as lec
        from core.auto_trainer import larc_event_library as lel

        self.lib = _build_duramente_library()
        lec.event_catalog.cache_clear()
        lel._support_index.cache_clear()
        self._patches = [
            patch("core.auto_trainer.larc_event_library.default_library", return_value=self.lib),
            patch("core.auto_trainer.larc_event_catalog.default_library", return_value=self.lib),
            patch("core.support_card_knowledge.default_library", return_value=self.lib),
            patch("core.auto_trainer.larc_event_catalog.load_reviewed_events", return_value=[]),
            patch("core.auto_trainer.larc_event_catalog.PERSONAL_PATH", Path("__missing_personal__.json")),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        from core.auto_trainer import larc_event_catalog as lec
        from core.auto_trainer import larc_event_library as lel

        for p in reversed(self._patches):
            p.stop()
        lec.event_catalog.cache_clear()
        lel._support_index.cache_clear()

    def test_duramente_30178_official_cn_titles_auto_and_manual(self) -> None:
        from core.auto_trainer import larc_event_catalog as lec
        from core.auto_trainer import larc_event_library as lel

        ctx = {"support_profile": {"cards": [{"slot": 0, "card_id": "30178"}]}, "trainee_id": "1046"}
        expected_stories = [
            ("理应挥洒的汗水", 2, "801108001"),
            ("值得追赶的背影", 2, "801108002"),
            ("Vol.1 《超越自律》", 1, "830178001"),
            ("Vol.2 《一脉相承》", 2, "830178002"),
            ("Vol.3 《其名为鸣声雷动》", 1, "830178003"),
        ]
        for title, count, story_id in expected_stories:
            hit = lel.resolve_support(f"协助卡事件\n{title}\n选项效果\n鸣声雷动", count, ctx)
            self.assertIsNotNone(hit, title)
            self.assertEqual(hit["event"]["story_id"], story_id, title)

        found = lec.search_events("Vol.2 《一脉相承》")
        self.assertTrue(any(ev["key"] == "support:30178:story:830178002" for ev in found["events"]), found)

        manual = lec.manual_event_choice(
            "协助卡事件\nVol.2 《一脉相承》\n选项效果\n鸣声雷动",
            2,
            ctx,
            {"manual_choices": {"support:30178:story:830178002": 2}},
        )
        self.assertIsNotNone(manual)
        self.assertEqual(manual["choice_index"], 2)
        self.assertEqual(manual["reason"], "user_event_choice")

    def test_target_skill_break_chain_from_normalization(self) -> None:
        from core.auto_trainer import larc_event_library as lel

        self.assertEqual(lel.normalize_target_skill_break_chain_from(None), "senior_4_second")
        self.assertEqual(lel.target_skill_break_chain_day("senior_4_second"), 55)
        self.assertIsNone(lel.target_skill_break_chain_day("off"))
        self.assertEqual(lel.target_skill_break_chain_day("always"), 0)
        self.assertEqual(
            lel.normalize_target_skill_break_chain_from({"year_index": 3, "month": 3, "half": "first"}),
            "senior_3_first",
        )
        self.assertEqual(lel.target_skill_break_chain_day("3_3_first"), 52)
        with self.assertRaises(ValueError):
            lel.normalize_target_skill_break_chain_from("senior_13_first")

    def test_late_stage_anti_sink_chain_break_on_duramente_vol2(self) -> None:
        from core.auto_trainer import larc_event_library as lel
        from core.auto_trainer.config import UGParentGoal

        ctx = {"support_profile": {"cards": [{"slot": 0, "card_id": "30178"}]}, "trainee_id": "1046"}
        linked = lel.resolve_support("协助卡事件\nVol.2 《一脉相承》\n选项效果\n鸣声雷动", 2, ctx)
        self.assertIsNotNone(linked)

        def make_state(year: int, month: int, half: str, vit: float = 60.0) -> SimpleNamespace:
            return SimpleNamespace(
                turn=20,
                vitality=vit,
                training_options=(),
                current_stats={"speed": 700, "stamina": 500, "power": 550, "guts": 450, "wisdom": 550},
                scenario_state={
                    "scenario": "larc",
                    "larc_period": {"status": "verified", "value": {"year_index": year, "month": month, "half": half}},
                },
            )

        def make_goal(prefs: dict) -> UGParentGoal:
            return UGParentGoal(
                schema_version="1",
                goal="ace",
                target_rank="UE",
                scenario="larc",
                target_stats={"speed": 1600, "stamina": 1250, "power": 1350, "guts": 1100, "wisdom": 1300},
                event_preferences=prefs,
            )

        # 1) Year 3 Feb (day 50 < 55): keep option 1 (+15 energy, wait for Vol.3)
        res_early = lel.score_library_event(linked, make_state(3, 2, "first"), make_goal({"target_skills": ["永不言弃"]}), ctx, "normal")
        self.assertEqual(res_early["choice_index"], 1)
        self.assertEqual(res_early["reason"], "usable_recovery_saves_future_turn")

        # 2) Year 3 Apr 2nd (day 55 >= 55): break chain on option 2 to secure 永不言弃
        res_late = lel.score_library_event(linked, make_state(3, 4, "second"), make_goal({"target_skills": ["永不言弃"]}), ctx, "normal")
        self.assertEqual(res_late["choice_index"], 2)
        self.assertEqual(res_late["reason"], "target_skill_late_break_chain")

        # 3) Explicit "off": still option 1 at Year 3 Apr 2nd
        res_off = lel.score_library_event(
            linked,
            make_state(3, 4, "second"),
            make_goal({"target_skills": ["永不言弃"], "target_skill_break_chain_from": "off"}),
            ctx,
            "normal",
        )
        self.assertEqual(res_off["choice_index"], 1)

        # 4) Explicit "always": option 2 even in Year 2 Jun
        res_always = lel.score_library_event(
            linked,
            make_state(2, 6, "first"),
            make_goal({"target_skills": ["永不言弃"], "target_skill_break_chain_from": "always"}),
            ctx,
            "normal",
        )
        self.assertEqual(res_always["choice_index"], 2)
        self.assertEqual(res_always["reason"], "target_skill_late_break_chain")

        # 5) Without target_skills: still option 1 at Year 3 Apr 2nd (zero side effects)
        res_no_target = lel.score_library_event(linked, make_state(3, 4, "second"), make_goal({}), ctx, "normal")
        self.assertEqual(res_no_target["choice_index"], 1)

        # 6) Already learned 永不言弃: no need to break chain -> option 1
        ctx_learned = {**ctx, "learned_skill_ids": ["cn:永不言弃"]}
        res_learned = lel.score_library_event(linked, make_state(3, 4, "second"), make_goal({"target_skills": ["永不言弃"]}), ctx_learned, "normal")
        self.assertEqual(res_learned["choice_index"], 1)


    def _write_mei_failure_receipt(self, run_root: Path, run_id: str, *, unlocked: bool = False) -> None:
        import json

        session_dir = run_root / "session"
        session_dir.mkdir(parents=True, exist_ok=True)
        (run_root / "progress.json").write_text(
            json.dumps({"run_session_id": run_id, "device": "127.0.0.1:16448", "next_turn": 6}),
            encoding="utf-8",
        )
        (session_dir / "larc_friend_outing.json").write_text(
            json.dumps(
                {
                    "run_session_id": run_id,
                    "unlocked": unlocked,
                    "unlock_event": {
                        "status": "confirmed_failed",
                        "gamble_armed": True,
                        "selected_option_index": 2,
                        "tap_performed": True,
                        "result_basis": "user_confirmed_energy_loss_and_absent_friend_badge",
                        "source": {"run_session_id": run_id, "turn_index": 4, "sha256": "aaa111"},
                        "result_source": {"run_session_id": run_id, "turn_index": 5, "sha256": "bbb222"},
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_authorized_mei_unlock_restart_requires_verified_receipt(self) -> None:
        import json
        from tempfile import TemporaryDirectory
        from scripts.career_workflow_runtime import DOCUMENT, WorkflowState, authorized_mei_unlock_restart

        doc = {
            "id": "wf_test_ace_068",
            "schema_version": "uma.career_workflow.v1",
            "template_id": "ace_career_cycle",
            "policies": {"default": {"career_options": {}, "cut_options": {}}},
            "run_graph": {
                "entry": "e1",
                "nodes": [
                    {"id": "e1", "kind": "entry"},
                    {
                        "id": "n1",
                        "kind": "career",
                        "policy": "default",
                        "ace_cycle": {
                            "target_graduates": 1,
                            "graduation": {"enabled": False, "min_stats": {}, "min_stat_sum": None, "distances": {}, "surfaces": {}},
                            "ss_sieve": None,
                        },
                    },
                    {"id": "f1", "kind": "finish"},
                ],
                "edges": [
                    {"from": "e1", "to": "n1", "outcome": "next"},
                    {"from": "n1", "to": "f1", "outcome": "completed"},
                ],
            },
        }
        with TemporaryDirectory() as td:
            batch_root = Path(td) / "batch"
            run_root = batch_root / "attempts" / "att1"
            (run_root / "session").mkdir(parents=True, exist_ok=True)
            (batch_root / "session").mkdir(parents=True, exist_ok=True)
            (batch_root / DOCUMENT).write_text(json.dumps(doc), encoding="utf-8")
            (run_root / DOCUMENT).write_text(json.dumps(doc), encoding="utf-8")
            store = WorkflowState(batch_root, endpoint="127.0.0.1:16448")
            store.data.update(run_id="att1", attempt_root=str(run_root.resolve()))
            store.save()

            self._write_mei_failure_receipt(run_root, "att1", unlocked=False)
            report = {"stop_reason": "mei_unlock_failed", "run_session_id": "att1"}
            self.assertTrue(authorized_mei_unlock_restart(store, run_root, report))
            self.assertFalse(authorized_mei_unlock_restart(store, run_root, {"stop_reason": "other", "run_session_id": "att1"}))

            self._write_mei_failure_receipt(run_root, "att1", unlocked=True)
            self.assertFalse(authorized_mei_unlock_restart(store, run_root, report))

            (run_root / "session" / "larc_friend_outing.json").unlink()
            self.assertFalse(authorized_mei_unlock_restart(store, run_root, report))

    def test_sieve_cycle_restarts_after_verified_mei_unlock_failure(self) -> None:
        import json
        from tempfile import TemporaryDirectory
        from core.auto_trainer.cut_farming import CutFarmingConfig, CutSession
        from core.auto_trainer.runtime_control import RunControl
        from scripts.career_sieve_cycle import SieveCycle
        from scripts.career_workflow_runtime import DOCUMENT, WorkflowState

        doc = {
            "id": "wf_test_ace_068",
            "schema_version": "uma.career_workflow.v1",
            "template_id": "ace_career_cycle",
            "policies": {"default": {"career_options": {}, "cut_options": {}}},
            "run_graph": {
                "entry": "e1",
                "nodes": [
                    {"id": "e1", "kind": "entry"},
                    {
                        "id": "n1",
                        "kind": "career",
                        "policy": "default",
                        "ace_cycle": {
                            "target_graduates": 1,
                            "graduation": {"enabled": False, "min_stats": {}, "min_stat_sum": None, "distances": {}, "surfaces": {}},
                            "ss_sieve": None,
                        },
                    },
                    {"id": "f1", "kind": "finish"},
                ],
                "edges": [
                    {"from": "e1", "to": "n1", "outcome": "next"},
                    {"from": "n1", "to": "f1", "outcome": "completed"},
                ],
            },
        }
        # Case 1: Verified Mei gamble failure on attempt 1 abandons & completes attempt 2
        with TemporaryDirectory() as td:
            batch_root = Path(td) / "batch"
            (batch_root / "session").mkdir(parents=True, exist_ok=True)
            (batch_root / DOCUMENT).write_text(json.dumps(doc), encoding="utf-8")
            cfg = CutFarmingConfig(character_id="104602", device_port=16448, max_attempts=3)
            sess = CutSession(batch_root, cfg)
            sess.data["ss_sieve_cycle"] = {
                "phase": "ready",
                "career_options": {
                    "setup": {"character_name": "醒目飞鹰", "character_id": "104602"},
                    "max_actions": 80,
                    "retain_training": "discard",
                    "larc_policy": {"variant": "baseline", "revision": "2.3"},
                    "terminal_skills": False,
                },
            }
            sess.save()

            created: list[Path] = []
            abandoned: list[str] = []

            def fake_new_attempt(session, endpoint):
                idx = len(created) + 1
                att = session.root / "attempts" / f"att{idx}"
                (att / "session").mkdir(parents=True, exist_ok=True)
                (att / "progress.json").write_text(json.dumps({"run_session_id": att.name, "device": endpoint, "next_turn": 1}), encoding="utf-8")
                WorkflowState(session.root, endpoint=endpoint).bind(att)
                created.append(att)
                return att

            def fake_runner(args):
                root = Path(args.output).resolve()
                if root.name == "att1":
                    self._write_mei_failure_receipt(root, "att1", unlocked=False)
                    return {
                        "run_session_id": "att1",
                        "stop_reason": "mei_unlock_failed",
                        "mei_unlock_failure": {"status": "confirmed_failed"},
                    }, 0
                return {"run_session_id": root.name, "career_complete": True, "stop_reason": "career_complete"}, 0

            def fake_restart(session):
                def _call(ctx, out, stop_after_abandon=True):
                    abandoned.append(ctx.run_id)
                    return {"ok": True, "reason": "ordinary_abandon_confirmed"}
                return _call

            with patch("scripts.career_sieve_cycle._ordinary_attempt", side_effect=fake_new_attempt):
                cycle = SieveCycle(
                    sess,
                    RunControl(),
                    runner=fake_runner,
                    restart=fake_restart(sess),
                    opening=lambda *a, **k: {"ok": True, "reason": "ordinary_start_confirmed"},
                    takeover_observer=lambda *a, **k: None,
                )
                cycle._args = lambda r: SimpleNamespace(output=str(r), d1_skill_root=None)
                res = cycle.run()

            self.assertEqual(res["status"], "completed", res)
            self.assertEqual(abandoned, ["att1"])
            self.assertEqual(len(sess.data["attempts"]), 2)
            self.assertIn("mei_unlock_failure", sess.data["attempts"][0])

        # Case 2: Unverified mei_unlock_failed without receipt must NOT restart
        with TemporaryDirectory() as td:
            batch_root = Path(td) / "batch2"
            (batch_root / "session").mkdir(parents=True, exist_ok=True)
            (batch_root / DOCUMENT).write_text(json.dumps(doc), encoding="utf-8")
            cfg = CutFarmingConfig(character_id="104602", device_port=16448, max_attempts=3)
            sess2 = CutSession(batch_root, cfg)
            sess2.data["ss_sieve_cycle"] = {
                "phase": "ready",
                "career_options": {
                    "setup": {"character_name": "醒目飞鹰", "character_id": "104602"},
                    "max_actions": 80,
                    "retain_training": "discard",
                    "larc_policy": {"variant": "baseline", "revision": "2.3"},
                    "terminal_skills": False,
                },
            }
            sess2.save()

            def fake_new_attempt2(session, endpoint):
                att = session.root / "attempts" / "att1"
                (att / "session").mkdir(parents=True, exist_ok=True)
                (att / "progress.json").write_text(json.dumps({"run_session_id": "att1", "device": endpoint, "next_turn": 1}), encoding="utf-8")
                WorkflowState(session.root, endpoint=endpoint).bind(att)
                return att

            with patch("scripts.career_sieve_cycle._ordinary_attempt", side_effect=fake_new_attempt2):
                cycle2 = SieveCycle(
                    sess2,
                    RunControl(),
                    runner=lambda args: ({"run_session_id": "att1", "stop_reason": "mei_unlock_failed"}, 0),
                    restart=lambda *a, **k: self.fail("must not abandon without verified receipt"),
                    opening=lambda *a, **k: {"ok": True, "reason": "ordinary_start_confirmed"},
                    takeover_observer=lambda *a, **k: None,
                )
                cycle2._args = lambda r: SimpleNamespace(output=str(r), d1_skill_root=None)
                res2 = cycle2.run()

            self.assertEqual(res2["status"], "route_failed", res2)
            self.assertEqual(res2["reason"], "mei_unlock_failed")


if __name__ == "__main__":
    unittest.main()
