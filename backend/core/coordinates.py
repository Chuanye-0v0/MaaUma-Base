"""Coordinate loading and resolution scaling utilities."""
from __future__ import annotations
import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
DEFAULT_REFERENCE_SIZE = (1440, 2560)
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]

def coordinate_binding_sha256(*, section: str, name: str, x: int, y: int, reference_size: tuple[int, int], fixture_sha256: str, source_size: tuple[int, int], observed_point: tuple[int, int]) -> str:
    """Hash the reviewed coordinate with its immutable fixture provenance."""
    payload = {'coordinate': f'{section}.{name}', 'fixture_sha256': fixture_sha256, 'observed_point': {'x': observed_point[0], 'y': observed_point[1]}, 'reference_point': {'x': x, 'y': y}, 'reference_size': {'width': reference_size[0], 'height': reference_size[1]}, 'source_size': {'width': source_size[0], 'height': source_size[1]}, 'version': 'coordinate_binding.v1'}
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(canonical.encode('ascii')).hexdigest()

@dataclass(frozen=True)
class CoordinateScaler:
    """Scale coordinates from a reference screenshot size to the device size."""
    reference_size: tuple[int, int] = DEFAULT_REFERENCE_SIZE
    current_size: tuple[int, int] = DEFAULT_REFERENCE_SIZE

    def scale_point(self, x: int | float, y: int | float) -> tuple[int, int]:
        ref_w, ref_h = self.reference_size
        cur_w, cur_h = self.current_size
        if ref_w <= 0 or ref_h <= 0 or cur_w <= 0 or (cur_h <= 0):
            return (int(round(x)), int(round(y)))
        return (int(round(float(x) * cur_w / ref_w)), int(round(float(y) * cur_h / ref_h)))

class CoordinateConfig:
    """Read named game coordinates from config/coordinates.json."""

    def __init__(self, path: str | Path='config/coordinates.json'):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open('r', encoding='utf-8') as f:
            return json.load(f)

    @property
    def reference_size(self) -> tuple[int, int]:
        screen = self.data.get('screen', {})
        return (int(screen.get('width', DEFAULT_REFERENCE_SIZE[0])), int(screen.get('height', DEFAULT_REFERENCE_SIZE[1])))

    def point(self, section: str, name: str) -> tuple[int, int]:
        coord = self.data[section][name]
        return (int(coord['x']), int(coord['y']))

    def require_verified(self, section: str, name: str) -> dict[str, Any]:
        coordinate_name = f'{section}.{name}'
        error = ValueError(f'coordinate_unverified:{coordinate_name}')
        try:
            coordinate = self.data[section][name]
        except (KeyError, TypeError):
            raise error from None
        if not isinstance(coordinate, dict):
            raise error
        verification = coordinate.get('verification')
        if not isinstance(verification, dict):
            raise error
        if verification.get('status') != 'verified':
            raise error
        if verification.get('basis') != 'real_live_frame_visual_review':
            raise error
        for path_key in ('source_path', 'fixture_path'):
            value = verification.get(path_key)
            if not isinstance(value, str) or not value.strip():
                raise error
        source_sha256 = verification.get('source_sha256')
        if not self._is_lower_sha256(source_sha256):
            raise error
        source_size = verification.get('source_size')
        observed_point = verification.get('observed_point')
        if not isinstance(source_size, dict) or not isinstance(observed_point, dict):
            raise error
        width = source_size.get('width')
        height = source_size.get('height')
        observed_x = observed_point.get('x')
        observed_y = observed_point.get('y')
        values = (width, height, observed_x, observed_y)
        if any((not isinstance(value, int) or isinstance(value, bool) for value in values)):
            raise error
        if width <= 0 or height <= 0:
            raise error
        try:
            coordinate_x = coordinate['x']
            coordinate_y = coordinate['y']
            if not isinstance(coordinate_x, int) or isinstance(coordinate_x, bool) or (not isinstance(coordinate_y, int)) or isinstance(coordinate_y, bool):
                raise ValueError
            reference_point = (coordinate_x, coordinate_y)
        except (KeyError, TypeError, ValueError):
            raise error from None
        expected_observed = CoordinateScaler(self.reference_size, (width, height)).scale_point(*reference_point)
        if (observed_x, observed_y) != expected_observed:
            raise error
        fixture_sha256 = self._verified_fixture_sha256(verification)
        if fixture_sha256 is None or not hmac.compare_digest(fixture_sha256, source_sha256):
            raise error
        binding = verification.get('coordinate_binding_sha256')
        if not self._is_lower_sha256(binding):
            raise error
        expected_binding = coordinate_binding_sha256(section=section, name=name, x=reference_point[0], y=reference_point[1], reference_size=self.reference_size, fixture_sha256=fixture_sha256, source_size=(width, height), observed_point=(observed_x, observed_y))
        if not hmac.compare_digest(binding, expected_binding):
            raise error
        return coordinate

    def verified_point(self, section: str, name: str) -> tuple[int, int]:
        coordinate = self.require_verified(section, name)
        return (int(coordinate['x']), int(coordinate['y']))

    def scaled_point(self, section: str, name: str, current_size: tuple[int, int]) -> tuple[int, int]:
        scaler = CoordinateScaler(self.reference_size, current_size)
        return scaler.scale_point(*self.point(section, name))

    @staticmethod
    def _is_lower_sha256(value: object) -> bool:
        return isinstance(value, str) and len(value) == 64 and (value == value.lower()) and all((char in '0123456789abcdef' for char in value))

    @staticmethod
    def _verified_fixture_sha256(verification: dict[str, Any]) -> str | None:
        fixture_path = verification.get('fixture_path')
        if not isinstance(fixture_path, str) or not fixture_path:
            return None
        relative_path = Path(fixture_path)
        if relative_path.is_absolute() or any((part in {'.', '..'} for part in relative_path.parts)) or relative_path.as_posix() != fixture_path:
            return None
        workspace = WORKSPACE_ROOT.resolve()
        candidate = workspace / relative_path
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(workspace)
        except (OSError, RuntimeError, ValueError):
            return None
        if not resolved.is_file():
            return None
        current = workspace
        for part in relative_path.parts:
            current = current / part
            if current.is_symlink():
                return None
        try:
            return hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError:
            return None
