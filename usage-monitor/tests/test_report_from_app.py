import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from report_from_app import CAVEAT, ReportError, connect_readonly, load_cache, main, write_report

ROOT = '00000000-0000-0000-0000-111111111111'
AGENT = '00000000-0000-0000-0000-222222222222'
FORK = '00000000-0000-0000-0000-333333333333'
NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def day(date='2026-10-26', multiplier=1, total=None):
    return {'date': date, 'inputTokens': 100*multiplier, 'cachedInputTokens': 80*multiplier,
            'outputTokens': 20*multiplier, 'reasoningOutputTokens': 7*multiplier,
            'totalTokens': 120*multiplier if total is None else total,
            'models': {'test': {}}, 'projects': {'private-project': {}}, 'costUSD': 0.01,
            'updatedAt': NOW.isoformat()}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root/'codex-usage-desktop.db'
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.executescript('''
                CREATE TABLE daily_usage_rollups(date TEXT PRIMARY KEY, input_tokens INTEGER,
                  cached_input_tokens INTEGER, output_tokens INTEGER, reasoning_output_tokens INTEGER,
                  total_tokens INTEGER, updated_at TEXT);
                CREATE TABLE scan_runs(id INTEGER PRIMARY KEY, scanned_at TEXT, timezone TEXT, imported_days INTEGER);
                CREATE TABLE session_file_rollups(path TEXT PRIMARY KEY, rows_json TEXT,
                  prompt_title TEXT, quota_usage_json TEXT);
                CREATE TABLE project_session_index(path TEXT PRIMARY KEY, detail_json TEXT, search_text TEXT);
            ''')
            db.execute('INSERT INTO scan_runs VALUES (1,?,?,1)', (NOW.isoformat(), 'Europe/Berlin'))

    def put(self, thread, rows, parent=None, **metadata):
        path = 'C:/sessions/rollout-'+thread+'.jsonl'
        detail = {'agentSessionId': thread, 'parentSessionId': parent,
                  'threadName': 'Private opening prompt', **metadata}
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute('INSERT INTO session_file_rollups VALUES (?,?,?,?)',
                       (path, json.dumps(rows), 'Private opening prompt', '{"account":"secret"}'))
            db.execute('INSERT INTO project_session_index VALUES (?,?,?)',
                       (path, json.dumps(detail), 'Private search body'))
            for row in rows:
                values = [row[key] for key in ('inputTokens', 'cachedInputTokens', 'outputTokens',
                                              'reasoningOutputTokens', 'totalTokens')]
                db.execute('''INSERT INTO daily_usage_rollups VALUES (?,?,?,?,?,?,?)
                  ON CONFLICT(date) DO UPDATE SET input_tokens=input_tokens+excluded.input_tokens,
                  cached_input_tokens=cached_input_tokens+excluded.cached_input_tokens,
                  output_tokens=output_tokens+excluded.output_tokens,
                  reasoning_output_tokens=reasoning_output_tokens+excluded.reasoning_output_tokens,
                  total_tokens=total_tokens+excluded.total_tokens''', [row['date'], *values, NOW.isoformat()])
        return path

    def load(self, *args, **kwargs):
        kwargs.setdefault('names_databases', ())
        return load_cache(self.cache, *args, now=NOW, **kwargs)

    def test_latest_day_and_explicit_family_preserve_app_counters(self):
        self.put(ROOT, [day('2026-10-25'), day()])
        self.put(AGENT, [day(multiplier=2)], parent=ROOT, agentNickname='Analyst', agentPath='/root/analyst')
        data, proof = self.load()
        self.assertEqual(data['window']['startDate'], '2026-10-26')
        self.assertEqual(data['totals']['totalTokens'], 360)
        self.assertEqual(data['totals']['sessions'], 2)
        self.assertIsNone(data['totals']['responses'])
        self.assertEqual(len(data['chats']), 1)
        self.assertEqual(data['chats'][0]['selfTokens'], 120)
        self.assertEqual(data['chats'][0]['agentsTokens'], 240)
        self.assertEqual(data['chats'][0]['agentCount'], 1)
        self.assertEqual(next(row for row in data['owners'] if row['id'] == AGENT)['label'], 'Analyst')
        self.assertTrue(proof['appDailySessionTotalsMatch'])
        self.assertEqual(data['source']['timeZone'], 'Europe/Berlin')

    def test_inclusive_dates_and_user_fork_stays_separate(self):
        self.put(ROOT, [day('2026-10-25'), day()])
        self.put(FORK, [day()], forkedFromId=ROOT)
        data, _ = self.load('2026-10-25', '2026-10-26')
        self.assertEqual(data['totals']['totalTokens'], 360)
        self.assertEqual(len(data['chats']), 2)
        self.assertEqual(len(data['daily']), 2)

    def test_inactive_parent_still_owns_agent_family(self):
        self.put(ROOT, [day('2026-10-25')])
        self.put(AGENT, [day()], parent=ROOT)
        data, _ = self.load()
        self.assertEqual(data['chats'][0]['id'], ROOT)
        self.assertEqual(data['chats'][0]['selfTokens'], 0)
        self.assertEqual(data['chats'][0]['agentsTokens'], 120)

    def test_fork_with_copied_agent_session_id_preserves_both_cache_rows(self):
        self.put(ROOT, [day()])
        self.put(FORK, [day()], agentSessionId=ROOT)
        data, _ = self.load()
        self.assertEqual(data['totals']['sessions'], 2)
        self.assertEqual(data['totals']['totalTokens'], 240)
        self.assertEqual({chat['id'] for chat in data['chats']}, {ROOT, FORK})

    def test_copied_parent_role_and_path_are_not_applied_to_fork(self):
        self.put(ROOT, [day()])
        self.put(FORK, [day()], parent=ROOT, agentSessionId=AGENT,
                 agentRole='Copied reviewer', agentPath='/root/copied-reviewer')
        data, _ = self.load()
        fork = next(owner for owner in data['owners'] if owner['id'] == FORK)
        self.assertEqual(len(data['chats']), 2)
        self.assertEqual(fork['kind'], 'self')
        self.assertIsNone(fork['parentId'])
        self.assertIsNone(fork['path'])
        self.assertNotIn('Copied reviewer', json.dumps(data))

    def test_total_discrepancy_is_preserved_not_recalculated(self):
        self.put(ROOT, [day(total=150)])
        data, _ = self.load()
        self.assertEqual(data['totals']['totalTokens'], 150)
        self.assertEqual(data['totals']['inputTokens'], 100)
        self.assertEqual(data['totals']['outputTokens'], 20)
        self.assertEqual(data['diagnostics']['status'], 'incomplete')

    def test_actual_sidebar_names_without_prompt_backed_fields(self):
        self.put(ROOT, [day()])
        state = self.root/'state_5.sqlite'
        with closing(sqlite3.connect(state)) as db, db:
            db.execute('CREATE TABLE threads(id TEXT, name TEXT, title TEXT)')
            db.execute('INSERT INTO threads VALUES (?,?,?)', (ROOT, 'Actual sidebar name', 'Private database prompt'))
        data, _ = self.load(names_databases=[state])
        self.assertEqual(data['chats'][0]['label'], 'Actual sidebar name')
        self.assertEqual(data['chats'][0]['labelSource'], 'threads.name')
        exported = json.dumps(data)
        for secret in ('Private opening prompt', 'Private database prompt', 'Private search body', 'secret', 'private-project'):
            self.assertNotIn(secret, exported)

    def test_empty_range_is_zero_recorded(self):
        self.put(ROOT, [day()])
        data, _ = self.load('2026-10-24', '2026-10-24')
        self.assertEqual(data['totals']['totalTokens'], 0)
        self.assertEqual(data['chats'], [])
        self.assertEqual(data['diagnostics']['status'], 'complete')

    def test_newest_numeric_state_database_is_discovered(self):
        self.put(ROOT, [day()])
        for version in (9, 10):
            with closing(sqlite3.connect(self.root/f'state_{version}.sqlite')) as db, db:
                db.execute('CREATE TABLE threads(id TEXT, name TEXT, title TEXT)')
                db.execute('INSERT INTO threads VALUES (?,?,?)', (ROOT, f'Name {version}', 'Private database prompt'))
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root)}):
            data, _ = load_cache(self.cache, now=NOW)
            explicit, _ = load_cache(self.cache, names_databases=[self.root/'state_9.sqlite'], now=NOW)
        self.assertEqual(data['chats'][0]['label'], 'Name 10')
        self.assertEqual(explicit['chats'][0]['label'], 'Name 9')

    def test_optional_names_error_keeps_numeric_result(self):
        self.put(ROOT, [day()])
        data, _ = self.load(names_databases=[self.root/'missing-state.sqlite'])
        self.assertEqual(data['totals']['totalTokens'], 120)
        self.assertEqual(data['chats'][0]['labelSource'], 'session_id')
        self.assertEqual(data['diagnostics']['status'], 'complete')

    def test_missing_hierarchy_preserves_totals_with_warning(self):
        self.put(ROOT, [day()])
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute('DROP TABLE project_session_index')
        data, _ = self.load()
        self.assertEqual(data['totals']['totalTokens'], 120)
        self.assertEqual(data['diagnostics']['status'], 'incomplete')
        self.assertIn('lack cached agent metadata', data['diagnostics']['warnings'][0])

    def test_null_agent_identity_preserves_totals_without_guessing_hierarchy(self):
        self.put(ROOT, [day()])
        self.put(AGENT, [day()], parent=ROOT, agentSessionId=None, agentRole='Unproved reviewer')
        data, _ = self.load()
        self.assertEqual(data['totals']['totalTokens'], 240)
        self.assertEqual(data['totals']['sessions'], 2)
        self.assertEqual(len(data['chats']), 2)
        self.assertEqual(data['diagnostics']['status'], 'incomplete')
        self.assertIn('lack cached agent metadata', data['diagnostics']['warnings'][0])
        self.assertNotIn('Unproved reviewer', json.dumps(data))

    def test_stale_scan_warns_with_refresh_action(self):
        self.put(ROOT, [day()])
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute('UPDATE scan_runs SET scanned_at=?', ((NOW-timedelta(hours=1)).isoformat(),))
        data, _ = self.load()
        self.assertEqual(data['source']['ageSeconds'], 3600)
        self.assertEqual(data['diagnostics']['status'], 'incomplete')
        self.assertIn('refresh local usage', data['diagnostics']['warnings'][0])

    def test_inconsistent_app_daily_and_session_totals_stop_report(self):
        self.put(ROOT, [day()])
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute('UPDATE daily_usage_rollups SET total_tokens=121')
        with self.assertRaisesRegex(ReportError, 'disagree'):
            self.load()

    def test_malformed_numeric_cache_stops_report(self):
        self.put(ROOT, [day()])
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute("UPDATE session_file_rollups SET rows_json='not JSON'")
        with self.assertRaisesRegex(ReportError, 'Malformed'):
            self.load()

    def test_parent_cycle_stops_report(self):
        self.put(ROOT, [day()], parent=AGENT)
        self.put(AGENT, [day()], parent=ROOT)
        with self.assertRaisesRegex(ReportError, 'Cyclic'):
            self.load()

    def test_readonly_connection_rejects_write_and_cache_unchanged(self):
        self.put(ROOT, [day()])
        before = self.cache.read_bytes()
        db = connect_readonly(self.cache)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM daily_usage_rollups')
        finally:
            db.close()
        self.load()
        self.assertEqual(self.cache.read_bytes(), before)

    def test_write_four_outputs_escaped_and_numeric_evidence(self):
        self.put(ROOT, [day()])
        data, proof = self.load()
        data['chats'][0]['label'] = '</script><script>alert(1)</script>&'
        out = self.root/'report'
        write_report(data, proof, out)
        self.assertEqual({path.name for path in out.iterdir()}, {'index.html', 'data.json', 'summary.txt', 'evidence.json'})
        html = (out/'index.html').read_text(encoding='utf-8')
        self.assertNotIn('</script><script>alert', html)
        self.assertIn('\\u003c/script\\u003e', html)
        evidence = json.loads((out/'evidence.json').read_text(encoding='utf-8'))
        self.assertEqual(evidence['htmlSha256'], hashlib.sha256((out/'index.html').read_bytes()).hexdigest())
        self.assertEqual(evidence['dataSha256'], hashlib.sha256((out/'data.json').read_bytes()).hexdigest())
        self.assertIn(CAVEAT, (out/'summary.txt').read_text(encoding='utf-8'))
        for path in out.iterdir():
            text = path.read_text(encoding='utf-8')
            for private in ('Private opening prompt', 'Private search body', '"account": "secret"'):
                self.assertNotIn(private, text)

    def test_cli_error_does_not_create_dashboard(self):
        out = self.root/'missing-report'
        with contextlib.redirect_stderr(io.StringIO()) as printed:
            status = main(['--database', str(self.root/'missing.db'), '--out', str(out)])
        self.assertEqual(status, 1)
        self.assertIn('refresh local usage', printed.getvalue())
        self.assertFalse(out.exists())

    def test_uuid_case_normalization_preserves_name_and_exact_family_counters(self):
        root = 'aaaaaaaa-0000-4000-8000-111111111111'
        agent = 'bbbbbbbb-0000-4000-8000-222222222222'
        self.put(root.upper(), [day()], agentSessionId=root)
        self.put(agent.upper(), [day(multiplier=2)], parent=root.upper(),
                 agentSessionId=agent, agentNickname='Case analyst')
        state = self.root/'case-names.sqlite'
        with closing(sqlite3.connect(state)) as db, db:
            db.execute('CREATE TABLE threads(id TEXT, name TEXT)')
            db.execute('INSERT INTO threads VALUES (?,?)', (root.upper(), 'Case-normalized sidebar'))
        data, _ = self.load(names_databases=[state])
        fields = ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningTokens', 'totalTokens')
        owners = {row['id']: row for row in data['owners']}
        self.assertEqual(set(owners), {root, agent})
        self.assertEqual(tuple(owners[root][field] for field in fields), (100,80,20,7,120))
        self.assertEqual(tuple(owners[agent][field] for field in fields), (200,160,40,14,240))
        self.assertEqual((owners[agent]['chatId'], owners[agent]['parentId'], owners[agent]['kind']),
                         (root, root, 'agent'))
        self.assertEqual(len(data['chats']), 1)
        chat = data['chats'][0]
        self.assertEqual((chat['id'], chat['label']), (root, 'Case-normalized sidebar'))
        self.assertEqual(set(chat['owners']), {root, agent})
        self.assertEqual(tuple(chat[field] for field in fields), (300,240,60,21,360))
        self.assertEqual((chat['selfTokens'], chat['agentsTokens'], chat['agentCount']), (120,240,1))

    def test_malformed_owned_parent_is_rejected_instead_of_silent_assignment(self):
        self.put(ROOT, [day()])
        path = self.put(AGENT, [day(multiplier=2)], parent=ROOT)
        for parent in (0, False, [], {}, '', 'not-a-session-id', '   '):
            with self.subTest(parent=parent):
                detail = {'agentSessionId': AGENT, 'parentSessionId': parent}
                with closing(sqlite3.connect(self.cache)) as db, db:
                    db.execute('UPDATE project_session_index SET detail_json=? WHERE path=?',
                               (json.dumps(detail), path))
                with self.assertRaisesRegex(ReportError, 'Invalid agent parent'):
                    self.load()

    def test_malformed_copied_fork_parent_is_not_owned_lineage(self):
        self.put(ROOT, [day()])
        self.put(FORK, [day(multiplier=2)], parent=[], agentSessionId=AGENT,
                 agentNickname='Copied analyst')
        data, _ = self.load()
        fields = ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningTokens', 'totalTokens')
        expected = {ROOT:(100,80,20,7,120), FORK:(200,160,40,14,240)}
        self.assertEqual({chat['id'] for chat in data['chats']}, set(expected))
        self.assertEqual(len(data['owners']), 2)
        for chat in data['chats']:
            self.assertEqual(tuple(chat[field] for field in fields), expected[chat['id']])
            self.assertEqual(chat['owners'], [chat['id']])
            self.assertEqual((chat['selfTokens'], chat['agentsTokens']), (expected[chat['id']][-1], 0))
        for owner in data['owners']:
            self.assertEqual(tuple(owner[field] for field in fields), expected[owner['id']])
            self.assertEqual((owner['chatId'], owner['parentId'], owner['kind']),
                             (owner['id'], None, 'self'))


if __name__ == '__main__':
    unittest.main()
