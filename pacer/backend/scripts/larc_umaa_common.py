"""Load only UMAA's standalone device-lease leaf, without importing its core."""
import importlib.util
from pathlib import Path
import sys


def occupancy(umaa_root):
    root = Path(umaa_root).resolve(strict=True)
    path = root / 'core/device_occupancy.py'
    name = '_umaa_shared_device_occupancy'
    cached = sys.modules.get(name)
    if cached is not None:
        if Path(cached.__file__).resolve() != path.resolve():
            raise ValueError('umaa_device_lock_source_changed')
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError('umaa_device_lock_unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[name] = module
    return module
