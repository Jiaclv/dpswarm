/**
 * DPSwarm dsh 插件（浏览器半）——factory bundle（手写，免构建）。
 *
 * 工件契约：调用 window.__ModuleLoader__.load({id, factory})，factory(require)
 * 的返回值即模块导出（apply/inject）；react 等白名单模块经注入 require 解析。
 *
 * 注册：
 * - settings.section（id: dpswarm）—— 独立模型与协作设置页
 * - settings.plugin.item（key: dpswarm）—— 插件概览与设置位置
 * - conversation.input.left —— composer 工具行内的 🧭 启动入口（"Workspace Write"
 *   模式选择器与模型选择器之间的座位），带三态状态灯
 *
 * 状态灯：灰=当前任务关闭，黄=连接检查中，绿=已开启且服务兼容，红=无法连接。
 * 开关由宿主设置保存结果决定；本地勾选不能授权执行。
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
    const { useEffect, useState, useRef, useCallback, useId } = React
    const h = React.createElement

    let settingsScope, settingsApi, settingsMirror
    // Resolve the active host service when the user loads the catalog. Remote
    // namespaces can mount after this entry and can be replaced on reconnect.
    let modelCatalogApi = () => undefined
    let writeTail = Promise.resolve()
    // Use the public atomic mutation API. SettingsScope.set() can resolve after
    // recovering a rejected write, so resolution alone is not proof of a save.
    function writeSettings(change) {
      const operation = writeTail.then(async () => {
        const snapshot = settingsScope?.getSnapshot()
        if (snapshot?.status !== 'ready' || !snapshot.writable || snapshot.mode !== 'host'
          || !Number.isSafeInteger(snapshot.revision) || !settingsApi?.mutate || !settingsMirror?.acceptView) {
          throw new Error('宿主设置当前不可写，请连接本机宿主后重试。')
        }
        const patch = typeof change === 'function' ? change(snapshot.value || {}) : change
        const response = await settingsApi.mutate({ ns: 'dpswarm', expectedRevision: snapshot.revision,
          ops: Object.entries(patch).map(([field, value]) => ({ op: 'set', path: [field], value })) })
        if (!response?.result?.ok) {
          // Refresh the failed namespace, never automatically overwrite a conflict.
          try {
            const fresh = await settingsApi.describe({})
            const view = fresh?.result?.value?.namespaces?.find(v => v.ns === 'dpswarm')
            if (fresh?.result?.ok && view) settingsMirror.acceptView(view)
          } catch { /* Keep the original rejection visible. */ }
          throw new Error(response?.result?.error?.message || '保存被宿主拒绝，请检查最新设置后重试。')
        }
        const view = response.result.value
        if (view?.ns !== 'dpswarm' || Object.entries(patch).some(([key, value]) => JSON.stringify(view.value?.[key]) !== JSON.stringify(value))) {
          throw new Error('宿主未确认所提交的设置，请重新打开设置核对。')
        }
        settingsMirror.acceptView(view)
      })
      writeTail = operation.catch(() => {})
      return operation
    }
    function panelUrl() {
      const value = settingsScope?.getSnapshot()?.value?.sidecarUrl
      if (typeof value !== 'string') return null
      try {
        const url = new URL(value)
        return url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname)
          && !url.username && !url.password && url.pathname === '/' && !url.search && !url.hash
          ? url.origin : null
      } catch { return null }
    }
    const NS = 'settings.dpswarm.card'
    const STYLE_ID = 'dpswarm-dsh-plugin-style'

    const DICT = {
      zh: {
        name: 'DPSwarm',
        description: '分别设置 Lead、实现者、测试者和 CM；按任务一键开启。',
        ok: '已开启 · 已连接', off: '固定团队关闭', version: '服务需更新', wait: '已开启 · 启动中', bad: '已开启 · 未连接', open: '打开控制面板',
        slots: 'worker 槽', points: '点数', items: 'items', nodes: 'nodes', rev: 'Spec rev',
        offline: '控制服务未连接，请运行安装自检并检查 Python 路径；旧版服务需要更新或更换端口。',
        hintStarting: '控制服务按需启动。若无法连接，请检查安装自检与 Python 配置。',
        hint: '先保存固定角色配置，再在当前任务输入框的罗盘菜单中开启。关闭会取消在途协作；已有交付仍需主 agent 验收或终止。',
        launchOff: 'DPSwarm（当前任务关闭）', launch: 'DPSwarm 固定协作',
        launchOk: 'DPSwarm（当前任务已开启）',
        launchWait: 'DPSwarm 控制面板（启动中…）',
        launchBad: 'DPSwarm 控制面板（未连接）',
      },
      en: {
        name: 'DPSwarm',
        description: 'Explicit fixed-team collaboration: implementer, tester, then Lead acceptance and implementer rework.',
        ok: 'enabled · online', off: 'off', version: 'update sidecar', wait: 'enabled · starting', bad: 'enabled · offline', open: 'Open panel',
        slots: 'worker slots', points: 'points', items: 'items', nodes: 'nodes', rev: 'Spec rev',
        offline: 'Control service offline. Run the installation check and verify the Python path and service version.',
        hintStarting: 'Control service starts on demand after explicit activation.',
        hint: 'Save role routes, then enable this session in the input toolbar. Disabling cancels active collaboration; existing deliveries still need Lead review.',
        launchOff: 'DPSwarm (off for this task)', launch: 'DPSwarm fixed team',
        launchOk: 'DPSwarm (enabled for this task)',
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
.dps-workerStatus{margin-top:12px;border-top:1px solid var(--dsw-alias-border-l2);padding-top:12px}
.dps-workerStatus h4{margin:0 0 8px;font-size:13px}
.dps-workerRow{padding:8px 0;border-bottom:1px solid var(--dsw-alias-border-l2)}
.dps-workerHeading{display:flex;justify-content:space-between;gap:12px;font-size:12px}
.dps-workerReason{font-size:12px;line-height:1.5;color:var(--dsw-alias-state-warn-primary);overflow-wrap:anywhere;margin:6px 0}
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
.dps-pop{position:fixed;z-index:100;width:min(380px,calc(100vw - 32px));max-height:480px;
  overflow-y:auto;padding:4px;border:1px solid var(--dsw-alias-border-inverted);
  border-radius:12px;background:var(--dsw-specific-menu);box-shadow:var(--dsw-shadow-lv3)}
.dps-popCard{padding:8px 12px;display:flex;flex-direction:column;gap:10px}
.dps-popHead{display:flex;align-items:center;gap:8px}
.dps-popName{flex:1;min-width:0;font-size:13px;font-weight:600;line-height:1.5;
  color:var(--dsw-alias-label-primary)}
.dps-popFoot{display:flex;justify-content:flex-end}
.dps-switch{display:flex;gap:10px;align-items:center;font-size:13px;cursor:pointer}
.dps-switch input{width:18px;height:18px;accent-color:var(--dsw-alias-brand-primary)}
.dps-route{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.dps-route label{display:flex;flex-direction:column;gap:4px;font-size:12px;min-width:0}
.dps-route input,.dps-route select{width:100%;box-sizing:border-box;padding:7px;border:1px solid var(--dsw-alias-border-l2);border-radius:6px;background:var(--dsw-alias-bg-layer-3);color:var(--dsw-alias-label-primary);font:inherit}
.dps-error{font-size:12px;color:var(--dsw-alias-state-error-primary);overflow-wrap:anywhere}
.dps-launchDotOff{background:var(--dsw-alias-label-tertiary)}
.dps-settings{display:flex;flex-direction:column;gap:16px;min-width:0;color:var(--dsw-alias-label-primary)}
.dps-settings h2{margin:0;font-size:20px;line-height:1.5}
.dps-settings h3{margin:0;font-size:15px;font-weight:600}
.dps-settingsCard{border:1px solid var(--dsw-alias-border-l2);border-radius:12px;padding:16px;display:flex;flex-direction:column;gap:12px;background:var(--dsw-alias-bg-layer-3);min-width:0}
.dps-settingsCard .dps-route{column-gap:16px;row-gap:12px}
.dps-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.dps-btnSecondary{background:var(--dsw-alias-bg-module-platform);color:var(--dsw-alias-label-secondary);border-color:var(--dsw-alias-border-l2)}
.dps-btn:disabled{opacity:.5;cursor:default}
.dps-saved{font-size:12px;color:var(--dsw-alias-state-success-primary)}
.dps-summary{display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px 12px;margin:0;font-size:12px;line-height:1.6}
.dps-summary dt{color:var(--dsw-alias-label-tertiary)}
.dps-summary dd{margin:0;overflow-wrap:anywhere}
@media(max-width:600px){.dps-settingsCard .dps-route{grid-template-columns:1fr}.dps-settingsCard{padding:12px}}
/* Model assignment: restrained host-native surfaces and one primary action per role. */
.dps-settings{gap:18px}
.dps-pageHeader{display:flex;align-items:center;gap:12px}
.dps-pageHeader h2{font-size:22px;letter-spacing:-.5px}
.dps-pageHeader p{margin:3px 0 0;font-size:13px;color:var(--dsw-alias-label-secondary)}
.dps-pageMark{width:40px;height:40px;display:grid;place-items:center;border-radius:12px;color:var(--dsw-alias-brand-primary);background:color-mix(in srgb,var(--dsw-alias-brand-primary) 9%,var(--dsw-alias-bg-layer-3))}
.dps-pageMark svg{width:24px;height:24px}
.dps-tabs{display:flex;gap:20px;border-bottom:1px solid var(--dsw-alias-border-l2)}
.dps-tabs button{appearance:none;border:0;border-bottom:2px solid transparent;padding:0 0 10px;background:none;color:var(--dsw-alias-label-tertiary);font:inherit;font-size:13px;cursor:pointer}
.dps-tabs button[aria-selected=true]{border-color:var(--dsw-alias-brand-primary);color:var(--dsw-alias-label-primary);font-weight:600}
.dps-tabs button:focus-visible,.dps-textButton:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:3px;border-radius:3px}
.dps-tabPanel{display:flex;flex-direction:column;gap:12px;min-width:0}.dps-tabPanel[hidden]{display:none}
.dps-leadStrip{display:flex;gap:10px;align-items:center;border-radius:10px;background:var(--dsw-alias-bg-layer-3);padding:12px}
.dps-leadStrip>div{flex:1;min-width:0}.dps-leadStrip b{font-size:13px}.dps-leadStrip p{margin:4px 0 0;font-size:12px;color:var(--dsw-alias-label-tertiary);line-height:1.5}
.dps-textButton{border:0;background:none;color:var(--dsw-alias-brand-primary);cursor:pointer;font:inherit;font-size:12px;padding:4px;flex-shrink:0}.dps-textButton:disabled{opacity:.5;cursor:default}
.dps-sectionLabel{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:6px 0 0;font-size:12px;color:var(--dsw-alias-label-tertiary)}.dps-sectionLabel>span:first-child{font-weight:600;color:var(--dsw-alias-label-secondary)}
.dps-roleCard{margin:0;padding:14px 16px;border:1px solid var(--dsw-alias-border-l2);border-radius:12px;background:var(--dsw-alias-bg-layer-2);display:flex;flex-direction:column;gap:12px;min-width:0}
.dps-roleHeading{display:flex;gap:9px;align-items:flex-start}.dps-roleHeading>div{min-width:0;flex:1}.dps-roleHeading h3{font-size:14px;line-height:1.5}.dps-roleHeading p{margin:3px 0 0;font-size:12px;line-height:1.5;color:var(--dsw-alias-label-tertiary)}
.dps-roleNumber{font-size:10px;line-height:22px;min-width:24px;text-align:center;border:1px solid var(--dsw-alias-border-l2);border-radius:6px;color:var(--dsw-alias-label-secondary);font-variant-numeric:tabular-nums}
.dps-routeState{font-size:10px;line-height:20px;padding:0 6px;background:var(--dsw-alias-bg-layer-3);border-radius:5px;color:var(--dsw-alias-label-tertiary);white-space:nowrap}
.dps-roleControls{display:grid;grid-template-columns:minmax(0,1fr) 122px;gap:12px;align-items:end}.dps-modelField:only-child{grid-column:1/-1}
/* Each subagent receives its own limits; Lead remains outside this budget. */
.dps-budgetModes{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px;border:0;padding:0;margin:0;min-width:0}
.dps-budgetMode{position:relative;display:flex;align-items:flex-start;gap:8px;cursor:pointer;border:1px solid var(--dsw-alias-border-l2);border-radius:10px;padding:12px 10px;background:var(--dsw-alias-bg-layer-3);min-width:0}
.dps-budgetMode:has(input:checked){border-color:var(--dsw-alias-brand-primary);background:color-mix(in srgb,var(--dsw-alias-brand-primary) 6%,var(--dsw-alias-bg-layer-3))}
.dps-budgetMode:has(input:focus-visible){outline:2px solid var(--dsw-alias-brand-primary);outline-offset:2px}.dps-budgetMode:has(input:disabled){opacity:.6;cursor:default}
.dps-budgetMode input{margin:3px 0 0;accent-color:var(--dsw-alias-brand-primary);flex-shrink:0}.dps-budgetMode span{display:flex;flex-direction:column;gap:5px;min-width:0}
.dps-budgetMode b{font-size:13px;font-weight:600;line-height:1.4}.dps-budgetMode small{font-size:11px;line-height:1.5;color:var(--dsw-alias-label-tertiary)}
.dps-budgetFields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.dps-budgetFields label{min-width:0;font-size:12px;color:var(--dsw-alias-label-secondary)}
.dps-budgetFields input{width:100%;box-sizing:border-box;margin:6px 0;padding:10px 12px;border:1px solid var(--dsw-alias-border-l2);border-radius:8px;background:var(--dsw-alias-bg-layer-3);color:var(--dsw-alias-label-primary);font:inherit;font-size:15px;font-variant-numeric:tabular-nums}
.dps-budgetFields input:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:1px}.dps-budgetFields input[aria-invalid=true]{border-color:var(--dsw-alias-state-error-primary)}
.dps-budgetInfo{border-radius:8px;background:var(--dsw-alias-bg-layer-3);padding:11px 12px;font-size:12px;line-height:1.65;color:var(--dsw-alias-label-secondary)}
.dps-budgetSummary{border-top:1px solid var(--dsw-alias-border-l2);padding-top:10px;display:flex;align-items:flex-start;justify-content:space-between;gap:8px}.dps-budgetSummary>div{min-width:0}.dps-budgetSummary b{font-size:12px;line-height:1.5}.dps-budgetSummary p{margin:4px 0 0}
@media(max-width:600px){.dps-budgetModes{grid-template-columns:1fr}.dps-budgetMode{padding:10px 12px}.dps-budgetMode span{gap:3px}.dps-budgetFields{grid-template-columns:1fr}}
.dps-inputCaption{font-size:11px;color:var(--dsw-alias-label-secondary);display:block;margin:0 0 6px}
.dps-modelTrigger{display:flex;align-items:center;gap:10px;width:100%;box-sizing:border-box;min-height:58px;appearance:none;text-align:left;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-3);border-radius:9px;padding:8px 10px;cursor:pointer;font:inherit;color:var(--dsw-alias-label-primary)}
.dps-modelTrigger:hover:not(:disabled){border-color:var(--dsw-alias-brand-primary)}.dps-modelTrigger:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:2px}.dps-modelTrigger:disabled{opacity:.6;cursor:default}
.dps-modelGlyph{display:grid;place-items:center;flex-shrink:0;width:30px;height:30px;border-radius:8px;background:color-mix(in srgb,var(--dsw-alias-brand-primary) 8%,var(--dsw-alias-bg-layer-3));color:var(--dsw-alias-brand-primary);font-size:12px;font-weight:650}
.dps-modelIdentity{display:flex;flex:1;flex-direction:column;min-width:0;gap:4px;text-align:left}.dps-modelIdentity b{font-size:13px;font-weight:550;line-height:1.4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.dps-modelIdentity small{font-size:11px;line-height:1.4;color:var(--dsw-alias-label-tertiary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dps-effortField{min-width:0}.dps-effortField select{box-sizing:border-box;width:100%;height:58px;border:1px solid var(--dsw-alias-border-l2);border-radius:9px;padding:8px;background:var(--dsw-alias-bg-layer-3);color:var(--dsw-alias-label-primary);font:inherit;font-size:12px;text-overflow:ellipsis}.dps-effortField select:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:2px}
.dps-manual{font-size:11px;color:var(--dsw-alias-label-tertiary)}.dps-manual summary{cursor:pointer;width:fit-content}.dps-manual[open]{display:flex;flex-direction:column;gap:8px}.dps-manual[open] .dps-route{margin-top:8px}
.dps-roleFooter{display:flex;justify-content:space-between;align-items:center;gap:8px;border-top:1px solid var(--dsw-alias-border-l2);padding-top:10px}.dps-roleFooter .dps-btn{font-size:12px;padding:4px 12px}
.dps-pageNote{margin:0;font-size:12px;line-height:1.7;color:var(--dsw-alias-label-tertiary)}
.dps-modelDialog{box-sizing:border-box;width:min(540px,calc(100vw - 32px));max-height:min(700px,calc(100dvh - 40px));padding:0;border:1px solid var(--dsw-alias-border-l2);border-radius:16px;background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-primary);font:inherit;box-shadow:var(--dsw-shadow-lv3);overflow:hidden}
.dps-modelDialog[open]{display:flex;flex-direction:column}.dps-modelDialog::backdrop{background:color-mix(in srgb,var(--dsw-alias-label-primary) 22%,transparent);backdrop-filter:blur(3px)}
.dps-pickerHeader{padding:20px 20px 14px;display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.dps-pickerHeader h3{margin:0;font-size:17px}.dps-pickerHeader p{margin:5px 0 0;font-size:12px;color:var(--dsw-alias-label-tertiary)}
.dps-iconButton{background:var(--dsw-alias-bg-layer-3);border:0;border-radius:7px;width:28px;height:28px;cursor:pointer;font:inherit;font-size:21px;line-height:1;color:var(--dsw-alias-label-secondary)}
.dps-pickerSearch{padding:0 20px}.dps-pickerSearch input{box-sizing:border-box;width:100%;border:1px solid var(--dsw-alias-border-l2);border-radius:9px;padding:11px 12px;background:var(--dsw-alias-bg-layer-3);color:var(--dsw-alias-label-primary);font:inherit;font-size:13px}.dps-pickerSearch input:focus{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:1px}
.dps-pickerMeta{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 20px;font-size:11px;color:var(--dsw-alias-label-tertiary)}
.dps-modelList{min-height:100px;max-height:370px;overflow:auto;padding:0 10px 10px;overscroll-behavior:contain}
.dps-modelOption{display:flex;align-items:center;gap:12px;box-sizing:border-box;width:100%;background:none;border:1px solid transparent;border-radius:9px;padding:10px;cursor:pointer;color:var(--dsw-alias-label-primary);font:inherit}
.dps-modelActive{background:var(--dsw-alias-bg-layer-3);border-color:var(--dsw-alias-border-l2)}.dps-modelOption[aria-selected=true] .dps-modelIdentity b,.dps-modelCheck{color:var(--dsw-alias-brand-primary)}.dps-modelCheck{width:18px;flex-shrink:0;font-size:15px}
.dps-pickerNotice{margin:0 20px 10px;font-size:12px;line-height:1.5;color:var(--dsw-alias-state-warn-primary)}
.dps-empty{padding:28px 16px;text-align:center;color:var(--dsw-alias-label-secondary);font-size:13px}.dps-empty p{font-size:12px;line-height:1.5;color:var(--dsw-alias-label-tertiary)}
.dps-pickerFooter{border-top:1px solid var(--dsw-alias-border-l2);padding:12px 20px;margin:0;font-size:11px;color:var(--dsw-alias-label-tertiary)}
@media(max-width:600px){.dps-roleControls{grid-template-columns:minmax(0,1fr)}.dps-effortField select{height:38px}.dps-roleCard{padding:12px}.dps-leadStrip{flex-wrap:wrap}.dps-leadStrip .dps-textButton{margin-left:40px}.dps-sectionLabel{align-items:flex-start;flex-direction:column;gap:4px}.dps-roleFooter{flex-wrap:wrap}.dps-roleFooter .dps-actions{margin-left:auto}.dps-tabs{gap:18px}.dps-pickerHeader{padding:16px}.dps-pickerSearch{padding:0 16px}}
/* 主开关条：一键同时开启固定团队与 CM（弹层内唯一开关） */
.dps-masterSwitch{display:flex;align-items:center;gap:10px;box-sizing:border-box;width:100%;cursor:pointer;border:1px solid var(--dsw-alias-border-l2);border-radius:10px;background:var(--dsw-alias-bg-layer-3);padding:10px 12px;color:var(--dsw-alias-label-primary)}
.dps-masterSwitch:hover{border-color:var(--dsw-alias-label-dimmed)}
.dps-masterSwitch:has(input:focus-visible){outline:2px solid var(--dsw-alias-brand-primary);outline-offset:2px}
.dps-masterSwitch:has(input:disabled){opacity:.6;cursor:default}
.dps-masterSwitch>svg{flex-shrink:0;color:var(--dsw-alias-brand-primary)}
.dps-masterSwitch>div{flex:1;min-width:0}
.dps-masterSwitch b{font-size:13px;font-weight:600;line-height:1.4}
.dps-masterSwitch small{display:block;margin-top:2px;font-size:11px;line-height:1.5;color:var(--dsw-alias-label-tertiary)}
.dps-masterSwitch input{flex-shrink:0;width:18px;height:18px;accent-color:var(--dsw-alias-brand-primary)}
/* 关键参数与总 token 占比条（弹层） */
.dps-keyParams{font-size:11px;gap:3px 10px}
.dps-keyParams dd{color:var(--dsw-alias-label-secondary)}
.dps-tokenShare{display:flex;flex-direction:column;gap:6px}
.dps-tokenBar{display:flex;height:6px;border-radius:4px;overflow:hidden;background:var(--dsw-alias-bg-module-platform)}
.dps-tokenBar i{display:block;height:100%;min-width:2px}
/* 协作模式选择（弹层）：串行 / 并行 / 分阶段，按会话保存 */
.dps-modeRow{display:flex;gap:6px}
.dps-modeChip{flex:1;appearance:none;border:1px solid var(--dsw-alias-border-l2);border-radius:8px;background:var(--dsw-alias-bg-layer-3);color:var(--dsw-alias-label-secondary);font:inherit;font-size:12px;line-height:1.5;padding:6px 0;cursor:pointer;text-align:center}
.dps-modeChip:hover:not(:disabled){border-color:var(--dsw-alias-label-dimmed);color:var(--dsw-alias-label-primary)}
.dps-modeChip:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:1px}
.dps-modeChipOn{border-color:var(--dsw-alias-brand-primary);color:var(--dsw-alias-brand-primary);background:color-mix(in srgb,var(--dsw-alias-brand-primary) 8%,var(--dsw-alias-bg-layer-3));font-weight:600}
.dps-modeChip:disabled{opacity:.5;cursor:default}
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
    function useSettings() {
      const [snapshot, setSnapshot] = useState(() => settingsScope?.getSnapshot() || { status: 'loading' })
      useEffect(() => {
        const refresh = () => setSnapshot(settingsScope?.getSnapshot() || { status: 'unavailable' })
        refresh()
        return settingsScope?.subscribe(refresh)
      }, [])
      return snapshot
    }

    function useSidecar(pollMs, sessionId) {
      const settings = useSettings(), value = settings.value || {}
      const on = sessionId
        ? (value.enabledSessions || []).includes(sessionId) || (value.cmEnabledSessions || []).includes(sessionId)
        : (value.enabledSessions || []).length > 0
      const url = panelUrl()
      const [phase, setPhase] = useState('wait'), [snap, setSnap] = useState(null)
      useEffect(() => {
        let alive = true, current
        const poll = async () => {
          if (!url || !on) { setPhase('off'); setSnap(null); return }
          current = new AbortController()
          const timer = setTimeout(() => current.abort(), 2500)
          try {
            const response = await fetch(url + '/api/status', { signal: current.signal, mode: 'cors',
              headers: sessionId ? { 'X-DPSwarm-Session': sessionId } : {} })
            if (!response.ok) throw new Error('Sidecar unavailable')
            const result = await response.json()
            if (alive) { setSnap(result); setPhase(result.bridge?.session_isolation === true ? 'ok' : 'version') }
          } catch { if (alive) { setSnap(null); setPhase('bad') } }
          finally { clearTimeout(timer) }
        }
        poll()
        const timer = setInterval(poll, pollMs)
        return () => { alive = false; clearInterval(timer); current?.abort() }
      }, [pollMs, sessionId, url, on])
      return [on ? phase : 'off', snap]
    }

    // Same legacy-compatible mode resolution as fixed-team.js.
    const implMode = cfg => cfg.implMode || (typeof cfg.implProvider === 'string' && cfg.implProvider.trim() ? 'model' : 'lead')
    const missingModelFields = (provider, model) => [['Provider', provider], ['模型', model]]
      .filter(([, value]) => typeof value !== 'string' || !value.trim()).map(([field]) => field)
    const missingRoutes = (cfg, cm = false) => {
      const roles = cm ? [['CM', cfg.cmProvider, cfg.cmModel ?? 'deepseek-v4-flash']] : [
        ...(implMode(cfg) === 'lead' ? [] : [['实现者', cfg.implProvider, cfg.implModel]]),
        ['测试者', cfg.testProvider, cfg.testModel],
        ...(cfg.reviewerMode === 'model' ? [['Reviewer', cfg.reviewerProvider, cfg.reviewerModel]] : [])]
      return roles.flatMap(([role, provider, model]) => {
        const fields = missingModelFields(provider, model)
        return fields.length ? [role + '的 ' + fields.join(' 和 ')] : []
      })
    }
    const routeHint = missing => '尚未配置：' + missing.join('、') + '。请在设置 → DPswarm 选择模型并保存。'

    /** 唯一主开关：固定团队与 CM 同时开启/关闭；后端两个数组仍分别记录。 */
    function DpswarmSwitch({ sessionId }) {
      const snapshot = useSettings(), cfg = snapshot.value || {}
      const [pending, setPending] = useState(false), [error, setError] = useState('')
      const teamOn = (cfg.enabledSessions || []).includes(sessionId)
      const cmOn = (cfg.cmEnabledSessions || []).includes(sessionId)
      const on = teamOn && cmOn, partial = teamOn !== cmOn
      const ready = snapshot.status === 'ready' && snapshot.writable === true && snapshot.mode === 'host' && typeof sessionId === 'string' && !!sessionId
      const missing = [...missingRoutes(cfg, false), ...missingRoutes(cfg, true)]
      const change = async event => {
        const next = event.target.checked
        setPending(true); setError('')
        try {
          const latest = settingsScope.getSnapshot()
          if (latest.status !== 'ready' || !latest.writable || latest.mode !== 'host') throw new Error('Host settings are unavailable')
          await writeSettings(value => {
            const missing = [...missingRoutes(value, false), ...missingRoutes(value, true)]
            if (next && missing.length) throw new Error(routeHint(missing))
            const enable = ids => [...new Set([...(ids || []), sessionId])]
            const disable = ids => (ids || []).filter(id => id !== sessionId)
            const apply = next ? enable : disable
            return { enabledSessions: apply(value.enabledSessions), cmEnabledSessions: apply(value.cmEnabledSessions) }
          })
        } catch (e) { setError(String(e.message || e)) } finally { setPending(false) }
      }
      const caption = !ready ? '打开一个任务后可开启；需要可写的宿主设置。'
        : !on && missing.length ? routeHint(missing)
          : partial ? (teamOn ? '当前仅固定团队已开启' : '当前仅 CM 已开启') + '；开启后两者同时生效。'
            : on ? '固定团队 + CM 已开启。关闭会取消在途协作与压缩；已采用摘要与已有交付保留。'
              : '固定团队（实现者 → 测试者 → 审查）+ CM 上下文管理，一键同时开启。'
      return h('div', null,
        h('label', { className: 'dps-masterSwitch' },
          h(Compass16, { 'aria-hidden': 'true' }),
          h('div', null, h('b', null, '开启 DPSwarm'), h('small', null, caption)),
          h('input', { type: 'checkbox', role: 'switch', checked: on,
            disabled: pending || !ready || (!on && missing.length > 0), onChange: change, 'aria-label': '开启 DPSwarm（固定团队 + CM）' })),
        error ? h('p', { className: 'dps-error', role: 'alert' }, error) : null)
    }

    /** 协作模式三档选择：serial（默认，禁止拆分）/ parallel（Lead 可拆 ≤3 子任务）/ staged（产物板+挂起唤醒）。 */
    const TEAM_MODES = [
      ['serial', '串行', '实现者→测试者→审查（默认）'],
      ['parallel', '并行', 'Lead 可拆分 ≤3 个互不重叠子任务并行'],
      ['staged', '分阶段', '产物状态板 + 相位 + 挂起唤醒'],
    ]
    const teamModeOf = (cfg, sessionId) => ((cfg.teamModeOverrides || []).filter(r => r?.sessionId === sessionId).at(-1)?.mode) || 'serial'
    function TeamModePicker({ sessionId }) {
      const snapshot = useSettings(), cfg = snapshot.value || {}
      const [pending, setPending] = useState(false), [error, setError] = useState('')
      const current = teamModeOf(cfg, sessionId)
      const ready = snapshot.status === 'ready' && snapshot.writable === true && snapshot.mode === 'host' && typeof sessionId === 'string' && !!sessionId
      const pick = async mode => {
        if (mode === current || pending) return
        setPending(true); setError('')
        try {
          const latest = settingsScope.getSnapshot()
          if (latest.status !== 'ready' || !latest.writable || latest.mode !== 'host') throw new Error('Host settings are unavailable')
          await writeSettings(value => ({ teamModeOverrides: [...(value.teamModeOverrides || []).filter(r => r?.sessionId !== sessionId), { sessionId, mode }] }))
        } catch (e) { setError(String(e.message || e)) } finally { setPending(false) }
      }
      return h('div', null,
        h('div', { className: 'dps-modeRow', role: 'radiogroup', 'aria-label': '协作模式' },
          TEAM_MODES.map(([mode, title, hint]) => h('button', { key: mode, type: 'button', role: 'radio', 'aria-checked': current === mode,
            className: current === mode ? 'dps-modeChip dps-modeChipOn' : 'dps-modeChip', disabled: !ready || pending, onClick: () => pick(mode), title: hint }, title))),
        error ? h('p', { className: 'dps-error', role: 'alert' }, error) : null)
    }

    function SettingsField({ field, label, type = 'text' }) {
      const snapshot = useSettings(), saved = snapshot.value?.[field] ?? ''
      const [value, setValue] = useState(String(saved)), [error, setError] = useState('')
      useEffect(() => setValue(String(saved)), [saved])
      const save = async () => {
        const next = type === 'number' ? Number(value) : value.trim()
        if (next === saved) return
        try {
          if (type === 'number' && (!Number.isInteger(next) || next < 10 || next > 7200)) throw new Error('请输入 10–7200 秒之间的整数')
          await writeSettings({ [field]: next }); setError('')
        }
        catch (e) { setError(String(e.message || e)); setValue(String(saved)) }
      }
      return h('label', null, label, h('input', { type, value, ...(type === 'number' ? { min: 10, max: 7200, step: 1 } : {}), disabled: snapshot.status !== 'ready' || !snapshot.writable || snapshot.mode !== 'host',
        'aria-label': label, onChange: e => setValue(e.target.value), onBlur: save,
        onKeyDown: e => { if (e.key === 'Enter') e.currentTarget.blur() } }),
        error ? h('span', { className: 'dps-error', role: 'alert' }, error) : null)
    }

    const ROLE_DEFAULTS = {
      impl: { model: '', effort: '' }, test: { model: 'glm-5.3-flash', effort: '' },
      cm: { model: 'deepseek-v4-flash', effort: 'off' }, reviewer: { model: '', effort: '' },
    }
    function useModelCatalog() {
      const [state, setState] = useState({ status: 'idle', groups: [], failures: [], error: '' })
      const generation = useRef(0), request = useRef(null)
      const load = useCallback(async () => {
        const ticket = ++generation.current
        request.current?.abort()
        const abort = new AbortController(); request.current = abort
        setState(old => ({ ...old, status: 'loading', error: '' }))
        let timer
        try {
          const liveLlm = modelCatalogApi()
          if (!liveLlm) throw new Error('当前 DPH 的模型目录暂不可用，请刷新列表或使用手动配置。')
          const result = await Promise.race([liveLlm.models({}, abort.signal), new Promise((_, reject) => {
            timer = setTimeout(() => { abort.abort(); reject(new Error('读取模型列表超时，请重试。')) }, 12000)
          })])
          if (!result?.result?.ok) throw new Error(result?.result?.error?.message || '无法读取 DPH 模型列表')
          if (generation.current !== ticket) return
          const value = result.result.value
          if (!Array.isArray(value?.groups) || !Array.isArray(value?.failures)) throw new Error('宿主返回的模型列表格式不兼容。')
          setState({ status: 'ready', groups: value.groups, failures: value.failures, error: '' })
        } catch (e) { if (generation.current === ticket) setState(old => ({ ...old, status: 'error', error: String(e.message || e) })) }
        finally { clearTimeout(timer) }
      }, [])
      useEffect(() => { load(); return () => { generation.current++; request.current?.abort() } }, [load])
      return { ...state, load }
    }
    function catalogRows(catalog) {
      return catalog.groups.flatMap(group => (group.models || []).map(model => ({ ...model, provider: group.id, providerName: group.name || group.id })))
    }
    function routeModel(catalog, route) { return catalogRows(catalog).find(m => m.provider === route.provider && m.id === route.model) }
    const effortList = model => (model?.reasoning?.efforts || []).filter(e => typeof e.id === 'string' && e.id)

    function ModelPicker({ title, catalog, current, allowLead, inheritLabel, inheritHint, onPick, onClose }) {
      const dialog = useRef(null), search = useRef(null), listId = useId()
      const [query, setQuery] = useState(''), [active, setActive] = useState(0)
      const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
      const matches = text => words.every(word => text.toLocaleLowerCase().includes(word))
      const options = [...(allowLead && matches(inheritLabel + ' 当前对话 主模型 默认 lead') ? [{ lead: true, id: 'lead', name: inheritLabel, providerName: '当前任务的主模型' }] : []),
        ...catalogRows(catalog).filter(m => matches([m.name, m.id, m.providerName, m.provider].join(' ')))]
      useEffect(() => { dialog.current.showModal(); search.current?.focus(); return () => dialog.current?.close() }, [])
      useEffect(() => { setActive(0) }, [query, catalog.groups])
      useEffect(() => { document.getElementById(listId + '-' + active)?.scrollIntoView({ block: 'nearest' }) }, [active, listId])
      const pick = row => { onPick(row); onClose() }
      const keyboard = e => {
        if (e.nativeEvent?.isComposing) return
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onClose(); return }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); setActive(i => options.length ? (i + (e.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length : 0) }
        if (e.key === 'Enter') { e.preventDefault(); if (options[active]) pick(options[active]) }
      }
      return h('dialog', { ref: dialog, className: 'dps-modelDialog', 'aria-label': '为' + title + '选择模型',
        onCancel: e => { e.preventDefault(); onClose() }, onKeyDown: keyboard },
        h('div', { className: 'dps-pickerHeader' }, h('div', null, h('h3', null, '选择' + title + '模型'), h('p', null, '来自 DPH 已配置的模型')),
          h('button', { type: 'button', className: 'dps-iconButton', 'aria-label': '关闭模型选择', onClick: onClose }, '×')),
        h('div', { className: 'dps-pickerSearch' }, h('input', { ref: search, type: 'search', value: query, placeholder: '搜索模型或 Provider…',
          role: 'combobox', 'aria-label': '搜索模型或 Provider', 'aria-expanded': true, 'aria-controls': listId, 'aria-autocomplete': 'list',
          'aria-activedescendant': options[active] ? listId + '-' + active : undefined, onChange: e => setQuery(e.target.value) })),
        h('div', { className: 'dps-pickerMeta' }, h('span', { role: 'status' }, catalog.status === 'loading' ? '正在读取模型列表…' : `${options.length} 个选项`),
          h('button', { type: 'button', className: 'dps-textButton', disabled: catalog.status === 'loading', onClick: catalog.load }, '刷新列表')),
        catalog.error ? h('p', { className: 'dps-pickerNotice', role: 'alert' }, catalog.error, catalog.groups.length ? ' 下方保留上次读取的模型。' : '') : null,
        catalog.failures.length ? h('p', { className: 'dps-pickerNotice', role: 'status' }, '部分 Provider 未能加载：' + catalog.failures.map(f => f.name || f.id).join('、') + '。其余模型仍可选择。') : null,
        h('div', { className: 'dps-modelList', id: listId, role: 'listbox', 'aria-label': 'DPH 模型列表' },
          options.length ? options.map((row, i) => {
            const selected = row.lead ? current.mode === 'lead' : current.mode !== 'lead' && row.provider === current.provider && row.id === current.model
            return h('button', { type: 'button', key: row.lead ? 'lead' : JSON.stringify([row.provider, row.id]), id: listId + '-' + i,
              role: 'option', 'aria-selected': selected, tabIndex: -1, className: 'dps-modelOption' + (active === i ? ' dps-modelActive' : ''),
              onMouseEnter: () => setActive(i), onClick: () => pick(row) },
              h('span', { className: 'dps-modelGlyph', 'aria-hidden': true }, row.lead ? 'L' : row.providerName.slice(0, 1).toUpperCase()),
              h('span', { className: 'dps-modelIdentity' }, h('b', null, row.name || row.id), h('small', null, row.lead ? inheritHint : row.providerName + ' · ' + row.id)),
              h('span', { className: 'dps-modelCheck', 'aria-hidden': true }, selected ? '✓' : ''))
          }) : h('div', { className: 'dps-empty' }, h('b', null, catalog.status === 'loading' ? '正在加载…' : query ? '没有找到匹配模型' : catalog.status === 'error' ? '暂时无法读取模型' : '还没有可选模型'),
            h('p', null, query ? '试试模型 ID 或 Provider 名称。' : catalog.status === 'error' ? '请刷新列表重试，或关闭此窗口后使用“手动配置”。' : '先到 DPH“设置 → 模型”完成配置，再刷新列表。'))),
        h('p', { className: 'dps-pickerFooter' }, '↑ ↓ 移动  ·  Enter 选择  ·  Esc 返回'))
    }

    function ModelRouteForm({ role, title, description, catalog, number }) {
      const snapshot = useSettings(), cfg = snapshot.value || {}, defaults = ROLE_DEFAULTS[role]
      const canInherit = role === 'impl' || role === 'reviewer'
      const inheritLabel = role === 'impl' ? '沿用当前对话模型' : '沿用 Lead'
      const inheritHint = role === 'impl' ? '默认 · 跟随 Provider、模型与推理强度' : '默认 · 不新增审查调用'
      const names = { provider: role + 'Provider', model: role + 'Model', effort: role + 'Effort', ...(canInherit ? { mode: role + 'Mode' } : {}) }
      const saved = { provider: cfg[names.provider] ?? '', model: cfg[names.model] ?? defaults.model, effort: cfg[names.effort] ?? defaults.effort,
        ...(canInherit ? { mode: role === 'impl' ? implMode(cfg) : cfg.reviewerMode ?? 'lead' } : {}) }
      const savedKey = JSON.stringify(saved)
      const [draft, setDraft] = useState(saved), [pending, setPending] = useState(false), [message, setMessage] = useState(''), [error, setError] = useState(''), [picker, setPicker] = useState(false)
      const dirty = JSON.stringify(draft) !== savedKey, inherited = canInherit && draft.mode === 'lead'
      const missing = inherited ? [] : missingModelFields(draft.provider, draft.model)
      useEffect(() => { setDraft(JSON.parse(savedKey)) }, [savedKey])
      const disabled = pending || snapshot.status !== 'ready' || !snapshot.writable || snapshot.mode !== 'host'
      const model = routeModel(catalog, draft), efforts = effortList(model)
      const unlistedEffort = draft.effort && !efforts.some(e => e.id === draft.effort)
      const edit = (field, value) => { setDraft(previous => ({ ...previous, [field]: value })); setMessage(''); setError('') }
      const pick = row => {
        setMessage(''); setError('')
        if (row.lead) { setDraft(old => ({ ...old, mode: 'lead' })); return }
        setDraft(old => ({ ...old, provider: row.provider, model: row.id, effort: effortList(row).some(e => e.id === old.effort) ? old.effort : '', ...(canInherit ? { mode: 'model' } : {}) }))
      }
      const save = async event => {
        event.preventDefault(); setError(''); setMessage('')
        if (missing.length) { setError(routeHint([title + '的 ' + missing.join(' 和 ')])); return }
        setPending(true)
        try { await writeSettings(Object.fromEntries(Object.entries(names).map(([key, field]) => [field, draft[key].trim()]))); setMessage('已保存') }
        catch (e) { setError(String(e.message || e)) } finally { setPending(false) }
      }
      return h('form', { className: 'dps-roleCard', onSubmit: save, 'aria-label': title + '配置' },
        h('div', { className: 'dps-roleHeading' }, h('span', { className: 'dps-roleNumber', 'aria-hidden': true }, number), h('div', null, h('h3', null, title), h('p', null, description)),
          h('span', { className: 'dps-routeState' }, dirty ? '待保存' : inherited ? (role === 'impl' ? '跟随对话' : '沿用 Lead') : missing.length ? '待配置' : '当前配置')),
        h('div', { className: 'dps-roleControls', style: inherited ? { gridTemplateColumns: 'minmax(0, 1fr)' } : undefined },
          h('div', { className: 'dps-modelField' }, h('span', { className: 'dps-inputCaption', id: 'dps-caption-' + role }, role === 'reviewer' ? '审查模型' : '模型'),
            h('button', { type: 'button', className: 'dps-modelTrigger', disabled, 'aria-label': '选择' + title + '模型', 'aria-haspopup': 'dialog', onClick: () => { setPicker(true); catalog.load() } },
              h('span', { className: 'dps-modelGlyph', 'aria-hidden': true }, inherited ? 'L' : (model?.providerName || draft.provider || 'M').slice(0, 1).toUpperCase()),
              h('span', { className: 'dps-modelIdentity' }, h('b', null, inherited ? (role === 'impl' ? inheritLabel : '当前 Lead') : model?.name || draft.model || '选择 DPH 模型'),
                h('small', null, inherited ? (role === 'impl' ? '包含 Provider 与推理强度' : '跟随主模型 · 不新增调用') : model?.providerName || draft.provider || '先从 DPH 中选择 Provider 与模型')),
              h(IconChevronDownOutline14, null))),
          !inherited ? h('label', { className: 'dps-effortField' }, h('span', { className: 'dps-inputCaption' }, '推理强度'),
            h('select', { value: draft.effort, disabled, 'aria-label': title + '推理强度', onChange: e => edit('effort', e.target.value) },
              h('option', { value: '' }, '模型默认'), ...efforts.map(e => h('option', { key: e.id, value: e.id }, e.name || e.id)),
              unlistedEffort ? h('option', { value: draft.effort }, draft.effort + '（当前配置）') : null)) : null),
        !inherited && !model && draft.provider && draft.model ? h('p', { className: 'dps-hint' }, '当前配置未在本次目录中列出；已保留原值，可刷新列表或手动检查。') : null,
        !inherited && unlistedEffort && model ? h('p', { className: 'dps-hint' }, '宿主未列出当前推理强度。可选择“模型默认”或该模型支持的选项。') : null,
        !inherited ? h('details', { className: 'dps-manual' }, h('summary', null, '手动配置'), h('p', { className: 'dps-hint' }, '用于目录未列出的自定义模型。凭据继续由 DPH 管理。'),
          h('div', { className: 'dps-route' }, ...[['provider', title + ' Provider'], ['model', title + '模型 ID'], ['effort', title + '推理强度 ID']].map(([key, label]) => h('label', { key }, label,
            h('input', { value: draft[key], disabled, 'aria-label': label, autoComplete: 'off', onChange: e => edit(key, e.target.value) }))))) : null,
        h('div', { className: 'dps-roleFooter' }, h('span', { className: message ? 'dps-saved' : 'dps-hint', role: message ? 'status' : undefined }, message || (dirty ? '更改保存后生效' : inherited ? (role === 'impl' ? '团队启动时跟随当前对话，支持手动选择其他模型' : 'Lead 负责审查和最终验收') : missing.length ? '尚未配置 ' + missing.join(' 和 ') + '，请选择模型并保存' : '从 DPH 模型目录选择')),
          h('div', { className: 'dps-actions' }, dirty ? h('button', { type: 'button', className: 'dps-btn dps-btnSecondary', disabled, onClick: () => { setDraft(JSON.parse(savedKey)); setError(''); setMessage('') } }, '撤销更改') : null,
            h('button', { type: 'submit', className: 'dps-btn dps-btnPrimary', disabled: disabled || !dirty, 'aria-label': '保存' + title + '配置' }, pending ? '保存中…' : '保存'))),
        error ? h('p', { className: 'dps-error', role: 'alert' }, error) : null,
        picker ? h(ModelPicker, { title, catalog, current: draft, allowLead: canInherit, inheritLabel, inheritHint, onPick: pick, onClose: () => setPicker(false) }) : null)
    }

    const BUDGET_MODES = [
      ['unlimited', '不做限制', '不启用子 agent 限额'],
      ['manual', '手动填写', '每个子 agent 独立使用'],
      ['auto', 'Lead 自动分配', '派发前按子任务分别评估'],
    ]
    const budgetMode = value => ['unlimited', 'manual', 'auto'].includes(value) ? value : 'unlimited'
    const positiveBudget = value => /^\d+$/.test(String(value).trim()) && Number.isSafeInteger(Number(value)) && Number(value) > 0
    const budgetNumber = value => positiveBudget(value) ? Number(value).toLocaleString('zh-CN') : '未配置'

    function BudgetSettings() {
      const snapshot = useSettings(), cfg = snapshot.value || {}, id = useId()
      const saved = { mode: budgetMode(cfg.workerBudgetMode), totalTokens: String(cfg.workerTokenLimit ?? 600000), maxCalls: String(cfg.workerCallLimit ?? 28) }
      const savedKey = JSON.stringify(saved)
      const [draft, setDraft] = useState(saved), [pending, setPending] = useState(false), [message, setMessage] = useState(''), [error, setError] = useState(''), [validated, setValidated] = useState(false)
      useEffect(() => { setDraft(JSON.parse(savedKey)); setValidated(false) }, [savedKey])
      const manual = draft.mode === 'manual'
      const dirty = draft.mode !== saved.mode || (manual && (draft.totalTokens !== saved.totalTokens || draft.maxCalls !== saved.maxCalls))
      const disabled = pending || snapshot.status !== 'ready' || !snapshot.writable || snapshot.mode !== 'host'
      const errors = { totalTokens: positiveBudget(draft.totalTokens) ? '' : '请输入大于 0 的安全整数。', maxCalls: positiveBudget(draft.maxCalls) ? '' : '请输入大于 0 的安全整数。' }
      const edit = (field, value) => { setDraft(previous => ({ ...previous, [field]: value })); setMessage(''); setError(''); setValidated(false) }
      const save = async event => {
        event.preventDefault(); setError(''); setMessage(''); setValidated(true)
        if (manual && (errors.totalTokens || errors.maxCalls)) { setError('请填写有效的子 agent token 限额和调用次数。'); return }
        setPending(true)
        try {
          // Unlimited/auto deliberately preserve and ignore stored manual values.
          await writeSettings({ workerBudgetMode: draft.mode, ...(manual ? { workerTokenLimit: Number(draft.totalTokens), workerCallLimit: Number(draft.maxCalls) } : {}) })
          setMessage('已保存 · 用于下一次子任务派发')
        } catch (e) { setError(String(e.message || e)) } finally { setPending(false) }
      }
      return h('form', { className: 'dps-roleCard', onSubmit: save, noValidate: true, 'aria-label': '子 agent 限额配置' },
        h('div', { className: 'dps-roleHeading' }, h('span', { className: 'dps-roleNumber', 'aria-hidden': true }, 'A'), h('div', null, h('h3', null, '子 agent 限额'), h('p', null, '每个子 agent 独立计量；Lead 主对话不受此限额约束。')),
          h('span', { className: 'dps-routeState' }, dirty ? '待保存' : '当前配置')),
        h('fieldset', { className: 'dps-budgetModes', 'aria-label': '子 agent 限额模式', disabled }, ...BUDGET_MODES.map(([mode, title, description]) => h('label', { className: 'dps-budgetMode', key: mode },
          h('input', { type: 'radio', name: id + '-budget-mode', value: mode, checked: draft.mode === mode, onChange: () => edit('mode', mode), 'aria-label': title }),
          h('span', null, h('b', null, title), h('small', null, description))))),
        manual ? h('div', { className: 'dps-budgetFields' }, ...[['totalTokens', '每个子 agent 的 token 限额', '该子 agent 的输入、缓存和输出合计'], ['maxCalls', '每个子 agent 的调用次数', '每个子 agent 各自拥有，不共享']].map(([field, label, hint]) => h('label', { key: field }, label,
          h('input', { type: 'text', inputMode: 'numeric', autoComplete: 'off', spellCheck: false, value: draft[field], disabled, 'aria-label': label, 'aria-invalid': validated && !!errors[field], 'aria-describedby': id + '-' + field + '-hint', onChange: e => edit(field, e.target.value) }),
          h('span', { id: id + '-' + field + '-hint', className: validated && errors[field] ? 'dps-error' : 'dps-hint' }, validated && errors[field] ? errors[field] : hint))))
          : h('div', { className: 'dps-budgetInfo', role: 'note' }, draft.mode === 'auto'
            ? '当前 Lead 读完任务后，在正常派发流程中决定各子 agent 的 token 限额、调用次数和理由；不额外调用评估模型。'
            : '不设置子 agent 的 token 或调用次数上限，也不进行预算评估。手动填写过的值不参与限制。'),
        manual ? h('p', { className: 'dps-hint' }, '初始参考：1,200,000 token / 50 次。这是异常护栏而非精确计划；触轨后 worker 会安全停车并交回 Lead 裁决是否继续。') : null,
        h('p', { className: 'dps-hint' }, '适用于 DPH 子 agent，包括固定团队外的子任务。各自的 CM 计入各自额度；Lead 主调用和主 CM 不受限制。'),
        h('p', { className: 'dps-hint' }, 'Token 在请求前估算、完成后结算，最后一次请求可能超出估算额度，随后停止继续调用。角色超时仍单独设置。'),
        h('div', { className: 'dps-roleFooter' }, h('span', { className: message ? 'dps-saved' : 'dps-hint', role: message ? 'status' : undefined }, message || (dirty ? '更改保存后生效' : '此处显示配置，不代表运行中的剩余额度')),
          h('div', { className: 'dps-actions' }, dirty ? h('button', { type: 'button', className: 'dps-btn dps-btnSecondary', disabled, onClick: () => { setDraft(JSON.parse(savedKey)); setError(''); setMessage(''); setValidated(false) } }, '撤销更改') : null,
            h('button', { type: 'submit', className: 'dps-btn dps-btnPrimary', disabled: disabled || !dirty, 'aria-label': '保存子 agent 限额' }, pending ? '保存中…' : '保存'))),
        error ? h('p', { className: 'dps-error', role: 'alert' }, error) : null)
    }

    const REWORK_MODES = [
      ['unlimited', '不做限制', '返工不设累计 token／调用上限（默认）'],
      ['fixed', '固定限额', '每次返工各自独立使用'],
    ]
    const reworkMode = value => ['unlimited', 'fixed'].includes(value) ? value : 'unlimited'

    function ReworkBudgetSettings() {
      const snapshot = useSettings(), cfg = snapshot.value || {}, id = useId()
      const saved = { mode: reworkMode(cfg.reworkBudgetMode), totalTokens: String(cfg.reworkTokenLimit ?? 600000), maxCalls: String(cfg.reworkCallLimit ?? 28) }
      const savedKey = JSON.stringify(saved)
      const [draft, setDraft] = useState(saved), [pending, setPending] = useState(false), [message, setMessage] = useState(''), [error, setError] = useState(''), [validated, setValidated] = useState(false)
      useEffect(() => { setDraft(JSON.parse(savedKey)); setValidated(false) }, [savedKey])
      const fixed = draft.mode === 'fixed'
      const dirty = draft.mode !== saved.mode || (fixed && (draft.totalTokens !== saved.totalTokens || draft.maxCalls !== saved.maxCalls))
      const disabled = pending || snapshot.status !== 'ready' || !snapshot.writable || snapshot.mode !== 'host'
      const errors = { totalTokens: positiveBudget(draft.totalTokens) ? '' : '请输入大于 0 的安全整数。', maxCalls: positiveBudget(draft.maxCalls) ? '' : '请输入大于 0 的安全整数。' }
      const edit = (field, value) => { setDraft(previous => ({ ...previous, [field]: value })); setMessage(''); setError(''); setValidated(false) }
      const save = async event => {
        event.preventDefault(); setError(''); setMessage(''); setValidated(true)
        if (fixed && (errors.totalTokens || errors.maxCalls)) { setError('请填写有效的返工 token 限额和调用次数。'); return }
        setPending(true)
        try {
          // Unlimited deliberately preserves and ignores stored fixed values.
          await writeSettings({ reworkBudgetMode: draft.mode, ...(fixed ? { reworkTokenLimit: Number(draft.totalTokens), reworkCallLimit: Number(draft.maxCalls) } : {}) })
          setMessage('已保存 · 用于下一次返工')
        } catch (e) { setError(String(e.message || e)) } finally { setPending(false) }
      }
      return h('form', { className: 'dps-roleCard', onSubmit: save, noValidate: true, 'aria-label': '返工限额配置' },
        h('div', { className: 'dps-roleHeading' }, h('span', { className: 'dps-roleNumber', 'aria-hidden': true }, 'B'), h('div', null, h('h3', null, '返工限额'), h('p', null, '返工交回原模型实现者继续修复；每次返工独立计量，旧用量不清零。')),
          h('span', { className: 'dps-routeState' }, dirty ? '待保存' : '当前配置')),
        h('fieldset', { className: 'dps-budgetModes', 'aria-label': '返工限额模式', disabled }, ...REWORK_MODES.map(([mode, title, description]) => h('label', { className: 'dps-budgetMode', key: mode },
          h('input', { type: 'radio', name: id + '-rework-mode', value: mode, checked: draft.mode === mode, onChange: () => edit('mode', mode), 'aria-label': title }),
          h('span', null, h('b', null, title), h('small', null, description))))),
        fixed ? h('div', { className: 'dps-budgetFields' }, ...[['totalTokens', '每次返工的 token 限额', '该次返工的输入、缓存和输出合计'], ['maxCalls', '每次返工的调用次数', '每次返工各自拥有，不共享']].map(([field, label, hint]) => h('label', { key: field }, label,
          h('input', { type: 'text', inputMode: 'numeric', autoComplete: 'off', spellCheck: false, value: draft[field], disabled, 'aria-label': label, 'aria-invalid': validated && !!errors[field], 'aria-describedby': id + '-' + field + '-hint', onChange: e => edit(field, e.target.value) }),
          h('span', { id: id + '-' + field + '-hint', className: validated && errors[field] ? 'dps-error' : 'dps-hint' }, validated && errors[field] ? errors[field] : hint))))
          : h('div', { className: 'dps-budgetInfo', role: 'note' }, '返工不设累计 token 或调用次数上限，满足原任务要求后停止。固定限额填过的值不参与限制。'),
        h('p', { className: 'dps-hint' }, '首次实现遵循上方子 agent 限额；此处只影响返工。首次实现和各次返工的实际消耗分别保留。用户停止和已设置的角色超时仍有效。'),
        h('div', { className: 'dps-roleFooter' }, h('span', { className: message ? 'dps-saved' : 'dps-hint', role: message ? 'status' : undefined }, message || (dirty ? '更改保存后生效' : '此处显示配置，不代表运行中的剩余额度')),
          h('div', { className: 'dps-actions' }, dirty ? h('button', { type: 'button', className: 'dps-btn dps-btnSecondary', disabled, onClick: () => { setDraft(JSON.parse(savedKey)); setError(''); setMessage(''); setValidated(false) } }, '撤销更改') : null,
            h('button', { type: 'submit', className: 'dps-btn dps-btnPrimary', disabled: disabled || !dirty, 'aria-label': '保存返工限额' }, pending ? '保存中…' : '保存'))),
        error ? h('p', { className: 'dps-error', role: 'alert' }, error) : null)
    }

    function ModelSummary() {
      const cfg = useSettings().value || {}
      return h('dl', { className: 'dps-summary' }, h('dt', null, 'Lead'), h('dd', null, '沿用当前任务的主模型'),
        ...[['impl', '实现者'], ['test', '测试者'], ['reviewer', 'Reviewer'], ['cm', 'CM']].flatMap(([role, title]) => [h('dt', { key: role + '-name' }, title),
          h('dd', { key: role }, role === 'impl' && implMode(cfg) === 'lead' ? '沿用当前对话模型' : role === 'reviewer' && cfg.reviewerMode !== 'model' ? '沿用 Lead' : (cfg[role + 'Provider'] || '未配置 Provider') + ' / ' + (cfg[role + 'Model'] || ROLE_DEFAULTS[role].model))]))
    }

    function RoleSettings({ close }) {
      const snapshot = useSettings(), catalog = useModelCatalog(), [tab, setTab] = useState('models')
      const tabs = [['models', '模型分工'], ['rules', '预算与运行'], ['advanced', '高级设置']]
      const changeTab = (event, i) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
        event.preventDefault()
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (i + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
        setTab(tabs[next][0]); document.getElementById('dps-tab-' + tabs[next][0])?.focus()
      }
      return h('section', { className: 'dps-settings', 'aria-label': 'DPswarm 模型与协作设置' },
        h('div', { className: 'dps-pageHeader' }, h('span', { className: 'dps-pageMark', 'aria-hidden': true }, h(Compass16, null)), h('div', null, h('h2', null, 'DPswarm'), h('p', null, '为每个职责，选择合适的模型。'))),
        h('div', { className: 'dps-tabs', role: 'tablist', 'aria-label': 'DPswarm 设置分类' }, tabs.map(([id, label], i) => h('button', { type: 'button', role: 'tab', key: id,
          id: 'dps-tab-' + id, 'aria-selected': tab === id, 'aria-controls': 'dps-panel-' + id, tabIndex: tab === id ? 0 : -1,
          onClick: () => setTab(id), onKeyDown: e => changeTab(e, i) }, label))),
        snapshot.status !== 'ready' || !snapshot.writable || snapshot.mode !== 'host' ? h('p', { className: 'dps-error', role: 'alert' }, '当前设置不可写，请连接本机 DPH 后重试。') : null,
        h('div', { role: 'tabpanel', id: 'dps-panel-models', 'aria-labelledby': 'dps-tab-models', hidden: tab !== 'models', className: 'dps-tabPanel' },
          h('div', { className: 'dps-leadStrip' }, h('span', { className: 'dps-modelGlyph', 'aria-hidden': true }, 'L'), h('div', null, h('b', null, 'Lead · 当前主模型'), h('p', null, '沿用任务中的模型，负责分工、必要返工与最终验收。')),
            typeof close === 'function' ? h('button', { type: 'button', className: 'dps-textButton', onClick: close, 'aria-label': '返回任务调整主模型' }, '回到任务选择 ↗') : null),
          h('div', { className: 'dps-sectionLabel' }, h('span', null, '协作角色'), h('span', null, '只在任务开关启用后执行')),
          h(ModelRouteForm, { role: 'impl', title: '实现者', description: '默认跟随对话模型，形成可交付的代码修改', number: '01', catalog }),
          h(ModelRouteForm, { role: 'test', title: '测试者', description: '独立验证实现与测试结果', number: '02', catalog }),
          h(ModelRouteForm, { role: 'reviewer', title: 'Reviewer', description: '审查候选与证据，默认由 Lead 承担', number: '03', catalog }),
          h('div', { className: 'dps-sectionLabel' }, h('span', null, '上下文管理'), h('span', null, '独立开关 · 单 agent 也可用')),
          h(ModelRouteForm, { role: 'cm', title: 'CM', description: '在角色上下文窗口接近满时整理历史，保留原始记录', number: 'CM', catalog }),
          h('p', { className: 'dps-pageNote' }, '实现者默认跟随对话模型与推理强度；也可手动指定。保存不会开启任务，运行中的团队保持启动时配置。')),
        h('div', { role: 'tabpanel', id: 'dps-panel-rules', 'aria-labelledby': 'dps-tab-rules', hidden: tab !== 'rules', className: 'dps-tabPanel' },
          h(BudgetSettings),
          h('div', { className: 'dps-settingsCard' }, h('h3', null, '预算内及时收尾'), h('p', { className: 'dps-hint' }, '额度是上限，不必用满。剩余额度接近下一次完整请求的成本时，子 agent 优先返回已完成内容、文件位置和未完成事项；收尾期间不继续调用工具。已保存文件不等于已通过验收，最终仍由 Lead 核验。')),
          h(ReworkBudgetSettings),
          h('div', { className: 'dps-settingsCard' }, h('h3', null, '按任务开启'), h('p', { className: 'dps-hint' }, '在输入框旁的罗盘菜单中一键开启：固定团队与 CM 同时对本任务生效。默认关闭；开启要求团队角色与 CM 路由均已配置。')),
          h('div', { className: 'dps-settingsCard' }, h('h3', null, '固定顺序与审查'), h('p', { className: 'dps-hint' }, '实现者 → 测试者 → Reviewer → Lead 最终验收。Reviewer 默认由 Lead 承担，不增加模型调用；指定独立模型后才额外执行审查。其输出是待核对意见，不自动接受交付。'),
            h('div', { className: 'dps-route' }, h(SettingsField, { field: 'workerTimeoutSeconds', label: '每个角色超时（秒）', type: 'number' }))),
          h('div', { className: 'dps-settingsCard' }, h('h3', null, 'CM 与配置生效'), h('p', { className: 'dps-hint' }, 'CM 按每个角色当前模型的上下文窗口分别判断，估算占用达到 80% 时才整理较早历史。窗口容量从 DPH 模型注册表读取，与每 worker 的累计 token／调用预算无关。'),
            h('p', { className: 'dps-hint' }, '保留最近 4 条消息与完整工具配对；可压缩内容太少时跳过，失败保留原文。窗口容量未知时不主动压缩，宿主原生保护仍保留。每个 agent 每轮最多 12 次。'),
            h('p', { className: 'dps-hint' }, '独立 CM 的设置用于下一次压缩；团队配置从启动冻结到验收结束。关闭 CM 后再开启，从下次团队运行生效。有待验收交付时，先完成验收或终止，再更换 Lead。'))),
        h('div', { role: 'tabpanel', id: 'dps-panel-advanced', 'aria-labelledby': 'dps-tab-advanced', hidden: tab !== 'advanced', className: 'dps-tabPanel' },
          h('div', { className: 'dps-settingsCard' }, h('h3', null, '连接与安装'), h('p', { className: 'dps-hint' }, '以下字段离开输入框后保存。模型 API 地址和凭据在 DPH“设置 → 模型”中管理。'), h('div', { className: 'dps-route' },
            h(SettingsField, { field: 'sidecarUrl', label: '控制服务地址' }), h(SettingsField, { field: 'pythonCmd', label: 'Python 路径' }),
            h(SettingsField, { field: 'workspace', label: '运行状态目录（可选）' }), h(SettingsField, { field: 'dpswarmDir', label: 'Python 源码目录（可选）' })))))
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
      if (!s) return { phase, connected, waiting: connected, chips: [] }
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
      const cls = phase === 'off' ? 'dps-dot dps-launchDotOff' : phase === 'ok' ? 'dps-dot dps-dotOk' : phase === 'bad' ? 'dps-dot dps-dotBad'
        : 'dps-dot dps-dotWait'
      const label = phase === 'off' ? L.off : phase === 'version' ? L.version : phase === 'ok' ? L.ok : phase === 'bad' ? L.bad : L.wait
      return h('span', { className: 'dps-badge' }, h('span', { className: cls }), label)
    }

    function chipsRow(chips) {
      return h('div', { className: 'dps-chips' },
        chips.map(([label, value]) => h('span', { className: 'dps-chip', key: String(label) },
          String(label), ' ', h('b', null, String(value)))))
    }

    // DSH 0.1 exposes the section slot but no public openSection service.
    // Navigate its visible, accessible controls only; fail closed if the shell changes.
    // No host internals, credentials, config writes or hashed CSS selectors are used.
    function openHostSettings(targetTab) {
      return new Promise((resolve, reject) => {
        let timer, observer, done = false, opened = false, selected = false
        const finish = error => { if (done) return; done = true; clearTimeout(timer); observer?.disconnect(); error ? reject(error) : resolve() }
        const advance = () => {
          if (done) return
          if (targetTab) {
            const tab = document.getElementById('dps-tab-' + targetTab)
            if (tab?.getClientRects().length) { tab.click(); finish(); return }
          }
          const nav = [...document.querySelectorAll('[role="dialog"] nav button')].filter(b => b.textContent.trim() === 'DPswarm' && b.getClientRects().length)
          if (nav.length === 1 && !selected) { selected = true; nav[0].click(); if (!targetTab) finish(); return }
          if (!opened) {
            const trigger = [...document.querySelectorAll('button[aria-haspopup="dialog"]')].filter(b => ['设置', 'Settings'].includes(b.textContent.trim()) && b.getClientRects().length)
            if (trigger.length === 1) { opened = true; trigger[0].click() }
          }
        }
        observer = new MutationObserver(advance)
        observer.observe(document.body, { childList: true, subtree: true })
        timer = setTimeout(() => finish(new Error('请打开 DPH 左下角「设置」，选择「DPswarm」。')), 5000)
        advance()
      })
    }

    function SettingsShortcut({ budget = false }) {
      const [busy, setBusy] = useState(false), [error, setError] = useState('')
      return h('div', null, h('button', { type: 'button', className: budget ? 'dps-textButton' : 'dps-btn dps-btnPrimary', disabled: busy,
        onClick: async () => { setBusy(true); setError(''); try { await openHostSettings(budget ? 'rules' : undefined) } catch (e) { setError(e.message) } finally { setBusy(false) } } }, busy ? '正在打开设置…' : budget ? '调整限额 ↗' : '模型与协作设置'),
        error ? h('p', { className: 'dps-error', role: 'alert' }, error) : null)
    }

    function openPanelLink(sessionId) {
      const url = panelUrl()
      if (!url) return h('span', { className: 'dps-hint' }, 'Sidecar URL unavailable in host settings')
      return h('a', { className: 'dps-btn', href: url + '/' + (sessionId ? '?session=' + encodeURIComponent(sessionId) : '') + '#dph=' + encodeURIComponent(location.origin),
        target: '_blank', rel: 'noreferrer', title: url }, L.open)
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
                  ? (status.waiting ? h('p', { className: 'dps-hint' }, '服务已就绪，等待主 agent 启动当前任务的固定协作。') : chipsRow(status.chips))
                  : h('p', { className: 'dps-hint' },
                      status.phase === 'off' ? '协作默认关闭，服务在启用后按需启动。' : status.phase === 'version' ? '此地址运行旧版控制服务，请更换端口或更新为 session_server。' : status.phase === 'wait' ? L.hintStarting : L.offline)),
              h('div', { className: 'dps-field' },
                h('p', { className: 'dps-hint' }, L.hint)),
              h(ModelSummary, null),
              h('p', { className: 'dps-hint' }, '模型与协作选项位于 DPH 设置中的 DPswarm 子页面。'),
              h('div', { className: 'dps-footer' }, h(SettingsShortcut), openPanelLink()))
          : null)
    }

    /** 🧭 弹层：主开关 + 关键参数 + 总 token 占比条；明细在后台面板与设置页。 */
    function DpswarmPanel({ sessionId }) {
      const [phase, st] = useSidecar(4000, sessionId)
      let status
      try { status = readStatus(phase, st) } catch (e) { status = { connected: false, chips: [] } }
      return h('div', { className: 'dps-popCard' },
        h('div', { className: 'dps-popHead' },
          h('span', { className: 'dps-popName' }, L.name),
          statusBadge(status.phase)),
        h(DpswarmSwitch, { sessionId }),
        h(TeamModePicker, { sessionId }),
        h(KeyParams, null),
        h(TokenShareBar, { status: st }),
        status.connected
          ? (status.waiting ? h('p', { className: 'dps-hint' }, '服务已就绪，等待主 agent 启动当前任务的固定协作。') : null)
          : phase === 'off' ? null
            : h('p', { className: 'dps-hint' },
                status.phase === 'version' ? '此地址运行旧版控制服务，请更换端口或更新为 session_server。' : status.phase === 'wait' ? L.hintStarting : L.offline),
        h('div', { className: 'dps-popFoot' }, h(SettingsShortcut), openPanelLink(sessionId)))
    }

    /** 关键参数一览：模型分工、限额与超时；完整配置与运行明细在设置页和后台面板。 */
    function KeyParams() {
      const cfg = useSettings().value || {}
      const facts = [
        ['实现者', implMode(cfg) === 'lead' ? '跟随对话' : (cfg.implModel || '待配置')],
        ['测试者', cfg.testModel || '待配置'],
        ['Reviewer', cfg.reviewerMode === 'model' ? (cfg.reviewerModel || '待配置') : '沿用 Lead'],
        ['CM', cfg.cmModel || 'deepseek-v4-flash'],
        ['限额', { unlimited: '不做限制', manual: '手动', auto: 'Lead 分配' }[cfg.workerBudgetMode] || '不做限制'],
        ['返工', reworkMode(cfg.reworkBudgetMode) === 'fixed' ? '固定 ' + budgetNumber(cfg.reworkTokenLimit) + ' / ' + budgetNumber(cfg.reworkCallLimit) + ' 次' : '不限'],
        ['超时', String(cfg.workerTimeoutSeconds ?? 600) + 's'],
      ]
      return h('dl', { className: 'dps-summary dps-keyParams', 'aria-label': '关键参数' },
        facts.flatMap(([k, v]) => [h('dt', { key: k }, k), h('dd', { key: k + '-v' }, v)]))
    }

    /** 总 token 消耗占比条：按角色分段（并行时细化到子任务，含各自 CM），悬停看明细；Lead 主对话不计量。 */
    function TokenShareBar({ status }) {
      const view = status?.worker_diagnostics
      if (!view || view.available !== true || !Array.isArray(view.workers) || !view.workers.length) return null
      const names = { implementer: '实现者', tester: '测试者', reviewer: 'Reviewer', worker: '子 agent' }
      const colors = { implementer: 'var(--dsw-alias-brand-primary)', tester: 'var(--dsw-alias-state-success-primary)', reviewer: 'var(--dsw-alias-state-warn-primary)', worker: 'var(--dsw-alias-label-tertiary)' }
      const amount = value => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('zh-CN') : '未知'
      const byKey = new Map(), calls = new Map(), remaining = new Map(), labels = new Map()
      let grand = 0
      for (const row of view.workers) {
        const base = names[row.role] ? row.role : 'worker'
        const key = row.subtask ? `${base}:${row.subtask}` : base
        labels.set(key, names[base] + (row.subtask ? '·' + row.subtask : ''))
        const tokens = Number.isSafeInteger(row.observed_tokens_lower_bound) ? row.observed_tokens_lower_bound : 0
        byKey.set(key, (byKey.get(key) || 0) + tokens); grand += tokens
        calls.set(key, (calls.get(key) || 0) + (Number.isSafeInteger(row.calls_used) ? row.calls_used : 0))
        if (Number.isSafeInteger(row.remaining_tokens)) remaining.set(key, (remaining.get(key) || 0) + row.remaining_tokens)
      }
      if (grand <= 0) return null
      const rank = key => ['implementer', 'tester', 'reviewer', 'worker'].indexOf(key.split(':')[0])
      const order = [...byKey.keys()].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
      return h('div', { className: 'dps-tokenShare' },
        h('div', { className: 'dps-tokenBar', role: 'img', 'aria-label': 'worker 总消耗 ' + amount(grand) + ' token，按角色分段' },
          order.map(key => h('i', { key,
            style: { width: (byKey.get(key) / grand * 100).toFixed(2) + '%', background: colors[key.split(':')[0]] },
            title: labels.get(key) + '：' + amount(byKey.get(key)) + ' token · ' + amount(calls.get(key)) + ' 次调用'
              + (remaining.has(key) ? ' · 剩余 ' + amount(remaining.get(key)) + ' token' : '') }))),
        h('p', { className: 'dps-hint' }, '总消耗 ', h('b', null, amount(grand)), ' token（', order.map(key => labels.get(key)).join(' / '), '），悬停分段查看明细；含各 worker 自身 CM，Lead 主对话不计量。'))
    }

    /** composer 工具行的罗盘启动按钮：角落三态状态灯，点开菜单面弹层。 */
    function DpswarmLaunch({ sessionId }) {
      const snapshot = useSettings()
      const teamOn = (snapshot.value?.enabledSessions || []).includes(sessionId)
      const cmOn = (snapshot.value?.cmEnabledSessions || []).includes(sessionId)
      const partial = teamOn !== cmOn
      const [phase] = useSidecar(4000, sessionId)
      const [open, setOpen] = useState(false)
      const wrapRef = useRef(null)
      const [placement, setPlacement] = useState({})
      useEffect(() => {
        if (!open) return
        const position = () => {
          const box = wrapRef.current?.getBoundingClientRect()
          if (!box) return
          const width = Math.min(380, window.innerWidth - 32)
          setPlacement({ left: Math.max(16, Math.min(box.left, window.innerWidth - width - 16)),
            bottom: Math.max(16, window.innerHeight - box.top + 4), maxHeight: Math.max(120, Math.min(480, box.top - 20)) })
        }
        position(); window.addEventListener('resize', position); window.addEventListener('scroll', position, true)
        return () => { window.removeEventListener('resize', position); window.removeEventListener('scroll', position, true) }
      }, [open])
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
      const dotCls = phase === 'off' ? 'dps-launchDot dps-launchDotOff' : phase === 'ok' ? 'dps-launchDot dps-launchDotOk'
        : phase === 'bad' ? 'dps-launchDot dps-launchDotBad'
          : 'dps-launchDot dps-launchDotWait'
      const label = phase === 'off' ? L.launchOff
        : partial ? `DPSwarm（部分开启 · ${teamOn ? '仅固定团队' : '仅 CM'}）`
          : phase === 'ok' ? L.launchOk : phase === 'bad' ? L.launchBad : L.launchWait
      return h('div', { ref: wrapRef, style: { position: 'relative', display: 'inline-block' } },
        h('button', { type: 'button', className: 'dps-launch', title: label,
          'aria-label': label, 'aria-expanded': open, onClick: toggle },
          h(Compass16, null),
          h('span', { className: dotCls, 'aria-hidden': 'true' })),
        open ? h('div', { className: 'dps-pop', style: placement }, h(DpswarmPanel, { sessionId })) : null)
    }

    const inject = ['slots', 'locale', 'connection', 'settingsScope']

    function apply(ctx) {
      settingsScope = ctx.settingsScope.bind({ namespace: 'dpswarm' })
      settingsMirror = ctx.settingsScope.describe()
      // ctx.get() is the public Cordis lookup without an inject requirement.
      // It returns only active providers. Never retain a namespace or legacy
      // API from apply(): either can arrive later or remount on reconnect.
      modelCatalogApi = () => {
        const remote = ctx.get('remote.session')
        if (typeof remote?.modelCatalog === 'function') {
          // Typed Remote has no cancellation parameter. The picker enforces
          // its timeout and ignores stale responses itself.
          return { models: async () => ({ result: await remote.modelCatalog() }) }
        }
        const legacy = ctx.connection.api?.llm
        return typeof legacy?.models === 'function' ? legacy : undefined
      }
      const settingsTransport = method => {
        if (!ctx.connection.isLoopback) throw new Error('宿主设置当前不可写，请连接本机宿主后重试。')
        const remote = ctx.get('remote.settings')
        if (typeof remote?.[method] === 'function') return { remote }
        const legacy = ctx.connection.api?.settings
        if (typeof legacy?.[method] === 'function') return { legacy }
        throw new Error('宿主设置服务暂不可用，请稍后重试。')
      }
      settingsApi = {
        mutate: async request => {
          const { remote, legacy } = settingsTransport('mutate')
          return remote
            ? { result: await remote.mutate(request.ns, request.ops, request.expectedRevision) }
            : legacy.mutate(request)
        },
        describe: async () => {
          const { remote, legacy } = settingsTransport('describe')
          return remote ? { result: await remote.describe() } : legacy.describe({})
        },
      }
      if (new URLSearchParams(location.search).get('dpswarm-settings') === '1') {
        const clean = new URL(location.href); clean.searchParams.delete('dpswarm-settings')
        history.replaceState(history.state, '', clean.href)
        // Defer until native settings navigation has mounted; no persistent mutation.
        setTimeout(() => openHostSettings().catch(e => ctx.logger?.warn?.(e.message)), 0)
      }
      ctx.slots.inject('settings.section', () => ctx.slots.register({
        name: 'settings.section', id: 'dpswarm', order: 30,
        label: () => 'DPswarm', locale: NS, inject: () => ({}),
      }, guarded(RoleSettings)))
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
