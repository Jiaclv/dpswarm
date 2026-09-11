import re
import sys
import glob

sys.path.insert(0, r"K:\秋招\项目\DPswarm\dpswarm-plugin")
from dpswarm.orchestrator_lg import _extract_atomic_facts

for f in sorted(glob.glob("runs-r2/*/artifacts/*.txt")):
    text = open(f, encoding="utf-8").read()
    out = _extract_atomic_facts(text)
    residual = re.sub(r"```[^\n]*\r?\n.*?```", "", text, flags=re.S)
    tables = [l for l in residual.splitlines() if l.strip().startswith("|")]
    kv = [l for l in residual.splitlines() if re.match(r"^\s*[A-Z_][A-Z0-9_]{2,}\s*=", l)]
    paths = [l for l in residual.splitlines() if re.search(r"[\w\-]+/[\w\-./]+\.(py|json|md|txt)", l)]
    sigs = [l for l in residual.splitlines() if re.match(r"^\s*(async\s+def|def|class)\s+\w+", l)]
    name = f.replace("\\", "/").split("/")[-1][:12]
    print("%s len=%5d l1=%5d | 网外: 表行%d kv%d 路径%d 签名%d"
          % (name, len(text), len(out), len(tables), len(kv), len(paths), len(sigs)))
