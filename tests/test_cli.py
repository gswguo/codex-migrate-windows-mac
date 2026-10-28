"""CLI boundaries use synthetic API doubles; native transforms have separate tests."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from codex_migrate.cli import main
from codex_migrate.errors import MigrationError
from codex_migrate.wizard import run_wizard


class CliTests(unittest.TestCase):
    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def operations(self, **functions):
        module = types.ModuleType('codex_migrate.operations')
        for name, value in functions.items():
            setattr(module, name, value)
        return patch.dict(sys.modules, {'codex_migrate.operations': module})

    def test_help_lists_available_commands_without_loading_operations(self):
        code, out, err = self.invoke(['--help'])
        self.assertEqual(code, 0)
        for command in ('inspect', 'export', 'plan', 'apply', 'repair-links', 'recover', 'verify', 'verify-native', 'wizard'):
            self.assertIn(command, out)
        self.assertEqual(err, '')

    def test_unknown_command_and_idle_bypass_are_usage_errors(self):
        for args in (['bogus'], ['inspect', '--check-idle=false']):
            code, out, err = self.invoke(args)
            self.assertEqual(code, 2)
            self.assertEqual(out, '')
            self.assertEqual(json.loads(err)['error']['code'], 'usage')

    def test_all_file_writing_commands_require_confirmation_before_loading_api(self):
        commands = [['export', '--output', 'synthetic.zip', '--thread-id', 'synthetic-thread'], ['plan', '--bundle', 'synthetic.zip', '--destination', 'synthetic-files', '--work', 'synthetic-work'], ['repair-links', '--map', 'E:/old=/synthetic/new', '--thread-id', 'synthetic-thread', '--work', 'synthetic-work'], ['apply', 'synthetic-plan.json'], ['recover', 'synthetic-plan.json'], ['verify-native', '/synthetic/disposable-home']]
        for args in commands:
            with self.subTest(args=args):
                code, out, err = self.invoke(args)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(err)['error']['code'], 'confirmation_required')

    def test_plan_json_and_exact_path_arguments(self):
        calls = []
        def plan(bundle, home, destination, work, **kwargs):
            calls.append((bundle, home, destination, work, kwargs))
            return Path('/synthetic/stage/plan.json')
        with self.operations(plan_import=plan):
            code, out, err = self.invoke(['plan', '--bundle', 'bundle.zip', '--home', '/synthetic/home', '--sqlite-home', '/synthetic/db', '--destination', '/synthetic/project', '--work', '/synthetic/stage', '--yes'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {'ok': True, 'result': {'plan': '/synthetic/stage/plan.json'}})
        self.assertEqual(calls, [(Path('bundle.zip'), Path('/synthetic/home'), Path('/synthetic/project'), Path('/synthetic/stage'), {'sqlite_home': Path('/synthetic/db')})])
        self.assertEqual(err, '')

    def test_repair_links_creates_plan_with_explicit_mappings_and_thread_selection(self):
        calls = []
        def plan(home, mappings, work, **kwargs):
            calls.append((home, mappings, work, kwargs))
            return Path('repair/plan.json')
        with self.operations(plan_link_repair=plan):
            code, out, err = self.invoke(['repair-links', '--home', '/synthetic/home', '--map', 'E:/old=/synthetic/new', '--thread-id', 'synthetic-thread', '--work', 'repair', '--yes'])
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][1], [('E:/old', '/synthetic/new')])
        self.assertEqual(calls[0][3], {'thread_ids': ['synthetic-thread'], 'sqlite_home': None})
        self.assertEqual(json.loads(out)['result']['plan'], 'repair/plan.json')

    def test_apply_recover_verify_forward_only_plan_path(self):
        for command, function in [('apply', 'apply_plan'), ('recover', 'recover_plan'), ('verify', 'verify_plan')]:
            with self.subTest(command=command):
                calls = []
                def operation(path):
                    calls.append(path)
                    return {'status': 'synthetic-complete'}
                with self.operations(**{function: operation}):
                    code, out, err = self.invoke([command, 'synthetic-plan.json'] + (['--yes'] if command != 'verify' else []))
                self.assertEqual(code, 0)
                self.assertEqual(calls, [Path('synthetic-plan.json')])
                self.assertEqual(json.loads(out)['result']['status'], 'synthetic-complete')

    def test_native_verification_passes_only_explicit_disposable_home_arguments(self):
        calls = []
        native = types.ModuleType('codex_migrate.native_verify')
        def verify(home, **kwargs):
            calls.append((home, kwargs))
            return {'status': 'synthetic-verified'}
        native.verify_native = verify
        with patch.dict(sys.modules, {'codex_migrate.native_verify': native}):
            code, out, err = self.invoke(['verify-native', '/synthetic/disposable-home', '--cli', '/synthetic/codex-cli', '--thread', 'synthetic-thread', '--resume', '--yes'])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [(Path('/synthetic/disposable-home'), {'cli': Path('/synthetic/codex-cli'), 'thread_ids': ['synthetic-thread'], 'resume': True})])
        self.assertEqual(json.loads(out)['result']['status'], 'synthetic-verified')

    def test_inspect_and_export_use_connected_core_apis(self):
        with patch('codex_migrate.discovery.inspect_home', return_value={'projects': []}):
            code, out, err = self.invoke(['inspect', '--home', '/synthetic/home'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['result'], {'projects': []})
        with patch('codex_migrate.exporter.export_bundle', return_value={'bundle_id': 'synthetic-bundle'}) as export:
            code, out, err = self.invoke(['export', '--home', '/synthetic/home', '--output', 'bundle.zip', '--project-id', 'synthetic-project', '--yes'])
        self.assertEqual(code, 0)
        self.assertEqual(export.call_args.kwargs['project_ids'], ['synthetic-project'])
        self.assertNotIn('check_idle', export.call_args.kwargs)

    def test_errors_are_machine_readable_without_tracebacks(self):
        with self.operations(apply_plan=lambda p: (_ for _ in ()).throw(MigrationError('target changed', code='target_changed'))):
            code, out, err = self.invoke(['apply', 'synthetic-plan.json', '--yes'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err), {'ok': False, 'error': {'code': 'target_changed', 'message': 'target changed'}})
        self.assertNotIn('Traceback', err)

    def test_repair_requires_explicit_thread_ids_before_api_dispatch(self):
        code, out, err = self.invoke(['repair-links', '--map', 'E:/old=/synthetic/new', '--work', 'repair', '--yes'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)['error']['code'], 'usage')

    def test_invalid_mapping_is_usage_error(self):
        code, out, err = self.invoke(['repair-links', '--map', 'missing-separator', '--work', 'repair', '--yes'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)['error']['code'], 'usage')

    def test_wizard_declines_apply_and_can_exit_without_mutation(self):
        answers = iter(['5', 'synthetic-plan.json', 'no', '0'])
        messages = []
        calls = []
        code = run_wizard(input_fn=lambda prompt: next(answers), output_fn=messages.append, execute=lambda args: calls.append(args))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertTrue(any('cancel' in message.lower() for message in messages))

    def test_wizard_confirms_and_passes_yes_to_cli(self):
        answers = iter(['5', 'synthetic-plan.json', 'YES', '0'])
        calls = []
        code = run_wizard(input_fn=lambda prompt: next(answers), output_fn=lambda text: None, execute=lambda args: calls.append(args) or 0)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [['apply', 'synthetic-plan.json', '--yes']])


if __name__ == '__main__':
    unittest.main()
