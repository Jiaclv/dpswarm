# 开发与验证

从仓库根目录执行以下命令。日常贡献只需要离线检查，不需要 API Key，也不需要运行真实模型实验。

## Python 控制面

```sh
python -m pip install -e "./dpswarm-plugin[dev]"
python -m pytest dpswarm-plugin/tests -q
```

Python 最低版本由 pyproject.toml 声明，CI 使用 Python 3.12。LangGraph 等可选依赖和真实 provider 用例有额外前提；跳过情况必须保留在验证结果中。MockProvider 成功不代表真实模型完成了任务。

当前 `mock_single.json` 的 CLI 路径存在顶层结果与条目状态不一致的已知限制，暂不作为成功任务演示。README 的快速开始只展示初始化、状态读取与回放；任务执行与验收按各自测试断言检查。

## JavaScript 插件

```sh
npm --prefix dpswarm-dsh-plugin run test:unit
```

该命令运行无需安装 DSH 的纯单元检查。完整集成套件使用：

```sh
npm --prefix dpswarm-dsh-plugin test
```

完整套件要求本机安装 DSH、Node.js 和可用 Python。当前集成验证使用 DSH `0.1.5-rc.1`；其他版本需重新检查公开接口。非标准宿主安装位置可设置 `DSH_HOST_ROOT`，目标目录需包含 `dsh-tools` 与 `schemastery`，而不是插件目录。`DPSWARM_TEST_PYTHON` 指定测试使用的 Python；安装器与其他工具的 Python 选择以各自帮助为准。测试可能启动临时本地 HTTP 服务，仍不代表真实模型端到端验证。

## 打包

```sh
python dpswarm-dsh-plugin/bin/pack_plugin.py dpswarm-dsh-plugin .tmp/packages
python -m pip wheel --no-deps --no-build-isolation ./dpswarm-plugin --wheel-dir .tmp/wheels
```

两种分发包均携带当前 LICENSE。DSH 插件按声明的文件清单生成内容哈希归档；不要以相同版本号推断磁盘文件和已运行进程一致。安装或更新后重启目标 profile，并核查连接的控制服务能力。不要清理仍被日常配置引用的安装目录。

## 公开内容

仓库保留源码、测试、通用文档、冻结实验合同和整理后的研究证据。凭据、本机部署备份、私人会话、原始模型工作区、锁文件、测试输出与内部执行计划留在本地。

更新公开报告时注明输入来源、版本、分母和变换。历史快照中的哈希与数值不因代码升级而重新解释。`scripts/publish_reports.py` 依赖未随仓库分发的完整分析归档及可选报告构建工具；重建 HTML 需明确配置 `DP_REPORT_BUILDER_DIR`。现成公开快照可直接阅读，不要求该工具。

历史 Codex 实验适配器通过 `DPSWARM_CODEX_JS` 指定本机安装的 `bin/codex.js`；它不属于默认产品入口或无凭据 CI。真实实验会产生调用成本，按实验说明另行配置和运行。
