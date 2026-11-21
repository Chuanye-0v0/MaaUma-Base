"""Optional five-page review archive and end-of-batch working-image cleanup."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import uuid

import cv2
import numpy as np

from core.auto_trainer.state import TRAINING_TYPES
from core.auto_trainer.pacer_storage import _io, _glob, _owned_path, _EXCLUDED

LABELS = {"speed": "速度", "stamina": "耐力", "power": "力量", "guts": "根性", "wisdom": "智力"}


def _inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if not path.is_relative_to(root):
        raise ValueError("training_review_path_outside_run")
    return path


def _working_files(folder, root):
    pending = [folder]
    while pending:
        directory = pending.pop()
        if not _owned_path(directory, root, root):
            continue
        for path in _glob(directory, '*'):
            if path.name in _EXCLUDED or not _owned_path(path, root, root):
                continue
            if _io(path).is_dir():
                pending.append(path)
            elif _io(path).is_file():
                yield path


def archive_training_turn(run_root, row, report):
    root = Path(run_root).resolve()
    capture = report.get("stages", {}).get("capture", {})
    folder = capture.get("raw_720_dir")
    if not folder:
        return None
    source = _inside(folder, root)
    destination = root / "training_review" / f"turn_{row['turn_index']:03d}"
    _io(destination).mkdir(parents=True, exist_ok=True)
    pages = {}
    for index, key in enumerate(TRAINING_TYPES, 1):
        original = _inside(source / f"{key}.png", root)
        if _io(original).is_file():
            name = f"{index:02d}_{LABELS[key]}.png"
            shutil.copyfile(_io(original), _io(destination / name))
            pages[key] = name
    metadata = {"回合": row["turn_index"], "回合开始状态": row.get("before"),
                "选择行动": row.get("kind"), "执行记录": row.get("execution"), "训练截图": pages}
    turn_root = _inside(row["output"], root)
    metadata["决策详情"] = report.get("stages", {}).get("live_loop", {})
    for name, label in (("capture/training_options.generated.json", "五项训练数据"),
                        ("capture/training_page_observation.json", "人头羁绊与充电")):
        path = turn_root / name
        if _io(path).is_file():
            metadata[label] = json.loads(_io(path).read_text(encoding="utf-8-sig"))
    _io(destination / "回合记录.json").write_text(json.dumps(metadata, ensure_ascii=False, separators=(',', ':')), encoding="utf-8")
    return str(destination)


def discard_completed_training_images(run_root, rows):
    """Run only after the worker stops; keep incomplete actions and their proof.

    Identity matching still needs reference portraits. Preserve those small
    crops before removing completed turns' full training pages. No other run,
    folder, JSON, race result or failed/in-flight action image is removed.
    """
    root = Path(run_root).resolve()
    folders = []
    for row in rows:
        if row.get("turn_consumed") and row.get("pipeline_code") == 0:
            supplied = Path(row["output"]).absolute() / "capture"
            if not _owned_path(supplied, root, root):
                continue
            folder = _inside(supplied, root)
            if _io(folder).is_dir():
                folders.append(folder)
    if not folders:
        return 0
    roster_path = root / "session/larc_member_roster.json"
    roster = json.loads(_io(roster_path).read_text(encoding="utf-8-sig"))
    crop_dir = root / "session/member_portraits"
    for index, member in enumerate(roster.get("members", []), 1):
        sources = member.get("portrait_sources") or {
            member["portrait_source"]["layout"]: member["portrait_source"]}
        replacements = {}
        for layout, source in sources.items():
            original = Path(source["absolute_path"]).resolve()
            if not any(original.is_relative_to(folder) for folder in folders):
                replacements[layout] = source
                continue
            image = cv2.imdecode(np.frombuffer(_io(original).read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("training_review_portrait_source_missing")
            x1, y1, x2, y2 = source["bbox"]
            crop = image[y1:y2, x1:x2]
            if crop.size == 0:
                raise ValueError("training_review_portrait_crop_empty")
            _io(crop_dir).mkdir(parents=True, exist_ok=True)
            path = crop_dir / f"member_{index:02d}_{len(replacements):02d}_{uuid.uuid4().hex[:8]}.png"
            encoded, payload = cv2.imencode('.png', crop)
            if not encoded:
                raise OSError("training_review_portrait_write_failed")
            _io(path).write_bytes(payload.tobytes())
            replacements[layout] = {**deepcopy(source), "absolute_path": str(path),
                                     "bbox": [0, 0, crop.shape[1], crop.shape[0]]}
        member["portrait_sources"] = replacements
        if member.get("portrait_source"):
            member["portrait_source"] = deepcopy(replacements[member["portrait_source"]["layout"]])
    from scripts.ug_parent_one_turn_pipeline import _atomic_write_json
    _atomic_write_json(_io(roster_path), roster)  # Durable identity references before deleting pages.
    count = 0
    for folder in folders:
        for path in _working_files(folder, root):
            if (_owned_path(path, root, root) and _io(path).is_file()
                    and path.suffix.lower() in {".png", ".jpg", ".jpeg"}):
                _io(_inside(path, root)).unlink()
                count += 1
    return count


def discard_consumed_training_images(run_root, row):
    """Release one successful turn's working pages after ready-home handoff.

    The runner calls this on its own thread only after observing the next home.
    Reuse its persisted consumption receipt; do not turn retention into an input
    decision or remove an unconfirmed turn's recovery pages.
    """
    root = Path(run_root).resolve()
    result = {"removed": 0, "removed_bytes": 0}
    if (not isinstance(row, dict) or row.get("turn_consumed") is not True
            or row.get("pipeline_code") != 0 or type(row.get("turn_index")) is not int):
        return {**result, "skipped": "turn_not_consumed"}
    supplied = Path(row.get("output", "")).absolute()
    if not _owned_path(supplied, root, root):
        return {**result, "skipped": "working_path_not_owned"}
    output = _inside(supplied, root)
    progress = json.loads(_io(root / "progress.json").read_text(encoding="utf-8-sig"))
    if (progress.get("run_session_id") != root.name or type(progress.get("next_turn")) is not int
            or progress["next_turn"] <= row["turn_index"] or output == root):
        return {**result, "skipped": "turn_not_consumed"}
    receipt = json.loads(_io(output / "action_receipt.json").read_text(encoding="utf-8-sig"))
    if not receipt or receipt != row.get("execution"):
        return {**result, "skipped": "consumption_receipt_changed"}
    pending = root / "session/larc_autonomous_pending.json"
    if _io(pending).is_file():
        value = json.loads(_io(pending).read_text(encoding="utf-8-sig"))
        if value.get("output") == str(output) or value.get("turn_index") == row["turn_index"]:
            return {**result, "skipped": "turn_still_pending"}
    folder = output / "capture"
    before = {p: _io(p).stat().st_size for p in _working_files(folder, root)
              if _owned_path(p, root, root) and _io(p).is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"}}
    result["removed"] = discard_completed_training_images(root, [row])
    result["removed_bytes"] = sum(size for p, size in before.items() if not _io(p).exists())
    return result
