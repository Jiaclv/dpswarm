子任务B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例，用例中的 schema 必须逐字引用 A 的交付。

## 上游交付交接（三层：L2 摘要导航 / L1 逐字依据 / L0 原文回查）

### L2 摘要层（摘要仅供导航，逐字内容以 L1/原文为准）
## 目标
相位交接摘要：将上游已验收交付物 MiniLedgerRecord JSON Schema（[wi-e6a2d1d480]）零新增事实压缩，供下游子任务使用；保留决定、引用、版本号与未决问题。

## 已验收决定与不变量
- Schema 采用 JSON Schema draft 2020-12（$schema: https://json-schema.org/draft/2020-12/schema），title: MiniLedgerRecord，type: object。
- 字段不变量（四个字段均 required，additionalProperties: false）：
  - id: string，minLength: 1，描述"记录唯一标识"。
  - amount: number，multipleOf: 0.01，描述"金额，精确到两位小数"。
  - currency: string，pattern: ^[A-Z]{3}$，描述"ISO 4217 三字母货币代码"。
  - tags: type 为 ["array","null"]，items 为 string，描述"标签数组，可为 null"。
- 设计决定（材料原文归纳）：
  - amount 以 multipleOf: 0.01 约束两位小数；材料建议下游实现用字符串或定点数存储以规避浮点误差。
  - currency 以 ^[A-Z]{3}$ 正则限定大写三字母，符合 ISO 4217。
  - tags 允许 null 与数组两种类型，items 确保数组元素均为字符串。
  - tags 通过允许 null 满足"可空但必须出现"。
  - additionalProperties: false 关闭未声明字段，保证 schema 严格性。
  - 选用 2020-12 草案，兼容主流校验器（Ajv、jsonschema 等）。

## 当前引用
- [wi-e6a2d1d480]：完整 Schema JSON（含 $schema、title、properties、required、additionalProperties 全文，本摘要未改动任何字面值）。
- https://json-schema.org/draft/2020-12/schema（2020-12 草案地址）。
- 校验器兼容性提及：Ajv、jsonschema。

## 未决问题
无。

## 下一动作
- 下游实现 amount 的存储时按材料建议采用字符串或定点数以规避浮点误差（此为材料内建议，非已定实现方案）。

### L1 原子事实层（逐字提取；逐字引用请以此层为准）
[wi-e6a2d1d480]
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "MiniLedgerRecord",
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "minLength": 1,
      "description": "记录唯一标识"
    },
    "amount": {
      "type": "number",
      "multipleOf": 0.01,
      "description": "金额，精确到两位小数"
    },
    "currency": {
      "type": "string",
      "pattern": "^[A-Z]{3}$",
      "description": "ISO 4217 三字母货币代码"
    },
    "tags": {
      "type": ["array", "null"],
      "items": { "type": "string" },
      "description": "标签数组，可为 null"
    }
  },
  "required": ["id", "amount", "currency", "tags"],
  "additionalProperties": false
}
```

### L0 原文层（artifact 引用，provenance 链不断）
- [wi-e6a2d1d480] submission_package=dep-5db80be39d；逐字原文：输出一行 `PULL: wi-e6a2d1d480` 经 §5.4 兜底通道取回