"""Source-only boundary and synthetic saved-record recognition regressions."""
import ast
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'backend'))
from core.account_login import observer


@pytest.fixture(autouse=True)
def unrelated_page_templates_are_not_needed(monkeypatch):
    # This regression supplies its own records layout; game assets are excluded
    # from the public source export and are unrelated to nickname verification.
    monkeypatch.setattr(observer, 'PageIdentity', lambda image: SimpleNamespace(
        story_surface=lambda: False, home=lambda tokens: False, dialog_above=lambda y: False))


def record_frame(tmp_path, *, repeated=False):
    image = np.zeros((1280, 720, 3), np.uint8)
    template = np.random.default_rng(45).integers(0, 256, (20, 80), dtype=np.uint8)
    image[540:560, 200:280] = template[:, :, None]
    if repeated:
        image[640:660, 200:280] = template[:, :, None]
    ok, encoded = cv2.imencode('.png', image)
    assert ok
    path = tmp_path / 'user-local-template.png'
    assert cv2.imwrite(str(path), template)
    return encoded.tobytes(), path


def record_tokens(first='uncertain-label', confidence=0.7):
    def token(text, score, box):
        return dict(text=text, confidence=score, bbox=box)
    return [token('登录记录', 0.99, [250, 350, 420, 390]),
            token(first, confidence, [200, 540, 300, 560]),
            token('上次登录', 0.99, [200, 575, 350, 595]),
            token('sample-beta', 0.99, [200, 640, 320, 660]),
            token('上次登录', 0.99, [200, 675, 350, 695])]


def observe(raw, tokens, **options):
    return observer.observe(raw, tokens, target='sample-alpha', collect_records=True,
                            records_layout_verified=True, **options)


def test_default_never_loads_or_guesses_a_personal_template(tmp_path, monkeypatch):
    raw, _ = record_frame(tmp_path)
    def forbidden_match(*args, **kwargs):
        raise AssertionError('default observation tried template matching')
    monkeypatch.setattr(observer.cv2, 'matchTemplate', forbidden_match)
    result = observe(raw, record_tokens())
    assert result['page'] == 'records'
    assert result['record_names'] is None
    assert 'select_record' not in result['actions']


def test_default_exact_confident_ocr_keeps_generic_behavior(tmp_path):
    raw, _ = record_frame(tmp_path)
    result = observe(raw, record_tokens('sample-alpha', 0.99))
    assert result['record_names'] == ['sample-alpha', 'sample-beta']
    assert 'select_record' in result['actions']


def test_explicit_local_mapping_can_verify_one_low_confidence_row(tmp_path):
    raw, template = record_frame(tmp_path)
    result = observe(raw, record_tokens(), saved_name_templates={'sample-alpha': template})
    assert result['record_names'] == ['sample-alpha', 'sample-beta']
    assert result['actions']['select_record']['point'] == [240, 550]
    assert result['target_visual_score'] >= 0.95


def test_local_template_selection_also_works_without_record_collection(tmp_path):
    raw, template = record_frame(tmp_path)
    result = observer.observe(raw, record_tokens(), target='sample-alpha',
                              records_layout_verified=True,
                              saved_name_templates={'sample-alpha': template})
    assert result['actions']['select_record']['point'] == [240, 550]
    assert 'record_names' not in result


def test_repeated_template_does_not_choose_a_record(tmp_path):
    raw, template = record_frame(tmp_path, repeated=True)
    result = observe(raw, record_tokens(), saved_name_templates={'sample-alpha': template})
    assert result['record_names'] is None
    assert 'select_record' not in result['actions']


def test_conflicting_local_names_do_not_supply_an_identity(tmp_path):
    raw, template = record_frame(tmp_path)
    result = observe(raw, record_tokens(), saved_name_templates={
        'sample-alpha': template, 'sample-gamma': template})
    assert result['record_names'] is None
    assert 'select_record' not in result['actions']


def test_missing_local_template_does_not_guess(tmp_path):
    raw, _ = record_frame(tmp_path)
    result = observe(raw, record_tokens(), saved_name_templates={
        'sample-alpha': tmp_path / 'not-provided.png'})
    assert result['record_names'] is None
    assert 'select_record' not in result['actions']


def test_local_template_does_not_rewrite_conflicting_confident_ocr(tmp_path):
    raw, template = record_frame(tmp_path)
    result = observe(raw, record_tokens('known-other-label', 0.99),
                     saved_name_templates={'sample-alpha': template})
    assert result['record_names'] == ['known-other-label', 'sample-beta']
    assert 'select_record' not in result['actions']


def test_backend_and_agent_have_no_private_engine_or_user_payload():
    assert Path(observer.__file__).resolve().is_relative_to(REPO / 'backend')
    secret = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,}\b')
    for folder in [REPO / 'backend', REPO / 'agent']:
        for path in folder.rglob('*'):
            if not path.is_file() or '__pycache__' in path.parts:
                continue
            relative = path.relative_to(REPO).as_posix()
            assert path.suffix == '.py' or (path.suffix == '.json' and relative.startswith('backend/config/')), \
                'non-source/user payload: ' + relative
            assert path.name.casefold() not in {'private-service-auth.json', 'distribution-receipt.json',
                'mfa-accounts.json', 'control_token.json', 'credentials.json'}
            assert '/core/auto_trainer/' not in '/' + relative
            assert not re.search(r'/(?:larc_panel|larc_cut|larc_umaa)[^/]*\.py$', relative)
            assert not secret.search(path.read_text(encoding='utf-8-sig')), 'credential literal: ' + relative
    # Public activation/component contracts are allowed client code, not engines.
    assert 'uma-cut' in (REPO / 'backend/desktop/maauma_service.py').read_text(encoding='utf8')


def test_record_observer_has_no_hardcoded_target_identity():
    tree = ast.parse(Path(observer.__file__).read_text(encoding='utf8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == 'target':
            assert not any(isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value
                           for value in node.comparators), 'hardcoded target-name fallback'
