from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from core.frame_io import read_image_with_retry

from core.auto_trainer.event_options import (
    detect_event_option_layout,
    is_carrot_event_narrative,
)
from core.auto_trainer.dashboard_runner import CooperativeRunnerDriver, FreshFrameEvidence
from core.auto_trainer.frame_safety import FormalActionSafetyGate, FrameSafetyAnalyzer
from core.auto_trainer.larc_pages import is_larc_race_day
from core.auto_trainer.runtime_control import reobserving_step
from core.auto_trainer.safe_advance import SafeAdvancePlanner, SafeAdvanceExecutor
from core.auto_trainer.route_handoff import handoff
from core.auto_trainer.safe_advance_transition import event_choice_option_index
from core.perception import Perception
from core.screen_text import analyze_screen_text, fuse_perception_with_text
from core.training_menu_precheck import TrainingMenuPrechecker


EVENT_CHOICE_CONFIRM_TOKEN = "UG_PARENT_EVENT_CHOICE_SANDBOX"
SUPPORTED_EVENT_CHOICE_SCREEN_SIZES = {(720, 1280), (1440, 2560)}


@dataclass(frozen=True)
class EventChoicePolicy:
    allow_choose_event: bool = False
    choice: str = "top"
    visible_text: str | None = None
    expected_event: tuple[str, ...] | None = None
    option_index: int | None = None


class EventChoicePlanner:
    """Plan one guarded click on a recognized event page."""

    def plan(
        self,
        image_path: str | Path,
        *,
        policy: EventChoicePolicy | None = None,
    ) -> dict[str, Any]:
        policy = policy or EventChoicePolicy()
        path = Path(image_path)
        img = read_image_with_retry(path)
        if img is None:
            return self._stop("image_read", image_path=str(path))

        perception = Perception().analyze_image(img)
        evidence: dict[str, Any] = {"image": str(path), "perception": perception}
        screen_text = (
            analyze_screen_text(policy.visible_text)
            if policy.visible_text is not None
            else None
        )
        if screen_text is not None:
            perception = fuse_perception_with_text(perception, screen_text)
            evidence["perception"] = perception
            evidence["screen_text"] = screen_text
            layout = self._event_option_layout(img, policy.visible_text)
            option_points = layout["points"]
            if screen_text.get("dangerous") and not (
                option_points
                and is_carrot_event_narrative(
                    str(screen_text.get("visible_text", "")),
                    list(screen_text.get("resource_terms", ())),
                )
            ):
                return self._stop("resource_prompt", evidence=evidence)
        else:
            layout = self._event_option_layout(img)
            option_points = layout["points"]

        if SafeAdvanceExecutor._career_home_controls_ready(img):
            # A fresh handoff can land on home after the event disappeared.
            # Do not click white scenery/quip boxes as if they were choices.
            return self._stop("event_choice:no_option_points", evidence=evidence,route_state='main_menu')

        if policy.option_index is not None and (
                type(policy.option_index) is not int or policy.expected_event is None
                or not 1 <= policy.option_index < len(policy.expected_event)):
            return self._stop("event_choice:index_requires_binding", evidence=evidence)
        choice = policy.choice
        selected_index = event_choice_option_index(choice, layout["option_count"])
        if choice not in {"top", "bottom"} and policy.expected_event is None:
            return self._stop("event_choice:index_requires_binding", evidence=evidence)
        if policy.expected_event is not None:
            requested = event_choice_option_index(choice, len(policy.expected_event) - 1)
            if requested is None or policy.option_index is not None and requested != policy.option_index:
                return self._stop("event_choice:options_mismatch", evidence=evidence)
            selected, binding = self._bind_expected_option(
                img, layout["ordered_points"], (screen_text or {}).get("normalized_text", ""),
                policy.expected_event, requested, explicit_index=policy.option_index is not None,
                allow_decline=choice == "bottom" and policy.option_index is not None,
                relative_choice=choice if policy.option_index is None and choice in {"top", "bottom"} else None)
            if selected is None:
                return self._stop(binding, evidence=evidence)
            if selected != selected_index:
                # Transition/receipt verification rechecks the physical slot.
                # Keep the policy and selector's original logical choice.
                choice = f"option_{selected}"
            selected_index = selected
            evidence["event_binding"] = binding
            evidence["event_option_binding"] = {"requested_index": requested, "observed_index": selected}

        if selected_index is not None:
            option_points = {**option_points, choice: layout["ordered_points"][selected_index - 1]}

        if SafeAdvancePlanner._connection_conflict_return_title_button(img) is not None:
            return self._stop("connection_conflict", evidence=evidence)

        if is_larc_race_day(img, evidence):
            return handoff("race_day_main_menu", evidence=evidence, route_state="main_menu")

        training_check = TrainingMenuPrechecker().check(path)
        evidence["training_menu_precheck"] = {
            "accepted": training_check.accepted,
            "reason": training_check.reason,
            "selected_tab": training_check.selected_tab,
            "failures": list(training_check.failures),
        }
        if training_check.accepted:
            return handoff("ready_training_menu", evidence=evidence)

        if option_points:
            if not policy.allow_choose_event:
                return self._stop("event_choice:choose_not_allowed", evidence=evidence)
            if choice not in option_points:
                return self._stop(f"event_choice:invalid_choice:{choice}", evidence=evidence)
            return {
                "action": "tap",
                "reason": f"choose_event_{choice}",
                "route_state": "event_choice",
                "tap": {
                    "coordinate_name": f"event_choice.{choice}",
                    "x": int(option_points[choice][0]),
                    "y": int(option_points[choice][1]),
                    "scaled": False,
                },
                "evidence": evidence,
            }

        if SafeAdvancePlanner._looks_like_main_training_menu(img):
            return handoff("ready_main_menu", evidence=evidence)

        # Goal-summary pages use a wide green continue button near the bottom,
        # rather than the dialogue advance button in the middle-right.  Keep
        # this detector narrow and only reach it after training-menu guards and
        # event-option detection, so it cannot become a generic screen tap.
        summary_next = self._event_summary_continue_point(img)
        if summary_next is not None:
            if not policy.allow_choose_event:
                return self._stop("event_choice:choose_not_allowed", evidence=evidence)
            return {
                "action": "tap",
                "reason": "advance_event_dialogue",
                "route_state": "event_choice",
                "tap": {
                    "coordinate_name": "event_choice.dialogue_next",
                    "x": summary_next[0],
                    "y": summary_next[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }

        if perception.get("screen") == "training_menu":
            return handoff("ready_training_menu", evidence=evidence)

        if perception.get("screen") == "event_choice":
            if not policy.allow_choose_event:
                return self._stop("event_choice:choose_not_allowed", evidence=evidence)
            dialogue_next = self._event_dialogue_advance_point(img)
            if dialogue_next is not None:
                return {
                    "action": "tap",
                    "reason": "advance_event_dialogue",
                    "route_state": "event_choice",
                    "tap": {
                        "coordinate_name": "event_choice.dialogue_next",
                        "x": dialogue_next[0],
                        "y": dialogue_next[1],
                        "scaled": False,
                    },
                    "evidence": evidence,
                }
            return self._stop("event_choice:no_option_points", evidence=evidence)
        return self._stop(
            f"unsupported_screen:{perception.get('screen', 'unknown')}",
            evidence=evidence,
        )

    @staticmethod
    def _bind_expected_option(img, points, visible_text, expected, requested, *, explicit_index, allow_decline,
                              relative_choice=None):
        from core.auto_trainer.larc_scenario_events import (
            acupuncture_decline_binding, acupuncture_followup_options, acupuncture_title,
        )
        from core.event_knowledge_lookup import normalize_text

        def compact(text):
            return "".join(normalize_text(text).split()).rstrip("!?。．.～~…")

        if any(not isinstance(text, str) or not text for text in expected):
            return None, "event_choice:identity_mismatch"
        title, *options = (compact(text) for text in expected)
        lines = [compact(line) for line in visible_text.splitlines()]
        exact_title = title in lines and bool(title)
        if not exact_title and not any(
                len(line) >= 5 and len(title) - len(line) in (1, 2) and title.startswith(line) for line in lines):
            return None, "event_choice:identity_mismatch"
        if not points:
            return None, "event_choice:options_unreadable"
        if abs(len(points) - len(options)) > 1 or options.count(options[requested - 1]) != 1:
            return None, "event_choice:options_mismatch"

        if options == [f"option:{i}" for i in range(1, len(options) + 1)]:
            if exact_title:
                if relative_choice is not None and not explicit_index:
                    # This policy authorizes the rendered extreme itself.
                    # A selector's fixed ordinal is a different contract.
                    return event_choice_option_index(relative_choice, len(points)), "event_title_and_relative_option"
                if explicit_index:
                    if len(points) == len(options):
                        return requested, "event_title_and_option_count"
                    return None, "event_choice:options_unreadable"
            return None, "event_choice:options_mismatch"

        acupuncture = bool(acupuncture_title(expected[:1]))
        if acupuncture and (not exact_title or len(points) != len(options)):
            return None, "event_choice:options_mismatch"
        wanted = options[requested - 1]
        if allow_decline and wanted not in lines and acupuncture_decline_binding(expected, requested, len(points)):
            return requested, "acupuncture_title_and_decline_layout"

        followup = acupuncture and len(options) == 2
        if exact_title and len(points) == len(options):
            # Full-frame OCR is emitted top-to-bottom. Keep the option block
            # contiguous and remove redundant headings, never duplicate rows.
            following = [line for line in lines[lines.index(title) + 1:] if line and line != title]
            if all(following.count(option) == 1 for option in options):
                start = following.index(options[0])
                if (following[start:start + len(options)] == options
                        and (not followup or acupuncture_followup_options(lines) == tuple(options))):
                    return requested, "current_ordered_event_text"

        observed, title_confirmed = EventChoicePlanner._option_labels_at_points(
            img, points, set(options), compact, expected_title=None if exact_title else title)
        if not title_confirmed:
            return None, "event_choice:identity_mismatch"
        matches = [i for i, labels in enumerate(observed, 1) if wanted in labels]
        if len(matches) != 1 or len(observed[matches[0] - 1]) != 1:
            return None, "event_choice:options_unreadable"

        # Unknown rows may be the single added/missed option. Known rows may
        # swap once with their immediate neighbor, never an arbitrary reorder.
        known = [options.index(next(iter(labels))) for labels in observed if len(labels) == 1]
        if not exact_title and len(set(known)) < min(2, len(options)):
            return None, "event_choice:identity_mismatch"
        inversions = [(a, b) for i, a in enumerate(known) for b in known[i+1:] if a > b]
        if (len(inversions) > 1 or any(a != b + 1 for a, b in inversions)
                or acupuncture and known != list(range(len(options)))):
            return None, "event_choice:options_mismatch"
        return matches[0], "current_rendered_option_text"

    @staticmethod
    def _option_labels_at_points(img, points, options, compact, *, expected_title):
        from core.auto_trainer.frame_safety import (
            LocalRapidOCRScreenTextReader, RapidOCRTextReaderError,
            _RAPIDOCR_MIN_CONFIDENCE, _rapidocr_visual_position,
        )

        height, width = img.shape[:2]
        # The geometry is from this frame; OCR boxes outside a row cannot
        # identify that control. A single crop avoids one OCR run per option.
        margin = round(height * .028)
        first_row_top = max(0, points[0][1] - margin)
        top = min(first_row_top, round(height * .14)) if expected_title else first_row_top
        bottom = min(height, points[-1][1] + margin + 1)
        try:
            regions = LocalRapidOCRScreenTextReader().read_regions(img[top:bottom])
        except RapidOCRTextReaderError:
            return [], expected_title is None
        labels = [set() for _ in points]
        title_confirmed = expected_title is None
        for region in regions:
            try:
                box, text, score = region
                score = float(score)
            except (TypeError, ValueError):
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            if analyze_screen_text(text).get("dangerous"):
                return [], expected_title is None
            position = _rapidocr_visual_position(box)
            if position is None or not _RAPIDOCR_MIN_CONFIDENCE <= score <= 1:
                continue
            y, x = position
            if not width * .10 <= x <= width * .90:
                continue
            label = compact(text)
            if expected_title and y + top < first_row_top and label == expected_title:
                title_confirmed = True
            if label in options:
                at = [i for i, (_, point_y) in enumerate(points) if abs(y + top - point_y) <= margin]
                if len(at) == 1:
                    labels[at[0]].add(label)
        return labels, title_confirmed

    @staticmethod
    def _event_option_layout(img: np.ndarray, visible_text=None) -> dict[str, Any]:
        layout = detect_event_option_layout(img, visible_text=visible_text)
        if layout["points"]:
            return layout
        single_option = SafeAdvancePlanner._single_green_event_option_button(img)
        if single_option is None:
            # A lone white contour is not a recognized selectable option.
            # Only count rows whose control geometry is actually available.
            return {**layout, "option_count": 0, "ordered_points": []}
        return {"points": {"top": single_option}, "ordered_points": [single_option], "option_count": 1}

    @staticmethod
    def _event_option_points(img: np.ndarray, visible_text=None) -> dict[str, tuple[int, int]]:
        return EventChoicePlanner._event_option_layout(img, visible_text)["points"]

    @staticmethod
    def _event_dialogue_advance_point(img: np.ndarray) -> tuple[int, int] | None:
        # This is ordinary dialogue, not an option-row detector. Reuse the
        # existing visible dialogue controls instead of guessing from fill hue.
        if (not SafeAdvancePlanner._looks_like_confirm_modal_panel(img)
                and SafeAdvancePlanner._looks_like_large_bottom_dialogue(img)
                and SafeAdvancePlanner._looks_like_dialogue_continue_arrow(img)):
            tap=SafeAdvancePlanner._advance_bottom_dialogue({},img).get('tap')
            if tap:return int(tap['x']),int(tap['y'])
        return None

    @staticmethod
    def _event_summary_continue_point(img: np.ndarray) -> tuple[int, int] | None:
        """Find the wide lower green continue control on a goal summary page."""
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            np.array([35, 80, 100], dtype=np.uint8),
            np.array([95, 255, 255], dtype=np.uint8),
        )
        y0, y1 = int(height * 0.78), int(height * 0.97)
        x0, x1 = int(width * 0.18), int(width * 0.82)
        roi = mask[y0:y1, x0:x1]
        if roi.size == 0:
            return None
        kernel = np.ones((5, 5), dtype=np.uint8)
        roi = cv2.morphologyEx(roi, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_area = int(width * height * 0.006)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True):
            area = cv2.contourArea(contour)
            if area < min_area:
                break
            x, y, w, h = cv2.boundingRect(contour)
            if w < int(width * 0.35) or h < int(height * 0.06):
                continue
            cx = x0 + x + w // 2
            cy = y0 + y + h // 2
            if int(width * 0.25) <= cx <= int(width * 0.75) and cy >= int(height * 0.80):
                return cx, cy
        return None

    @staticmethod
    def _stop(
        reason: str,
        *,
        evidence: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        data = {"action": "stop", "reason": reason, "evidence": evidence or {}}
        data.update(extra)
        return data


class EventChoiceExecutor:
    def __init__(
        self,
        *,
        adb_factory: Callable[[], Any] | None = None,
        planner: EventChoicePlanner | None = None,
        safety_analyzer: FrameSafetyAnalyzer | None = None,
        safety_gate: FormalActionSafetyGate | None = None,
    ) -> None:
        self.adb_factory = adb_factory or self._default_adb_factory
        self.planner = planner or EventChoicePlanner()
        self.safety_analyzer = safety_analyzer or FrameSafetyAnalyzer()
        self.safety_gate = safety_gate or FormalActionSafetyGate()

    @reobserving_step
    def run_once(
        self,
        *,
        mode: str,
        execute: bool,
        confirm_token: str | None,
        policy: EventChoicePolicy | None = None,
        screenshot_path: str | Path | None = None,
        control_driver: CooperativeRunnerDriver | None = None,
        transition_preflight: Callable[[dict[str, Any]], None] | None = None,
        choice_selector=None,
        prior_observation=None,
        initial_frame=None,
        _observation_attempt=0,
        _bound_selection=None,
    ) -> dict[str, Any]:
        screenshot_path = Path(screenshot_path) if screenshot_path is not None else (
            Path(tempfile.gettempdir()) / "uma_event_choice_route" / "screen.png"
        )

        if control_driver is not None:
            control_driver.reach("before_capture")
        adb = self.adb_factory()
        if mode == "live":
            if not adb.connect():
                return {"performed": False, "reason": "adb_connect_failed", "decision": {}}
            size = tuple(adb.get_screen_size())
            if size not in SUPPORTED_EVENT_CHOICE_SCREEN_SIZES:
                return {"performed": False, "reason": f"unsupported_screen_size:{size}", "decision": {}}
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                screenshot_path.unlink(missing_ok=True)
            except OSError:
                return {
                    "performed": False,
                    "reason": "screenshot_capture_failed",
                    "decision": {},
                }
            if initial_frame is not None:
                adb.save_frame(str(screenshot_path), initial_frame)
                captured = screenshot_path
            else:
                captured = adb.screenshot(str(screenshot_path))
            try:
                if captured is None or not screenshot_path.is_file():
                    raise OSError("screenshot_target_missing")
                if not screenshot_path.read_bytes():
                    raise OSError("screenshot_target_empty")
            except OSError:
                return {
                    "performed": False,
                    "reason": "screenshot_capture_failed",
                    "decision": {},
                }
        elif mode != "dry-run":
            return {"performed": False, "reason": "mode", "decision": {}}

        base_policy = policy or EventChoicePolicy()
        from core.auto_trainer.event_frame_reuse import reuse_event_packet
        packet = reuse_event_packet(screenshot_path, prior_observation) if mode == "live" else None
        reused = packet is not None
        packet = packet or self.safety_analyzer.analyze(
            screenshot_path,
            supplied_text=(base_policy.visible_text if mode == "dry-run" else None),
        )
        effective_policy = replace(base_policy, visible_text=packet.visible_text)
        selection = _bound_selection
        if choice_selector is not None and effective_policy.expected_event is None:
            selection = choice_selector(cv2.imread(str(screenshot_path)), packet.visible_text)
            if selection.get("status") == "selected":
                index, expected = selection["choice_index"], selection["expected_event"]
                choice = ("top" if index == 1 else "bottom" if index == len(expected) - 1
                          else {2: "second", 3: "third", 4: "fourth"}.get(index, f"option_{index}"))
                effective_policy = replace(effective_policy, choice=choice,
                                           expected_event=expected, option_index=index)
        if control_driver is not None:
            control_driver.reach("before_decision")
        if selection is not None and selection.get('status')=='blocked':
            decision={'action':'stop','reason':selection['reason'],'evidence':{}}
        else:
            decision = self.planner.plan(screenshot_path, policy=effective_policy)
        if selection is not None:
            decision.setdefault("evidence", {})["event_selection"] = selection
        decision.setdefault("evidence", {})["event_text_reused"] = reused
        missing_options=(decision.get('reason') in {'event_choice:no_option_points','event_choice:options_unreadable'}
            or decision.get('reason')=='event_choice:identity_mismatch' and packet.text_status=='abstained')
        if (missing_options and mode=='live' and execute and base_policy.allow_choose_event
                and confirm_token==EVENT_CHOICE_CONFIRM_TOKEN and decision.get('route_state')!='main_menu'
                and not packet.dangerous and (selection is None or selection.get('status')!='blocked')):
            if _observation_attempt<2:
                time.sleep(.12)
                return self.run_once(mode=mode,execute=execute,confirm_token=confirm_token,
                    policy=replace(effective_policy,visible_text=None),
                    screenshot_path=screenshot_path.with_name(screenshot_path.stem+'_reread'+str(_observation_attempt+1)+'.png'),
                    control_driver=control_driver,transition_preflight=transition_preflight,
                    choice_selector=choice_selector,prior_observation=None,initial_frame=None,
                    _observation_attempt=_observation_attempt+1,
                    _bound_selection=selection if selection and selection.get('status')=='selected' else None)
            decision['reason']='event_choice:observation_unverified'
        decision.setdefault('evidence',{})['read_only_observation_attempts']=_observation_attempt+1
        scoped_packet = packet
        if (
            decision.get("action") == "tap"
            and str(decision.get("reason", "")).startswith("choose_event_")
            and is_carrot_event_narrative(packet.visible_text, packet.resource_terms)
        ):
            scoped_packet = replace(packet, resource_terms=(), dangerous=False)
        safety_verdict = self.safety_gate.evaluate(scoped_packet, decision)
        safety_evidence = {
            "frame_safety": packet.to_dict(),
            "safety_verdict": safety_verdict.to_dict(),
        }
        if scoped_packet is not packet:
            safety_evidence["scoped_frame_safety"] = scoped_packet.to_dict()
            safety_evidence["frame_safety_scope_reason"] = (
                "event_narrative_carrot_false_positive"
            )
        if not safety_verdict.allowed:
            return {
                "performed": False,
                "reason": "action_safety_gate",
                "decision": decision,
                **safety_evidence,
            }
        if decision.get("action") != "tap":
            return {
                "performed": False,
                "reason": str(decision.get("reason", "stop")),
                "decision": decision,
                **safety_evidence,
            }
        if mode == "dry-run":
            return {
                "performed": False,
                "reason": "dry_run",
                "decision": decision,
                **safety_evidence,
            }
        if not execute:
            return {
                "performed": False,
                "reason": "execute",
                "decision": decision,
                **safety_evidence,
            }
        if confirm_token != EVENT_CHOICE_CONFIRM_TOKEN:
            return {
                "performed": False,
                "reason": "confirm_token",
                "decision": decision,
                **safety_evidence,
            }

        source_path = screenshot_path.resolve(strict=True)
        source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if source_sha != packet.source_image_sha256:
            return {
                "performed": False,
                "reason": "frame_safety_sha_mismatch",
                "decision": decision,
                **safety_evidence,
            }

        tap = decision["tap"]
        receipt_source = {
            "action_id": tap["coordinate_name"],
            "action_state": "event_choice",
            "coordinate_name": tap["coordinate_name"],
            "source_path": str(source_path),
            "source_sha256": source_sha,
        }
        if transition_preflight is not None:
            try:
                transition_preflight(receipt_source)
            except (OSError, ValueError) as exc:
                return {
                    "performed": False,
                    "reason": f"event_route_preflight:{exc}",
                    "decision": decision,
                    **safety_evidence,
                }
        if control_driver is not None:
            control_driver.reach("before_tap")
        tap_ok = adb.tap(
            tap["x"],
            tap["y"],
            scaled=bool(tap.get("scaled", False)),
        )
        if tap_ok is not True:
            return {
                "performed": False,
                "reason": "adb_tap_outcome_unknown",
                "decision": decision,
                **safety_evidence,
            }
        if control_driver is not None:
            post_path = screenshot_path.with_name(
                f"{screenshot_path.stem}_post{screenshot_path.suffix or '.png'}"
            )
            adb.screenshot(str(post_path))
            post_bytes = post_path.read_bytes()
            if not post_bytes:
                raise RuntimeError("event_choice_post_frame_empty")
            control_driver.after_fresh_frame(FreshFrameEvidence(
                path=str(post_path),
                sha256=hashlib.sha256(post_bytes).hexdigest(),
                captured_at=time.time(),
            ))
        return {
            "performed": True,
            "reason": str(decision["reason"]),
            "decision": decision,
            "route_action_receipt_source": receipt_source,
            **safety_evidence,
        }

    @staticmethod
    def _default_adb_factory() -> Any:
        from core.adb import ADB

        return ADB()
