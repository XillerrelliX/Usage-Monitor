"""Synthetic batch orchestration: exact selection, bounded work, no fallback."""
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'scripts'))
import codex_report as batch
import dashboard_data as ledger
import local_setup
import project_metadata
import query_meter
import refresh_usage
import report_from_app as core
import standalone_app
from test_scopes import fixture, ROOT, OTHER, A, B, NOW


class CodexReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='usage-report-synthetic-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.out = self.root / 'dashboard'
        self.config = self.root / 'profile' / 'settings.json'
        self.engine = self.root / 'engine.exe'
        self.engine.write_bytes(b'SYNTHETIC_ENGINE')
        self.database, self.names = fixture(self.root)
        with closing(sqlite3.connect(self.database)) as database, database:
            database.execute("UPDATE scan_runs SET timezone='UTC'")
        self.bundle, self.proof, _ = ledger.build_bundle(self.database, [self.names], now=NOW)
        self.bundle['projectMetadata'] = project_metadata.resolve_project_metadata(self.bundle, [self.names])
        self.bundle['offlineScan'] = {'scanId': 7, 'timezone': 'UTC'}
        self.settings = {'schemaVersion': 2, 'database': str(self.database),
                         'namesDatabases': [str(self.names)], 'projectState': None,
                         'outputDirectory': str(self.out), 'cacheFile': None,
                         'offline': {'codexHome': str(self.home), 'timezone': 'UTC',
                                     'engine': str(self.engine), 'runtimeDirectory': str(self.root / 'runtime')}}
        local_setup.save_settings(self.config, self.settings)
        self.environment = patch.dict(os.environ, {'CODEX_THREAD_ID': ROOT}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.base = 'http://127.0.0.1:43210/' + 'a' * 40 + '/'
        self.service = {'baseUrl': self.base, 'dashboardUrl': self.base + 'index.html',
                        'refreshUrl': self.base + 'refresh.html'}
        self.query_id = '33333333-3333-4333-8333-333333333333'
        self.query = {'schemaVersion': 1, 'queryId': self.query_id, 'status': 'provisional',
                      'final': False, 'terminal': False, 'coverage': 'turn-start',
                      'cutoff': '2026-10-26T10:00:00Z', 'counters': None,
                      'subtotal': {'totalTokens': 41}, 'turns': [{'PRIVATE': 'DO_NOT_EMIT'}],
                      'viewPath': str(self.out / 'query-usage' / self.query_id / 'index.html')}

    def args(self, *values):
        return batch.parse_arguments(['--codex-report', '--config', str(self.config),
                                      '--scope', 'all', '--period', 'last3', '--today', '2026-10-26',
                                      '--timezone', 'UTC', *values])

    def publish(self, selection):
        # Real numeric proof and generated data/HTML, with only source scanning mocked.
        refresh_usage.write_outputs(self.out, self.bundle, self.proof, selection=selection)
        return {'initialSelection': selection, 'sourceLastScanAt': self.bundle['source']['lastScanAt'],
                'offlineScan': self.bundle['offlineScan'], 'localServer': self.service,
                'dashboardUrl': refresh_usage.selected_dashboard_url(self.service['dashboardUrl'], selection),
                'warnings': []}, 0

    def report(self, args=None, meter=None, side_effect=None):
        meter = meter or Mock()
        meter.begin.return_value = self.query
        meter.sample.return_value = {**self.query, 'subtotal': {'totalTokens': 57}}
        with patch.object(query_meter, 'Meter', return_value=meter), \
             patch.object(batch, '_refresh_once', side_effect=side_effect or (lambda request, *_: self.publish(request['selection']))) as refresh:
            result, status = batch.run_report(args or self.args(), PACKAGE, self.engine)
        return result, status, meter, refresh

    def test_fixed_five_counters_one_refresh_and_same_query_sample(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result, status, meter, refresh = self.report()
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(tuple(result['totals'][field] for field in core.FIELDS), (259, 160, 40, 14, 299))
        self.assertEqual(result['selection'], {'startDate': '2026-10-24', 'endDate': '2026-10-26', 'projects': [], 'chats': []})
        self.assertEqual(result['timeZone'], 'UTC')
        self.assertEqual(result['cutoff'], self.bundle['source']['lastScanAt'])
        self.assertIn(status, (0, 2))  # Synthetic old snapshots retain their diagnostics.
        refresh.assert_called_once()
        meter.begin.assert_called_once_with(self.home, ROOT)
        meter.spawn_watch.assert_called_once_with(self.query_id, 3600, 2)
        meter.sample.assert_called_once_with(self.query_id)
        self.assertEqual(result['queryUsage']['status'], 'provisional')
        self.assertEqual(result['queryUsage']['subtotal']['totalTokens'], 57)
        self.assertEqual(result['queryUsage']['viewUrl'], self.base + 'query-usage/' + self.query_id + '/index.html')
        self.assertNotIn('turns', result['queryUsage'])
        self.assertNotIn('DO_NOT_EMIT', json.dumps(result))
        self.assertEqual(result['dashboardUrl'], self.service['dashboardUrl'] + '#from=2026-10-24&to=2026-10-26')

    def test_this_chat_uses_current_id_and_allows_absent_prior_report(self):
        self.assertFalse((self.out / 'data.json').exists())
        result, _, meter, _ = self.report(self.args('--scope', 'chat'))
        self.assertEqual(result['selection']['chats'], [ROOT])
        self.assertEqual(tuple(result['totals'][field] for field in core.FIELDS), (169, 85, 35, 12, 204))
        meter.begin.assert_called_once_with(self.home, ROOT)

    def test_explicit_intersection_uses_exact_union_without_double_counting(self):
        self.publish({'startDate': '2026-10-24', 'endDate': '2026-10-26', 'projects': [], 'chats': []})
        # The verified synthetic metadata fallback retains all folders in one
        # exact unassigned named-project bucket, preserving independent totals.
        identity = project_metadata.UNASSIGNED_ID
        result, _, meter, _ = self.report(self.args('--scope', 'selection', '--project-id', identity,
                                                   '--project-id', identity, '--chat-id', ROOT, '--chat-id', ROOT))
        self.assertEqual(result['selection']['projects'], [identity])
        self.assertEqual(result['selection']['chats'], [ROOT])
        self.assertEqual(tuple(result['totals'][field] for field in core.FIELDS), (169, 85, 35, 12, 204))
        meter.begin.assert_called_once_with(self.home, ROOT)

    def test_presets_calendar_dates_across_dst_and_year_boundary(self):
        self.settings['offline']['timezone'] = 'Europe/Berlin'
        local_setup.save_settings(self.config, self.settings)
        for today, period, start in [('2026-10-26', 'last3', '2026-10-24'),
                                     ('2026-01-02', 'last7', '2025-12-27'),
                                     ('2026-10-25', 'today', '2026-10-25')]:
            with self.subTest(today=today, period=period):
                request = batch.prepare_request(self.args('--today', today, '--period', period, '--timezone', 'Europe/Berlin'))
                self.assertEqual(request['selection']['startDate'], start)
                self.assertEqual(request['selection']['endDate'], today)
                self.assertEqual(request['timeZone'], 'Europe/Berlin')

    def test_first_use_requires_zone_and_uses_normal_offline_pipeline(self):
        self.config.unlink()
        args = self.args()
        args.timezone = None
        with self.assertRaisesRegex(batch.ReportError, 'codex_report_timezone_required'):
            batch.prepare_request(args)
        with patch.dict(os.environ, {'CODEX_HOME': str(self.home)}):
            request = batch.prepare_request(self.args('--timezone', 'UTC'))
        self.assertEqual(request['home'], self.home)
        self.assertEqual(request['out'], self.config.parent / 'dashboard')

    def test_bad_scope_dates_zone_and_flags_fail_before_meter_or_refresh(self):
        cases = [(['--scope', 'project'], 'codex_report_exact_project_required'),
                 (['--scope', 'selection'], 'codex_report_explicit_selection_required'),
                 (['--project-id', 'unconfirmed'], 'codex_report_scope_conflict'),
                 (['--today', '2026-02-30'], 'codex_report_invalid_dates'),
                 (['--today', '2026-1-2'], 'codex_report_invalid_dates'),
                 (['--timezone', 'Europe/Berlin'], 'codex_report_timezone_mismatch'),
                 (['--from-date', '2026-10-24'], 'codex_report_period_requires_today')]
        for values, code in cases:
            with self.subTest(values=values), patch.object(query_meter, 'Meter') as meter, \
                 patch.object(batch, '_refresh_once') as refresh:
                with self.assertRaisesRegex(batch.ReportError, code):
                    batch.run_report(self.args(*values), PACKAGE, self.engine)
                meter.assert_not_called()
                refresh.assert_not_called()
        for flag in ('--query-meter', '--export', '--serve-only', '--refresh', '--read-cache', '--codex-home', '--selection-file'):
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(batch.ReportError, 'codex_report_invalid_arguments'):
                    batch.parse_arguments(['--codex-report', '--scope', 'all', flag])
        args = self.args()
        args.timezone = None
        with self.assertRaisesRegex(batch.ReportError, 'codex_report_timezone_required'):
            batch.prepare_request(args)

    def test_unknown_selection_cannot_refresh_or_broaden(self):
        self.publish({'startDate': '2026-10-24', 'endDate': '2026-10-26', 'projects': [], 'chats': []})
        with patch.object(query_meter, 'Meter') as meter, patch.object(batch, '_refresh_once') as refresh:
            with self.assertRaisesRegex(batch.ReportError, 'codex_report_verified_selection_required'):
                batch.run_report(self.args('--scope', 'project', '--project-id', 'missing'), PACKAGE, self.engine)
            meter.assert_not_called()
            refresh.assert_not_called()

    def test_missing_execution_chat_only_blocks_this_chat(self):
        for value in ('', 'not-a-chat-id'):
            with self.subTest(value=value), patch.dict(os.environ, {'CODEX_THREAD_ID': value}):
                result, _, meter, refresh = self.report()
                self.assertEqual(result['queryUsage']['status'], 'unavailable')
                meter.begin.assert_not_called()
                refresh.assert_called_once()
                with patch.object(batch, '_refresh_once') as refresh:
                    with self.assertRaisesRegex(batch.ReportError, 'codex_report_current_chat_required'):
                        batch.run_report(self.args('--scope', 'chat'), PACKAGE, self.engine)
                    refresh.assert_not_called()

    def test_active_meter_and_begin_failure_leave_report_valid(self):
        meter = Mock()
        meter.begin.side_effect = query_meter.MeterError('query_measurement_already_active')
        result, _, meter, refresh = self.report(meter=meter)
        self.assertEqual(result['queryUsage']['errorCode'], 'query_measurement_already_active')
        self.assertEqual(result['totals']['totalTokens'], 299)
        meter.sample.assert_not_called()
        meter.spawn_watch.assert_not_called()
        refresh.assert_called_once()

    def test_sample_failure_is_separate_and_preserves_specific_query_link(self):
        meter = Mock()
        meter.sample.side_effect = query_meter.MeterError('query_meter_busy')
        result, _, _, _ = self.report(meter=meter)
        self.assertEqual(result['queryUsage']['errorCode'], 'query_meter_busy')
        self.assertEqual(result['queryUsage']['queryId'], self.query_id)
        self.assertIn('/' + self.query_id + '/', result['queryUsage']['viewUrl'])
        self.assertEqual(result['totals']['totalTokens'], 299)

    def test_report_failure_stops_without_sample_rescan_or_stale_totals(self):
        def failure(*_):
            raise batch.ReportError('codex_report_refresh_failed')
        meter = Mock()
        with self.assertRaisesRegex(batch.ReportError, 'codex_report_refresh_failed'):
            self.report(meter=meter, side_effect=failure)
        meter.sample.assert_not_called()

    def test_generated_selection_cutoff_and_scan_identity_must_match(self):
        for key in ('initialSelection', 'sourceLastScanAt', 'offlineScan'):
            def changed(request, *_, key=key):
                result, status = self.publish(request['selection'])
                result[key] = None
                return result, status
            with self.subTest(key=key), self.assertRaisesRegex(batch.ReportError, 'codex_report_publication_changed'):
                self.report(side_effect=changed)

    def test_proof_changed_during_projection_is_rejected(self):
        original = project_metadata.project_report
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            with (self.out / 'evidence.json').open('ab') as stream:
                stream.write(b'\n')
            return result
        with patch.object(project_metadata, 'project_report', side_effect=changed):
            with self.assertRaisesRegex(batch.ReportError, 'codex_report_publication_changed'):
                self.report()

    def test_refresh_api_once_captures_nested_output_and_retains_warning_exit(self):
        request = batch.prepare_request(self.args())
        expected = {'initialSelection': request['selection']}
        def refresh(arguments):
            self.assertEqual(arguments.count('--offline'), 1)
            self.assertEqual(arguments.count('--engine'), 1)
            self.assertIn('--no-open', arguments)
            print(json.dumps(expected))
            print('SAFE_INTERNAL_DIAGNOSTIC', file=sys.stderr)
            return 2
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(refresh_usage, 'main', side_effect=refresh) as called, \
             redirect_stdout(stdout), redirect_stderr(stderr):
            result, status = batch._refresh_once(request, PACKAGE, self.engine)
        called.assert_called_once()
        self.assertEqual((result, status), (expected, 2))
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('', ''))

    def test_public_python_entry_verifies_once_and_emits_one_json(self):
        output, errors = io.StringIO(), io.StringIO()
        expected = {'schemaVersion': 1, 'totals': {'totalTokens': 299}, 'modelCalls': 0, 'networkUsed': False}
        with patch('windows_package.verify_bundle') as verified, \
             patch('prebuilt_runtime.resolve_prebuilt', return_value={'pythonPackages': str(PACKAGE / 'runtime/python-packages'), 'engine': str(self.engine)}) as resolved, \
             patch.object(batch, 'run_report', return_value=(expected, 2)) as report, \
             redirect_stdout(output), redirect_stderr(errors):
            status = standalone_app.main(['--codex-report', '--scope', 'all', '--period', 'today', '--today', '2026-10-26'])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue()), expected)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(errors.getvalue(), '')
        verified.assert_called_once()
        resolved.assert_called_once()
        report.assert_called_once()


if __name__ == '__main__':
    unittest.main()
