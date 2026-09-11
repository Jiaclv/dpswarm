子任务B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（空列表=通过），附 3 个 doctest 用例，用例中的 schema 必须逐字引用 A 的交付
打回重试（归因 context，处置 fix-context-and-retry）。
打回理由：子任务B 要求的产出（validate_record 实现代码 + 3 个逐字引用 A schema 的 doctest 用例）未交付，当前仅是一份阻塞报告，故不能通过验收。但阻塞本身合理且处理得当：A 验收后的 schema 文本确实未传入 B 的上下文，执行者拒绝凭空编造并声称『逐字引用』是正确行为，避免了虚假交付与后续 doctest 全部失效的风险。归因是上下文缺失而非执行者能力问题。处置建议：不要更换或惩罚该执行者，应将 A 的 schema 原文注入 B 的上下文后原样重派；可顺带采纳其预承诺的输出规范（『<字段路径>: <原因>』点号路径格式、先缺字段→后类型→后额外约束的固定校验顺序）作为本次验收的补充标准。
任务全文：子任务B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（空列表=通过），附 3 个 doctest 用例，用例中的 schema 必须逐字引用 A 的交付

补充材料：
