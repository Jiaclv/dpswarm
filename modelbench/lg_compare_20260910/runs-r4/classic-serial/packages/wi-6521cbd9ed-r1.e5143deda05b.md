子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例；用例中的 schema 必须逐字引用 A 的交付文本，不得改写。

## 原始任务全文（含全部材料，唯一行为依据）
两阶段串行交付，第二阶段严格依赖第一阶段产出：
- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与不超过 10 行的设计说明；
- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 schema 必须逐字引用 A 的交付）。
分工提示：B 必须等 A 验收后才能开始，建议 fission 拆 2 个 subtasks 并为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束。
打回重试（归因 context，处置 fix-context-and-retry）。
打回理由：交付违反 deps=[0] 的逐字引用硬约束：常量 MINI_LEDGER_SCHEMA 自称『A 交付文本逐字副本（仅 json.loads，无任何改写）』，但与上游已验收 JSON 实质不符：(1) 省略顶层键 $schema、$id、title；(2) 删除 id 的 minLength:1 约束；(3) 将 tags 的 type 由 ["array","null"] 篡改为 "array"，丢失可空语义。这属于实质改写而非逐字引用，并直接导致校验行为偏离 A 的 schema（tags:null 会被误判违规、id 为空串会漏检）。此外，交付代码在 `if key not in schema.get("properties` 处截断，函数体不完整（缺少 additionalProperties 收尾、pattern/multipleOf/items 检查与 return），按所交文本存在语法错误、不可运行，三个 doctest 无法核验。实质产出不完整且依赖约束相对上游交付不成立，故 reject；B 应以 A 的 JSON 逐字重建常量（保留 minLength 与可空 tags），补全函数体后重跑 doctest 再交付。
任务全文：子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例；用例中的 schema 必须逐字引用 A 的交付文本，不得改写。

## 原始任务全文（含全部材料，唯一行为依据）
两阶段串行交付，第二阶段严格依赖第一阶段产出：
- 子任务 A「schema 设计」：为一个「迷你记账记录」设计 JSON Schema——字段含 id(字符串)、amount(数值，两位小数)、currency(三字母代码)、tags(字符串数组，可空)；交付一个 ```json 代码块（schema 全文）与不超过 10 行的设计说明；
- 子任务 B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（返回违规信息列表，空列表=通过），附 3 个 doctest 用例（用例里的 schema 必须逐字引用 A 的交付）。
分工提示：B 必须等 A 验收后才能开始，建议 fission 拆 2 个 subtasks 并为 B 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束。

补充材料：
