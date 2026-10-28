# 源码发布与私人数据边界

公开源码目标是 [gswguo/codex-migrate-windows-mac](https://github.com/gswguo/codex-migrate-windows-mac)。本页描述维护者发布要求，不宣称某次推送、CI、原生验证或 Release 已完成。

普通用户无需学习 Git：在仓库页选择 **Code → Download ZIP**，解压后按[使用指南](usage.md)运行源码启动器。源码 ZIP 不包含 Python，也不是私人迁移 ZIP。只有经过相应平台验证的打包资产才能被描述为独立发行包。

维护者只发布本目录的源码、文档、许可证、CI 配置与合成测试。不得添加真实项目、聊天、附件、迁移 ZIP、SQLite、凭据、恢复备份、私人日志或截图。`.gitignore` 不能清除已经进入 Git 历史的秘密；推送前检查提交清单和历史。

维护者完成独立代码审查、合成测试及安装入口检查后，使用本人已认证的 GitHub 工具发布到上述仓库。不要把访问令牌写进命令记录、脚本、文档或远程 URL，也不要从含私人迁移数据的父目录初始化或推送仓库。

发布说明需要分别列明：

- 工具版本、固定原生 profile，以及仍待验证的 Codex/OS 组合。
- Windows/macOS CI 的实际结果；配置存在不等于已经通过。
- 原生分页、resume、重启、项目归属和链接点击各自的实际验收状态。
- 完整数据库暂存快照、逐文件摘要比较发布、停机与恢复条件。
- 256 MiB/数据库结构化规划上限、ZIP 限制与其他不支持范围。
- 如有 Release 资产，明确源码包或独立安装包、系统/架构、SHA256、签名与已验证范围。

当前为 alpha，未完成真实 Windows 桌面闭环前不得写“Windows 稳定可用”。没有对应平台的打包和签名验证，不得宣传“所有设备双击即可无提示运行”。MIT 许可证已经落在根目录 [LICENSE](../LICENSE)；问题复现和私人报告规则见 [CONTRIBUTING](../CONTRIBUTING.md) 与 [SECURITY](../SECURITY.md)。
