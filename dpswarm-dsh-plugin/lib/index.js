import { Sidecar } from './sidecar.js'
import { pathToFileURL } from 'node:url'
import { runSubagentToCompletion } from './subagent-run.js'

// 宿主模块解析（零依赖插件）：所有 @deepseek-ai 模块从 dsh 安装内部取同一实例。
// file: 安装若给插件配上依赖副本，Symbol/模块单例会分叉，实测会破坏宿主的
// 工具调度（registry[TOOL_RUNTIME_SCHEDULER] undefined）——副本即事故。
const HOST_ROOT = (process.env.DSH_HOST_ROOT
  || 'C:/Users/93711/AppData/Roaming/npm/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai')
const hostImport = (rel) => import(pathToFileURL(HOST_ROOT + '/' + rel).href)
const [toolsMod, settingsMod, zMod] = await Promise.all([
  hostImport('dsh-tools/lib/index.js'),
  hostImport('dsh-settings/lib/index.js'),
  hostImport('schemastery/lib/index.cjs'),
])
const { defineTool } = toolsMod
const installSettingsSection = settingsMod.installSettingsSection
const z = zMod.default ?? zMod

export const name = 'dpswarm-dsh-plugin'
export const inject = ['tools', 'subagents', 'settings', 'systemPrompt']

export const DPSWARM_NS = 'dpswarm'

export const Config = z.object({
  sidecarUrl: z.string().default('http://127.0.0.1:8791'),
  autoStart: z.boolean().default(true),
  dpswarmDir: z.string().default(''),
  pythonCmd: z.string().default('python'),
  subagentProvider: z.string().default('spawn'),
})

export function requireRootCaller(parent) {
        if (!parent?.session?.header || typeof parent.session.id !== 'string'
            || parent.id !== parent.session.id) {
          throw new Error('PARENT_IDENTITY_REQUIRED: trusted DSH session header is required')
        }
        if (!['provider', 'model'].every(key => typeof parent.options?.[key] === 'string' && parent.options[key].trim())) {
          throw new Error('ROOT_MODEL_REQUIRED: trusted DSH parent requested provider/model must be explicit')
        }
        const depths = [parent.session.header.delegationDepth ?? 0, parent.options?.subagentDepth ?? 0]
        if (depths.some(depth => !Number.isSafeInteger(depth) || depth < 0 || depth > 0)) {
          throw new Error('NESTED_DELEGATION_UNSUPPORTED: DPSwarm bridge currently supports root callers only')
        }
}

export function apply(ctx, config) {
  // 机制一（§3）+ 设置卡片的 Host 半：注册 settings namespace。
  // 卡片编辑 sidecar 连接参数；DPSwarm 运行时参数在控制面 Spec（revision）里。
  let source = () => config
  const schema = z.object({
    sidecarUrl: z.string().default(config.sidecarUrl),
    subagentProvider: z.string().default(config.subagentProvider),
  })
  const entry = { sidecarUrl: config.sidecarUrl, subagentProvider: config.subagentProvider }
  const hooks = { setSource: (current) => { source = current }, onChange: () => {} }
  // Freeze one settings snapshot per tool call; an in-flight worker must finish
  // against the same sidecar that admitted it even if settings change meanwhile.
  const sidecarForCall = () => new Sidecar({ ...config, ...source() })
  // npm 版：独立函数（自管 inject）；repo 源码版：settingsCtx.settings.installSection
  try {
    if (process.env.DPSWARM_SKIP_SETTINGS === '1') {
      // diagnostic bypass
    } else if (typeof installSettingsSection === 'function') {
      installSettingsSection(ctx, DPSWARM_NS, schema, entry, hooks)
    } else {
      ctx.inject(['settings'], (settingsCtx) => {
        settingsCtx.settings.installSection(ctx, DPSWARM_NS, schema, entry, hooks)
      })
    }
  } catch (e) { console.error('dpswarm: settings section install failed: ' + e.message) }

  if (process.env.DPSWARM_SKIP_TOOLS === '1') return
  ctx.inject(['tools', 'subagents'], (runtimeCtx) => {
    // 机制一注入位置之一（§3：persona/系统提示层）：Lead 的委派决策参考——
    // 经济性边界（§6）+ 拓扑语义与验收纪律（§4/§7）。不过度指挥：只给事实与约束。
    try {
      runtimeCtx.systemPrompt.section({
        name: 'dpswm:guide',
        order: 2500,  // npm 版无 getSectionOrder：固定排在 subagent 段（~2400）之后
        text: () => [
          '## DPSwarm 按需协作能力（dpswarm_* 工具）',
          '你是当前主 agent，默认单干。理解任务后或执行途中，根据任务可分性、上下文交接成本和资源事实，自主判断是否调用 DPSwarm；插件可用不代表必须委派。',
          '在已有任务授权与运行边界内，无需用户另说“开启多 agent”或点击团队开关。需要协作时由你兼任 Lead，按需分工与验收，收回结果后继续原任务；用户明确指定的模型、协作方式和限制始终优先。',
          '当前桥接仅支持顶层 DSH agent 委派，子 agent 内不得再次调用 dpswarm_delegate。',
          '需要委派时按以下纪律使用 DPSwarm：',
          '1. 委派前先调 dpswarm_models 拿事实（模型级别/AA 分维/价格/当前槽位与点数容量），据此选精确 provider/model；不要凭记忆猜路由。', 
          '2. dpswarm_delegate：derive=单子任务外派；fission=多 worker 团队（需 S 级）；split=1主1副同构。硬准入（级别方向/深度/槽位/点数）不过会返回结构化原因——改路由或等容量，不得绕过。', 
          '3. 经济性边界：只有当子任务的上下文需求能被一段简短描述覆盖时，委派才划算；读到刚好够判断就决策，不要读完材料再决定。', 
          '4. 每份交付必须 dpswarm_review 裁决：accept=证据落盘发布；reject 需给归因（capability=换模型升级，上限=你的级别；context=修料；description=修述；contradiction=任务矛盾，不硬磕直接上交）。reject 后 item 停在 rejected：用 dpswarm_delegate(item_id=…, subtask=…修正后的路由与提示) 重跑——重试预算（首跑+≤2 次）在重跑时消费，耗尽自动上交；确定放弃用 verdict=terminate（释放槽位与点数）。', 
          '5. dpswarm_status 可随时查团队状态与观测账。', 
        ].join(String.fromCharCode(10)),
      })
    } catch (e) { console.error('dpswarm: prompt section failed: ' + e.message) }

    // ── 机制一：事实注入 ──────────────────────────────────────────────
        runtimeCtx.tools.register(defineTool({
      name: 'dpswarm_models',
      description:
        'DPswarm 模型事实（代码生成，非猜测）：可用模型目录、S/A/B/C/D 级别、'
        + 'AA 分维评分（按任务类型匹配）、上下文窗口、价格，以及当前 root 的'
        + ' worker 槽位与节点点数占用。委派前调用，在有约束的空间里选路由（§2/§3）。',
      parameters: {
        task_type: { type: 'string', description: "任务类型（coding/reasoning/search/overall），按它取 AA 分维" },
      },
      output: {
          schema: { type: 'object', additionalProperties: true },
          render: (_args, value) => [{ type: 'text', text: String.fromCharCode(96,96,96) + 'json' + String.fromCharCode(10) + JSON.stringify(value, null, 2) + String.fromCharCode(10) + String.fromCharCode(96,96,96) }],
        },
      async execute() {
        const sidecar = sidecarForCall()
        await sidecar.ensure()
        const s = await sidecar.call('GET', '/api/status')
        const spec = s.spec
        const aaMeta = s.aa_snapshot
          ? `（AA 快照 ${s.aa_snapshot.snapshot_date}，${s.aa_snapshot.models} 条）`
          : ''
        const lines = [
          `root 容量：worker 槽 ${s.snapshot.open_worker_slots_used}/${spec.max_open_work_items}，`
          + `点数 ${s.snapshot.active_points}/${spec.max_active_node_points}，`
          + `深度 ≤${spec.max_depth} 层，裂变 ≤${spec.max_team_workers}，attempt ≤${spec.max_attempts}`,
          `模型目录（级别 | AA 分维${aaMeta}；src=aa@日期=外部快照，declared=声明值，demo=演示）：`,
        ]
        for (const f of s.catalog) {
          lines.push(`- ${f.provider}/${f.model} [${f.level}] AA=${JSON.stringify(f.aa)}`
            + ` ctx=${f.ctx} $${f.in}/${f.out} src=${f.src || 'declared'}`)
        }
        return { facts: lines.join('\n'), spec_revision: spec.revision }
      },
    }))

    // ── 机制五：拓扑委派（硬准入在 sidecar，执行在 dsh subagent）────────
    runtimeCtx.tools.register(defineTool({
      name: 'dpswarm_delegate',
      description:
        '当前主 agent 判断协作有益时自主调用的委派能力；默认单干，无需用户开启团队模式。'
        + 'DPswarm 拓扑委派（§7）：derive（单子）/ fission（多 worker 团队，仅 S 级 Lead）'
        + '/ split（1 主 1 副同构，协助者真实执行、回报经 peer 通道入账）。先调 dpswarm_models'
        + ' 选路由；控制面硬准入（级别方向/深度/槽位/点数）不过会返回结构化原因。'
        + 'worker 是 dsh subagent，完成后自动 submit，交付文本返回给你审查（用 dpswarm_review 裁决）。'
        + 'reject 后重跑既有 item：传 item_id + subtask（单条，不填 kind）——重试预算在此消费，'
        + '耗尽自动上交（escalated）。subtask 可带 deps:[0基下标]（§7 DAG），依赖未就绪的 item'
        + ' 不启动、在返回值 pending 里列出。',
      parameters: {
        kind: { type: 'string', description: 'derive | fission | split（新建模式必填；item_id 重跑模式省略）' },
        subtasks: {
          type: 'array',
          description: '新建模式子任务列表：[{title, prompt, provider, model, reasoning_effort, deps: [0基下标数组]}]；split 恰 1 条；超 max_team_workers 结构化拒绝',
          items: { type: 'object', additionalProperties: true },
        },
        item_id: { type: 'string', description: '重跑模式：reject 后的既有 item_id（与 subtask 单条搭配，不填 kind）' },
        subtask: { type: 'object', additionalProperties: true, description: '重跑模式单条子任务 {title, prompt, provider, model, reasoning_effort}；capability 归因换模在此给新路由' },
      },
      output: {
          schema: { type: 'object', additionalProperties: true },
          render: (_args, value) => [{ type: 'text', text: String.fromCharCode(96,96,96) + 'json' + String.fromCharCode(10) + JSON.stringify(value, null, 2) + String.fromCharCode(10) + String.fromCharCode(96,96,96) }],
      },
      async execute(args, exec) {
        const parent = exec.agent
        requireRootCaller(parent)
        const sidecar = sidecarForCall()
        await sidecar.ensure()
        await sidecar.call('POST', '/api/execution/root', {
          parent_session_id: parent.session.id, delegation_depth: 0,
          provider: parent.options.provider, model: parent.options.model,
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
          const unknownUsage = { input_tokens: null, output_tokens: null,
            cache_read_tokens: null, cache_write_tokens: null, cost_usd: null }
          const published = async run => {
            currentRun = run.id
            boundFence = await sidecar.call('POST', '/api/execution/bind', {
              item_id: it.item_id, node_id: currentNode, attempt: reservation.attempt,
              context_epoch: reservation.context_epoch, reservation_session_id: reservation.session_id,
              execution_session_id: run.id, parent_session_id: parent.session.id,
              execution_provider: sidecar.cfg.subagentProvider,
            })
          }
          try {
          const agentOptions = {}
          if (st.provider) agentOptions.provider = st.provider
          if (st.model) agentOptions.model = st.model
          if (st.reasoning_effort) agentOptions.reasoningEffort = st.reasoning_effort
          const opts = Object.keys(agentOptions).length > 0 ? { agentOptions } : {}
          const basePrompt = st.prompt ?? st.title ?? String(it.item_id)
          let prompt = basePrompt
          // split：协助者是独立执行 session（§7）——先跑协助者（写范围后半），
          // 回报经 peer 通道入账（§9.5，消息账本即 evidence），主执行者再开工。
          if (it.assistant_node_id && it.channel_id) {
            currentNode = it.assistant_node_id
            reservation = it.assistant_fence
            if (!reservation) throw new Error('ASSISTANT_FENCE_REQUIRED: sidecar contract is outdated')
            const assistOut = await runSubagentToCompletion(runtimeCtx.subagents, sidecar.cfg.subagentProvider, {
              label: `dpswarm:assist:${st.title ?? it.item_id}`,
              prompt: [{ type: 'text', text: '协助分工（写范围后半）：' + basePrompt }],
              parent,
              signal: exec.signal,
              ...opts,
            }, { onPublished: published })
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
          const out = await runSubagentToCompletion(runtimeCtx.subagents, sidecar.cfg.subagentProvider, {
            label: `dpswarm:${st.title ?? it.item_id}`,
            prompt: [{ type: 'text', text: prompt }],
            parent,
            signal: exec.signal,
            ...opts,
          }, { onPublished: published })
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
          }
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
            error: String(s.reason?.message ?? s.reason).slice(0, 300),
            details: s.reason?.details ?? {}, control_settlement: s.reason?.controlSettlement,
          })
        })
        if (failed.length > 0) {
          // Failure settlement is explicit and fenced; failed HTTP settlement
          // remains visible to the Lead rather than claiming resource release.
          const reasons = failed.map(f => `${f.item_id}: ${f.error}`).join('；')
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
      },
    }))

    // ── 机制二：验收制（通过=成功 / 打回=失败 + 归因四分支）────────────
    runtimeCtx.tools.register(defineTool({
      name: 'dpswarm_review',
      description:
        'DPswarm Lead 验收（§4/§8）：accept = 引用 submit 已落盘的证据包并原子发布'
        + '（解锁后继、释放槽与点数；证据不可替换，不接受另行提供正文）；reject = 打回'
        + '（归因 capability/context/description/contradiction）——item 停在 rejected 等待重跑：'
        + '用 dpswarm_delegate(item_id=…, subtask=…) 重跑（≤max_attempts，重试预算在重跑时消费，'
        + '预算耗尽自动上交 escalate）；contradiction 归因直接上交；terminate = 放弃该 item'
        + '（释放槽与点数）。',
      parameters: {
        item_id: { type: 'string', required: true },
        verdict: { type: 'string', required: true, description: 'accept | reject | terminate' },
        reason: { type: 'string', description: '裁决理由（打回理由 = 免费失败归因数据）' },
        attribution: { type: 'string', description: 'capability | context | description | contradiction（reject 必填语义）' },
        route: { type: 'object', additionalProperties: true, description: 'capability 归因的换模提议 {provider, model, reasoning_effort}——仅回显提示，重跑请求的 subtask 需自带该路由' },
      },
      output: {
          schema: { type: 'object', additionalProperties: true },
          render: (_args, value) => [{ type: 'text', text: String.fromCharCode(96,96,96) + 'json' + String.fromCharCode(10) + JSON.stringify(value, null, 2) + String.fromCharCode(10) + String.fromCharCode(96,96,96) }],
        },
      async execute(args, exec) {
        requireRootCaller(exec?.agent)
        const sidecar = sidecarForCall()
        await sidecar.ensure()
        await sidecar.call('POST', '/api/execution/root', {
          parent_session_id: exec.agent.session.id, delegation_depth: 0,
          provider: exec.agent.options.provider, model: exec.agent.options.model,
        })
        const r = await sidecar.call('POST', '/api/review', {
          item_id: args.item_id, verdict: args.verdict, reason: args.reason,
          attribution: args.attribution, // P1-3：accept 引用 submit 落盘证据，不再传正文
        })
        return r
      },
    }))

    // ── 状态与观测 ───────────────────────────────────────────────────
    runtimeCtx.tools.register(defineTool({
      name: 'dpswarm_status',
      description: 'DPswarm 运行状态：work items/nodes/teams/seal 相位 + 观测汇总'
        + '（终局分布、token 账、委派经济性）。',
      parameters: {},
      output: {
          schema: { type: 'object', additionalProperties: true },
          render: (_args, value) => [{ type: 'text', text: String.fromCharCode(96,96,96) + 'json' + String.fromCharCode(10) + JSON.stringify(value, null, 2) + String.fromCharCode(10) + String.fromCharCode(96,96,96) }],
        },
      async execute() {
        const sidecar = sidecarForCall()
        await sidecar.ensure()
        const [s, o] = await Promise.all([
          sidecar.call('GET', '/api/status'),
          sidecar.call('GET', '/api/observation'),
        ])
        return { snapshot: s.snapshot, spec: { revision: s.spec.revision }, observation: o }
      },
    }))

    ctx.effect(() => {
      // 连接体检（异步、不阻塞装载）：失败仅记日志，工具首次调用会再 ensure。
      const sidecar = sidecarForCall()
      void sidecar.probe().then(ok => {
        if (!ok) void sidecar.ensure().catch((e) =>
          ctx.logger?.warn?.(`dpswarm sidecar: ${e.message}`))
      })
    }, 'dpswarm: sidecar ensure')
  })
}
