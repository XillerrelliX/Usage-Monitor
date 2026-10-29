"""Synthetic raw-rollout integration and transaction tests for the offline engine.

The expected counters below are independently calculated constants.  The native
parser is executed when CODEX_USAGE_TEST_ENGINE names a built executable; normal
source-package consumers can run the remaining tests without a Rust toolchain.
No test discovers or opens the current user's Codex directory.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'scripts'))
import dashboard_data as ledger
import offline_refresh as offline
import refresh_usage as refresh
import report_from_app as core

ROOT = '10000000-0000-4000-8000-000000000001'
CHILD = '20000000-0000-4000-8000-000000000002'
NESTED = '30000000-0000-4000-8000-000000000003'
FORK = '40000000-0000-4000-8000-000000000004'
COMPRESSED = '50000000-0000-4000-8000-000000000005'
ARCHIVED = '60000000-0000-4000-8000-000000000006'
A, B, C = '/fixture/one/shared', '/fixture/two/shared', '/fixture/separate'
PRIVATE = 'PRIVATE_PROMPT_SENTINEL_7b3ce2d4'
NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
ENGINE_ENV = os.environ.get('CODEX_USAGE_TEST_ENGINE')

# Input includes cached input; output includes reasoning. Neither subset is added
# again to total. Constants intentionally use all five independent counters.
TOTAL = (220, 100, 62, 17, 282)
DAY_TOTALS = {'2026-10-24': (128, 68, 34, 10, 162),
              '2026-10-25': (37, 12, 13, 3, 50),
              '2026-10-26': (55, 20, 15, 4, 70)}
PROJECT_TOTALS = {A: (147, 77, 36, 11, 183), B: (33, 8, 17, 4, 50),
                  C: (40, 15, 9, 2, 49)}
OWNER_TOTALS = {ROOT: (137, 69, 37, 11, 174), CHILD: (11, 4, 6, 2, 17),
                NESTED: (13, 3, 8, 1, 21), FORK: (17, 7, 4, 0, 21),
                COMPRESSED: (19, 9, 2, 1, 21), ARCHIVED: (23, 8, 5, 2, 28)}


def counters(values):
    return dict(zip(('input_tokens', 'cached_input_tokens', 'output_tokens',
                     'reasoning_output_tokens', 'total_tokens'), values))


def event(day, *, total=None, last=None, clock='12:00:00'):
    info = {}
    if total is not None:
        info['total_token_usage'] = counters(total)
    if last is not None:
        info['last_token_usage'] = counters(last)
    return {'timestamp': day + 'T' + clock + 'Z', 'type': 'event_msg',
            'payload': {'type': 'token_count', 'info': info}}


def context(day, project, model):
    return {'timestamp': day + 'T10:00:00Z', 'type': 'turn_context',
            'payload': {'cwd': project, 'model': model}}


def session(identity, project, events, *, parent=None, metadata_identity=None):
    payload = {'id': metadata_identity or identity, 'cwd': project}
    if parent:
        payload['source'] = {'subagent': {'thread_spawn': {
            'parent_thread_id': parent, 'depth': 2 if identity == NESTED else 1,
            'agent_path': '/root/child/nested' if identity == NESTED else '/root/child',
            'agent_nickname': 'Stale fork nickname' if metadata_identity else 'Fixture agent',
            'agent_role': 'synthetic-worker'}}}
    return [
        {'timestamp': '2026-10-24T09:00:00Z', 'type': 'session_meta', 'payload': payload},
        {'timestamp': '2026-10-24T09:01:00Z', 'type': 'event_msg',
         'payload': {'type': 'user_message', 'message': PRIVATE}},
        {'timestamp': '2026-10-24T09:02:00Z', 'type': 'response_item',
         'payload': {'type': 'message', 'role': 'user',
                     'content': [{'type': 'input_text', 'text': PRIVATE}]}},
        *events,
    ]


def zstd_raw_frame(payload):
    """Standard Zstd frame with one raw block, requiring no Python dependency."""
    if len(payload) >= 128 * 1024:
        raise ValueError('Fixture exceeds one Zstd block.')
    # Single-segment frame, 32-bit content size, final uncompressed block.
    return (b'\x28\xb5\x2f\xfd\xa0' + len(payload).to_bytes(4, 'little')
            + ((len(payload) << 3) | 1).to_bytes(3, 'little') + payload)


def write_session(home, identity, entries, *, archived=False, compressed=False):
    directory = home / ('archived_sessions' if archived else 'sessions/2026/10/24')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ('rollout-2026-10-24T09-00-00-' + identity + '.jsonl'
                        + ('.zst' if compressed else ''))
    payload = ('\n'.join(json.dumps(row, separators=(',', ':')) for row in entries) + '\n').encode()
    path.write_bytes(zstd_raw_frame(payload) if compressed else payload)
    return path


def raw_fixture(directory):
    home = directory / 'synthetic codex home ü'
    # Repeated cumulative totals contribute zero. Explicit last usage wins over
    # a deliberately much larger cumulative jump. A reset contributes zero until
    # the next cumulative increase, whose delta is (10,2,5,2,15).
    root = session(ROOT, A, [
        context('2026-10-24', A, 'fixture-model-a'),
        event('2026-10-24', total=(100, 60, 20, 5, 120)),
        event('2026-10-24', total=(100, 60, 20, 5, 120)),
        event('2026-10-24', total=(9000, 6000, 2000, 500, 11000), last=(7, 2, 3, 1, 10)),
        event('2026-10-24', total=(4, 1, 2, 0, 6)),
        event('2026-10-24', total=(14, 3, 7, 2, 21)),
        context('2026-10-25', B, 'fixture-model-b'),
        event('2026-10-25', total=(34, 8, 16, 5, 50)),
    ])
    paths = {'root': write_session(home, ROOT, root)}
    # A stale archive must never replace an active session with the same ID.
    write_session(home, ROOT, session(ROOT, A, [context('2026-10-24', A, 'fixture-model-a'),
                                              event('2026-10-24', last=(999, 99, 99, 9, 1098))]), archived=True)
    write_session(home, ROOT, root, compressed=True)
    for key, identity, day, project, model, values, parent, compressed, archived in [
        ('child', CHILD, '2026-10-24', A, 'a', OWNER_TOTALS[CHILD], ROOT, False, False),
        ('nested', NESTED, '2026-10-26', B, 'b', OWNER_TOTALS[NESTED], CHILD, False, False),
        ('fork', FORK, '2026-10-25', C, 'c', OWNER_TOTALS[FORK], ROOT, False, False),
        ('compressed', COMPRESSED, '2026-10-26', A, 'a', OWNER_TOTALS[COMPRESSED], None, True, False),
        ('archived', ARCHIVED, '2026-10-26', C, 'c', OWNER_TOTALS[ARCHIVED], None, True, True),
    ]:
        entries = session(identity, project, [context(day, project, 'fixture-model-' + model),
                                              event(day, last=values)], parent=parent,
                          metadata_identity=CHILD if identity == FORK else None)
        paths[key] = write_session(home, identity, entries, archived=archived, compressed=compressed)
    # Files outside the rollout directories are not usage inputs.
    (home / 'auth.json').write_text(PRIVATE, encoding='utf-8')
    (home / 'config.toml').write_text('# ' + PRIVATE, encoding='utf-8')
    (home / 'sessions/ignored.txt').write_text(PRIVATE, encoding='utf-8')
    return home, paths


class OfflineOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'synthetic home'
        self.home.mkdir()

    def test_owned_cache_cannot_replace_any_codex_input(self):
        protected = self.home / 'auth.json'
        protected.write_text(PRIVATE, encoding='utf-8')
        with patch.object(offline.subprocess, 'run', side_effect=AssertionError('Must refuse before launch')):
            with self.assertRaisesRegex(ValueError, 'outside'):
                with offline.prepare_database(PACKAGE, protected, self.home, engine=Path(sys.executable)):
                    self.fail('A cache inside the source directory was accepted')
        self.assertEqual(protected.read_text(encoding='utf-8'), PRIVATE)

    def test_unknown_existing_database_is_never_claimed_or_modified(self):
        for name, application_id, marker in [
            ('stock-app.db', 0, None),
            ('unknown-marker.db', 0x43554F46, ('wrong-version', 'wrong-commit')),
        ]:
            with self.subTest(name=name):
                database = self.root / name
                with closing(sqlite3.connect(database)) as db, db:
                    db.execute('CREATE TABLE private_source(value TEXT)')
                    db.execute('INSERT INTO private_source VALUES(?)', (PRIVATE,))
                    db.execute('PRAGMA application_id=' + str(application_id))
                    if marker:
                        db.execute('CREATE TABLE offline_engine_meta(schema_version,source_commit,codex_home)')
                        db.execute('INSERT INTO offline_engine_meta VALUES(?,?,?)', (*marker, str(self.home)))
                original = database.read_bytes()
                with patch.object(offline.subprocess, 'run', side_effect=AssertionError('Must refuse before launch')):
                    with self.assertRaises((ValueError, OSError)):
                        with offline.prepare_database(PACKAGE, database, self.home, engine=Path(sys.executable)):
                            self.fail('An unowned database was accepted.')
                self.assertEqual(database.read_bytes(), original)


@unittest.skipUnless(ENGINE_ENV, 'Native integration requires CODEX_USAGE_TEST_ENGINE; synthetic fixtures only.')
class OfflineNativeIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine = Path(ENGINE_ENV).resolve()
        self.assertTrue(self.engine.is_file(), 'CODEX_USAGE_TEST_ENGINE does not exist')
        self.home, self.paths = raw_fixture(self.root)
        self.database = self.root / 'owned-cache.sqlite'
        self.runtime = self.root / 'runtime'

    def prepare(self, **kwargs):
        options = {'timezone': 'UTC', 'runtime_dir': self.runtime, 'engine': self.engine}
        options.update(kwargs)
        return offline.prepare_database(PACKAGE, self.database, self.home, **options)

    def scan(self):
        with self.prepare() as prepared:
            ledger.build_bundle(prepared.path, names_databases=(), cache_file=None, now=NOW)
            prepared.publish()
        return ledger.build_bundle(self.database, names_databases=(), cache_file=None, now=NOW)[0]

    def vector(self, row):
        return tuple(row[field] for field in core.FIELDS)

    def view(self, bundle, **scope):
        return ledger.project_bundle(bundle, '2026-10-24', '2026-10-26', now=NOW, **scope)

    def assert_reconciled(self, view, expected):
        self.assertEqual(self.vector(view['totals']), expected)
        for dimension in ('daily', 'owners', 'chats', 'models', 'projects'):
            self.assertEqual(tuple(sum(row[field] for row in view[dimension]) for field in core.FIELDS),
                             expected, dimension)
        for dimension in ('chats', 'models', 'projects'):
            for row in view[dimension]:
                self.assertEqual(tuple(sum(day[field] for day in row['daily']) for field in core.FIELDS),
                                 self.vector(row), dimension)

    def test_raw_fixture_exact_counters_lineage_scopes_and_original_adapter(self):
        real_run = offline.subprocess.run
        launches = []

        def inspect_launch(command, **kwargs):
            launches.append((command, kwargs))
            return real_run(command, **kwargs)

        with patch.dict(os.environ, {'OPENAI_API_KEY': PRIVATE, 'ANTHROPIC_API_KEY': PRIVATE}), \
             patch.object(offline.subprocess, 'run', side_effect=inspect_launch), self.prepare() as prepared:
            staging = prepared.path
            self.assertTrue(staging.is_file())
            self.assertFalse(self.database.exists(), 'Staging must remain private until validation succeeds')
            details = prepared.details
            self.assertEqual(details['schemaVersion'], 1)
            self.assertEqual(details['sourceCommit'], core.UPSTREAM_COMMIT)
            self.assertEqual(details['timezone'], 'UTC')
            self.assertEqual(details['importedDays'], 3)
            self.assertEqual(details['filesScanned'], 6)
            self.assertEqual(details['filesParsed'], 6)
            self.assertGreater(details['bytesRead'], 0)
            bundle, proof, _ = ledger.build_bundle(staging, names_databases=(), cache_file=None, now=NOW)
            self.assertTrue(proof['mappingOwnerIdsMatch'])
            prepared.publish()
        self.assertEqual(len(launches), 1)
        command, options = launches[0]
        self.assertIsInstance(command, list)
        self.assertFalse(options.get('shell', False))
        self.assertNotIn('OPENAI_API_KEY', options['env'])
        self.assertNotIn('ANTHROPIC_API_KEY', options['env'])
        self.assertNotIn(PRIVATE, json.dumps(command) + json.dumps(options['env']))
        self.assertFalse(staging.exists())
        self.assertTrue(self.database.is_file())
        view = self.view(bundle)
        self.assert_reconciled(view, TOTAL)
        self.assertEqual({row['date']: self.vector(row) for row in view['daily']}, DAY_TOTALS)
        self.assertEqual({row['id']: self.vector(row) for row in view['owners']}, OWNER_TOTALS)
        self.assertEqual({row['id']: self.vector(row) for row in view['projects']}, PROJECT_TOTALS)
        self.assertEqual({row['id']: self.vector(row) for row in view['models']},
                         {'fixture-model-a': PROJECT_TOTALS[A], 'fixture-model-b': PROJECT_TOTALS[B],
                          'fixture-model-c': PROJECT_TOTALS[C]})
        self.assert_reconciled(self.view(bundle, chat=ROOT), (161, 76, 51, 14, 212))
        self.assert_reconciled(self.view(bundle, chat=FORK), OWNER_TOTALS[FORK])
        self.assert_reconciled(self.view(bundle, chat=ROOT, project=A), DAY_TOTALS['2026-10-24'])
        self.assert_reconciled(self.view(bundle, chat=ROOT, project=B), PROJECT_TOTALS[B])
        nested = next(row for row in view['owners'] if row['id'] == NESTED)
        self.assertEqual((nested['parentId'], nested['chatId']), (CHILD, ROOT))
        fork = next(row for row in view['owners'] if row['id'] == FORK)
        self.assertEqual((fork['parentId'], fork['chatId'], fork['kind']), (None, FORK, 'self'))
        self.assertNotIn('Stale fork nickname', json.dumps(self.view(bundle, chat=FORK)))
        for start, end in [('2026-10-24', '2026-10-26'), ('2026-10-25', '2026-10-25'),
                           ('2026-10-26', '2026-10-26')]:
            for scope in ({}, {'chat': ROOT}, {'chat': FORK}, {'project': A}, {'project': B},
                          {'project': C}):
                with self.subTest(start=start, end=end, scope=scope):
                    actual = ledger.project_bundle(bundle, start, end, now=NOW, **scope)
                    expected, _ = core.load_cache(self.database, start, end, names_databases=(), now=NOW, **scope)
                    for name in ('totals', 'daily', 'trend', 'features', 'scope'):
                        self.assertEqual(actual[name], expected[name], name)
                    for name in ('owners', 'chats', 'models', 'projects'):
                        left, right = copy.deepcopy(actual[name]), copy.deepcopy(expected[name])
                        for rows in (left, right):
                            for row in rows:
                                if 'owners' in row:
                                    row['owners'].sort()
                        self.assertEqual(sorted(left, key=lambda row: row['id']),
                                         sorted(right, key=lambda row: row['id']), name)
        # No prompt, title, or private source text reaches SQLite
        # or any generated ledger/report, including a clean database's raw pages.
        self.assertNotIn(PRIVATE.encode(), self.database.read_bytes())
        bundle, proof, _ = ledger.build_bundle(self.database, names_databases=(),
                                              cache_file=self.root / 'usage-ledger.json', now=NOW)
        refresh.write_outputs(self.root / 'dashboard', bundle, proof)
        for path in [self.root / 'usage-ledger.json',
                     *(self.root / 'dashboard' / name for name in ('index.html', 'data.json', 'evidence.json', 'summary.txt'))]:
            self.assertNotIn(PRIVATE, path.read_text(encoding='utf-8'), str(path))

    def test_incremental_rescan_append_and_active_to_archive_move(self):
        self.assert_reconciled(self.view(self.scan()), TOTAL)
        with self.prepare() as prepared:
            self.assertEqual(prepared.details['filesParsed'], 0)
            prepared.publish()
        appended = event('2026-10-26', last=(3, 1, 2, 1, 5))
        with self.paths['child'].open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(appended) + '\n')
        self.assert_reconciled(self.view(self.scan()), (223, 101, 64, 18, 287))
        destination = self.home / 'archived_sessions' / self.paths['child'].name
        self.paths['child'].replace(destination)
        bundle = self.scan()
        self.assert_reconciled(self.view(bundle), (223, 101, 64, 18, 287))
        self.assertEqual(len(bundle['owners']), 6)

    def test_failed_scan_validation_and_publication_leave_owned_database_intact(self):
        self.scan()
        original = self.database.read_bytes()
        # A genuine decoder failure occurs in the native subprocess after staging.
        broken = self.home / 'sessions/invalid.jsonl.zst'
        broken.write_bytes(b'not a Zstandard frame')
        with self.assertRaises((ValueError, OSError)):
            with self.prepare():
                self.fail('A corrupt compressed rollout was accepted')
        self.assertEqual(self.database.read_bytes(), original)
        broken.unlink()
        with self.assertRaisesRegex(ValueError, 'review rejected'):
            with self.prepare() as prepared:
                staging = prepared.path
                raise ValueError('review rejected')
        self.assertFalse(staging.exists())
        self.assertEqual(self.database.read_bytes(), original)
        real_replace = offline.os.replace

        def fail_publish(source, destination):
            if Path(destination).resolve() == self.database.resolve():
                raise OSError('synthetic atomic publication failure')
            return real_replace(source, destination)

        with patch.object(offline.os, 'replace', side_effect=fail_publish):
            with self.assertRaisesRegex(OSError, 'atomic publication'):
                with self.prepare() as prepared:
                    staging = prepared.path
                    prepared.publish()
        self.assertFalse(staging.exists())
        self.assertEqual(self.database.read_bytes(), original)
        self.assert_reconciled(self.view(self.scan()), TOTAL)

    def test_exclusive_scan_and_new_home_do_not_mix_owners(self):
        self.scan()
        with self.prepare() as prepared:
            with self.assertRaises((ValueError, OSError)):
                with self.prepare():
                    self.fail('Concurrent writer accepted the same database')
            prepared.publish()
        new_home = self.root / 'other synthetic home'
        write_session(new_home, ARCHIVED, session(ARCHIVED, C, [context('2026-10-26', C, 'fixture-model-c'),
                                                             event('2026-10-26', last=(5, 2, 3, 1, 8))]))
        with offline.prepare_database(PACKAGE, self.database, new_home, timezone='UTC',
                                      runtime_dir=self.runtime, engine=self.engine) as prepared:
            bundle, _, _ = ledger.build_bundle(prepared.path, names_databases=(), cache_file=None, now=NOW)
            self.assert_reconciled(self.view(bundle), (5, 2, 3, 1, 8))
            self.assertEqual([row['id'] for row in bundle['owners']], [ARCHIVED])
            prepared.publish()

    def test_timezone_change_rebuilds_day_buckets(self):
        home = self.root / 'boundary synthetic home'
        write_session(home, ROOT, session(ROOT, A, [context('2026-10-24', A, 'fixture-model-a'),
                                                   event('2026-10-24', last=(7, 3, 2, 1, 9), clock='23:30:00')]))
        for zone, expected_day in [('UTC', '2026-10-24'), ('Europe/Berlin', '2026-10-25')]:
            with offline.prepare_database(PACKAGE, self.database, home, timezone=zone,
                                          runtime_dir=self.runtime, engine=self.engine) as prepared:
                bundle, _, _ = ledger.build_bundle(prepared.path, names_databases=(), cache_file=None, now=NOW)
                self.assertEqual(bundle['coverage']['recordedDays'], [expected_day])
                self.assertEqual(self.vector(bundle['daily'][0]), (7, 3, 2, 1, 9))
                prepared.publish()

    def test_recorded_total_counter_is_preserved_when_it_differs_from_input_plus_output(self):
        home = self.root / 'independent counters synthetic home'
        write_session(home, ROOT, session(ROOT, A, [context('2026-10-24', A, 'fixture-model-a'),
                                                   event('2026-10-24', last=(8, 3, 7, 4, 99))]))
        with offline.prepare_database(PACKAGE, self.database, home, timezone='UTC',
                                      runtime_dir=self.runtime, engine=self.engine) as prepared:
            bundle, _, _ = ledger.build_bundle(prepared.path, names_databases=(), cache_file=None, now=NOW)
            self.assert_reconciled(self.view(bundle), (8, 3, 7, 4, 99))
            self.assertTrue(any('input plus output' in warning for warning in bundle['diagnostics']['warnings']))
            prepared.publish()

    def test_main_saved_offline_refresh_and_failure_preserve_report_and_settings(self):
        config, out = self.root / 'settings.json', self.root / 'dashboard'
        project_state = self.root / 'synthetic-project-state.json'
        project_state.write_text('{}', encoding='utf-8')
        command = ['--config', str(config), '--codex-home', str(self.home),
                   '--timezone', 'UTC', '--engine', str(self.engine), '--runtime-dir', str(self.runtime),
                   '--project-state', str(project_state), '--out', str(out), '--no-open']
        for arguments in (command, ['--config', str(config), '--no-open'],
                          ['--config', str(config), '--offline', '--no-open']):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(core, 'discover_names_database', side_effect=AssertionError('No real-user discovery')), \
                 patch.object(refresh.webbrowser, 'open', side_effect=AssertionError('No browser requested')), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = refresh.main(arguments)
            self.assertIn(status, (0, 2), stderr.getvalue())
            result = json.loads(stdout.getvalue())
            self.assertEqual(result['modelCalls'], 0)
            self.assertFalse(result['networkUsed'])
        saved = json.loads(config.read_text(encoding='utf-8'))
        self.assertEqual(saved['schemaVersion'], 2)
        self.assertEqual(saved['offline']['codexHome'], str(self.home.resolve()))
        self.assertEqual(saved['offline']['timezone'], 'UTC')
        source = Path(saved['database'])
        protected = [config, source, out / 'index.html', out / 'data.json', out / 'evidence.json',
                     out / 'summary.txt', out / 'usage-ledger.json']
        before = {path: path.read_bytes() for path in protected}
        # A failed accounting gate and a failed final rename must preserve the
        # entire previous report/settings transaction, even after a real scan.
        real_replace = offline.os.replace

        def fail_publish(source_file, destination):
            if Path(destination).resolve() == source.resolve():
                raise OSError('synthetic main publication failure')
            return real_replace(source_file, destination)

        failure_gates = [
            patch.object(ledger, 'build_bundle', side_effect=core.ReportError('synthetic accounting mismatch')),
            patch.object(offline.os, 'replace', side_effect=fail_publish),
        ]
        for failure_gate in failure_gates:
            stdout, stderr = io.StringIO(), io.StringIO()
            with failure_gate, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(refresh.main(['--config', str(config), '--no-open']), 1)
            self.assertFalse(json.loads(stderr.getvalue())['networkUsed'])
            for path, payload in before.items():
                self.assertEqual(path.read_bytes(), payload, str(path))
        (self.home / 'sessions/invalid.jsonl.zst').write_bytes(b'broken compression')
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(refresh.main(['--config', str(config), '--no-open']), 1)
        self.assertFalse(json.loads(stderr.getvalue())['networkUsed'])
        for path, payload in before.items():
            self.assertEqual(path.read_bytes(), payload, str(path))
        for path in (source, out / 'index.html', out / 'data.json', out / 'usage-ledger.json'):
            self.assertNotIn(PRIVATE.encode(), path.read_bytes(), str(path))


if __name__ == '__main__':
    unittest.main()
