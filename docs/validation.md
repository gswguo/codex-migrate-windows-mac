# 验证记录 / Validation record

日期 / Date: **2026-10-28**。工具版本 / Tool: **0.1.0a1 alpha**。公开仓库：[gswguo/codex-migrate-windows-mac](https://github.com/gswguo/codex-migrate-windows-mac)。

本页只记录人工构造数据的验证结果，不包含私人迁移数据、个人路径、会话 ID 或私人案例统计。本机最终测试：**144 项通过**，包含两项启用真实 CLI 的一次性合成集成测试。GitHub CI 结果以仓库 Actions 页面为准。

This record covers invented synthetic data only. It includes no private migration data, personal paths, conversation IDs, or private-case statistics. The final local suite passed **144 tests**, including two enabled disposable integrations against the actual CLI. See repository Actions for GitHub CI results.

## 已执行的原生接口验证 / Native interface checks performed

环境为 macOS 上的本机 Codex CLI **`0.164.0-alpha.11.1`**，数据库固定配置为 **`native-state5-history1-61-8-alpha`**。使用带一次性标记、无凭据、无真实用户 home 的合成副本。

The native CLI was **`0.164.0-alpha.11.1` on macOS**, with pinned database profile **`native-state5-history1-61-8-alpha`**. Inputs were explicitly marked disposable synthetic homes without credentials or a real user home.

| 场景 / Scenario | 实际结果 / Observed result | 结果边界 / Limit |
| --- | --- | --- |
| 新建合成 home / Fresh synthetic home | 原生读取、分页与 resume 通过 / Native read, pagination, and resume passed | 不代表任意旧历史格式 / Does not cover arbitrary history formats |
| 选择性项目导出后导入另一个合成目标 / Selective project export and import into another synthetic target | 3 个聊天、3 个 turn、3 个 item 的原生读取与 resume 通过 / Native read and resume passed for 3 chats, 3 turns, and 3 items | 仅合成数据与指定 CLI / Synthetic data and this CLI only |
| 未归属项目的聊天导入目标 / Import of an unassigned conversation | 原生读取与 resume 通过，cwd 作为独立根保留，不创建假项目 / Native read and resume passed; cwd retained as an independent root without inventing a project | 不代表自动项目合并 / Does not establish automatic project merging |

这些请求没有发送模型任务。app-server 可改变合成副本，因此原生验证不是对真实 home 的只读操作。

No model task was sent. The app-server may mutate a disposable copy; this verification is not a read-only operation against a production home.

## 实现约束与检查范围 / Implementation constraints and check scope

- 项目可执行文件仅保留文件所有者的执行位；POSIX 发布文件采用私有 `0600` 或 `0700`，不复制组/其他用户执行位、setuid 等权限。Windows 权限语义不同，不以 POSIX 位证明 Windows 桌面验收。
- 空项目根和空子目录纳入清单与事务目录操作，恢复仅清理能证明由本事务创建且仍安全的目录。
- 原地链接修复转换显式选择及受影响后继；所需祖先仅提供上下文、按原字节保留，不因祖先存在而修改无关兄弟聊天。计划分别列出实际转换聊天与上下文祖先。
- 导入在完整目标数据库快照中合并，再按原目标摘要逐文件发布；恢复保留计划外的新变化。它不提供跨文件单次原子提交。

Project executability preserves only the owner's execute bit. POSIX output files use private `0600`/`0700`, without group/other execution or setuid bits. Empty roots/subdirectories are included in journaled directory operations. Link repair transforms explicitly selected chats and affected descendants; required ancestors are byte-preserved context and do not pull unrelated siblings into repair. Import merges into complete staged database snapshots before fingerprint-checked per-file publication.

这些行为由对应合成回归测试约束；原生测试使用环境变量 `CODEX_MIGRATE_NATIVE_TEST_CLI` 指定 Codex 可执行文件，默认跳过，不会自动使用真实用户数据。

Synthetic regression tests cover these behaviors. Native tests use `CODEX_MIGRATE_NATIVE_TEST_CLI` to select the Codex executable and skip by default; they never automatically use real user data.

## 尚未验证 / Not yet established

- 真实 Windows 桌面的导出→导入→重启→继续聊天完整闭环。
- 所有目标桌面版本中的项目归属显示、用户实际继续聊天和历史文件链接逐个点击打开。
- 任意 Codex 版本、任意原生格式、全部 Markdown/工具输出语法或多设备并发同步。
- 含 Python 的独立安装器、签名后的 Windows/macOS 可执行发行包。

The real Windows desktop workflow remains unverified. Native RPC success does not establish every desktop build's visible membership, actual user continuation, or file-link clicks. This is not support for every Codex version or syntax, concurrent synchronization, or a signed standalone installer. Source launchers still require an existing **Python 3.11+** interpreter.
