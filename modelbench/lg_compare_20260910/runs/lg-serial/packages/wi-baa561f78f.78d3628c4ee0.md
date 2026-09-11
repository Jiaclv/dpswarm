校验器实现：严格依据子任务 0 验收后的 schema 文本，纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（空列表=通过），附 3 个 doctest 用例，用例中 schema 逐字引用子任务 0 的交付

## 上游交付摘要（不可信摘要，以实际文件为准）
## 目标
对上游已验收交付——“迷你记账”单条记录的 LedgerRecord JSON Schema 设计（材料 [wi-dee399de10]）——做相位交接压缩：保留 schema 结构、设计决定与外委事项，供下游子任务组装使用，零新增事实。

## 已验收决定与不变量
- 规范版本：JSON Schema draft 2020-12（$schema: https://json-schema.org/draft/2020-12/schema）。
- 文档标识：$id: https://example.com/schemas/ledger-record.json；title "LedgerRecord"；语义为“迷你记账的单条记录”；顶层类型 object。
- 必填字段 required: ["id", "amount", "currency"]。
- additionalProperties: false——防止冗余字段混入，保证记录结构稳定。
- id: string，minLength 1，记录唯一标识。
- amount: number，multipleOf 0.01，金额精确到两位小数；决定采用数值约束（而非字符串格式化），避免字符串格式化与数值类型冲突。
- currency: string，pattern "^[A-Z]{3}$" 仅做结构校验，对应 ISO 4217 三字母货币代码；决定不做 ISO 4217 全集枚举（枚举太长），有效值校验交由应用层。
- tags: array，items 为 string 且 minLength 1；default: [] 表达“可空”（空数组语义）；未列入 required，允许缺省，可为空数组。

## 当前引用
- 材料标识：[wi-dee399de10]
- $schema: https://json-schema.org/draft/2020-12/schema
- $id: https://example.com/schemas/ledger-record.json

## 未决问题
- 材料未列出显式未决问题。唯一外委事项（schema 层不解决）：currency 的 ISO 4217 有效值校验由应用层承担，schema 仅保证 ^[A-Z]{3}$ 结构。

## 下一动作
无（材料未给出）。