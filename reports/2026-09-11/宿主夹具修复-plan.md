# 宿主夹具修复 plan（2026-09-11，dpswarm-dsh-plugin 0.11.0 冻结后）

目标：修复宿主升级到 `dsh@0.1.5-rc.1`（内置 `dsh-session 0.1.5-rc.2`）造成的 83 个测试失败。纪律：机制版本已冻结（tag mechanism-freeze-20260911），本轮**只修夹具与宿主兼容层**，不改任何机制语义（写锁/验收/返工/mailbox 行为一律不动）。

## 宿主实证（读安装版源码）

- `dsh-session/lib/index.js` `validateSessionHeader`：header 必须 `version === 3`、`id` 与会话一致、`createdAt` 非负安全整数、**`isSeeded` 必填布尔**；`seedLength` 字段**禁止出现**（fork 边界迁到构造参数 `inheritedEventCount` + `session/end-seed` 事件 + 实例字段 `inheritedEventCount`/`firstLiveSeq`，公开读法 `session.ownEvents()`）。
- `Session` 原型：**`events` getter 已删除**，改为 `snapshotEvents()` / `eventAt()` / `ownEvents()`；`surface`、`requestHeader()`、`deriveMessages()`、`append(type, data, {surfaceOp})` 保留。surface 型事件（system/user/assistant message、tool/result）必须带 `surfaceOp`；其余事件类型禁止携带。
- `dsh-token-meter` `TokenMeter`：新声明 `static inject = ['sessionProjections']`，构造函数直接 `ctx.sessionProjections.register(...)` —— 测试里的裸 `new Context()` 必须先 `ctx.provide('sessionProjections', …)`。
- `dsh-settings`：模块级 `installSettingsSection` 导出已删除；`SettingsProvider`（服务名 `settings`）实例方法 `installSection(owner, ns, schema, entry, hooks)`。插件 `lib/index.js` 的 fallback 分支本就调 `settingsCtx.settings.installSection`，与新宿主兼容；失效的只是测试假件。

## 失败归类（83 = 12+41+6+1+21+1）

### A. header v3 迁移（夹具）——约 41 个
- 现象：`Error: session header version must be 3, got 0`。
- 位置：`tests/budget-host.test.mjs:14-16`（12 个）、`tests/lead-route.test.mjs:34,54`（21 个）、`tests/fixed-team.test.mjs:313,335`（6 个）、`tests/mechanism-911-characterization.test.mjs:99-100`（1 个）、`tests/cm.test.mjs:30,212,234,243,310`（cm 的一部分）。
- 修法：`version: 0` → `SESSION_FORMAT_VERSION`（=3）并补 `isSeeded: false`；**带 seed 的 create**（cm.test:234/243 fork/子会话）必须 `header.isSeeded: true` + 第 4 参 `inheritedEventCount = seed.length`（snapshot 模式要求 seed 恰等于继承前缀）。seeded 构造会自动追加一条 `session/end-seed` 事件（非 surface 事件，不影响 `deriveMessages`，但影响 `snapshotEvents().length` 类断言——逐个核对）。
- 风险：低。纯格式迁移；事件序连续性/`surfaceOp` 规则由 append 在运行时校验，夹具现有 append（user/message 带 `surfaceOp:'append'`、turn/start 等不带）已合规。

### B. TokenMeter 新依赖 sessionProjections（夹具）——cm.test 的主体（41 个）
- 现象：`TypeError: Cannot read properties of undefined (reading 'register')` at `new TokenMeter` (dsh-token-meter/lib/index.js:615)。
- 修法：`tests/cm.test.mjs` fixture 的 `new Context()` 后补 `ctx.provide('sessionProjections', { register() {} })`。
- 风险：修完后可能暴露下一层宿主 API 漂移（`DPSwarmCM extends BasicCompactionEngine`、`toolPairingBalanced*`、`renderContextSnapshot` 等）——按实际报错逐项修，仍属夹具/兼容层。

### C. settings API（夹具）——1 个
- 现象：`TypeError: settingsCtx.settings.installSection is not a function`（index.test.mjs:39）。
- 修法：测试假 settings 服务补 `installSection(owner, ns, schema, entry, hooks)`（调 `hooks.setSource(...)`/`hooks.onChange()`，对齐新宿主 SettingsProvider 行为）。插件运行时无需改（fallback 分支已兼容新宿主）。
- 风险：低。

### D. 插件运行时宿主兼容（lib，单独标注的“运行时 bug”类）
实证（node 直探安装版 Session 原型）：新宿主真实 Session 上 **`session.events === undefined`**、**header 无 `seedLength`**。插件 lib/ 以下读取在新宿主下行为退化或直接 TypeError——这不是夹具问题，是运行时兼容缺口：
1. `lib/cm.js:42` `session.events.filter(...)` → **TypeError**（CM 每次装配都炸）。
2. `lib/cm-runtime.js:13` `session.events?.length || 0` → 恒 0（turn 边界找错）。
3. `lib/budget-runtime.js:20,98,252` `(session.events||[]).slice(header.seedLength||0)` → 恒 []（原生进度/额度演算读不到）。
4. `lib/worker-diagnostics.js:62-66` `Array.isArray(session.events)` → false（证据标不可用，属优雅降级）。
5. `lib/team-required.js:29-40` 同上 → `directUserMessage` 恒 null → **TEAM_REQUIRED 对所有任务报 TASK_MISSING**（严重）。
- 修法（行为等价的兼容读取层，不是机制改动）：新增 `lib/host-session-compat.js`，导出 `sessionEvents(session)`（`Array.isArray(session.events)` 优先——测试假件照旧；否则 `session.snapshotEvents()`）与 `forkBoundary(session)`（旧 `header.seedLength` 优先，新 `inheritedEventCount` 兜底，缺省 0）。上述 5 处改调这两个助手。语义完全等价：同一"自有事件集合"与"fork 边界"值，只是读取位置随宿主迁移。
- 风险：中低。兼容函数必须同时服务"真宿主 Session"与"测试假件（裸对象）"两条路径；team-required/budget-runtime/worker-diagnostics 的既有单测用裸对象夹具钉行为，可当回归网。

## 修复顺序

1. D（兼容层）先行——A 修完后真 Session 会流进插件运行时，没有 D 会换成另一批红。
2. A（5 个文件的 header v3）→ B（cm fixture）→ C（settings 假件）。
3. 全量 `node --test dpswarm-dsh-plugin/tests/*.test.mjs`（仓库根），逐项消红；新暴露的宿主漂移按 B 的同纪律处理。
4. 结果回填本文档"结果"一节。

## 结果（2026-09-11 跑完回填）

**全量 `node --test dpswarm-dsh-plugin/tests/*.test.mjs`（仓库根）：401 tests / 401 pass / 0 fail —— 全绿，无未修项。**（修复前同环境基线 303 pass / 83 fail。）

### 逐类落地情况

- **A. header v3 迁移（41 个）——全修**。`budget-host`（12）、`lead-route`（21）、`fixed-team`（6）、`mechanism-911`（1）与 `cm.test` 的非 seeded 站点：`version: 0` → 导入宿主 `SESSION_FORMAT_VERSION`（=3）并补 `isSeeded: false`。`cm.test` 的 2 个 seeded create（child/fork）：补 `isSeeded: true` + 第 4 参 `inheritedEventCount = seed.length`（snapshot 模式硬性要求），seed 改从 `h.session.snapshotEvents()` 取。副作用核对：seeded 构造自动追加的 `session/end-seed` 事件不影响任何 `deriveMessages`/计数断言。
- **B. TokenMeter 新依赖（cm 41 的主体）——全修**。fixture `ctx.provide('sessionProjections', { register() {} })`。修后按实际报错继续剥了两层同族漂移（均夹具/兼容层）：
  1. 新 TokenMeter 在 `ctx.get('llm')` 上调 `imageRequestPricing(provider, model)` → llm 假件补 `imageRequestPricing: () => null`。
  2. 新 dsh-session 的 assistant/message 事件嵌 provider 流，TokenMeter 锚点按 `data.stream` 重组定价 → 带 usage 的 assistant 事件补 `stream: [{type:'chunk', chunk:{type:'text-delta',…}}]`（与 message.content 同文本，量级等价旧估算）。
- **C. settings API（1 个）——全修**。index.test 假 settings 服务补 `installSection(owner, ns, schema, entry, hooks)`（调 `hooks.setSource`/`hooks.onChange`，对齐新宿主 SettingsProvider）；插件运行时本就兼容（fallback 分支调的就是 `settingsCtx.settings.installSection`），未改。
- **D. 插件运行时宿主兼容（单独标注的运行时缺口）——全修，机制语义不变**。新增 `lib/host-session-compat.js`（`sessionEvents` / `forkBoundary` / `headerPricesSystem` / `systemMessageTokens`）：
  1. **`session.events` getter 已删**（新宿主改 `snapshotEvents()`）。裸读共 8 处：`cm.js`（1 处直接 TypeError）、`cm-runtime.js`（恒 0）、`budget-runtime.js`（3 处恒 []）、`budget.js`、`lead-route.js`（2 处，其中 1 处直接 TypeError）、`team-required.js`（恒空 → TEAM_REQUIRED 对所有任务误报 TASK_MISSING）、`worker-diagnostics.js`（证据误判不可用）。全部改走 `sessionEvents()`：假件裸对象（`Array.isArray(events)`）优先，真宿主走 `snapshotEvents()`——行为等价读取层。
  2. **`header.seedLength` 禁止出现在 v3 header**（fork 边界迁到实例字段 `inheritedEventCount`）。`budget-runtime`（3 处）、`team-required`、`worker-diagnostics` 改走 `forkBoundary()`：旧字段优先、新字段兜底、缺省 0——同一"自有事件前缀"值。
  3. **`canonicalHeader` 不再保留 `header.system`**（系统提示改走 system/message 面事件；仓库内旧版源码仍保留 system，实证为宿主行为变更）。这会静默削弱两个机制面：DP CM 的窗口压力看不见系统提示（cm.js）、worker 额度/收尾测算看不见系统提示与收尾指令（budget.js 2 处）。兼容修法：原始 header（含 system/tools）直传 `measure`（其内部自行规范化），宿主丢弃 system 时（`headerPricesSystem` 一次性探测）用 `tokenMeter.estimateMessage` 显式补价同一文本；旧宿主仍由 header 计价、不重复加。`sameContent` 的系统比较从 `prior.system`（新宿主恒空）改为 `prior.system ?? 面上最后一条 system/message 文本`。语义保持"系统提示始终参与压力与额度测算"。
- **附带夹具修**：`lead-route.test` 的 alien 会话克隆原先 `events: cold.session.events`（新宿主恒 undefined）→ 改 `snapshotEvents()`；`cm.test` 全部 `h.session.events` 读改 `snapshotEvents()`（14 处）。

### 复验

- 全量 401/401 绿（含 0.11.0 mailbox 15 项、既有机制表征全部保持——写锁/验收/返工/mailbox 行为零改动）。
- 改动面：夹具 6 个文件 + 新 `lib/host-session-compat.js` + 6 个 lib 文件的等价读取替换与 system 补价（见上 D 类标注）。
- 未修项：无。遗留观察一项：`forkBoundary` 的新宿主分支（inheritedEventCount）在真 seeded Session 上目前只经 cm.test 间接触达，后续可补一条直接表征（非阻塞）。
