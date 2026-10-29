"""Public query commands verify the shipped payload and never refresh usage."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'scripts'))
import standalone_app
import windows_package
import prebuilt_runtime


class QueryEntryTests(unittest.TestCase):
    def test_query_dispatch_uses_verified_engine_without_refresh(self):
        import query_meter
        engine = str(PACKAGE / 'runtime/bin/codex-usage-offline.exe')
        supplied = ['--query-meter', 'begin', '--watch', '--thread-id', '01977e3d-d9f6-72b7-93cf-f3f2f83c382c']
        with patch.object(windows_package, 'verify_bundle') as verified, \
             patch.object(prebuilt_runtime, 'resolve_prebuilt', return_value={
                 'engine': engine, 'pythonPackages': str(PACKAGE / 'runtime/python-packages')}), \
             patch.object(query_meter, 'main', return_value=0) as measured, \
             patch('refresh_usage.main', side_effect=AssertionError('Query command must not refresh')):
            self.assertEqual(standalone_app.main(supplied), 0)
        verified.assert_called_once_with(PACKAGE)
        self.assertEqual(measured.call_args.args[0], supplied[1:] + ['--engine', engine])

    def test_query_command_rejects_payload_failure_before_controller(self):
        import query_meter
        with patch.object(windows_package, 'verify_bundle', side_effect=ValueError('damaged payload')), \
             patch.object(query_meter, 'main') as measured:
            self.assertEqual(standalone_app.main(['--query-meter', 'status']), 1)
        measured.assert_not_called()

    def test_public_entry_never_accepts_replacement_engine(self):
        import query_meter
        for arguments in (['--engine', 'unverified.exe'], ['--engine=unverified.exe']):
            with self.subTest(arguments=arguments), \
                 patch.object(windows_package, 'verify_bundle'), \
                 patch.object(prebuilt_runtime, 'resolve_prebuilt', return_value={
                     'engine': 'verified.exe', 'pythonPackages': str(PACKAGE)}), \
                 patch.object(query_meter, 'main') as measured:
                self.assertEqual(standalone_app.main(['--query-meter', 'begin', *arguments]), 1)
            measured.assert_not_called()

    def test_help_still_verifies_public_payload(self):
        import query_meter
        with patch.object(windows_package, 'verify_bundle') as verified, \
             patch.object(prebuilt_runtime, 'resolve_prebuilt', return_value={
                 'engine': 'verified.exe', 'pythonPackages': str(PACKAGE)}), \
             patch.object(query_meter, 'main', return_value=0) as measured:
            self.assertEqual(standalone_app.main(['--query-meter', '--help']), 0)
        verified.assert_called_once_with(PACKAGE)
        measured.assert_called_once_with(['--help'])


if __name__ == '__main__':
    unittest.main()
