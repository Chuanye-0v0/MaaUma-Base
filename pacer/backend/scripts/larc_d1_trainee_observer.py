"""Read-only isolated use of UMAA's exact trainee/outfit recognizer."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys


def _enable_unicode_paths():
    helper = Path(__file__).with_name('larc_worker_control.py')
    if not helper.is_file():
        return
    spec = importlib.util.spec_from_file_location('_trainee_observer_unicode_io', helper)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.enable_unicode_image_paths()


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('d1-root','image','tokens','variant'):
        parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    root=Path(args.d1_root).resolve(strict=True)
    if not (root/'core/autonomous_training/trainee.py').is_file():
        bridge=root/'bridges/skills/backend'
        if (bridge/'core/autonomous_training/trainee.py').is_file():
            root=bridge.resolve(strict=True)
        else:
            raise ValueError('ordinary_trainee_library_missing')
    _enable_unicode_paths()
    sys.path.insert(0,str(root))
    from core.autonomous_training.trainee import variant_observation
    from core.autonomous_training.plant_catalog import VARIANTS
    import cv2
    if args.variant not in VARIANTS:
        raise ValueError('ordinary_trainee_variant_unknown')
    image=cv2.imread(args.image)
    if image is None or image.shape[:2] not in {(1280,720),(2560,1440)}:
        raise ValueError('ordinary_trainee_frame_unreadable')
    image=cv2.resize(image,(720,1280))
    result=variant_observation(image,
        json.loads(Path(args.tokens).read_text(encoding='utf-8')),args.variant)
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
