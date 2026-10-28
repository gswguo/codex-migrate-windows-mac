# Codex Migrate Windows–Mac

[English](README.en.md) · [使用说明](docs/usage.md) · [MIT 许可证](LICENSE)

在本机选择性导出、预检和迁移 Codex 项目与聊天，并修复历史中的本地文件链接。提供 Python 命令行、交互向导和 Windows/macOS 源码启动器。

**当前为 `0.1.0a1` alpha。** 已实现可运行的核心与合成测试；仅接受内置适配器精确匹配的数据布局。自动化测试、文件存在/摘要验证、原生聊天分页与实际点击链接是不同的验收项目。Windows 桌面端完整闭环仍待真机验收；请勿将其视为稳定发行版或全版本兼容工具。

2026-10-28：macOS 原生 CLI `0.164.0-alpha.11.1` 已通过新建合成 home、选择性项目导入及未归属聊天导入目标的读取/分页/resume；详见[验证记录](docs/validation.md)。这不代表 Windows 桌面或实际文件点击验收。

项目由社区独立开发，与 OpenAI 无隶属或背书关系。

## 下载与运行

打开 [gswguo/codex-migrate-windows-mac](https://github.com/gswguo/codex-migrate-windows-mac)，使用 **Code → Download ZIP** 下载源码并解压到普通目录。无需学习 Git，不要把程序解压到 `.codex` 或私人迁移包里面。源码 ZIP 与包含个人项目/聊天的迁移 ZIP 是两种文件。

如果目标电脑尚未使用过 Codex，先正常打开官方应用、完成自己的登录与初始化，再退出；工具需要目标已有且匹配的数据布局。源端和目标端登录各自保留，工具不复制、替换或要求上传登录凭据。

先在系统终端打开启动器，再退出 Codex 后执行导出/规划/应用。仅 `inspect` 是可在应用打开时进行的只读检查。

需要 Python **3.11+**。核心无第三方运行依赖。下载源码后，在仓库目录运行：

macOS：

```sh
./launchers/Codex-Migrate.command --help
./launchers/Codex-Migrate.command
```

Windows PowerShell：

```powershell
.\launchers\Codex-Migrate.cmd --help
.\launchers\Codex-Migrate.cmd
```

不带参数进入中英双语向导，也可双击启动器。启动器不会安装 Python；可用 `CODEX_MIGRATE_PYTHON` 指定解释器的完整路径。源码包不是包含 Python 的独立安装包。

## 工作流程

1. `inspect`：只读检查数据位置、兼容性、项目和聊天 ID。
2. `export`：显式选择项目/聊天，生成私人 ZIP。包含必需祖先，不自动导出无关聊天。
3. `plan`：在目标电脑生成隔离暂存副本与 `plan.json`，供检查。
4. `apply`：确认后发布冻结计划，保留恢复日志与备份。
5. `verify`：核对已发布文件；再完成原生窗口分页、继续聊天与文件点击验收。

已迁入的聊天可用 `repair-links` 生成单独修复计划；检查后另行 `apply`。所有写文件命令都要求 `--yes`，向导会逐次要求输入 `YES`。它不跳过空闲、兼容、冲突或摘要检查。

**停机窗口：** 导出与目标规划/写入前退出 Codex 及相关写入者。首次目标写入后一直保持关闭，直至验证完成。如果工具中断或电脑重启，先运行 `recover`，得到明确恢复结果后再打开 Codex。工具不运行时无法阻止应用启动；若中断期间发生了额外修改，保全现状并停止自动覆盖。

## 支持边界与隐私

- Windows/macOS 路径按来源语义处理，使用显式根映射、最长路径段匹配与目录约束。Markdown 仅修改识别到的链接目标，保留代码与叙述；缺失、歧义、未支持引用分别报告。
- 历史事件及展示投影同步转换，按最终 UTF-8 字节重建 turn、projection 与 `history_base` 边界。修复只转换显式选择和受影响后继，祖先作为原字节上下文保留，不修改无关兄弟聊天。
- 空项目根/子目录按事务保留；未归属项目的聊天携带独立 cwd 根，不创建假项目。项目文件仅保留所有者执行位，POSIX 文件为私有 `0600`/`0700`，不复刻完整旧权限。
- 现有目标数据受指纹和冲突检查保护；相同 ID 不做猜测合并。首版不提供多设备并发同步或自动往返交接。
- 不迁移登录凭据、钥匙串、完整全局配置、插件或自动化。项目和聊天本身仍可能含秘密，导出包不能视为已全面脱敏。
- 迁移包、项目、聊天、计划、日志和备份只应留在私人目录。公开仓库与 CI 仅接收合成数据；工具不要求上传聊天。

`verify-native` 可对无凭据的一次性合成测试副本运行 app-server 验证；它可能写入副本，也必须单独确认。真实 home 与带凭据目录会被拒绝。此结果仍不能代替原生窗口点击验收。

alpha 当前仅接受固定的 `native-state5-history1-61-8-alpha` 结构配置，精确比对 schema 对象与迁移记录，不是“所有 state_5/history_1 文件都兼容”。导入把已有目标数据库完整备份到暂存区、合并允许的数据行，再在停机窗口核对原文件摘要并逐文件发布；不是在线更新数据库，也不提供跨文件整体原子提交。

容量上限：每个数据库的结构化规划数据 256 MiB；单个 JSON 记录/清单 16 MiB；ZIP 单成员 8 GiB、总展开量 64 GiB、最多 100,000 个成员、压缩比不超过 1000。大项目可能先触及结构化规划上限。需要为解包、完整数据库快照及恢复备份准备额外磁盘空间；当前不提供准确空间预估。

详细命令、恢复说明与验收表见[使用指南](docs/usage.md)。架构见[完整设计](docs/superpowers/specs/2026-10-27-cross-platform-migration-design.md)和[历史链接修复方案](docs/history-link-repair.md)。开发与问题报告见 [CONTRIBUTING](CONTRIBUTING.md) 与 [SECURITY](SECURITY.md)。

## 开发验证

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

GitHub Actions 配置为 Windows/macOS、Python 3.11/3.12 的合成测试矩阵。CI 结果与原生桌面验收分别记录，不因有测试配置就宣称已通过全部平台。
