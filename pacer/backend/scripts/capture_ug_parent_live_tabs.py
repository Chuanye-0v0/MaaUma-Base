from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.auto_trainer.live_capture_package import build_live_capture_package
from core.auto_trainer.pacer_storage import _io
from core.auto_trainer.executor import DefaultTrainingExecutionVerifier
from core.auto_trainer.route_transition_session import RouteTransitionSession
from core.auto_trainer.state import TRAINING_TYPES
from core.bounded_training_tab_capture import (
    GAIN_DISPLAY_MAX_FOREGROUND_XOR_RATIO,
    POST_TAP_INTERVAL_SECONDS,
    POST_TAP_MAX_ATTEMPTS,
    POST_TAP_REQUIRED_CONSECUTIVE_MATCHES,
    gain_display_foreground_xor_ratio,
)
from core.coordinates import CoordinateConfig
from core.training_menu_precheck import TrainingMenuPrecheckResult, TrainingMenuPrechecker


CONFIRM_TOKEN = "CAPTURE_UG_PARENT_LIVE_TABS"
FORMAL_STABLE_CAPTURE_MAX_ATTEMPTS = 16  # Slow L'Arc charge previews; successful pages still exit early.


def _observation_precheck_result(result, *, scenario):
    """Treat a missing color strip as an observation warning on proven L'Arc tabs."""
    if scenario != 'larc' or result.failures != ['failure_rate_region']:
        return result
    return replace(result, accepted=True, reason='training_menu_observation_without_failure_region',
                   evidence={**result.evidence, 'observation_warning': 'failure_rate_region'}, failures=[])


def _stable_capture_readers(scenario, gain_reader, participant_reader):
    """The native/portable fast wait only observes the UI tab, not gains."""
    if scenario == 'larc' and gain_reader is None:
        from core.lightweight_gain_reader import LightweightGainReader
        from core.larc_support_reader import LarcSupportReader
        gain_reader = LightweightGainReader()
        participant_reader = LarcSupportReader()
    return gain_reader, participant_reader


def _main_menu_entry_check(png_bytes, image, *, scenario):
    """Keep the old entry decision, skipping only fields it never consults."""
    from core.main_menu_state import analyze_main_menu_state, analyze_main_menu_navigation
    from core.auto_trainer.main_menu_state_provider import main_menu_navigation_check
    observation = (analyze_main_menu_navigation(png_bytes) if scenario == 'larc'
        else analyze_main_menu_state(png_bytes, scenario=scenario))
    return main_menu_navigation_check(observation, image, scenario=scenario)


def _publish_initial_precheck_attempts(
    *,
    precheck_path: Path,
    raw_dir: Path,
    selected_tab: str,
) -> list[Path]:
    """Publish verified initial-tab frames where sidecar consensus reads them.

    The first selected training tab is observed before any tab tap, so its
    normalized precheck frames live under ``precheck/``.  Later tabs already
    write their frames below ``raw_720/``.  Keep the original precheck evidence
    intact while copying only this selected tab's current stable-attempt frames
    into the common raw namespace used by field-level consensus.
    """
    _io(raw_dir).mkdir(parents=True, exist_ok=True)
    for stale_path in _io(raw_dir).glob(f"{selected_tab}.attempt-*.precheck_*.png"):
        stale_path.unlink(missing_ok=True)

    source_pattern = f"{precheck_path.stem}.attempt-*.precheck_*.png"
    source_paths = sorted(_io(precheck_path.parent).glob(source_pattern))
    published: list[Path] = []
    for source_path in source_paths:
        suffix = source_path.name.removeprefix(f"{precheck_path.stem}.")
        destination = raw_dir / f"{selected_tab}.{suffix}"
        shutil.copy2(_io(source_path), _io(destination))
        published.append(destination)
    return published


def _wait_for_stable_capture(
    *,
    adb,
    final_path: Path,
    verify,
    sleep_seconds: float,
    allowed_selected: set[str],
    reject_conflicting_accepted_selected: bool = False,
    tolerate_conflicting_trailing_selected: str | set[str] | None = None,
    max_attempts: int = POST_TAP_MAX_ATTEMPTS,
    gain_stability_roi: tuple[int, int, int, int] | None = None,
    gain_reader=None,
    participant_reader=None,
    capture_image=None,
) -> tuple[Path, object]:
    for stale_path in _io(final_path.parent).glob(
        f"{final_path.stem}.attempt-*.precheck_*.png"
    ):
        stale_path.unlink(missing_ok=True)
    consecutive = 0
    last_match: str | None = None
    last_result = None
    had_matched_attempt = False
    accepted_selected_seen: set[str] = set()
    attempt_trace: list[str] = []
    attempt_paths: list[Path] = []
    last_matching_frame_path: Path | None = None
    last_gain_display_difference: float | None = None
    real_precheck_seen = False
    screenshot_failure_count = 0
    charge_preview_ready = True
    direct_screenshot = getattr(type(adb), "screenshot_exec_out", None)

    try:
        for attempt in range(1, max_attempts + 1):
            time.sleep(sleep_seconds if attempt == 1 else POST_TAP_INTERVAL_SECONDS)
            attempt_path = final_path.with_name(f"{final_path.stem}.attempt-{attempt:02d}.png")
            attempt_paths.append(attempt_path)
            image = (capture_image(str(attempt_path)) if capture_image is not None
                     else direct_screenshot(adb, str(attempt_path)) if direct_screenshot is not None
                     else adb.screenshot(str(attempt_path)))
            if image is None:
                screenshot_failure_count += 1
                attempt_trace.append(f"attempt={attempt} screenshot_failed")
                consecutive = 0
                last_match = None
                last_matching_frame_path = None
                last_gain_display_difference = None
                continue
            if not _io(attempt_path).exists():
                _io(attempt_path).touch()
            result = verify(attempt_path)
            real_precheck_seen = real_precheck_seen or isinstance(
                result,
                TrainingMenuPrecheckResult,
            )
            last_result = result
            selected = getattr(result, "selected_tab", None)
            accepted = getattr(result, "accepted", False)
            reason = getattr(result, "reason", "unknown")
            attempt_trace.append(
                f"attempt={attempt} accepted={accepted} "
                f"reason={reason} selected_tab={selected}"
            )
            if accepted and selected in allowed_selected:
                accepted_selected_seen.add(str(selected))
            matched_selected = (
                selected
                if accepted and selected in allowed_selected
                else None
            )
            if matched_selected is None:
                consecutive = 0
                last_match = None
                last_matching_frame_path = None
                last_gain_display_difference = None
                if not real_precheck_seen and attempt >= POST_TAP_MAX_ATTEMPTS:
                    break
                continue
            had_matched_attempt = True
            if matched_selected == last_match:
                consecutive += 1
                if (
                    isinstance(result, TrainingMenuPrecheckResult)
                    and last_matching_frame_path is not None
                ):
                    last_gain_display_difference = gain_display_foreground_xor_ratio(
                        last_matching_frame_path,
                        attempt_path,
                        **({"roi": gain_stability_roi} if gain_stability_roi is not None else {}),
                    )
                    attempt_trace[-1] += (
                        " gain_display_foreground_xor="
                        f"{last_gain_display_difference:.6f}"
                    )
            else:
                consecutive = 1
                last_match = matched_selected
                last_gain_display_difference = None
            requires_gain_stability = isinstance(result, TrainingMenuPrecheckResult)
            if consecutive >= POST_TAP_REQUIRED_CONSECUTIVE_MATCHES:
                if isinstance(tolerate_conflicting_trailing_selected, set):
                    conflict_is_tolerated = (
                        matched_selected in tolerate_conflicting_trailing_selected
                    )
                else:
                    conflict_is_tolerated = (
                        tolerate_conflicting_trailing_selected is not None
                        and matched_selected == tolerate_conflicting_trailing_selected
                    )
                if (
                    reject_conflicting_accepted_selected
                    and len(accepted_selected_seen) > 1
                    and not conflict_is_tolerated
                ):
                    raise RuntimeError(
                        "precheck_conflicting_selected_tab:"
                        + " | ".join(attempt_trace)
                    )
                gain_display_is_stable = (
                    not requires_gain_stability
                    or (
                        last_gain_display_difference is not None
                        and last_gain_display_difference
                        <= GAIN_DISPLAY_MAX_FOREGROUND_XOR_RATIO
                    )
                )
                if not gain_display_is_stable and gain_reader is not None and last_matching_frame_path is not None:
                    # Red clothing can animate inside the L'Arc ROI. Compare
                    # the existing reader's two rows, not only their sums.
                    earlier_rows = _verified_larc_gain_rows(last_matching_frame_path, gain_reader)
                    later_rows = _verified_larc_gain_rows(attempt_path, gain_reader)
                    gain_display_is_stable = earlier_rows is not None and earlier_rows == later_rows
                    attempt_trace[-1] += f" verified_larc_gain_rows_match={gain_display_is_stable}"
                if gain_display_is_stable:
                    charge_preview_ready = True
                    if participant_reader is not None and matched_selected != "ss":
                        participants = participant_reader.read(image)
                        charges = [row["charge"] for row in participants["participants"]
                                   if row["charge"]["status"] != "not_visible"]
                        charge_preview_ready = participants["status"] == "accepted" and all(
                            charge["status"] == "accepted" for charge in charges)
                        attempt_trace[-1] += f" charge_preview_ready={charge_preview_ready}"
                    if charge_preview_ready:
                        os.replace(str(_io(attempt_path)), str(_io(final_path)))
                        attempt_paths.remove(attempt_path)
                        return final_path, result
            last_matching_frame_path = attempt_path
            if not real_precheck_seen and attempt >= POST_TAP_MAX_ATTEMPTS:
                break
        if not charge_preview_ready:
            raise RuntimeError("precheck_charge_preview_not_ready:" + " | ".join(attempt_trace))
        if had_matched_attempt:
            if last_gain_display_difference is not None:
                raise RuntimeError(
                    "precheck_gain_display_unstable:"
                    + " | ".join(attempt_trace)
                )
            raise RuntimeError(
                "precheck_unstable_selected_tab:"
                + " | ".join(attempt_trace)
            )
        if screenshot_failure_count == max_attempts:
            raise RuntimeError(
                f"screenshot_failed:{final_path.stem}:attempts={max_attempts}"
            )
        raise RuntimeError(str(getattr(last_result, "reason", "unknown")))
    finally:
        for attempt_path in attempt_paths:
            _io(attempt_path).unlink(missing_ok=True)


def _larc_participant_signature(evidence) -> tuple | None:
    if not isinstance(evidence, dict) or evidence.get("status") != "accepted":
        return None
    rows = []
    for participant in evidence.get("participants", []):
        role = participant["role"]
        bond = participant["bond"]
        charge = participant["charge"]
        hint = participant["training_hint"]
        reward = participant["ss_reward"]
        rows.append((
            participant["slot"], role.get("kind"), role.get("icon_type"),
            bond.get("status"), bond.get("color"), bond.get("is_max"),
            charge.get("status"), charge.get("filled_cells"),
            charge.get("preview_cells"), charge.get("is_full"),
            hint.get("status"), hint.get("present"),
            reward.get("status"), reward.get("kind"), reward.get("amount"),
        ))
    return tuple(rows)


def _verified_larc_gain_rows(path: Path, reader) -> tuple | None:
    import cv2

    image = cv2.imread(str(_io(path)))
    if image is None or image.shape[:2] not in {(1280, 720), (2560, 1440)}:
        return None
    if image.shape[:2] == (2560, 1440):
        image = cv2.resize(image, (720, 1280))
    return _larc_gain_rows_from_image(image, reader)


def _larc_gain_rows_from_image(image, reader) -> tuple | None:
    import cv2
    from core.lightweight_gain_reader import FIELDS

    if image.shape[:2] == (2560, 1440):
        image = cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
    gains = reader.read_gains(image, scenario="larc")
    rows = tuple((row["status"], row["value"]) for field in FIELDS for row in gains[field]["rows"])
    if (len(rows) != 12 or not any(status == "accepted" for status, _ in rows)
            or any(status not in {"accepted", "absent", "unresolved"} for status, _ in rows)):
        return None
    # Raw capture is not a numeric correctness gate. Preserve an unreadable
    # row as unresolved/None for the parser and candidate scoring; one such
    # row must not prevent collecting the other four pages or an SS preview.
    return rows


def _selected_larc_tab_fast(image) -> str | None:
    """Read only the selected Decide chevrons while polling a MuMu frame."""
    import cv2
    import numpy as np

    if image is None or image.shape != (1280, 720, 3):
        return None
    hsv = cv2.cvtColor(image[995:1175], cv2.COLOR_BGR2HSV)
    selected = []
    for tab, center in zip((*TRAINING_TYPES, "ss"), (67, 184, 302, 420, 538, 654)):
        ribbon = hsv[95:180, center - 48:center + 48]
        gold = (
            (ribbon[:, :, 0] >= 15) & (ribbon[:, :, 0] <= 38)
            & (ribbon[:, :, 1] > 100) & (ribbon[:, :, 2] > 150)
        ).astype(np.uint8)
        _, labels, components, _ = cv2.connectedComponentsWithStats(gold, connectivity=8)
        chevrons = []
        for label, row in enumerate(components[1:], start=1):
            x, y, width, height, area = row
            if width < 48 or not 12 <= height <= 35 or area < 250:
                continue
            component = labels[y:y + height, x:x + width] == label
            columns = component[:, [width // 10, width // 2, width * 9 // 10]]
            tops = np.argmax(columns, axis=0)
            if columns.any(axis=0).all() and tops[1] + 3 <= min(tops[0], tops[2]):
                chevrons.append(row)
        decided = (
            any(row[cv2.CC_STAT_TOP] < 28 for row in chevrons)
            and any(row[cv2.CC_STAT_TOP] > 40 for row in chevrons)
        )
        if tab == "ss":
            star = hsv[0:65, center - 48:center + 48]
            decided = decided and float(np.mean(
                (star[:, :, 0] >= 90) & (star[:, :, 0] <= 125)
                & (star[:, :, 1] > 70) & (star[:, :, 2] > 130)
            )) > 0.35
        if decided:
            selected.append(tab)
    return selected[0] if len(selected) == 1 else None


def _wait_for_native_capture(
    *, frame_stream, final_path: Path, verify, allowed_selected: set[str],
    gain_reader, participant_reader, reject_conflicting_accepted_selected=False,
    timeout_seconds: float = 4.0,
) -> tuple[Path, object]:
    """Wait for the selected UI tab; raw capture is not a perception audit."""
    deadline = time.monotonic() + timeout_seconds
    started=deadline-timeout_seconds;capture_seconds=0.
    previous = None
    seen: set[str] = set()
    attempts = 0
    last_selected = None
    last_precheck = None
    try:
        while time.monotonic() < deadline:
            capture_started=time.monotonic()
            image = (frame_stream.read_image(deadline=deadline) if getattr(frame_stream,'accepts_deadline',False)
                     else frame_stream.read_image())
            capture_seconds+=time.monotonic()-capture_started
            attempts += 1
            selected = _selected_larc_tab_fast(image)
            last_selected = selected
            if selected not in allowed_selected:
                previous = None
                time.sleep(0.015)
                continue
            if reject_conflicting_accepted_selected:
                seen.add(selected)
                if len(seen) > 1:
                    raise RuntimeError(f"native_conflicting_selected_tabs:{sorted(seen)}")
            # The actual parser reads these pixels after collection. Do not
            # duplicate its numeric/support/charge decisions while waiting for
            # the UI, or a blinking icon/uncertain digit can stop the whole run.
            current = selected
            if current == previous:
                frame_stream.save_image(str(final_path), image)
                result = (verify(final_path) if verify is not None else TrainingMenuPrecheckResult(
                    accepted=True,
                    reason="native_stable_larc_training_tab",
                    selected_tab=selected,
                    evidence={"source_resolution": [720, 1280], "stable_frames": 2,
                              "capture_backend":getattr(frame_stream,'transport','mumu-ipc'),
                              "capture_scope": "ui_page_only", "perception_verified": False},
                    failures=[],
                ))
                if getattr(result, "accepted", False) and getattr(result, "selected_tab", None) == selected:
                    result.evidence['capture_timing']={'tab':selected,'frames':attempts,
                        'capture_seconds':round(capture_seconds,4),'total_seconds':round(time.monotonic()-started,4),
                        'backend':getattr(frame_stream,'transport','mumu-ipc')}
                    return final_path, result
                last_precheck = getattr(result, "reason", "unknown")
                previous = None
            else:
                previous = current
            time.sleep(0.015)
        raise RuntimeError(
            f"native_capture_timeout:allowed={sorted(allowed_selected)}:"
            f"last_selected={last_selected}:frames={attempts}:last_precheck={last_precheck}"
        )
    except Exception:
        _io(final_path).unlink(missing_ok=True)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture five UG parent training tabs from a live emulator."
    )
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--actual-group-id", required=True)
    parser.add_argument("--run-session-id", required=True)
    parser.add_argument("--turn-index", type=int, required=True)
    parser.add_argument("--source-state-key", required=True)
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--state-context-json", required=True)
    parser.add_argument("--transition-session-json")
    parser.add_argument("--adb-port", type=int, default=None)
    parser.add_argument("--settle-seconds", type=float, default=0.9)
    parser.add_argument(
        "--capture-transport", choices=("auto", "direct-png", "raw-stream", "mumu-ipc", "adb-png"),
        default="auto",
        help="auto uses MuMu IPC for device 2's L'Arc pages, otherwise direct PNG",
    )
    parser.add_argument(
        "--start-screen",
        choices=["main-menu", "training-menu"],
        default="main-menu",
        help="Screen where live capture starts. Defaults to the main training screen.",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-token")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--refresh-tabs", nargs="+", choices=TRAINING_TYPES,
                        help="Refresh at most two tabs in an already captured, unconsumed turn.")
    parser.add_argument("--prior-manifest")
    parser.add_argument("--prior-execution-root")
    return parser.parse_args(argv)


def _refresh_sources(args, state_context):
    """Reuse only verified pages from this exact run, turn and capture group."""
    tabs = getattr(args, 'refresh_tabs', None)
    if not tabs:
        if args.prior_manifest or args.prior_execution_root:
            raise ValueError('refresh_tabs_required')
        return {}, None
    if (len(tabs) > 2 or len(set(tabs)) != len(tabs) or not args.prior_manifest
            or not args.prior_execution_root or args.start_screen != 'training-menu'):
        raise ValueError('candidate_refresh_context_invalid')
    from core.auto_trainer.capture_adapter import load_capture_group_raws
    root = Path(args.prior_execution_root).resolve()
    if root != Path(args.output_root).resolve():
        raise ValueError('candidate_refresh_root_mismatch')
    manifest = json.loads(_io(Path(args.prior_manifest)).read_text(encoding='utf-8-sig'))
    group = next((g for g in manifest.get('groups', [])
                  if g.get('actual_group_id') == args.actual_group_id), {})
    if (group.get('run_session_id') != args.run_session_id
            or group.get('turn_index') != args.turn_index
            or group.get('source_state_key') != args.source_state_key
            or state_context.get('turn') != args.turn_index):
        raise ValueError('candidate_refresh_run_or_turn_mismatch')
    load_capture_group_raws(manifest_path=args.prior_manifest, execution_root=root,
                           actual_group_id=args.actual_group_id)
    result = {}
    for tab in TRAINING_TYPES:
        item = group.get('provenance', {}).get('source_images_by_tab', {}).get(tab, {})
        path = Path(item.get('path', '')).resolve()
        if not path.is_relative_to(root) or not _io(path).is_file():
            raise ValueError(f'candidate_refresh_source_outside_group:{tab}')
        if hashlib.sha256(_io(path).read_bytes()).hexdigest() != item.get('sha256'):
            raise ValueError(f'candidate_refresh_source_changed:{tab}')
        if tab in tabs:
            retained = root / 'candidate_refresh_sources' / f'{tab}.png'
            _io(retained.parent).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_io(path), _io(retained))
            result[tab] = retained
        else:
            result[tab] = path
    return result, group


def _transition_source(path: Path) -> str:
    data = _io(path).read_bytes()
    if not data:
        raise RuntimeError(f"transition_source_empty:{path}")
    return hashlib.sha256(data).hexdigest()


def _transition_preflight(
    session: RouteTransitionSession,
    *,
    source_path: Path,
    observed_state: str,
    action_id: str,
    coordinate_name: str,
    events: list[dict[str, object]],
) -> str:
    source_sha = _transition_source(source_path)
    session.observe(observed_state, source_sha256=source_sha)
    session.commit_action(
        action_id,
        expected_next_states=("training_menu",),
        source_sha256=source_sha,
    )
    events.append({
        "phase": "preflight",
        "observed_state": observed_state,
        "action_id": action_id,
        "coordinate_name": coordinate_name,
        "source_path": str(source_path.resolve()),
        "source_sha256": source_sha,
        "expected_next_states": ["training_menu"],
    })
    return source_sha


def _transition_postcheck(
    session: RouteTransitionSession,
    *,
    session_path: Path,
    source_path: Path,
    action_id: str,
    coordinate_name: str,
    source_sha: str,
    events: list[dict[str, object]],
) -> None:
    post_sha = _transition_source(source_path)
    session.observe("training_menu", source_sha256=post_sha)
    events.append({
        "phase": "postcheck",
        "observed_state": "training_menu",
        "action_id": action_id,
        "coordinate_name": coordinate_name,
        "source_path": str(source_path.resolve()),
        "source_sha256": post_sha,
        "preflight_source_sha256": source_sha,
        "tap_succeeded": True,
    })
    session.save(session_path)


def _transition_persist_pending(
    session: RouteTransitionSession,
    *,
    session_path: Path,
    action_id: str,
    coordinate_name: str,
    events: list[dict[str, object]],
) -> None:
    event = {
        "phase": "tap_succeeded_persisting",
        "action_id": action_id,
        "coordinate_name": coordinate_name,
        "tap_succeeded": True,
        "pending_expected_next_states": ["training_menu"],
    }
    events.append(event)
    session.save(session_path)
    event["phase"] = "tap_succeeded_pending_persisted"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    indent = 2 if args.pretty else None
    transition_events: list[dict[str, object]] = []
    transition_session: RouteTransitionSession | None = None
    transition_session_path = (
        Path(args.transition_session_json)
        if args.transition_session_json
        else None
    )
    if not (args.live and args.execute):
        print(json.dumps(_dry_run_plan(args), ensure_ascii=False, indent=indent))
        return 0
    if args.confirm_token != CONFIRM_TOKEN:
        print(
            json.dumps(
                {
                    "is_valid": False,
                    "error": "confirm_token_mismatch",
                    "required_confirm_token": CONFIRM_TOKEN,
                },
                ensure_ascii=False,
                indent=indent,
            )
        )
        return 2

    frame_stream = None
    capture_timings=[]
    failure_region_warning_tabs = set()
    capture_transport_fallback = None
    page_handoff = None
    entry_precheck = None
    try:
        from core.adb import ADB
        from core.native_capture_adb import native_instance_id

        state_context = json.loads(
            Path(args.state_context_json).read_text(encoding="utf-8-sig")
        )
        if not isinstance(state_context, dict):
            raise ValueError("state_context_root_not_object")
        scenario = state_context.get("scenario_state", {}).get("scenario", "grand_masters")
        if scenario not in {"grand_masters", "larc"}:
            raise ValueError(f"unsupported_training_layout:{scenario}")
        requested_transport = args.capture_transport
        if requested_transport == "auto":
            from core.cut_portable_runtime import generic_adb_capture
            args.capture_transport = (
                "mumu-ipc" if scenario == "larc" and native_instance_id(args.adb_port) is not None
                else "adb-png" if scenario=='larc' and generic_adb_capture()
                else "direct-png"
            )
        if args.capture_transport in {"raw-stream", "mumu-ipc", "adb-png"} and scenario != "larc":
            raise ValueError(f"{args.capture_transport}_requires_larc")
        if args.capture_transport == "mumu-ipc" and (
                args.adb_port not in {16384, 16416, 16448} or native_instance_id(args.adb_port) is None):
            raise ValueError("mumu_ipc_requires_mapped_device")
        coordinate_section = "larc_training_menu" if scenario == "larc" else "training_menu"
        gain_stability_roi = (32, 1500, 1396, 1660) if scenario == "larc" else None
        gain_reader = None
        participant_reader = None
        if transition_session_path is not None:
            transition_session = RouteTransitionSession.load(
                transition_session_path,
                expected_run_session_id=args.run_session_id,
            )
        adb = ADB(port=args.adb_port) if args.adb_port is not None else ADB()
        if not adb.connect():
            raise RuntimeError("adb_connect_failed")
        screen_size = None
        if args.capture_transport == "mumu-ipc":
            from config.settings import ADB_PATH
            from core.nemu_ipc_capture import NemuIpcCapture

            mumu_root = Path(ADB_PATH).resolve().parents[3]
            try:
                frame_stream = NemuIpcCapture(mumu_root, instance_id=native_instance_id(args.adb_port)).start()
                initial_native_frame = frame_stream.read_image()
                screen_size = (int(initial_native_frame.shape[1]), int(initial_native_frame.shape[0]))
            except Exception as exc:
                if frame_stream is not None:
                    frame_stream.close()
                    frame_stream = None
                if requested_transport != "auto":
                    raise
                capture_transport_fallback = f"{type(exc).__name__}:{exc}"
                args.capture_transport = "direct-png"

        if args.capture_transport != "mumu-ipc":
            screen_size = tuple(adb.get_screen_size())
        if screen_size != (720, 1280):
            raise RuntimeError(f"unsupported_screen_size:{screen_size}")
        if args.capture_transport=='adb-png':frame_stream=_AdbPngFrames(adb)

        output_root = Path(args.output_root)
        has_published_capture = _io(output_root / "execution_manifest.json").is_file()
        source_images, prior_group = _refresh_sources(args, state_context)
        # A refresh retains the previous formal pages until capture succeeds;
        # ordinary native capture writes the one canonical group directly.
        raw_dir = (output_root / "rf" / uuid.uuid4().hex[:10] if args.refresh_tabs or has_published_capture else
                   output_root / "incoming_raw" / args.actual_group_id
                   if _normalized_capture_size(scenario, args.capture_transport) == (720, 1280)
                   else output_root / "raw_720")
        _io(raw_dir).mkdir(parents=True, exist_ok=True)
        capture_work_root = raw_dir if has_published_capture else output_root
        coords = CoordinateConfig()
        if args.start_screen == "main-menu":
            before_path = capture_work_root / "main_menu_before.png"
            if transition_session is not None or scenario == "larc":
                before_image = (
                    frame_stream.screenshot(str(before_path))
                    if args.capture_transport in {"mumu-ipc","adb-png"}
                    else adb.screenshot(str(before_path))
                )
                if before_image is None:
                    raise RuntimeError("main_menu_entry_screenshot_failed")
                if scenario == "larc":
                    from core.auto_trainer.transition_wait import entry_page_handoff
                    page_handoff = entry_page_handoff(before_image, before_path)
                    if page_handoff is not None:
                        entry_precheck = {"accepted": False, "reason": "entry_page_handoff",
                                          "surface": page_handoff["surface"]}
                        raise RuntimeError("main_menu_entry_precheck_rejected:entry_page_handoff:"
                                           + page_handoff["surface"])
                entry_precheck = _main_menu_entry_check(_io(before_path).read_bytes(), before_image, scenario=scenario)
                if not entry_precheck["accepted"]:
                    raise RuntimeError("main_menu_entry_precheck_rejected:" + entry_precheck["reason"])
            point = coords.point("main_menu", "training")
            if transition_session is not None:
                entry_source_sha = _transition_preflight(
                    transition_session,
                    source_path=before_path,
                    observed_state="main_menu",
                    action_id="main_menu.training",
                    coordinate_name="main_menu.training",
                    events=transition_events,
                )
            try:
                direct_tap = getattr(type(adb), "tap_no_wait", None)
                tap_result = (
                    direct_tap(adb, point[0], point[1], scaled=True)
                    if args.capture_transport in {"mumu-ipc","adb-png"} and direct_tap is not None
                    else adb.tap(point[0], point[1], scaled=True)
                )
            except Exception as exc:
                if transition_session is not None:
                    transition_events.append({
                        "phase": "tap_error",
                        "action_id": "main_menu.training",
                        "coordinate_name": "main_menu.training",
                        "error": f"{type(exc).__name__}: {exc}",
                        "tap_succeeded": False,
                    })
                raise
            if transition_session is not None and tap_result is not True:
                transition_events.append({
                    "phase": "tap_failed",
                    "action_id": "main_menu.training",
                    "coordinate_name": "main_menu.training",
                    "tap_return": tap_result,
                    "tap_succeeded": False,
                })
                raise RuntimeError(
                    f"transition_tap_failed:main_menu.training:{tap_result!r}"
                )
            if transition_session is not None:
                _transition_persist_pending(
                    transition_session,
                    session_path=transition_session_path,
                    action_id="main_menu.training",
                    coordinate_name="main_menu.training",
                    events=transition_events,
                )

        if args.capture_transport == "raw-stream":
            frame_stream = adb.open_raw_frame_stream()
        capture_image = frame_stream.screenshot if frame_stream is not None else None

        precheck_path = capture_work_root / "precheck" / "training_menu.png"
        _io(precheck_path.parent).mkdir(parents=True, exist_ok=True)
        verifier = DefaultTrainingExecutionVerifier(scenario=scenario)
        native_verifier = (
            TrainingMenuPrechecker(scenario="larc")
            if args.capture_transport in {"mumu-ipc","adb-png"} else None
        )
        def observation_check(result):
            return _observation_precheck_result(result, scenario=scenario)

        def record_observation_warning(result):
            if getattr(result, 'evidence', {}).get('observation_warning') == 'failure_rate_region':
                failure_region_warning_tabs.add(result.selected_tab)

        try:
            if args.capture_transport in {"mumu-ipc","adb-png"}:
                precheck_path, precheck = _wait_for_native_capture(
                    frame_stream=frame_stream, final_path=precheck_path,
                    verify=lambda path: observation_check(native_verifier.check(path)),
                    allowed_selected=set(TRAINING_TYPES) | {"ss"},
                    gain_reader=gain_reader, participant_reader=participant_reader,
                    reject_conflicting_accepted_selected=True,
                )
            else:
                gain_reader, participant_reader = _stable_capture_readers(scenario, gain_reader, participant_reader)
                precheck_path, precheck = _wait_for_stable_capture(
                    adb=adb,
                    final_path=precheck_path,
                    verify=lambda path: observation_check(verifier.precheck(path)),
                    sleep_seconds=args.settle_seconds,
                    allowed_selected=set(TRAINING_TYPES) | ({"ss"} if scenario == "larc" else set()),
                    reject_conflicting_accepted_selected=True,
                    tolerate_conflicting_trailing_selected=(
                        None if scenario == "larc" else set(TRAINING_TYPES)
                    ),
                    max_attempts=FORMAL_STABLE_CAPTURE_MAX_ATTEMPTS,
                    gain_stability_roi=gain_stability_roi,
                        gain_reader=gain_reader,
                        participant_reader=participant_reader,
                        capture_image=capture_image,
                    )
        except RuntimeError as exc:
            if str(exc).startswith(
                (
                    "precheck_unstable_selected_tab:",
                    "precheck_conflicting_selected_tab:",
                    "precheck_gain_display_unstable:",
                    "precheck_charge_preview_not_ready:",
                )
            ):
                raise RuntimeError(str(exc)) from exc
            raise RuntimeError(f"precheck_rejected:{exc}") from exc
        if not getattr(precheck, "accepted", False):
            raise RuntimeError(f"precheck_rejected:{getattr(precheck, 'reason', 'unknown')}")
        record_observation_warning(precheck)
        timing=getattr(precheck,'evidence',{}).get('capture_timing')
        if isinstance(timing,dict):capture_timings.append(timing)
        selected_tab = getattr(precheck, "selected_tab", None)
        if selected_tab not in TRAINING_TYPES and not (scenario == "larc" and selected_tab == "ss"):
            raise RuntimeError(f"precheck_selected_tab_unknown:{selected_tab}")

        if transition_session is not None:
            if args.start_screen == "main-menu":
                _transition_postcheck(
                    transition_session,
                    session_path=transition_session_path,
                    source_path=precheck_path,
                    action_id="main_menu.training",
                    coordinate_name="main_menu.training",
                    source_sha=entry_source_sha,
                    events=transition_events,
                )
            else:
                source_sha = _transition_source(precheck_path)
                transition_session.observe("training_menu", source_sha256=source_sha)
                transition_session.save(transition_session_path)
                transition_events.append({
                    "phase": "observe",
                    "observed_state": "training_menu",
                    "action_id": None,
                    "coordinate_name": None,
                    "source_path": str(precheck_path.resolve()),
                    "source_sha256": source_sha,
                    "tap_succeeded": False,
                })

        refresh_tabs = set(args.refresh_tabs or [])
        refreshed = set()
        current_tab = str(selected_tab)
        current_path = precheck_path
        if current_tab in refresh_tabs:
            # A second tap on the selected training could consume it. Wait and
            # capture the same selected page without sending any input.
            gain_reader, participant_reader = _stable_capture_readers(scenario, gain_reader, participant_reader)
            current_path, current_check = _wait_for_stable_capture(
                adb=adb, final_path=raw_dir / f'{current_tab}.png',
                verify=lambda path: observation_check(verifier.verify_target_tab(current_tab, path)),
                sleep_seconds=max(.2, args.settle_seconds), allowed_selected={current_tab},
                max_attempts=FORMAL_STABLE_CAPTURE_MAX_ATTEMPTS,
                gain_stability_roi=gain_stability_roi, gain_reader=gain_reader,
                participant_reader=participant_reader, capture_image=capture_image)
            if not current_check.accepted:
                raise RuntimeError('candidate_refresh_current_tab_not_stable')
            record_observation_warning(current_check)
            source_images[current_tab] = current_path
            refreshed.add(current_tab)
        elif current_tab in TRAINING_TYPES and not refresh_tabs:
            source_images[current_tab] = precheck_path
            _publish_initial_precheck_attempts(
                precheck_path=precheck_path,
                raw_dir=raw_dir,
                selected_tab=current_tab,
            )
        direct_tap = getattr(type(adb), "tap_no_wait", None)
        for tab in TRAINING_TYPES:
            if refresh_tabs and (tab not in refresh_tabs or tab in refreshed):
                continue
            if tab not in source_images or tab in refresh_tabs:
                point = coords.point(coordinate_section, tab)
                action_id = f"capture.training_tab.{tab}"
                coordinate_name = f"{coordinate_section}.{tab}"
                source_sha = None
                if transition_session is not None:
                    source_sha = _transition_preflight(
                        transition_session,
                        source_path=current_path,
                        observed_state="training_menu",
                        action_id=action_id,
                        coordinate_name=coordinate_name,
                        events=transition_events,
                    )
                try:
                    tap_result = (direct_tap(adb, point[0], point[1], scaled=True)
                                  if direct_tap is not None else adb.tap(point[0], point[1], scaled=True))
                except Exception as exc:
                    if transition_session is not None:
                        transition_events.append({
                            "phase": "tap_error",
                            "action_id": action_id,
                            "coordinate_name": coordinate_name,
                            "error": f"{type(exc).__name__}: {exc}",
                            "tap_succeeded": False,
                        })
                    raise
                if transition_session is not None and tap_result is not True:
                    transition_events.append({
                        "phase": "tap_failed",
                        "action_id": action_id,
                        "coordinate_name": coordinate_name,
                        "tap_return": tap_result,
                        "tap_succeeded": False,
                    })
                    raise RuntimeError(
                        f"transition_tap_failed:{coordinate_name}:{tap_result!r}"
                    )
                if transition_session is not None:
                    _transition_persist_pending(
                        transition_session,
                        session_path=transition_session_path,
                        action_id=action_id,
                        coordinate_name=coordinate_name,
                        events=transition_events,
                    )
                path = raw_dir / f"{tab}.png"
                try:
                    if args.capture_transport in {"mumu-ipc","adb-png"} and not refresh_tabs:
                        path, tab_check = _wait_for_native_capture(
                            frame_stream=frame_stream, final_path=path,
                            verify=None,
                            allowed_selected={tab}, gain_reader=gain_reader,
                            participant_reader=participant_reader,
                        )
                    else:
                        gain_reader, participant_reader = _stable_capture_readers(scenario, gain_reader, participant_reader)
                        path, tab_check = _wait_for_stable_capture(
                            adb=adb,
                            final_path=path,
                            verify=lambda image_path, target=tab: observation_check(verifier.verify_target_tab(target, image_path)),
                            sleep_seconds=args.settle_seconds,
                            allowed_selected={tab},
                            max_attempts=FORMAL_STABLE_CAPTURE_MAX_ATTEMPTS,
                            gain_stability_roi=gain_stability_roi,
                            gain_reader=gain_reader,
                            participant_reader=participant_reader,
                            capture_image=capture_image,
                        )
                except RuntimeError as exc:
                    raise RuntimeError(f"tab_verification_rejected:{tab}:{exc}") from exc
                if not getattr(tab_check, "accepted", False):
                    raise RuntimeError(
                        "tab_verification_rejected:"
                        f"{tab}:{getattr(tab_check, 'reason', 'unknown')}"
                    )
                record_observation_warning(tab_check)
                timing=getattr(tab_check,'evidence',{}).get('capture_timing')
                if isinstance(timing,dict):capture_timings.append(timing)
                if transition_session is not None:
                    _transition_postcheck(
                        transition_session,
                        session_path=transition_session_path,
                        source_path=path,
                        action_id=action_id,
                        coordinate_name=coordinate_name,
                        source_sha=source_sha,
                        events=transition_events,
                    )
                source_images[tab] = path
                refreshed.add(tab)
                current_tab = tab
                current_path = path

        capture_backend_details = ({'backend': args.capture_transport}
            if args.capture_transport in {'mumu-ipc', 'raw-stream'} else
            getattr(adb, 'capture_diagnostics', lambda: {'backend': args.capture_transport})())
        if frame_stream is not None:
            frame_stream.close()
            frame_stream = None
        report = build_live_capture_package(
            output_root=output_root,
            actual_group_id=args.actual_group_id,
            run_session_id=args.run_session_id,
            turn_index=args.turn_index,
            source_state_key=args.source_state_key,
            operator_id=args.operator_id,
            source_images=source_images,
            state_context=state_context,
            source_resolution=(720, 1280),
            normalized_resolution=_normalized_capture_size(scenario,args.capture_transport),
            prior_group=prior_group,
            refreshed_tabs=refreshed,
        )
        if capture_transport_fallback is not None:
            report["capture_transport_fallback"] = capture_transport_fallback
        report['capture_transport']=args.capture_transport
        report['capture_backend_details']=capture_backend_details
        report['capture_timings']=capture_timings
        if failure_region_warning_tabs:
            report['observation_warnings'] = {
                'failure_rate_region': sorted(failure_region_warning_tabs),
            }
        if entry_precheck is not None:
            report["main_menu_entry_precheck"] = entry_precheck
        if transition_session is not None:
            report["transition_session_path"] = str(transition_session_path.resolve())
            report["transition_events"] = transition_events
        print(json.dumps(report, ensure_ascii=False, indent=indent))
        return 0
    except Exception as exc:
        output_root = Path(args.output_root)
        print(
            json.dumps(
                {
                    "is_valid": False,
                    "errors": [f"{type(exc).__name__}: {exc}"],
                    **({"main_menu_entry_precheck": entry_precheck} if entry_precheck is not None else {}),
                    **({"page_handoff": page_handoff} if page_handoff is not None else {}),
                    **(
                        {
                            "transition_session_path": str(transition_session_path.resolve()),
                            "transition_events": transition_events,
                        }
                        if transition_session_path is not None
                        else {}
                    ),
                    "diagnostics": {
                        "output_root": str(output_root),
                        "screenshots": _diagnostic_screenshots(output_root),
                    },
                },
                ensure_ascii=False,
                indent=indent,
            )
        )
        return 2
    finally:
        if frame_stream is not None:
            frame_stream.close()


def _normalized_capture_size(scenario,transport):
    from core.cut_portable_runtime import generic_adb_capture
    # The L'Arc readers already consume native 720p on the MuMu fast path.
    # Reuse it in the standalone ADB path instead of enlarging then shrinking.
    return (720,1280) if transport in {'mumu-ipc','adb-png'} or scenario=='larc' and generic_adb_capture() else (1440,2560)


class _AdbPngFrames:
    """Feed the selected portable capture backend into page handoffs."""
    accepts_deadline=True
    transport='adb-png'
    def __init__(self,adb):self.adb=adb
    def read_image(self,*,deadline=None):return self.adb.read_frame(deadline=deadline)
    def screenshot(self,path):return self.adb.screenshot(path)
    @staticmethod
    def save_image(path,image):
        import cv2
        destination=Path(path);_io(destination.parent).mkdir(parents=True,exist_ok=True)
        ok,payload=cv2.imencode('.png',image)
        if not ok:raise RuntimeError('adb_png_encode_failed')
        _io(destination).write_bytes(payload.tobytes())
    def close(self):
        close=getattr(self.adb,'close',None)
        if close is not None:close()


def _dry_run_plan(args: argparse.Namespace) -> dict[str, object]:
    return {
        "dry_run": True,
        "will_tap": False,
        "tap_scaled": True,
        "required_live_gate": {
            "live": True,
            "execute": True,
            "confirm_token": CONFIRM_TOKEN,
        },
        "expected_screen_size": [720, 1280],
        "start_screen": args.start_screen,
        "capture_transport": args.capture_transport,
        "tab_order": list(TRAINING_TYPES),
        "output_root": str(Path(args.output_root)),
        "transition_session_json": args.transition_session_json,
        "actual_group_id": args.actual_group_id,
        "run_session_id": args.run_session_id,
        "turn_index": args.turn_index,
    }


def _diagnostic_screenshots(output_root: Path) -> list[str]:
    from core.auto_trainer.pacer_storage import _io, _rglob
    if not _io(output_root).exists():
        return []
    return [
        str(path)
        for path in sorted(_rglob(output_root, "*.png"))
        if _io(path).is_file()
    ]


if __name__ == "__main__":
    raise SystemExit(main())
