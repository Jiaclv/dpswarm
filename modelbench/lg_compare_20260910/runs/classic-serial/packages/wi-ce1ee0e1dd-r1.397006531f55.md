子任务B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（空列表=通过），附 3 个 doctest 用例，用例中的 schema 必须逐字引用 A 的交付
打回重试（归因 context，处置 fix-context-and-retry）。
打回理由：两项硬伤：(1) 交付物本身不完整，与「其余交付物已全部完成」的声明相矛盾——代码在 _check_node 处截断（t = s 引用未定义名，函数体未完成），且 doctest 为 0 个（任务硬性要求 3 个、schema 逐字引用 A 的交付），因此两遍校验顺序、错误格式承诺均无法验证，仅此一项即不可验收；(2) 阻塞根因核验成立：重试前置条件确实未满足，B 的上下文中只有指针 pkg://wi-ce1ee0e1dd-r1/49a4c3104720 而无 schema 原文，B 拒绝编造「逐字引用」是正确行为，避免了重犯上一轮错误。根因是依赖内容注入失败，而非执行者能力或任务描述缺陷。处置：将 A 验收后的 schema 原文全文直接注入 B 的任务上下文（而非仅包指针），或为其开通包内容读取通道后再重试；已交付的实现骨架方向正确（integer/number 排除 bool、两遍遍历设计、纯标准库均符合预承诺），schema 真正到位后补全 _check_node 并填入 3 个 doctest 即可，无需推翻重写。
任务全文：子任务B「校验器实现」：严格依据 A 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（空列表=通过），附 3 个 doctest 用例，用例中的 schema 必须逐字引用 A 的交付

补充材料：
