"""Private cut feedback/service interface. Request JSON is read from stdin."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from desktop.cut_support_client import main

if __name__=='__main__':raise SystemExit(main())
