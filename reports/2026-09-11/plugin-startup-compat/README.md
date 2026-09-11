# DPH / DPswarm 启动兼容修复（2026-09-11）

用户报错：`Failed to load plugins` → `dpswarm-dsh-plugin` → `Cannot read properties of undefined (reading 'settings')`。

## 原因与修复

本机全局 DSH CLI 为 0.1.5-rc.1（其客户端包为 0.1.5-rc.2），web profile 中插件为 0.9.7。新版连接服务已移除 `connection.api`，插件前端仍在 apply 中读取 `connection.api.settings`，导致浏览器插件加载失败。与模型 API key 无关。

工作区及已安装插件的 `lib/client.js` 同步了同一最小修复：

- 可选依赖新版 `remote`，仍支持没有此服务的旧版宿主。
- 新版保存走 `remote.settings.mutate(ns, ops, expectedRevision)`，刷新走 `remote.settings.describe()`。
- 新版模型目录走 `remote.session.modelCatalog()`；返回值在边界适配为现有 UI 的响应结构。
- 保留明确的保存确认、冲突拒绝、只读连接限制与旧 `connection.api` 分支。

已安装目录：`C:/Users/93711/.dsh/profiles/web/node_modules/dpswarm-dsh-plugin/lib/client.js`。仅更新该文件，没有安装工作区中其他尚未发布的 0.10.0 改动。

## 核验

- 使用备份的原始已安装文件复现了完全相同的 `undefined.settings` 异常。
- 修复后 `node --test dpswarm-dsh-plugin/tests/client-startup.test.mjs`：3/3 通过。
- `tests/client-host-compat-browser.mjs`：旧接口、新 Remote、Remote 只读连接三组 Chromium 回归均通过，页面错误为 0。
- 浏览器验证了模型目录、选中但未保存、保存拒绝后保留原值及草稿、成功保存时的 revision/字段传递、保存模型不启用任务、只读连接拒绝写入。
- 源码与已安装文件 SHA256 一致；已安装文件语法检查通过。外部模型调用为 0。

具体日志、截图、安装校验和及备份位于 `.tmp/plugin-startup-20260911/`。浏览器回归复用了 `tests/client-browser.mjs` 生成的 React fixture，设置与目录均为隔离数据。

实际 3080 页面受 DSH 登录保护，独立无头浏览器仅获得正常的认证提示，未复核用户已登录的页面。需重启 DPH 并刷新原页面，使已有进程/浏览器重新加载修复文件。

排查时另运行过未修改的服务端 `tests/index.test.mjs`：3/4 通过，旧 settings mock 缺少新版 `installSection` 方法。该测试夹具版本问题与此次浏览器 `connection.api` 报错不同，本次未扩展修改范围；未声明全仓库测试通过。
