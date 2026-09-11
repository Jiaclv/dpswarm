校验器实现：严格依据子任务 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例，用例中的 schema 必须逐字引用 A 的交付
打回重试（归因 context，处置 fix-context-and-retry）。
打回理由：交付不满足验收硬性要求且本体不完整：(1) 任务要求 doctest 用例中的 schema 逐字引用子任务 A 验收后的交付文本，但交付方声明会话中未收到 A 的交付，仅以 A_DELIVERED_SCHEMA_1/2/3 占位块标注，验收方无法逐字比对核验，'逐字引用'这一验收点直接落空；(2) 代码在第一个 doctest 中途截断（'A_DELIVERED_' 处中断），validate_record 函数体及第 2、3 个 doctest 完全缺失，无法运行 doctest 验证，交付物不构成可审阅的完整模块。就已完成部分看：消息格式约定稳定可排序、bool 优先于 int/number 的类型判定、未知类型保守判失败等解释层设计均合理，说明执行者具备实现能力，任务描述本身也无歧义或自相矛盾之处；失败根因在于上游依赖（A 的验收后 schema 原文）未被注入本子任务上下文，属于环境/上下文缺失而非能力或描述问题。处置建议：将 A 的 schema 原文逐字替换三处占位块（含空格与键序）、补全 validate_record 函数体与 3 个 doctest 后重新送审，届时需重点复核占位替换是否真正逐字、doctest 是否全部通过。
任务全文：校验器实现：严格依据子任务 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例，用例中的 schema 必须逐字引用 A 的交付

补充材料：
