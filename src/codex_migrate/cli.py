"""Dependency-free command-line entry point with explicit write confirmation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from .errors import MigrationError


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise MigrationError(message, code='usage')


def _mapping(value):
    old, separator, new = value.partition('=')
    if not separator or not old or not new:
        raise argparse.ArgumentTypeError('mapping must be OLD_ROOT=NEW_ROOT')
    return old, new


def _layout(parser):
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))), help='Codex home (default: CODEX_HOME or ~/.codex)')
    parser.add_argument('--sqlite-home', type=Path, help='Explicit SQLite home; otherwise resolve Codex configuration')


def _confirmed(parser):
    parser.add_argument('--yes', action='store_true', help='Confirm this command may write its declared output or target')


def build_parser():
    parser = _Parser(prog='codex-migrate', description='Local Codex migration alpha. Review plans before applying; keep Codex closed through recovery and verification.')
    parser.add_argument('--version', action='version', version='codex-migrate 0.1.0a1')
    commands = parser.add_subparsers(dest='command', required=True, parser_class=_Parser)
    inspect = commands.add_parser('inspect', help='Read inventory and compatibility without writing')
    _layout(inspect)
    export = commands.add_parser('export', help='Write a new selective private migration ZIP')
    _layout(export)
    export.add_argument('--output', '-o', type=Path, required=True)
    export.add_argument('--project-id', action='append', help='Project to include; may be repeated')
    export.add_argument('--thread-id', action='append', help='Thread to include; may be repeated')
    _confirmed(export)
    plan = commands.add_parser('plan', help='Stage an import and write a reviewable plan; does not apply it')
    _layout(plan)
    plan.add_argument('--bundle', type=Path, required=True)
    plan.add_argument('--destination', type=Path, required=True, help='Final project files directory')
    plan.add_argument('--work', type=Path, required=True, help='New private staging directory')
    _confirmed(plan)
    repair = commands.add_parser('repair-links', help='Stage existing-history link repair; apply separately')
    _layout(repair)
    repair.add_argument('--map', dest='mappings', type=_mapping, action='append', required=True, metavar='OLD=NEW')
    repair.add_argument('--thread-id', action='append', required=True, help='Explicit thread scope; required ancestors/descendants may be added to the plan')
    repair.add_argument('--work', type=Path, required=True, help='New private staging directory')
    _confirmed(repair)
    for name, description in [('apply', 'Apply a reviewed frozen plan while Codex is closed'), ('recover', 'Recover an interrupted plan while preserving unexpected later changes'), ('verify', 'Read-only verification of a plan and published hashes')]:
        command = commands.add_parser(name, help=description)
        command.add_argument('plan_path', type=Path, metavar='PLAN')
        if name != 'verify':
            _confirmed(command)
    native = commands.add_parser('verify-native', help='Run app-server checks only on a disposable synthetic home; may write the copy')
    native.add_argument('home', type=Path, metavar='HOME')
    native.add_argument('--cli', type=Path, help='Explicit native Codex CLI executable')
    native.add_argument('--thread', dest='thread_ids', action='append', help='Synthetic thread to verify; may be repeated')
    native.add_argument('--resume', action='store_true', help='Exercise resume on the disposable copy; never sends a model turn')
    _confirmed(native)
    commands.add_parser('wizard', help='Interactive Chinese/English menu with explicit write confirmations')
    return parser


def _dispatch(args):
    if args.command == 'verify-native':
        from .native_verify import verify_native
        return verify_native(args.home, cli=args.cli, thread_ids=args.thread_ids, resume=args.resume)
    if args.command == 'inspect':
        from .discovery import inspect_home
        return inspect_home(args.home, sqlite_home=args.sqlite_home)
    if args.command == 'export':
        from .exporter import export_bundle
        return export_bundle(args.home, args.output, project_ids=args.project_id, thread_ids=args.thread_id, sqlite_home=args.sqlite_home)
    # Planning/recovery code is loaded only for commands that use it.
    import importlib
    operations = importlib.import_module('.operations', __package__)
    if args.command == 'plan':
        path = operations.plan_import(args.bundle, args.home, args.destination, args.work, sqlite_home=args.sqlite_home)
        return {'plan': str(path)}
    if args.command == 'repair-links':
        path = operations.plan_link_repair(args.home, args.mappings, args.work, thread_ids=args.thread_id, sqlite_home=args.sqlite_home)
        return {'plan': str(path)}
    function = {'apply': 'apply_plan', 'recover': 'recover_plan', 'verify': 'verify_plan'}[args.command]
    return getattr(operations, function)(args.plan_path)


def _error(code, message):
    print(json.dumps({'ok': False, 'error': {'code': code, 'message': message}}, ensure_ascii=False), file=sys.stderr)


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        if args.command == 'wizard':
            from .wizard import run_wizard
            return run_wizard()
        if args.command in {'export', 'plan', 'repair-links', 'apply', 'recover', 'verify-native'} and not args.yes:
            raise MigrationError('This command writes files. Review its arguments, then pass --yes to confirm.', code='confirmation_required')
        result = _dispatch(args)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, default=str))
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except MigrationError as exc:
        _error(exc.code, str(exc))
        return 2 if exc.code in {'usage', 'confirmation_required'} else 1
    except (OSError, sqlite3.Error, ValueError) as exc:
        _error('io_error' if isinstance(exc, OSError) else 'invalid_data', str(exc))
        return 1
    except (KeyboardInterrupt, EOFError):
        _error('cancelled', 'Cancelled. If target writes had begun, keep Codex closed and run recover before reopening it.')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
