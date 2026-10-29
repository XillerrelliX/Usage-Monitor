"""Exact cross-runtime projections from approved synthetic fixtures only."""
import copy
import itertools
import json
import shutil
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import dashboard_data as ledger
import project_metadata


@unittest.skipUnless(shutil.which('node'), 'Node is required for browser parity checks.')
class MultiselectParityTests(unittest.TestCase):
    def assert_parity(self, bundle, selections, invalid, now):
        cases = []
        for index, selection in enumerate(selections):
            actual = project_metadata.project_report(bundle, selection['startDate'], selection['endDate'],
                now=datetime.fromisoformat(now.replace('Z', '+00:00')),
                **{key: value for key, value in selection.items() if key not in ('startDate', 'endDate')})
            cases.append({'name': 'selection-'+str(index), 'selection': selection, 'report': actual})
        for selection in invalid:
            with self.subTest(invalid=selection), self.assertRaises(ledger.ReportError):
                project_metadata.project_report(bundle, selection['startDate'], selection['endDate'],
                    now=datetime.fromisoformat(now.replace('Z', '+00:00')),
                    **{key: value for key, value in selection.items() if key not in ('startDate', 'endDate')})
        result = subprocess.run([shutil.which('node'), str(ROOT/'tests/test_multiselect_parity.mjs')],
            input=json.dumps({'bundle': bundle, 'now': now, 'cases': cases, 'invalid': invalid}),
            encoding='utf-8', capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+'\n'+result.stderr)
        self.assertIn('multiselect parity', result.stdout)

    def test_named_project_all_unions_chat_intersections_and_sparse_dates(self):
        fixture = json.loads((ROOT/'tests/fixtures/project-metadata.fixture.json').read_text(encoding='utf-8'))
        bundle, ids, projects = fixture['bundle'], fixture['ids'], fixture['projectIds']
        base = {'startDate': '2026-10-21', 'endDate': '2026-10-27'}
        selections = []
        project_ids = list(projects.values())
        for count in range(len(project_ids)+1):
            for selection in itertools.combinations(project_ids, count):
                for chats in ([], [ids['A'], ids['D']], [ids['A'], ids['E'], ids['H'], ids['R']]):
                    selections.append({**base, 'projects': list(reversed(selection)), 'chats': chats})
        for start, end in [('2026-10-24', '2026-10-24'), ('2026-10-28', '2026-10-30'),
                           ('2026-10-23', '2026-10-26'), ('2025-10-27', '2026-10-27')]:
            selections.append({'startDate': start, 'endDate': end, 'projects': [projects['alpha'], projects['beta']],
                               'chats': [ids['A'], ids['D'], ids['R'], ids['RR']]})
        selections.extend([
            {**base, 'projects': [projects['alpha']]*2, 'chats': [ids['A'], ids['A'].upper()]},
            {**base, 'projects': [projects['beta']], 'chats': [ids['A'], ids['H']]},
            {**base, 'project': projects['alpha'], 'projects': [projects['alpha']]*2,
             'chat': ids['A'], 'chats': [ids['A']]},
        ])
        invalid = [{**base, **selection} for selection in (
            {'projects': [projects['alpha'], 'unknown']}, {'chats': [ids['A'], 'unknown']},
            {'projects': [fixture['folders']['alpha']]}, {'projects': [None]}, {'chats': [False]},
            {'projects': 'unknown'}, {'chats': ids['A']}, {'project': projects['alpha'], 'projects': []},
            {'chat': ids['A'], 'chats': [ids['D']]}, {'startDate': '2026-10-28'},
            {'startDate': '2025-10-26'}, {'startDate': '2026-02-30'})]
        self.assert_parity(bundle, selections, invalid, fixture['now'])

    def test_exact_safe_integer_boundary_for_multiple_record_slices(self):
        fixture = json.loads((ROOT/'tests/fixtures/browser-bundle.fixture.json').read_text(encoding='utf-8'))
        bundle = copy.deepcopy(fixture)
        bundle.pop('projectMetadata', None)
        owner = bundle['owners'][0]
        chat = owner['chatId']
        first = dict(zip(ledger.FIELDS, [9007199254739000, 4, 991, 500, 9007199254739991]))
        second = dict(zip(ledger.FIELDS, [1000, 0, 0, 0, 1000]))
        values = ledger._sum([first, second])
        bundle['records'] = [{'key': 'safe-integer', 'ownerId': owner['id'], 'date': '2026-10-24', **values,
            'projects': {'one': first, 'two': second}, 'models': {'m': values},
            'projectModels': {'one': {'m': first}, 'two': {'m': second}}}]
        bundle['daily'] = [{'date': '2026-10-24', **values}]
        bundle['appModels'] = [{'id': 'm', 'label': 'm', **values, 'daily': bundle['daily']}]
        bundle['appProjects'] = [{'id': key, 'label': key, **part, 'daily': [{'date': '2026-10-24', **part}]}
                                 for key, part in [('one', first), ('two', second)]]
        base = {'startDate': '2026-10-23', 'endDate': '2026-10-25'}
        self.assert_parity(bundle, [base, {**base, 'projects': ['two', 'one', 'one']},
            {**base, 'projects': ['two', 'one'], 'chats': [chat, chat]},
            {**base, 'projects': ['one'], 'chats': [chat]}], [], '2026-10-27T12:00:00Z')


if __name__ == '__main__':
    unittest.main()
