from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.auto_trainer.pacer_storage import _io, _rglob

from core.auto_trainer.dashboard_progress import build_progress_snapshot, build_verified_deck_status
from core.auto_trainer.dashboard_training_evidence import build_training_evidence
from core.auto_trainer.career_goal_ledger import (
    CareerGoalLedgerError,
    CareerGoalLedgerStore,
)
from core.auto_trainer.race_calendar import RaceCalendar, RaceCalendarError
from core.auto_trainer.race_plan import RacePlan, RacePlanError

from core.auto_trainer.dashboard import _looks_like_turn_dir, _turn_sort_key, summarize_turn


class DashboardLookupError(ValueError):
    """Raised when a dashboard v2 resource cannot be resolved safely."""


@dataclass(frozen=True)
class TurnRecordIndexV2:
    """Per-request, run-bound evidence index; it is never process-cached."""

    run_root: Path
    workspace_root: Path
    groups: dict[int, list[dict[str, Any]]]
    events: tuple[dict[str, Any], ...]


def resolve_run_dir(runs_root: str | Path, run_id: str) -> Path:
    if not run_id or run_id in {".", ".."} or "/" in run_id or "\\" in run_id:
        raise DashboardLookupError("invalid_run_id")
    root = Path(runs_root).resolve()
    candidate = (root / run_id).resolve()
    if candidate.parent != root or not candidate.is_dir():
        raise DashboardLookupError("run_not_found")
    return candidate


def find_turn_dirs(run_dir: str | Path) -> list[Path]:
    path = Path(run_dir)
    candidates = list(path.iterdir()) if path.is_dir() else []
    nested = path / "turns"
    if nested.is_dir():
        candidates.extend(nested.iterdir())
    for source_root in _supervised_turn_roots(path):
        candidates.extend(source_root.iterdir())
    unique = {
        child.resolve(): child
        for child in candidates
        if child.is_dir()
        and _is_within(child, path)
        and ("_turn_" in child.name or child.name.startswith("turn_") or _looks_like_turn_dir(child))
    }
    if path.is_dir() and not unique and _looks_like_turn_dir(path):
        unique[path.resolve()] = path
    return sorted(unique.values(), key=_turn_sort_key)


def _supervised_turn_roots(run_dir: Path) -> list[Path]:
    """Return only the run-local supervisor containers that hold turn packages."""
    roots = [run_dir / "supervisor_runs"]
    roots.extend(sorted(run_dir.glob("supervisor_continuation_*")))
    return [root for root in roots if root.is_dir() and _is_within(root, run_dir)]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def list_runs_v2(
    runs_root: str | Path,
    *,
    workspace_root: str | Path,
    full_limit: int = 2,
) -> dict[str, Any]:
    root = Path(runs_root)
    run_dirs = [
        item
        for item in root.iterdir()
        if item.is_dir() and _canonical_run_manifest(item) is not None
    ] if root.is_dir() else []
    run_dirs.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    limit = max(0, min(int(full_limit), 2))
    runs = []
    for position, run_dir in enumerate(run_dirs):
        manifest = _canonical_run_manifest(run_dir)
        if manifest is None:
            continue
        display_title, display_subtitle = _catalog_display_text(run_dir)
        runs.append(
            {
                "run_id": run_dir.name,
                "display_title": display_title,
                "display_subtitle": display_subtitle,
                "last_modified": run_dir.stat().st_mtime,
                "turn_count": len(find_turn_dirs(run_dir)),
                "detail_level": "full" if position < limit else "index",
                "status": manifest["status"],
                "phases": manifest["phases"],
            }
        )
    return {
        "schema_version": "ug_parent_dashboard.v2.runs",
        "full_limit": limit,
        "run_count": len(runs),
        "runs": runs,
    }


def _catalog_display_text(run_dir: Path) -> tuple[str, str]:
    manifest = _canonical_run_manifest(run_dir)
    if manifest is None:
        raise DashboardLookupError("run_manifest_invalid")
    return str(manifest["display_title"]), str(manifest["display_subtitle"])


_DEFAULT_RUN_PHASES = (
    ("career_setup", "新局配置", "completed"),
    ("shorten_events", "缩短所有事件", "completed"),
    ("double_skip", "跳过双箭头", "completed"),
    ("regular_turns", "常规回合", "running"),
    ("career_goals", "生涯目标", "pending"),
    ("skill_purchase", "技能购买", "pending"),
    ("settlement", "终局结算", "pending"),
    ("factor_inheritance", "因子继承", "pending"),
)
_PHASE_STATUSES = {"pending", "running", "completed", "warning", "blocked"}
_TRAINING_TYPES = ("speed", "stamina", "power", "guts", "wisdom")
_TRAINING_ARCHIVE_SCHEMA = "ug_parent_dashboard.v2.training_evidence_archive"
_TRAINING_ARCHIVE_KEYS = frozenset({
    "schema_version", "turn_packet_sha256",
    "turn_packet_bytes_sha256", "cards",
})
_TRAINING_ARCHIVE_CARD_KEYS = frozenset({
    "training_type", "packet_image_path", "archive_path",
    "source_image_sha256",
})
_MAX_TRAINING_ARCHIVE_BYTES = 1_000_000
_DEFAULT_RACE_CALENDAR = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "race_calendar"
    / "cn_standard_v1.json"
)


def _canonical_run_manifest(run_dir: Path) -> dict[str, Any] | None:
    manifest = _load_json(run_dir / "run_manifest.json")
    if not isinstance(manifest, dict):
        return None
    if manifest.get("schema_version") != "ug_parent_dashboard.v2.run_manifest":
        return None
    if manifest.get("run_id") != run_dir.name:
        return None
    if not isinstance(manifest.get("display_title"), str) or not manifest["display_title"].strip():
        return None
    if not isinstance(manifest.get("display_subtitle"), str):
        return None
    if manifest.get("status") not in _PHASE_STATUSES:
        return None
    phases = manifest.get("phases")
    if not isinstance(phases, list):
        return None
    for phase in phases:
        if not isinstance(phase, dict) or set(phase) != {"id", "label", "status"}:
            return None
        if not isinstance(phase["id"], str) or not isinstance(phase["label"], str):
            return None
        if phase["status"] not in _PHASE_STATUSES:
            return None
    return manifest


def record_dashboard_turn(
    run_dir: str | Path,
    source_turn_dir: str | Path,
    *,
    turn_index: int,
    workspace_root: str | Path,
    display_title: str,
    display_subtitle: str,
) -> dict[str, Any]:
    if not isinstance(turn_index, int) or isinstance(turn_index, bool) or turn_index < 1:
        raise ValueError("turn_index_invalid")
    run = Path(run_dir)
    source = Path(source_turn_dir)
    if not source.is_dir():
        raise ValueError("source_turn_dir_missing")
    run.mkdir(parents=True, exist_ok=True)
    manifest_path = run / "run_manifest.json"
    if not manifest_path.exists():
        manifest = {
            "schema_version": "ug_parent_dashboard.v2.run_manifest",
            "run_id": run.name,
            "display_title": str(display_title),
            "display_subtitle": str(display_subtitle),
            "status": "running",
            "phases": [
                {"id": phase_id, "label": label, "status": status}
                for phase_id, label, status in _DEFAULT_RUN_PHASES
            ],
        }
        _write_json_exclusive(manifest_path, manifest)
    if _canonical_run_manifest(run) is None:
        raise ValueError("run_manifest_invalid")

    packet = canonical_turn_packet(source, workspace_root)
    packet["identity"] = {"run_id": run.name, "turn_index": turn_index}
    digest = packet_sha256(packet)
    wrapper = {
        "schema_version": "ug_parent_dashboard.v2.turn_packet",
        "packet": packet,
        "turn_packet_sha256": digest,
    }
    destination = run / "turns" / f"turn_{turn_index:03d}" / "turn_packet.json"
    if destination.exists():
        existing = _load_json(destination)
        if existing != wrapper:
            raise ValueError("turn_packet_conflict")
    else:
        _write_json_exclusive(destination, wrapper)
    training_archive_path = _archive_training_images(
        destination.parent,
        packet,
        turn_packet_sha256=digest,
        workspace_root=workspace_root,
        source_turn_root=source,
    )
    return {
        "run_manifest_path": str(manifest_path),
        "turn_packet_path": str(destination),
        "turn_packet_sha256": digest,
        "training_evidence_archive_path": (
            str(training_archive_path) if training_archive_path is not None else None
        ),
    }


def archive_recorded_turn_training_evidence(
    run_dir: str | Path,
    *,
    turn_index: int,
    source_workspace_root: str | Path,
) -> dict[str, Any]:
    if not isinstance(turn_index, int) or isinstance(turn_index, bool) or turn_index < 1:
        raise ValueError("turn_index_invalid")
    run = Path(run_dir)
    turn_dir = run / "turns" / f"turn_{turn_index:03d}"
    stored = _load_json(turn_dir / "turn_packet.json")
    packet = stored.get("packet") if isinstance(stored, dict) else None
    digest = stored.get("turn_packet_sha256") if isinstance(stored, dict) else None
    identity = packet.get("identity") if isinstance(packet, dict) else None
    if (
        not isinstance(stored, dict)
        or stored.get("schema_version") != "ug_parent_dashboard.v2.turn_packet"
        or not isinstance(packet, dict)
        or not isinstance(digest, str)
        or packet_sha256(packet) != digest
        or not isinstance(identity, dict)
        or identity.get("run_id") != run.name
        or identity.get("turn_index") != turn_index
    ):
        raise ValueError("dashboard_turn_packet_invalid")
    manifest_path = _archive_training_images(
        turn_dir,
        packet,
        turn_packet_sha256=digest,
        workspace_root=source_workspace_root,
    )
    if manifest_path is None:
        raise ValueError("dashboard_training_options_missing")
    return {
        "turn_packet_sha256": digest,
        "training_evidence_archive_path": str(manifest_path),
        "training_evidence_archive_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
    except FileExistsError:
        raise ValueError("dashboard_artifact_already_exists") from None


def _archive_training_images(
    turn_dir: Path,
    packet: dict[str, Any],
    *,
    turn_packet_sha256: str,
    workspace_root: str | Path,
    source_turn_root: str | Path | None = None,
) -> Path | None:
    capture = packet.get("capture") if isinstance(packet.get("capture"), dict) else {}
    options = capture.get("options")
    if not isinstance(options, list):
        return None
    by_type = {
        option.get("training_type"): option
        for option in options
        if isinstance(option, dict) and option.get("training_type") in _TRAINING_TYPES
    }
    if len(options) != len(_TRAINING_TYPES) or set(by_type) != set(_TRAINING_TYPES):
        return None

    roots = [Path(workspace_root).resolve()]
    if source_turn_root is not None:
        roots.append(Path(source_turn_root).resolve(strict=True))
    cards = []
    for training_type in _TRAINING_TYPES:
        option = by_type[training_type]
        packet_image_path = option.get("image_path")
        if not isinstance(packet_image_path, str) or not packet_image_path.strip():
            raise ValueError("dashboard_training_image_source_invalid")
        requested = Path(packet_image_path)
        source = (
            requested if requested.is_absolute() else roots[0] / requested
        ).resolve(strict=True)
        if not any(_path_is_within(source, root) for root in roots):
            raise ValueError("dashboard_training_image_source_invalid") from None
        if not source.is_file():
            raise ValueError("dashboard_training_image_source_invalid")
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        suffix = source.suffix.lower() if source.suffix else ".png"
        archived = turn_dir / "evidence" / "training" / f"{training_type}{suffix}"
        archived.parent.mkdir(parents=True, exist_ok=True)
        if archived.exists():
            if archived.read_bytes() != payload:
                raise ValueError("dashboard_training_archive_conflict")
        else:
            try:
                with archived.open("xb") as handle:
                    handle.write(payload)
            except FileExistsError:
                if archived.read_bytes() != payload:
                    raise ValueError("dashboard_training_archive_conflict") from None
        cards.append({
            "training_type": training_type,
            "packet_image_path": packet_image_path,
            "archive_path": archived.relative_to(turn_dir).as_posix(),
            "source_image_sha256": digest,
        })

    packet_path = turn_dir / "turn_packet.json"
    if not packet_path.is_file() or packet_path.is_symlink():
        raise ValueError("dashboard_turn_packet_invalid")
    manifest = {
        "schema_version": _TRAINING_ARCHIVE_SCHEMA,
        "turn_packet_sha256": turn_packet_sha256,
        "turn_packet_bytes_sha256": hashlib.sha256(
            packet_path.read_bytes()
        ).hexdigest(),
        "cards": cards,
    }
    manifest_path = turn_dir / "training_evidence_archive.json"
    if manifest_path.exists():
        if _load_json(manifest_path) != manifest:
            raise ValueError("dashboard_training_archive_manifest_conflict")
    else:
        _write_json_exclusive(manifest_path, manifest)
    return manifest_path


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _load_strict_training_archive(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw or len(raw) > _MAX_TRAINING_ARCHIVE_BYTES:
        return None

    def reject_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_key")
            value[key] = item
        return value

    try:
        archive = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        canonical = (
            json.dumps(
                archive,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    return archive if type(archive) is dict and raw == canonical else None


def _validate_training_archive_cards(
    turn_dir: Path,
    packet: dict[str, Any],
    *,
    turn_packet_sha256: str,
    workspace_root: str | Path,
    manifest: dict[str, Any],
) -> dict[str, str]:
    if (
        frozenset(manifest) != _TRAINING_ARCHIVE_KEYS
        or manifest.get("schema_version") != _TRAINING_ARCHIVE_SCHEMA
        or manifest.get("turn_packet_sha256") != turn_packet_sha256
        or not _is_sha256(manifest.get("turn_packet_bytes_sha256"))
        or type(manifest.get("cards")) is not list
        or len(manifest["cards"]) != len(_TRAINING_TYPES)
    ):
        return {}
    capture = packet.get("capture") if isinstance(packet.get("capture"), dict) else {}
    options = capture.get("options") if isinstance(capture.get("options"), list) else []
    packet_paths = {
        option.get("training_type"): option.get("image_path")
        for option in options
        if (
            type(option) is dict
            and option.get("training_type") in _TRAINING_TYPES
            and type(option.get("image_path")) is str
            and option["image_path"]
        )
    }
    if len(options) != len(_TRAINING_TYPES) or frozenset(packet_paths) != frozenset(_TRAINING_TYPES):
        return {}
    if [
        card.get("training_type") if type(card) is dict else None
        for card in manifest["cards"]
    ] != list(_TRAINING_TYPES):
        return {}

    root = Path(workspace_root).resolve()
    turn_root = turn_dir.resolve()
    archived_paths: dict[str, str] = {}
    for card in manifest["cards"]:
        if type(card) is not dict or frozenset(card) != _TRAINING_ARCHIVE_CARD_KEYS:
            return {}
        training_type = card.get("training_type")
        packet_image_path = card.get("packet_image_path")
        archive_path = card.get("archive_path")
        expected_sha = card.get("source_image_sha256")
        if (
            type(training_type) is not str
            or training_type not in _TRAINING_TYPES
            or type(packet_image_path) is not str
            or not packet_image_path
            or packet_image_path != packet_paths.get(training_type)
            or type(archive_path) is not str
            or not archive_path
            or not _is_sha256(expected_sha)
        ):
            return {}
        requested = Path(archive_path)
        if (
            requested.is_absolute()
            or requested.parent != Path("evidence") / "training"
            or requested.stem != training_type
        ):
            return {}
        candidate = turn_dir / requested
        if (
            candidate.is_symlink()
            or candidate.parent.is_symlink()
            or candidate.parent.parent.is_symlink()
        ):
            return {}
        try:
            archived = candidate.resolve(strict=True)
            archived.relative_to(turn_root)
            relative = archived.relative_to(root)
            raw_image = archived.read_bytes()
        except (OSError, ValueError):
            return {}
        if (
            not archived.is_file()
            or len(raw_image) > 25_000_000
            or hashlib.sha256(raw_image).hexdigest() != expected_sha
        ):
            return {}
        archived_paths[training_type] = relative.as_posix()
    return (
        archived_paths
        if frozenset(archived_paths) == frozenset(_TRAINING_TYPES)
        else {}
    )


def _archived_training_image_paths(
    turn_dir: Path,
    packet: dict[str, Any],
    *,
    turn_packet_sha256: str,
    workspace_root: str | Path,
) -> dict[str, str] | None:
    manifest_path = turn_dir / "training_evidence_archive.json"
    if not manifest_path.exists():
        return None
    manifest = _load_strict_training_archive(manifest_path)
    if manifest is None:
        return {}
    return _validate_training_archive_cards(
        turn_dir,
        packet,
        turn_packet_sha256=turn_packet_sha256,
        workspace_root=workspace_root,
        manifest=manifest,
    )


_STAT_KEYS = ("speed", "stamina", "power", "guts", "wisdom", "skill_pt")
_FINAL_SECTIONS = ("profile", "final_stats", "skills", "races", "factors", "inheritance", "evidence")


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _unknown_progress() -> dict[str, str]:
    return {"status": "unknown"}


def _load_tracker_or_unknown(turn_dir: Path, name: str) -> dict[str, Any]:
    data = _load_json(turn_dir / name)
    if data is None:
        return _unknown_progress()
    return data if isinstance(data.get("status"), str) else {"status": "unknown", "evidence": data}


_CONDITION_SUMMARY_KEYS = frozenset({
    "schema_version", "policy", "status", "dirty", "probe_performed",
    "probe_reasons", "last_explicit_probe_turn", "next_periodic_due_turn",
    "report_sha256", "source_frame_sha256", "authority_id",
    "provider_source_sha256", "authority_rebound", "recovery_reason",
})
_CONDITION_STATUSES = frozenset({"healthy", "registered", "negative"})
_CONDITION_PROBE_REASONS = (
    "dirty", "suspected_negative", "last_unknown", "critical_race", "periodic_audit",
)
_EVENT_CHOICE_SHADOW_SCHEMA = "uma.event_choice_shadow_slot.v1"
_EVENT_CHOICE_SHADOW_FIELDS = frozenset({
    "schema_version", "status", "abstain_reason", "action_eligible",
})
_RELIABILITY_BUNDLE_KEYS = frozenset({
    "schema_version", "run_session_id", "generation", "global_action_turn",
    "turn_id", "authority_bundle_sha256", "training_packet_sha256",
    "gate_manifest_id", "gate_manifest_sha256", "training",
})
_RELIABILITY_TAB_KEYS = frozenset({"source_frame_sha256", "fields"})
_RELIABILITY_FIELD_KEYS = frozenset({
    "path", "status", "value", "turn_id", "source_frame_sha256",
    "evidence_schema", "gate_id", "reasons",
})
_RELIABILITY_TABS = ("speed", "stamina", "power", "guts", "wisdom")
_RELIABILITY_BINDING_KEYS = frozenset({
    "schema_version", "run_session_id", "generation", "global_action_turn",
    "turn_id", "authority_bundle_sha256", "training_packet_sha256",
    "perception_reliability_sha256",
})
_RELIABILITY_UNAVAILABLE = {
    "status": "unavailable",
    "reason": "perception_reliability_unavailable",
}
_TRUSTED_RELIABILITY_GATE_SHA = (
    "451d48629ca02f8a20e612f3287ba004cfc83a89f518cc5e74f69e3a87d95eaa"
)
_TRUSTED_RELIABILITY_GATE_ID = "perception-gates-20260810-v1"
_RELIABILITY_SUPPORT_COUNT_FIELDS = {
    "support.visible_count": {
        "evidence_schema": "support-visible-count-evidence.v1",
        "gate_id": "support-visible-count-20260729-v1",
    },
    "support.effective_count": {
        "evidence_schema": "support-effective-count-evidence.v1",
        "gate_id": "support-effective-count-20260729-v1",
    },
}


def _unknown_condition_summary() -> dict[str, Any]:
    return {
        "status": "unknown", "dirty": True, "probe_performed": False,
        "probe_reasons": ["condition_summary_missing"],
        "last_explicit_probe_turn": None, "next_periodic_due_turn": None,
        "report_sha256": None, "source_frame_sha256": None,
    }


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _load_condition_summary_or_unknown(turn_dir: Path) -> dict[str, Any]:
    """Load display-only condition evidence from this turn root, never elsewhere.

    This is packet-bound dashboard data, not live condition authority.  A malformed
    or absent local summary is deliberately indistinguishable from unknown state.
    """
    summary = _load_json(turn_dir / "condition_turn_summary.json")
    if not isinstance(summary, dict) or set(summary) != _CONDITION_SUMMARY_KEYS:
        return _unknown_condition_summary()
    if (
        summary.get("schema_version") != "condition_turn_summary.v1"
        or summary.get("policy") != "triggered"
        or summary.get("status") not in _CONDITION_STATUSES
        or not isinstance(summary.get("dirty"), bool)
        or not isinstance(summary.get("probe_performed"), bool)
        or not isinstance(summary.get("authority_rebound"), bool)
        or not isinstance(summary.get("probe_reasons"), list)
        or any(
            not isinstance(reason, str) or reason not in _CONDITION_PROBE_REASONS
            for reason in summary["probe_reasons"]
        )
        or summary["probe_reasons"] != sorted(
            set(summary["probe_reasons"]), key=_CONDITION_PROBE_REASONS.index
        )
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (summary.get("last_explicit_probe_turn"), summary.get("next_periodic_due_turn"))
        )
        or summary["next_periodic_due_turn"] < summary["last_explicit_probe_turn"]
        or any(
            not _is_sha256(summary.get(key))
            for key in ("report_sha256", "source_frame_sha256", "authority_id", "provider_source_sha256")
        )
        or (summary.get("recovery_reason") is not None and not isinstance(summary.get("recovery_reason"), str))
        or summary["probe_performed"] == summary["authority_rebound"]
        or (summary["probe_performed"] and not summary["probe_reasons"])
        or (summary["authority_rebound"] and summary["probe_reasons"])
    ):
        return _unknown_condition_summary()
    return summary


def _load_event_choice_shadow_audit(turn_dir: Path) -> dict[str, Any]:
    """Project the non-executable supervised turn-report slot, fail closed."""
    report = _load_json(turn_dir / "turn_report.json")
    slot = report.get("event_choice_shadow") if isinstance(report, dict) else None
    if not isinstance(slot, dict) or set(slot) != _EVENT_CHOICE_SHADOW_FIELDS:
        return {"status": "unavailable"}
    status = slot.get("status")
    reason = slot.get("abstain_reason")
    if (
        slot.get("schema_version") != _EVENT_CHOICE_SHADOW_SCHEMA
        or slot.get("action_eligible") is not False
        or status not in {"identified", "abstain"}
        or (status == "abstain" and (not isinstance(reason, str) or not reason))
        or (status == "identified" and reason is not None)
    ):
        return {"status": "unavailable"}
    return {
        "status": status,
        "abstain_reason": reason,
        "action_eligible": False,
    }


def _has_trusted_detached_turn_packet_digest(
    turn_dir: Path,
    packet: dict[str, Any],
    *,
    workspace_root: str | Path,
) -> bool:
    """Check local archive consistency, not cryptographic tamper resistance.

    This rejects accidental replay, partial archives, conflicts, and association
    mistakes.  With no external key or immutable ledger, it does not defend
    against an actor who can coordinate writes to every file in the run directory.
    """
    packet_path = turn_dir / "turn_packet.json"
    archive_path = turn_dir / "training_evidence_archive.json"
    try:
        if (
            not packet_path.is_file()
            or packet_path.is_symlink()
            or not archive_path.is_file()
            or archive_path.is_symlink()
        ):
            return False
        raw_packet = packet_path.read_bytes()
        archive = _load_strict_training_archive(archive_path)
    except OSError:
        return False
    return (
        type(archive) is dict
        and bool(_validate_training_archive_cards(
            turn_dir,
            packet,
            turn_packet_sha256=packet_sha256(packet),
            workspace_root=workspace_root,
            manifest=archive,
        ))
        and hashlib.sha256(raw_packet).hexdigest()
        == archive["turn_packet_bytes_sha256"]
    )


def _load_perception_reliability_audit(
    turn_dir: Path,
    *,
    run_dir: Path,
    packet: dict[str, Any],
    packet_status: str,
    workspace_root: str | Path,
    expected_turn_index: int,
) -> dict[str, Any]:
    """Project audit data after local consistency checks, never as tamper-proof."""
    unavailable = lambda: dict(_RELIABILITY_UNAVAILABLE)
    report = _load_json(turn_dir / "turn_report.json")
    artifacts = report.get("artifacts") if isinstance(report, dict) else None
    if not isinstance(artifacts, dict):
        stages = report.get("stages") if isinstance(report, dict) else None
        stage = (
            stages.get("perception_reliability")
            if isinstance(stages, dict)
            else None
        )
        if (
            isinstance(stage, dict)
            and stage.get("status") == "unavailable"
            and stage.get("reason")
            == "legacy_formal_capture_provenance_missing"
        ):
            return {
                "status": "unavailable",
                "reason": "legacy_formal_capture_provenance_missing",
            }
        return unavailable()
    if not _has_trusted_detached_turn_packet_digest(
        turn_dir,
        packet,
        workspace_root=workspace_root,
    ):
        return unavailable()
    binding = (
        packet.get("perception_authority_binding")
        if packet_status == "reliable" and isinstance(packet, dict)
        else None
    )
    if (
        type(binding) is not dict
        or frozenset(binding) != _RELIABILITY_BINDING_KEYS
        or binding.get("schema_version")
        != "ug_parent_dashboard.v2.perception_authority_binding"
        or type(binding.get("run_session_id")) is not str
        or not binding["run_session_id"]
        or type(binding.get("generation")) is not int
        or binding["generation"] <= 0
        or type(binding.get("global_action_turn")) is not int
        or binding["global_action_turn"] != expected_turn_index
        or type(binding.get("turn_id")) is not str
        or not binding["turn_id"]
        or any(
            not _is_sha256(binding.get(key))
            for key in (
                "authority_bundle_sha256",
                "training_packet_sha256",
                "perception_reliability_sha256",
            )
        )
    ):
        return unavailable()
    requested = artifacts.get("perception_reliability_path")
    expected_sha = artifacts.get("perception_reliability_sha256")
    expected_authority = artifacts.get("perception_authority_bundle_sha256")
    if (
        type(requested) is not str
        or not requested
        or not _is_sha256(expected_sha)
        or not _is_sha256(expected_authority)
        or expected_sha != binding["perception_reliability_sha256"]
        or expected_authority != binding["authority_bundle_sha256"]
    ):
        return unavailable()
    try:
        root = run_dir.resolve(strict=True)
        workspace = Path(workspace_root).resolve(strict=True)
        root.relative_to(workspace)
        path = Path(requested)
        path = (path if path.is_absolute() else turn_dir / path).resolve(strict=True)
        path.relative_to(root)
        raw = path.read_bytes()
    except (OSError, ValueError):
        return unavailable()
    if len(raw) > 1_000_000 or hashlib.sha256(raw).hexdigest() != expected_sha:
        return unavailable()

    def reject_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_key")
            value[key] = item
        return value

    try:
        bundle = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return unavailable()
    canonical = (
        json.dumps(
            bundle,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if raw != canonical:
        return unavailable()
    if (
        type(bundle) is not dict
        or frozenset(bundle) != _RELIABILITY_BUNDLE_KEYS
        or bundle.get("schema_version") != "uma.perception_reliability_bundle.v1"
        or type(bundle.get("run_session_id")) is not str
        or not bundle["run_session_id"]
        or type(bundle.get("generation")) is not int
        or bundle["generation"] <= 0
        or type(bundle.get("global_action_turn")) is not int
        or bundle["global_action_turn"] != expected_turn_index
        or type(bundle.get("turn_id")) is not str
        or not bundle["turn_id"]
        or bundle.get("run_session_id") != binding["run_session_id"]
        or bundle.get("generation") != binding["generation"]
        or bundle.get("global_action_turn") != binding["global_action_turn"]
        or bundle.get("turn_id") != binding["turn_id"]
        or bundle.get("authority_bundle_sha256")
        != binding["authority_bundle_sha256"]
        or bundle.get("training_packet_sha256")
        != binding["training_packet_sha256"]
        or not _is_sha256(bundle.get("training_packet_sha256"))
        or bundle.get("gate_manifest_id") != _TRUSTED_RELIABILITY_GATE_ID
        or bundle.get("gate_manifest_sha256") != _TRUSTED_RELIABILITY_GATE_SHA
    ):
        return unavailable()
    training = bundle.get("training")
    if (
        type(training) is not dict
        or frozenset(training) != frozenset(_RELIABILITY_TABS)
    ):
        return unavailable()
    projected_tabs = []
    for tab in _RELIABILITY_TABS:
        raw_tab = training.get(tab)
        if type(raw_tab) is not dict or frozenset(raw_tab) != _RELIABILITY_TAB_KEYS:
            return unavailable()
        frame_sha = raw_tab.get("source_frame_sha256")
        fields = raw_tab.get("fields")
        if (
            not _is_sha256(frame_sha)
            or type(fields) is not dict
            or frozenset(fields)
            != frozenset(_RELIABILITY_SUPPORT_COUNT_FIELDS)
        ):
            return unavailable()
        projected_fields = []
        for field_name, specification in (
            _RELIABILITY_SUPPORT_COUNT_FIELDS.items()
        ):
            field = fields[field_name]
            expected_path = f"training.{tab}.{field_name}"
            reasons = (
                field.get("reasons") if isinstance(field, dict) else None
            )
            status = field.get("status") if isinstance(field, dict) else None
            value = field.get("value") if isinstance(field, dict) else None
            if (
                type(field) is not dict
                or frozenset(field) != _RELIABILITY_FIELD_KEYS
                or field.get("path") != expected_path
                or field.get("turn_id") != bundle["turn_id"]
                or field.get("source_frame_sha256") != frame_sha
                or field.get("evidence_schema")
                != specification["evidence_schema"]
                or field.get("gate_id") != specification["gate_id"]
                or type(reasons) is not list
                or any(
                    type(reason) is not str
                    or re.fullmatch(r"[a-z0-9_.-]+", reason) is None
                    for reason in reasons
                )
                or len(reasons) != len(set(reasons))
                or status not in {"decision_safe", "shadow", "unknown"}
                or (
                    status == "decision_safe"
                    and (
                        type(value) is not int
                        or not 0 <= value <= 6
                        or reasons
                    )
                )
                or (
                    status == "shadow"
                    and (
                        type(value) is not int
                        or not 0 <= value <= 6
                        or not reasons
                    )
                )
                or (
                    status == "unknown"
                    and (value is not None or not reasons)
                )
            ):
                return unavailable()
            projected_fields.append({
                "path": expected_path,
                "status": status,
                "value": value,
                "gate_id": field["gate_id"],
                "source_frame_sha256": frame_sha,
                "reasons": list(reasons),
            })
        projected_tabs.append({
            "tab": tab,
            "source_frame_sha256": frame_sha,
            "fields": projected_fields,
        })
    return {
        "status": "available",
        "artifact_sha256": expected_sha,
        "authority_bundle_sha256": expected_authority,
        "tabs": projected_tabs,
    }


def _normalize_game_state(game_state: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(game_state)
    stats = normalized.get("current_stats") if isinstance(normalized.get("current_stats"), dict) else {}
    normalized["current_stats"] = {key: stats.get(key) for key in _STAT_KEYS}
    return normalized


def _relative_debug_evidence(turn_dir: Path, workspace_root: str | Path) -> list[str]:
    workspace = Path(workspace_root).resolve()
    evidence: list[str] = []
    try:
        candidates = _rglob(turn_dir, "*")
        for path in candidates:
            if not _io(path).is_file() or path.name in {"turn_packet.json", "support_progress.json", "outing_progress.json"}:
                continue
            if path.suffix.lower() not in {".json", ".png", ".jpg", ".jpeg"}:
                continue
            resolved = path.resolve()
            if workspace not in resolved.parents:
                continue
            evidence.append(str(resolved.relative_to(workspace)))
    except OSError:
        return []
    return sorted(evidence)[:50]


def _summarize_safety(execution: dict[str, Any]) -> dict[str, Any]:
    checks = execution.get("safety_checks") if isinstance(execution.get("safety_checks"), list) else []
    return {"status": "unknown" if not checks else "observed", "checks": checks}


def _turn_index(turn_dir: Path, turn: dict[str, Any]) -> int | None:
    value = (turn.get("game_state") or {}).get("turn_index")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    match = re.search(r"(?:^|_)turn_?(\d+)(?:_|$)", turn_dir.name)
    if match is not None:
        return int(match.group(1))
    key = _turn_sort_key(turn_dir)[0]
    return key if key != 999999 else None


def _packet_for_candidate(
    run_dir: Path,
    turn_dir: Path,
    turn_index: int,
    workspace_root: str | Path,
    legacy: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    packet, status = _packet_for_turn(
        run_dir,
        turn_dir,
        turn_index,
        workspace_root,
        legacy=legacy,
    )
    if status == "reliable":
        return packet, status
    rebound = dict(packet)
    rebound["identity"] = {"run_id": run_dir.name, "turn_index": turn_index}
    return rebound, status


def _candidate_has_complete_training_evidence(
    packet: dict[str, Any],
    *,
    workspace_root: str | Path,
) -> bool:
    evidence = build_training_evidence(
        packet,
        turn_packet_sha256=packet_sha256(packet),
        workspace_root=workspace_root,
    )
    return evidence.get("status") == "reliable"


def _source_priority(run_dir: Path, turn_dir: Path) -> tuple[int, float, str]:
    try:
        relative = turn_dir.resolve().relative_to(run_dir.resolve())
    except (OSError, ValueError):
        return (-1, -1.0, "")
    first = relative.parts[0] if relative.parts else ""
    if first == "turns":
        rank = 0
    elif first == "supervisor_runs":
        rank = 1
    elif first.startswith("supervisor_continuation_"):
        rank = 2
    else:
        rank = 1
    try:
        modified = turn_dir.stat().st_mtime
    except OSError:
        modified = -1.0
    return rank, modified, relative.as_posix()


def _turn_record_groups(
    run_dir: Path,
    workspace_root: str | Path,
    *,
    turn_index: int | None = None,
) -> dict[int, list[dict[str, Any]]]:
    groups: dict[int, list[dict[str, Any]]] = {}
    run_root = run_dir.resolve()
    for turn_dir in find_turn_dirs(run_dir):
        if not _is_within(turn_dir, run_root):
            continue
        named_index = _turn_index(turn_dir, {})
        if turn_index is not None and named_index is not None and named_index != turn_index:
            continue
        legacy = summarize_turn(turn_dir, workspace_root=workspace_root)
        index = named_index if named_index is not None else _turn_index(turn_dir, legacy)
        if index is None or (turn_index is not None and index != turn_index):
            continue
        packet, packet_status = _packet_for_candidate(
            run_dir,
            turn_dir,
            index,
            workspace_root,
            legacy,
        )
        complete = packet_status != "conflict" and _candidate_has_complete_training_evidence(
            packet,
            workspace_root=workspace_root,
        )
        try:
            source_path = turn_dir.resolve().relative_to(run_root).as_posix()
        except (OSError, ValueError):
            continue
        groups.setdefault(index, []).append(
            {
                "turn_dir": turn_dir,
                "packet": packet,
                "packet_status": packet_status,
                "complete": complete,
                "source_turn_id": turn_dir.name,
                "source_path": source_path,
                "source_priority": _source_priority(run_dir, turn_dir),
            }
        )
    for versions in groups.values():
        versions.sort(key=lambda version: version["source_priority"])
    return groups


def _select_turn_record(versions: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [version for version in versions if version["complete"]]
    non_conflict = [version for version in versions if version["packet_status"] != "conflict"]
    return (complete or non_conflict or versions)[-1]


def _evidence_versions(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_turn_id": version["source_turn_id"],
            "source_path": version["source_path"],
            "packet_status": version["packet_status"],
            "complete": version["complete"],
            "turn_packet_sha256": packet_sha256(version["packet"]),
        }
        for version in versions
    ]


def _extra_timeline_events(run_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not run_dir.is_dir():
        return events
    for path in run_dir.iterdir():
        if not path.is_dir() or not _is_within(path, run_dir):
            continue
        index = _turn_index(path, {})
        name = path.name.lower()
        action = "event" if "event" in name else "race" if "race" in name else None
        if index is None or action is None:
            continue
        events.append({
            "turn_index": index,
            "timeline_kind": "event",
            "event_id": path.name,
            "event_label": path.name.replace("_", " "),
            "action_type": action,
        })
    return sorted(events, key=lambda event: (event["turn_index"], event["event_id"]))


def build_turn_record_index_v2(
    run_dir: str | Path,
    *,
    workspace_root: str | Path,
) -> TurnRecordIndexV2:
    """Build one bounded snapshot for a single dashboard request.

    Callers that need a timeline plus several details can pass this object back to
    both builders.  The object is intentionally not stored outside the request.
    """
    run = Path(run_dir).resolve()
    workspace = Path(workspace_root).resolve()
    return TurnRecordIndexV2(
        run_root=run,
        workspace_root=workspace,
        groups=_turn_record_groups(run, workspace),
        events=tuple(_extra_timeline_events(run)),
    )


def _validated_turn_record_index(
    run_dir: Path,
    workspace_root: str | Path,
    index: TurnRecordIndexV2 | None,
) -> TurnRecordIndexV2 | None:
    if index is None:
        return None
    run = run_dir.resolve()
    workspace = Path(workspace_root).resolve()
    if not isinstance(index, TurnRecordIndexV2) or index.run_root != run or index.workspace_root != workspace:
        raise DashboardLookupError("turn_record_index_mismatch")
    for versions in index.groups.values():
        for version in versions:
            turn_dir = version.get("turn_dir") if isinstance(version, dict) else None
            if not isinstance(turn_dir, Path) or not _is_within(turn_dir, run):
                raise DashboardLookupError("turn_record_index_invalid")
    return index


def canonical_turn_packet(
    turn_dir: str | Path,
    workspace_root: str | Path,
    *,
    legacy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = Path(turn_dir)
    legacy = legacy if isinstance(legacy, dict) else summarize_turn(path, workspace_root=workspace_root)
    index = _turn_index(path, legacy)
    game_state = _normalize_game_state(legacy.get("game_state") if isinstance(legacy.get("game_state"), dict) else {})
    execution = legacy.get("execution") if isinstance(legacy.get("execution"), dict) else {}
    return {
        "identity": {"run_id": path.parent.parent.name if path.parent.name == "turns" else path.parent.name, "turn_index": index},
        "game_state": game_state,
        "current_screenshot": legacy.get("current_screenshot"),
        "capture": legacy.get("capture") if isinstance(legacy.get("capture"), dict) else {},
        "support_progress": _load_tracker_or_unknown(path, "support_progress.json"),
        "outing_progress": _load_tracker_or_unknown(path, "outing_progress.json"),
        "condition": _load_condition_summary_or_unknown(path),
        "decision": legacy.get("decision") if isinstance(legacy.get("decision"), dict) else {},
        "safety_gate": _summarize_safety(execution),
        "execution": execution,
        "post_action": _load_json(path / "post_action.json") or {},
        "debug_evidence": _relative_debug_evidence(path, workspace_root),
    }


def packet_sha256(packet: dict[str, Any]) -> str:
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _conflict_packet(run_dir: Path, turn_index: int) -> dict[str, Any]:
    return {
        "identity": {"run_id": run_dir.name, "turn_index": turn_index},
        "game_state": {"current_stats": {key: None for key in _STAT_KEYS}},
        "current_screenshot": None,
        "capture": {},
        "support_progress": _unknown_progress(),
        "outing_progress": _unknown_progress(),
        "condition": _unknown_condition_summary(),
        "decision": {},
        "safety_gate": {"status": "conflict"},
        "execution": {},
        "post_action": {},
        "debug_evidence": [],
    }


def _packet_for_turn(
    run_dir: Path,
    turn_dir: Path,
    turn_index: int,
    workspace_root: str | Path,
    *,
    legacy: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    stored = _load_json(turn_dir / "turn_packet.json")
    if stored is not None:
        packet = stored.get("packet")
        stored_hash = stored.get("turn_packet_sha256")
        identity = packet.get("identity") if isinstance(packet, dict) else None
        condition = packet.get("condition") if isinstance(packet, dict) else None
        if (
            stored.get("schema_version") == "ug_parent_dashboard.v2.turn_packet"
            and isinstance(packet, dict)
            and isinstance(identity, dict)
            and identity.get("run_id") == run_dir.name
            and identity.get("turn_index") == turn_index
            and condition == _load_condition_summary_or_unknown(turn_dir)
            and isinstance(stored_hash, str)
            and packet_sha256(packet) == stored_hash
        ):
            return packet, "reliable"
        return _conflict_packet(run_dir, turn_index), "conflict"
    return canonical_turn_packet(turn_dir, workspace_root, legacy=legacy), "partial"


def _find_turn_dir(run_dir: Path, turn_index: int, workspace_root: str | Path) -> Path:
    if not isinstance(turn_index, int) or isinstance(turn_index, bool) or turn_index < 0:
        raise DashboardLookupError("turn_not_found")
    for turn_dir in find_turn_dirs(run_dir):
        turn = summarize_turn(turn_dir, workspace_root=workspace_root)
        if _turn_index(turn_dir, turn) == turn_index:
            return turn_dir
    raise DashboardLookupError("turn_not_found")


def _action_type(decision: dict[str, Any]) -> str:
    action = str(decision.get("action") or "").lower()
    if action in {"train", "training"}:
        return "training"
    if action in {"race", "event", "rest", "skill"}:
        return action
    return "none" if not action else "unknown"


_DEFAULT_DECK_MANIFEST = Path(__file__).resolve().parents[2] / "config" / "current_support_deck.json"


def build_turn_detail_v2(
    run_dir: str | Path,
    turn_index: int,
    *,
    workspace_root: str | Path,
    deck_manifest_path: str | Path | None = None,
    turn_record_index: TurnRecordIndexV2 | None = None,
) -> dict[str, Any]:
    run = Path(run_dir)
    index = _validated_turn_record_index(run, workspace_root, turn_record_index)
    groups = index.groups if index is not None else _turn_record_groups(
        run,
        workspace_root,
        turn_index=turn_index,
    )
    versions = groups.get(turn_index)
    if not versions:
        raise DashboardLookupError("turn_not_found")
    selected = _select_turn_record(versions)
    turn_dir = selected["turn_dir"]
    packet = selected["packet"]
    packet_status = selected["packet_status"]
    turn_packet_sha256 = packet_sha256(packet)
    progress = build_progress_snapshot(
        run,
        turn_dir,
        deck_manifest_path=deck_manifest_path or _DEFAULT_DECK_MANIFEST,
    )
    return {
        "schema_version": "ug_parent_dashboard.v2.turn_detail",
        "packet_status": packet_status,
        "evidence_versions": _evidence_versions(versions),
        "selected_evidence_version": _evidence_versions([selected])[0],
        "event_choice_shadow_audit": _load_event_choice_shadow_audit(turn_dir),
        "perception_reliability": _load_perception_reliability_audit(
            turn_dir,
            run_dir=run,
            packet=packet,
            packet_status=packet_status,
            workspace_root=workspace_root,
            expected_turn_index=turn_index,
        ),
        **packet,
        "turn_packet_sha256": turn_packet_sha256,
        "training_evidence": build_training_evidence(
            packet,
            turn_packet_sha256=turn_packet_sha256,
            workspace_root=workspace_root,
            archived_image_paths=_archived_training_image_paths(
                turn_dir,
                packet,
                turn_packet_sha256=turn_packet_sha256,
                workspace_root=workspace_root,
            ),
        ),
        "support_progress": progress["support_progress"],
        "outing_progress": progress["outing_progress"],
        "career_race": _build_career_race_view(run, turn_index),
    }


def build_timeline_v2(
    run_dir: str | Path,
    *,
    workspace_root: str | Path,
    turn_record_index: TurnRecordIndexV2 | None = None,
) -> dict[str, Any]:
    run = Path(run_dir)
    index = _validated_turn_record_index(run, workspace_root, turn_record_index)
    index = index or build_turn_record_index_v2(run, workspace_root=workspace_root)
    race_context = _load_race_calendar_context(run)
    points = []
    groups = index.groups
    for point_turn_index, versions in groups.items():
        selected = _select_turn_record(versions)
        packet = selected["packet"]
        evidence_status = selected["packet_status"]
        game_state = packet.get("game_state") if isinstance(packet.get("game_state"), dict) else {}
        decision = packet.get("decision") if isinstance(packet.get("decision"), dict) else {}
        execution = packet.get("execution") if isinstance(packet.get("execution"), dict) else {}
        action = _action_type(decision)
        if execution.get("performed") is True:
            status = "executed"
        elif decision.get("action"):
            status = "planned"
        elif action == "none":
            status = "no_action"
        else:
            status = "unknown"
        points.append({
            "turn_index": point_turn_index,
            "timeline_kind": "action",
            "period_text": game_state.get("period_text"),
            "action_type": action,
            "action_target": decision.get("target"),
            "status": status,
            "evidence_status": evidence_status,
            "turn_packet_sha256": packet_sha256(packet),
            "race_calendar": _timeline_race_entries(
                race_context,
                point_turn_index,
            ),
            "race_schedule_status": _race_schedule_status_for_turn(
                run,
                point_turn_index,
            ),
            "evidence_version_count": len(versions),
        })
    for event in index.events:
        versions = groups.get(event["turn_index"], [])
        selected = _select_turn_record(versions) if versions else None
        points.append({
            "turn_index": event["turn_index"],
            "timeline_kind": "event",
            "event_id": event["event_id"],
            "event_label": event["event_label"],
            "period_text": None,
            "action_type": event["action_type"],
            "action_target": None,
            "status": "observed",
            "evidence_status": selected["packet_status"] if selected else "event_only",
            "turn_packet_sha256": packet_sha256(selected["packet"]) if selected else None,
            "race_calendar": _timeline_race_entries(race_context, event["turn_index"]),
            "race_schedule_status": _race_schedule_status_for_turn(run, event["turn_index"]),
            "evidence_version_count": len(versions),
        })
    points.sort(key=lambda point: (point["turn_index"], 0 if point["timeline_kind"] == "action" else 1, point.get("event_id", "")))
    return {"schema_version": "ug_parent_dashboard.v2.timeline", "run_id": run.name, "points": points}


def _build_career_race_view(
    run_dir: Path,
    turn_index: int,
) -> dict[str, Any]:
    required = (
        run_dir / "career_goal_ledger.json",
        run_dir / "race_calendar_manifest.json",
        run_dir / "race_plan_snapshot.json",
    )
    if any(not path.is_file() for path in required):
        return {
            "status": "unknown",
            "current_turn": turn_index,
            "reserve_opportunities": None,
            "active_goals": [],
            "verified_opportunities": [],
            "directives": [],
            "current_decision": {"status": "unknown"},
            "recent_results": [],
            "warnings": ["career_race_evidence_missing"],
            "artifact_sha256": None,
        }
    warnings: list[str] = []
    decision_path = (
        run_dir / "race_schedule_decisions" / f"turn_{turn_index:03d}.json"
    )
    paths = [*required]
    if decision_path.is_file():
        paths.append(decision_path)
    try:
        ledger = CareerGoalLedgerStore().load(
            required[0],
            expected_run_session_id=run_dir.name,
        )
        race_context = _load_race_calendar_context(run_dir)
        if race_context["status"] != "verified":
            raise DashboardLookupError("race_calendar_context_invalid")
        calendar = race_context["calendar"]
        plan = race_context["plan"]
        decision_payload = (
            _load_json(decision_path)
            if decision_path.is_file()
            else None
        )
        current_decision, ledger_view = _validated_schedule_decision(
            decision_payload,
            run_dir=run_dir,
            turn_index=turn_index,
            ledger=ledger,
        )
        if current_decision["status"] == "unknown":
            warnings.append("race_schedule_decision_missing")
        opportunity_map = current_decision.get(
            "verified_opportunity_race_ids",
            {},
        )
        active_goals = []
        goals = ledger_view["goals"]
        for goal in goals:
            if goal["status"] != "active":
                continue
            required_remaining = max(
                int(goal["required_count"])
                - int(goal["completed_count"]),
                0,
            )
            active_goals.append(
                {
                    **dict(goal),
                    "required_remaining": required_remaining,
                    "force_by_turn": _force_by_turn(
                        calendar,
                        opportunity_map.get(goal["goal_instance_id"], []),
                        required_remaining=required_remaining,
                        reserve_opportunities=plan.reserve_opportunities,
                    ),
                }
            )
        opportunity_ids = {
            race_id
            for values in opportunity_map.values()
            for race_id in values
        }
        verified_opportunities = [
            _calendar_entry_for_dashboard(calendar.get(race_id))
            for race_id in sorted(opportunity_ids)
            if calendar.get(race_id) is not None
        ]
        directives = [
            {
                "race_id": item.race_id,
                "directive": item.directive,
                "priority": item.priority,
                "source": item.source,
                "enabled": item.enabled,
                "executable": item.executable,
                "outcome_requirement": (
                    dict(item.outcome_requirement)
                    if item.outcome_requirement is not None
                    else None
                ),
            }
            for item in plan.directives
        ]
        recent_results = []
        for item in ledger_view["race_results"][-5:]:
            observation_sha256 = item.get("observation_sha256")
            if not _is_sha256(observation_sha256):
                raise DashboardLookupError(
                    "career_race_result_binding_invalid"
                )
            recent_results.append(
                {
                    **dict(item),
                    "progressed_goal_ids": [
                        goal["goal_instance_id"]
                        for goal in goals
                        if observation_sha256
                        in goal["evidence_refs"]
                    ],
                }
            )
    except (
        CareerGoalLedgerError,
        DashboardLookupError,
        RaceCalendarError,
        RacePlanError,
        TypeError,
        ValueError,
    ) as exc:
        return {
            "status": "conflict",
            "current_turn": turn_index,
            "reserve_opportunities": None,
            "active_goals": [],
            "verified_opportunities": [],
            "directives": [],
            "current_decision": {"status": "conflict"},
            "recent_results": [],
            "warnings": [f"career_race_evidence_conflict:{exc}"],
            "artifact_sha256": _artifact_sha256(paths),
        }
    return {
        "status": "verified",
        "current_turn": turn_index,
        "reserve_opportunities": plan.reserve_opportunities,
        "active_goals": active_goals,
        "verified_opportunities": verified_opportunities,
        "directives": directives,
        "current_decision": current_decision,
        "recent_results": recent_results,
        "warnings": warnings,
        "artifact_sha256": _artifact_sha256(paths),
    }


def _load_race_calendar_context(run_dir: Path) -> dict[str, Any]:
    manifest = _load_json(run_dir / "race_calendar_manifest.json")
    snapshot = _load_json(run_dir / "race_plan_snapshot.json")
    if not isinstance(manifest, dict) or not isinstance(snapshot, dict):
        return {"status": "unknown", "calendar": None, "plan": None}
    try:
        calendar = RaceCalendar.load(_DEFAULT_RACE_CALENDAR)
        if (
            manifest.get("schema_version")
            != "race_calendar_manifest.v1"
            or manifest.get("run_session_id") != run_dir.name
            or manifest.get("calendar_id") != calendar.calendar_id
            or manifest.get("calendar_sha256")
            != calendar.calendar_sha256
            or manifest.get("apk_database_sha256")
            != calendar.apk_database_sha256
            or manifest.get("entry_count") != len(calendar.entries)
        ):
            raise DashboardLookupError("race_calendar_manifest_mismatch")
        plan_payload = dict(snapshot)
        if plan_payload.pop("run_session_id", None) != run_dir.name:
            raise DashboardLookupError("race_plan_run_mismatch")
        plan = RacePlan.from_dict(plan_payload)
    except (
        DashboardLookupError,
        RaceCalendarError,
        RacePlanError,
        TypeError,
        ValueError,
    ):
        return {"status": "conflict", "calendar": None, "plan": None}
    return {"status": "verified", "calendar": calendar, "plan": plan}


def _validated_schedule_decision(
    payload: dict[str, Any] | None,
    *,
    run_dir: Path,
    turn_index: int,
    ledger: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if payload is None:
        return (
            {
                "status": "unknown",
                "candidates": [],
                "verified_opportunity_race_ids": {},
            },
            _current_ledger_view(ledger),
        )
    required_keys = {
        "schema_version",
        "run_session_id",
        "turn_index",
        "source_frame_sha256",
        "ledger_path",
        "ledger_revision",
        "ledger_view_sha256",
        "decision",
    }
    if (
        set(payload) != required_keys
        or payload.get("schema_version") != "race_schedule_decision.v1"
        or payload.get("run_session_id") != run_dir.name
        or payload.get("turn_index") != turn_index
        or not isinstance(payload.get("ledger_path"), str)
        or Path(payload["ledger_path"]).resolve()
        != (run_dir / "career_goal_ledger.json").resolve()
        or not _is_sha256(payload.get("source_frame_sha256"))
    ):
        raise DashboardLookupError("race_schedule_binding_mismatch")
    ledger_view = _ledger_view_for_revision(
        ledger,
        payload.get("ledger_revision"),
        payload.get("ledger_view_sha256"),
    )
    decision = payload.get("decision")
    if not isinstance(decision, dict):
        raise DashboardLookupError("race_schedule_decision_invalid")
    if (
        not isinstance(decision.get("status"), str)
        or not _is_sha256(decision.get("packet_sha256"))
        or not isinstance(decision.get("candidates"), list)
        or not isinstance(
            decision.get("verified_opportunity_race_ids"),
            dict,
        )
    ):
        raise DashboardLookupError("race_schedule_decision_invalid")
    for candidate in decision["candidates"]:
        if (
            not isinstance(candidate, dict)
            or not _is_sha256(candidate.get("evidence_sha256"))
            or candidate.get("action_kind")
            not in {"enter_race", "defer_to_local_policy", "stop"}
        ):
            raise DashboardLookupError("race_schedule_candidate_invalid")
    return json.loads(json.dumps(decision)), ledger_view


def _current_ledger_view(ledger: Any) -> dict[str, Any]:
    if ledger.revisions:
        return json.loads(json.dumps(ledger.revisions[-1]["view"]))
    return {
        "run_session_id": ledger.run_session_id,
        "revision": ledger.revision,
        "observed_turn": ledger.observed_turn,
        "goals": [goal.to_dict() for goal in ledger.goals],
        "race_results": [dict(result) for result in ledger.race_results],
    }


def _ledger_view_for_revision(
    ledger: Any,
    revision: object,
    expected_view_sha256: object,
) -> dict[str, Any]:
    if (
        type(revision) is not int
        or revision <= 0
        or not _is_sha256(expected_view_sha256)
    ):
        raise DashboardLookupError("race_schedule_binding_mismatch")
    record = next(
        (
            item
            for item in ledger.revisions
            if item.get("revision") == revision
        ),
        None,
    )
    if not isinstance(record, dict):
        raise DashboardLookupError("race_schedule_binding_mismatch")
    view = record.get("view")
    revision_sha256 = record.get("revision_sha256")
    if not isinstance(view, dict) or not _is_sha256(revision_sha256):
        raise DashboardLookupError("race_schedule_binding_mismatch")
    actual_view_sha256 = hashlib.sha256(
        json.dumps(
            {
                "view": view,
                "head_revision_sha256": revision_sha256,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if actual_view_sha256 != expected_view_sha256:
        raise DashboardLookupError("race_schedule_binding_mismatch")
    return json.loads(json.dumps(view))


def _force_by_turn(
    calendar: RaceCalendar,
    race_ids: object,
    *,
    required_remaining: int,
    reserve_opportunities: int,
) -> int | None:
    if not isinstance(race_ids, list):
        return None
    turns = sorted(
        {
            entry.career_time.global_turn
            for race_id in race_ids
            if isinstance(race_id, str)
            for entry in [calendar.get(race_id)]
            if entry is not None
            and entry.career_time is not None
            and entry.schedule_status == "verified"
        }
    )
    if not turns:
        return None
    threshold = required_remaining + reserve_opportunities
    index = max(0, len(turns) - threshold)
    return turns[min(index, len(turns) - 1)]


def _timeline_race_entries(
    context: dict[str, Any],
    turn_index: int,
) -> list[dict[str, Any]]:
    if context.get("status") != "verified":
        return []
    calendar = context["calendar"]
    plan = context["plan"]
    directives = {
        item.race_id: item.directive
        for item in plan.directives
        if item.executable
    }
    return [
        {
            **_calendar_entry_for_dashboard(entry),
            "directive": directives.get(entry.race_id),
        }
        for entry in calendar.query_turn(turn_index, formal_only=True)
    ]


def _race_schedule_status_for_turn(
    run_dir: Path,
    turn_index: int,
) -> str:
    payload = _load_json(
        run_dir
        / "race_schedule_decisions"
        / f"turn_{turn_index:03d}.json"
    )
    decision = payload.get("decision") if isinstance(payload, dict) else None
    status = decision.get("status") if isinstance(decision, dict) else None
    return status if isinstance(status, str) else "unknown"


def _calendar_entry_for_dashboard(entry: Any) -> dict[str, Any]:
    career_time = entry.career_time
    names = dict(entry.names)
    name = (
        names.get("zh_cn")
        or names.get("zh")
        or names.get("ja")
        or entry.race_id
    )
    return {
        "race_id": entry.race_id,
        "name": name,
        "grade": entry.grade,
        "global_turn": (
            career_time.global_turn
            if career_time is not None
            else None
        ),
    }


def _artifact_sha256(paths: list[Path]) -> str | None:
    if not paths or any(not path.is_file() for path in paths):
        return None
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_final_summary_v2(run_dir: str | Path, *, workspace_root: str | Path) -> dict[str, Any]:
    del workspace_root
    run = Path(run_dir)
    source = _load_json(run / "final_summary.json") or {}
    payload: dict[str, Any] = {"schema_version": "ug_parent_dashboard.v2.final_summary", "run_id": run.name}
    for section in _FINAL_SECTIONS:
        value = source.get(section)
        payload[section] = value if isinstance(value, dict) else {"status": "unknown"}
    return payload


def build_career_setup_status_v2(
    run_dir: str | Path,
    *,
    deck_manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    run = Path(run_dir)
    verified_support_deck = build_verified_deck_status(
        run,
        deck_manifest_path=deck_manifest_path or _DEFAULT_DECK_MANIFEST,
    )
    try:
        roots = [
            path for path in run.iterdir()
            if path.is_dir() and path.name.startswith("career_setup")
        ]
        candidates = sorted(
            (
                path
                for root in roots
                for path in _rglob(root, "career_setup_result.json")
                if _io(path).is_file()
            ),
            key=lambda path: _io(path).stat().st_mtime,
            reverse=True,
        )
    except OSError:
        candidates = []
    if not candidates:
        return {
            "schema_version": "ug_parent_dashboard.v2.career_setup",
            "status": "unknown",
            "verified_support_deck": verified_support_deck,
        }
    path = candidates[0]
    try:
        raw = _io(path).read_bytes()
        source = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        source = None
        raw = b""
    if not isinstance(source, dict) or source.get("schema_version") != "uma.career_setup_cli.v1":
        return {
            "schema_version": "ug_parent_dashboard.v2.career_setup",
            "status": "conflict",
            "evidence_sha256": hashlib.sha256(raw).hexdigest(),
            "verified_support_deck": verified_support_deck,
        }
    packet = source.get("packet")
    session = source.get("session_after")
    execution = source.get("execution")
    if not isinstance(session, dict) or not isinstance(execution, dict):
        return {
            "schema_version": "ug_parent_dashboard.v2.career_setup",
            "status": "conflict",
            "evidence_sha256": hashlib.sha256(raw).hexdigest(),
            "verified_support_deck": verified_support_deck,
        }
    expected = session.get("expected_page_states")
    if not isinstance(expected, list) or any(not isinstance(item, str) for item in expected):
        return {
            "schema_version": "ug_parent_dashboard.v2.career_setup",
            "status": "conflict",
            "evidence_sha256": hashlib.sha256(raw).hexdigest(),
            "verified_support_deck": verified_support_deck,
        }
    payload: dict[str, Any] = {
        "schema_version": "ug_parent_dashboard.v2.career_setup",
        "status": "observed",
        "session_status": session.get("status"),
        "expected_page": expected[0] if len(expected) == 1 else expected,
        "last_action": {
            "tap_count": execution.get("tap_count"),
            "reason": execution.get("reason"),
        },
        "evidence_sha256": hashlib.sha256(raw).hexdigest(),
        "verified_support_deck": verified_support_deck,
    }
    if isinstance(packet, dict):
        identities = packet.get("identities")
        matches = packet.get("identity_matches")
        if not (
            isinstance(identities, list)
            and isinstance(matches, list)
            and len(identities) == len(matches)
            and all(type(item) is bool for item in matches)
            and all(isinstance(item, str) for item in identities)
        ):
            return {
                "schema_version": "ug_parent_dashboard.v2.career_setup",
                "status": "conflict",
                "evidence_sha256": hashlib.sha256(raw).hexdigest(),
                "verified_support_deck": verified_support_deck,
            }
        payload.update({
            "page_state": packet.get("page_state"),
            "source_sha256": packet.get("source_sha256"),
            "action_id": packet.get("action_id"),
            "allowlist_matches": matches,
            "allowlist_identities": [
                identity if matched else "未决"
                for identity, matched in zip(identities, matches, strict=True)
            ],
        })
    return payload
