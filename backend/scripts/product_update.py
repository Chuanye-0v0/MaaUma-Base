"""Command-line entry for the bundled product updater; never imports game code."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from desktop.product_updates import main
if __name__ == '__main__':
    raise SystemExit(main())
