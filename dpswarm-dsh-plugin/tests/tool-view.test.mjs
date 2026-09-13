import test from 'node:test'
import assert from 'node:assert/strict'
import {modelToolView,acceptancePage} from '../lib/tool-view.js'

const fixture=()=>({available:true,contract_id:'contract',revision:8,evidence_revision:4,
  candidate:{candidate_id:'c',manifest_digest:'d'.repeat(64),candidate_item_ids:['wi'],snapshot:{view_path:'/sealed/c',candidate_files:[{path:'a.html',sha256:'f'.repeat(64),size:100000,operation:'file',content_base64:'X'.repeat(150000)}]}},
  requirements:[{id:'r',description:'user requirement',mandatory:true}],findings:[{id:'f',status:'open',description:'real issue'}],
  review:{review_id:'rev',verdict:'blocked',report:'P'.repeat(80000)},review_format:{schema:{required:['candidate_id']},template:{verdict:'blocked'}}})

test('run, status and repair output do not repeat candidate bodies or huge reports',()=>{
  const full={ok:true,acceptance:fixture(),deliveries:[{item_id:'wi',output:'A'.repeat(40000),diagnostic:{closeout:{report_status:'final',report:{text:'B'.repeat(50000)}}}}]}
  const view=modelToolView(full)
  assert.ok(JSON.stringify(view).length<8000)
  assert.equal(view.acceptance.findings[0].status,'open');assert.equal(view.acceptance.review.verdict,'blocked')
  assert.equal(view.acceptance.candidate.manifest_digest,'d'.repeat(64))
  assert.equal(view.deliveries[0].output_truncated,true)
  assert.equal(full.acceptance.candidate.snapshot.candidate_files[0].content_base64.length,150000,'source untouched')
  assert.equal(view.acceptance.review_format,undefined)
})
test('acceptance schema remains readable and oversized metadata is paged as complete JSON',()=>{
  const full=fixture(),view=acceptancePage(full)
  assert.deepEqual(view.review_format,full.review_format)
  assert.equal(view.candidate.candidate_files[0].path,'a.html')
  const review=acceptancePage(full,{pointer:'/review'})
  assert.equal(review.entries.find(e=>e.key==='report').complete,false)
  const page=acceptancePage(full,{pointer:'/review/report',offset:8000})
  assert.equal(page.value.length,8000);assert.equal(page.next_offset,16000)
  full.requirements=Array.from({length:100},(_,i)=>({id:String(i),description:'R'.repeat(1000)}))
  assert.ok(acceptancePage(full).entries)
  const requirements=acceptancePage(full,{pointer:'/requirements',offset:12,limit:3})
  assert.equal(requirements.entries[0].value.id,'12');assert.equal(requirements.next_offset,15)
  assert.throws(()=>acceptancePage(full,{pointer:'/constructor'}),{code:'ACCEPTANCE_POINTER_MISSING'})
  assert.throws(()=>acceptancePage(full,{pointer:'/candidate/snapshot/candidate_files/0/content_base64'}),{code:'ACCEPTANCE_POINTER_MISSING'})
})

test('Chinese report pages fit byte transport limits and resume without losing text',()=>{
  const report='证据并非验收\n'.repeat(9000)
  let offset=0, collected=''
  while(offset<report.length) {
    const page=modelToolView({item_id:'wi',report_status:'final',offset,limit:40000,total_chars:report.length,text:report.slice(offset,offset+40000)})
    assert.ok(Buffer.byteLength(JSON.stringify(page),'utf8')<24000)
    assert.ok(page.returned_chars>0)
    collected+=page.text;offset+=page.returned_chars
    assert.equal(page.next_offset,offset<report.length?offset:null)
  }
  assert.equal(collected,report)
})
