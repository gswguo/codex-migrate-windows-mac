"""Small bilingual source-run wizard that shares the confirmed CLI boundary."""
from __future__ import annotations


def run_wizard(*, input_fn=None, output_fn=None, execute=None):
    if input_fn is None:
        input_fn = input
    if output_fn is None:
        output_fn = print
    if execute is None:
        from .cli import main
        execute = main
    output_fn('Codex Migrate alpha · 本地迁移向导 / Local migration wizard')
    output_fn('先阅读计划。写入期间保持 Codex 关闭；中断后先恢复再启动。 / Review the plan. Keep Codex closed during writes; recover before reopening after interruption.')
    def ask(prompt):
        return input_fn(prompt).strip()
    def home_args():
        home = ask('Codex home（留空使用配置 / blank for default）: ')
        sqlite_home = ask('SQLite home（留空使用配置 / blank for configured location）: ')
        return (['--home', home] if home else []) + (['--sqlite-home', sqlite_home] if sqlite_home else [])
    try:
        while True:
            output_fn('1 检查 / Inspect\n2 导出 / Export\n3 生成导入计划 / Plan import\n4 生成链接修复计划 / Plan link repair\n5 执行计划 / Apply plan\n6 恢复 / Recover\n7 验证 / Verify\n8 隔离副本原生验证 / Native verification on disposable copy\n0 退出 / Exit')
            choice = ask('选择 / Choose: ')
            if choice == '0':
                return 0
            writing = choice in {'2', '3', '4', '5', '6', '8'}
            if choice == '1':
                args = ['inspect'] + home_args()
            elif choice == '2':
                args = ['export'] + home_args()
                output = ask('新迁移包路径 / New ZIP path: ')
                projects = ask('项目 ID（逗号分隔）/ Project IDs (comma separated): ')
                threads = ask('聊天 ID（逗号分隔）/ Thread IDs (comma separated): ')
                args += ['--output', output]
                for flag, values in [('--project-id', projects), ('--thread-id', threads)]:
                    for value in values.split(','):
                        if value.strip():
                            args += [flag, value.strip()]
            elif choice == '3':
                args = ['plan'] + home_args()
                for flag, prompt in [('--bundle', '迁移包 / Bundle ZIP: '), ('--destination', '最终项目目录 / Final project directory: '), ('--work', '新的暂存目录 / New staging directory: ')]:
                    args += [flag, ask(prompt)]
            elif choice == '4':
                args = ['repair-links'] + home_args()
                output_fn('只生成计划；检查后另选执行。 / Creates a plan only; apply after review.')
                while True:
                    old = ask('旧根路径（留空结束）/ Old root (blank to finish): ')
                    if not old:
                        break
                    args += ['--map', old + '=' + ask('新根路径 / New root: ')]
                threads = ask('必填聊天 ID（逗号分隔）/ Required thread IDs (comma separated): ')
                if not any(value.strip() for value in threads.split(',')):
                    output_fn('必须显式选择聊天；已取消 / Explicit thread selection required; cancelled')
                    continue
                for value in threads.split(','):
                    if value.strip():
                        args += ['--thread-id', value.strip()]
                args += ['--work', ask('新的暂存目录 / New staging directory: ')]
            elif choice == '8':
                output_fn('仅限无凭据的一次性合成测试副本，禁止真实 home。 / Disposable synthetic copy without credentials only; never use a real home.')
                args = ['verify-native', ask('合成测试副本 HOME / Disposable synthetic HOME: ')]
                cli = ask('Codex CLI 路径（空=发现）/ CLI path (blank=discover): ')
                if cli:
                    args += ['--cli', cli]
                for value in ask('合成聊天 ID（逗号分隔）/ Synthetic thread IDs (comma separated): ').split(','):
                    if value.strip():
                        args += ['--thread', value.strip()]
                if ask('测试副本 resume？输入 YES / Exercise resume on copy? Type YES: ') == 'YES':
                    args.append('--resume')
            elif choice in {'5', '6', '7'}:
                args = [{'5': 'apply', '6': 'recover', '7': 'verify'}[choice], ask('计划文件 / Plan JSON path: ')]
            else:
                output_fn('无效选择 / Invalid choice')
                continue
            if writing:
                output_fn('即将执行 / Command arguments: ' + repr(args))
                if ask('确认允许此命令写文件，输入 YES / Type YES to confirm file writes: ') != 'YES':
                    output_fn('已取消 / Cancelled')
                    continue
                args.append('--yes')
            status = execute(args)
            if status:
                output_fn('命令失败，请查看诊断；若已开始写目标，保持 Codex 关闭并先恢复。 / Command failed; review diagnostics. If target writes began, keep Codex closed and recover first.')
    except (EOFError, KeyboardInterrupt):
        output_fn('已取消；若已开始写目标，先恢复再打开 Codex。 / Cancelled; recover before reopening Codex if target writes began.')
        return 130
