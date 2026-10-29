"""Calendar trends and independent cached group rankings, without raw log data."""
import contextlib
import io
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import report_from_app as adapter

ROOT = 'aaaaaaaa-0000-4000-8000-111111111111'
AGENT = 'bbbbbbbb-0000-4000-8000-222222222222'
NESTED = 'cccccccc-0000-4000-8000-333333333333'
FORK = 'dddddddd-0000-4000-8000-444444444444'
NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
FIELDS = adapter.FIELDS
JSON_FIELDS = adapter.JSON_FIELDS
DB_FIELDS = adapter.DB_FIELDS


def values(multiplier=1, *, total=None):
    return dict(zip(FIELDS, (103*multiplier, 61*multiplier, 37*multiplier,
                            13*multiplier, 140*multiplier if total is None else total)))


def encoded(vector):
    return dict(zip(JSON_FIELDS, (vector[field] for field in FIELDS)))


def partition(vector, labels, salt=0):
    """Unequal, independently weighted counter vectors with exact integer sums."""
    result = {label: {} for label in labels}
    for field_index, field in enumerate(FIELDS):
        weights = [((index+1+salt) % len(labels)+1)**(1+field_index % 2)
                   for index in range(len(labels))]
        denominator = sum(weights)
        portions = [vector[field]*weight//denominator for weight in weights]
        for index in range(vector[field]-sum(portions)):
            portions[index % len(labels)] += 1
        for label, portion in zip(labels, portions):
            result[label][JSON_FIELDS[field_index]] = portion
    return result


class TrendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root/'cache.sqlite'
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.executescript('''
                CREATE TABLE daily_usage_rollups(date TEXT PRIMARY KEY, input_tokens INTEGER,
                    cached_input_tokens INTEGER, output_tokens INTEGER,
                    reasoning_output_tokens INTEGER, total_tokens INTEGER,
                    models_json TEXT, projects_json TEXT);
                CREATE TABLE scan_runs(id INTEGER PRIMARY KEY, scanned_at TEXT, timezone TEXT);
                CREATE TABLE session_file_rollups(path TEXT PRIMARY KEY, rows_json TEXT,
                    prompt_title TEXT, quota_usage_json TEXT);
                CREATE TABLE project_session_index(path TEXT PRIMARY KEY, detail_json TEXT);
            ''')
            db.execute('INSERT INTO scan_runs VALUES (1,?,?)', (NOW.isoformat(), 'Europe/Berlin'))

    def put(self, thread, rows, parent=None, *, owned_id=None):
        path = 'C:/fixture/rollout-'+thread+'.jsonl'
        payload = [{'date': day, **encoded(vector), 'models': {'session-only-secret': {}},
                    'projects': {'session-only-secret': {}}} for day, vector in rows]
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute('INSERT INTO session_file_rollups VALUES (?,?,?,?)',
                       (path, json.dumps(payload), 'Prompt secret', 'Account secret'))
            db.execute('INSERT INTO project_session_index VALUES (?,?)', (path, json.dumps({
                'agentSessionId': owned_id or thread, 'parentSessionId': parent,
                'agentNickname': 'Agent name', 'agentPath': '/root/agent'})))
            for day, vector in rows:
                existing = db.execute('SELECT '+', '.join(DB_FIELDS)+
                                      ' FROM daily_usage_rollups WHERE date=?', (day,)).fetchone()
                combined = {field: vector[field]+(existing[index] if existing else 0)
                            for index, field in enumerate(FIELDS)}
                db.execute('INSERT OR REPLACE INTO daily_usage_rollups VALUES (?,?,?,?,?,?,?,?)',
                           [day, *(combined[field] for field in FIELDS),
                            json.dumps({'model-default': encoded(combined)}),
                            json.dumps({'C:/project/default': encoded(combined)})])
        return path

    def load(self, *args, **kwargs):
        return adapter.load_cache(self.cache, *args, names_databases=(), now=NOW, **kwargs)

    def mutate(self, query, parameters=()):
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute(query, parameters)

    def maps(self, day, models, projects):
        self.mutate('UPDATE daily_usage_rollups SET models_json=?, projects_json=? WHERE date=?',
                    (json.dumps(models), json.dumps(projects), day))

    def test_thirty_calendar_bins_distinguish_gap_from_recorded_zero(self):
        self.put(ROOT, [('2026-09-27', values()), ('2026-10-24', values(0)),
                        ('2026-10-26', values(3))])
        data, proof = self.load(days=30)
        trend = data['trend']
        self.assertEqual((trend['startDate'], trend['endDate'], trend['dayCount'], trend['timeZone']),
                         ('2026-09-27', '2026-10-26', 30, 'Europe/Berlin'))
        self.assertEqual(len(trend['days']), 30)
        days = {row['date']: row for row in trend['days']}
        self.assertFalse(days['2026-10-25']['recorded'])
        self.assertTrue(all(days['2026-10-25'][field] is None for field in FIELDS))
        self.assertTrue(days['2026-10-24']['recorded'])
        self.assertTrue(all(days['2026-10-24'][field] == 0 for field in FIELDS))
        self.assertEqual(len(data['daily']), 3)
        self.assertEqual(data['totals']['totalTokens'], 560)
        self.assertEqual(data['generatedAt'], NOW.isoformat())
        self.assertTrue(proof['appPerDaySessionTotalsMatch'])

    def test_legacy_load_latest_day_and_explicit_dates_override_days(self):
        self.put(ROOT, [('2026-10-23', values()), ('2026-10-24', values(2))])
        latest, _ = self.load()
        self.assertEqual(latest['trend']['dayCount'], 1)
        selected, _ = self.load('2026-10-23', '2026-10-26', days=999)
        self.assertEqual(selected['trend']['dayCount'], 4)
        self.assertEqual(selected['trend']['endDate'], '2026-10-26')
        self.assertFalse(selected['trend']['days'][-1]['recorded'])
        self.assertEqual(selected['source']['lastScanAt'], NOW.isoformat())

    def test_dst_boundaries_use_inclusive_app_dates_without_timezone_dependency(self):
        self.put(ROOT, [('2026-03-28', values()), ('2026-03-29', values(2)),
                        ('2026-03-30', values(3)), ('2026-10-24', values(4)),
                        ('2026-10-25', values(5)), ('2026-10-26', values(6))])
        for start, end in [('2026-03-28', '2026-03-30'), ('2026-10-24', '2026-10-26')]:
            with self.subTest(start=start):
                data, _ = self.load(start, end)
                self.assertEqual(data['trend']['dayCount'], 3)
                self.assertEqual(data['trend']['days'][0]['date'], start)
                self.assertEqual(data['trend']['days'][-1]['date'], end)
                self.assertTrue(all(row['recorded'] for row in data['trend']['days']))
                self.assertEqual(data['daily'], [{key: value for key, value in row.items() if key != 'recorded'}
                                                for row in data['trend']['days']])

    def test_invalid_days_dates_and_excessive_spans_have_explicit_errors(self):
        self.put(ROOT, [('2026-10-26', values())])
        for days in (0, -1, True, 1.5, '30', adapter.MAX_DAYS+1):
            with self.subTest(days=days), self.assertRaisesRegex(adapter.ReportError, 'Days must'):
                self.load(days=days)
        for start, end, message in [('2026-10-26', None, 'both'),
                                    (None, '2026-10-26', 'both'),
                                    ('20261025', '2026-10-26', 'YYYY-MM-DD'),
                                    ('2026-02-30', '2026-10-26', 'YYYY-MM-DD'),
                                    ('2026-10-27', '2026-10-26', 'before'),
                                    ('2024-01-01', '2026-10-26', 'no more than')]:
            with self.subTest(start=start), self.assertRaisesRegex(adapter.ReportError, message):
                self.load(start, end)

    def test_nested_agents_retroactively_follow_inactive_root_each_day(self):
        self.put(ROOT.upper(), [('2026-09-01', values(9))], owned_id=ROOT)
        self.put(AGENT, [('2026-10-24', values(2))], parent=ROOT.upper())
        self.put(NESTED, [('2026-10-24', values(3)), ('2026-10-26', values(4))], parent=AGENT)
        data, _ = self.load('2026-10-24', '2026-10-26')
        self.assertEqual(len(data['chats']), 1)
        chat = data['chats'][0]
        self.assertEqual(chat['id'], ROOT)
        self.assertEqual(chat['selfTokens'], 0)
        self.assertEqual(chat['agentCount'], 2)
        self.assertEqual(chat['agentsTokens'], 1260)
        self.assertEqual([row['date'] for row in chat['daily']], ['2026-10-24', '2026-10-26'])
        self.assertEqual([{field: row[field] for field in FIELDS} for row in chat['daily']],
                         [values(5), values(4)])
        self.assertEqual({field: chat[field] for field in FIELDS}, values(9))

    def test_copied_fork_lineage_never_changes_chat_daily_identity(self):
        self.put(ROOT, [('2026-10-24', values())])
        self.put(FORK, [('2026-10-26', values(2))], parent=ROOT, owned_id=AGENT)
        data, _ = self.load(days=3)
        chats = {chat['id']: chat for chat in data['chats']}
        self.assertEqual(set(chats), {ROOT, FORK})
        self.assertEqual(chats[ROOT]['daily'], [{'date': '2026-10-24', **values()}])
        self.assertEqual(chats[FORK]['daily'], [{'date': '2026-10-26', **values(2)}])
        fork = next(owner for owner in data['owners'] if owner['id'] == FORK)
        self.assertEqual((fork['parentId'], fork['kind'], fork['path']), (None, 'self', None))

    def test_seven_models_twelve_projects_preserve_all_vectors_and_full_paths(self):
        models = ['model-'+str(index) for index in range(7)]
        projects = ['C:/alpha/shared', 'D:/beta/shared']+['C:/project/'+str(index) for index in range(10)]
        source = {}
        for index, day in enumerate(('2026-10-24', '2026-10-26')):
            vector = values(100+index*17)
            self.put(ROOT if index == 0 else FORK, [(day, vector)])
            model_map = partition(vector, models, index)
            project_map = partition(vector, projects, index+2)
            for project in project_map.values():
                project['models'] = {'nested-model-secret': {'irrelevant': 'Never export this'}}
            self.maps(day, model_map, project_map)
            source[day] = {'models': model_map, 'projects': project_map}
        data, proof = self.load(days=3)
        self.assertEqual(data['features'], {'models': True, 'projects': True})
        for kind, expected_labels in [('models', models), ('projects', projects)]:
            ranking = data[kind]
            self.assertEqual({row['id'] for row in ranking}, set(expected_labels))
            self.assertEqual([row['totalTokens'] for row in ranking],
                             sorted((row['totalTokens'] for row in ranking), reverse=True))
            for row in ranking:
                self.assertEqual(row['label'], row['id'])
                expected_daily = [{'date': day, **dict(zip(FIELDS, (groups[kind][row['id']][field]
                                                                  for field in JSON_FIELDS)))}
                                  for day, groups in source.items()]
                self.assertEqual(row['daily'], expected_daily)
                for field in FIELDS:
                    self.assertEqual(row[field], sum(day[field] for day in expected_daily))
                self.assertAlmostEqual(row['sharePercent'], row['totalTokens']/data['totals']['totalTokens']*100)
            for field in FIELDS:
                self.assertEqual(sum(row[field] for row in ranking), data['totals'][field])
            for day in data['daily']:
                for field in FIELDS:
                    self.assertEqual(sum(entry[field] for row in ranking for entry in row['daily']
                                         if entry['date'] == day['date']), day[field])
            self.assertAlmostEqual(sum(row['sharePercent'] for row in ranking), 100)
        self.assertEqual(proof['rankingDailyAndPeriodTotalsMatch'], {'models': True, 'projects': True})
        exported = json.dumps(data)
        for secret in ('nested-model-secret', 'Never export this', 'session-only-secret', 'Prompt secret', 'Account secret'):
            self.assertNotIn(secret, exported)

    def test_rank_daily_is_sparse_and_does_not_invent_cross_dimension_intersections(self):
        self.put(ROOT, [('2026-10-24', values()), ('2026-10-26', values(2))])
        self.maps('2026-10-24', {'model-a': encoded(values())}, {'C:/a': encoded(values())})
        self.maps('2026-10-26', {'model-b': encoded(values(2))}, {'D:/b': encoded(values(2))})
        data, _ = self.load(days=3)
        self.assertEqual({row['id']: row['daily'] for row in data['models']},
                         {'model-a': [{'date': '2026-10-24', **values()}],
                          'model-b': [{'date': '2026-10-26', **values(2)}]})
        for row in [*data['models'], *data['projects']]:
            self.assertEqual(set(row), {'id', 'label', *FIELDS, 'sharePercent', 'daily'})

    def test_optional_column_absence_disables_only_its_feature(self):
        self.put(ROOT, [('2026-10-26', values())])
        self.mutate('ALTER TABLE daily_usage_rollups DROP COLUMN models_json')
        data, proof = self.load()
        self.assertEqual(data['features'], {'models': False, 'projects': True})
        self.assertEqual(data['models'], [])
        self.assertEqual(data['diagnostics']['status'], 'complete')
        self.assertIsNone(proof['rankingDailyAndPeriodTotalsMatch']['models'])

    def test_missing_nonzero_map_disables_whole_ranking_with_warning(self):
        self.put(ROOT, [('2026-10-24', values()), ('2026-10-26', values(2))])
        for missing in (None, '{}'):
            with self.subTest(missing=missing):
                self.mutate('UPDATE daily_usage_rollups SET models_json=? WHERE date=?', (missing, '2026-10-26'))
                data, proof = self.load(days=3)
                self.assertEqual(data['features'], {'models': False, 'projects': True})
                self.assertEqual(data['models'], [])
                self.assertEqual(data['totals']['totalTokens'], 420)
                self.assertEqual(data['diagnostics']['status'], 'incomplete')
                self.assertTrue(any('rankings are unavailable' in warning for warning in data['diagnostics']['warnings']))
                self.assertIsNone(proof['rankingDailyAndPeriodTotalsMatch']['models'])

    def test_null_maps_on_recorded_zero_day_need_no_fabricated_group(self):
        self.put(ROOT, [('2026-10-26', values(0))])
        self.mutate('UPDATE daily_usage_rollups SET models_json=NULL, projects_json=NULL')
        data, _ = self.load()
        self.assertEqual(data['features'], {'models': True, 'projects': True})
        self.assertEqual((data['models'], data['projects']), ([], []))
        self.assertEqual(data['diagnostics']['status'], 'complete')

    def test_malformed_group_maps_and_counter_types_are_rejected(self):
        self.put(ROOT, [('2026-10-26', values())])
        valid = encoded(values())
        malformed = ['not JSON', '[]', 'null', '{"model":1}', '{"model":{}}',
                     '{"dup":'+json.dumps(valid)+',"dup":'+json.dumps(valid)+'}']
        for field in JSON_FIELDS:
            for bad in (True, False, -1, 1.25, '12', None):
                malformed.append(json.dumps({'model': {**valid, field: bad}}))
        for kind in ('models', 'projects'):
            for payload in malformed:
                with self.subTest(kind=kind, payload=payload), self.assertRaises(adapter.ReportError):
                    self.mutate('UPDATE daily_usage_rollups SET '+kind+'_json=?', (payload,))
                    self.load()
            self.mutate('UPDATE daily_usage_rollups SET '+kind+'_json=?', (json.dumps({'valid': valid}),))

    def test_every_group_counter_must_reconcile_daily_even_when_period_matches(self):
        self.put(ROOT, [('2026-10-24', values(3)), ('2026-10-26', values(4))])
        for kind in ('models', 'projects'):
            for field in JSON_FIELDS:
                first, second = encoded(values(3)), encoded(values(4))
                first[field] += 1
                second[field] -= 1
                self.mutate('UPDATE daily_usage_rollups SET '+kind+'_json=? WHERE date=?',
                            (json.dumps({'valid': first}), '2026-10-24'))
                self.mutate('UPDATE daily_usage_rollups SET '+kind+'_json=? WHERE date=?',
                            (json.dumps({'valid': second}), '2026-10-26'))
                with self.subTest(kind=kind, field=field), self.assertRaisesRegex(adapter.ReportError, kind+' totals disagree'):
                    self.load(days=3)
            for day, multiplier in [('2026-10-24', 3), ('2026-10-26', 4)]:
                self.mutate('UPDATE daily_usage_rollups SET '+kind+'_json=? WHERE date=?',
                            (json.dumps({'valid': encoded(values(multiplier))}), day))

    def test_every_session_counter_must_reconcile_by_date_not_only_period(self):
        path = self.put(ROOT, [('2026-10-24', values(3)), ('2026-10-26', values(4))])
        for field in JSON_FIELDS:
            first, second = encoded(values(3)), encoded(values(4))
            first[field] += 1
            second[field] -= 1
            self.mutate('UPDATE session_file_rollups SET rows_json=? WHERE path=?',
                        (json.dumps([{'date': '2026-10-24', **first}, {'date': '2026-10-26', **second}]), path))
            with self.subTest(field=field), self.assertRaisesRegex(adapter.ReportError, 'session totals disagree on'):
                self.load(days=3)

    def test_recorded_total_and_subset_discrepancies_are_preserved_in_all_outputs(self):
        vector = {'inputTokens': 100, 'cachedInputTokens': 150, 'outputTokens': 20,
                  'reasoningTokens': 30, 'totalTokens': 170}
        self.put(ROOT, [('2026-10-26', vector)])
        data, _ = self.load()
        for row in [data['totals'], data['daily'][0], data['trend']['days'][0], data['chats'][0],
                    data['chats'][0]['daily'][0], data['models'][0], data['projects'][0]]:
            self.assertEqual({field: row[field] for field in FIELDS}, vector)
        self.assertEqual(data['diagnostics']['status'], 'incomplete')
        self.assertTrue(any('input plus output' in warning for warning in data['diagnostics']['warnings']))
        self.assertTrue(any('subset exceeds' in warning for warning in data['diagnostics']['warnings']))
        self.assertIsNone(data['totals']['uncachedInputTokens'])

    def test_cache_bytes_are_unchanged_after_optional_rank_queries(self):
        self.put(ROOT, [('2026-10-26', values())])
        before = self.cache.read_bytes()
        self.load(days=30)
        self.assertEqual(self.cache.read_bytes(), before)

    def test_export_json_and_embedded_payload_roundtrip_special_labels_and_real_newlines(self):
        self.put(ROOT, [('2026-10-26', values())])
        data, proof = self.load(days=30)
        label = '</script><script>alert("x")</script>& café \\ literal\nsecond line'
        data['chats'][0]['label'] = label
        out = self.root/'roundtrip'
        adapter.write_report(data, proof, out)
        exported = (out/'data.json').read_text(encoding='utf-8')
        self.assertEqual(json.loads(exported), data)
        self.assertTrue(exported.endswith('\n'))
        html = (out/'index.html').read_text(encoding='utf-8')
        embedded = re.search(r'<script\b[^>]*id="usage-data"[^>]*>(.*?)</script>', html, re.S).group(1)
        self.assertEqual(json.loads(embedded), data)
        self.assertEqual(json.loads(embedded)['chats'][0]['label'], label)
        self.assertNotIn('</script><script>alert', html)
        self.assertEqual(json.loads((out/'evidence.json').read_text(encoding='utf-8'))['generatedAt'], NOW.isoformat())
        self.assertGreater(len((out/'summary.txt').read_text(encoding='utf-8').splitlines()), 7)

    def test_cli_defaults_to_one_day_and_explicit_dates_override(self):
        self.put(ROOT, [('2026-10-26', values())])
        for extra, expected in [([], ('2026-10-26', '2026-10-26', 1)),
                                 (['--days', '7'], ('2026-10-20', '2026-10-26', 7)),
                                 (['--start-date', '2026-10-24', '--end-date', '2026-10-26', '--days', '999'],
                                  ('2026-10-24', '2026-10-26', 3))]:
            with self.subTest(extra=extra), patch.object(adapter, 'write_report') as write, contextlib.redirect_stdout(io.StringIO()):
                status = adapter.main(['--database', str(self.cache), '--out', str(self.root/'out'), *extra])
                self.assertIn(status, (0, 2))
                trend = write.call_args.args[0]['trend']
                self.assertEqual((trend['startDate'], trend['endDate'], trend['dayCount']), expected)


if __name__ == '__main__':
    unittest.main()
