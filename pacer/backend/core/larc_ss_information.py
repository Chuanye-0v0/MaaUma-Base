"""Read the scrollable SS information page from its actual visible rows."""
from pathlib import Path
import json

import cv2
import numpy as np


def _read_template(path):
    """Keep asset pixels unchanged while supporting Windows Unicode paths."""
    try:
        image = cv2.imdecode(np.frombuffer(Path(path).read_bytes(), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    except (OSError, cv2.error) as error:
        raise ValueError("ss_information_template_unavailable") from error
    if image is None:
        raise ValueError("ss_information_template_unavailable")
    return image


def _runs(mask):
    padded = np.r_[False, mask, False].astype(np.int8)
    starts = np.flatnonzero(np.diff(padded) == 1)
    ends = np.flatnonzero(np.diff(padded) == -1)
    return list(zip(starts.tolist(), ends.tolist()))


def information_rows(canonical):
    """White card bands, with three coloured reward icons inside each card.

    Clipped top/bottom rows are not assigned fabricated missing rewards.
    """
    hsv = cv2.cvtColor(canonical, cv2.COLOR_BGR2HSV)
    stripe = hsv[154:1110, 447:460]
    white = ((stripe[:, :, 1] < 18) & (stripe[:, :, 2] >= 248)).mean(axis=1) > 0.80
    rows = []
    for top, bottom in _runs(white):
        if not 112 <= bottom-top <= 144:
            continue
        top, bottom = top+154, bottom+154
        icons = hsv[top:bottom, 127:151]
        colored = ((icons[:, :, 1] > 80) & (icons[:, :, 2] > 130)).sum(axis=1) >= 5
        colored = cv2.morphologyEx(colored.astype(np.uint8)[:, None], cv2.MORPH_CLOSE, np.ones((7,1),np.uint8))[:,0] > 0
        centers = [(start+end)//2+top for start,end in _runs(colored) if end-start >= 10]
        if len(centers) == 3 and all(23 <= b-a <= 39 for a,b in zip(centers,centers[1:])):
            rows.append({"top":top,"bottom":bottom,"reward_centers":centers,
                         "bbox":[45,centers[1]-34,111,centers[1]+31]})
    return rows


def text_mask(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # Both current brown and future grey labels on white; colour is not meaning.
    brown = (hsv[:, :, 0] >= 8) & (hsv[:, :, 0] < 40) & (hsv[:, :, 1] > 45) & (hsv[:, :, 2] < 200)
    grey = (hsv[:, :, 1] < 70) & (hsv[:, :, 2] > 130) & (hsv[:, :, 2] < 235)
    mask = (brown | grey).astype(np.uint8)*255
    # Desktop recordings can include a pointer after the label. Do not make
    # its bounding box part of the text or stretch a four-character label.
    occupied = (mask > 0).sum(axis=0) >= 3
    for start,end in _runs(~occupied):
        if start > 8 and end-start >= 12:
            mask[:, start:] = 0
            break
    points = cv2.findNonZero(mask)
    if points is None:
        return None
    x,y,w,h = cv2.boundingRect(points)
    if h < 8 or w < 20:
        return None
    # Use the mask only for locating the string. Grey-level correlation keeps
    # antialiased strokes in the faded future labels instead of thresholding
    # them into different glyph shapes.
    return cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)[:,x:x+w]


class LarcSSInformationReader:
    def __init__(self):
        from core.larc_reward_reader import LarcRewardReader

        self._icons = LarcRewardReader()
        root = Path(__file__).resolve().parents[1] / "assets/larc_support"
        manifest = json.loads((root / "ss_information_templates.json").read_text(encoding="utf-8"))
        self._titles = [_read_template(root / name)
                        for name in [manifest["title_file"], *manifest.get("additional_title_files", [])]]
        self._labels = [(t["kind"],_read_template(root/t["file"])) for t in manifest["labels"]]
        self._groups = [(t["training_type"], _read_template(root/t["file"]))
                        for t in manifest["training_group_labels"]]

    def _training_groups(self, hsv):
        stripe = hsv[154:1110, 180:620]
        green = ((stripe[:, :, 0] >= 30) & (stripe[:, :, 0] <= 85)
                 & (stripe[:, :, 1] > 150) & (stripe[:, :, 2] > 140)).mean(axis=1) > 0.85
        groups = []
        for top, bottom in _runs(green):
            if not 15 <= bottom-top <= 28:
                continue
            top, bottom = top+154, bottom+154
            label = hsv[top-6:bottom+6, 28:78]
            white = ((label[:, :, 1] < 70) & (label[:, :, 2] > 215)).astype(np.uint8)*255
            # The shared suffix means "training" and must not dominate the
            # comparison between, for example, guts and wisdom.
            scores = sorted(((float(cv2.matchTemplate(white, template[:, :34], cv2.TM_CCOEFF_NORMED).max()), kind)
                             for kind, template in self._groups), reverse=True)
            kind = scores[0][1] if scores[0][0] >= 0.80 and scores[0][0]-scores[1][0] >= 0.06 else None
            groups.append((top, kind))  # An unreadable heading still ends the preceding group.
        return groups

    def read(self, image):
        result = {"schema_version":"larc_ss_information.v1","status":"abstained",
                  "reason":"not_ss_information","members":[],"page_verified":False,"decision_safe":False}
        if not isinstance(image,np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
            return result
        h,w = image.shape[:2]
        if not 0.55 <= w/h <= 0.575:
            return result
        canonical = cv2.resize(image,(720,1280),interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(canonical,cv2.COLOR_BGR2HSV)
        title = (((hsv[28:99,80:635,1] < 65) & (hsv[28:99,80:635,2] > 215)).astype(np.uint8)*255)
        if max(float(cv2.matchTemplate(title,template,cv2.TM_CCOEFF_NORMED).max()) for template in self._titles) < 0.85:
            return result
        result["page_verified"] = True
        rows = information_rows(canonical)
        groups = self._training_groups(hsv)
        for row in rows:
            rewards = []
            for cy in row["reward_centers"]:
                symbol = hsv[cy-15:cy+16,125:153]
                pink_hint = float(np.mean((symbol[:,:,0] >= 145) & (symbol[:,:,0] <= 175)
                                         & (symbol[:,:,1] > 100) & (symbol[:,:,2] > 170))) > 0.20
                patch = text_mask(canonical[cy-15:cy+16,154:477])
                scores_by_kind = {}
                if patch is not None:
                    for kind,template in self._labels:
                        if kind == "skill_hint":
                            continue  # The distinct pink exclamation mark is enough.
                        # Cropped glyph strings retain length to separate
                        # "获得属性" from "获得技能点数" and other prefix overlaps.
                        ratio = patch.shape[1]/template.shape[1]
                        if not 0.87 <= ratio <= 1.13:
                            continue
                        candidate = cv2.resize(patch,(template.shape[1],template.shape[0]),interpolation=cv2.INTER_AREA)
                        score = float(cv2.matchTemplate(candidate,template[2:-2,2:-2],cv2.TM_CCOEFF_NORMED).max())
                        scores_by_kind[kind] = max(score, scores_by_kind.get(kind, -1.))
                # Traditional and simplified labels are examples of the same
                # reward, not competing meanings in the ambiguity margin.
                scores = sorted(((score, kind) for kind, score in scores_by_kind.items()), reverse=True)
                accepted = bool(scores and scores[0][0] >= 0.78 and (len(scores)==1 or scores[0][0]-scores[1][0] >= 0.05))
                kind = "skill_hint" if pink_hint else scores[0][1] if accepted else None
                if kind is None:
                    icon = self._icons.read_icon(canonical[cy-15:cy+16,122:153])
                    if icon not in {None,"numeric_gain","positive_condition"}:
                        kind = icon
                rewards.append({"kind":kind, "status":"accepted" if kind else "unknown",
                                "amount":None,"earned":False})
            bbox = [round(row["bbox"][0]*w/720),round(row["bbox"][1]*h/1280),
                    round(row["bbox"][2]*w/720),round(row["bbox"][3]*h/1280)]
            preceding = [(top, kind) for top, kind in groups if top < row["top"]]
            group_top, training_type = preceding[-1] if preceding else (None, None)
            # Card pitch determines the ordinal even if an earlier card had
            # an unreadable icon and was omitted by information_rows().
            index = round((row["top"] - group_top - 40) / 144) if training_type else None
            if index is not None and (index < 0 or abs(row["top"]-group_top-40-index*144) > 9):
                training_type, index = None, None
            result["members"].append({"bbox":bbox,"current":rewards[0],"upcoming":rewards[1:],
                                      "training_type":training_type, "training_member_index":index})
        result.update(status="accepted" if rows else "abstained", reason="visible_complete_rows" if rows else "no_complete_rows")
        return result
