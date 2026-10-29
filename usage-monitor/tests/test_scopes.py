"""All report dimensions use exact scopes from explicit cached numeric maps."""
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import report_from_app as adapter

ROOT = '10000000-0000-4000-8000-000000000001'
AGENT = '20000000-0000-4000-8000-000000000002'
NESTED = '30000000-0000-4000-8000-000000000003'
FORK = '40000000-0000-4000-8000-000000000004'
OTHER = '50000000-0000-4000-8000-000000000005'
A, B, C = r'C:\one\shared', r'D:\two\shared', r'E:\separate'
NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def vector(i, c, o, r, total=None):
    return dict(zip(adapter.FIELDS, (i, c, o, r, i+o if total is None else total)))


def encoded(values):
    return dict(zip(adapter.JSON_FIELDS, (values[field] for field in adapter.FIELDS)))


PARTS = {
    'earlier': vector(199, 103, 53, 21),
    'a': vector(101, 71, 13, 5), 'b': vector(17, 2, 9, 3),
    'c': vector(5, 1, 4, 2), 'd': vector(43, 11, 7, 1),
    'e': vector(3, 0, 2, 1), 'f': vector(19, 6, 3, 2),
    'g': vector(71, 69, 2, 0),
}


def fixture(directory):
    cache, names = directory/'fixture.sqlite', directory/'names.sqlite'
    sources = {
        ROOT: [('2026-10-23', {A: PARTS['earlier']}),
               ('2026-10-24', {A: PARTS['a'], B: PARTS['b']}),
               ('2026-10-25', {B: PARTS['c']})],
        AGENT: [('2026-10-24', {A: PARTS['d']})],
        NESTED: [('2026-10-26', {B: PARTS['e']})],
        FORK: [('2026-10-24', {C: PARTS['f']})],
        OTHER: [('2026-10-26', {A: PARTS['g']})],
    }
    days, projects, models = defaultdict(list), defaultdict(lambda: defaultdict(list)), defaultdict(lambda: defaultdict(list))
    with closing(sqlite3.connect(cache)) as db, db:
        db.executescript('''
            CREATE TABLE scan_runs(id INTEGER PRIMARY KEY, scanned_at TEXT, timezone TEXT);
            CREATE TABLE daily_usage_rollups(date TEXT PRIMARY KEY, input_tokens INTEGER,
                cached_input_tokens INTEGER, output_tokens INTEGER, reasoning_output_tokens INTEGER,
                total_tokens INTEGER, models_json TEXT, projects_json TEXT);
            CREATE TABLE session_file_rollups(path TEXT PRIMARY KEY, rows_json TEXT,
                prompt_title TEXT, quota_usage_json TEXT);
            CREATE TABLE project_session_index(path TEXT PRIMARY KEY, detail_json TEXT, search_text TEXT);
        ''')
        db.execute('INSERT INTO scan_runs VALUES(1,?,?)', (NOW.isoformat(), 'Europe/Berlin'))
        for thread, rows in sources.items():
            payload = []
            for day, mapping in rows:
                values = adapter.sum_counters(mapping.values())
                day_models = defaultdict(list)
                project_map = {}
                for key, part in mapping.items():
                    model = {A: 'model-a', B: 'model-b', C: 'model-c'}[key]
                    project_map[key] = {**encoded(part), 'models': {model: encoded(part)},
                                        'private': 'Never export a whole project value'}
                    projects[day][key].append(part)
                    models[day][model].append(part)
                    day_models[model].append(part)
                payload.append({'date': day, **encoded(values), 'projects': project_map,
                                'models': {model: encoded(adapter.sum_counters(parts))
                                           for model, parts in day_models.items()},
                                'private': 'Never export a whole source row'})
                days[day].append(values)
            path = 'C:/fixture/rollout-'+thread+'.jsonl'
            parent = {AGENT: ROOT, NESTED: AGENT}.get(thread)
            metadata = {'agentSessionId': thread, 'parentSessionId': parent}
            if thread == FORK:
                metadata.update(agentSessionId=AGENT, parentSessionId=ROOT,
                                agentNickname='Stale copied nickname')
            db.execute('INSERT INTO session_file_rollups VALUES(?,?,?,?)',
                       (path, json.dumps(payload), 'Private prompt title', 'Private account usage'))
            db.execute('INSERT INTO project_session_index VALUES(?,?,?)',
                       (path, json.dumps(metadata), 'Private search text'))
        for day, parts in days.items():
            values = adapter.sum_counters(parts)
            db.execute('INSERT INTO daily_usage_rollups VALUES(?,?,?,?,?,?,?,?)',
                       (day, *(values[field] for field in adapter.FIELDS),
                        json.dumps({model: encoded(adapter.sum_counters(rows)) for model, rows in models[day].items()}),
                        json.dumps({key: encoded(adapter.sum_counters(rows)) for key, rows in projects[day].items()})))
    with closing(sqlite3.connect(names)) as db, db:
        db.execute('CREATE TABLE threads(id TEXT, name TEXT, title TEXT)')
        db.executemany('INSERT INTO threads VALUES(?,?,?)',
                       [(ROOT, 'Chosen chat', 'Private title'), (FORK, 'Separate fork', 'Private title'),
                        (OTHER, 'Unrelated chat', 'Private title')])
    return cache, names


class ScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache, self.names = fixture(self.root)

    def load(self, **kwargs):
        return adapter.load_cache(self.cache, names_databases=[self.names], now=NOW, days=3, **kwargs)

    def mutate_row(self, thread, change):
        path = 'C:/fixture/rollout-'+thread+'.jsonl'
        with closing(sqlite3.connect(self.cache)) as db, db:
            rows = json.loads(db.execute('SELECT rows_json FROM session_file_rollups WHERE path=?', (path,)).fetchone()[0])
            change(rows)
            db.execute('UPDATE session_file_rollups SET rows_json=? WHERE path=?', (json.dumps(rows), path))

    def assert_totals(self, data, *parts):
        expected = adapter.sum_counters([PARTS[part] for part in parts])
        self.assertEqual({field: data['totals'][field] for field in adapter.FIELDS}, expected)
        for rows in (data['daily'], data['chats'], data['owners']):
            self.assertEqual(adapter.sum_counters(rows), expected)
        for chat in data['chats']:
            self.assertEqual(adapter.sum_counters(chat['daily']), {field: chat[field] for field in adapter.FIELDS})
            self.assertEqual(adapter.sum_counters([owner for owner in data['owners'] if owner['chatId'] == chat['id']]),
                             {field: chat[field] for field in adapter.FIELDS})
        self.assertEqual(adapter.sum_counters([day for day in data['trend']['days'] if day['recorded']]), expected)
        for kind in ('models', 'projects'):
            if data['features'][kind]:
                self.assertEqual(adapter.sum_counters(data[kind]), expected)
                for row in data[kind]:
                    self.assertEqual(adapter.sum_counters(row['daily']), {field: row[field] for field in adapter.FIELDS})
                for day in data['daily']:
                    self.assertEqual(adapter.sum_counters([entry for row in data[kind] for entry in row['daily']
                                                           if entry['date'] == day['date']]),
                                     {field: day[field] for field in adapter.FIELDS})

    def test_chat_scope_includes_nested_agents_excludes_fork_and_other_everywhere(self):
        data, proof = self.load(chat=ROOT.upper())
        self.assert_totals(data, 'a', 'b', 'c', 'd', 'e')
        self.assertEqual([row['id'] for row in data['chats']], [ROOT])
        self.assertEqual({row['id'] for row in data['owners']}, {ROOT, AGENT, NESTED})
        self.assertEqual({row['id'] for row in data['projects']}, {A, B})
        self.assertEqual({row['id'] for row in data['models']}, {'model-a', 'model-b'})
        self.assertEqual(next(row for row in data['owners'] if row['id'] == NESTED)['parentId'], AGENT)
        self.assertEqual(data['chats'][0]['agentCount'], 2)
        self.assertEqual(data['scope'], {'kind': 'chat', 'id': ROOT, 'label': 'Chosen chat', 'empty': False})
        self.assertEqual(proof['scope'], data['scope'])
        self.assertTrue(proof['appPerDaySessionTotalsMatch'])
        self.assertTrue(proof['scopedDailyOwnerChatTotalsMatch'])
        for excluded in ('Separate fork', 'Unrelated chat', 'model-c', C, FORK, OTHER, 'Private', 'Never export', 'Stale copied'):
            self.assertNotIn(excluded, json.dumps(data))

    def test_exact_project_scope_splits_multifolder_chat_and_same_basenames(self):
        first, _ = self.load(project=A)
        second, _ = self.load(project=B)
        self.assert_totals(first, 'a', 'd', 'g')
        self.assert_totals(second, 'b', 'c', 'e')
        self.assertEqual({row['id'] for row in first['chats']}, {ROOT, OTHER})
        self.assertEqual({row['id'] for row in second['chats']}, {ROOT})
        self.assertEqual({row['id'] for row in first['owners']}, {ROOT, AGENT, OTHER})
        self.assertEqual({row['id'] for row in second['owners']}, {ROOT, NESTED})
        self.assertEqual([row['id'] for row in first['projects']], [A])
        self.assertEqual([row['id'] for row in second['projects']], [B])
        self.assertEqual([row['id'] for row in first['models']], ['model-a'])
        self.assertEqual([row['id'] for row in second['models']], ['model-b'])
        self.assertNotEqual(first['totals']['totalTokens'], second['totals']['totalTokens'])
        self.assertNotEqual(first['chats'][0]['totalTokens'], second['chats'][0]['totalTokens'])
        self.assertEqual(first['scope']['id'], A)

    def test_no_scope_preserves_all_chats_projects_and_fork_identity(self):
        data, _ = self.load()
        self.assert_totals(data, 'a', 'b', 'c', 'd', 'e', 'f', 'g')
        self.assertEqual({row['id'] for row in data['chats']}, {ROOT, FORK, OTHER})
        fork, _ = self.load(chat=FORK)
        self.assert_totals(fork, 'f')
        self.assertEqual(fork['owners'][0]['kind'], 'self')
        self.assertIsNone(fork['owners'][0]['parentId'])

    def test_unknown_project_basename_or_agent_id_is_explicit_empty_scope(self):
        for scope in ({'project': 'shared'}, {'project': A.lower()}, {'chat': AGENT},
                      {'chat': '90000000-0000-4000-8000-000000000009'}):
            with self.subTest(scope=scope):
                data, _ = self.load(**scope)
                self.assert_totals(data)
                self.assertTrue(data['scope']['empty'])
                self.assertEqual((data['chats'], data['owners'], data['daily'], data['models'], data['projects']),
                                 ([], [], [], [], []))
                self.assertFalse(any(row['recorded'] for row in data['trend']['days']))
                self.assertIn('No usage records match', data['scopeNote'])

    def test_missing_or_malformed_project_maps_stop_project_scope(self):
        for malformed in (None, [], 'invalid', {A: {}}, {A: {**encoded(PARTS['g']), 'cachedInputTokens': True}}):
            with self.subTest(malformed=malformed):
                self.mutate_row(OTHER, lambda rows: rows[0].update(projects=malformed))
                with self.assertRaisesRegex(adapter.ReportError, 'Project scope is unavailable'):
                    self.load(project=B)

    def test_every_project_map_counter_and_daily_attribution_must_reconcile(self):
        with closing(sqlite3.connect(self.cache)) as db, db:
            original = db.execute('SELECT rows_json FROM session_file_rollups WHERE path LIKE ?', ('%'+OTHER+'.jsonl',)).fetchone()[0]
        for field in adapter.JSON_FIELDS:
            with self.subTest(field=field):
                self.mutate_row(OTHER, lambda rows: rows[0].update(projects={A: {**encoded(PARTS['g']), field: PARTS['g'][adapter.FIELDS[adapter.JSON_FIELDS.index(field)]]+1}}))
                with self.assertRaisesRegex(adapter.ReportError, 'source session day'):
                    self.load(project=A)
                with closing(sqlite3.connect(self.cache)) as db, db:
                    db.execute('UPDATE session_file_rollups SET rows_json=? WHERE path LIKE ?', (original, '%'+OTHER+'.jsonl'))
        self.mutate_row(OTHER, lambda rows: rows[0].update(projects={B: encoded(PARTS['g'])}))
        with self.assertRaisesRegex(adapter.ReportError, 'app daily group totals disagree'):
            self.load(project=A)

    def test_chat_totals_survive_missing_optional_maps_without_wider_rankings(self):
        self.mutate_row(ROOT, lambda rows: [row.pop('models') for row in rows])
        self.mutate_row(AGENT, lambda rows: rows[0].pop('projects'))
        data, proof = self.load(chat=ROOT)
        self.assert_totals(data, 'a', 'b', 'c', 'd', 'e')
        self.assertEqual(data['features'], {'models': False, 'projects': False})
        self.assertEqual((data['models'], data['projects']), ([], []))
        self.assertEqual(proof['rankingDailyAndPeriodTotalsMatch'], {'models': None, 'projects': None})
        self.assertEqual(data['diagnostics']['status'], 'incomplete')

    def test_project_model_breakdown_missing_is_unavailable_without_changing_project_totals(self):
        self.mutate_row(OTHER, lambda rows: rows[0]['projects'][A].pop('models'))
        data, _ = self.load(project=A)
        self.assert_totals(data, 'a', 'd', 'g')
        self.assertEqual(data['features'], {'models': False, 'projects': True})
        self.assertEqual(data['models'], [])

    def assert_project_models_unavailable(self):
        data, proof = self.load(project=B)
        self.assert_totals(data, 'b', 'c', 'e')
        self.assertEqual(data['features'], {'models': False, 'projects': True})
        self.assertEqual(data['models'], [])
        self.assertIsNone(proof['rankingDailyAndPeriodTotalsMatch']['models'])
        self.assertTrue(proof['rankingDailyAndPeriodTotalsMatch']['projects'])
        self.assertTrue(any('Scoped models rankings are unavailable' in warning
                            for warning in data['diagnostics']['warnings']))

    def test_project_nested_model_label_must_match_independent_recorded_distribution(self):
        self.mutate_row(ROOT, lambda rows: rows[1]['projects'][B].update(
            models={'model-not-in-app': encoded(PARTS['b'])}))
        self.assert_project_models_unavailable()

    def test_each_nested_model_counter_distribution_must_match_independent_session_day(self):
        for field in adapter.JSON_FIELDS:
            with self.subTest(field=field):
                primary = encoded(PARTS['b'])
                secondary = encoded(vector(0, 0, 0, 0))
                primary[field] -= 1
                secondary[field] = 1
                self.mutate_row(ROOT, lambda rows: rows[1]['projects'][B].update(
                    models={'model-b': primary, 'model-a': secondary}))
                self.assert_project_models_unavailable()

    def test_model_distribution_must_match_each_day_even_when_period_totals_match(self):
        # Establish a valid second-day split, including its independent app map.
        primary = encoded(PARTS['c'])
        secondary = encoded(vector(0, 0, 0, 0))
        primary['inputTokens'] -= 1
        secondary['inputTokens'] = 1
        self.mutate_row(ROOT, lambda rows: rows[2].update(
            models={'model-b': primary, 'model-a': secondary},
            projects={B: {**encoded(PARTS['c']), 'models': {'model-b': primary, 'model-a': secondary}}}))
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute('UPDATE daily_usage_rollups SET models_json=? WHERE date=?',
                       (json.dumps({'model-b': primary, 'model-a': secondary}), '2026-10-25'))
        valid, _ = self.load(project=B)
        self.assertTrue(valid['features']['models'])
        baseline_models = {row['id']: {field: row[field] for field in adapter.FIELDS}
                           for row in valid['models']}
        # Move one model-a input token from day two to day one in nested maps only.
        def shift_days(rows):
            first = encoded(PARTS['b'])
            added = encoded(vector(0, 0, 0, 0))
            first['inputTokens'] -= 1
            added['inputTokens'] = 1
            rows[1]['projects'][B]['models'] = {'model-b': first, 'model-a': added}
            rows[2]['projects'][B]['models'] = {'model-b': encoded(PARTS['c']),
                                               'model-a': encoded(vector(0, 0, 0, 0))}
        self.mutate_row(ROOT, shift_days)
        with closing(sqlite3.connect(self.cache)) as db:
            rows = json.loads(db.execute('SELECT rows_json FROM session_file_rollups WHERE path LIKE ?',
                                         ('%'+ROOT+'.jsonl',)).fetchone()[0])
            day1, day2 = rows[1]['projects'][B]['models'], rows[2]['projects'][B]['models']
            self.assertEqual(adapter.sum_counters([dict(zip(adapter.FIELDS, (day1['model-a'][field] for field in adapter.JSON_FIELDS))),
                                                   dict(zip(adapter.FIELDS, (day2['model-a'][field] for field in adapter.JSON_FIELDS)))]),
                             baseline_models['model-a'])
        self.assert_project_models_unavailable()

    def test_project_models_require_top_level_session_maps_to_match_app_daily_models(self):
        def rename_models(rows):
            rows[2]['models'] = {'renamed-model': rows[2]['models']['model-b']}
            rows[2]['projects'][B]['models'] = {'renamed-model': encoded(PARTS['c'])}
        self.mutate_row(ROOT, rename_models)
        self.assert_project_models_unavailable()

    def test_scopes_still_check_full_app_session_reconciliation(self):
        for field in adapter.DB_FIELDS:
            with self.subTest(field=field), closing(sqlite3.connect(self.cache)) as db, db:
                db.execute('UPDATE daily_usage_rollups SET '+field+'='+field+'+1 WHERE date=?', ('2026-10-24',))
                db.commit()
                with self.assertRaisesRegex(adapter.ReportError, 'disagree'):
                    self.load(chat=OTHER)
                db.execute('UPDATE daily_usage_rollups SET '+field+'='+field+'-1 WHERE date=?', ('2026-10-24',))

    def test_default_one_day_explicit_today_and_inclusive_n_day_boundaries(self):
        today, _ = adapter.load_cache(self.cache, names_databases=[self.names], now=NOW, project=B)
        self.assert_totals(today, 'e')
        self.assertEqual((today['window']['startDate'], today['window']['endDate']), ('2026-10-26', '2026-10-26'))
        chosen, _ = adapter.load_cache(self.cache, '2026-10-25', '2026-10-25', [self.names], now=NOW, chat=ROOT, days=999)
        self.assert_totals(chosen, 'c')
        several, _ = self.load(chat=ROOT)
        self.assert_totals(several, 'a', 'b', 'c', 'd', 'e')
        self.assertEqual([row['date'] for row in several['trend']['days']], ['2026-10-24', '2026-10-25', '2026-10-26'])

    def test_known_scopes_with_no_usage_in_selected_dates_remain_empty(self):
        for scope in ({'project': A}, {'chat': ROOT}):
            with self.subTest(scope=scope):
                data, proof = adapter.load_cache(self.cache, '2026-10-27', '2026-10-28',
                                                [self.names], now=NOW, **scope)
                self.assert_totals(data)
                self.assertTrue(data['scope']['empty'])
                self.assertEqual(data['trend']['dayCount'], 2)
                self.assertFalse(any(day['recorded'] for day in data['trend']['days']))
                self.assertEqual(data['diagnostics']['status'], 'complete')
                self.assertTrue(proof['appPerDaySessionTotalsMatch'])

    def test_scope_cli_exports_only_selected_data_and_leaves_sources_unchanged(self):
        before = {path: path.read_bytes() for path in (self.cache, self.names)}
        out = self.root/'report'
        with contextlib.redirect_stdout(io.StringIO()):
            status = adapter.main(['--database', str(self.cache), '--names-database', str(self.names),
                                   '--start-date', '2026-10-24', '--end-date', '2026-10-26',
                                   '--project', B, '--out', str(out)])
        self.assertIn(status, (0, 2))
        data = json.loads((out/'data.json').read_text(encoding='utf-8'))
        self.assert_totals(data, 'b', 'c', 'e')
        self.assertIn('Scope: '+B, (out/'summary.txt').read_text(encoding='utf-8'))
        for path, original in before.items():
            self.assertEqual(path.read_bytes(), original)
        for path in out.iterdir():
            for excluded in ('Unrelated chat', 'Separate fork', 'Private', 'Never export', 'model-a', 'model-c'):
                self.assertNotIn(excluded, path.read_text(encoding='utf-8'))

    def test_mutually_exclusive_and_blank_scopes_are_rejected(self):
        with self.assertRaisesRegex(adapter.ReportError, 'not both'):
            self.load(project=A, chat=ROOT)
        for scope in ({'project': ''}, {'chat': '  '}):
            with self.subTest(scope=scope), self.assertRaisesRegex(adapter.ReportError, 'nonempty'):
                self.load(**scope)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            adapter.main(['--database', str(self.cache), '--out', str(self.root/'invalid'),
                          '--project', A, '--chat', ROOT])
        self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
