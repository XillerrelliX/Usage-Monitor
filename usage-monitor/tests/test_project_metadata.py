"""Fixture-only selective metadata and five-counter named-project tests."""
import copy
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import dashboard_data as ledger
import project_metadata as projects
import report_from_app as core
from test_scopes import fixture, ROOT, AGENT, NESTED, FORK, OTHER, A, B, C, NOW

P, Q, R = 'actual-project-alpha', 'actual-project-beta', 'actual-project-gamma'
L = 'legacy-project-alpha'
BUCKET = projects.UNASSIGNED_ID


class ProjectMetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database, self.names = fixture(self.directory)
        self.cache = self.directory / 'ledger.json'
        with closing(sqlite3.connect(self.names)) as db, db:
            db.executescript('''
                ALTER TABLE threads ADD COLUMN project_id TEXT;
                CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT, metadata TEXT);
                CREATE TABLE project_roots(project_id TEXT, path TEXT);
            ''')
            db.executemany('INSERT INTO projects VALUES(?,?,?)', [
                (P, 'Alpha real name', 'PRIVATE ACCOUNT DATA'), (Q, 'Beta real name', 'PRIVATE PROMPT'),
                (R, 'Gamma real name', 'PRIVATE IDENTITY')])
            db.executemany('INSERT INTO project_roots VALUES(?,?)', [(P, A), (Q, B), (R, C)])
        self.bundle, _, _ = ledger.build_bundle(self.database, [self.names], cache_file=self.cache, now=NOW)
        self.state_path = self.directory / 'project-state.json'

    def state(self, *, assignments=None, projectless=None, roots=None, migration=True):
        state = {
            'local-projects': {L: {'id': L, 'name': 'Stale legacy label', 'rootPaths': roots or [A],
                                    'private': 'PRIVATE PROMPT'}},
            'thread-project-assignments': assignments or {},
            'projectless-thread-ids': projectless or [],
            'app-server-project-id-by-legacy-project-id-by-host': {
                'local:' + str(self.directory.resolve()): {L: P} if migration else {},
                'remote:PRIVATE ACCOUNT': {L: 'remote-project-id'}},
            'account_identity': 'PRIVATE ACCOUNT IDENTITY',
            'queued-follow-ups': ['PRIVATE PROMPT'],
        }
        self.state_path.write_text(json.dumps(state), encoding='utf-8')
        return self.state_path

    def resolve(self, **kwargs):
        return projects.resolve_project_metadata(self.bundle, [self.names], **kwargs)

    def prepared(self, metadata=None):
        bundle = copy.deepcopy(self.bundle)
        bundle['projectMetadata'] = metadata or self.resolve()
        return projects.prepare_project_view(bundle)

    def view(self, metadata=None, **selection):
        bundle = copy.deepcopy(self.bundle)
        bundle['projectMetadata'] = metadata or self.resolve()
        return projects.project_report(bundle, '2026-10-24', '2026-10-26', now=NOW, **selection)

    def assert_vector(self, report, expected):
        self.assertEqual(tuple(report['totals'][field] for field in core.FIELDS), expected)
        for kind in ('daily', 'owners', 'chats', 'projects'):
            self.assertEqual(tuple(core.sum_counters(report[kind])[field] for field in core.FIELDS), expected, kind)
        if report['features']['models']:
            self.assertEqual(tuple(core.sum_counters(report['models'])[field] for field in core.FIELDS), expected)

    def test_fixed_five_counter_root_slice_vectors_and_same_basenames(self):
        self.assert_vector(self.view(), (259, 160, 40, 14, 299))
        self.assert_vector(self.view(project=P), (215, 151, 22, 6, 237))
        self.assert_vector(self.view(project=Q), (25, 3, 15, 6, 40))
        self.assert_vector(self.view(project=R), (19, 6, 3, 2, 22))
        self.assertEqual(self.view(project=P)['scope']['label'], 'Alpha real name')
        self.assertEqual(self.view()['scope']['label'], 'All chats and named projects')
        self.assertEqual(self.view(project=P)['sourceFolders'], [A])
        self.assertEqual(self.view(project=P)['projectAttribution']['provenance'], ['verified-root'])

    def test_raw_bundle_maps_cache_and_proof_are_unchanged(self):
        original = copy.deepcopy(self.bundle)
        cache_before = self.cache.read_bytes()
        view = self.prepared()
        self.assertEqual(self.bundle, original)
        self.assertEqual(cache_before, self.cache.read_bytes())
        self.assertEqual(view['sourceAppProjects'], self.bundle['appProjects'])
        for source, grouped in zip(self.bundle['records'], view['records']):
            self.assertEqual(grouped['sourceProjects'], source['projects'])
            self.assertEqual(grouped['sourceProjectModels'], source['projectModels'])
            self.assertEqual(core.sum_counters(grouped['projects'].values()), {f: source[f] for f in core.FIELDS})
        self.assertEqual(projects.prepare_project_view(view), view)

    def test_multiple_named_projects_and_chats_preserve_labels_and_source_intersection(self):
        view = self.view(projects=[Q, P, P], chats=[OTHER, ROOT, ROOT])
        self.assert_vector(view, (240, 154, 37, 12, 277))
        self.assertEqual(view['selection'], {'projects': [P, Q], 'chats': [ROOT, OTHER]})
        self.assertEqual(view['sourceFolders'], sorted([A, B]))
        self.assertEqual([row['label'] for row in view['projects']], ['Alpha real name', 'Beta real name'])
        self.assertEqual([row['label'] for row in view['comparison']['series']], ['Chosen chat', 'Unrelated chat'])
        self.assertEqual([row['totalTokens'] for row in view['comparison']['series']], [204, 73])
        self.assertEqual(view['scope']['label'], 'Alpha real name, Beta real name • Chosen chat, Unrelated chat')
        project_only = self.view(projects=[Q, P])
        self.assertEqual([row['label'] for row in project_only['comparison']['series']], ['Alpha real name', 'Beta real name'])
        narrowed = self.view(projects=[P, R], chats=[ROOT, OTHER])
        self.assertEqual(narrowed['sourceFolders'], [A])
        self.assertEqual(narrowed['totals']['totalTokens'], 237)
        with self.assertRaisesRegex(core.ReportError, 'saved catalog'):
            self.view(projects=[P, A])

    def test_multiple_named_projects_keep_verified_owner_overrides_and_projectless_bucket(self):
        metadata = self.resolve(global_state_file=self.state(projectless=[ROOT]))
        view = self.view(metadata, projects=[BUCKET, P], chats=[ROOT, OTHER])
        self.assert_vector(view, (240, 154, 37, 12, 277))
        self.assertEqual({row['id'] for row in view['projects']}, {BUCKET, P})
        self.assertEqual(view['comparison']['series'][0]['totalTokens'], 204)
        self.assertEqual(view['sourceFolders'], sorted([A, B]))
        empty = self.view(metadata, projects=[P, R], chats=[ROOT])
        self.assertTrue(empty['scope']['empty'])
        self.assertEqual(empty['comparison']['series'][0]['daily'], [])
        self.assertEqual(empty['sourceFolders'], [])

    def test_explicit_names_database_never_discovers_global_state(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not read real global state')):
            metadata = self.resolve()
        self.assertEqual({r['id'] for r in metadata['catalog']}, {P, Q, R, BUCKET})

    def test_explicit_current_project_id_wins_even_without_existing_path(self):
        with closing(sqlite3.connect(self.names)) as db, db:
            db.execute('UPDATE threads SET project_id=? WHERE id=?', (Q, ROOT))
        metadata = self.resolve(global_state_file=self.state(projectless=[ROOT]))
        self.assertEqual(metadata['ownerAssignments'][ROOT]['projectId'], Q)
        self.assertEqual(metadata['ownerAssignments'][ROOT]['reason'], 'current-project-id')
        self.assertEqual(metadata['ownerAssignments'][NESTED]['projectId'], Q)
        self.assert_vector(self.view(metadata, project=Q, chat=ROOT), (169, 85, 35, 12, 204))

    def test_exact_legacy_id_migration_merges_ids_not_names_or_remote_hosts(self):
        metadata = self.resolve(global_state_file=self.state(assignments={ROOT: {'projectKind': 'local', 'projectId': L}}))
        self.assertEqual(metadata['ownerAssignments'][ROOT]['projectId'], P)
        self.assertEqual({row['id'] for row in metadata['catalog']}, {P, Q, R, BUCKET})
        self.assertEqual(next(row for row in metadata['catalog'] if row['id'] == P)['label'], 'Alpha real name')
        self.assertNotIn('PRIVATE', json.dumps(metadata))
        self.assertNotIn('remote-project-id', json.dumps(metadata))

    def test_absent_migration_never_joins_same_name_ids(self):
        state = self.state(assignments={ROOT: {'projectKind': 'local', 'projectId': L}}, migration=False)
        metadata = self.resolve(global_state_file=state)
        self.assertEqual(metadata['ownerAssignments'][ROOT]['projectId'], L)
        self.assertIn(L, {row['id'] for row in metadata['catalog']})
        self.assertEqual(next(row for row in metadata['sourceFolders'] if row['id'] == A)['reason'], 'ambiguous-root')

    def test_projectless_overrides_assigned_legacy_and_named_root(self):
        state = self.state(assignments={ROOT: {'projectKind': 'local', 'projectId': L}}, projectless=[ROOT])
        metadata = self.resolve(global_state_file=state)
        self.assertEqual(metadata['ownerAssignments'][ROOT]['projectId'], BUCKET)
        self.assertEqual(metadata['ownerAssignments'][NESTED]['projectId'], BUCKET)
        self.assert_vector(self.view(metadata, project=BUCKET, chat=ROOT), (169, 85, 35, 12, 204))
        self.assertEqual(self.view(metadata, project=P, chat=ROOT)['totals']['totalTokens'], 0)

    def test_mixed_projectless_same_folder_does_not_reassign_other_chat(self):
        metadata = self.resolve(global_state_file=self.state(projectless=[OTHER]))
        self.assert_vector(self.view(metadata, project=BUCKET), (71, 69, 2, 0, 73))
        self.assert_vector(self.view(metadata, project=P), (144, 82, 20, 6, 164))
        self.assertEqual(self.view(metadata, project=BUCKET)['sourceFolders'], [A])

    def test_old_missing_new_folder_path_maps_only_explicit_owner(self):
        old = copy.deepcopy(self.bundle)
        missing = r'Z:\never-created\new-folder'
        for row in self.bundle['records']:
            if A in row['projects']:
                row['projects'][missing] = row['projects'].pop(A)
                row['projectModels'][missing] = row['projectModels'].pop(A)
        for rank in self.bundle['appProjects']:
            if rank['id'] == A:
                rank['id'] = rank['label'] = missing
        metadata = self.resolve(global_state_file=self.state(assignments={ROOT: {'projectKind': 'local', 'projectId': L}}))
        self.assertEqual(next(row for row in metadata['sourceFolders'] if row['id'] == missing)['projectId'], BUCKET)
        self.assert_vector(self.view(metadata, project=P, chat=ROOT), (169, 85, 35, 12, 204))
        self.assert_vector(self.view(metadata, project=BUCKET, chat=OTHER), (71, 69, 2, 0, 73))
        self.bundle = old

    def test_nested_nearest_parent_overrides_root_but_fork_stays_separate(self):
        with closing(sqlite3.connect(self.names)) as db, db:
            db.execute('INSERT INTO threads(id,project_id) VALUES(?,?)', (AGENT, Q))
        metadata = self.resolve(global_state_file=self.state(assignments={ROOT: {'projectKind': 'local', 'projectId': L}}))
        self.assertEqual(metadata['ownerAssignments'][AGENT]['projectId'], Q)
        self.assertEqual(metadata['ownerAssignments'][NESTED]['inheritedFrom'], AGENT)
        self.assertFalse(metadata['ownerAssignments'][FORK]['wholeOwner'])
        self.assert_vector(self.view(metadata, project=Q, chat=ROOT), (46, 11, 9, 2, 55))
        self.assert_vector(self.view(metadata, project=R, chat=FORK), (19, 6, 3, 2, 22))

    def test_missing_intermediate_parent_uses_exact_verified_chat_root(self):
        self.bundle['owners'] = [row for row in self.bundle['owners'] if row['id'] != AGENT]
        metadata = self.resolve(global_state_file=self.state(assignments={ROOT: {'projectKind': 'local', 'projectId': L}}))
        self.assertEqual(metadata['ownerAssignments'][NESTED]['projectId'], P)
        self.assertEqual(metadata['ownerAssignments'][NESTED]['inheritedFrom'], ROOT)

    def test_parent_from_another_chat_cannot_propagate(self):
        nested = next(row for row in self.bundle['owners'] if row['id'] == NESTED)
        nested['parentId'] = OTHER
        with closing(sqlite3.connect(self.names)) as db, db:
            db.execute('UPDATE threads SET project_id=? WHERE id=?', (Q, OTHER))
        metadata = self.resolve()
        self.assertFalse(metadata['ownerAssignments'][NESTED]['wholeOwner'])

    def test_windows_boundaries_longest_root_case_and_malicious_paths(self):
        roots = [(P, r'C:\safe'), (Q, r'C:\safe\nested')]
        self.assertEqual(projects._root_match(r'c:/SAFE/nested/task', roots)[0], Q)
        self.assertEqual(projects._root_match(r'\\?\C:\safe\nested\task', roots)[0], Q)
        for value in [r'C:\safeevil', r'C:\safe\..\evil', 'safe', '__proto__', 'file://C:/safe', 'C:\\safe\x00tail']:
            self.assertIsNone(projects._root_match(value, roots)[0], value)
        self.assertEqual(projects._root_match('/safe/child', [(P, '/safe')])[0], P)
        self.assertIsNone(projects._root_match('/Safe/child', [(P, '/safe')])[0])
        self.assertEqual(projects._root_match(r'C:\safe', [(P, r'C:\safe'), (Q, r'C:\safe')])[1], 'ambiguous-root')

    def test_unknown_explicit_id_is_bucket_and_cannot_fall_back_to_root(self):
        with closing(sqlite3.connect(self.names)) as db, db:
            db.execute('UPDATE threads SET project_id=? WHERE id=?', ('unknown-id', OTHER))
        metadata = self.resolve()
        self.assertEqual(metadata['ownerAssignments'][OTHER]['projectId'], BUCKET)
        self.assertEqual(metadata['ownerAssignments'][OTHER]['reason'], 'unknown-project-id')

    def test_conflicting_explicit_databases_are_unassigned_not_first_source_wins(self):
        second = self.directory / 'second-project-state.sqlite'
        with closing(sqlite3.connect(self.names)) as db, db:
            db.execute('UPDATE threads SET project_id=? WHERE id=?', (P, ROOT))
        with closing(sqlite3.connect(second)) as db, db:
            db.execute('CREATE TABLE threads(id TEXT, project_id TEXT)')
            db.execute('INSERT INTO threads VALUES(?,?)', (ROOT, Q))
        metadata = projects.resolve_project_metadata(self.bundle, [self.names, second])
        self.assertEqual(metadata['ownerAssignments'][ROOT]['projectId'], BUCKET)
        self.assertEqual(metadata['ownerAssignments'][ROOT]['reason'], 'conflicting-project-assignment')
        self.assertEqual(metadata['ownerAssignments'][NESTED]['projectId'], BUCKET)
        self.assertIn('Conflicting current project assignments', ' '.join(metadata['diagnostics']['warnings']))
        reverse = projects.resolve_project_metadata(self.bundle, [second, self.names])
        self.assertEqual(metadata['ownerAssignments'], reverse['ownerAssignments'])
        self.assert_vector(self.view(metadata, project=BUCKET, chat=ROOT), (169, 85, 35, 12, 204))

    def test_unknown_schemas_fail_closed_and_absent_metadata_is_legacy_identity(self):
        self.assertIs(projects.prepare_project_view(self.bundle), self.bundle)
        self.assertEqual(projects.project_report(self.bundle, '2026-10-24', '2026-10-26', now=NOW),
                         ledger.project_bundle(self.bundle, '2026-10-24', '2026-10-26', now=NOW))
        for change in [lambda m: m.update(schemaVersion=9), lambda m: m.update(mode='guessed'),
                       lambda m: m['catalog'].append(copy.deepcopy(m['catalog'][0])),
                       lambda m: m['ownerAssignments'][ROOT].update(wholeOwner=True, projectId='bad')]:
            bad = self.resolve()
            change(bad)
            with self.assertRaises(core.ReportError):
                self.prepared(bad)

    def test_missing_nested_models_remain_unavailable(self):
        self.bundle['features']['projectModels'] = False
        for row in self.bundle['records']:
            row['projectModels'] = None
        report = self.view(project=P)
        self.assertEqual(report['totals']['totalTokens'], 237)
        self.assertFalse(report['features']['models'])
        self.assertEqual(report['models'], [])

    def test_corrupt_nested_model_part_is_not_reused_as_whole_row_model(self):
        self.bundle['records'][0]['projectModels'][A]['model-a']['reasoningTokens'] += 1
        prepared = self.prepared()
        self.assertFalse(prepared['features']['projectModels'])
        self.assertTrue(all(row['projectModels'] is None for row in prepared['records']))
        self.assertEqual(self.view(project=P)['totals']['totalTokens'], 237)

    def test_missing_raw_slices_disable_grouped_filters_keep_all_totals(self):
        self.bundle['features']['sessionProjects'] = False
        for row in self.bundle['records']:
            row['projects'] = row['projectModels'] = None
        prepared = self.prepared()
        self.assertFalse(prepared['features']['projectFilter'])
        self.assertEqual(prepared['appProjects'], [])
        self.assertEqual(self.view()['totals']['totalTokens'], 299)
        with self.assertRaises(core.ReportError):
            self.view(project=P)

    def test_each_counter_mismatch_in_source_or_independent_app_day_rejected(self):
        for field in core.FIELDS:
            with self.subTest(field=field):
                bad = copy.deepcopy(self.bundle)
                bad['records'][0]['projects'][A][field] += 1
                bad['projectMetadata'] = self.resolve()
                with self.assertRaises(core.ReportError):
                    projects.prepare_project_view(bad)
                bad = copy.deepcopy(self.bundle)
                bad['daily'][0][field] += 1
                bad['projectMetadata'] = self.resolve()
                with self.assertRaises(core.ReportError):
                    projects.prepare_project_view(bad)

    def test_changed_names_roots_refresh_on_numeric_cache_hit(self):
        first = self.resolve()
        with closing(sqlite3.connect(self.names)) as db, db:
            db.execute('UPDATE projects SET name=? WHERE id=?', ('Renamed actual project', P))
            db.execute('UPDATE project_roots SET path=? WHERE project_id=?', (r'Z:\different-root', P))
        cached, _, meta = ledger.build_bundle(self.database, [self.names], cache_file=self.cache, now=NOW)
        self.assertEqual(meta['status'], 'hit')
        second = projects.resolve_project_metadata(cached, [self.names])
        self.assertNotEqual(first, second)
        self.assertEqual(next(row for row in second['catalog'] if row['id'] == P)['label'], 'Renamed actual project')
        self.assertEqual(next(row for row in second['sourceFolders'] if row['id'] == A)['projectId'], BUCKET)
        self.assertEqual(cached['records'], self.bundle['records'])

    def test_selective_metadata_reads_are_readonly_and_do_not_export_private_columns(self):
        state = self.state()
        before = hashlib.sha256(self.names.read_bytes()).hexdigest()
        queries = []
        original = core.connect_readonly

        def traced(path):
            connection = original(path)
            connection.set_trace_callback(queries.append)
            return connection

        with patch.object(core, 'connect_readonly', side_effect=traced):
            metadata = self.resolve(global_state_file=state)
        self.assertEqual(before, hashlib.sha256(self.names.read_bytes()).hexdigest())
        self.assertNotIn('PRIVATE', json.dumps(metadata))
        selects = [query for query in queries if query.lstrip().upper().startswith('SELECT')]
        self.assertTrue(any('SELECT id, project_id FROM threads WHERE' in query for query in selects))
        for query in selects:
            for forbidden in ('title', 'prompt', 'metadata', 'account', 'creator', 'cwd'):
                self.assertNotIn(forbidden, query.lower())

    def test_default_codex_home_and_explicit_state_are_portable(self):
        default = self.directory / '.codex-global-state.json'
        self.state(projectless=[OTHER]).replace(default)
        with patch.dict(os.environ, {'CODEX_HOME': str(self.directory)}):
            metadata = projects.resolve_project_metadata(self.bundle)
        self.assertEqual(metadata['ownerAssignments'][OTHER]['projectId'], BUCKET)


if __name__ == '__main__':
    unittest.main()
