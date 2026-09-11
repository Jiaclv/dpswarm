schema 设计（已验收：wi-cfccd04196；保留为结构节点与 B 的依赖源，勿重复执行）

## 原始任务全文（含全部材料，唯一行为依据）
两阶段串行交付，第二阶段严格依赖第一阶段产出：
- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与不超过 10 行的设计说明；
- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 schema 必须逐字引用 A 的交付）。
分工提示：B 必须等 A 验收后才能开始，fission 拆且只拆 2 个 subtasks（A 与 B），为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束，不得再继续裂变。
打回重试（归因 context，处置 fix-context-and-retry）。
打回理由：交付为空，未包含任何 schema 设计的实质产出，不满足『交付内容必须是任务的实质产出』，亦不满足 accept 条件；该情形等同无实质交付的阻塞/占位状态，按规则不得 accept。任务注明『已验收：wi-cfccd04196；保留为结构节点，勿重复执行』可解释交付为空的原因（实际产物在已验收的 wi-cfccd04196 中，本节点为结构占位），但该说明只是任务元数据，不构成可核验的交付证据：无引用可 FETCH 回查，无内容可按版本核对，无法确认 wi-cfccd04196 的产物与 schema 设计相符。归因 context：交付为空源于该节点作为已验收结构占位的编排安排，而非执行方能力缺陷或任务描述错误；建议以 wi-cfccd04196 的既有验收记录做账面关联来满足 B 的依赖，而非将本节点视为工作失败重新执行。
任务全文：schema 设计（已验收：wi-cfccd04196；保留为结构节点与 B 的依赖源，勿重复执行）

## 原始任务全文（含全部材料，唯一行为依据）
两阶段串行交付，第二阶段严格依赖第一阶段产出：
- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与不超过 10 行的设计说明；
- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 schema 必须逐字引用 A 的交付）。
分工提示：B 必须等 A 验收后才能开始，fission 拆且只拆 2 个 subtasks（A 与 B），为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束，不得再继续裂变。

补充材料：
