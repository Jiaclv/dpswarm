校验器实现：严格依据子任务 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例，用例中的 schema 必须逐字引用 A 的交付

## 原始任务全文（含全部材料，唯一行为依据）
两阶段串行交付，第二阶段严格依赖第一阶段产出：
- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与不超过 10 行的设计说明；
- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 schema 必须逐字引用 A 的交付）。
分工提示：B 必须等 A 验收后才能开始，fission 拆且只拆 2 个 subtasks（A 与 B），为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束，不得再继续裂变。
打回重试（归因 context，处置 fix-context-and-retry）。
打回理由：交付声称『逐字引用自 [wi-cc8152c306]』的 schema 与上游 A 的实际交付不一致，逐项比对差异如下：(1) 缺失 $schema 声明与 title 字段；(2) id 丢失 minLength:1 约束；(3) tags 的 type 由 ["array","null"] 篡改为 "array"，丢失可空语义；(4) 丢失 uniqueItems:true。由此派生的 SCHEMA 常量及全部 3 个 doctest 用例均基于该被篡改的 schema，直接违反任务硬性依赖约束『用例中的 schema 必须逐字引用 A 的交付』；且校验器功能上因此无法覆盖 A 的 schema：空字符串 id 将被漏判通过、合法的 null tags 会被误判违规、重复元素不再拒绝。此外，实现代码在 'if field not in record: erro' 处截断，交付不完整，doctest 无法运行验证。依赖约束（逐字引用）相对上游交付不成立，按验收规则 reject。
任务全文：校验器实现：严格依据子任务 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例，用例中的 schema 必须逐字引用 A 的交付

## 原始任务全文（含全部材料，唯一行为依据）
两阶段串行交付，第二阶段严格依赖第一阶段产出：
- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与不超过 10 行的设计说明；
- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 schema 必须逐字引用 A 的交付）。
分工提示：B 必须等 A 验收后才能开始，fission 拆且只拆 2 个 subtasks（A 与 B），为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束，不得再继续裂变。

补充材料：
