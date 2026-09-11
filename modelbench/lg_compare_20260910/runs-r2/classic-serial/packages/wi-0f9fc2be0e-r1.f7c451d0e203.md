校验器实现：严格依据子任务 0 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（空列表=通过），附 3 个 doctest 用例；用例中 schema 必须逐字引用子任务 0 的交付文本
打回重试（归因 context，处置 fix-context-and-retry）。
打回理由：不可验收，三重问题：(1) 硬性条款未满足——本会话未附子任务 0 验收后的 schema 文本，'用例中 schema 必须逐字引用交付文本'无法执行；交付方拒绝伪造并请求补充，该升级行为纪律正确，但不能因此豁免条款本身。(2) 代码本体不完整：末行在 `spec.get("type") == "array" a` 处截断（语法错误，无法运行），docstring 声称支持的 items/min/max 分支缺失，与文中'其余部分均已完成'的声明自相矛盾。(3) doctest 期望值写作双引号（如 ["id: expected integer, got str"]），而 Python 对字符串列表的 repr 输出单引号，即使补全代码，三条用例也全部失败。处置：orchestrator 先补充子任务 0 的 schema 原文，交付方补全截断的数组处理分支、将 doctest 期望值改为单引号后重新提交。归属 context：主导阻塞是上游 schema 文本缺失这一上下文缺口，任何能力充分的交付方在本会话同样无法满足逐字引用条款；代码截断与引号缺陷为次要、可修复问题。
任务全文：校验器实现：严格依据子任务 0 验收后的 schema 文本，用纯标准库实现 validate_record(record: dict, schema: dict) -> list[str]（空列表=通过），附 3 个 doctest 用例；用例中 schema 必须逐字引用子任务 0 的交付文本

补充材料：
