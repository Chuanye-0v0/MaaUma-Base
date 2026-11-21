"""Pure display-name matching; a match never authorizes an action by itself.

Character translations are explicit aliases attached to a character ID.  They
are deliberately absent from ``name_similarity``, which is also used for skill
labels whose IDs, prices and receipts are checked by their existing callers.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
import unicodedata


_TRADITIONAL = str.maketrans({
    "無": "无", "聲": "声", "鈴": "铃", "櫻": "樱", "黃": "黄",
    "驥": "骥", "風": "风", "烏": "乌", "詩": "诗", "劇": "剧",
    "魯": "鲁", "維": "维", "鑽": "钻", "寶": "宝",
    "東": "东", "鷹": "鹰", "徵": "征", "級": "级", "灣": "湾",
    "雲": "云", "愛": "爱", "麗": "丽", "氣": "气", "勝": "胜",
    "櫟": "栎", "樂": "乐", "賽": "赛", "馬": "马", "來": "来",
    "歐": "欧", "陸": "陆", "獎": "奖", "瑪": "玛", "綠": "绿",
    "線": "线", "圓": "圆", "彎": "弯", "擊": "击", "雙": "双",
    "腳": "脚", "勢": "势", "膽": "胆", "讀": "读",
    "熱": "热", "衝": "冲", "絆": "绊", "條": "条", "覺": "觉",
    "覓": "觅", "奧": "奥", "麥": "麦", "聖": "圣", "獅": "狮",
    "龍": "龙", "飛": "飞", "閃": "闪", "蘭": "兰", "優": "优",
    "駿": "骏", "實": "实", "數": "数", "圍": "围", "勁": "劲",
    "進": "进", "遠": "远", "會": "会", "發": "发",
    "標": "标", "準": "准", "離": "离",
    "與": "与", "轉": "转", "時": "时", "後": "后", "順": "顺",
    "斷": "断", "續": "续", "終": "终", "點": "点",
})
_IGNORED = frozenset("[]【】（）()·・○◎◯|")

# These pairs are supplied by the CN name audit, not inferred by similarity.
# The local roster itself is not changed here.  1006 retains the existing OCR
# rendering alias already accepted by career_setup_perception.
_CHARACTER_ALIASES = {
    "1006": ("小栗帽", "小宁帆"),
    "1031": ("艾尼斯风神", "艾尼风神"),
    "1052": ("乌拉拉", "春乌菈菈"),
    "1062": ("待兼诗歌剧", "待兼唐怀瑟"),
    "1076": ("樱花劳蕾尔", "樱花桂冠"),
    "1091": ("比布鲁斯", "维普露斯"),
}


def normalize_name(text: object) -> str:
    """Normalize display formatting and a small, explicit traditional map."""
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFKC", text).translate(_TRADITIONAL).casefold()
    return "".join(char for char in text if not char.isspace() and char not in _IGNORED)


def _one_edit_apart(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    for index, char in enumerate(left):
        if char != right[index]:
            return (left[index + 1:] == right[index + 1:]
                    if len(left) == len(right) else left[index:] == right[index + 1:])
    return len(left) != len(right)


def name_similarity(left: object, right: object) -> float:
    """Return character-set Jaccard with a one-edit short-CJK fallback.

The fallback uses ``1 - distance / (len(left) + len(right))`` for distance one.
This is a text similarity, not an OCR confidence or proof of skill identity.
Jaccard is insensitive to character order; callers retain their exact IDs.
"""
    left, right = normalize_name(left), normalize_name(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_chars, right_chars = set(left), set(right)
    score = len(left_chars & right_chars) / len(left_chars | right_chars)
    if (2 <= len(left) <= 5 and 2 <= len(right) <= 5
            and all("\u3400" <= char <= "\u9fff" for char in left + right)
            and _one_edit_apart(left, right)):
        score = max(score, 1.0 - 1.0 / (len(left) + len(right)))
    return score


def _character_id(record: Mapping) -> str | None:
    value = record.get("character_id")
    if value is None:
        card_id = record.get("id")
        if type(card_id) is int and 100000 <= card_id <= 999999:
            card_id = str(card_id)
        if (isinstance(card_id, str) and len(card_id) == 6
                and card_id.isascii() and card_id.isdecimal()):
            value = card_id[:4]
    if type(value) is int:
        try:
            return str(value) if value > 0 else None
        except ValueError:
            return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def character_candidates(records: object) -> list[dict]:
    """Aggregate full or projected roster records by character ID, without IO.

Aliases already present in the source data are preserved, including aliases
awaiting the separate, user-approved data correction.  This function does not
silently apply the proposed roster patch or rename the canonical data label.
"""
    if isinstance(records, Mapping):
        records = records.get("records")
    if not isinstance(records, (list, tuple)):
        return []
    grouped: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        identifier = _character_id(record)
        name = record.get("name")
        if identifier is None or not normalize_name(name):
            continue
        entry = grouped.setdefault(identifier, {
            "character_id": identifier, "name": name, "aliases": [],
        })
        aliases = record.get("aliases", ())
        labels = [name]
        if isinstance(aliases, (list, tuple)):
            labels.extend(aliases)
        labels.extend(record.get(key) for key in ("name_ja", "name_tw"))
        for label in labels:
            if (normalize_name(label) and label != entry["name"]
                    and label not in entry["aliases"]):
                entry["aliases"].append(label)
    return list(grouped.values())


def match_character(observed: object, candidates: object) -> tuple[str | None, float, str]:
    """Resolve against all candidates: exact, explicit alias, then unique similar.

Duplicate costumes count as one character.  Exact/alias collisions or tied best
similar candidates remain unresolved, so a caller cannot obtain a different
character merely by putting its expected candidate first in the roster.
"""
    observed = normalize_name(observed)
    records = character_candidates(candidates)
    if not observed or not records:
        return None, 0.0, "none"
    exact = {row["character_id"] for row in records
             if normalize_name(row["name"]) == observed}
    if exact:
        return (next(iter(exact)), 1.0, "exact") if len(exact) == 1 else (None, 0.0, "none")
    alias_matches = set()
    scored = []
    for row in records:
        identifier = row["character_id"]
        aliases = [*row["aliases"], *_CHARACTER_ALIASES.get(identifier, ())]
        if any(normalize_name(alias) == observed for alias in aliases):
            alias_matches.add(identifier)
        score = max(name_similarity(observed, label) for label in [row["name"], *aliases])
        scored.append((identifier, score))
    if alias_matches:
        return ((next(iter(alias_matches)), 1.0, "alias") if len(alias_matches) == 1
                else (None, 0.0, "none"))
    best_score = max(score for _identifier, score in scored)
    best = {identifier for identifier, score in scored if math.isclose(score, best_score, abs_tol=1e-12)}
    if best_score >= 0.60 and len(best) == 1:
        return next(iter(best)), best_score, "similar"
    return None, 0.0, "none"


def is_confident(score: object, basis: object) -> bool:
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return False
    if not 0.0 <= score <= 1.0 or not math.isfinite(score):
        return False
    return basis in ("exact", "alias") or basis == "similar" and score >= 0.60
