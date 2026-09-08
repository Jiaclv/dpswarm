import { AuditJournal } from './audit.js'
import { workerDiagnostics } from './worker-diagnostics.js'
import { runSubagentToCompletion } from './subagent-run.js'
import { effectiveLeadRoute, prepareChildRoute } from './lead-route.js'

export function requireRootCaller(parent) {
        if (!parent?.session?.header || typeof parent.session.id !== 'string'
            || parent.id !== parent.session.id) {
          throw new Error('PARENT_IDENTITY_REQUIRED: trusted DSH session header is required')
        }
        const depths = [parent.session.header.delegationDepth ?? 0, parent.options?.subagentDepth ?? 0]
        if (depths.some(depth => !Number.isSafeInteger(depth) || depth < 0 || depth > 0)) {
          throw new Error('NESTED_DELEGATION_UNSUPPORTED: DPSwarm bridge currently supports root callers only')
        }
}


// Internal adapter; dynamic topology is not exposed by the fixed-team plugin.
export async function delegateOnce(args, exec, sidecar, subagents, { routeJournal = new AuditJournal({ sidecarFactory: () => sidecar }), modelRegistry, modelRoutes, hostModels, modelRole, onChildStarted, resolveSession, budget, runId, onDiagnostic, beforeChildStart } = {}) {
        const parent = exec.agent
        requireRootCaller(parent)
        const leadRoute = effectiveLeadRoute(parent)
        // Revalidate the complete frozen role set; a removed provider or changed
        // default cannot dispatch a replacement under an old registration.
        const validated = modelRegistry ? await modelRegistry.resolve(modelRoutes, { signal: exec.signal, expected: hostModels }) : null
        if (validated) {
          modelRegistry.checkLead(effectiveLeadRoute(parent), modelRoutes)
          const child = validated.models.find(row => row.role === modelRole), task = args.subtasks?.[0]
          if (args.kind !== 'derive' || args.subtasks?.length !== 1 || !child || !task
              || child.provider !== task.provider || child.model !== task.model
              || (task.reasoning_effort !== undefined && child.reasoningEffort !== task.reasoning_effort)) {
            throw Object.assign(new Error('HOST_MODEL_ROUTE_DRIFT: child is outside the frozen fixed role'), { code: 'HOST_MODEL_ROUTE_DRIFT' })
          }
        }
        await sidecar.ensure()
        if (validated) await modelRegistry.publish(sidecar, validated)
        await sidecar.call('POST', '/api/execution/root', {
          parent_session_id: parent.session.id, delegation_depth: 0,
          provider: leadRoute.provider, model: leadRoute.model,
        })
        const rerun = Boolean(args.item_id)
        let admission
        if (rerun) {
          if (!args.subtask) {
            throw new Error('重跑模式需要 subtask（单条：title/prompt/provider/model/reasoning_effort）')
          }
          admission = await sidecar.call('POST', '/api/delegate', {
            item_id: args.item_id, subtask: args.subtask,
          })
        } else {
          if (!args.kind || !Array.isArray(args.subtasks) || args.subtasks.length === 0) {
            throw new Error('新建模式需要 kind + subtasks；reject 后重跑既有 item 用 item_id + subtask')
          }
          admission = await sidecar.call('POST', '/api/delegate', {
            kind: args.kind, subtasks: args.subtasks,
          })
        }
        // 重跑预算耗尽：控制面已自动上交（item 终态 escalated），无 items 可执行
        if (!admission.items) {
          return {
            admitted: 0, outcome: admission.outcome,
            message: admission.message ?? 'item 已处置（终态），无新执行者',
            next: admission.outcome === 'escalated'
              ? '重试预算耗尽已上交（§8）：由你（Lead）接管——自干或新建 work item'
              : '该 item 无需执行',
          }
        }
        // 机制五（§7）fission 是多 worker 团队——物理并行执行（allSettled：
        // 单个 worker 失败不中断其余，失败清单回报 Lead 裁决 review/重跑；
        // 节点已激活，失败后的 CP 状态仍须显式处置）。
        const runOne = async (it, st) => {
          let currentNode = it.node_id
          let reservation = it
          let currentRun = null
          let boundFence = null
          let physicalCleanupConfirmed = false
          let childRoute, nativeSession, evidenceError
          const unknownUsage = { input_tokens: null, output_tokens: null,
            cache_read_tokens: null, cache_write_tokens: null, cost_usd: null }
          const published = async run => {
            currentRun = run.id
            nativeSession = run.localAgent?.session
            if (!nativeSession && resolveSession) {
              try { nativeSession = await resolveSession(run.id) }
              catch (error) { evidenceError = String(error?.message ?? error) }
            }
            boundFence = await sidecar.call('POST', '/api/execution/bind', {
              item_id: it.item_id, node_id: currentNode, attempt: reservation.attempt,
              context_epoch: reservation.context_epoch, reservation_session_id: reservation.session_id,
              execution_session_id: run.id, parent_session_id: parent.session.id,
              execution_provider: sidecar.cfg.subagentProvider,
            })
            await childRoute.bind(run.id)
            await onChildStarted?.({ execution_session_id: run.id, item_id: it.item_id, node_id: currentNode })
          }
          const terminal = async info => {
            const diagnostic = await workerDiagnostics({ ...info,
              session: nativeSession || info.run.localAgent?.session, sessionId: info.run.id,
              rootId: parent.session.id, role: modelRole, budget, evidenceError })
            try {
              await routeJournal.append(parent.session.id, 'dpswarm/worker-diagnostic', {
                root_session_id: parent.session.id, owner_session_id: info.run.id,
                worker_session_id: info.run.id, role: modelRole || 'worker',
                item_id: it.item_id, ...(runId ? { run_id: runId } : {}), diagnostic,
              })
            } catch (error) {
              diagnostic.audit_error = { code: error?.code || 'WORKER_DIAGNOSTIC_AUDIT_FAILED',
                message: String(error?.message ?? error) }
              if (!diagnostic.failure) diagnostic.failure = { code: 'WORKER_DIAGNOSTIC_AUDIT_FAILED',
                category: 'unknown', source: 'plugin-audit', message: diagnostic.audit_error.message,
                raw_code: error?.code || null, raw_message: diagnostic.audit_error.message }
              diagnostic.closeout.completion = diagnostic.closeout.report_available || diagnostic.closeout.candidates.length ? 'partial' : 'failed'
            }
            await onDiagnostic?.({ item_id: it.item_id, role: modelRole || 'worker', execution_session_id: info.run.id, diagnostic })
            return diagnostic
          }
          try {
          const agentOptions = {}
          if (st.provider) agentOptions.provider = st.provider
          if (st.model) agentOptions.model = st.model
          const verifiedRoute = validated?.models.find(row => row.role === modelRole)
          if (validated && !verifiedRoute) throw Object.assign(new Error('HOST_MODEL_ROUTE_DRIFT: child is outside the frozen host profile'), { code: 'HOST_MODEL_ROUTE_DRIFT' })
          if (verifiedRoute?.reasoningEffort !== undefined) agentOptions.reasoningEffort = verifiedRoute.reasoningEffort
          if (st.reasoning_effort) agentOptions.reasoningEffort = st.reasoning_effort
          const routeOptions = { provider: st.provider || leadRoute.provider, model: st.model || leadRoute.model, ...agentOptions }
          const prepareRoute = label => {
            childRoute?.close()
            childRoute = prepareChildRoute(parent, routeOptions, { journal: routeJournal, label })
            return { agentOptions: childRoute.agentOptions }
          }
          const basePrompt = st.prompt ?? st.title ?? String(it.item_id)
          let prompt = basePrompt
          // split：协助者是独立执行 session（§7）——先跑协助者（写范围后半），
          // 回报经 peer 通道入账（§9.5，消息账本即 evidence），主执行者再开工。
          if (it.assistant_node_id && it.channel_id) {
            currentNode = it.assistant_node_id
            reservation = it.assistant_fence
            if (!reservation) throw new Error('ASSISTANT_FENCE_REQUIRED: sidecar contract is outdated')
            const assistOut = await runSubagentToCompletion(subagents, sidecar.cfg.subagentProvider, {
              label: `dpswarm:assist:${st.title ?? it.item_id}`,
              prompt: [{ type: 'text', text: '协助分工（写范围后半）：' + basePrompt }],
              parent,
              signal: exec.signal,
              ...prepareRoute(`dpswarm:assist:${st.title ?? it.item_id}`),
            }, { onPublished: published, onTerminal: terminal })
            physicalCleanupConfirmed = true
            await sidecar.call('POST', '/api/peer', {
              channel_id: it.channel_id, from_node: it.assistant_node_id, body: assistOut.text,
              context_epoch: boundFence.context_epoch, session_id: boundFence.session_id, token_usage: unknownUsage,
            })
            prompt = basePrompt + '\n\n## 协助者回报（经 peer 通道）\n' + assistOut.text
          }
          currentNode = it.node_id
          reservation = it
          currentRun = null
          boundFence = null
          physicalCleanupConfirmed = false
          if (beforeChildStart) {
            try { await beforeChildStart(); exec.signal?.throwIfAborted() }
            catch (error) {
              // The trusted rework task check runs after all preceding awaits,
              // immediately before creating this child; there is none to drain.
              physicalCleanupConfirmed = true
              error.details = { ...error.details, published: false, physicalCleanupConfirmed: true }
              throw error
            }
          }
          const out = await runSubagentToCompletion(subagents, sidecar.cfg.subagentProvider, {
            label: `dpswarm:${st.title ?? it.item_id}`,
            prompt: [{ type: 'text', text: prompt }],
            parent,
            signal: exec.signal,
            ...prepareRoute(`dpswarm:${st.title ?? it.item_id}`),
          }, { onPublished: published, onTerminal: terminal })
          physicalCleanupConfirmed = true
          await sidecar.call('POST', '/api/submit', {
            item_id: it.item_id, node_id: it.node_id, output: out.text,
            stop_reason: out.stopReason,
            // P1-2 fence：delegate 返回的 epoch/session 原样回带（旧 session 拒写）
            context_epoch: boundFence.context_epoch, session_id: boundFence.session_id,
            token_usage: unknownUsage,
          })
          return {
            item_id: it.item_id, title: st.title, kind: it.kind,
            level: it.level, stop_reason: out.stopReason, output: out.text,
            execution_session_id: out.sessionId, token_usage: unknownUsage,
            diagnostic: out.diagnostic, budget: out.diagnostic?.budget || null,
            closeout: out.diagnostic?.closeout || null, failure: out.diagnostic?.failure || null,
          }
          } catch (error) {
            let settlement
            try {
              settlement = await sidecar.call('POST', '/api/execution/fail', {
                item_id: it.item_id, node_id: currentNode, attempt: reservation?.attempt,
                context_epoch: reservation?.context_epoch, reservation_session_id: reservation?.session_id,
                execution_session_id: currentRun, published: currentRun !== null || error?.details?.published === true,
                code: error?.code,
                error: String(error?.message ?? error), details: error?.details ?? {},
                stop_reason: error?.details?.stopReason ?? (exec.signal?.aborted ? 'aborted' : 'error'),
                physical_cleanup_confirmed: error?.details?.physicalCleanupConfirmed ?? physicalCleanupConfirmed,
              })
            } catch (settlementError) {
              settlement = { ok: false, error: String(settlementError?.message ?? settlementError) }
            }
            const failure = error instanceof Error ? error : new Error(String(error))
            failure.controlSettlement = settlement
            throw failure
          } finally { childRoute?.close() }
        }
        const settled = await Promise.allSettled(admission.items.map((it, i) => {
          const st = rerun ? args.subtask
            : (args.subtasks ?? [])[it.subtask_index ?? i] ?? {}
          return runOne(it, st)
        }))
        const deliveries = []
        const failed = []
        settled.forEach((s, i) => {
          const it = admission.items[i]
          if (s.status === 'fulfilled') deliveries.push(s.value)
          else failed.push({
            item_id: it.item_id,
            code: s.reason?.code ?? 'SUBAGENT_EXECUTION_FAILED',
            error: String(s.reason?.message ?? s.reason),
            execution_session_id: s.reason?.details?.sessionId || null,
            diagnostic: s.reason?.details?.worker_diagnostic || null,
            budget: s.reason?.details?.worker_diagnostic?.budget || null,
            closeout: s.reason?.details?.worker_diagnostic?.closeout || null,
            failure: s.reason?.details?.worker_diagnostic?.failure || null,
            details: s.reason?.details ?? {}, control_settlement: s.reason?.controlSettlement,
          })
        })
        if (failed.length > 0) {
          // Failure settlement is explicit and fenced; failed HTTP settlement
          // remains visible to the Lead rather than claiming resource release.
          const reasons = failed.map(f => `${f.item_id}: ${f.code}`).join('；')
          console.error('dpswarm: worker 失败（未提交）— ' + reasons)
        }
        const pending = admission.pending ?? []
        return {
          admitted: admission.items.length,
          deliveries,
          failed,
          pending,
          next: failed.length
            ? '执行失败详情见 failed；control_settlement.ok=true 表示已终止控制面任务并释放资源。'
              + '若结算失败，需处理 sidecar 连接并明确终止该 item。物理清理结果单独记录；不可把 dispose 失败当作子进程已退出。'
            : pending.length
              ? 'deps 未就绪的 item 列在 pending（waiting_on）；上游 accept 后用'
                + ' dpswarm_delegate(item_id=…, subtask=…) 启动。'
              : '逐项审查以上交付：dpswarm_review(item_id, verdict=accept|reject|terminate,'
                + ' attribution)；reject 后用 dpswarm_delegate(item_id=…, subtask=…) 重跑。',
        }
}
