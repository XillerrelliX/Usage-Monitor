"""Actual-engine rollback after publication and later report/config writes."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_offline_refresh as fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import dashboard_data
import local_setup
import refresh_usage


@unittest.skipUnless(os.environ.get('CODEX_USAGE_TEST_ENGINE'), 'Requires the actual offline scanner.')
class RefreshTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home, _ = fixture.raw_fixture(self.root)
        self.config, self.out = self.root/'settings.json', self.root/'dashboard'
        self.engine = Path(os.environ['CODEX_USAGE_TEST_ENGINE'])
        self.run_main(['--offline', '--codex-home', str(self.home), '--engine', str(self.engine),
                       '--timezone', 'UTC', '--out', str(self.out)])
        self.source = Path(json.loads(self.config.read_text())['database'])
        self.before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in
                       [self.config, self.source, self.root/'.usage-python', *self.out.rglob('*')] if path.is_file()}

    def run_main(self, arguments=(), expected_success=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = refresh_usage.main(['--config', str(self.config), '--no-open', *arguments])
        if expected_success:
            self.assertIn(code, (0, 2), stderr.getvalue())
        else:
            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue(), '')
            self.assertFalse(json.loads(stderr.getvalue())['networkUsed'])

    def assert_preserved(self):
        for path, expected in self.before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected, str(path))

    def test_invalid_output_parent_preserves_completed_source(self):
        blocked = self.root/'existing file'
        blocked.write_text('Unrelated file must remain unchanged', encoding='utf-8')
        self.run_main(['--out', str(blocked)], expected_success=False)
        self.assert_preserved()
        self.assertEqual(blocked.read_text(), 'Unrelated file must remain unchanged')

    def test_final_accounting_failure_after_source_publish_restores_source(self):
        original = dashboard_data.build_bundle
        calls = []

        def reject_final(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise ValueError('Final accounting rejected after source publication')
            return original(*args, **kwargs)

        with patch.object(dashboard_data, 'build_bundle', side_effect=reject_final):
            self.run_main(expected_success=False)
        self.assertEqual(len(calls), 2)
        self.assert_preserved()

    def test_report_pointer_and_interpreter_failures_restore_every_file(self):
        for module, name in ((refresh_usage, 'write_outputs'), (local_setup, 'save_dashboard_pointer'),
                             (local_setup, 'save_interpreter_hint')):
            with self.subTest(stage=name):
                original = getattr(module, name)

                def fail_after_write(*args, **kwargs):
                    original(*args, **kwargs)
                    raise OSError('Synthetic failure after '+name)

                with patch.object(module, name, side_effect=fail_after_write):
                    self.run_main(expected_success=False)
                self.assert_preserved()

    def test_settings_override_written_then_rejected_is_fully_rolled_back(self):
        destination = self.root/'changed output'
        original = local_setup.save_settings

        def fail_after_settings(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('Synthetic failure after changed settings')

        with patch.object(local_setup, 'save_settings', side_effect=fail_after_settings):
            self.run_main(['--out', str(destination)], expected_success=False)
        self.assert_preserved()
        self.assertEqual([path for path in destination.rglob('*') if path.is_file()], [])


if __name__ == '__main__':
    unittest.main()
