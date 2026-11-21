"""Installed package features; selecting an update channel does not enable code."""
import json
from pathlib import Path
from desktop.product_updates import manifest_channel

ASSESSMENT_FILES=frozenset({
    'backend/core/auto_trainer/larc_career_assessment.py',
    'backend/core/auto_trainer/larc_assessment_report.py',
    'backend/scripts/larc_continuation_review.py',
})
CONTINUATION_GATE_FILES=frozenset({
    'backend/core/auto_trainer/larc_continuation.py',
    'backend/scripts/larc_continuation_runtime.py',
    'backend/scripts/larc_continuation_collect.py',
    'backend/scripts/larc_continuation_inspection.py',
    'backend/scripts/larc_continuation_skill_worker.py',
})
CONTINUATION_FILES=ASSESSMENT_FILES | CONTINUATION_GATE_FILES
# Shared ordinary-career helpers are required by the read-only skill worker.
# They are not preview-exclusive files and must not be rejected in stable.
CONTINUATION_RUNTIME_DEPENDENCIES=frozenset({
    'backend/scripts/larc_d1_prerace_skill_worker.py',
    'backend/scripts/larc_skill_completion_frames.py',
    'backend/scripts/larc_worker_control.py',
    'backend/scripts/capture_ug_parent_live_tabs.py',
})
PACER_CENTER_FILES=frozenset({
    'backend/core/career_panel_presets.py','backend/scripts/larc_panel_runner.py',
    'backend/scripts/larc_control_window.py','backend/scripts/larc_cut_standalone_host.py',
    'backend/scripts/larc_umaa_host.py','backend/core/auto_trainer/cut_runtime_license.py',
    'backend/core/auto_trainer/career_review_export.py',
})


def continuation_dependencies_present(root, manifest):
    required = CONTINUATION_GATE_FILES | CONTINUATION_RUNTIME_DEPENDENCIES
    return required.issubset(manifest.get('files', [])) and all(
        (Path(root) / name).is_file() for name in required)


def profile(root=None):
    development=root is None
    if root is None:
        root=Path(__file__).resolve().parents[1]
        if root.name.casefold()=='backend':root=root.parent;development=False
    path=Path(root)/'product-package-manifest.json'
    if not path.exists():return {'channel':'preview','continuation_assessment':development,'continuation_gates':development,'decision_preferences':development}
    try:
        manifest=json.loads(path.read_text(encoding='utf-8-sig'))
        channel=manifest_channel(manifest) or 'stable'
        legacy=manifest.get('version')=='10.20.0-preview.11'
        enabled=(manifest.get('features') or {}).get('continuation_assessment',legacy) is True
        gates=((manifest.get('features') or {}).get('continuation_gates') is True
               and continuation_dependencies_present(root, manifest))
        return {'channel':channel,'continuation_assessment':channel=='preview' and enabled,
                'continuation_gates':channel=='preview' and gates,
                'decision_preferences':channel=='preview' and (manifest.get('features') or {}).get('decision_preferences') is True}
    except (OSError,ValueError,TypeError,AttributeError):
        return {'channel':'stable','continuation_assessment':False,'continuation_gates':False,'decision_preferences':False}


def continuation_enabled(root=None):
    return profile(root)['continuation_assessment']


def pacer_center_enabled(root=None):
    """Only an installed preview explicitly offers the combined private panel.

    Update-channel settings do not activate this feature. Explicit roots with
    no manifest (including old standalone installs) keep their existing mode.
    """
    if root is None:
        root=Path(__file__).resolve().parents[1]
    root=Path(root)
    if root.name.casefold()=='backend':root=root.parent
    try:
        manifest=json.loads((root/'product-package-manifest.json').read_text(encoding='utf-8-sig'))
        return (manifest.get('product_id')=='uma-cut' and manifest_channel(manifest)=='preview'
                and (manifest.get('features') or {}).get('pacer_center') is True)
    except (OSError,ValueError,TypeError,AttributeError):
        return False
