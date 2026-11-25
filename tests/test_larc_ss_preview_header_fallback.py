from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pacer" / "backend"))

import numpy as np

from core.auto_trainer.larc_pages import plan_larc_ss_preview

_SS_OCR_TEXT = (
    "还剩\n"
    "高级4月上半月\n"
    "胜率\n"
    "全胜时\n"
    "+8，570积分\n"
    "协助Pt\n"
    "63,255积分\n"
    "vs5人\n"
    "超星赛\n"
    "决定"
)


def _synth_sss_preview_image() -> np.ndarray:
    """Build a synthetic 720x1280 SS preview image with pink SSS label and golden chevrons."""
    img = np.full((1280, 720, 3), 180, dtype=np.uint8)
    img[1052:1085, 612:701] = (140, 50, 230)
    img[1096:1114, 616:688] = (30, 200, 240)
    img[1138:1156, 616:688] = (30, 200, 240)
    return img


class LarcSsPreviewHeaderFallbackTests(unittest.TestCase):
    """Verify SS preview recognition when translucent top-left header is dropped."""

    def test_dropped_top_left_ss_header_falls_back_cleanly(self) -> None:
        img = _synth_sss_preview_image()
        decision = plan_larc_ss_preview(
            img,
            {"screen_text": {"normalized_text": _SS_OCR_TEXT}},
            allow_execute=False,
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision["action"], "stop")
        self.assertEqual(decision["reason"], "ready_larc_ss_preview")
        ss_info = decision["evidence"]["larc_ss"]
        self.assertEqual(ss_info["status"], "accepted")
        self.assertEqual(ss_info["kind"], "SSS")
        self.assertEqual(ss_info["ready_count"], 5)
        self.assertEqual(ss_info["supporter_points_if_all_win"], 8570)

    def test_ordinary_training_header_never_matches_ss_preview(self) -> None:
        img = _synth_sss_preview_image()
        decision = plan_larc_ss_preview(
            img,
            {"screen_text": {"normalized_text": "训练\n" + _SS_OCR_TEXT}},
            allow_execute=False,
        )
        self.assertIsNone(decision)

    def test_blank_image_without_decision_ribbon_returns_none(self) -> None:
        blank = np.full((1280, 720, 3), 180, dtype=np.uint8)
        decision = plan_larc_ss_preview(
            blank,
            {"screen_text": {"normalized_text": _SS_OCR_TEXT}},
            allow_execute=False,
        )
        self.assertIsNone(decision)


if __name__ == "__main__":
    unittest.main()
