# 模型侧调研结论(GLM / DeepSeek)

核实日期:**2026-09-03**(以当日 `date` 为准)。方法:直接抓取 bigmodel.cn / docs.bigmodel.cn / api-docs.deepseek.com 官方页面,辅以第三方交叉验证;**未调用任何真实 API**(无 key)。
注意:模型名与价目变动频繁,本文件只反映核实当日状态;实验前请跑 `preflight.py` 实测。

---

## 1. GLM 侧(open.bigmodel.cn / bigmodel.cn)

### 1.1 接入端点(官方)

| 协议 | Base URL |
|---|---|
| OpenAI Chat Completions | `https://open.bigmodel.cn/api/paas/v4` |
| OpenAI Responses | `https://open.bigmodel.cn/api/v1` |
| Anthropic Messages | `https://open.bigmodel.cn/api/anthropic` |

来源:[GLM-5.3 模型页](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3)。

Coding Plan 专用编程通道(第三方插件实测口径,官方文档未公开列出):`https://open.bigmodel.cn/api/coding/paas/v4` —— 见 [dsh-bigmodel-catalog](https://github.com/OpenSaozi/dsh-bigmodel-catalog) 与 [接入示例](https://github.com/SamuelQZQ/auto-coding-agent-demo)。**存疑项**,用 Coding Plan key 时需实测。

### 1.2 候选模型核实表

| model id | 状态(2026-09-03) | 上下文 | 最大输出 | thinking / reasoning_effort | 价格(CNY/百万 tok,输入/输出/缓存读) |
|---|---|---|---|---|---|
| `glm-5.3` | 在售(最新旗舰,2026-08-14 发布) | 1M | 128K | **思考强制开启**,`thinking.type` 仅支持 `enabled`;`reasoning_effort`: low/high/max,默认 max | ¥8 / ¥28 / ¥2(缓存写限时免费) |
| `glm-5.3-flash` | 在售(多模态:图片/视频/文件/文本) | 1M | 128K | 未单独核实,按 5.3 系列口径 | 5折限时:¥0.4 / ¥1.4 / ¥0.115(原价 ¥0.8/¥2.8/¥0.23) |
| `glm-5.2` | 在售 | 1M | 128K | 支持 thinking 开关(文档未逐一核实 effort) | ¥8 / ¥28 / ¥2 |
| `glm-4.7` | **在售**(价格页旗舰表在列) | 200K | 128K | `thinking.type`: enabled/disabled;`reasoning_effort` **未见官方文档**(存疑) | 分档:输入<32K 且输出<2K:¥2/¥8/¥0.4;输入<32K 且输出≥2K:¥3/¥14/¥0.6;输入[32K,200K):¥4/¥16/¥0.8 |
| `glm-4.6` | **存疑**:模型文档页仍在、`GET /models` 2026-05 仍返回(第三方报告),但价格页旗舰表已不列(疑移入"历史模型");阿里云侧公告 2026-10-10 下架 4.6/4.7(阿里云平台,非智谱自家) | 200K | 128K | `thinking.type`: enabled/disabled | **价格页未列,查不到** → null |
| `glm-4.5-air` | 在售(价格页在列) | 128K | 96K(文档页口径) | `thinking.type`: enabled/disabled(默认动态思考) | 分档:输入<32K 且输出<2K:¥0.8/¥2/¥0.16;输入<32K 且输出≥2K:¥0.8/¥6/¥0.16;输入[32K,128K):¥1.2/¥8/¥0.24 |
| `glm-4.5-flash` | **已下线**(2026-01-30,由 glm-4.7-flash 替代;价格页已无此项) | (曾为 128K) | — | — | 已下线 → null |
| `glm-4.7-flash` | 在售,**免费** | 200K | 128K | 支持 | 免费(第三方口径:仅 1 并发) |
| `glm-4.7-flashx` | 在售 | 200K | 128K | 支持 | ¥0.5 / ¥3 / ¥0.1 |

要点:
- 分档价的"32"= 32K tokens;"0.2"= 0.2万 = 2K tokens(z.ai 国际站口径为输出 <2K 分档)。抓取页面未标单位,以[价格页](https://bigmodel.cn/pricing)实物为准。
- GLM-5.1 / GLM-5 / GLM-5-Turbo 仍在售(5.1: <32K 档 ¥6/¥24/¥1.3;5-Turbo: ¥5/¥22/¥1.2;5: ¥4/¥18/¥1)。5.1 上下文官方价格页未标(华为云文档 198K,第三方有 1M 说法,**存疑**)。
- Batch API 半价(价格页:"只需五折费用")。
- 账号若订阅过 Coding Plan,调 GLM-5.3 API 暂只能走 OpenAI Chat Completion 协议(GLM-5.3 模型页注意事项)。

来源:[价格页](https://bigmodel.cn/pricing)、[模型概览](https://docs.bigmodel.cn/cn/guide/start/model-overview)、[glm-4.7](https://docs.bigmodel.cn/cn/guide/models/text/glm-4.7)、[glm-4.6](https://docs.bigmodel.cn/cn/guide/models/text/glm-4.6)、[glm-4.5/4.5-Air](https://docs.bigmodel.cn/cn/guide/models/text/glm-4.5)、[glm-5.3](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3);下线信息:[smzdm 报道](https://post.smzdm.com/p/ad73vnnd);/models 返回:[openhanako issue](https://github.com/liliMozi/openhanako/issues/1266)。

### 1.3 服务端 web_search 工具(chat/completions 内置)

请求格式(随 `tools` 参数传入,服务端执行搜索):

```json
{
  "model": "glm-4.7",
  "messages": [{"role": "user", "content": "智谱 GLM 最新模型是什么"}],
  "tools": [{
    "type": "web_search",
    "web_search": {
      "enable": true,                      // 必须 true;默认 false,不传等于没开
      "search_engine": "search_std",       // search_std | search_pro | search_pro_sogou | search_pro_quark
      "search_query": "可选,自定义搜索词",
      "search_result": true,               // true 时在响应里返回搜索来源
      "search_prompt": "可选,含 {search_result} 占位的总结提示词模板"
    }
  }]
}
```

- **互斥**:函数调用 / 知识库检索 / 网络搜索三者互斥,同时传只生效优先级最高的一个:函数调用 > 知识库检索 > 网络搜索([官方 FAQ](https://docs.bigmodel.cn/cn/faq/api-issues))。
- **额外计费:按次**。Search-Std ¥0.01/次、Search-Pro ¥0.03/次、Search-Pro-Sogou ¥0.05/次、Search-Pro-Quark ¥0.05/次([价格页"搜索工具服务"](https://bigmodel.cn/pricing))。
- 响应:`search_result: true` 时搜索来源随 message 返回(官方 FAQ 确认该参数与行为);**具体字段名官方页本次未复现** —— 历史版本为 `choices[0].message.web_search[]`(title/link/content/icon/media/ts),新版本可能改用引用标注,**以 preflight 实测为准(存疑)**。
- Coding Plan 内用 MCP 联网搜索:1.2 积分/次。
- 参数名权威依据:官方 FAQ(`enable` 默认 false、`search_result`)+ 官方文档示例转引([cnblogs 2026-01](https://www.cnblogs.com/jzssuanfa/p/19525763))。

### 1.4 GLM Coding Plan(订阅制)与按量的区别

来源:[Coding Plan 官方文档](https://docs.bigmodel.cn/cn/coding-plan/overview)、[购买页](https://bigmodel.cn/claude-code)、套餐售价第三方核对 [CreditsPlan](https://creditsplan.cn/brands/bigmodel/)。

- **计费方式**:积分制,不是 prompt 数限制。额度=每 5 小时窗口 + 每周上限:
  - Lite:2,000 积分/5h,10,000/周
  - Pro:12,000 积分/5h,60,000/周
  - Max:28,000 积分/5h,140,000/周
- **积分抵扣**:`消耗积分 = (输入tok×Input系数 + 缓存命中tok×Cached系数 + 输出tok×Output系数)/10000`;GLM-5.3 系数 6.9/1.7/24,GLM-5.3-Flash 2.3/0.56/8;非高峰(除周一至五 14:00–18:00 UTC+8)按 50% 抵扣。
- **关键限制**:套餐额度**只在官方指定编程工具环境**(Claude Code、Kilo Code、OpenClaw、OpenCode、TRAE、CodeBuddy 等)里有效;普通 `paas/v4` API 调用不消耗套餐额度,走按量余额。实验若要用 Coding Plan 省钱,必须走编码工具通道/专用 coding 端点(见 1.1 存疑项)。
- **可用模型**:套餐内仅 GLM-5.3、GLM-5.3-Flash;调 `glm-5.2`/`glm-5.1` 自动切 GLM-5.3,调 `glm-5-turbo`/`glm-4.7` 自动切 GLM-5.3-Flash —— **在 Coding Plan 里写 glm-4.7 实际拿到的是 5.3-Flash**。
- 售价(第三方 CreditsPlan 核对官方购买页,2026-08-25):Lite ¥118/月、Pro ¥538/月、Max ¥1078/月;团队标准 ¥598/席/月、团队高级 ¥1198/席/月;包季 8 折、包年 7 折(折扣为官方页确认,**具体数字需购买页复核,存疑**)。
- 按量价对照:GLM-5.3 ¥8/¥28;官方宣传非高峰用满订阅较按量最高省 92%。

---

## 2. DeepSeek 侧(api.deepseek.com)

来源(均为官方,核实于 2026-09-03):[首页/模型](https://api-docs.deepseek.com/)、[价格](https://api-docs.deepseek.com/quick_start/pricing)、[速率限制](https://api-docs.deepseek.com/quick_start/rate_limit)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode)、[更新日志](https://api-docs.deepseek.com/updates/)。

### 2.1 模型核实表

| model id | 状态 | 当前版本 | 上下文 | 最大输出 | 价格(USD/百万 tok) |
|---|---|---|---|---|---|
| `deepseek-v4-flash` | 在售 | DeepSeek-V4-Flash-**0731**(2026-07-31) | 1M | 384K(上限) | 峰谷制,见下 |
| `deepseek-v4-pro` | 在售 | DeepSeek-V4-Pro-**0813**(2026-08-13 GA) | 1M | 384K | 峰谷制,见下 |
| `deepseek-v4-flash-vision-exp` | 在售(实验性,多模态) | — | 1M | 384K | 同 flash;图片按尺寸折算成 token 计入输入 |
| `deepseek-chat` | **已停用**(2026-07-24 15:59 UTC) | — | — | — | 调用应报 Model Not Exist |
| `deepseek-reasoner` | **已停用**(同上) | — | — | — | 同上 |

**重要**:`deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 停用(官方 changelog 2026-04-24 宣布,三个月缓冲期)。停用前二者分别指向 v4-flash 的非思考/思考模式。**实验代码必须改用 `deepseek-v4-flash` / `deepseek-v4-pro` + `thinking` 参数**,不能再用旧名。

### 2.2 价格(2026-08-16 16:00 UTC 起生效,峰谷制)

Peak 时段:周一至周五 01:00–04:00 与 06:00–10:00 UTC;其余为 off-peak;**off-peak 单价 = peak 的一半**(下表列 off-peak,peak ×2):

| model | 输入(缓存命中) | 输入(缓存未命中) | 输出 |
|---|---|---|---|
| deepseek-v4-flash | $0.007 | $0.22 | $0.66 |
| deepseek-v4-pro | $0.022 | $0.66 | $1.98 |
| deepseek-v4-flash-vision-exp | $0.007 | $0.22 | $0.66 |

- 思考链计费:V4 官方价格页/思考模式页**未再单列条款**(存疑);V3.2 时代官方口径为"CoT 按输出 token 计费",V4 thinking 默认开启,预期 `completion_tokens` 含 CoT —— 以实测 `usage` 字段为准。
- 余额优先扣赠送额度。

### 2.3 思考模式与参数

- 默认**开启** thinking,默认 effort=`high`;`{"thinking":{"type":"enabled|disabled"}}` + `reasoning_effort: low|high|max`(medium/xhigh 会被映射为 high)。
- Anthropic 格式用 `reasoning.effort: none|low|high|max`(`none` 关思考)。
- 思考模式下 `temperature`/`top_p` 等参数被忽略(不报错);CoT 经 `reasoning_content` 返回;**带 `tools` 的多轮请求必须完整回传 `reasoning_content`,否则 400**。
- Base URL:OpenAI 格式 `https://api.deepseek.com`;Anthropic 格式 `https://api.deepseek.com/anthropic`。

### 2.4 速率限制

- 按**账号级并发数**限制(不按 RPM/TPM):v4-pro 500、v4-flash 2500、vision-exp 2500;超限返回 HTTP 429;可免费申请扩容。
- `user_id` 参数可做内容安全 / KVCache / 调度隔离(扩容用户按 user_id 分池限流)。
- 请求 10 分钟未开始推理,服务端断连(期间以空行/keep-alive 注释保活)。

---

## 3. 存疑 / 未核实项汇总

| 项 | 状态 | 建议 |
|---|---|---|
| GLM `glm-4.6` 是否仍可调、单价 | 价格页未列,文档页仍在 | preflight 实测;不可用则用 glm-4.7 |
| GLM `glm-4.7` 是否支持 `reasoning_effort` | 官方 4.7 页只写 thinking.type | preflight 实测或问客服 |
| GLM web_search 响应字段名(来源数组) | 官方页未复现字段级文档 | preflight 的 web_search 检查实测确认 |
| GLM Coding Plan 具体月费数字 | 第三方(CreditsPlan)核对,官方购买页为动态渲染 | 购买页人工复核 |
| Coding Plan 编程通道 URL `/api/coding/paas/v4` | 仅第三方插件佐证 | 用 Coding Plan key 实测 |
| glm-5.1 / glm-5 / glm-5-turbo 上下文长度 | 价格页未标 | 需要时查模型页 |
| DeepSeek V4 思考链是否计入输出计费 | 官方页未明示 | 实测 usage 字段 |
| GLM 免费模型并发限制(如 4.7-flash 1 并发) | 第三方口径 | 官方速率限制页核对 |

## 4. 对本实验的直接结论

1. DeepSeek 侧角色模型一律用 `deepseek-v4-flash` / `deepseek-v4-pro`;reasoner 场景= v4 模型开 thinking,不要再写 `deepseek-chat`/`deepseek-reasoner`。
2. GLM 侧候选按性价比排序:`glm-4.7`(主力,200K)、`glm-4.5-air`(便宜轻量)、`glm-4.7-flash`(免费,适合 context manager 打杂);旗舰对照用 `glm-5.3`/`glm-5.2`(1M);`glm-4.5-flash` 已死,`glm-4.6` 别再选。
3. 若 GLM 走 Coding Plan:额度只在指定编码工具/编程通道内有效,且 `glm-4.7` 会被静默切换为 GLM-5.3-Flash —— 对比实验会污染模型变量,建议对比实验用按量 key + `paas/v4`。
4. GLM 联网需求用内置 `web_search` 工具(search_std 最便宜 ¥0.01/次,记得 `enable: true`),不要与 function call 混在同一请求的 tools 里。
