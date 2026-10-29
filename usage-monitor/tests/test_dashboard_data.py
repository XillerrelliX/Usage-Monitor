"""Fixture-only ledger tests, with fixed arithmetic and original core oracles."""
import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import dashboard_data as ledger
import report_from_app as core
from test_scopes import fixture, ROOT, AGENT, NESTED, FORK, OTHER, A, B, C, NOW, encoded, vector


class LedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database, self.names = fixture(self.root)
        self.cache_file = self.root/'usage-ledger.json'

    def build(self, **kwargs):
        return ledger.build_bundle(self.database, [self.names], cache_file=self.cache_file, now=NOW, **kwargs)

    def view(self, bundle, **kwargs):
        return ledger.project_bundle(bundle, '2026-10-24', '2026-10-26', now=NOW, **kwargs)

    def mutate(self, thread, mutation):
        path = 'C:/fixture/rollout-'+thread+'.jsonl'
        with closing(sqlite3.connect(self.database)) as db, db:
            payload = json.loads(db.execute('SELECT rows_json FROM session_file_rollups WHERE path=?', (path,)).fetchone()[0])
            mutation(payload)
            db.execute('UPDATE session_file_rollups SET rows_json=? WHERE path=?', (json.dumps(payload), path))

    def zero_day(self, day):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('INSERT INTO daily_usage_rollups VALUES(?,?,?,?,?,?,?,?)', (day, 0, 0, 0, 0, 0, '{}', '{}'))

    def assert_numeric_equal(self, actual, expected):
        for name in ('totals', 'daily', 'trend', 'features'):
            self.assertEqual(actual[name], expected[name], name)
        for name in ('owners', 'chats', 'models', 'projects'):
            left, right = copy.deepcopy(actual[name]), copy.deepcopy(expected[name])
            for rows in (left, right):
                for row in rows:
                    if 'owners' in row:
                        row['owners'].sort()
            self.assertEqual(sorted(left, key=lambda row: row['id']), sorted(right, key=lambda row: row['id']), name)

    def test_fixed_handcomputed_all_chat_project_and_intersection_vectors(self):
        bundle, proof, _ = self.build()
        cases = [({}, (259, 160, 40, 14, 299)),
                 ({'chat': ROOT}, (169, 85, 35, 12, 204)),
                 ({'project': A}, (215, 151, 22, 6, 237)),
                 ({'project': A, 'chat': ROOT}, (144, 82, 20, 6, 164)),
                 ({'project': B, 'chat': ROOT}, (25, 3, 15, 6, 40)),
                 ({'project': B, 'chat': OTHER}, (0, 0, 0, 0, 0))]
        for selection, expected in cases:
            with self.subTest(selection=selection):
                view = self.view(bundle, **selection)
                self.assertEqual(tuple(view['totals'][field] for field in core.FIELDS), expected)
                for name in ('daily', 'owners', 'chats', 'models', 'projects'):
                    self.assertEqual(tuple(core.sum_counters(view[name])[field] for field in core.FIELDS), expected, name)
        self.assertTrue(proof['mappingOwnerIdsMatch'])

    def test_original_core_all_chat_and_project_oracles_for_every_subrange(self):
        bundle, _, _ = self.build()
        for start, end in [('2026-10-23', '2026-10-26'), ('2026-10-24', '2026-10-26'),
                           ('2026-10-25', '2026-10-25'), ('2026-10-22', '2026-10-28')]:
            for scope in ({}, {'chat': ROOT}, {'chat': FORK}, {'project': A}, {'project': B}, {'project': C}):
                with self.subTest(start=start, end=end, scope=scope):
                    expected, _ = core.load_cache(self.database, start, end, names_databases=[self.names], now=NOW, **scope)
                    actual = ledger.project_bundle(bundle, start, end, now=NOW, **scope)
                    self.assert_numeric_equal(actual, expected)
                    self.assertEqual(actual['scope'], expected['scope'])

    def test_exact_duplicate_basenames_and_unequal_multifolder_parts(self):
        bundle, _, _ = self.build()
        self.assertNotEqual(self.view(bundle, project=A)['totals'], self.view(bundle, project=B)['totals'])
        self.assertEqual(self.view(bundle, project='shared')['totals']['totalTokens'], 0)
        self.assertEqual(self.view(bundle, project=A.lower())['totals']['totalTokens'], 0)
        self.assertEqual({row['id'] for row in bundle['appProjects']}, {A, B, C})
        self.assertEqual(self.view(bundle, project=A, chat=ROOT)['chats'][0]['label'], 'Chosen chat')

    def test_nested_agents_forks_inactive_parent_and_uuid_normalization(self):
        bundle, _, _ = self.build()
        view = ledger.project_bundle(bundle, '2026-10-26', '2026-10-26', chat=ROOT.upper(), now=NOW)
        self.assertEqual([row['id'] for row in view['owners']], [NESTED])
        self.assertEqual(view['owners'][0]['parentId'], AGENT)
        self.assertEqual(view['owners'][0]['chatId'], ROOT)
        self.assertEqual(view['scope']['label'], 'Chosen chat')
        fork = self.view(bundle, chat=FORK)
        self.assertEqual(fork['totals']['totalTokens'], 22)
        self.assertEqual(fork['owners'][0]['kind'], 'self')
        self.assertIsNone(fork['owners'][0]['parentId'])
        self.assertNotIn('Stale copied', json.dumps(bundle))

    def test_multiselect_unions_deduplicate_records_owners_and_verified_agents(self):
        bundle, _, _ = self.build()
        view = self.view(bundle, projects=[B, A, A], chats=[OTHER, ROOT.upper(), ROOT])
        self.assertEqual(tuple(view['totals'][field] for field in core.FIELDS), (240, 154, 37, 12, 277))
        self.assertEqual(view['selection'], {'projects': sorted([A, B]), 'chats': sorted([ROOT, OTHER])})
        self.assertEqual({row['id'] for row in view['owners']}, {ROOT, AGENT, NESTED, OTHER})
        self.assertEqual(view['totals']['sessions'], 4)
        for kind in ('daily', 'owners', 'chats', 'models', 'projects'):
            self.assertEqual(core.sum_counters(view[kind]), {field: view['totals'][field] for field in core.FIELDS})
        self.assertEqual(view['comparison']['kind'], 'chats')
        self.assertEqual([(row['id'], row['totalTokens']) for row in view['comparison']['series']], [(ROOT, 204), (OTHER, 73)])
        self.assertEqual(core.sum_counters(view['comparison']['series']), {field: view['totals'][field] for field in core.FIELDS})

    def test_multiselect_dimensions_intersect_and_comparison_preserves_empty_selection(self):
        bundle, _, _ = self.build()
        view = self.view(bundle, projects=[A, C], chats=[ROOT, FORK])
        self.assertEqual(tuple(view['totals'][field] for field in core.FIELDS), (163, 88, 23, 8, 186))
        self.assertEqual([(row['id'], row['totalTokens']) for row in view['comparison']['series']], [(ROOT, 164), (FORK, 22)])
        empty = self.view(bundle, projects=[B], chats=[OTHER, FORK])
        self.assertTrue(empty['scope']['empty'])
        self.assertEqual(empty['totals']['sessions'], 0)
        self.assertEqual([row['id'] for row in empty['comparison']['series']], [FORK, OTHER])
        for row in empty['comparison']['series']:
            self.assertEqual(row['daily'], [])
            self.assertEqual(tuple(row[field] for field in core.FIELDS), (0, 0, 0, 0, 0))

    def test_multiselect_project_comparison_sums_only_distinct_selected_slices(self):
        bundle, _, _ = self.build()
        view = self.view(bundle, projects=[B, A, B])
        self.assertEqual(view['totals']['totalTokens'], 277)
        self.assertEqual(view['comparison']['kind'], 'projects')
        self.assertEqual([(row['id'], row['totalTokens']) for row in view['comparison']['series']], [(A, 237), (B, 40)])
        self.assertEqual([row['totalTokens'] for row in view['comparison']['series'][0]['daily']], [164, 73])
        self.assertEqual(self.view(bundle, projects=[], chats=[])['totals'], self.view(bundle)['totals'])
        self.assertEqual(self.view(bundle)['comparison'], {'kind': None, 'series': []})
        self.assertEqual(self.view(bundle, projects=[A], chats=[ROOT])['totals'], self.view(bundle, project=A, chat=ROOT)['totals'])

    def test_multiselect_invalid_unknown_and_conflicting_ids_never_broaden(self):
        bundle, _, _ = self.build()
        for scope in ({'projects': A}, {'chats': ROOT}, {'projects': [None]}, {'chats': [False]},
                      {'projects': ['']}, {'chats': [' ']}, {'projects': [A, 'unknown']},
                      {'chats': [ROOT, 'unknown']}, {'projects': [A.lower()]},
                      {'project': A, 'projects': []}, {'chat': ROOT, 'chats': [OTHER]}):
            with self.subTest(scope=scope), self.assertRaises(core.ReportError):
                self.view(bundle, **scope)
        exact = self.view(bundle, project=A, projects=[A, A], chat=ROOT.upper(), chats=[ROOT])
        self.assertEqual(exact['totals']['totalTokens'], 164)
        self.assertEqual(self.view(bundle, project='unknown')['totals']['totalTokens'], 0)

    def test_multiselect_comparison_sparse_recorded_zero_and_date_boundaries(self):
        bundle, _, _ = self.build()
        zero = copy.deepcopy(bundle['records'][0])
        zero.update(key='synthetic-zero', ownerId=ROOT, date='2026-10-22', **vector(0, 0, 0, 0))
        zero.update(projects={A: vector(0, 0, 0, 0)}, models={'zero': vector(0, 0, 0, 0)},
                    projectModels={A: {'zero': vector(0, 0, 0, 0)}})
        bundle['records'].append(zero)
        view = ledger.project_bundle(bundle, '2026-10-21', '2026-10-24', projects=[A], chats=[ROOT], now=NOW)
        self.assertEqual([(row['date'], row['totalTokens']) for row in view['comparison']['series'][0]['daily']],
                         [('2026-10-22', 0), ('2026-10-23', 252), ('2026-10-24', 164)])
        self.assertFalse(view['trend']['days'][0]['recorded'])
        self.assertIsNone(view['trend']['days'][0]['totalTokens'])
        self.assertTrue(view['trend']['days'][1]['recorded'])
        self.assertEqual(view['trend']['days'][1]['totalTokens'], 0)
        narrow = ledger.project_bundle(bundle, '2026-10-23', '2026-10-23', projects=[A], chats=[ROOT], now=NOW)
        self.assertEqual(narrow['comparison']['series'][0]['totalTokens'], 252)

    def test_multiselect_duplicate_record_is_rejected_before_counting(self):
        bundle, _, _ = self.build()
        bundle['records'].append(copy.deepcopy(bundle['records'][0]))
        with self.assertRaisesRegex(core.ReportError, 'duplicate identity'):
            self.view(bundle, projects=[A, B], chats=[ROOT, OTHER])

    def test_multiselect_missing_nested_model_part_cannot_become_empty_verified_ranking(self):
        bundle, _, _ = self.build()
        row = next(row for row in bundle['records'] if row['date'] == '2026-10-24' and row['ownerId'] == ROOT)
        row['projectModels'].pop(A)
        view = self.view(bundle, projects=[A, B], chats=[ROOT])
        self.assertEqual(view['totals']['totalTokens'], 204)
        self.assertFalse(view['features']['models'])
        self.assertEqual(view['models'], [])
        self.assertEqual(view['comparison']['series'][0]['totalTokens'], 204)

    def test_multiselect_large_integer_counters_and_overflow(self):
        bundle, _, _ = self.build()
        huge = 9007199254740000
        row = copy.deepcopy(bundle['records'][0])
        row.update(key='large-one', ownerId=ROOT, date='2026-10-24', **vector(huge, 4, 0, 0))
        row.update(projects={A: vector(huge, 4, 0, 0), B: vector(0, 0, 0, 0)},
                   models={'large': vector(huge, 4, 0, 0)},
                   projectModels={A: {'large': vector(huge, 4, 0, 0)}, B: {'large': vector(0, 0, 0, 0)}})
        bundle['records'] = [row]
        view = self.view(bundle, projects=[A, B, A], chats=[ROOT, ROOT])
        self.assertEqual(view['totals']['totalTokens'], huge)
        self.assertEqual(view['comparison']['series'][0]['inputTokens'], huge)
        overflow = copy.deepcopy(row)
        overflow.update(key='large-two', ownerId=OTHER)
        bundle['records'].append(overflow)
        with self.assertRaisesRegex(core.ReportError, 'JavaScript integer'):
            self.view(bundle, projects=[A, B], chats=[ROOT, OTHER])

    def test_fallback_and_metadata_identity_follow_core_exactly(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            old = 'C:/fixture/rollout-'+OTHER+'.jsonl'
            db.execute('UPDATE session_file_rollups SET path=? WHERE path=?', ('C:/unidentified.jsonl', old))
            db.execute('DELETE FROM project_session_index WHERE path=?', (old,))
        bundle, _, _ = self.build()
        oracle, _ = core.load_cache(self.database, '2026-10-23', '2026-10-26', names_databases=[self.names], now=NOW)
        actual = ledger.project_bundle(bundle, '2026-10-23', '2026-10-26', now=NOW)
        self.assert_numeric_equal(actual, oracle)
        self.assertIn('session-5', {row['id'] for row in bundle['owners']})

    def test_imports_all_history_in_nonoverlapping_validated_chunks(self):
        self.zero_day('2024-01-01')
        bundle, proof, _ = self.build()
        self.assertEqual(bundle['coverage']['startDate'], '2024-01-01')
        self.assertEqual(bundle['coverage']['endDate'], '2026-10-26')
        chunks = proof['validatedChunks']
        self.assertGreater(len(chunks), 1)
        for left, right in zip(chunks, chunks[1:]):
            self.assertEqual((datetime.fromisoformat(right['startDate'])-datetime.fromisoformat(left['endDate'])).days, 1)
        self.assertEqual(core.sum_counters(bundle['daily'])['totalTokens'], 551)
        self.assertEqual(core.sum_counters(bundle['records'])['totalTokens'], 551)

    def test_sparse_missing_and_recorded_zero_stay_distinct(self):
        self.zero_day('2026-10-22')
        bundle, _, _ = self.build()
        view = ledger.project_bundle(bundle, '2026-10-21', '2026-10-23', now=NOW)
        missing, zero, used = view['trend']['days']
        self.assertFalse(missing['recorded'])
        self.assertIsNone(missing['totalTokens'])
        self.assertTrue(zero['recorded'])
        self.assertEqual(zero['totalTokens'], 0)
        self.assertTrue(used['recorded'])
        scoped = ledger.project_bundle(bundle, '2026-10-21', '2026-10-22', chat=ROOT, now=NOW)
        self.assertFalse(any(row['recorded'] for row in scoped['trend']['days']))

    def test_empty_cache_produces_portable_empty_ledger(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('DELETE FROM daily_usage_rollups')
            db.execute('DELETE FROM session_file_rollups')
            db.execute('DELETE FROM project_session_index')
        bundle, _, _ = self.build()
        self.assertEqual(bundle['coverage'], {'startDate': None, 'endDate': None, 'recordedDays': [], 'historyComplete': True})
        self.assertEqual(bundle['records'], [])
        self.assertEqual(self.view(bundle)['totals']['totalTokens'], 0)

    def test_missing_session_project_maps_disable_project_filter_only(self):
        self.mutate(OTHER, lambda rows: rows[0].pop('projects'))
        bundle, _, _ = self.build()
        self.assertFalse(bundle['features']['projectFilter'])
        self.assertTrue(bundle['features']['projects'])
        self.assertEqual(self.view(bundle)['totals']['totalTokens'], 299)
        with self.assertRaisesRegex(core.ReportError, 'Project scope is unavailable'):
            self.view(bundle, project=B)
        scoped = self.view(bundle, chat=ROOT)
        self.assertFalse(scoped['features']['projects'])
        self.assertEqual(scoped['projects'], [])

    def test_missing_nested_models_disable_only_project_model_breakdown(self):
        self.mutate(ROOT, lambda rows: rows[1]['projects'][A].pop('models'))
        bundle, _, _ = self.build()
        self.assertTrue(bundle['features']['projectFilter'])
        self.assertFalse(bundle['features']['projectModels'])
        scoped = self.view(bundle, project=A, chat=ROOT)
        self.assertEqual(scoped['totals']['totalTokens'], 164)
        self.assertFalse(scoped['features']['models'])
        self.assertEqual(scoped['models'], [])
        self.assertTrue(scoped['features']['projects'])

    def test_each_five_session_map_counter_must_reconcile(self):
        for field in core.JSON_FIELDS:
            with self.subTest(field=field):
                with closing(sqlite3.connect(self.database)) as db:
                    original = db.execute('SELECT rows_json FROM session_file_rollups WHERE path LIKE ?', ('%'+OTHER+'.jsonl',)).fetchone()[0]
                self.mutate(OTHER, lambda rows: rows[0]['projects'][A].update({field: rows[0]['projects'][A][field]+1}))
                bundle, _, _ = self.build(refresh=True)
                self.assertFalse(bundle['features']['projectFilter'])
                with closing(sqlite3.connect(self.database)) as db, db:
                    db.execute('UPDATE session_file_rollups SET rows_json=? WHERE path LIKE ?', (original, '%'+OTHER+'.jsonl'))

    def test_mismatched_independent_app_project_distribution_is_unavailable(self):
        self.mutate(OTHER, lambda rows: rows[0].update(projects={B: encoded(vector(71, 69, 2, 0))}))
        bundle, _, _ = self.build()
        self.assertFalse(bundle['features']['projectFilter'])
        self.assertIn('app daily group totals disagree', bundle['diagnostics']['attribution']['sessionProjects'])

    def test_corrupt_independent_daily_totals_are_rejected(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('UPDATE daily_usage_rollups SET total_tokens=total_tokens+1 WHERE date=?', ('2026-10-26',))
        with self.assertRaises(core.ReportError):
            self.build()
        self.assertFalse(self.cache_file.exists())

    def test_recorded_total_and_subset_anomalies_are_preserved(self):
        # Change the same five-dimensional value in every independent representation.
        self.mutate(OTHER, lambda rows: (rows[0].update(cachedInputTokens=100, totalTokens=90),
                    rows[0]['models']['model-a'].update(cachedInputTokens=100, totalTokens=90),
                    rows[0]['projects'][A].update(cachedInputTokens=100, totalTokens=90),
                    rows[0]['projects'][A]['models']['model-a'].update(cachedInputTokens=100, totalTokens=90)))
        with closing(sqlite3.connect(self.database)) as db, db:
            row = db.execute('SELECT models_json,projects_json FROM daily_usage_rollups WHERE date=?', ('2026-10-26',)).fetchone()
            models, projects = map(json.loads, row)
            models['model-a'].update(cachedInputTokens=100, totalTokens=90)
            projects[A].update(cachedInputTokens=100, totalTokens=90)
            db.execute('UPDATE daily_usage_rollups SET cached_input_tokens=100,total_tokens=95,models_json=?,projects_json=? WHERE date=?',
                       (json.dumps(models), json.dumps(projects), '2026-10-26'))
        bundle, _, _ = self.build()
        view = self.view(bundle, chat=OTHER)
        self.assertEqual(view['totals']['totalTokens'], 90)
        self.assertEqual(view['totals']['cachedInputTokens'], 100)
        self.assertIsNone(view['totals']['uncachedInputTokens'])
        self.assertIn('preserved without adjustment', ' '.join(view['diagnostics']['warnings']))

    def test_range_scope_and_timezone_validation(self):
        bundle, _, _ = self.build()
        for start, end in [('2026-1-01', '2026-10-26'), ('2026-10-27', '2026-10-26'),
                           ('2025-01-01', '2026-10-26'), (None, None)]:
            with self.subTest(start=start, end=end), self.assertRaises(core.ReportError):
                ledger.project_bundle(bundle, start, end, now=NOW)
        for scope in ({'project': ''}, {'chat': ' '}, {'chat': False}):
            with self.subTest(scope=scope), self.assertRaises(core.ReportError):
                self.view(bundle, **scope)
        with self.assertRaises(core.ReportError):
            ledger.project_bundle(bundle, '2026-10-24', '2026-10-26', now=datetime(2026, 10, 26))

    def test_366_day_view_allowed_and_367_rejected(self):
        bundle, _, _ = self.build()
        self.assertEqual(ledger.project_bundle(bundle, '2025-10-26', '2026-10-26', now=NOW)['trend']['dayCount'], 366)
        with self.assertRaises(core.ReportError):
            ledger.project_bundle(bundle, '2025-10-25', '2026-10-26', now=NOW)

    def test_unsafe_javascript_source_counter_rejected(self):
        self.mutate(OTHER, lambda rows: rows[0].update(inputTokens=9007199254740992))
        with self.assertRaisesRegex(core.ReportError, 'JavaScript integer'):
            self.build()

    def test_unsafe_javascript_accumulated_counter_rejected(self):
        with self.assertRaisesRegex(core.ReportError, 'JavaScript integer'):
            ledger._sum([vector(9007199254740991, 0, 0, 0), vector(1, 0, 0, 0)])

    def test_cache_hit_skips_core_and_all_numeric_loading(self):
        first, _, meta = self.build()
        with (patch.object(core, 'load_cache', side_effect=AssertionError('Numeric core must not run on a hit')),
              patch.object(ledger, '_read_numeric', side_effect=AssertionError('Numeric loader must not run on a hit'))):
            second, _, hit = self.build()
        self.assertEqual(hit['status'], 'hit')
        self.assertEqual(first['records'], second['records'])
        self.assertEqual(meta['numericComputedAt'], hit['numericComputedAt'])

    def test_sidebar_names_refresh_on_hit_with_relevant_ids_only(self):
        self.build()
        with closing(sqlite3.connect(self.names)) as db, db:
            db.execute('UPDATE threads SET name=? WHERE id=?', ('Renamed sidebar chat', ROOT))
            db.execute('INSERT INTO threads VALUES(?,?,?)', ('unrelated', 'Never read unrelated name', 'Private title'))
        with patch.object(core, 'load_cache', side_effect=AssertionError('No numeric reload')):
            bundle, _, meta = self.build()
        self.assertEqual(meta['status'], 'hit')
        self.assertEqual(self.view(bundle, chat=ROOT)['scope']['label'], 'Renamed sidebar chat')
        self.assertNotIn('Never read unrelated', json.dumps(bundle))

    def test_invalid_cache_preserved_and_recomputed(self):
        self.build()
        invalid = '{invalid bytes'
        self.cache_file.write_text(invalid, encoding='utf-8')
        bundle, _, meta = self.build()
        self.assertEqual(meta['status'], 'miss')
        self.assertEqual(meta['reason'], 'invalid_ledger')
        self.assertEqual(Path(meta['recoveryPath']).read_text(encoding='utf-8'), invalid)
        self.assertEqual(self.view(bundle)['totals']['totalTokens'], 299)

    def test_rehashed_invalid_mapping_cache_is_not_trusted(self):
        self.build()
        entry = json.loads(self.cache_file.read_text(encoding='utf-8'))
        entry['bundle']['records'][0]['ownerId'] = 'invented-owner'
        entry['sha256'] = core.digest({key: value for key, value in entry.items() if key != 'sha256'})
        self.cache_file.write_text(json.dumps(entry), encoding='utf-8')
        _, _, meta = self.build()
        self.assertEqual(meta['reason'], 'invalid_ledger')

    def test_force_refresh_and_changed_native_source_revalidate(self):
        self.build()
        with patch.object(core, 'load_cache', wraps=core.load_cache) as oracle:
            _, _, forced = self.build(refresh=True)
            self.assertGreater(oracle.call_count, 0)
        self.assertEqual(forced['reason'], 'refresh_requested')
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('UPDATE scan_runs SET scanned_at=?', ((NOW+timedelta(minutes=1)).isoformat(),))
        _, _, changed = self.build()
        self.assertEqual(changed['reason'], 'source_changed')

    def test_wal_commit_invalidates_revision_and_reloads(self):
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        self.build()
        connection.execute('UPDATE scan_runs SET scanned_at=?', ((NOW+timedelta(seconds=1)).isoformat(),))
        connection.commit()
        with patch.object(core, 'load_cache', wraps=core.load_cache) as oracle:
            _, _, meta = self.build()
            self.assertGreater(oracle.call_count, 0)
        self.assertEqual(meta['reason'], 'source_changed')

    def test_source_changes_during_load_retries_without_mixing_revisions(self):
        original = ledger._read_numeric
        count = []
        def changing(database, metadata, now):
            result = original(database, metadata, now)
            count.append(1)
            if len(count) == 1:
                with closing(sqlite3.connect(database)) as db, db:
                    db.execute('UPDATE scan_runs SET scanned_at=?', ((NOW+timedelta(seconds=1)).isoformat(),))
            return result
        with patch.object(ledger, '_read_numeric', side_effect=changing):
            bundle, proof, _ = self.build()
        self.assertEqual(len(count), 2)
        self.assertEqual(bundle['source']['revisionSha256'], core.digest(core.source_revision(self.database)))
        self.assertEqual(proof['sourceRevisionSha256'], bundle['source']['revisionSha256'])

    def test_repeated_concurrent_change_fails_bounded_without_saving(self):
        original = ledger._read_numeric
        count = []
        def changing(database, metadata, now):
            result = original(database, metadata, now)
            count.append(1)
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('UPDATE scan_runs SET scanned_at=?', ((NOW+timedelta(seconds=len(count))).isoformat(),))
            return result
        with patch.object(ledger, '_read_numeric', side_effect=changing), self.assertRaisesRegex(core.ReportError, 'kept changing'):
            self.build()
        self.assertEqual(len(count), 3)
        self.assertFalse(self.cache_file.exists())

    def test_concurrent_ledger_writer_does_not_replace_existing_output(self):
        self.build()
        original = self.cache_file.read_bytes()
        lock = Path(str(self.cache_file)+'.lock')
        lock.write_text('another writer', encoding='utf-8')
        _, _, meta = self.build(refresh=True)
        self.assertFalse(meta['historySaved'])
        self.assertIn('could not be saved', meta['warning'])
        self.assertEqual(self.cache_file.read_bytes(), original)
        self.assertTrue(lock.exists())

    def test_cache_path_cannot_overwrite_source_or_names_or_wal(self):
        for path in (self.database, self.names, Path(str(self.database)+'-wal')):
            with self.subTest(path=path), self.assertRaisesRegex(core.ReportError, 'separate'):
                ledger.build_bundle(self.database, [self.names], cache_file=path, now=NOW)

    def test_old_overlapping_report_history_is_never_read_or_summed(self):
        old = self.root/'report-history.json'
        old.write_text(json.dumps({'entries': [{'totalTokens': 99999999}, {'totalTokens': 99999999}]}), encoding='utf-8')
        original = old.read_bytes()
        bundle, _, _ = self.build()
        self.assertEqual(self.view(bundle)['totals']['totalTokens'], 299)
        self.assertEqual(old.read_bytes(), original)

    def test_no_private_source_fields_or_raw_source_paths_exported(self):
        bundle, proof, _ = self.build()
        text = json.dumps(bundle)
        for private in ('Private', 'Never export', 'Stale copied nickname', 'rollout-', 'C:/fixture',
                        'prompt_title', 'quota_usage_json', 'threadName', 'search_text', 'title'):
            self.assertNotIn(private, text)
        self.assertTrue(proof['allAnalysisLocal'])
        self.assertFalse(proof['networkUsed'])

    def test_unknown_python_timezone_reports_current_day_status_unknown(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('UPDATE scan_runs SET timezone=?', ('Missing/IANA-Zone',))
        bundle, _, _ = self.build()
        view = self.view(bundle)
        self.assertIsNone(view['window']['currentAppDate'])
        self.assertIsNone(view['window']['includesCurrentAppDay'])
        self.assertIn('timezone cannot be resolved', ' '.join(view['diagnostics']['warnings']))


if __name__ == '__main__':
    unittest.main()
