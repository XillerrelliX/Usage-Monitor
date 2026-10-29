"""Portable launch/export behavior and real fixture refreshes without Codex."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import refresh_usage as refresh
import dashboard_data
import local_setup
import standalone_app
import prebuilt_runtime
import windows_package
import project_metadata
from test_scopes import fixture, NOW, ROOT, FORK, OTHER, A, B, C


class RefreshUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fake_package(self):
        root = self.root/'source package'
        files = {'SKILL.md': 'local skill', 'refresh.cmd': '@echo off', 'refresh.sh': '#!/bin/sh\n',
                 'setup.cmd': '@echo off', 'setup.sh': '#!/bin/sh\n',
                 'scripts/refresh_usage.py': 'code', 'scripts/report_from_app.py': 'code',
                 'scripts/dashboard_data.py': 'code',
                 'scripts/project_metadata.py': 'code',
                 'scripts/local_setup.py': 'code',
                 'assets/dashboard.html': '<script>/*__BROWSER_RUNTIME__*/</script>__DATA_JSON__',
                 'assets/browser_data.js': 'window.runtime=true;', 'tests/test_public.py': 'code',
                 'tests/fixtures/browser-bundle.fixture.json': '{"synthetic":true}'}
        for name, content in files.items():
            p = root/name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding='utf-8')
        return root

    def test_platform_discovery_uses_local_profile_not_another_users_path(self):
        home = self.root/'different user'
        cases = [('win32', {'APPDATA': str(home/'Roaming')}, home/'Roaming'),
                 ('darwin', {}, home/'Library/Application Support'),
                 ('linux', {'XDG_DATA_HOME': str(home/'custom-data')}, home/'custom-data'),
                 ('linux', {}, home/'.local/share')]
        for platform, environ, directory in cases:
            with self.subTest(platform=platform, environ=environ):
                paths = refresh.source_candidates(platform, environ, home)
                self.assertEqual(paths, [directory/'com.ccusage.codex.desktop/codex-usage-desktop.db'])

    def test_missing_source_is_actionable_and_does_not_search_logs(self):
        with self.assertRaisesRegex(ValueError, '--database PATH'):
            refresh.find_database(None, platform='linux', environ={}, home=self.root)
        source = self.root/'selected.sqlite'
        source.write_bytes(b'fixture')
        self.assertEqual(refresh.find_database(source), source.resolve())

    def test_export_omits_every_personal_output_and_cache(self):
        root = self.fake_package()
        for name in ['usage-ledger.json', 'usage-history.json', 'index.html', 'data.json',
                     'evidence.json', 'summary.txt', 'private.sqlite',
                     'output/data.json', 'scripts/secrets.json', 'tests/private.sqlite',
                     'settings.json', 'local-settings.json', '.config/settings.json',
                     'scripts/settings.json', 'tests/settings.json', 'assets/settings.json',
                     '.usage-setup.json', 'scripts/.usage-setup.json', 'assets/.usage-setup.json']:
            p = root/name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('PRIVATE NEVER EXPORT', encoding='utf-8')
        archive = self.root/'portable.zip'
        result = refresh.export_package(archive, root)
        with zipfile.ZipFile(archive) as zipped:
            names = zipped.namelist()
            self.assertTrue(result['privateUsageDataIncluded'] is False)
            self.assertTrue(all('PRIVATE NEVER EXPORT' not in zipped.read(n).decode() for n in names))
            self.assertIn('codex-local-usage/scripts/refresh_usage.py', names)
            self.assertIn('codex-local-usage/scripts/local_setup.py', names)
            self.assertIn('codex-local-usage/setup.cmd', names)
            self.assertIn('codex-local-usage/setup.sh', names)
            self.assertNotIn('codex-local-usage/.usage-setup.json', names)
            self.assertIn('codex-local-usage/tests/fixtures/browser-bundle.fixture.json', names)
            self.assertTrue(zipped.getinfo('codex-local-usage/refresh.sh').external_attr >> 16 & 0o111)
            self.assertTrue(zipped.getinfo('codex-local-usage/setup.sh').external_attr >> 16 & 0o111)
            manifest = json.loads(zipped.read('codex-local-usage/package-manifest.json'))
            self.assertEqual(len(manifest['files']), len(names)-1)

    def test_export_does_not_need_a_database(self):
        root = self.fake_package()
        with patch.object(refresh, 'find_database', side_effect=AssertionError('No database on export')), \
             patch.object(refresh, 'source_candidates', side_effect=AssertionError('No source discovery on export')), \
             patch.object(local_setup, 'default_settings_file', side_effect=AssertionError('No settings path on export')), \
             patch.object(local_setup, 'resolve_settings_file', side_effect=AssertionError('No dashboard pointer on export')), \
             patch.object(local_setup, 'save_dashboard_pointer', side_effect=AssertionError('No pointer write on export')), \
             patch.object(local_setup, 'load_settings', side_effect=AssertionError('No settings read on export')), \
             patch.object(local_setup, 'save_settings', side_effect=AssertionError('No settings write on export')):
            with patch.object(refresh, 'export_package', wraps=lambda target: {'export': str(target)}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(refresh.main(['--config', str(root/'unreadable-config.json'),
                                                   '--setup', '--export', str(root/'export.zip')]), 0)

    def test_export_keeps_embedded_font_licenses_without_private_asset_notes(self):
        root = self.fake_package()
        for name, text in {'OFL-PublicSans.txt': 'Public font license',
                           'OFL-Sora.txt': 'Public font license',
                           'font-sources.json': '{"source":"https://example.com/public-font"}',
                           'private-notes.txt': 'PERSONAL ASSET NOTES',
                           'private-state.json': '{"personal":true}'}.items():
            (root/'assets'/name).write_text(text, encoding='utf-8')
        archive = self.root/'licensed-portable.zip'
        refresh.export_package(archive, root)
        with zipfile.ZipFile(archive) as zipped:
            names = zipped.namelist()
            self.assertIn('codex-local-usage/assets/OFL-PublicSans.txt', names)
            self.assertIn('codex-local-usage/assets/OFL-Sora.txt', names)
            self.assertIn('codex-local-usage/assets/font-sources.json', names)
            self.assertNotIn('codex-local-usage/assets/private-notes.txt', names)
            self.assertNotIn('codex-local-usage/assets/private-state.json', names)

    def test_render_escapes_untrusted_names_and_substitutes_data_last(self):
        root = self.fake_package()
        value = '</script><script>alert(1)</script>&__DATA_JSON__/*__BROWSER_RUNTIME__*/'
        html = refresh.render_dashboard({'label': value}, root)
        self.assertNotIn('</script><script>alert(1)', html)
        self.assertIn('window.runtime=true;', html)
        payload = json.loads(html.split('</script>', 1)[1])
        self.assertEqual(payload, {'browserBundle': {'label': value}})

    def test_bad_template_cannot_silently_embed_wrong_data(self):
        root = self.fake_package()
        (root/'assets/dashboard.html').write_text('__DATA_JSON____DATA_JSON__', encoding='utf-8')
        with self.assertRaises(ValueError):
            refresh.render_dashboard({}, root)

    def test_refresh_configuration_is_local_exact_and_keeps_untrusted_data_inert(self):
        root = self.fake_package()
        base = 'http://127.0.0.1:12345/' + 'a' * 32 + '/'
        config = {'baseUrl': base, 'refreshUrl': base+'refresh.html',
                  'dashboardUrl': base+'index.html', 'autoRefresh': True}
        value = '</script><script>untrusted()</script>'
        html = refresh.render_dashboard({'name': value}, root, refresh=config)
        payload = json.loads(html.split('</script>', 1)[1])
        self.assertEqual(payload['refresh'], config)
        self.assertEqual(payload['browserBundle'], {'name': value})
        self.assertNotIn('</script><script>untrusted()', html)
        invalid = [
            {'baseUrl': base.replace('127.0.0.1', 'localhost'), 'refreshUrl': base+'refresh.html'},
            {'baseUrl': base.replace('127.0.0.1', 'example.com'), 'refreshUrl': base+'refresh.html'},
            {'baseUrl': base, 'refreshUrl': 'https://example.com/refresh.html'},
            {'baseUrl': base, 'refreshUrl': base+'refresh.html', 'autoRefresh': 'true'},
            {'baseUrl': base, 'refreshUrl': base+'refresh.html', 'dashboardUrl': base+'../index.html'},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                refresh.render_dashboard({}, root, refresh=value)

    def selection_fixture(self):
        database, names = fixture(self.root)
        with contextlib.closing(sqlite3.connect(names)) as db, db:
            db.executescript('CREATE TABLE projects(id TEXT, name TEXT); CREATE TABLE project_roots(project_id TEXT, path TEXT);')
            db.executemany('INSERT INTO projects VALUES(?,?)', [('project-a', 'A'), ('project-b', 'B'), ('project-c', 'C')])
            db.executemany('INSERT INTO project_roots VALUES(?,?)', [('project-a', A), ('project-b', B), ('project-c', C)])
        return database, names

    def run_selection(self, database, names, *selection):
        command = ['--config', str(self.root/'machine/settings.json'), '--database', str(database),
                   '--names-database', str(names), '--out', str(self.root/'dashboard ü # &'), '--no-open', *selection]
        stream, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(errors):
            status = refresh.main(command)
        return status, json.loads(stream.getvalue() or errors.getvalue())

    def test_exact_single_and_multiple_selection_are_embedded_and_encoded(self):
        database, names = self.selection_fixture()
        cases = [(['--project-id', 'project-a', '--chat-id', ROOT], ['project-a'], [ROOT]),
                 (['--project-id', 'project-b', '--project-id', 'project-a', '--project-id', 'project-a',
                   '--chat-id', OTHER, '--chat-id', ROOT], ['project-a', 'project-b'], [ROOT, OTHER])]
        for options, projects, chats in cases:
            with self.subTest(options=options):
                status, result = self.run_selection(database, names, '--from-date', '2026-10-24',
                                                     '--to-date', '2026-10-26', *options)
                self.assertIn(status, (0, 2), result)
                expected = {'startDate': '2026-10-24', 'endDate': '2026-10-26', 'projects': projects, 'chats': chats}
                self.assertEqual(result['initialSelection'], expected)
                fragment = parse_qs(urlsplit(result['dashboardUrl']).fragment)
                self.assertEqual(fragment, {'from': ['2026-10-24'], 'to': ['2026-10-26'], 'project': projects, 'chat': chats})
                self.assertIn('%23', urlsplit(result['dashboardUrl']).path)
                saved = Path(result['dashboard']).parent
                self.assertEqual(json.loads((saved/'data.json').read_text(encoding='utf-8'))['initialSelection'], expected)
                html = (saved/'index.html').read_text(encoding='utf-8')
                self.assertIn('"initialSelection":'+json.dumps(expected, separators=(',', ':')), html)
                self.assertNotIn('__INITIAL_SELECTION_JSON__', html)
                self.assertEqual(json.loads((saved/'.usage-selection.json').read_text())['selection'], expected)

    def test_invalid_selection_preserves_prior_report_cache_and_setup_without_opening(self):
        database, names = self.selection_fixture()
        status, result = self.run_selection(database, names)
        self.assertIn(status, (0, 2))
        saved = Path(result['dashboard']).parent
        paths = [self.root/'machine/settings.json', *(saved/name for name in (
            'index.html', 'data.json', 'evidence.json', 'summary.txt', 'usage-ledger.json', '.usage-selection.json'))]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        invalid = [('--from-date', '2026-02-30', '--to-date', '2026-03-01'),
                   ('--from-date', '2026-10-26'),
                   ('--from-date', '2026-10-26', '--to-date', '2026-10-24'),
                   ('--from-date', '2025-01-01', '--to-date', '2026-10-26'),
                   ('--project-id', 'missing-project'), ('--chat-id', 'missing-chat'),
                   ('--project-id', 'project-a', '--project-id', 'missing-project')]
        with patch.object(refresh.webbrowser, 'open', side_effect=AssertionError('Invalid selection must never open')):
            for options in invalid:
                with self.subTest(options=options):
                    status, failure = self.run_selection(database, names, *options)
                    self.assertEqual(status, 1, failure)
                    self.assertIn('error', failure)
                    self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths})

    def test_selection_data_remains_inert_even_with_script_and_placeholder_markers(self):
        package = self.fake_package()
        template = '<script id="defaults" type="application/json">__INITIAL_SELECTION_JSON__</script>__DATA_JSON__<script>/*__BROWSER_RUNTIME__*/</script>'
        (package/'assets/dashboard.html').write_text(template, encoding='utf-8')
        identity = '</script>&__DATA_JSON____INITIAL_SELECTION_JSON__/*__BROWSER_RUNTIME__*/'
        selection = {'startDate': '2026-10-24', 'endDate': '2026-10-26', 'projects': [identity], 'chats': []}
        html = refresh.render_dashboard({'label': identity}, package, selection)
        default = html.split('>', 1)[1].split('</script>', 1)[0]
        self.assertEqual(json.loads(default), selection)
        payload = html.split('</script>', 1)[1].split('<script>', 1)[0]
        self.assertEqual(json.loads(payload), {'browserBundle': {'label': identity}, 'initialSelection': selection})
        self.assertNotIn('</script>&', html)
        decoded = parse_qs(urlsplit(refresh.dashboard_url(self.root/'dash # ü/index.html', selection)).fragment)
        self.assertEqual(decoded['project'], [identity])

    def test_saved_dashboard_refresh_retains_selection_and_explicit_options_override(self):
        database, names = self.selection_fixture()
        status, first = self.run_selection(database, names, '--from-date', '2026-10-24', '--to-date', '2026-10-26',
                                            '--project-id', 'project-a', '--chat-id', ROOT)
        self.assertIn(status, (0, 2), first)
        saved = Path(first['dashboard']).parent
        # The copied source entry reads only its own bounded saved selection.
        process = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(saved/'scripts/refresh_usage.py'), '--no-open'],
                                 cwd=self.root, capture_output=True, text=True, encoding='utf-8')
        self.assertIn(process.returncode, (0, 2), process.stderr)
        self.assertEqual(json.loads(process.stdout)['initialSelection'], first['initialSelection'])
        process = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(saved/'scripts/refresh_usage.py'), '--no-open',
                                  '--from-date', '2026-10-25', '--to-date', '2026-10-25', '--chat-id', FORK],
                                 cwd=self.root, capture_output=True, text=True, encoding='utf-8')
        self.assertIn(process.returncode, (0, 2), process.stderr)
        self.assertEqual(json.loads(process.stdout)['initialSelection'], {
            'startDate': '2026-10-25', 'endDate': '2026-10-25', 'projects': [], 'chats': [FORK]})
        # A public package invocation still starts at today/all even if the same output was selected before.
        status, public = self.run_selection(database, names)
        self.assertIn(status, (0, 2), public)
        self.assertIsNone(public['initialSelection'])

    def test_standalone_entry_forwards_all_selection_flags_without_changing_them(self):
        captured = []
        installed = {'engine': str(self.root/'engine.exe'), 'pythonPackages': str(self.root/'packages')}
        options = ['--from-date', '2026-10-24', '--to-date', '2026-10-26',
                   '--project-id', 'project-a', '--project-id', 'project-b', '--chat-id', ROOT, '--chat-id', OTHER]
        with patch.object(windows_package, 'verify_bundle'), patch.object(prebuilt_runtime, 'resolve_prebuilt', return_value=installed), \
             patch.object(local_setup, 'load_settings', return_value=None), \
             patch.object(refresh, 'main', side_effect=lambda args: captured.extend(args) or 0):
            self.assertEqual(standalone_app.main(['--config', str(self.root/'config.json'), '--no-open', *options]), 0)
        for flag in ('--from-date', '--to-date', '--project-id', '--chat-id'):
            values = [captured[index+1] for index, value in enumerate(captured[:-1]) if value == flag]
            expected = [options[index+1] for index, value in enumerate(options[:-1]) if value == flag]
            self.assertEqual(values, expected)
        self.assertIn('--serve-dashboard', captured)
        captured.clear()
        with patch.object(windows_package, 'verify_bundle'), patch.object(prebuilt_runtime, 'resolve_prebuilt', return_value=installed), \
             patch.object(local_setup, 'load_settings', return_value=None), \
             patch.object(refresh, 'main', side_effect=lambda args: captured.extend(args) or 0):
            self.assertEqual(standalone_app.main(['--config', str(self.root/'config.json'), '--no-open', '--no-serve']), 0)
        self.assertIn('--no-serve', captured)
        self.assertNotIn('--serve-dashboard', captured)

    def test_public_export_and_query_meter_never_start_refresh_or_read_settings(self):
        installed = {'engine': str(self.root/'engine.exe'), 'pythonPackages': str(self.root/'packages')}
        server = SimpleNamespace(ensure_server=lambda *args: self.fail('No refresh service on export or query measurement'))
        meter_calls = []
        meter = SimpleNamespace(main=lambda args: meter_calls.append(args) or 0)
        with patch.object(windows_package, 'verify_bundle'), \
             patch.object(prebuilt_runtime, 'resolve_prebuilt', return_value=installed), \
             patch.object(windows_package, 'reexport_bundle', return_value={'export': 'synthetic.zip'}), \
             patch.object(local_setup, 'load_settings', side_effect=AssertionError('No report settings read')), \
             patch.object(refresh, 'main', side_effect=AssertionError('No report pipeline')), \
             patch.dict(sys.modules, {'refresh_server': server, 'query_meter': meter}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(standalone_app.main(['--export', str(self.root/'public.zip')]), 0)
            self.assertEqual(standalone_app.main(['--query-meter', 'sample', '--query-id', 'synthetic']), 0)
        self.assertEqual(meter_calls, [['sample', '--query-id', 'synthetic', '--engine', installed['engine']]])

    def test_service_starts_after_valid_publication_and_failure_keeps_generated_report(self):
        database, names = self.selection_fixture()
        out = self.root/'dashboard'
        config = self.root/'machine/settings.json'
        selection = ['--from-date', '2026-10-24', '--to-date', '2026-10-26', '--chat-id', ROOT]
        command = ['--config', str(config), '--database', str(database), '--names-database', str(names),
                   '--out', str(out), '--no-open', '--serve-dashboard', *selection]
        base = 'http://127.0.0.1:12345/'+'a'*32+'/'
        service = {'baseUrl': base, 'dashboardUrl': base+'index.html', 'refreshUrl': base+'refresh.html',
                   'pid': 123, 'reused': False}
        def start(package, settings, destination):
            self.assertEqual(local_setup.load_settings(settings)['outputDirectory'], str(out))
            payload = json.loads((destination/'data.json').read_text(encoding='utf-8'))
            self.assertEqual(payload['initialSelection']['chats'], [ROOT])
            self.assertTrue((destination/'evidence.json').is_file())
            return service
        mock_server = SimpleNamespace(ensure_server=start)
        stream = io.StringIO()
        with patch.dict(sys.modules, {'refresh_server': mock_server}), contextlib.redirect_stdout(stream):
            self.assertIn(refresh.main(command), (0, 2))
        result = json.loads(stream.getvalue())
        self.assertEqual(urlsplit(result['dashboardUrl']).scheme, 'http')
        self.assertEqual(urlsplit(result['fileDashboardUrl']).scheme, 'file')
        self.assertEqual(parse_qs(urlsplit(result['dashboardUrl']).fragment)['chat'], [ROOT])
        self.assertEqual(result['localServer'], service)
        mock_server.ensure_server = lambda *args: (_ for _ in ()).throw(RuntimeError('Synthetic local startup failure'))
        stream = io.StringIO()
        with patch.dict(sys.modules, {'refresh_server': mock_server}), contextlib.redirect_stdout(stream):
            self.assertEqual(refresh.main(command), 2)
        failed = json.loads(stream.getvalue())
        self.assertEqual(failed['dashboardUrl'], failed['fileDashboardUrl'])
        self.assertIn('local refresh is unavailable', failed['warnings'][-1])
        evidence = json.loads((out/'evidence.json').read_text(encoding='utf-8'))
        self.assertEqual(evidence['htmlSha256'], hashlib.sha256((out/'index.html').read_bytes()).hexdigest())
        self.assertEqual(json.loads((out/'data.json').read_text(encoding='utf-8'))['initialSelection'],
                         result['initialSelection'])
        self.assertEqual(local_setup.load_settings(config)['outputDirectory'], str(out))

    def test_serve_only_uses_existing_report_without_scanning_or_changing_settings(self):
        database, names = self.selection_fixture()
        status, first = self.run_selection(database, names, '--from-date', '2026-10-24', '--to-date', '2026-10-26',
                                            '--chat-id', ROOT)
        self.assertIn(status, (0, 2))
        out = Path(first['dashboard']).parent
        config = self.root/'machine/settings.json'
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in (config, out/'index.html', out/'data.json', out/'usage-ledger.json')}
        payload = json.loads((out/'data.json').read_text(encoding='utf-8'))
        base = 'http://127.0.0.1:12345/'+'a'*32+'/'
        service = {'baseUrl': base, 'dashboardUrl': base+'index.html', 'refreshUrl': base+'refresh.html',
                   'pid': 123, 'reused': False}
        server = SimpleNamespace(verified_existing_report=lambda path: (payload['browserBundle'], payload['initialSelection']),
                                 ensure_server=lambda *args: service)
        installed = {'engine': str(self.root/'engine.exe'), 'pythonPackages': str(self.root/'packages')}
        stream = io.StringIO()
        with patch.object(windows_package, 'verify_bundle'), \
             patch.object(prebuilt_runtime, 'resolve_prebuilt', return_value=installed), \
             patch.object(dashboard_data, 'build_bundle', side_effect=AssertionError('No source scan in serve-only')), \
             patch.object(local_setup, 'save_settings', side_effect=AssertionError('No settings write in serve-only')), \
             patch.dict(sys.modules, {'refresh_server': server}), contextlib.redirect_stdout(stream):
            self.assertEqual(standalone_app.main(['--config', str(config), '--serve-only', '--no-open']), 0)
        result = json.loads(stream.getvalue())
        self.assertFalse(result['scanPerformed'])
        self.assertEqual(result['initialSelection'], first['initialSelection'])
        self.assertEqual(parse_qs(urlsplit(result['dashboardUrl']).fragment)['chat'], [ROOT])
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before})

    def test_scope_without_dates_freezes_app_today_and_corrupt_saved_selection_fails_closed(self):
        database, names = self.selection_fixture()
        bundle, _, _ = dashboard_data.build_bundle(database, [names], now=NOW)
        bundle['projectMetadata'] = project_metadata.resolve_project_metadata(bundle, [names])
        bundle['source']['timeZone'] = 'UTC'
        selection = refresh.validate_selection(bundle, {
            'startDate': None, 'endDate': None, 'projects': [], 'chats': [ROOT]}, now=NOW)
        self.assertEqual(selection['startDate'], '2026-10-26')
        self.assertEqual(selection['endDate'], '2026-10-26')
        saved = self.root/'saved dashboard'
        saved.mkdir()
        state = saved/'.usage-selection.json'
        state.write_text('{"schemaVersion":1,"selection":{},"selection":null}', encoding='utf-8')
        import argparse
        parser = argparse.ArgumentParser()
        refresh.selection_arguments(parser)
        with self.assertRaisesRegex(ValueError, 'Invalid saved dashboard selection'):
            refresh.requested_selection(parser.parse_args([]), root=saved)

    def test_refresh_calls_no_model_or_browser_when_no_open_and_reuses_cache(self):
        database, names = fixture(self.root)
        out = self.root/'local dashboard'
        config = self.root/'machine/settings.json'
        command = ['--config', str(config), '--database', str(database),
                   '--names-database', str(names), '--out', str(out), '--no-open']
        with patch.object(refresh.webbrowser, 'open', side_effect=AssertionError('No browser launch in this mode')):
            results = []
            for _ in range(2):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    code = refresh.main(command)
                self.assertIn(code, (0, 2))
                results.append(json.loads(stream.getvalue()))
        self.assertEqual(results[0]['cache']['status'], 'miss')
        self.assertEqual(results[1]['cache']['status'], 'hit')
        self.assertEqual(results[0]['setup'], {'status': 'created', 'settingsFile': str(config)})
        self.assertEqual(results[1]['setup'], {'status': 'reused', 'settingsFile': str(config)})
        self.assertEqual(results[1]['modelCalls'], 0)
        self.assertFalse(results[1]['networkUsed'])
        self.assertNotIn('recordedDays', results[1]['coverage'])
        self.assertTrue((out/'refresh.cmd').is_file())
        self.assertTrue((out/'scripts/refresh_usage.py').is_file())
        payload = json.loads((out/'data.json').read_text(encoding='utf-8'))
        self.assertEqual(len(payload['browserBundle']['daily']), 4)

    def test_clean_export_runs_after_relocation_with_another_appdata_directory(self):
        database, names = fixture(self.root)
        archive = self.root/'portable.zip'
        refresh.export_package(archive)
        destination = self.root/'another machine ü'
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(destination)
        app = destination/'codex-local-usage'
        roaming = self.root/'different-profile/AppData/Roaming'
        selected = roaming/'com.ccusage.codex.desktop/codex-usage-desktop.db'
        selected.parent.mkdir(parents=True)
        shutil.copy2(database, selected)
        environment = dict(os.environ, APPDATA=str(roaming))
        config = self.root/'other machine config/settings.json'
        # Explicit source works on every test host; discovery is separately verified above.
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(app/'scripts/refresh_usage.py'),
                                 '--config', str(config), '--database', str(selected),
                                 '--names-database', str(names), '--no-open'],
                                cwd=self.root, env=environment, capture_output=True, text=True, encoding='utf-8')
        self.assertIn(result.returncode, (0, 2), result.stderr)
        output = json.loads(result.stdout)
        dashboard = config.parent/'dashboard'
        self.assertEqual(output['dashboard'], str(dashboard/'index.html'))
        self.assertTrue((dashboard/'index.html').is_file())
        self.assertTrue((dashboard/'usage-ledger.json').is_file())
        self.assertFalse((app/'settings.json').exists())
        self.assertFalse((app/'index.html').exists())
        self.assertEqual(json.loads(config.read_text(encoding='utf-8'))['database'], str(selected.resolve()))
        self.assertEqual(output['modelCalls'], 0)
        self.assertNotIn('local dashboard', result.stdout)


if __name__ == '__main__':
    unittest.main()
