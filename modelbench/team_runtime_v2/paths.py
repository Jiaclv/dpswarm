from pathlib import Path
import sys


class RecoveryRequired(RuntimeError):
    """An interrupted side effect requires explicit reconciliation."""


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
LEGACY = ROOT.parent / 'team_eval20260903'
PLUGIN = REPO / 'dpswarm-plugin'
for path in (PLUGIN, LEGACY):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
