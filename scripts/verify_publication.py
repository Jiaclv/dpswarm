"""Verify the published evidence snapshot without model calls or grading.

Only the Python standard library is required. Original archive hashes are checked
when those archives are available; absence is explicitly reported, never a pass.
Browser results are separate receipts, not reproduced by this numerical verifier.
"""
import argparse
import csv
import hashlib
import json
import math
import re
import subprocess
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports/2026-09-07"

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))

def rows(name):
    with (REPORT / name).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))

def truth(value):
    return value.lower() == "true"

def verify(selection=None):
    checks = []
    def check(name, ok, detail=None):
        checks.append(dict(name=name, ok=bool(ok), detail=detail))

    manifest = read_json(REPORT / "PUBLICATION_MANIFEST.json")
    bad_public = [r["published"] for r in manifest["records"] if not (ROOT / r["published"]).exists() or sha(ROOT / r["published"]) != r["published_sha256"]]
    check("published_file_hashes", not bad_public, dict(files=len(manifest["records"]), mismatches=bad_public))
    originals = [r for r in manifest["records"] if (ROOT / r["source"]).exists()]
    bad_source = [r["source"] for r in originals if sha(ROOT / r["source"]) != r["source_sha256"]]
    check("available_original_hashes", not bad_source, dict(checked=len(originals), unavailable=len(manifest["records"])-len(originals), mismatches=bad_source))

    attempts = rows("full-history/ALL_ATTEMPTS.csv")
    qualified = rows("full-history/QUALIFIED_SWE_EPISODES.csv")
    check("inventory_denominators", len(attempts)==306 and len(qualified)==249 and len({r['task'] for r in qualified})==26,
          dict(attempts=len(attempts), qualified=len(qualified), unique_tasks=len({r['task'] for r in qualified})))
    check("qualified_episode_identity", len({r['episode_key'] for r in qualified})==249)
    paired = rows("component-attribution/PAIRED_18_DELIVERY.csv")
    totals = {key:sum(truth(r[key]) for r in paired) for key in ["s_delivery", "t_delivery", "s_pass", "t_pass"]}
    check("paired_18_totals", len(paired)==18 and totals==dict(s_delivery=13,t_delivery=17,s_pass=11,t_pass=13), totals)
    both = [r for r in paired if truth(r['s_delivery']) and truth(r['t_delivery'])]
    check("both_delivered_same_results", len(both)==13 and sum(truth(r['s_pass']) for r in both)==11 and all(r['s_pass']==r['t_pass'] for r in both))
    origins = rows("component-attribution/POSITIVE_10_ORIGINS.csv")
    counts = Counter(r['origin'] for r in origins)
    check("positive_episode_origins", len(origins)==10 and len({r['task'] for r in origins})==3 and sorted(counts.values())==[2,4,4], dict(counts))

    cm = [r for r in qualified if r['scope']=='C1']
    arms = {arm:[r for r in cm if r['arm']==arm] for arm in ['FCM_OFF','FCM_ON']}
    bridge = read_json(REPORT / "component-attribution/CM_COMPONENT_BRIDGE.json")
    arm_totals = {arm:dict(n=len(rr), passed=sum(truth(r['official_resolved']) for r in rr), production=sum(truth(r['prod_delivery']) for r in rr), tokens=sum(float(r['token']) for r in rr)) for arm,rr in arms.items()}
    check("cm_direct_arms", all(v['n']==9 and v['passed']==4 and v['production']==8 for v in arm_totals.values()), arm_totals)
    pairs = {(r['task'],r['rep']):r['official_resolved'] for r in arms['FCM_OFF']}
    check("cm_paired_outcomes", len(pairs)==9 and all(pairs.get((r['task'],r['rep']))==r['official_resolved'] for r in arms['FCM_ON']))
    role = rows("component-attribution/CM_ROLE_ACCOUNTING.csv")
    check("cm_token_bridge", bridge['ordinary_input_delta']+bridge['ordinary_output_delta']==bridge['ordinary_total_delta'] and bridge['ordinary_total_delta']+bridge['cm_total_tokens']==bridge['net_token_delta']==-202460 and bridge['on_total_tokens']==arm_totals['FCM_ON']['tokens']==3747917 and bridge['off_total_tokens']==arm_totals['FCM_OFF']['tokens']==3950377 and sum(float(r['total_tokens_delta']) for r in role)==bridge['net_token_delta'])
    check("cm_cost_bridge", math.isclose(sum(float(r['api_equivalent_usd_delta']) for r in role),bridge['net_cost_delta'],abs_tol=1e-9) and math.isclose(bridge['net_token_pct'],100*bridge['net_token_delta']/bridge['off_total_tokens'],abs_tol=1e-9))

    render = {}
    for name in ['full-history','component-attribution']:
        receipt = read_json(REPORT/name/'PUBLICATION_RENDER.json')
        files = {'html_sha256':'REPORT_ZH.html','artifact_sha256':'portable/artifact.json','source_md_sha256':'REPORT_ZH.md'}
        check(name+'_canonical_structure_and_binding', receipt['canonical_structure']['ok'] and all(sha(REPORT/name/p)==receipt[k] for k,p in files.items()))
        render[name] = dict(canonical_structure=True, browser='separate real-time QA receipt; not rerun here')

    browser = read_json(REPORT / "PUBLICATION_BROWSER_QA.json")
    check("recorded_browser_qa_bound_to_current_html", browser['ok'] and all(r['ok'] and sha(REPORT/r['name']/'REPORT_ZH.html')==r['html_sha256'] for r in browser['results']), "Recorded real-time browser QA, not rerun by this script")

    # A link must exist in the selected publication or Git, not only in local archives.
    tracked = set(subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode('utf-8').split('\0'))
    available = tracked | (set(read_json(selection)) if selection else set())
    if not selection: available |= {p.relative_to(ROOT).as_posix() for p in REPORT.rglob('*') if p.is_file()}
    docs = [ROOT/p for p in ['README.md','reports/README.md','dpswarm-plugin/README.md','dpswarm-dsh-plugin/README.md','modelbench/big_budget_team_20260906/README.md','modelbench/mechanism_ablation_20260906/README.md']]
    docs += list(REPORT.rglob('*.md'))
    broken=[]; link_count=0
    for doc in docs:
        text=re.sub(r'```[\s\S]*?```','',doc.read_text('utf-8-sig'))
        for target in re.findall(r'!?\[[^\]]*\]\((<[^>]+>|[^)]+)\)',text):
            target=unquote(target.strip('<>').split('#')[0])
            if not target or re.match(r'(?:[\w+.-]+://|mailto:)',target): continue
            link_count+=1
            path=(doc.parent/target).resolve()
            if not path.is_relative_to(ROOT) or path.relative_to(ROOT).as_posix() not in available or not path.is_file():
                broken.append(dict(document=doc.relative_to(ROOT).as_posix(),target=target))
    check('published_document_links',not broken,dict(documents=len(docs),links=link_count,broken=broken))
    return dict(date='2026-09-07',scope='publication copy; local recomputation, not a new independent review or experiment',ok=all(c['ok'] for c in checks),checks=checks,render=render)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    result=verify(args.selection)
    if args.output: args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['ok'] else 1)
