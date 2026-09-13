// The model sees bounded protocol data; immutable file bodies stay in candidate views.
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value))
const excerpt = (value, limit = 500) => typeof value === 'string' && value.length > limit
  ? { excerpt: value.slice(0, limit), original_chars: value.length, truncated: true } : value
const pick = (value, keys) => value && Object.fromEntries(keys.filter(k=>value[k]!==undefined).map(k=>[k,clone(value[k])]))

export function candidateView(candidate) {
  if (!candidate) return candidate
  const result = pick(candidate, ['id','candidate_id','generation','manifest_digest','requirement_revision',
    'candidate_item_ids','roster_evidence','verification_roster','view_path','status','created_at'])
  const snapshot = candidate.snapshot || candidate
  result.snapshot = pick(snapshot,['schema','view_path','entry_paths','dependency_complete','unknown_dependencies','consumed_manifest_refs'])
  const files = snapshot.candidate_files || candidate.candidate_files || []
  result.candidate_files = files.map(file=>pick(file,['path','sha256','size','operation']))
  result.contents_omitted = true
  result.content_source = 'Read files in the immutable candidate view; use dpswarm_acceptance with a JSON pointer and paging for metadata. File bytes are never evidence of acceptance.'
  return result
}

export function acceptanceView(value, { detail = false } = {}) {
  if (!value) return value
  // Omit an absent candidate instead of writing an explicit `undefined`:
  // the host rejects tool results that are not lossless JSON (live 88122af1
  // shape: a contract exists but no candidate was ever captured).
  const result = { ...value, ...(value.candidate ? { candidate: candidateView(value.candidate) } : {}) }
  if (value.review) {
    result.review = pick(value.review,['id','review_id','candidate_id','reviewer_id','evidence_revision','requirement_revision','verdict','status','at'])
    result.review.full_review_pointer = '/review'
  }
  if (!detail) {
    for (const name of ['requirements','findings','evidence']) if (Array.isArray(value[name])) {
      result[name] = value[name].slice(-12).map(row => {
        const brief = pick(row,['id','finding_id','evidence_id','candidate_id','roster_id','requirement_id','status','mandatory','classification','resolution','severity','parse_error'])
        if (row.description) brief.description = excerpt(row.description,240)
        return brief
      })
      result[name+'_total'] = value[name].length
      result[name+'_omitted'] = Math.max(0,value[name].length-12)
    }
    delete result.review_format
    result.review_format_source = 'dpswarm_acceptance: read the current complete schema and template before reviewing. For a large member use pointer, offset and limit; summaries are not a pass.'
  }
  return result
}

/** Selective projection preserves machine-readable flags and never slices serialized JSON. */
export function modelToolView(value) {
  if (!value || typeof value !== 'object') return value
  const result = clone(value)
  if (typeof result.text === 'string' && Number.isSafeInteger(result.total_chars) && result.report_status) {
    let length = result.text.length
    while (Buffer.byteLength(JSON.stringify(result.text.slice(0,length)), 'utf8') > 20000) length = Math.floor(length * 0.8)
    if (length && /[\uD800-\uDBFF]/.test(result.text[length-1])) length--
    result.text = result.text.slice(0,length)
    result.returned_chars = length
    result.truncated = result.offset + length < result.total_chars
    result.next_offset = result.truncated ? result.offset + length : null
  }
  if (result.acceptance) result.acceptance = acceptanceView(result.acceptance)
  if (result.candidate?.snapshot) result.candidate = candidateView(result.candidate)
  // repair_report used to return raw delegate diagnostics unlike run/rework.
  for (const key of ['deliveries','failed','worker_diagnostics']) if (Array.isArray(result[key])) {
    result[key] = result[key].map(row => {
      if (typeof row.output === 'string' && row.output.length > 1000) {
        row.output_original_chars = row.output.length;row.output = row.output.slice(0,1000);row.output_truncated=true
        row.detail_source = 'dpswarm_report(item_id, offset, limit)'
      }
      const d=row.diagnostic
      if (d?.closeout) for (const name of ['report','progress']) {
        const report=d.closeout[name]
        if (report?.text?.length>1000) {
          report.original_chars=report.text.length;report.text=report.text.slice(0,1000);report.truncated=true
          report.detail_source='dpswarm_report(item_id, offset, limit)'
        }
      }
      if (d?.budget?.recent) delete d.budget.recent
      if (row.acceptance) row.acceptance=acceptanceView(row.acceptance)
      return row
    })
  }
  return result
}

const own=(value,key)=>Object.prototype.hasOwnProperty.call(value,key)
const escape=key=>String(key).replaceAll('~','~0').replaceAll('/','~1')
const fail=(code,message)=>Object.assign(new Error(`${code}: ${message}`),{code})
export function acceptancePage(value, { pointer, offset=0, limit=12, expected_revision } = {}) {
  if (expected_revision !== undefined && expected_revision !== value.revision) throw fail('ACCEPTANCE_PAGE_CHANGED','Contract revision changed between pages; re-read the current summary before combining evidence.')
  // No file contents in the metadata tool, even when selecting the snapshot itself.
  const data={...clone(value),candidate:candidateView(value.candidate)}
  if (pointer===undefined && Buffer.byteLength(JSON.stringify(acceptanceView(data,{detail:true})),'utf8')<=26000) return acceptanceView(data,{detail:true})
  pointer ??= ''
  if (typeof pointer!=='string' || (pointer && !pointer.startsWith('/')) || /~(?:[^01]|$)/.test(pointer)
    || !Number.isSafeInteger(offset) || offset<0 || !Number.isSafeInteger(limit) || limit<1 || limit>40) throw fail('ACCEPTANCE_PAGE_INVALID','Use a JSON pointer, nonnegative offset and limit 1–40.')
  let selected=data
  for(const key of pointer ? pointer.slice(1).split('/').map(x=>x.replaceAll('~1','/').replaceAll('~0','~')) : []) {
    if(!selected || typeof selected!=='object' || !own(selected,key)) throw fail('ACCEPTANCE_POINTER_MISSING','No member at this pointer; inspect the parent page.')
    selected=selected[key]
  }
  const context=pick(data,['available','contract_id','revision','evidence_revision','review_authority','report_error'])
  if(selected===null || typeof selected!=='object') {
    const text=typeof selected==='string'?selected:null
    return {...context,pointer,value:text===null?selected:text.slice(offset,offset+8000),offset,
      next_offset:text!==null && offset+8000<text.length?offset+8000:null}
  }
  const keys=Object.keys(selected),entries=[]
  for(const key of keys.slice(offset,offset+limit)) {
    const item=selected[key],serialized=JSON.stringify(item),complete=serialized.length<=1200
    if (key.length>2000) throw fail('ACCEPTANCE_KEY_TOO_LONG','Metadata key is too long for a bounded page.')
    const entry={key,pointer:pointer+'/'+escape(key),complete,...(complete?{value:item}:{type:Array.isArray(item)?'array':typeof item,
      size:typeof item==='object'&&item!==null?Object.keys(item).length:serialized.length})}
    if(entries.length && Buffer.byteLength(JSON.stringify([...entries,entry]),'utf8')>16000) break
    entries.push(entry)
  }
  return {...context,pointer,entries,total:keys.length,offset,next_offset:offset+entries.length<keys.length?offset+entries.length:null,
    next:'Read incomplete entries by pointer; use next_offset to continue. Review only after reading the required schema, template and all current requirements/findings. A page is not the full contract.'}
}
