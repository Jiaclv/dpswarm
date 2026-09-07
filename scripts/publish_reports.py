"""Export frozen local analyses into a reviewable GitHub report snapshot.

Requires the local modelbench analysis archives; does not call models or graders.
Original analyses stay immutable. Publication hashes describe the copies, while
original validation receipts remain explicitly labeled historical evidence.
"""
from pathlib import Path
import base64,hashlib,json,re,subprocess,os,shutil
from urllib.parse import unquote
R=Path(__file__).resolve().parents[1];OUT=R/'reports/2026-09-07'
SPECS={
 'full-history':R/'modelbench/cumulative_analysis_20260907_v2',
 'component-attribution':R/'modelbench/component_attribution_20260907_v1',
}
REPO_URL='https://github.com/Jiaclv/dpswarm/blob/main/'
EXCLUDE={'ARTIFACT_HASHES.json','PORTABLE_RECEIPT.json','EPISODES.json','WORKERS.json','TOOL_TIMELINES.json','PATCHES.json'}
RECEIPTS={'REPORT_VALIDATION.json','DATA_VALIDATION.json','REVIEWED_SOURCE_HASHES.json','SOURCE_HASHES.json'}
NATIVE_SCRIPT=Path(os.environ.get('DP_REPORT_BUILDER_DIR','D:/codex-home/plugins/cache/openai-curated-remote/data-analytics/0.2.10-13ceeea1f599/skills/build-report/scripts'))
NODE=Path(os.environ.get('DP_REPORT_NODE') or shutil.which('node') or (Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'))
LINK=re.compile(r'(?<!!)\[([^\]]+)\]\((<[^>]+>|[^)]+)\)')
records=[];omitted=[];source_digest={}
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,s):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(s,encoding='utf-8',newline='\n')
def dump(p,v):write(p,json.dumps(v,ensure_ascii=False,indent=2)+'\n')
def normalize(s):
 for prefix in sorted({str(R)+'\\',R.as_posix()+'/',str(R).replace('\\','\\\\')+'\\\\'},key=len,reverse=True):s=s.replace(prefix,'')
 # Notebook/runtime locations are provenance only; strip machine user identities.
 s=re.sub(r'[A-Za-z]:[/\\]+Users[/\\]+[^/\\\s"<>]+', 'LOCAL_HOME',s)
 s=re.sub(r'[A-Za-z]:[/\\]+codex-home', 'LOCAL_CODEX_HOME',s)
 return s
def walk(v):
 if isinstance(v,str):return normalize(v)
 if isinstance(v,list):return [walk(x) for x in v]
 if isinstance(v,dict):return {normalize(k):walk(x) for k,x in v.items()}
 return v
def selected(root):
 files=[]
 for p in root.iterdir():
  if p.is_file() and p.suffix in {'.csv','.json','.md'} and p.name not in EXCLUDE:files.append(p)
 files += list((root/'figures').glob('*.png'))
 for sub in ['context_synthesis','role_synthesis','team_synthesis','independent_review']:
  if (root/sub).exists():files += [p for p in (root/sub).iterdir() if p.is_file() and p.suffix in {'.csv','.json','.md','.txt'}]
 return files
mapping={p.resolve():OUT/name/p.relative_to(root) for name,root in SPECS.items() for p in selected(root)}
# Preserve the original HTML/MD hashes even though publication notes/links change.
for root in SPECS.values():
 for p in selected(root)+[root/'REPORT_ZH.html',root/'portable/artifact.json']:source_digest[p]=sha(p)

def link_target(raw,source):
 target=unquote(raw.strip().strip('<>'))
 if re.match(r'^(https?://|mailto:|#)',target,re.I):return target,False
 file,sep,anchor=target.partition('#');file=re.sub(r':\d+$','',file)
 if re.match(r'^[A-Za-z]:[/\\]',file):p=Path(file)
 elif file.replace('\\','/').startswith(('modelbench/','reports/','dpswarm-')):p=R/file
 else:p=source.parent/file
 p=p.resolve();dest=mapping.get(p)
 if dest is not None:return dest.relative_to(R).as_posix()+('#'+anchor if sep else ''),True
 if p.exists() and p.is_relative_to(R) and not p.is_relative_to(R/'modelbench') and p.suffix=='.md':
  tracked=subprocess.run(['git','ls-files','--error-unmatch',str(p.relative_to(R))],cwd=R,capture_output=True).returncode==0
  if tracked:return p.relative_to(R).as_posix()+('#'+anchor if sep else ''),True
 return normalize(target),None

def links(s,source,dest,for_html=False):
 def replace(m):
  label,raw=m.groups();target,available=link_target(raw,source)
  if available is None:
   omitted.append({'source':source.relative_to(R).as_posix(),'reference':normalize(raw),'reason':'local archival evidence is not bundled'})
   return label+'（本地归档）'
  if available:
   if for_html:target=REPO_URL+target
   else:
    import os
    file,sep,anchor=target.partition('#');target=Path(os.path.relpath(R/file,dest.parent)).as_posix()+('#'+anchor if sep else '')
  return f'[{label}](<{target}>)'
 return LINK.sub(replace,s)

ADD=("\n\n**2026-09-07 归因补充：**测试者的平均独立净收益仍未识别，但逐次轨迹已确认 Pylint 的测试暴露实现遗漏、Lead 随后补修，以及 Matplotlib 的测试诊断与 Lead 接管。10 次正向层内团队运行的生产来源为：4 次实现者改动直接保留、4 次 Lead 接管、2 次共同补修；只涉及三道题，不是因果贡献百分比。详见 [具体贡献拆解](../component-attribution/REPORT_ZH.md)。\n")
def refine(s,source,html_mode=False):
 if source.parent==SPECS['full-history'] and source.name=='REPORT_ZH.md':
  s=s.replace('测试者独立贡献、review 正确率、自主组队和动态路由仍未识别。','测试者平均独立净贡献、review 正确率、自主组队和动态路由仍未识别。')
  insertion=ADD.replace('../component-attribution/REPORT_ZH.md',REPO_URL+'reports/2026-09-07/component-attribution/REPORT_ZH.md') if html_mode else ADD
  # The first narrative block is amended; every original section stays in order.
  if '## 1. 全历史逐机制综合后的判断' in s:
   marker='这些判断来自逐机制筛查全部历史记录，而非把最新阶段当成全系统结论。'
   s=s.replace(marker,marker+insertion,1)
 return s
for source,dest in mapping.items():
 if source.suffix=='.png':dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(source.read_bytes())
 else:
  content=source.read_text('utf-8-sig')
  if source.suffix=='.json':content=json.dumps(walk(json.loads(content)),ensure_ascii=False,indent=2)+'\n'
  else:content=normalize(content)
  if source.suffix=='.md':
   content=links(content,source,dest)
   content=refine(content,source).rstrip()+"\n"
  write(dest,content)
 records.append({'source':source.relative_to(R).as_posix(),'source_sha256':source_digest[source],'published':dest.relative_to(R).as_posix(),'published_sha256':sha(dest),'original_validation_receipt':source.name in RECEIPTS})
for name,root in SPECS.items():
 artifact=walk(json.loads((root/'portable/artifact.json').read_text('utf-8-sig')))
 for block in artifact['manifest']['blocks']:
  if block.get('type')=='markdown':block['body']=refine(links(block['body'],root/'REPORT_ZH.md',OUT/name/'REPORT_ZH.md',True),root/'REPORT_ZH.md',True)
 artifact['manifest']['description']+=' GitHub 发布快照；原始实验日志为单独的本地归档。'
 ap=OUT/name/'portable/artifact.json';dump(ap,artifact);hp=OUT/name/'REPORT_ZH.html'
 script=f"""import fs from 'node:fs';
import {{buildPortableArtifact}} from '{(NATIVE_SCRIPT/'build_portable_artifact.mjs').as_uri()}';
import {{verifyPortableArtifactStructure}} from '{(NATIVE_SCRIPT/'verify_portable_artifact.mjs').as_uri()}';
const [a,h]=process.argv.slice(1);fs.writeFileSync(h,buildPortableArtifact(JSON.parse(fs.readFileSync(a,'utf8'))));console.log(JSON.stringify(verifyPortableArtifactStructure({{artifactPath:a,htmlPath:h}})));"""
 result=subprocess.run([str(NODE),'--input-type=module','-e',script,str(ap),str(hp)],capture_output=True,text=True,encoding='utf-8',timeout=60,check=True)
 structural=json.loads(result.stdout.splitlines()[-1]);assert structural['ok']
 dump(OUT/name/'PUBLICATION_RENDER.json',{'canonical_structure':structural,'html_sha256':sha(hp),'artifact_sha256':sha(ap),'source_md_sha256':sha(OUT/name/'REPORT_ZH.md'),'scope':'publication copy; original report validation is retained separately','canonical_browser':'not certified; separate real-time browser check is recorded for this publication'})
 records.append({'source':(root/'REPORT_ZH.html').relative_to(R).as_posix(),'source_sha256':source_digest[root/'REPORT_ZH.html'],'published':hp.relative_to(R).as_posix(),'published_sha256':sha(hp),'transformation':'same canonical report reader; updated report links and explicitly identified attribution note'})
 records.append({'source':(root/'portable/artifact.json').relative_to(R).as_posix(),'source_sha256':source_digest[root/'portable/artifact.json'],'published':ap.relative_to(R).as_posix(),'published_sha256':sha(ap)})
 assert len(re.findall(r'^## ',(OUT/name/'REPORT_ZH.md').read_text('utf-8'),re.M))==len(re.findall(r'^## ',(root/'REPORT_ZH.md').read_text('utf-8-sig'),re.M))
# Include historical plan copies in the same publication manifest.
for label, relative in [('B1', 'modelbench/big_budget_team_20260906/PLAN_ZH.md'), ('C1', 'modelbench/mechanism_ablation_20260906/PLAN_ZH.md')]:
 source=R/relative; source_digest[source]=sha(source)
 dest=OUT/'plans'/f'{label}_PLAN_ZH.md'
 content=normalize(source.read_text('utf-8-sig'))
 content=LINK.sub(lambda m: m.group(0) if re.match(r'^(https?://|mailto:|#)', m.group(2).strip('<>')) else m.group(1)+'（原计划的本地归档引用）', content)
 title, separator, rest=content.partition('\n')
 content=title+'\n\n> 历史计划发布副本。计划格数不等于执行数量，实际完成范围和结果见[报告索引](../../README.md)。本文不构成新的实验启动安排。\n'+rest
 write(dest,content)
 records.append({'source':relative,'source_sha256':source_digest[source],'published':dest.relative_to(R).as_posix(),'published_sha256':sha(dest),'transformation':'historical plan copy; machine paths normalized and archive-only links labeled'})
assert all(sha(p)==v for p,v in source_digest.items())
# Records retain original hashes; they do not claim the normalized CSV byte hashes are original.
dump(OUT/'PUBLICATION_MANIFEST.json',{'date':'2026-09-07','source_report_policy':'Original analyses are immutable; this publication updates navigation, strips machine-specific path prefixes, normalizes text line endings to LF, and adds the already-validated attribution interpretation to the cumulative report. Numerical observations and existing report sections are preserved.','source_files_unchanged':True,'records':records,'local_archive_references':omitted,'source_validation_policy':'DATA_VALIDATION and REPORT_VALIDATION remain original-analysis receipts, not publication certification. PUBLICATION_VALIDATION records checks of these copies.'})
print(json.dumps({'published_files':len(records),'bytes':sum((R/r['published']).stat().st_size for r in records),'original_sources_unchanged':True,'local_archive_links_marked':len(omitted)},ensure_ascii=False))
