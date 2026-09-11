校验器实现

## 原始任务全文（含全部材料，唯一行为依据）
两阶段串行交付，第二阶段严格依赖第一阶段产出：
- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与不超过 10 行的设计说明；
- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 schema 必须逐字引用 A 的交付）。
分工提示：B 必须等 A 验收后才能开始，建议 fission 拆 2 个 subtasks 并为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束。

## 上游交付交接（三层：L2 摘要导航 / L1 逐字依据 / L0 原文回查）（handoff_profile=verbatim）

### L1 原子事实层（逐字提取；逐字引用请以此层为准）
[wi-1bb08b2f1b]
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Mini Expense Record",
  "type": "object",
  "additionalProperties": false,
  "required": ["id", "amount", "currency", "tags"],
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
      "items": {
        "type": "string"
      }
    }
  }
}
```

### L2 摘要层（摘要仅供导航，逐字内容以 L1/原文为准）
## 目标
为"Mini Expense Record"（迷你支出记录）定义 JSON Schema 并固化字段约束与设计说明。

## 已验收决定与不变量
上游交付（[wi-1bb08b2f1b]）的 schema 原文（逐字保留，契约载体）：
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Mini Expense Record",
  "type": "object",
  "additionalProperties": false,
  "required": ["id", "amount", "currency", "tags"],
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
      "items": {
        "type": "string"
      }
    }
  }
}
```
已验收决定与不变量（逐条）：
1. schema 采用 JSON Schema draft 2020-12；根类型为 object，`additionalProperties: false`。
2. 四个字段均为必填：`id`、`amount`、`currency`、`tags`，且不允许额外字段。
3. `id`：string，`minLength: 1`。
4. `amount`：用 `number`，`multipleOf: 0.01` 表示精确到两位小数。
5. JSON 数值格式不会保留尾部零，例如 `1.5` 等价于 `1.50`。
6. `currency`：限制为大写三字母代码，`pattern: ^[A-Z]{3}$`。
7. `tags`：允许字符串数组或 `null`（`type: ["array", "null"]`），数组项必须是字符串。

## 当前引用
- 上游 item 标识：[wi-1bb08b2f1b]
- schema 元引用（`$schema`）：https://json-schema.org/draft/2020-12/schema

## 未决问题
无（材料未包含未决问题或已确认死路）。

## 下一动作
无（材料未包含下游动作指引）。

### L0 原文层（artifact 引用，provenance 链不断）
- [wi-1bb08b2f1b] submission_package=dep-35801b0dbb；逐字原文：输出一行 `PULL: wi-1bb08b2f1b` 经 §5.4 兜底通道取回