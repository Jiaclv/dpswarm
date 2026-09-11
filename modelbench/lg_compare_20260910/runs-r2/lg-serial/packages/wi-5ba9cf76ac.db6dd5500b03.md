子任务 B「校验器实现」：严格依据子任务 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例，用例中的 schema 必须逐字引用 A 的交付。

## 上游交付交接（三层：L2 摘要导航 / L1 逐字依据 / L0 原文回查）

### L2 摘要层（摘要仅供导航，逐字内容以 L1/原文为准）
## 目标
为迷你记账的单笔收支记录定义 JSON Schema（title: ExpenseRecord，描述：单笔收支的基本信息），作为上游已验收交付供下游子任务使用（材料 wi-c28194eb88）。

## 已验收决定与不变量
- Schema 基线：采用 JSON Schema 2020-12 草案，$schema = https://json-schema.org/draft/2020-12/schema ，与主流校验器兼容；$id = https://example.com/schemas/expense-record.json 。
- 结构不变量：required = ["id", "amount", "currency", "tags"]，四个字段全部必填；additionalProperties = false（关闭，保证结构严格）。
- 字段约束：
  - id：string，记录唯一标识，minLength = 1。
  - amount：number，金额保留两位小数，用 multipleOf = 0.01 约束精度。
  - currency：string，ISO 4217 三字母货币代码，pattern = ^[A-Z]{3}$ 保证大写三字母格式。
  - tags：array，标签列表，items 为 string 且 minLength = 1；允许空数组但不允许空字符串元素；default = []（便于省略）。

## 当前引用
- 材料/交付物 ID：wi-c28194eb88
- $schema：https://json-schema.org/draft/2020-12/schema
- $id：https://example.com/schemas/expense-record.json
- 外部规范：ISO 4217（currency 字段的货币代码标准）

## 未决问题
无（材料未列出未决问题）。

## 下一动作
无（材料未定义后续动作）。

### L1 原子事实层（逐字提取；逐字引用请以此层为准）
[wi-c28194eb88]
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://example.com/schemas/expense-record.json",
  "title": "ExpenseRecord",
  "description": "迷你记账记录：单笔收支的基本信息",
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "记录唯一标识",
      "minLength": 1
    },
    "amount": {
      "type": "number",
      "description": "金额，保留两位小数",
      "multipleOf": 0.01
    },
    "currency": {
      "type": "string",
      "description": "ISO 4217 三字母货币代码",
      "pattern": "^[A-Z]{3}$"
    },
    "tags": {
      "type": "array",
      "description": "标签列表，可为空数组",
      "items": { "type": "string", "minLength": 1 },
      "default": []
    }
  },
  "required": ["id", "amount", "currency", "tags"],
  "additionalProperties": false
}
```

### L0 原文层（artifact 引用，provenance 链不断）
- [wi-c28194eb88] submission_package=dep-3ae61c290c；逐字原文：输出一行 `PULL: wi-c28194eb88` 经 §5.4 兜底通道取回