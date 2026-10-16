"""Portable agent entry for embedded Python and MFA's first IPC identifier.

No discovery, VM configuration, device switching or task start occurs here.
Keep this entry compatible with existing independent LDPlayer launchers.
"""
from pathlib import Path
import runpy
import sys

agent_root = Path(__file__).resolve().parent
sys.path.insert(0, str(agent_root))
sys.path.insert(0, str(agent_root.parent / 'backend'))
entry = agent_root / 'main.py'
sys.argv = [str(entry), *sys.argv[1:2]]
runpy.run_path(str(entry), run_name='__main__')
