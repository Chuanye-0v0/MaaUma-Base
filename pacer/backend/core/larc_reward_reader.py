"""Small, project-captured SS reward symbols; no character-to-reward rules."""
import json
from pathlib import Path

import cv2
import numpy as np


def _read_template(path):
    """Keep asset pixels unchanged while supporting Windows Unicode paths."""
    try:
        image = cv2.imdecode(np.frombuffer(Path(path).read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    except (OSError, cv2.error) as error:
        raise ValueError("larc_reward_template_unavailable") from error
    if image is None:
        raise ValueError("larc_reward_template_unavailable")
    return image


class LarcRewardReader:
    def __init__(self):
        root = Path(__file__).resolve().parents[1] / "assets/larc_support"
        self._templates = []
        manifest = json.loads((root / "reward_templates.json").read_text(encoding="utf-8"))
        for item in manifest["icons"]:
            self._templates.append((item["kind"], _read_template(root / item["file"])))

    def read_training(self, image, slot, *, panel_visible):
        offset = slot * 115
        result = {"status": "not_visible", "kind": None, "amount": None,
                  "earned": False, "reason": "charge_panel_absent"}
        if not panel_visible:
            return result
        # The symbol inside the speech bubble is independent of the avatar.
        patch = image[240+offset:271+offset, 509:540]
        kind = self.read_icon(patch)
        if kind is None:
            return dict(result, status="unknown", reason="reward_icon_unrecognized")
        return dict(result, status="accepted", kind=kind, reason="visible_reward_icon")

    def read_icon(self, patch):
        scored = []
        for kind, template in self._templates:
            score = float(cv2.matchTemplate(patch, template, cv2.TM_CCOEFF_NORMED).max())
            scored.append((score, kind))
        scored.sort(reverse=True)
        score, kind = scored[0]
        if score < 0.84 or (len(scored) > 1 and score-scored[1][0] < 0.05):
            return None
        # Both stat gain and skill PT are numeric rewards (the six gain columns).
        # The shared arrow is useful without forcing another verification pass.
        return kind
