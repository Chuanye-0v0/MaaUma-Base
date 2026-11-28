"""Regression tests for home/setup popup dismissal in cut_restart_route."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from core.auto_trainer import cut_restart_route as route


def _tok(text: str, bbox: list[int], confidence: float = 0.99) -> dict:
    return {"text": text, "bbox": bbox, "confidence": confidence}


@pytest.fixture
def synthetic_frame(tmp_path: Path) -> Path:
    img = np.full((1280, 720, 3), 240, dtype=np.uint8)
    out = tmp_path / "frame.png"
    cv2.imwrite(str(out), img)
    return out


@pytest.fixture
def dummy_config() -> SimpleNamespace:
    return SimpleNamespace(
        max_tp_items=5,
        max_carrots=0,
        discard_found_for_validation=False,
        trainee_variant_id="103102",
        character_name="艾尼斯风神",
    )


def _run(frame_path: Path, dummy_config: SimpleNamespace, tokens: list[dict], boxes: list[tuple[int, int, int, int]], *, phase: str = "setup") -> dict:
    with (
        patch.object(route, "connecting_overlay_visible", return_value=False),
        patch.object(route, "career_comic_loading_visible", return_value=False),
        patch.object(route, "ocr_tokens", return_value=tokens),
        patch.object(route, "button_boxes", return_value=boxes),
        patch.object(route, "_home_hud_verified", return_value=True),
        patch.object(route, "global_home_portal_point", return_value=None),
        patch.object(route.SafeAdvancePlanner, "_looks_like_white_loading_tips", return_value=False),
    ):
        return route.analyze_restart_frame(frame_path, config=dummy_config, phase=phase, check_start_tp=False)


def test_real_activity_confirm_modal_dismissed(synthetic_frame: Path, dummy_config: SimpleNamespace):
    tokens = [
        _tok("TP", [210, 35, 240, 60]),
        _tok("25/100", [250, 35, 340, 60]),
        _tok("活动确认", [290, 390, 430, 430]),
        _tok("爱丽速子的因子研究活动已开启。要前往活动页面吗？", [140, 660, 580, 710]),
        _tok("取消", [175, 815, 245, 855]),
        _tok("前往活动首页", [430, 815, 580, 855]),
        _tok("主页", [330, 1185, 390, 1225]),
    ]
    boxes = [(110, 795, 200, 80), (390, 795, 220, 80)]
    res = _run(synthetic_frame, dummy_config, tokens, boxes)
    assert res["state"] == "dismiss_home_popup"
    assert res["popup_kind"] == "activity_confirm"
    assert res["center"] == [210, 835]


def test_real_factor_research_invitation_modal_dismissed(synthetic_frame: Path, dummy_config: SimpleNamespace):
    tokens = [
        _tok("活动确认", [290, 390, 430, 430]),
        _tok("您持有因子研究报告", [250, 700, 470, 735]),
        _tok("要前往活动页面吗？", [250, 745, 470, 780]),
        _tok("取消", [175, 815, 245, 855]),
        _tok("前往活动首页", [430, 815, 580, 855]),
    ]
    boxes = [(110, 795, 200, 80), (390, 795, 220, 80)]
    res = _run(synthetic_frame, dummy_config, tokens, boxes)
    assert res["state"] == "dismiss_home_popup"
    assert res["popup_kind"] == "activity_confirm"
    assert res["center"] == [210, 835]


def test_scenario_select_with_floating_factor_research_badge_not_misclassified(synthetic_frame: Path, dummy_config: SimpleNamespace):
    tokens = [
        _tok("剧本评分纪录", [420, 160, 560, 195]),
        _tok("选择剧本", [280, 210, 440, 250]),
        _tok("Reach for the stars 逐星之旅", [180, 360, 540, 410]),
        _tok("凯旋门计划", [270, 620, 450, 665]),
        _tok("下一步", [310, 1020, 410, 1065]),
        _tok("爱丽速子的因子研究活动中", [625, 600, 715, 710]),
        _tok("活动", [640, 720, 695, 745]),
        _tok("强化编成", [60, 1185, 160, 1225]),
        _tok("剧情", [210, 1185, 270, 1225]),
        _tok("主页", [330, 1185, 390, 1225]),
        _tok("赛马", [450, 1185, 510, 1225]),
        _tok("招募", [570, 1185, 630, 1225]),
    ]
    boxes = [(220, 1000, 280, 85)]
    res = _run(synthetic_frame, dummy_config, tokens, boxes)
    assert res["state"] == "continue_scenario"
    assert res["reason"] == "continue_scenario"


def test_home_with_floating_factor_research_badge_and_notice_icon_enters_career(synthetic_frame: Path, dummy_config: SimpleNamespace):
    tokens = [
        _tok("公告", [635, 320, 695, 355]),
        _tok("任务", [635, 420, 695, 455]),
        _tok("爱丽速子的因子研究活动中", [625, 600, 715, 710]),
        _tok("育成", [480, 1050, 560, 1100]),
        _tok("商店", [120, 1185, 180, 1225]),
        _tok("主页", [330, 1185, 390, 1225]),
    ]
    boxes = [(420, 1030, 200, 90)]
    res = _run(synthetic_frame, dummy_config, tokens, boxes)
    assert res["state"] == "enter_ordinary_career"


def test_enhance_page_with_floating_badge_not_misclassified_as_popup(synthetic_frame: Path, dummy_config: SimpleNamespace):
    tokens = [
        _tok("强化编成", [280, 210, 440, 250]),
        _tok("返回", [60, 1040, 140, 1080]),
        _tok("爱丽速子的因子研究活动中", [625, 600, 715, 710]),
        _tok("主页", [330, 1185, 390, 1225]),
    ]
    boxes = [(40, 1020, 120, 80)]
    res = _run(synthetic_frame, dummy_config, tokens, boxes)
    assert res["state"] == "unknown"
    assert res["reason"] == "restart_page_unrecognized"


def test_real_notice_modal_dismissed(synthetic_frame: Path, dummy_config: SimpleNamespace):
    tokens = [
        _tok("公告", [310, 320, 410, 360]),
        _tok("维护及版本说明", [230, 440, 490, 480]),
        _tok("关闭", [310, 860, 410, 900]),
    ]
    boxes = [(220, 840, 280, 80)]
    res = _run(synthetic_frame, dummy_config, tokens, boxes)
    assert res["state"] == "dismiss_home_popup"
    assert res["popup_kind"] == "notice_popup"
    assert res["center"] == [360, 880]


def test_activity_text_without_cancel_button_box_falls_through(synthetic_frame: Path, dummy_config: SimpleNamespace):
    tokens = [
        _tok("活动确认", [290, 390, 430, 430]),
        _tok("要前往活动页面吗？", [250, 745, 470, 780]),
        _tok("前往活动首页", [430, 815, 580, 855]),
        _tok("主页", [330, 1185, 390, 1225]),
    ]
    boxes = [(390, 795, 220, 80)]
    res = _run(synthetic_frame, dummy_config, tokens, boxes)
    assert res["reason"] != "dismiss_home_popup_control_unverified"
    assert res["state"] == "unknown"


def test_abandon_or_tp_or_final_confirm_with_cancel_never_dismissed_as_home_popup(synthetic_frame: Path, dummy_config: SimpleNamespace):
    abandon_tokens = [
        _tok("确认放弃", [280, 390, 440, 430]),
        _tok("是否放弃本次育成？活动确认提示", [160, 620, 560, 670]),
        _tok("取消", [175, 815, 245, 855]),
        _tok("前往活动首页", [430, 815, 580, 855]),
    ]
    boxes = [(110, 795, 200, 80), (390, 795, 220, 80)]
    res = _run(synthetic_frame, dummy_config, abandon_tokens, boxes)
    assert res["state"] != "dismiss_home_popup"
    assert res["reason"] != "dismiss_home_popup_control_unverified"
