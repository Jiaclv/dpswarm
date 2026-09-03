/**
 * DPSwarm dsh 插件（浏览器半）——factory bundle（手写，免构建）。
 *
 * 工件契约：调用 window.__ModuleLoader__.load({id, factory})，factory(require)
 * 的返回值即模块导出（apply/inject）；react 等白名单模块经注入 require 解析。
 *
 * 注册：
 * - settings.plugin.item 卡片（key: dpswarm）—— Plugins 设置页内的 DPSwarm 卡
 * - conversation.input.left —— composer 工具行内的 🧭 启动入口（"Workspace Write"
 *   模式选择器与模型选择器之间的座位），带三态状态灯
 *
 * 状态灯语义（黄/绿/红 = 启动中/已连接/离线）：
 * - 黄：首次探测完成前（dsh 刚起、autoStart 正在拉起 sidecar 的窗口）；
 *   连续 3 次探测失败（≈12s）仍从未连上才转红——覆盖 Python sidecar 冷启动耗时
 * - 绿：/api/status 探测成功
 * - 红：曾连上后探测失败（sidecar 掉线），或冷启动窗口耗尽仍不可达；再探成功回绿
 *
 * 视觉：对齐 dsh Web UI。卡片 chrome 逐条镜像 ui-settings-plugins 的
 * PluginCard.module.css（边框/圆角/字号/间距/徽标），弹层镜像 ui-input-trigger
 * 的 MenuView.module.css（菜单面/阴影）；全部颜色走平台 --dsw-* 主题 token
 * （黄 = --dsw-alias-state-warn-primary），暗亮主题自动适配，无一处硬编码色值。
 * 样式经一次性 <style> 注入（无构建管线，不能 import css module）；图标优先取
 * ui-primitives（require 失败则用同形内联 SVG 兜底）。整卡防御式渲染：
 * ErrorBoundary + 数据形状全兜底，任何 props / sidecar 响应异常都不抛错。
 */
window.__ModuleLoader__.load({
  id: 'dpswarm-dsh-plugin',
  factory: (require) => {
    const React = require('react')
    const { useEffect, useState, useRef, useCallback } = React
    const h = React.createElement

    const PANEL_URL = 'http://127.0.0.1:8791'
    const NS = 'settings.dpswarm.card'
    const STYLE_ID = 'dpswarm-dsh-plugin-style'

    const DICT = {
      zh: {
        name: 'DPSwarm',
        description: '为当前 agent 提供按需多模型协作能力：默认单独工作，需要时由 agent 自主分工并验收。',
        ok: '已连接', wait: '启动中', bad: '未连接', open: '打开控制面板',
        slots: 'worker 槽', points: '点数', items: 'items', nodes: 'nodes', rev: 'Spec rev',
        offline: '未连接控制面。启动 sidecar：cd dpswarm-plugin && python -m dpswarm.server',
        hintStarting: '控制面启动中——dsh 装载时自动拉起 sidecar，数秒内连接。',
        hint: '正常向 agent 描述任务即可，无需开启团队模式。'
          + '此面板用于查看协作状态和配置运行边界。',
        launch: 'DPSwarm 控制面板',
        launchOk: 'DPSwarm 控制面板（已连接）',
        launchWait: 'DPSwarm 控制面板（启动中…）',
        launchBad: 'DPSwarm 控制面板（未连接）',
      },
      en: {
        name: 'DPSwarm',
        description: 'On-demand multi-model collaboration for your current agent. '
          + 'It works alone by default and delegates and reviews work when useful.',
        ok: 'online', wait: 'starting', bad: 'offline', open: 'Open panel',
        slots: 'worker slots', points: 'points', items: 'items', nodes: 'nodes', rev: 'Spec rev',
        offline: 'Control plane offline. Start the sidecar: '
          + 'cd dpswarm-plugin && python -m dpswarm.server',
        hintStarting: 'Control plane starting — the sidecar is auto-launched with dsh; '
          + 'connects within seconds.',
        hint: 'Describe your task normally; the agent decides when collaboration helps. '
          + 'This panel shows collaboration status and configures runtime limits.',
        launch: 'DPSwarm control panel',
        launchOk: 'DPSwarm control panel (online)',
        launchWait: 'DPSwarm control panel (starting…)',
        launchBad: 'DPSwarm control panel (offline)',
      },
    }
    const L = (() => {
      try { return String(navigator.language || 'zh').toLowerCase().startsWith('zh') ? DICT.zh : DICT.en }
      catch (e) { return DICT.zh }
    })()

    // ── 主题 token 样式（一次性注入；全部 var(--dsw-*)，无硬编码色值）──────
    const CSS = `
/* 卡片 chrome：镜像 ui-settings-plugins/PluginCard.module.css */
.dps-card{list-style:none;border:1px solid var(--dsw-alias-border-l2);border-radius:12px;
  background:var(--dsw-alias-bg-layer-3);transition:border-color .16s,background .16s}
.dps-card:hover{border-color:var(--dsw-alias-label-dimmed)}
.dps-cardOpen{background:var(--dsw-alias-bg-layer-2);border-color:var(--dsw-alias-label-dimmed)}
.dps-header{width:100%;appearance:none;border:0;background:none;font:inherit;color:inherit;
  text-align:left;cursor:pointer;display:flex;align-items:center;gap:12px;
  padding:14px 16px;border-radius:12px}
.dps-header:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:-2px}
.dps-headText{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}
.dps-name{font-size:15px;font-weight:600;line-height:1.4;color:var(--dsw-alias-label-primary)}
.dps-description{font-size:13px;line-height:1.5;color:var(--dsw-alias-label-tertiary)}
.dps-chevron{flex:none;display:inline-flex;color:var(--dsw-alias-label-tertiary);transition:transform .16s}
.dps-chevronOpen{transform:rotate(180deg)}
/* 字段区：镜像 PluginCard .body + fields.module.css */
.dps-body{border-top:1px solid var(--dsw-alias-border-l2);margin:0 16px;padding-bottom:8px}
.dps-field{display:flex;flex-direction:column;gap:6px;padding:12px 0}
.dps-field + .dps-field{border-top:1px solid var(--dsw-alias-border-l2)}
/* 状态徽标：镜像 PluginCard .pending 徽标几何 */
.dps-badge{flex:none;display:inline-flex;align-items:center;gap:6px;border-radius:999px;
  padding:1px 8px;font-size:11px;line-height:17px;font-weight:500;white-space:nowrap;
  background:var(--dsw-alias-bg-module-platform);color:var(--dsw-alias-label-secondary)}
.dps-dot{flex:none;width:6px;height:6px;border-radius:50%;background:var(--dsw-alias-label-tertiary)}
.dps-dotOk{background:var(--dsw-alias-state-success-primary)}
.dps-dotWait{background:var(--dsw-alias-state-warn-primary)}
.dps-dotBad{background:var(--dsw-alias-state-error-primary)}
/* 快照 chips：镜像 ui-primitives Pill 几何 + 卡内徽标底色 */
.dps-chips{display:flex;flex-wrap:wrap;gap:8px}
.dps-chip{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 8px;
  border-radius:12px;font-size:12px;line-height:18px;white-space:nowrap;
  background:var(--dsw-alias-bg-module-platform);color:var(--dsw-alias-label-tertiary)}
.dps-chip b{font-weight:500;color:var(--dsw-alias-label-primary);font-variant-numeric:tabular-nums}
/* 提示行：镜像 fields.module.css .hint */
.dps-hint{margin:0;font-size:12px;line-height:1.5;color:var(--dsw-alias-label-tertiary)}
/* 操作行：镜像 PluginCard .footer + .save */
.dps-footer{display:flex;align-items:center;justify-content:flex-end;gap:8px;
  padding:12px 0 4px;border-top:1px solid var(--dsw-alias-border-l2)}
.dps-btn{appearance:none;display:inline-flex;align-items:center;gap:6px;
  border:1px solid transparent;border-radius:8px;padding:5px 14px;font:inherit;
  font-size:13px;line-height:1.5;cursor:pointer;text-decoration:none}
.dps-btnPrimary{background:var(--dsw-alias-label-primary);color:var(--dsw-alias-bg-layer-3)}
.dps-btn:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:1px}
/* composer 工具行启动按钮：Button.module.css .toolbar 家族几何（随行排布，非浮动） */
.dps-launch{flex:none;position:relative;width:28px;height:28px;padding:0;
  display:inline-flex;align-items:center;justify-content:center;border:none;border-radius:8px;
  background:var(--dsw-alias-button-tool-bar-fill);color:var(--dsw-alias-label-secondary);cursor:pointer}
.dps-launch:hover{background:var(--dsw-alias-button-tool-bar-hover);color:var(--dsw-alias-label-primary)}
.dps-launch:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:1px}
/* 按钮角落三态状态灯：wait 呼吸、ok 常亮绿、bad 常亮红 */
.dps-launchDot{position:absolute;top:2px;right:2px;width:7px;height:7px;border-radius:50%;
  pointer-events:none}
.dps-launchDotWait{background:var(--dsw-alias-state-warn-primary);animation:dps-pulse 1.2s ease-in-out infinite}
.dps-launchDotOk{background:var(--dsw-alias-state-success-primary)}
.dps-launchDotBad{background:var(--dsw-alias-state-error-primary)}
@keyframes dps-pulse{0%,100%{opacity:1}50%{opacity:.3}}
/* 弹层：镜像 ui-input-trigger/MenuView.module.css .menu（自工具行向上展开） */
.dps-pop{position:absolute;bottom:36px;left:0;z-index:100;width:320px;max-height:320px;
  overflow-y:auto;padding:4px;border:1px solid var(--dsw-alias-border-inverted);
  border-radius:12px;background:var(--dsw-specific-menu);box-shadow:var(--dsw-shadow-lv3)}
.dps-popCard{padding:8px 12px;display:flex;flex-direction:column;gap:10px}
.dps-popHead{display:flex;align-items:center;gap:8px}
.dps-popName{flex:1;min-width:0;font-size:13px;font-weight:600;line-height:1.5;
  color:var(--dsw-alias-label-primary)}
.dps-popFoot{display:flex;justify-content:flex-end}
`
    function ensureStyles() {
      try {
        if (typeof document === 'undefined' || !document.getElementById) return
        if (document.getElementById(STYLE_ID)) return
        const el = document.createElement('style')
        el.id = STYLE_ID
        el.textContent = CSS
        document.head.appendChild(el)
      } catch (e) { /* 样式注入失败只损失美化，不阻断渲染 */ }
    }
    ensureStyles()

    // ── 图标：优先 ui-primitives（白名单模块），require 失败用同形内联 SVG ──
    const IconChevronDownOutline14 = (() => {
      try {
        const icon = require('@deepseek-ai/dsh-client-ui-primitives').IconChevronDownOutline14
        if (typeof icon === 'function') return icon
      } catch (e) { /* 兜底 */ }
      const path = 'M11.8486 5.5L11.4238 5.92383L8.69727 8.65137C8.44157 8.90706 8.21562 9.13382 '
        + '8.01172 9.29785C7.79912 9.46883 7.55595 9.61756 7.25 9.66602C7.08435 9.69222 6.91565 '
        + '9.69222 6.75 9.66602C6.44405 9.61756 6.20088 9.46883 5.98828 9.29785C5.78438 9.13382 '
        + '5.55843 8.90706 5.30273 8.65137L2.57617 5.92383L2.15137 5.5L3 4.65137L3.42383 '
        + '5.07617L6.15137 7.80273C6.42595 8.07732 6.59876 8.24849 6.74023 8.3623C6.87291 '
        + '8.46904 6.92272 8.47813 6.9375 8.48047C6.97895 8.48703 7.02105 8.48703 7.0625 '
        + '8.48047C7.07728 8.47813 7.12709 8.46904 7.25977 8.3623C7.40124 8.24849 7.57405 '
        + '8.07732 7.84863 7.80273L10.5762 5.07617L11 4.65137L11.8486 5.5Z'
      return function ChevronFallback() {
        return h('svg', { width: 14, height: 14, viewBox: '0 0 14 14', fill: 'none',
          xmlns: 'http://www.w3.org/2000/svg', 'aria-hidden': 'true' },
          h('path', { d: path, fill: 'currentColor' }))
      }
    })()

    /** 罗盘图标（无 ui-primitives 对应件）：currentColor 描边，随主题变色。 */
    function Compass16(props) {
      return h('svg', { width: 16, height: 16, viewBox: '0 0 16 16', fill: 'none',
        xmlns: 'http://www.w3.org/2000/svg', 'aria-hidden': 'true', ...props },
        h('circle', { cx: 8, cy: 8, r: 6.1, stroke: 'currentColor', strokeWidth: 1.2 }),
        h('path', { d: 'M10.9 5.1L8.35 7.65L5.1 10.9L7.65 8.35Z', fill: 'currentColor',
          stroke: 'currentColor', strokeWidth: 0.8, strokeLinejoin: 'round' }))
    }

    /** 渲染边界：组件任何一处抛错都只损失本卡，绝不白屏宿主页面。 */
    class Boundary extends React.Component {
      constructor(props) { super(props); this.state = { failed: false } }
      static getDerivedStateFromError() { return { failed: true } }
      componentDidCatch() { /* 插件侧异常就地吞掉；Host 日志面不因此染红 */ }
      render() { return this.state.failed ? null : this.props.children }
    }
    const guarded = (Comp) => function DpswarmGuarded(props) {
      return h(Boundary, null, h(Comp, props))
    }

    // ── sidecar 探测与安全取值 ─────────────────────────────────────────
    function probe() {
      try {
        if (typeof fetch !== 'function') return Promise.resolve(null)
        return fetch(PANEL_URL + '/api/status', { mode: 'cors' })
          .then(r => (r && r.ok && r.json ? r.json() : null)).catch(() => null)
      } catch (e) { return Promise.resolve(null) }
    }

    /**
     * 轮询控制面，返回 [phase, snapshot]。phase ∈ 'wait' | 'ok' | 'bad'：
     * 从未连上时保持 wait（黄），连续 FAILS_TO_RED 次失败才判 bad——这个窗口
     * 覆盖 autoStart 冷启动 Python sidecar 的耗时；曾连上则一次失败即 bad，
     * 之后任何一次成功恢复 ok。卸载即停，回调竞态由 alive 关闭。
     */
    const FAILS_TO_RED = 3
    function useSidecar(pollMs) {
      const [phase, setPhase] = useState('wait')
      const [snap, setSnap] = useState(null)
      useEffect(() => {
        let alive = true
        let fails = 0
        const poll = () => {
          probe().then(s => {
            if (!alive) return
            if (s) { fails = 0; setSnap(s); setPhase('ok') }
            else {
              setSnap(null)
              fails += 1
              setPhase(prev => {
                // 曾连上：一次失败即判离线；从未连上：连续 FAILS_TO_RED 次
                // 失败才判离线——把 autoStart 冷启动窗口让给"启动中"黄灯。
                if (prev === 'ok' || fails >= FAILS_TO_RED) return 'bad'
                return prev
              })
            }
          })
        }
        poll()
        const timer = setInterval(poll, pollMs)
        return () => { alive = false; clearInterval(timer) }
      }, [pollMs])
      return [phase, snap]
    }

    const isObj = (v) => v !== null && typeof v === 'object'
    const count = (v) => { try { return isObj(v) ? Object.keys(v).length : 0 } catch (e) { return 0 } }
    const num = (v) => (typeof v === 'number' && Number.isFinite(v)) || (typeof v === 'string' && v !== '') ? v : '—'
    const pair = (used, max) => (typeof used === 'number' && Number.isFinite(used)
      && typeof max === 'number' && Number.isFinite(max)) ? `${used}/${max}` : '—'

    /** 从任意形状的响应里安全导出渲染所需事实（永不抛错）。 */
    function readStatus(phase, st) {
      const connected = phase === 'ok'
      const s = connected && isObj(st) && isObj(st.snapshot) ? st.snapshot : null
      const spec = connected && isObj(st) && isObj(st.spec) ? st.spec : null
      if (!s) return { phase, connected: false, chips: [] }
      return {
        phase,
        connected: true,
        chips: [
          [L.slots, pair(s.open_worker_slots_used, spec && spec.max_open_work_items)],
          [L.points, pair(s.active_points, spec && spec.max_active_node_points)],
          [L.items, num(count(s.work_items))],
          [L.nodes, num(count(s.nodes))],
          [L.rev, num(spec && spec.revision)],
        ],
      }
    }

    function statusBadge(phase) {
      const cls = phase === 'ok' ? 'dps-dot dps-dotOk' : phase === 'bad' ? 'dps-dot dps-dotBad'
        : 'dps-dot dps-dotWait'
      const label = phase === 'ok' ? L.ok : phase === 'bad' ? L.bad : L.wait
      return h('span', { className: 'dps-badge' }, h('span', { className: cls }), label)
    }

    function chipsRow(chips) {
      return h('div', { className: 'dps-chips' },
        chips.map(([label, value]) => h('span', { className: 'dps-chip', key: String(label) },
          String(label), ' ', h('b', null, String(value)))))
    }

    function openPanelLink() {
      return h('a', { className: 'dps-btn dps-btnPrimary', href: PANEL_URL + '/',
        target: '_blank', rel: 'noreferrer', title: PANEL_URL }, L.open)
    }

    /** Plugins 设置页的 DPSwarm 卡片：chrome 镜像官方 PluginCard。 */
    function DpswarmCard() {
      const [phase, st] = useSidecar(4000)
      const [open, setOpen] = useState(true)
      let status
      try { status = readStatus(phase, st) } catch (e) { status = { connected: false, chips: [] } }
      return h('li', { className: open ? 'dps-card dps-cardOpen' : 'dps-card' },
        h('button', { type: 'button', className: 'dps-header', 'aria-expanded': open,
          onClick: () => { setOpen(!open) } },
          h('span', { className: 'dps-headText' },
            h('span', { className: 'dps-name' }, L.name),
            h('span', { className: 'dps-description' }, L.description)),
          statusBadge(status.phase),
          h('span', { className: open ? 'dps-chevron dps-chevronOpen' : 'dps-chevron',
            'aria-hidden': 'true' }, h(IconChevronDownOutline14, null))),
        open
          ? h('div', { className: 'dps-body' },
              h('div', { className: 'dps-field' },
                status.connected
                  ? chipsRow(status.chips)
                  : h('p', { className: 'dps-hint' },
                      status.phase === 'wait' ? L.hintStarting : L.offline)),
              h('div', { className: 'dps-field' },
                h('p', { className: 'dps-hint' }, L.hint)),
              h('div', { className: 'dps-footer' }, openPanelLink()))
          : null)
    }

    /** 🧭 弹层的紧凑面板：同 token，无卡片外壳（弹层自身即菜单面）。 */
    function DpswarmPanel() {
      const [phase, st] = useSidecar(4000)
      let status
      try { status = readStatus(phase, st) } catch (e) { status = { connected: false, chips: [] } }
      return h('div', { className: 'dps-popCard' },
        h('div', { className: 'dps-popHead' },
          h('span', { className: 'dps-popName' }, L.name),
          statusBadge(status.phase)),
        status.connected
          ? chipsRow(status.chips)
          : h('p', { className: 'dps-hint' },
              status.phase === 'wait' ? L.hintStarting : L.offline),
        h('div', { className: 'dps-popFoot' }, openPanelLink()))
    }

    /** composer 工具行的罗盘启动按钮：角落三态状态灯，点开菜单面弹层。 */
    function DpswarmLaunch() {
      const [phase] = useSidecar(4000)
      const [open, setOpen] = useState(false)
      const wrapRef = useRef(null)
      const toggle = useCallback(() => setOpen(o => !o), [])
      useEffect(() => {
        if (!open) return
        // 指针落点在自身之外即收起（捕获相位，与官方 MenuView 同法）；
        // Esc 收起。按钮自身在 wrap 内，不触发外部判定，click 的 toggle 不被抵消。
        const onPointerDown = (ev) => {
          const target = ev.target
          if (typeof Node === 'function' && target instanceof Node
            && wrapRef.current && !wrapRef.current.contains(target)) setOpen(false)
        }
        const onKeyDown = (ev) => { if (ev.key === 'Escape') setOpen(false) }
        document.addEventListener('pointerdown', onPointerDown, true)
        document.addEventListener('keydown', onKeyDown)
        return () => {
          document.removeEventListener('pointerdown', onPointerDown, true)
          document.removeEventListener('keydown', onKeyDown)
        }
      }, [open])
      const dotCls = phase === 'ok' ? 'dps-launchDot dps-launchDotOk'
        : phase === 'bad' ? 'dps-launchDot dps-launchDotBad'
          : 'dps-launchDot dps-launchDotWait'
      const label = phase === 'ok' ? L.launchOk : phase === 'bad' ? L.launchBad : L.launchWait
      return h('div', { ref: wrapRef, style: { position: 'relative', display: 'inline-block' } },
        h('button', { type: 'button', className: 'dps-launch', title: label,
          'aria-label': label, 'aria-expanded': open, onClick: toggle },
          h(Compass16, null),
          h('span', { className: dotCls, 'aria-hidden': 'true' })),
        open ? h('div', { className: 'dps-pop' }, h(DpswarmPanel, null)) : null)
    }

    const inject = ['slots', 'locale', 'connection']

    function apply(ctx) {
      // Plugins 设置页卡片（keyed：key = Host 侧 namespace 'dpswarm'）
      try {
        ctx.slots.inject('settings.plugin.item', () => ctx.slots.register({
          name: 'settings.plugin.item',
          key: 'dpswarm',
          locale: NS,
          inject: () => ({}),
        }, guarded(DpswarmCard)))
      } catch (e) { ctx.logger?.warn?.(`dpswarm card: ${e}`) }
      // composer 工具行启动入口（"Workspace Write" 与模型选择器之间的座位）
      try {
        ctx.slots.inject('conversation.input.left', () => ctx.slots.register({
          name: 'conversation.input.left',
          id: 'dpswarm-launch',
          order: 40,
          locale: NS,
          inject: () => ({}),
        }, guarded(DpswarmLaunch)))
      } catch (e) { ctx.logger?.warn?.(`dpswarm launch: ${e}`) }
    }

    return { inject, apply }
  },
})
