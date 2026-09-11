校验器实现：纯标准库 validate_record(record, schema)->list[str]，3 个 doctest，schema 逐字引用子任务 A 验收后的交付文本

## 原始任务全文（含全部材料，唯一行为依据）
两阶段串行交付，第二阶段严格依赖第一阶段产出：
- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与不超过 10 行的设计说明；
- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 schema 必须逐字引用 A 的交付）。
分工提示：B 必须等 A 验收后才能开始，fission 拆且只拆 2 个 subtasks（A 与 B），为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束，不得再继续裂变。
打回重试（归因 context，处置 fix-context-and-retry）。
打回理由：本轮交付未包含任务的实质产出：全文为逐字比对表格、依赖门控说明与收束声明，未附 validate_record 的实现代码、3 个 doctest，也未展示交付文件中实际内嵌的 SCHEMA 字面量，仅以『维持前一轮内容不变』指涉本轮不可核验的内容。据此无法验证纯标准库约束、doctest 数量与正确性、以及 schema 是否逐字引用——『逐字一致』仅是 worker 自述结论，且其在交付中自相矛盾地将标注『⏸ 状态：待验收』的 A 原文标注为『上游已验收交付』。依赖约束亦未闭环：任务要求引用『子任务 A 验收后』的交付文本，而 A 尚处待验收状态，worker 自身也请求待 A 验收后再行验收。按验收规则，此类缺少可核验材料的状态/流程报告不得 accept。请待 A 验收正式闭环后，重新提交包含完整函数实现、3 个 doctest 及逐字内嵌 SCHEMA 字面量的代码本体以供验收。
任务全文：校验器实现：纯标准库 validate_record(record, schema)->list[str]，3 个 doctest，schema 逐字引用子任务 A 验收后的交付文本

## 原始任务全文（含全部材料，唯一行为依据）
两阶段串行交付，第二阶段严格依赖第一阶段产出：
- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与不超过 10 行的设计说明；
- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 schema 必须逐字引用 A 的交付）。
分工提示：B 必须等 A 验收后才能开始，fission 拆且只拆 2 个 subtasks（A 与 B），为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束，不得再继续裂变。

补充材料：
