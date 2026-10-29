"""Verified local report reuse, source revisions, freshness and safe fallbacks."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import report_from_app as adapter
from test_scopes import A, B, NOW, ROOT, fixture


class PersistentCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database, self.names = fixture(self.root)
        self.history = self.root/'history'/'usage-history.json'

    def report(self, **kwargs):
        options = dict(now=NOW, days=3, names_databases=[self.names], history_file=self.history)
        options.update(kwargs)
        return adapter.load_report(self.database, **options)

    def history_data(self):
        return json.loads(self.history.read_text(encoding='utf-8'))

    def save_history(self, data):
        self.history.write_text(json.dumps(data), encoding='utf-8')

    def resign(self, entry):
        entry['sha256'] = adapter.digest({key: value for key, value in entry.items() if key != 'sha256'})

    def double_fixture_counters(self, connection=None):
        """A real committed import replacing every independent counter consistently."""
        def alter(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key in adapter.JSON_FIELDS:
                        value[key] = nested*2
                    else:
                        alter(nested)
            elif isinstance(value, list):
                for nested in value:
                    alter(nested)
        manager = closing(sqlite3.connect(self.database)) if connection is None else contextlib.nullcontext(connection)
        with manager as db, db:
            db.execute('UPDATE daily_usage_rollups SET '+', '.join(field+'='+field+'*2' for field in adapter.DB_FIELDS))
            for day, models, projects in db.execute('SELECT date, models_json, projects_json FROM daily_usage_rollups').fetchall():
                maps = [json.loads(models), json.loads(projects)]
                for mapping in maps:
                    alter(mapping)
                db.execute('UPDATE daily_usage_rollups SET models_json=?, projects_json=? WHERE date=?',
                           (json.dumps(maps[0]), json.dumps(maps[1]), day))
            for path, payload in db.execute('SELECT path, rows_json FROM session_file_rollups').fetchall():
                rows = json.loads(payload)
                alter(rows)
                db.execute('UPDATE session_file_rollups SET rows_json=? WHERE path=?', (json.dumps(rows), path))

    def assert_snapshot_matches_current_source(self, data):
        entry = self.history_data()['entries'][-1]
        self.assertEqual(entry['selector']['source'], adapter.source_revision(self.database))
        self.assertEqual(entry['data']['totals'], data['totals'])
        direct, _ = adapter.load_cache(self.database, '2026-10-24', '2026-10-26', [self.names], now=NOW)
        self.assertEqual(data['totals'], direct['totals'])

    def test_unchanged_hit_skips_all_source_accounting_and_preserves_original_snapshot(self):
        first, first_proof = self.report()
        original_bytes = self.history.read_bytes()
        with patch.object(adapter, 'load_cache', side_effect=AssertionError('full load on hit')), \
                patch.object(adapter, 'load_rankings', side_effect=AssertionError('source ranking on hit')), \
                patch.object(adapter, 'checked_maps', side_effect=AssertionError('source reconciliation on hit')):
            second, proof = self.report(now=NOW+timedelta(minutes=5))
        self.assertEqual(first['cache']['status'], 'miss')
        self.assertEqual(second['cache']['status'], 'hit')
        self.assertEqual(first['totals'], second['totals'])
        self.assertEqual(first['owners'], second['owners'])
        self.assertEqual(proof['cache']['numericComputedAt'], NOW.isoformat())
        self.assertEqual(proof['generatedAt'], (NOW+timedelta(minutes=5)).isoformat())
        self.assertEqual(self.history.read_bytes(), original_bytes)
        entry = self.history_data()['entries'][0]
        self.assertEqual(entry['data']['generatedAt'], NOW.isoformat())
        self.assertEqual(entry['proof']['generatedAt'], NOW.isoformat())
        self.assertNotIn('cache', entry['data'])
        self.assertEqual(proof['cache']['verifiedProofSha256'], adapter.digest(entry['proof']))

    def test_freshness_and_today_are_recomputed_on_hit_without_relabeling_old_day(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE scan_runs SET timezone='UTC'")
        first, _ = self.report()
        self.assertTrue(first['window']['includesCurrentAppDay'])
        self.assertTrue(any('partial' in warning for warning in first['diagnostics']['warnings']))
        with patch.object(adapter, 'load_cache', side_effect=AssertionError('unneeded rescan')):
            older, _ = self.report(now=NOW+timedelta(days=1))
            near_scan, _ = self.report(now=NOW+timedelta(minutes=1))
        self.assertEqual(older['window']['currentAppDate'], '2026-10-27')
        self.assertFalse(older['window']['includesCurrentAppDay'])
        self.assertFalse(any('partial' in warning for warning in older['diagnostics']['warnings']))
        self.assertEqual(older['source']['ageSeconds'], 86400)
        self.assertTrue(any('1,440 minutes old' in warning for warning in older['diagnostics']['warnings']))
        self.assertFalse(any('minutes old' in warning for warning in near_scan['diagnostics']['warnings']))
        self.assertEqual(older['window']['endDate'], '2026-10-26')

    def test_unknown_app_timezone_does_not_invent_today(self):
        with patch.object(adapter, 'ZoneInfo', side_effect=adapter.ZoneInfoNotFoundError('no tzdata')):
            data, _ = self.report()
            cached, _ = self.report(now=NOW+timedelta(days=1))
        for value in (data, cached):
            self.assertIsNone(value['window']['currentAppDate'])
            self.assertIsNone(value['window']['includesCurrentAppDay'])
            self.assertTrue(any('may still be partial' in warning for warning in value['diagnostics']['warnings']))
        self.assertEqual(cached['cache']['status'], 'hit')

    def test_historical_and_current_ranges_both_reuse_unchanged_source(self):
        historical = dict(start_date='2026-10-23', end_date='2026-10-24')
        self.report(**historical)
        self.report()
        with patch.object(adapter, 'load_cache', side_effect=AssertionError('unneeded rescan')):
            old, _ = self.report(**historical)
            current, _ = self.report()
        self.assertEqual((old['cache']['status'], current['cache']['status']), ('hit', 'hit'))
        self.assertFalse(old['window']['includesCurrentAppDay'])

    def test_exact_scopes_dates_database_and_timezone_never_collide(self):
        variants = ({}, {'project': A}, {'project': B}, {'chat': ROOT},
                    {'start_date': '2026-10-24', 'end_date': '2026-10-24'})
        for options in variants:
            data, _ = self.report(**options)
            self.assertEqual(data['cache']['status'], 'miss')
        with patch.object(adapter, 'load_cache', side_effect=AssertionError('unneeded rescan')):
            for options in variants:
                self.assertEqual(self.report(**options)[0]['cache']['status'], 'hit')
        other = self.root/'copy.sqlite'
        shutil.copyfile(self.database, other)
        data, _ = adapter.load_report(other, days=3, now=NOW, names_databases=[self.names], history_file=self.history)
        self.assertEqual(data['cache']['status'], 'miss')
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE scan_runs SET timezone='UTC'")
        changed, _ = self.report()
        self.assertEqual(changed['cache']['status'], 'miss')
        self.assertEqual(changed['window']['timeZone'], 'UTC')
        self.assertEqual(len(self.history_data()['entries']), 7)

    def test_uuid_case_is_canonical_but_project_identity_is_exact(self):
        first, _ = self.report(chat=ROOT)
        with patch.object(adapter, 'load_cache', side_effect=AssertionError('unneeded rescan')):
            second, _ = self.report(chat=ROOT.upper())
        self.assertEqual(first['cache']['entryKey'], second['cache']['entryKey'])
        empty, _ = self.report(project=A.lower())
        self.assertTrue(empty['scope']['empty'])

    def test_native_refresh_invalidates_even_historical_snapshot_and_retains_older_entries(self):
        period = dict(start_date='2026-10-23', end_date='2026-10-24')
        first, _ = self.report(**period)
        old_entry = self.history_data()['entries'][0]
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('UPDATE scan_runs SET scanned_at=?', ((NOW+timedelta(minutes=2)).isoformat(),))
        with patch.object(adapter, 'load_cache', wraps=adapter.load_cache) as load:
            changed, _ = self.report(**period)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(changed['cache']['status'], 'miss')
        self.assertNotEqual(first['cache']['entryKey'], changed['cache']['entryKey'])
        self.assertEqual(self.history_data()['entries'][0], old_entry)
        self.assertEqual(len(self.history_data()['entries']), 2)

    def test_wal_only_commit_invalidates_with_unchanged_main_database(self):
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        first, _ = self.report()
        main_bytes = self.database.read_bytes()
        before = adapter.source_revision(self.database)
        writer.execute('UPDATE scan_runs SET scanned_at=?', ((NOW+timedelta(minutes=1)).isoformat(),))
        writer.commit()
        self.assertEqual(self.database.read_bytes(), main_bytes)
        self.assertNotEqual(adapter.source_revision(self.database)['wal'], before['wal'])
        with patch.object(adapter, 'load_cache', wraps=adapter.load_cache) as load:
            second, _ = self.report()
        self.assertEqual(load.call_count, 1)
        self.assertEqual(second['cache']['status'], 'miss')
        self.assertNotEqual(first['cache']['entryKey'], second['cache']['entryKey'])

    def test_wal_commit_during_hit_assembly_retries_to_consistent_new_counts(self):
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        first, _ = self.report()
        main_before = self.database.read_bytes()
        original_display = adapter.refresh_display
        changed = False
        def concurrent_import(*args):
            nonlocal changed
            if not changed:
                changed = True
                self.double_fixture_counters(writer)
            return original_display(*args)
        with patch.object(adapter, 'refresh_display', side_effect=concurrent_import):
            data, _ = self.report()
        self.assertEqual(self.database.read_bytes(), main_before)
        self.assertEqual(data['cache']['status'], 'miss')
        self.assertEqual(data['totals']['totalTokens'], first['totals']['totalTokens']*2)
        self.assertEqual(len(self.history_data()['entries']), 2)
        self.assertEqual(self.history_data()['entries'][0]['data']['totals'], first['totals'])
        self.assert_snapshot_matches_current_source(data)

    def test_commit_during_full_load_never_stores_old_counts_under_new_revision(self):
        original_load = adapter.load_cache
        original_totals = original_load(self.database, '2026-10-24', '2026-10-26', [self.names], now=NOW)[0]['totals']
        changed = False
        def concurrent_import(*args, **kwargs):
            nonlocal changed
            result = original_load(*args, **kwargs)
            if not changed:
                changed = True
                self.double_fixture_counters()
            return result
        with patch.object(adapter, 'load_cache', side_effect=concurrent_import):
            data, _ = self.report()
        self.assertEqual(data['totals']['totalTokens'], original_totals['totalTokens']*2)
        self.assertEqual(len(self.history_data()['entries']), 1)
        self.assert_snapshot_matches_current_source(data)

    def test_wal_refresh_between_metadata_probe_and_full_snapshot_retries_before_proof_validation(self):
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        original_load = adapter.load_cache
        original_totals = original_load(self.database, '2026-10-24', '2026-10-26', [self.names], now=NOW)[0]['totals']
        next_scan = (NOW+timedelta(minutes=1)).isoformat()
        changed = False
        def concurrent_refresh(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                self.double_fixture_counters(writer)
                writer.execute('UPDATE scan_runs SET scanned_at=?', (next_scan,))
                writer.commit()
            return original_load(*args, **kwargs)
        with patch.object(adapter, 'load_cache', side_effect=concurrent_refresh):
            data, proof = self.report()
        self.assertEqual(data['source']['lastScanAt'], next_scan)
        self.assertEqual(proof['lastScanAt'], next_scan)
        self.assertEqual(data['totals']['totalTokens'], original_totals['totalTokens']*2)
        self.assertEqual(len(self.history_data()['entries']), 1)
        self.assert_snapshot_matches_current_source(data)

    def test_continuous_commits_fail_boundedly_without_saving_an_inconsistent_snapshot(self):
        first, _ = self.report()
        original_history = self.history.read_bytes()
        original_display = adapter.refresh_display
        def concurrent_import(*args):
            self.double_fixture_counters()
            return original_display(*args)
        with patch.object(adapter, 'refresh_display', side_effect=concurrent_import):
            with self.assertRaisesRegex(adapter.ReportError, 'kept changing'):
                self.report()
        self.assertEqual(self.history.read_bytes(), original_history)
        direct, _ = adapter.load_cache(self.database, '2026-10-24', '2026-10-26', [self.names], now=NOW)
        self.assertGreater(direct['totals']['totalTokens'], first['totals']['totalTokens'])

    def test_corrupt_history_is_backed_up_exactly_then_rebuilt(self):
        self.report()
        original = b'broken JSON\xff\x00 historical bytes'
        self.history.write_bytes(original)
        data, _ = self.report()
        self.assertEqual((data['cache']['status'], data['cache']['reason']), ('miss', 'invalid_history'))
        recovery = Path(data['cache']['recoveryPath'])
        self.assertEqual(recovery.read_bytes(), original)
        self.assertIn('preserved', data['cache']['warning'])
        self.assertEqual(len(self.history_data()['entries']), 1)
        self.assertEqual(self.report()[0]['cache']['status'], 'hit')

    def test_duplicate_json_keys_and_deeply_nested_corruption_fall_back_safely(self):
        self.report()
        for original in (b'{"schemaVersion":1,"schemaVersion":1,"entries":[]}',
                         ('['*1200+']'*1200).encode('ascii')):
            with self.subTest(original_length=len(original)):
                self.history.write_bytes(original)
                data, _ = self.report()
                self.assertEqual(data['cache']['reason'], 'invalid_history')
                self.assertEqual(Path(data['cache']['recoveryPath']).read_bytes(), original)

    def test_corrupt_payload_or_proof_recomputes_without_deleting_bad_entry(self):
        self.report()
        for target in ('data', 'proof'):
            with self.subTest(target=target):
                history = self.history_data()
                entry = history['entries'][-1]
                if target == 'data':
                    entry['data']['totals']['totalTokens'] += 1
                else:
                    entry['proof']['appDailySessionTotalsMatch'] = False
                self.save_history(history)
                with patch.object(adapter, 'load_cache', wraps=adapter.load_cache) as load:
                    data, _ = self.report()
                self.assertEqual(load.call_count, 1)
                self.assertEqual(data['cache']['reason'], 'invalid_entry')
                self.assertEqual(self.history_data()['entries'][:-1], history['entries'])

    def test_valid_checksum_does_not_replace_required_proof_or_accounting_invariants(self):
        self.report()
        changes = (lambda entry: entry['proof'].update(appPerDaySessionTotalsMatch=False),
                   lambda entry: entry['proof'].update(scope={'kind': 'all', 'id': None}),
                   lambda entry: entry['data']['owners'][0].update(chatId='wrong-root'),
                   lambda entry: entry['data']['totals'].update(uncachedInputTokens=999),
                   lambda entry: entry['data']['trend']['days'][0].update(recorded=False))
        for change in changes:
            with self.subTest(change=change):
                history = self.history_data()
                change(history['entries'][-1])
                self.resign(history['entries'][-1])
                self.save_history(history)
                with patch.object(adapter, 'load_cache', wraps=adapter.load_cache) as load:
                    data, _ = self.report()
                self.assertEqual(load.call_count, 1)
                self.assertEqual(data['cache']['reason'], 'invalid_entry')

    def test_algorithm_change_invalidates_without_deleting_original_algorithm_entry(self):
        self.report()
        first = self.history_data()['entries'][0]
        with patch.object(adapter, 'REPORT_ALGORITHM_VERSION', 'next-algorithm'):
            with patch.object(adapter, 'load_cache', wraps=adapter.load_cache) as load:
                data, _ = self.report()
            self.assertEqual(load.call_count, 1)
            self.assertEqual(data['cache']['status'], 'miss')
            with patch.object(adapter, 'load_cache', side_effect=AssertionError('unneeded rescan')):
                self.assertEqual(self.report()[0]['cache']['status'], 'hit')
        self.assertEqual(self.history_data()['entries'][0], first)

    def test_names_change_and_unrelated_state_writes_relabel_hit_without_rescan(self):
        self.report(chat=ROOT)
        source_revision = adapter.source_revision(self.database)
        for query, values in [('UPDATE threads SET title=?', ('Changed private prompt',)),
                              ('UPDATE threads SET name=? WHERE id=?', ('Renamed sidebar', ROOT)),
                              ('DELETE FROM threads WHERE id=?', (ROOT,))]:
            with closing(sqlite3.connect(self.names)) as db, db:
                db.execute(query, values)
            with patch.object(adapter, 'load_cache', side_effect=AssertionError('numeric rescan for label')):
                data, proof = self.report(chat=ROOT)
            self.assertEqual(data['cache']['status'], 'hit')
            self.assertEqual(proof['scope'], data['scope'])
            self.assertEqual(adapter.source_revision(self.database), source_revision)
            self.assertNotIn('Changed private prompt', json.dumps(data))
            if query.startswith('UPDATE threads SET name'):
                self.assertEqual(data['chats'][0]['label'], 'Renamed sidebar')
                self.assertEqual(data['scope']['label'], 'Renamed sidebar')
            if query.startswith('DELETE'):
                self.assertEqual(data['chats'][0]['labelSource'], 'session_id')

    def test_new_state_database_rotation_is_seen_on_hit(self):
        shutil.copyfile(self.names, self.root/'state_9.sqlite')
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root)}):
            self.report(chat=ROOT, names_databases=None)
            next_names = self.root/'state_10.sqlite'
            shutil.copyfile(self.names, next_names)
            with closing(sqlite3.connect(next_names)) as db, db:
                db.execute('UPDATE threads SET name=? WHERE id=?', ('Newest sidebar', ROOT))
            with patch.object(adapter, 'load_cache', side_effect=AssertionError('numeric rescan for rotation')):
                data, _ = self.report(chat=ROOT, names_databases=None)
        self.assertEqual(data['cache']['status'], 'hit')
        self.assertEqual(data['scope']['label'], 'Newest sidebar')

    def test_refresh_forces_full_accounting_and_appends_a_snapshot(self):
        self.report()
        with patch.object(adapter, 'load_cache', wraps=adapter.load_cache) as load:
            data, _ = self.report(refresh=True)
        self.assertEqual(load.call_count, 1)
        self.assertEqual((data['cache']['status'], data['cache']['reason']), ('miss', 'refresh_requested'))
        self.assertEqual(len(self.history_data()['entries']), 2)

    def test_bad_entry_fallback_does_not_swallow_source_accounting_error(self):
        self.report()
        history = self.history_data()
        history['entries'][-1]['sha256'] = 'broken'
        self.save_history(history)
        before = self.history.read_bytes()
        with patch.object(adapter, 'load_cache', side_effect=adapter.ReportError('Counter mismatch')):
            with self.assertRaisesRegex(adapter.ReportError, 'Counter mismatch'):
                self.report()
        self.assertEqual(self.history.read_bytes(), before)

    def test_source_and_optional_names_bytes_are_unchanged_on_miss_hit_and_refresh(self):
        originals = {path: path.read_bytes() for path in (self.database, self.names)}
        self.report()
        self.report()
        self.report(refresh=True)
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)

    def test_recorded_subset_anomalies_are_reused_without_repair(self):
        def alter(value):
            if isinstance(value, dict):
                if 'inputTokens' in value:
                    value['cachedInputTokens'] = value['inputTokens']*2
                    value['reasoningOutputTokens'] = value['outputTokens']*2
                for nested in value.values():
                    alter(nested)
            elif isinstance(value, list):
                for nested in value:
                    alter(nested)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('UPDATE daily_usage_rollups SET cached_input_tokens=input_tokens*2, reasoning_output_tokens=output_tokens*2')
            for row in db.execute('SELECT date, models_json, projects_json FROM daily_usage_rollups').fetchall():
                maps = [json.loads(row[index]) for index in (1, 2)]
                for mapping in maps:
                    alter(mapping)
                db.execute('UPDATE daily_usage_rollups SET models_json=?, projects_json=? WHERE date=?',
                           (json.dumps(maps[0]), json.dumps(maps[1]), row[0]))
            for path, payload in db.execute('SELECT path, rows_json FROM session_file_rollups').fetchall():
                rows = json.loads(payload)
                alter(rows)
                db.execute('UPDATE session_file_rollups SET rows_json=? WHERE path=?', (json.dumps(rows), path))
        first, _ = self.report(project=A)
        with patch.object(adapter, 'load_cache', side_effect=AssertionError('unneeded rescan')):
            cached, _ = self.report(project=A)
        self.assertEqual(first['totals'], cached['totals'])
        self.assertIsNone(cached['totals']['uncachedInputTokens'])
        self.assertGreater(cached['totals']['cachedInputTokens'], cached['totals']['inputTokens'])
        self.assertGreater(cached['totals']['reasoningTokens'], cached['totals']['outputTokens'])
        self.assertTrue(any('subset exceeds' in warning for warning in cached['diagnostics']['warnings']))

    def test_history_cannot_overwrite_source_or_names_database(self):
        for path in (self.database, self.names, Path(str(self.database)+'-wal')):
            with self.subTest(path=path), self.assertRaisesRegex(adapter.ReportError, 'separate'):
                self.report(history_file=path)

    def test_current_renderer_is_used_on_cache_hit_and_cli_summary_is_compact(self):
        out = self.root/'report'
        arguments = ['--database', str(self.database), '--names-database', str(self.names),
                     '--days', '7', '--history-file', str(self.history), '--out', str(out)]
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertIn(adapter.main(arguments), (0, 2))
        first = json.loads(printed.getvalue())
        with patch.object(adapter, 'load_cache', side_effect=AssertionError('unneeded rescan')), \
                patch.object(adapter, 'write_report', wraps=adapter.write_report) as writer, \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertIn(adapter.main(arguments), (0, 2))
        second = json.loads(printed.getvalue())
        self.assertEqual(writer.call_count, 1)
        self.assertEqual((first['cache']['status'], second['cache']['status']), ('miss', 'hit'))
        for field in ('recordedTokens', 'chats', 'sessions', 'scope', 'startDate', 'endDate',
                      'timeZone', 'sourceLastScanAt', 'sourceAgeSeconds', 'warnings', 'cache'):
            self.assertIn(field, second)
        self.assertEqual(len(printed.getvalue().strip().splitlines()), 1)
        summary = (out/'summary.txt').read_text(encoding='utf-8')
        self.assertIn('Report result cache: hit', summary)
        proof = json.loads((out/'evidence.json').read_text(encoding='utf-8'))
        self.assertEqual(proof['cache'], second['cache'])
        self.assertEqual(proof['htmlSha256'], hashlib.sha256((out/'index.html').read_bytes()).hexdigest())
        self.assertEqual(proof['dataSha256'], hashlib.sha256((out/'data.json').read_bytes()).hexdigest())
        with patch.object(adapter, 'load_cache', wraps=adapter.load_cache) as load, contextlib.redirect_stdout(io.StringIO()):
            adapter.main([*arguments, '--refresh'])
        self.assertEqual(load.call_count, 1)

    def test_default_history_is_in_output_and_retains_no_private_source_fields(self):
        out = self.root/'default-output'
        with contextlib.redirect_stdout(io.StringIO()):
            adapter.main(['--database', str(self.database), '--names-database', str(self.names), '--out', str(out)])
        history = out/'usage-history.json'
        self.assertTrue(history.is_file())
        text = history.read_text(encoding='utf-8')
        for private in ('Private prompt title', 'Private account usage', 'Private search text',
                        'Never export a whole source row', 'Never export a whole project value'):
            self.assertNotIn(private, text)

    def test_writer_lock_preserves_existing_history_and_reports_unsaved_result(self):
        self.report()
        original = self.history.read_bytes()
        lock = Path(str(self.history)+'.lock')
        lock.write_text('another writer', encoding='utf-8')
        data, _ = self.report(refresh=True)
        self.assertFalse(data['cache']['historySaved'])
        self.assertIn('could not be saved', data['cache']['warning'])
        self.assertEqual(self.history.read_bytes(), original)
        self.assertEqual(lock.read_text(encoding='utf-8'), 'another writer')


if __name__ == '__main__':
    unittest.main()
