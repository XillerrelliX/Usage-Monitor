"""Machine-local one-time setup, persistence and offline direct refresh behavior."""
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
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import dashboard_data
import local_setup
import refresh_usage as refresh
import report_from_app as core
from test_scopes import fixture, ROOT, AGENT, NESTED, FORK, OTHER


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root/'machine/settings.json'
        self.settings = {'schemaVersion': 1, 'database': str(self.root/'source.sqlite'),
                         'namesDatabases': None, 'projectState': None,
                         'outputDirectory': str(self.root/'dashboard'), 'cacheFile': None}

    def test_platform_defaults_belong_to_current_user_and_create_nothing(self):
        home = self.root/'different user ü'
        cases = [
            ('win32', {'LOCALAPPDATA': str(home/'Local'), 'APPDATA': str(home/'Roaming')},
             home/'Local', home/'Local'),
            ('win32', {}, home/'AppData/Local', home/'AppData/Local'),
            ('darwin', {}, home/'Library/Application Support', home/'Library/Application Support'),
            ('linux', {'XDG_CONFIG_HOME': str(home/'configuration'), 'XDG_DATA_HOME': str(home/'data')},
             home/'configuration', home/'data'),
            ('linux', {}, home/'.config', home/'.local/share'),
        ]
        for platform, environ, config_base, data_base in cases:
            with self.subTest(platform=platform, environ=environ):
                self.assertEqual(local_setup.default_settings_file(platform, environ, home),
                                 config_base/'codex-local-usage/settings.json')
                self.assertEqual(local_setup.default_dashboard_directory(platform, environ, home),
                                 data_base/'codex-local-usage/dashboard')
        self.assertFalse(home.exists())

    def test_first_save_round_trip_and_unchanged_reuse_do_not_touch_settings(self):
        self.assertIsNone(local_setup.load_settings(self.config))
        self.assertFalse(self.config.parent.exists())
        self.assertEqual(local_setup.save_settings(self.config, self.settings), 'created')
        self.assertEqual(local_setup.load_settings(self.config), self.settings)
        before = self.config.read_bytes(), self.config.stat().st_mtime_ns
        self.assertEqual(local_setup.save_settings(self.config, dict(self.settings)), 'reused')
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mtime_ns), before)
        updated = {**self.settings, 'namesDatabases': [str(self.root/'state_9.sqlite')],
                   'projectState': str(self.root/'state.json'), 'cacheFile': str(self.root/'custom-ledger.json')}
        self.assertEqual(local_setup.save_settings(self.config, updated), 'updated')
        self.assertEqual(local_setup.load_settings(self.config), updated)
        self.assertEqual(list(self.config.parent.glob('.usage-settings-*')), [])

    def test_malformed_or_unsupported_settings_fail_without_replacing_bytes(self):
        invalid = [b'{', b'\xff\xfe\x00', b'[]', b'null', b'{}']
        changes = [
            {'schemaVersion': 2}, {'schemaVersion': True}, {'database': 'relative.sqlite'},
            {'database': None}, {'outputDirectory': ''}, {'namesDatabases': 'state.sqlite'},
            {'namesDatabases': [7]}, {'namesDatabases': ['relative.sqlite']},
            {'projectState': []}, {'cacheFile': 'relative-ledger.json'},
        ]
        invalid.extend(json.dumps({**self.settings, **change}).encode() for change in changes)
        self.config.parent.mkdir(parents=True)
        for original in invalid:
            with self.subTest(original=original):
                self.config.write_bytes(original)
                before = self.config.stat().st_mtime_ns
                with self.assertRaisesRegex(ValueError, 'setup|settings|version'):
                    local_setup.load_settings(self.config)
                self.assertEqual(self.config.read_bytes(), original)
                self.assertEqual(self.config.stat().st_mtime_ns, before)
                with self.assertRaises(ValueError):
                    local_setup.save_settings(self.config, self.settings)
                self.assertEqual(self.config.read_bytes(), original)

    def test_invalid_new_settings_and_failed_atomic_write_preserve_existing_file(self):
        local_setup.save_settings(self.config, self.settings)
        original = self.config.read_bytes(), self.config.stat().st_mtime_ns
        with self.assertRaises(ValueError):
            local_setup.save_settings(self.config, {**self.settings, 'outputDirectory': 'relative'})
        with patch.object(local_setup.os, 'replace', side_effect=OSError('Cannot commit machine settings')):
            with self.assertRaises(OSError):
                local_setup.save_settings(self.config, {**self.settings, 'cacheFile': str(self.root/'another.json')})
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mtime_ns), original)
        self.assertEqual(list(self.config.parent.glob('.usage-settings-*')), [])

    def test_dashboard_pointer_remembers_config_and_reuses_unchanged_bytes(self):
        local_setup.save_settings(self.config, self.settings)
        out = self.root/'copied dashboard ü'
        pointer = out/'.usage-setup.json'
        self.assertEqual(local_setup.save_dashboard_pointer(out, self.config), 'created')
        self.assertEqual(json.loads(pointer.read_text(encoding='utf-8')),
                         {'schemaVersion': 1, 'settingsFile': str(self.config)})
        with patch.object(local_setup, 'default_settings_file', side_effect=AssertionError('Pointer must win')):
            self.assertEqual(local_setup.resolve_settings_file(out), self.config)
        before = pointer.read_bytes(), pointer.stat().st_mtime_ns
        self.assertEqual(local_setup.save_dashboard_pointer(out, self.config), 'reused')
        self.assertEqual((pointer.read_bytes(), pointer.stat().st_mtime_ns), before)
        other_config = self.root/'other computer/settings.json'
        local_setup.save_settings(other_config, self.settings)
        self.assertEqual(local_setup.save_dashboard_pointer(out, other_config), 'updated')
        self.assertEqual(local_setup.resolve_settings_file(out), other_config)

    def test_no_dashboard_pointer_uses_default_settings_without_creating_files(self):
        out = self.root/'unconfigured portable source'
        with patch.object(local_setup, 'default_settings_file', return_value=self.config) as default:
            self.assertEqual(local_setup.resolve_settings_file(out), self.config)
            default.assert_called_once_with()
        self.assertFalse(out.exists())
        self.assertFalse(self.config.exists())

    def test_bad_dashboard_pointer_is_preserved_and_explicit_config_bypasses_it(self):
        local_setup.save_settings(self.config, self.settings)
        out = self.root/'copied dashboard'
        out.mkdir()
        pointer = out/'.usage-setup.json'
        invalid = [b'{', b'\xff\xfe\x00', b'[]', b'null', b'{}',
                   json.dumps({'schemaVersion': 2, 'settingsFile': str(self.config)}).encode(),
                   json.dumps({'schemaVersion': True, 'settingsFile': str(self.config)}).encode(),
                   b'{"schemaVersion":1,"settingsFile":"relative/settings.json"}',
                   b'{"schemaVersion":1,"settingsFile":null}']
        for original in invalid:
            with self.subTest(original=original):
                pointer.write_bytes(original)
                before = pointer.stat().st_mtime_ns
                with patch.object(local_setup, 'default_settings_file', side_effect=AssertionError('No fallback from bad pointer')):
                    with self.assertRaisesRegex(ValueError, 'pointer|setup'):
                        local_setup.resolve_settings_file(out)
                    self.assertEqual(local_setup.resolve_settings_file(out, self.config), self.config)
                    with self.assertRaisesRegex(ValueError, 'pointer|setup'):
                        local_setup.save_dashboard_pointer(out, self.config)
                self.assertEqual(pointer.read_bytes(), original)
                self.assertEqual(pointer.stat().st_mtime_ns, before)

    def test_missing_pointer_destination_fails_without_creating_default_settings(self):
        out = self.root/'copied dashboard'
        out.mkdir()
        pointer = out/'.usage-setup.json'
        pointer.write_text(json.dumps({'schemaVersion': 1, 'settingsFile': str(self.config)}), encoding='utf-8')
        before = pointer.read_bytes(), pointer.stat().st_mtime_ns
        with patch.object(local_setup, 'default_settings_file', side_effect=AssertionError('No default fallback')):
            with self.assertRaisesRegex(ValueError, '--config'):
                local_setup.resolve_settings_file(out)
        self.assertEqual((pointer.read_bytes(), pointer.stat().st_mtime_ns), before)
        self.assertFalse(self.config.exists())

    def test_pointer_can_be_updated_to_an_explicit_valid_config_after_old_destination_disappears(self):
        out = self.root/'copied dashboard'
        out.mkdir()
        pointer = out/'.usage-setup.json'
        pointer.write_text(json.dumps({'schemaVersion': 1, 'settingsFile': str(self.root/'missing-settings.json')}),
                           encoding='utf-8')
        local_setup.save_settings(self.config, self.settings)
        self.assertEqual(local_setup.resolve_settings_file(out, self.config), self.config)
        self.assertEqual(local_setup.save_dashboard_pointer(out, self.config), 'updated')
        self.assertEqual(local_setup.resolve_settings_file(out), self.config)

    def test_pointer_is_not_saved_before_a_valid_settings_file_exists(self):
        out = self.root/'copied dashboard'
        with self.assertRaises(ValueError):
            local_setup.save_dashboard_pointer(out, self.config)
        self.assertFalse((out/'.usage-setup.json').exists())
        self.config.parent.mkdir(parents=True)
        self.config.write_bytes(b'{"schemaVersion":999}')
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            local_setup.save_dashboard_pointer(out, self.config)
        self.assertFalse((out/'.usage-setup.json').exists())
        self.assertEqual(self.config.read_bytes(), before)


class MainSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database, self.names = fixture(self.root)
        self.config = self.root/'machine/settings.json'
        self.addCleanup(patch.stopall)
        patch.object(refresh.webbrowser, 'open', side_effect=AssertionError('No browser launch')).start()
        patch.object(core, 'discover_names_database', return_value=[self.names]).start()
        # Automatic project metadata and fallback settings stay inside the synthetic profile.
        profile = self.root/'isolated profile'
        patch.dict(os.environ, {'CODEX_HOME': str(profile/'codex'), 'LOCALAPPDATA': str(profile/'Local'),
                                'APPDATA': str(profile/'Roaming'), 'XDG_CONFIG_HOME': str(profile/'config'),
                                'XDG_DATA_HOME': str(profile/'data'), 'HOME': str(profile),
                                'USERPROFILE': str(profile)}).start()

    def run_main(self, arguments=(), *, success=True, config=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        command = (['--config', str(self.config)] if config else []) + list(arguments) + ['--read-cache', '--no-open']
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = refresh.main(command)
        if success:
            self.assertIn(code, (0, 2), stderr.getvalue())
            self.assertEqual(stderr.getvalue(), '')
            result = json.loads(stdout.getvalue())
            self.assertEqual(result['modelCalls'], 0)
            self.assertFalse(result['networkUsed'])
            self.assertIsInstance(result['chats'], int)
            for excluded in ('Chosen chat', 'Private', ROOT, AGENT, NESTED, FORK, OTHER):
                self.assertNotIn(excluded, stdout.getvalue())
            self.assertEqual(set(result['setup']), {'status', 'settingsFile'})
            return result
        self.assertEqual(code, 1, stdout.getvalue())
        self.assertEqual(stdout.getvalue(), '')
        error = json.loads(stderr.getvalue())
        self.assertEqual(error['modelCalls'], 0)
        self.assertFalse(error['networkUsed'])
        self.assertNotIn('setup', error)
        return error

    def test_first_automatic_run_saves_discovery_and_second_run_reuses_it(self):
        before_sources = {path: path.read_bytes() for path in (self.database, self.names)}
        with patch.object(refresh, 'source_candidates', return_value=[self.database]) as discover:
            first = self.run_main()
            discover.assert_called_once_with()
        out = self.config.parent/'dashboard'
        self.assertEqual(first['dashboard'], str(out/'index.html'))
        self.assertEqual(first['setup'], {'status': 'created', 'settingsFile': str(self.config)})
        self.assertEqual(first['cache']['status'], 'miss')
        self.assertEqual(local_setup.load_settings(self.config), {
            'schemaVersion': 1, 'database': str(self.database), 'namesDatabases': None,
            'projectState': None, 'outputDirectory': str(out), 'cacheFile': None})
        before = self.config.read_bytes(), self.config.stat().st_mtime_ns
        with patch.object(refresh, 'source_candidates', side_effect=AssertionError('Discovery must be saved')), \
             patch.object(dashboard_data, '_read_numeric', side_effect=AssertionError('Cached ledger must be reused')):
            second = self.run_main(['--setup'])
        self.assertEqual(second['cache']['status'], 'hit')
        self.assertEqual(second['setup'], {'status': 'reused', 'settingsFile': str(self.config)})
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mtime_ns), before)
        for path, original in before_sources.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertTrue((out/'setup.cmd').is_file())
        self.assertTrue((out/'setup.sh').is_file())
        self.assertTrue((out/'scripts/local_setup.py').is_file())
        self.assertEqual(json.loads((out/'.usage-setup.json').read_text(encoding='utf-8')),
                         {'schemaVersion': 1, 'settingsFile': str(self.config)})

    def test_default_output_is_per_user_data_not_installed_package(self):
        out = self.root/'os-user-data/codex-local-usage/dashboard'
        with patch.object(local_setup, 'default_settings_file', return_value=self.config), \
             patch.object(local_setup, 'default_dashboard_directory', return_value=out):
            result = self.run_main(['--database', str(self.database), '--names-database', str(self.names)],
                                   config=False)
        self.assertEqual(result['dashboard'], str(out/'index.html'))
        self.assertEqual(local_setup.load_settings(self.config)['outputDirectory'], str(out))

    def test_setup_is_idempotent_and_refresh_alone_forces_numeric_revalidation(self):
        arguments = ['--database', str(self.database), '--names-database', str(self.names)]
        self.run_main(['--setup', *arguments])
        with patch.object(dashboard_data, '_read_numeric', side_effect=AssertionError('Setup is not forced refresh')):
            setup = self.run_main(['--setup'])
        self.assertEqual(setup['cache']['status'], 'hit')
        forced = self.run_main(['--refresh'])
        self.assertEqual(forced['cache']['status'], 'miss')
        self.assertEqual(forced['cache']['reason'], 'refresh_requested')
        self.assertEqual(forced['setup']['status'], 'reused')

    def test_successful_explicit_overrides_are_saved_and_used_on_bare_refresh(self):
        self.run_main(['--database', str(self.database), '--names-database', str(self.names)])
        database, names = self.root/'new source.sqlite', self.root/'new names.sqlite'
        shutil.copy2(self.database, database)
        shutil.copy2(self.names, names)
        state, out, cache = self.root/'project state.json', self.root/'new dashboard', self.root/'ledger/custom.json'
        state.write_text('{}', encoding='utf-8')
        result = self.run_main(['--database', str(database), '--names-database', str(names),
                                '--project-state', str(state), '--out', str(out), '--cache-file', str(cache)])
        self.assertEqual(result['setup']['status'], 'updated')
        expected = {'schemaVersion': 1, 'database': str(database), 'namesDatabases': [str(names)],
                    'projectState': str(state), 'outputDirectory': str(out), 'cacheFile': str(cache)}
        self.assertEqual(local_setup.load_settings(self.config), expected)
        with patch.object(dashboard_data, 'build_bundle', wraps=dashboard_data.build_bundle) as build:
            repeated = self.run_main()
        self.assertEqual(build.call_args.args, (database, [names]))
        self.assertEqual(build.call_args.kwargs['cache_file'], cache)
        self.assertEqual(repeated['dashboard'], str(out/'index.html'))
        self.assertEqual(repeated['setup']['status'], 'reused')
        self.assertEqual(local_setup.load_settings(self.config), expected)

    def test_relative_setup_arguments_are_saved_as_absolute_machine_paths(self):
        state = self.root/'project state.json'
        state.write_text('{}', encoding='utf-8')
        previous_directory = Path.cwd()
        try:
            os.chdir(self.root)
            result = self.run_main(['--config', 'relative machine/settings.json',
                                    '--database', 'fixture.sqlite', '--names-database', 'names.sqlite',
                                    '--project-state', 'project state.json', '--out', 'relative dashboard',
                                    '--cache-file', 'custom ledger/history.json'], config=False)
        finally:
            os.chdir(previous_directory)
        config = self.root/'relative machine/settings.json'
        self.assertEqual(result['setup']['settingsFile'], str(config))
        self.assertEqual(local_setup.load_settings(config), {
            'schemaVersion': 1, 'database': str(self.database), 'namesDatabases': [str(self.names)],
            'projectState': str(state), 'outputDirectory': str(self.root/'relative dashboard'),
            'cacheFile': str(self.root/'custom ledger/history.json')})

    def test_default_ledger_location_follows_a_successful_output_override(self):
        self.run_main(['--database', str(self.database), '--names-database', str(self.names)])
        out = self.root/'moved dashboard'
        moved = self.run_main(['--out', str(out)])
        self.assertEqual(moved['setup']['status'], 'updated')
        self.assertEqual(moved['cacheFile'], str(out/'usage-ledger.json'))
        self.assertIsNone(local_setup.load_settings(self.config)['cacheFile'])
        repeated = self.run_main()
        self.assertEqual(repeated['dashboard'], str(out/'index.html'))
        self.assertEqual(repeated['cacheFile'], str(out/'usage-ledger.json'))
        self.assertEqual(repeated['cache']['status'], 'hit')

    def test_missing_source_and_failed_render_never_mark_first_setup_complete(self):
        for failure in ('missing source', 'failed render'):
            with self.subTest(failure=failure):
                if failure == 'missing source':
                    error = self.run_main(['--database', str(self.root/'missing.sqlite')], success=False)
                    self.assertIn('database', error['error'].lower())
                else:
                    with patch.object(refresh, 'render_dashboard', side_effect=ValueError('Synthetic render failure')):
                        error = self.run_main(['--database', str(self.database),
                                               '--names-database', str(self.names)], success=False)
                    self.assertIn('render failure', error['error'])
                self.assertFalse(self.config.exists())

    def test_missing_automatic_source_keeps_setup_absent_with_actionable_prerequisite(self):
        with patch.object(refresh, 'source_candidates', return_value=[self.root/'uninstalled app/missing.db']):
            error = self.run_main(['--setup'], success=False)
        self.assertIn('--database PATH', error['error'])
        self.assertIn('Codex Usage Desktop', error['error'])
        self.assertFalse(self.config.exists())

    def test_failed_accounting_and_render_preserve_previous_settings_and_overrides(self):
        self.run_main(['--database', str(self.database), '--names-database', str(self.names)])
        original = self.config.read_bytes(), self.config.stat().st_mtime_ns
        pointer = self.config.parent/'dashboard/.usage-setup.json'
        original_pointer = pointer.read_bytes(), pointer.stat().st_mtime_ns
        broken = self.root/'broken.sqlite'
        shutil.copy2(self.database, broken)
        with closing(sqlite3.connect(broken)) as db, db:
            db.execute('UPDATE daily_usage_rollups SET input_tokens=input_tokens+1 WHERE date=?', ('2026-10-24',))
        error = self.run_main(['--database', str(broken), '--out', str(self.root/'failed output')], success=False)
        self.assertIn('disagree', error['error'])
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mtime_ns), original)
        self.assertEqual((pointer.read_bytes(), pointer.stat().st_mtime_ns), original_pointer)
        self.assertFalse((self.root/'failed output/.usage-setup.json').exists())
        with patch.object(refresh, 'render_dashboard', side_effect=ValueError('Synthetic render failure')):
            self.run_main(['--out', str(self.root/'other failed output')], success=False)
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mtime_ns), original)
        self.assertEqual((pointer.read_bytes(), pointer.stat().st_mtime_ns), original_pointer)
        self.assertFalse((self.root/'other failed output/.usage-setup.json').exists())
        self.assertEqual(self.run_main()['setup']['status'], 'reused')

    def test_bad_existing_settings_fail_before_source_discovery_and_are_preserved(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_bytes(b'{"schemaVersion":999}')
        before = self.config.read_bytes(), self.config.stat().st_mtime_ns
        with patch.object(refresh, 'find_database', side_effect=AssertionError('No discovery for bad settings')):
            error = self.run_main(['--database', str(self.database)], success=False)
        self.assertIn('--config', error['error'])
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mtime_ns), before)

    def test_bad_copied_dashboard_pointer_fails_before_discovery_and_is_preserved(self):
        package = self.root/'copied runtime'
        package.mkdir()
        pointer = package/'.usage-setup.json'
        pointer.write_bytes(b'{"schemaVersion":999}')
        before = pointer.read_bytes(), pointer.stat().st_mtime_ns
        with patch.object(refresh, 'PACKAGE_ROOT', package), \
             patch.object(refresh, 'find_database', side_effect=AssertionError('No discovery after bad pointer')):
            error = self.run_main(config=False, success=False)
        self.assertIn('pointer', error['error'])
        self.assertEqual((pointer.read_bytes(), pointer.stat().st_mtime_ns), before)
        self.assertFalse(local_setup.default_settings_file().exists())
        self.assertFalse(self.config.exists())

    def test_discovered_setup_and_direct_subprocess_refresh_survive_changed_working_directory(self):
        profile = self.root/'isolated profile'
        environment = dict(os.environ, APPDATA=str(profile/'Roaming'), LOCALAPPDATA=str(profile/'Local'),
                           XDG_DATA_HOME=str(profile/'data'), XDG_CONFIG_HOME=str(profile/'config'),
                           HOME=str(profile), USERPROFILE=str(profile), CODEX_HOME=str(profile/'codex'))
        selected = refresh.source_candidates(sys.platform, environment, profile)[0]
        selected.parent.mkdir(parents=True)
        shutil.copy2(self.database, selected)
        audit_dir = self.root/'offline-audit'
        audit_dir.mkdir()
        (audit_dir/'sitecustomize.py').write_text(
            'import os, sys\n'
            'from pathlib import Path\n'
            'Path(os.environ["USAGE_TEST_AUDIT_READY"]).write_text("active", encoding="utf-8")\n'
            'def deny_external_calls(event, args):\n'
            '    if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system", "webbrowser.open"}:\n'
            '        raise AssertionError("Offline refresh attempted external call: " + event)\n'
            'sys.addaudithook(deny_external_calls)\n', encoding='utf-8')
        environment['PYTHONPATH'] = str(audit_dir)
        command = [sys.executable, '-B', '-X', 'utf8', str(refresh.PACKAGE_ROOT/'scripts/refresh_usage.py'),
                   '--config', str(self.config), '--read-cache', '--no-open']
        results = []
        before = None
        for position, cwd in enumerate((self.root, self.root/'unrelated working directory')):
            cwd.mkdir(exist_ok=True)
            marker = self.root/('offline-audit-'+str(position)+'.txt')
            environment['USAGE_TEST_AUDIT_READY'] = str(marker)
            if position == 0:
                args = [*command, '--names-database', str(self.names)]
            else:
                copied_runtime = Path(results[0]['dashboard']).parent/'scripts/refresh_usage.py'
                args = [sys.executable, '-B', '-X', 'utf8', str(copied_runtime), '--no-open']
            result = subprocess.run(args, cwd=cwd, env=environment, capture_output=True, text=True, encoding='utf-8')
            self.assertIn(result.returncode, (0, 2), result.stderr)
            self.assertTrue(marker.is_file(), 'The subprocess offline guard must execute.')
            output = json.loads(result.stdout)
            results.append(output)
            self.assertEqual(output['modelCalls'], 0)
            self.assertFalse(output['networkUsed'])
            self.assertNotIn('Chosen chat', result.stdout)
            pointer = Path(output['dashboard']).parent/'.usage-setup.json'
            current = (self.config.read_bytes(), self.config.stat().st_mtime_ns,
                       pointer.read_bytes(), pointer.stat().st_mtime_ns,
                       hashlib.sha256(Path(output['cacheFile']).read_bytes()).hexdigest())
            if position == 0:
                before = current
                self.assertEqual(local_setup.load_settings(self.config)['database'], str(selected.resolve()))
                # A changed profile now contains no discoverable app cache. Saved paths must win.
                environment.update(APPDATA=str(profile/'other-roaming'), LOCALAPPDATA=str(profile/'other-local'),
                                   XDG_DATA_HOME=str(profile/'other-data'), XDG_CONFIG_HOME=str(profile/'other-config'),
                                   HOME=str(profile/'other-home'), USERPROFILE=str(profile/'other-home'),
                                   CODEX_HOME=str(profile/'other-codex'))
            else:
                self.assertEqual(current, before)
            self.assertFalse(local_setup.default_settings_file(sys.platform, environment, Path(environment['HOME'])).exists())
        self.assertEqual(results[0]['setup']['status'], 'created')
        self.assertEqual(results[1]['setup']['status'], 'reused')
        self.assertEqual(results[0]['cache']['status'], 'miss')
        self.assertEqual(results[1]['cache']['status'], 'hit')
        self.assertEqual(results[0]['dashboard'], results[1]['dashboard'])
        self.assertEqual(results[1]['setup']['settingsFile'], str(self.config))

    @unittest.skipUnless(os.name == 'nt', 'Windows batch launchers require cmd.exe.')
    def test_windows_setup_then_refresh_launchers_keep_settings_and_verified_ledger(self):
        archive = self.root/'portable.zip'
        refresh.export_package(archive)
        destination = self.root/'portable package ü'
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(destination)
        app = destination/'codex-local-usage'
        config = self.root/'computer settings ü/settings.json'
        database, names = self.root/'synthetic source ü.sqlite', self.root/'synthetic sidebar ü.sqlite'
        shutil.copy2(self.database, database)
        shutil.copy2(self.names, names)
        commands = [
            [str(app/'setup.cmd'), '--config', str(config), '--database', str(database),
             '--names-database', str(names), '--no-open'],
            [str(config.parent/'dashboard/refresh.cmd'), '--no-open'],
        ]
        results, before = [], None
        for position, command in enumerate(commands):
            # cmd.exe's /s quoting keeps the batch path and Unicode/space arguments intact.
            shell_command = subprocess.list2cmdline([os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/s', '/c'])
            shell_command += ' "' + subprocess.list2cmdline(command) + '"'
            result = subprocess.run(shell_command,
                                    cwd=self.root, capture_output=True, text=True, encoding='utf-8')
            self.assertIn(result.returncode, (0, 2), result.stderr + result.stdout)
            output = json.loads(result.stdout)
            results.append(output)
            self.assertEqual(output['modelCalls'], 0)
            self.assertFalse(output['networkUsed'])
            ledger = Path(output['cacheFile'])
            pointer = Path(output['dashboard']).parent/'.usage-setup.json'
            current = (hashlib.sha256(ledger.read_bytes()).hexdigest(),
                       config.read_bytes(), config.stat().st_mtime_ns,
                       pointer.read_bytes(), pointer.stat().st_mtime_ns)
            if position == 0:
                before = current
            else:
                self.assertEqual(current, before)
        self.assertEqual(results[0]['setup']['status'], 'created')
        self.assertEqual(results[1]['setup']['status'], 'reused')
        self.assertEqual(results[0]['cache']['status'], 'miss')
        self.assertEqual(results[1]['cache']['status'], 'hit')
        self.assertEqual(results[0]['dashboard'], str(config.parent/'dashboard/index.html'))
        self.assertEqual(results[1]['dashboard'], results[0]['dashboard'])
        self.assertFalse(local_setup.default_settings_file().exists())
        self.assertFalse((app/'.usage-setup.json').exists())


if __name__ == '__main__':
    unittest.main()
