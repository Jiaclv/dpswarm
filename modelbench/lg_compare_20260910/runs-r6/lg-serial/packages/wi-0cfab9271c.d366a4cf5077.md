校验器实现：严格依据子任务 A 验收后的 schema 文本，用纯 Python 标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例，用例中的 schema 必须逐字引用 A 的交付文本

## 原始任务全文（含全部材料，唯一行为依据）
两阶段串行交付，第二阶段严格依赖第一阶段产出：
- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与不超过 10 行的设计说明；
- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 schema 必须逐字引用 A 的交付）。
分工提示：B 必须等 A 验收后才能开始，fission 拆且只拆 2 个 subtasks（A 与 B），为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束，不得再继续裂变。

## 上游交付交接（三层：L2 摘要导航 / L1 逐字依据 / L0 原文回查）（handoff_profile=verbatim）

**逐字内容禁止凭记忆复述**：交付中需要逐字引用上游内容时，必须先输出一行 `PULL: <上游 item 标识>` 取回原文（§5.4 兜底通道，L0 层列有标识），再逐字直贴；L1 层仅供定位预览。

### L1 原子事实层（逐字提取；逐字引用请以此层为准）
[wi-3ef26a8a60]
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "MiniBookkeepingRecord",
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "minLength": 1
    },
    "amount": {
      "type": "number",
      "multipleOf": 0.01
    },
    "currency": {
      "type": "string",
      "pattern": "^[A-Z]{3}$"
    },
    "tags": {
      "type": ["array", "null"],
      "items": { "type": "string", "minLength": 1 },
      "uniqueItems": true
    }
  },
  "required": ["id", "amount", "currency", "tags"],
  "additionalProperties": false
}
```

### L2 摘要层（摘要仅供导航，逐字内容以 L1/原文为准）
## 目标
为记账记录（MiniBookkeepingRecord）定义 JSON Schema，作为子任务 B doctest 校验的逐字引用依据。

## 已验收决定与不变量
Schema 全文（上游要求按原文验收，逐字保留）：
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "MiniBookkeepingRecord",
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "minLength": 1
    },
    "amount": {
      "type": "number",
      "multipleOf": 0.01
    },
    "currency": {
      "type": "string",
      "pattern": "^[A-Z]{3}$"
    },
    "tags": {
      "type": ["array", "null"],
      "items": { "type": "string", "minLength": 1 },
      "uniqueItems": true
    }
  },
  "required": ["id", "amount", "currency", "tags"],
  "additionalProperties": false
}
```

设计决定（转述自上游设计说明，不改变原意）：
1. `amount` 以 `multipleOf: 0.01` 约束两位小数——JSON Schema 无法限制浮点字面位数，此为标准做法；下游实现需注意二进制浮点精度。
2. `currency` 以 `^[A-Z]{3}$` 匹配 ISO 4217 三字母大写代码。
3. `tags` 声明 `["array", "null"]` 满足可空；元素为非空字符串且 `uniqueItems` 防重复标签。
4. `id`/`amount`/`currency`/`tags` 四字段全部 `required` 且 `additionalProperties: false`，保证结构严格可预测。

## 当前引用
- 上游 item：[wi-3ef26a8a60]（schema 全文与设计说明的唯一来源）
- $schema：https://json-schema.org/draft/2020-12/schema
- `^[A-Z]{3}$` 对应 ISO 4217 三字母大写代码

## 未决问题
无

## 下一动作
子任务 B 将 [wi-3ef26a8a60] 中 schema 原文逐字引用为 doctest 校验依据，按原文验收。

### L0 原文层（artifact 引用，provenance 链不断）
- [wi-3ef26a8a60] submission_package=dep-95f91e9230；逐字原文：输出一行 `PULL: wi-3ef26a8a60` 经 §5.4 兜底通道取回