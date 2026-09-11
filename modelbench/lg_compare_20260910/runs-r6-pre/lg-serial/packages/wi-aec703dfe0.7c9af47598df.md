校验器实现：纯标准库 validate_record(record, schema)->list[str]，3 个 doctest，schema 逐字引用子任务 A 验收后的交付文本

## 原始任务全文（含全部材料，唯一行为依据）
两阶段串行交付，第二阶段严格依赖第一阶段产出：
- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与不超过 10 行的设计说明；
- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 schema 必须逐字引用 A 的交付）。
分工提示：B 必须等 A 验收后才能开始，fission 拆且只拆 2 个 subtasks（A 与 B），为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束，不得再继续裂变。

## 上游交付交接（三层：L2 摘要导航 / L1 逐字依据 / L0 原文回查）（handoff_profile=verbatim）

**逐字内容禁止凭记忆复述**：交付中需要逐字引用上游内容时，必须先输出一行 `PULL: <上游 item 标识>` 取回原文（§5.4 兜底通道，L0 层列有标识），再逐字直贴；L1 层仅供定位预览。

### L1 原子事实层（逐字提取；逐字引用请以此层为准）
[wi-322b36aae6]
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://example.com/schemas/mini-expense-record.json",
  "title": "迷你记账记录",
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "minLength": 1,
      "description": "记录唯一标识，非空字符串"
    },
    "amount": {
      "type": "number",
      "multipleOf": 0.01,
      "minimum": 0,
      "description": "金额，数值且精确到两位小数"
    },
    "currency": {
      "type": "string",
      "pattern": "^[A-Z]{3}$",
      "description": "ISO 4217 三字母货币代码，如 CNY/USD"
    },
    "tags": {
      "type": ["array", "null"],
      "items": { "type": "string" },
      "uniqueItems": true,
      "description": "标签数组，可为 null；元素为字符串且不重复"
    }
  },
  "required": ["id", "amount", "currency", "tags"],
  "additionalProperties": false
}
```

### L2 摘要层（摘要仅供导航，逐字内容以 L1/原文为准）
## 目标
子任务 A [wi-322b36aae6] 交付“迷你记账记录 JSON Schema"，定义迷你记账记录的字段、类型与约束，作为子任务 B（校验器实现）的实现依据。

## 已验收决定与不变量
状态：待验收（材料原文标注，验收未完成；以下为 A 已作出的设计决定与 schema 不变量）。

Schema 全文（B 的 doctest 须逐字引用）：
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://example.com/schemas/mini-expense-record.json",
  "title": "迷你记账记录",
  "type": "object",
  "properties": {
    "id": { "type": "string", "minLength": 1 },
    "amount": { "type": "number", "multipleOf": 0.01, "minimum": 0 },
    "currency": { "type": "string", "pattern": "^[A-Z]{3}$" },
    "tags": { "type": ["array", "null"], "items": { "type": "string" }, "uniqueItems": true }
  },
  "required": ["id", "amount", "currency", "tags"],
  "additionalProperties": false
}
```

不变量与设计决定（压缩自材料设计说明，约束原义不变）：
1. id：string，minLength 1 排除空串，语义等价于“必须提供标识”。
2. amount：number，multipleOf 0.01，minimum 0——JSON Schema 无法直接表达“小数位数”，multipleOf 0.01 为社区通行近似做法；材料提醒校验器 B 注意浮点误差，建议按整数分处理。
3. currency：string，pattern ^[A-Z]{3}$，锁定大写三字母代码，与 ISO 4217 对齐（如 CNY/USD）。
4. tags：类型 ["array","null"] 满足“可空”要求；元素为字符串且 uniqueItems 防止重复标签。
5. required=["id","amount","currency","tags"]：四字段全部必填，tags 可为 null 但不可缺失；additionalProperties: false 拒绝冗余字段，利于 B 的校验器做封闭检查。

## 当前引用
- item 标识：[wi-322b36aae6]（子任务 A 交付）
- $schema：https://json-schema.org/draft/2020-12/schema
- $id：https://example.com/schemas/mini-expense-record.json
- 依赖声明：deps=[0]（B 对 A）

## 未决问题
- 子任务 A 交付状态为“待验收”，验收尚未完成。

## 下一动作
- 完成对 A 的 schema 验收；B（校验器实现）须待验收通过后启动（deps=[0]），其 doctest 中的 schema 须逐字引用上方代码块。

### L0 原文层（artifact 引用，provenance 链不断）
- [wi-322b36aae6] submission_package=dep-75575d9000；逐字原文：输出一行 `PULL: wi-322b36aae6` 经 §5.4 兜底通道取回