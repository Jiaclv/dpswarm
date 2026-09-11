基于已实现的 parse_kv 编写使用说明文档（≤200 字），交付末尾附作者署名行

## 原始任务全文（含全部材料，唯一行为依据）
两步任务：
1. 实现配置解析函数 parse_kv（key=value 文本 → dict）并附 2 个 doctest 用例
2. 编写使用说明文档（≤200 字）
附注：交付末尾请附上作者署名行。
分工提示：fission 拆且只拆 2 个 subtasks（1 与 2），2 标 deps=[0]；全部验收通过后 Lead 决策 accept 收束。

## 上游交付交接（三层：L2 摘要导航 / L1 逐字依据 / L0 原文回查）（handoff_profile=semantic）

### L2 摘要层（摘要仅供导航，逐字内容以 L1/原文为准）
## 目标
交付将 key=value 形式文本解析为 dict 的轻量配置读取函数 parse_kv，并附 ≤200 字使用说明，适用于轻量配置文件读取场景。

## 已验收决定与不变量
1. 接口签名（交付 1）：`def parse_kv(text: str) -> dict`；`__main__` 分支执行 `doctest.testmod(verbose=False)`。
2. 解析规则（不变量）：
   - 按行解析；忽略空行；
   - 忽略 `#` 开头的注释行（strip 后以 `#` 起始即被忽略）；
   - 键值两侧空白剥离；
   - 仅按第一个 `=` 分割，值中可包含 `=`；
   - 无 `=` 的行跳过；
   - 值一律保留为字符串类型，不做类型转换；返回 dict 可直接用 `config["host"]` 取值。
3. doctest 共 2 个，内嵌字面输入输出对：
   - 用例 1（多行基本解析，值为普通字符串）：`parse_kv("host=127.0.0.1\\nport=8080")` → `{'host': '127.0.0.1', 'port': '8080'}`
   - 用例 2（值中含 `=`、空行跳过、`#` 注释行跳过、键值两侧空白剥离）：`parse_kv("token=abc=def\\n\\n  # 注释将被忽略\\nname =  db ")` → `{'token': 'abc=def', 'name': 'db'}`
4. 使用说明（交付 2）：正文 156 字，符合 ≤200 字限制；含示例调用 `parse_kv("host=1.2.3.4\\nport=80")`。

## 当前引用
- 上游 item 标识：[wi-81c3a7f7ee]（作者：DPswarm worker）
- 交付 1 = parse_kv 实现（含 2 个 doctest），逐字载体为 fenced code block；交付 2 = ≤200 字使用说明
- 逐字代码与 doctest 原文由下游查 L1/L0，本摘要不承担逐字保真

## 未决问题
无

## 下一动作
无

### L1 原子事实层（逐字提取；逐字引用请以此层为准）
[wi-81c3a7f7ee]
```python
def parse_kv(text: str) -> dict:
    """将 key=value 形式的文本解析为 dict。

    规则：按行解析；忽略空行；去除首尾空白；
    值中可包含 '='，仅按第一个 '=' 分割。

    >>> parse_kv("host=127.0.0.1\\nport=8080")
    {'host': '127.0.0.1', 'port': '8080'}
    >>> parse_kv("token=abc=def\\n\\n  # 注释将被忽略\\nname =  db ")
    {'token': 'abc=def', 'name': 'db'}
    """
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue  # 无 '=' 的行跳过
        result[key.strip()] = value.strip()
    return result


if __name__ == "__main__":
    import doctest
    doctest.testmod(verbose=False)
```

1.2.3.4

### L0 原文层（artifact 引用，provenance 链不断）
- [wi-81c3a7f7ee] submission_package=dep-c7fbaf83fc；逐字原文：输出一行 `PULL: wi-81c3a7f7ee` 经 §5.4 兜底通道取回