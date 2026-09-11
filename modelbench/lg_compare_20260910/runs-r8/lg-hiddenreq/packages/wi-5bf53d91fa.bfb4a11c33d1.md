基于已实现的 parse_kv 编写使用说明文档（≤200 字），末尾附作者署名行

## 原始任务全文（含全部材料，唯一行为依据）
两步任务：
1. 实现配置解析函数 parse_kv（key=value 文本 → dict）并附 2 个 doctest 用例
2. 编写使用说明文档（≤200 字）
附注：交付末尾请附上作者署名行。
分工提示：fission 拆且只拆 2 个 subtasks（1 与 2），2 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束。

## 上游交付交接（三层：L2 摘要导航 / L1 逐字依据 / L0 原文回查）（handoff_profile=semantic）

### L2 摘要层（摘要仅供导航，逐字内容以 L1/原文为准）
## 目标
交付配置解析函数 parse_kv：将多行 key=value 格式文本解析为 dict（[wi-2def439b3f]）。

## 已验收决定与不变量
- 接口契约：调用 `parse_kv(text)` 传入多行 key=value 文本，返回 dict；值始终为字符串，类型转换由调用方后续自行处理。
- 解析不变量：逐行处理，键与值两侧空白自动去除；空行与以 `#` 开头的注释行被跳过；按首个 `=` 分割为键值。
- doctest 内嵌于函数 docstring，且含 `if __name__ == "__main__"` 下的 `doctest.testmod()` 自验入口。验收样例（原样语义）：
  - `parse_kv("host=127.0.0.1\nport=8080")` → `{'host': '127.0.0.1', 'port': '8080'}`
  - 输入含注释行 `# 注释与空行被忽略`、空行、`name = app `（键值两侧带空白）→ `{'name': 'app'}`
- 使用说明交付约束：篇幅 ≤200 字。

## 当前引用
- 上游 item：[wi-2def439b3f]「交付：parse_kv 配置解析」，作者 DPswarm worker。
- 实现代码与 doctest 逐字原文内嵌于该 item，下游按 L1/L0 查取，摘要不承担逐字保真。

## 未决问题
无

## 下一动作
无

### L1 原子事实层（逐字提取；逐字引用请以此层为准）
[wi-2def439b3f]
```python
def parse_kv(text):
    """解析 key=value 格式文本为 dict。

    >>> parse_kv("host=127.0.0.1\\nport=8080")
    {'host': '127.0.0.1', 'port': '8080'}

    >>> parse_kv("# 注释与空行被忽略\\n\\nname = app \\n")
    {'name': 'app'}
    """
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result

if __name__ == "__main__":
    import doctest
    doctest.testmod()
```

### L0 原文层（artifact 引用，provenance 链不断）
- [wi-2def439b3f] submission_package=dep-570f9398e9；逐字原文：输出一行 `PULL: wi-2def439b3f` 经 §5.4 兜底通道取回