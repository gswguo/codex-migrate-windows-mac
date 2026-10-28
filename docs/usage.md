# 使用指南（alpha）

[English](usage.en.md) · [返回首页](../README.md)

需要 Python 3.11+。本工具仅接受内置适配器识别的数据布局；遇到未知 schema、项目归属、权限或偏移格式会停止。Windows/macOS 合成测试不能代替每个桌面构建的原生真机验收。

## 下载与准备

到 [gswguo/codex-migrate-windows-mac](https://github.com/gswguo/codex-migrate-windows-mac) 下载 **Code → Download ZIP** 并解压；无需安装 Git。程序目录、私人迁移包和暂存目录保持分开。双击 `launchers` 中对应系统的启动器，或在系统终端运行它；源码不是已经包含 Python 的独立安装包。

目标电脑必须先由官方 Codex 应用创建本机数据布局；如果尚未使用，先正常打开、登录并初始化，再完全退出。遇到数据库缺失或不兼容时停止，不手建 SQLite 表。原电脑登录继续留在原电脑，目标自己的登录继续留在目标；工具不迁移或覆盖凭据。

先打开独立系统终端/启动器，再退出 Codex，避免把执行任务依赖在即将关闭的聊天窗口上。可先用只读 `inspect` 查 ID；真正导出、目标规划、修复或执行前必须退出 Codex及相关写入者。规划后保持关闭，直到应用与文件校验明确完成，再重开进行人工验收。

## 入口和数据位置

macOS 使用 `./launchers/Codex-Migrate.command`，Windows 使用 `.\launchers\Codex-Migrate.cmd`。不带参数进入向导；后接 `--help` 或命令参数进入 CLI。缺少 Python 时启动器只提示，不自动安装。`CODEX_MIGRATE_PYTHON` 可指定已有 Python 可执行文件。

开发者也可在 macOS 执行 `PYTHONPATH=src python3 -m codex_migrate --help`；PowerShell 先设置 `$env:PYTHONPATH = 'src'`，再运行 `python -m codex_migrate --help`。可选用 `python -m pip install .` 安装 `codex-migrate` 命令。以下示例采用安装后的命令；未安装时，把 `codex-migrate` 替换为上述启动器。

每个涉及本地布局的命令都可指定 `--home` 和 `--sqlite-home`。`--home` 优先，否则使用 `CODEX_HOME` 或当前用户的 `.codex`；SQLite 位置按显式参数、Codex 配置及环境变量解析。先用 `inspect` 核对实际显示的位置，不需要扫描整台电脑。WSL 与 Windows 原生数据不能因目录名相似就视为相同布局。

示例路径和 ID 均为虚构，请替换为自己的值，并把私人输出放在仓库之外。

## 先检查，再选择导出

`inspect` 只读，不要求 `--yes`：

```sh
codex-migrate inspect --home "/Users/example/.codex"
```

输出含项目 ID、聊天 ID、根目录和兼容性信息。退出源端 Codex 与相关写入者，然后显式选择项目或聊天；参数可重复：

```sh
codex-migrate export --home "C:/Users/example/.codex" --project-id "synthetic-project-id" --output "D:/PrivateTransfer/project.zip" --yes
```

也可用 `--thread-id`。必需的父聊天/历史祖先会纳入依赖闭包；无关兄弟聊天不会因此自动加入。ZIP 仅导出适配器允许的数据行与选中的文件，不复制整个源数据库或全局状态。输出包必须是新文件。

常见秘密文件、构建产物和部分目录会被排除，具体信息保留在报告中。项目及聊天仍可能含敏感信息。不要把 ZIP、日志或数据库上传到公开 issue、Git 或 CI。

## 在目标电脑生成计划

通过自己的私人渠道传送 ZIP。退出目标 Codex，选定项目存放的父目录和一个新的私人暂存目录：

```sh
codex-migrate plan --bundle "/Volumes/Transfer/project.zip" --home "/Users/example/.codex" --destination "/Users/example/Documents/ImportedProjects" --work "/Users/example/PrivateMigration/import-01" --yes
```

`--destination` 是父目录，每个项目根实际放到其下的稳定 `root-…` 子目录；共享源根只存一份。目标必须已有精确匹配的数据库，计划不会为未知版本创建 schema。

`--yes` 在此只确认写入隔离暂存区。该命令返回 `plan.json` 路径；正式应用需要下一步单独确认。检查计划中的根映射、项目与聊天范围、原有数据冲突、文件发布列表、权限变化和未解决链接。目标已有的同 ID 内容不能靠修改时间自动合并。

计划与暂存内容绑定摘要。预览后若目标聊天续写、文件移动或内容改变，应重新生成计划，不手改冻结计划强行执行。

## 确认执行与验证

确保 Codex 仍已退出：

```sh
codex-migrate apply "/Users/example/PrivateMigration/import-01/plan.json" --yes
codex-migrate verify "/Users/example/PrivateMigration/import-01/plan.json"
```

规划时把目标 `state_5.sqlite` 与 `thread_history_1.sqlite` 完整快照到私人暂存区，在副本内合并选中行并检查完整性。执行时以原文件摘要为比较条件（CAS），发布已经准备好的整份数据库及事件/JSON；同时受控处理旧 WAL/SHM 等旁文件。恢复日志逐文件记录前后状态与备份。数据库、事件文件和 JSON 不共享单个原子事务，发布中途不可启动 Codex。`verify` 核对计划与文件状态，不代表已经通过原生聊天窗口验收。

获得明确完成和验证结果后，再打开 Codex，检查：项目归属可见；所选聊天从头到尾分页无缺漏；能够继续原聊天；文件链接逐个实际打开。分别记录“目标存在”“摘要一致”“原生点击通过”。缺失附件或未纳入的外部文件可以保持未解决状态，不应宣称所有链接已恢复。

## 仅修复已有聊天链接

旧根到新根必须显式声明。路径中有空格时给整个参数加引号；`--map` 可以重复：

```sh
codex-migrate repair-links --home "/Users/example/.codex" --map "E:/Research/ProjectA=/Users/example/Documents/ProjectA" --thread-id "synthetic-thread-id" --work "/Users/example/PrivateMigration/repair-01" --yes
```

此命令**只生成修复计划**。显式选中的聊天及依赖其历史字节边界的后继进入转换范围，计划展示扩大的范围。所需祖先仅作上下文并保持原始字节；不会因为共用祖先而转换无关兄弟聊天。缺少必需对象则停止。`--thread-id` 必须显式提供，可重复；不支持省略后默认修复全部。检查后使用上面的 `apply` 和 `verify`。

识别到的 Markdown 链接目标、图片/附件字段与已知原生文本字段会同步修复；代码、命令示例、纯文字路径和链接标签保留。缺失、歧义或未支持语法不会猜测修改。文件摘要可证明内容一致；缺少旧摘要时只能标记为用户映射但未核实原文件身份。

## 中断、恢复与之后的改动

从首次目标写入到验证完成一直保持 Codex 关闭。工具崩溃或电脑重启后，**先恢复，再打开 Codex**：

```sh
codex-migrate recover "/Users/example/PrivateMigration/import-01/plan.json" --yes
```

工具停止运行时无法阻止系统启动 Codex。若中断后已打开、同步程序改写了对象或用户产生了新内容，退出相关程序、保全当前对象并查看差异。自动恢复会拒绝覆盖无法证明属于原计划的变化。不要直接用旧备份覆盖后来聊天。已完成计划再次恢复也不会撤销后续正常编辑。

停机、兼容性、文件占用或摘要检查无法确认时应停止；公共 CLI 不提供绕过空闲检查的开关。`--yes` 只是写入确认。

## 当前兼容性与容量限制

仅接受内置 `profiles/native-v1.json` 的 `native-state5-history1-61-8-alpha`，精确比对数据库对象、SQL 定义、索引和迁移记录/摘要。源/目标均须匹配；文件名相同不表示兼容。当前没有真实 Windows 桌面闭环通过声明。

| 限制 | alpha 上限 |
| --- | --- |
| 结构化规划数据 | 每个数据库 256 MiB；导入按导出 JSONL 总量，修复按选中历史字段的估算量限制 |
| 单个 JSON 记录、manifest | 各 16 MiB |
| ZIP 单成员 / 总展开量 | 8 GiB / 64 GiB |
| ZIP 成员数量 / 最大压缩比 | 100,000（含清单）/ 1000 |

空项目根和空子目录纳入清单及恢复事务。未归属项目的聊天可用 `--thread-id` 导出，其 cwd 作为独立根，不创建假项目。项目文件保留所有者执行位；POSIX 输出采用私有 `0600`（普通文件）或 `0700`（所有者可执行文件），不复制组/其他用户执行位或完整旧权限。

项目文件/事件流式处理，但结构化行仍有内存规划上限；64 GiB 包上限不代表任意同样大小聊天可导入。需要容纳解包文件、现有两份数据库的完整快照、发布暂存副本与恢复备份。当前没有准确的可用空间预算器，空间不足会报错；若正式写入已开始，按恢复流程处理。

源 `.git`、常见构建目录和秘密文件默认排除；这是项目文件快照，**不迁移 Git 历史**。Git worktree、符号链接及不支持的外部布局会拒绝。没有自动同步、往返共同基线交接或分叉聊天合并。

## 隔离合成副本的原生验证

`verify-native` 使用本机 Codex app-server 检查聊天读取与分页，可选择对副本执行 resume；不会发送模型对话请求。这个过程可能更新副本，必须 `--yes`。它仅接受无登录凭据的一次性合成测试目录，拒绝真实 Codex home 与凭据目录。不要把自己的真实 `.codex` 路径传给它。

```sh
codex-migrate verify-native "/Users/example/PrivateMigration/synthetic-check/home" --cli "/path/to/codex" --thread "synthetic-thread-id" --resume --yes
```

`--thread` 可重复；`--cli` 留空时按本机可发现能力定位。`--resume` 是可选的副本能力检查，不会创建模型任务。这是开发者验收入口，不是直接指向 `plan/native` 或真实 home 的用户导入步骤。合成副本必须含精确标记文件 `.codex-migrate-disposable`，内容为 `{"format":"codex-migrate-disposable/v1"}`；所有 rollout 路径必须实际位于副本内，且不得带凭据、配置、插件、自动化、符号链接或共享硬链接。不要仅给真实数据目录添加标记来绕过保护。先用合成数据构造完整隔离副本，再运行此命令；它不代替迁移后的项目归属、实际继续聊天和原生链接点击验收。

## 输出和退出码

CLI 正常结果写 stdout：`{"ok":true,"result":...}`；诊断写 stderr：`{"ok":false,"error":{"code":"...","message":"..."}}`。帮助和版本输出为普通文本；向导包含交互提示。

| 退出码 | 含义 |
| --- | --- |
| 0 | 命令成功返回，仍需依据结果完成对应验收 |
| 1 | 迁移、数据或 I/O 错误 |
| 2 | 参数错误或缺少写入确认 `--yes` |
| 130 | 用户取消 |

返回值中的待处理引用、原生验证状态和警告应一并检查。工具不会把文件存在等同于原生窗口实际点击成功。

最新原生合成验收与仍待完成项目见[验证记录](validation.md)。
