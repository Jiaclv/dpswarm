校验器实现（修复缺口：需求清单第 1 项）

## 原始任务全文（含全部材料，唯一行为依据）
两阶段串行交付，第二阶段严格依赖第一阶段产出：
- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与不超过 10 行的设计说明；
- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 schema 必须逐字引用 A 的交付）。
分工提示：B 必须等 A 验收后才能开始，fission 拆且只拆 2 个 subtasks（A 与 B），为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束，不得再继续裂变。
打回重试（归因 context，处置 fix-context-and-retry）。
打回理由：交付为「缺少材料/无法完成」式阻塞报告，而非任务实质产出：未包含任何 validate_record 实现代码或 doctest 用例，按验收规则不得 accept。交付者拒绝在缺少 A 验收后 schema 原文的情况下凭字段需求自行重构并冒充原文，这一依赖约束本身成立（可读上下文中仅有 pkg://wi-bc9abce202-r1/28b757f0af72 指针，无逐字原文，强行伪造依赖材料不可接受），因此阻塞不归因于交付者能力，而归因于上游已验收的 schema 原文未被内联注入可读上下文。补救动作：将 A 验收后的 ```json 代码块逐字内联注入任务上下文后重派，交付者已承诺届时可直接交付完整实现与 3 个 doctest 用例。
任务全文：校验器实现（修复缺口：需求清单第 1 项）

## 原始任务全文（含全部材料，唯一行为依据）
两阶段串行交付，第二阶段严格依赖第一阶段产出：
- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与不超过 10 行的设计说明；
- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 schema 必须逐字引用 A 的交付）。
分工提示：B 必须等 A 验收后才能开始，fission 拆且只拆 2 个 subtasks（A 与 B），为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束，不得再继续裂变。

补充材料：
