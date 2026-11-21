"""Once-per-run in-career deck inspection route (菜单 -> 编成信息 -> 关闭)."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import time

import cv2

from core.auto_trainer.frame_safety import FormalActionSafetyGate, FrameSafetyAnalyzer
from core.auto_trainer.larc_deck_inspect import (
    FORMATION_BUTTON,
    FORMATION_CLOSE,
    MENU_CLOSE,
    classify_page,
    home_menu_point,
    needs_inspection,
    parse_formation_info,
    record_failure,
    record_inspection,
)
from core.auto_trainer.larc_outing_route import menu_identity
from core.auto_trainer.main_menu_state_provider import provide_main_menu_state
from core.auto_trainer.route_transition_session import (
    RouteTransitionError,
    RouteTransitionSession,
)
from core.auto_trainer.run_support_profile import load_profile
from core.execution_frames import ExecutionFrames
from core.native_capture_adb import adb_for_capture
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json


def _scale_point(point, image):
    height, width = image.shape[:2]
    return (round(point[0] * width / 720), round(point[1] * height / 1280))


class DeckInspectNavigation:
    def __init__(
        self,
        ctx,
        *,
        adb=None,
        safety=None,
        gate=None,
        provider=None,
        line_reader=None,
        sleep=time.sleep,
    ):
        self.ctx = ctx
        self.frames = ExecutionFrames(adb or adb_for_capture(ctx.port))
        self.safety = safety or getattr(ctx, 'screen_reader', None) or FrameSafetyAnalyzer()
        self.gate = gate or FormalActionSafetyGate()
        self.provider = provider or provide_main_menu_state
        self.line_reader = line_reader
        self.sleep = sleep
        self.route = RouteTransitionSession.load(
            ctx.route, expected_run_session_id=ctx.run_id
        )
        self.sequence = 0
        self.tapped = False

    def observe(self, path, screen, *, resync=False):
        sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        self.route.observe(screen, source_sha256=sha, resync=resync)
        self.route.save(self.ctx.route)
        return sha

    def tap(self, path, point, screen, action, next_states, *, packet=None):
        if not self.frames.connect():
            raise ValueError('deck_inspect_device_connect_failed')
        sha = self.observe(path, screen)
        decision = {
            'action': 'tap',
            'reason': action,
            'route_state': screen,
            'tap': {
                'coordinate_name': action,
                'x': point[0],
                'y': point[1],
                'scaled': False,
            },
        }
        analyzed = packet or self.safety.analyze(path)
        verdict = self.gate.evaluate(analyzed, decision)
        if not verdict.allowed or analyzed.source_image_sha256 != sha:
            raise ValueError(f'deck_inspect_safety_blocked:{verdict.reason}')
        if self.frames.tap(*point, scaled=False) is not True:
            raise ValueError('deck_inspect_tap_unconfirmed')
        self.tapped = True
        self.route.commit_action(
            action, expected_next_states=next_states, source_sha256=sha
        )
        self.route.save(self.ctx.route)
        self.sleep(.2)
        return analyzed

    def capture(self, folder):
        if not self.frames.connect():
            raise ValueError('deck_inspect_device_connect_failed')
        path = Path(folder) / f'frame_{self.sequence:02d}.png'
        self.sequence += 1
        image = self.frames.screenshot(str(path))
        if image is None and path.is_file():
            image = cv2.imread(str(path))
        if image is None or not path.is_file():
            raise ValueError('deck_inspect_capture_failed')
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        return path, image, sha

    def _wait_modal(self, folder, target, *, polls=10):
        partial_settled = False
        for _ in range(polls):
            path, image, sha = self.capture(folder)
            if sha == self.route.action_source_sha256:
                self.sleep(.2)
                continue
            pixel_page = classify_page(image)
            if pixel_page != target:
                self.sleep(.2)
                continue
            if target == 'formation_info' and not partial_settled:
                settled_probe = parse_formation_info(image)
                if not (settled_probe['complete'] and all(r.get('card_id') for r in settled_probe['slots'])):
                    partial_settled = True
                    self.sleep(.2)
                    continue
            packet = self.safety.analyze(path)
            if classify_page(image, packet.visible_text) == target or pixel_page == target:
                return path, image, sha, packet
            self.sleep(.2)
        raise ValueError(f'deck_inspect_{target}_not_settled')

    def _return_main_menu(self, folder, before_state, *, polls=12):
        turn = self.ctx.progress['next_turn']
        menu_closed = False
        for _ in range(polls):
            path, image, sha = self.capture(folder)
            if sha == self.route.action_source_sha256:
                self.sleep(.2)
                continue
            page = classify_page(image)
            if page == 'career_menu' and not menu_closed:
                packet = self.safety.analyze(path)
                self.tap(
                    path,
                    _scale_point(MENU_CLOSE, image),
                    'modal',
                    'deck_inspect.close_menu',
                    ('main_menu', 'modal', 'loading'),
                    packet=packet,
                )
                menu_closed = True
                continue
            if page != 'main_menu':
                self.sleep(.2)
                continue
            provider = self.provider(
                path,
                path.with_suffix('.state.json'),
                turn_index=turn,
                scenario='larc',
            )
            after_state = provider.get('state_context', {})
            if (
                provider.get('valid_for_training') is True
                and after_state.get('scenario_state', {}).get('scenario') == 'larc'
                and provider.get('larc_period', {}).get('status') == 'verified'
            ):
                if before_state and menu_identity(after_state) != menu_identity(before_state):
                    raise ValueError('deck_inspect_return_identity_mismatch')
                self.observe(path, 'main_menu')
                self._rebind_menu_observation(str(path), sha)
                return str(path), provider
            self.sleep(.2)
        raise ValueError('deck_inspect_return_main_menu_unverified')

    def _rebind_menu_observation(self, screenshot_path, sha):
        observation = getattr(self.ctx, 'menu_observation', None)
        if not isinstance(observation, dict):
            return
        updated = deepcopy(observation)
        updated['screenshot_path'] = screenshot_path
        result = updated.setdefault('result', {})
        result.setdefault('frame_safety', {})['source_image_sha256'] = sha
        self.ctx.menu_observation = updated

    def run(self, source, provider, folder):
        image = cv2.imread(str(source))
        menu_pt = home_menu_point(image) if image is not None else None
        if menu_pt is None:
            return {'ok': False, 'visited': False, 'reason': 'home_menu_icon_not_found'}, source, provider
        before_state = (provider or {}).get('state_context', {})
        self.tap(
            source,
            _scale_point(menu_pt, image),
            'main_menu',
            'deck_inspect.open_menu',
            ('modal', 'loading', 'main_menu'),
        )
        menu_path, menu_img, _, menu_packet = self._wait_modal(folder, 'career_menu')
        self.tap(
            menu_path,
            _scale_point(FORMATION_BUTTON, menu_img),
            'modal',
            'deck_inspect.open_formation',
            ('modal', 'loading', 'main_menu'),
            packet=menu_packet,
        )
        info_path, info_img, info_sha, info_packet = self._wait_modal(folder, 'formation_info')
        parsed = parse_formation_info(info_img, line_reader=self.line_reader)
        turn = self.ctx.progress['next_turn']
        if parsed['status'] in {'complete', 'partial'}:
            saved = record_inspection(
                self.ctx.root,
                self.ctx.run_id,
                parsed,
                turn_index=turn,
                source_sha256=info_sha,
            )
        else:
            saved = record_failure(
                self.ctx.root,
                self.ctx.run_id,
                'formation_cards_unreadable',
                turn_index=turn,
            )
        self.ctx.support_profile = saved
        self.tap(
            info_path,
            _scale_point(FORMATION_CLOSE, info_img),
            'modal',
            'deck_inspect.close_formation',
            ('modal', 'main_menu', 'loading'),
            packet=info_packet,
        )
        returned_source, returned_provider = self._return_main_menu(folder, before_state)
        return (
            {
                'ok': parsed['status'] in {'complete', 'partial'},
                'visited': True,
                'turn_consumed': False,
                'status': parsed['status'],
                'types': parsed['types'],
                'counts': parsed['counts'],
                'card_ids': parsed['card_ids'],
                'rarities': parsed['rarities'],
                'levels': parsed['levels'],
                'limit_breaks': parsed['limit_breaks'],
                'formation_screenshot_path': str(info_path),
                'formation_sha256': info_sha,
                'returned_screenshot_path': returned_source,
            },
            returned_source,
            returned_provider,
        )


def prepare_deck_inspection(
    ctx,
    source,
    provider,
    output_dir,
    *,
    cut_mode=False,
    navigation_factory=DeckInspectNavigation,
):
    """Inspect 编成信息 once per run when no complete 6-slot typed deck is known."""
    if cut_mode:
        return None, source, provider
    profile = getattr(ctx, 'support_profile', None)
    if (ctx.root / 'session/support_card_profile.json').is_file():
        try:
            profile = load_profile(ctx.root, ctx.run_id)
            ctx.support_profile = profile
        except (OSError, ValueError):
            profile = None
    if not needs_inspection(profile):
        return None, source, provider
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    turn = ctx.progress['next_turn']
    navigation = None
    try:
        navigation = navigation_factory(ctx)
        report, new_source, new_provider = navigation.run(source, provider, output)
        if not report.get('visited') and needs_inspection(getattr(ctx, 'support_profile', None)):
            ctx.support_profile = record_failure(
                ctx.root, ctx.run_id, report.get('reason', 'not_visited'), turn_index=turn
            )
    except (ValueError, RuntimeError, OSError, RouteTransitionError) as exc:
        reason = f'{type(exc).__name__}:{exc}'
        tapped = bool(getattr(navigation, 'tapped', False))
        if needs_inspection(getattr(ctx, 'support_profile', None)):
            try:
                ctx.support_profile = record_failure(
                    ctx.root, ctx.run_id, reason, turn_index=turn
                )
            except (OSError, ValueError):
                pass
        report = {
            'ok': False,
            'visited': tapped,
            'turn_consumed': False,
            'status': 'failed',
            'reason': reason,
        }
        new_source, new_provider = (None if tapped else source), provider
    _atomic_write_json(output / 'report.json', report)
    return report, new_source, new_provider
