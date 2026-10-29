"""Selective local project labels and a pure, reconciled project display view.

Numeric cache records remain the accounting authority. This module never reads
rollouts, chat text, credentials, account metadata, or repository configuration.
"""
from __future__ import annotations

import copy
import json
import ntpath
import os
import posixpath
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

import dashboard_data as ledger
import report_from_app as core

SCHEMA_VERSION = 1
MODE = 'named-projects'
UNASSIGNED_ID = '__no_named_project__'
UNASSIGNED_LABEL = 'No named project'
FIELDS = core.FIELDS
ReportError = core.ReportError
UNAVAILABLE_WARNING = 'Named project attribution is unavailable without independently verified source-folder slices.'


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and '\x00' not in value


def _thread_id(value):
    return value.lower() if isinstance(value, str) and core.SESSION_ID.fullmatch(value) else value


def _path_key(value):
    """Normalize only absolute local path syntax, retaining directory boundaries."""
    if not _text(value):
        return None
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    windows = value.replace('/', '\\')
    if ntpath.isabs(windows) and (ntpath.splitdrive(windows)[0] or windows.startswith('\\\\')):
        return ('windows', ntpath.normpath(windows).casefold().rstrip('\\') + '\\')
    if value.startswith('/') and not value.startswith('//'):
        return ('posix', posixpath.normpath(value).rstrip('/') + '/')
    return None


def _root_match(folder, roots):
    key = _path_key(folder)
    if key is None:
        return None, 'no-verified-root', None
    candidates = []
    for identity, root in roots:
        root_key = _path_key(root)
        if root_key and key[0] == root_key[0] and key[1].startswith(root_key[1]):
            candidates.append((len(root_key[1]), identity, root))
    if not candidates:
        return None, 'no-verified-root', None
    longest = max(item[0] for item in candidates)
    winners = [item for item in candidates if item[0] == longest]
    if len({item[1] for item in winners}) != 1:
        return None, 'ambiguous-root', None
    _, identity, root = sorted(winners, key=lambda item: item[2])[0]
    return identity, 'verified-root', root


def _decision(identity, reason, source, inherited_from=None):
    result = {'projectId': identity, 'wholeOwner': True, 'reason': reason, 'source': source}
    if inherited_from is not None:
        result['inheritedFrom'] = inherited_from
    return result


def resolve_project_metadata(bundle, names_databases=None, *, global_state_file=None):
    """Refresh optional project metadata independently of the numeric ledger cache.

    Explicit names database arguments form an isolation boundary: they suppress
    automatic global-state discovery unless an explicit state file is supplied.
    """
    paths = core.discover_names_database() if names_databases is None else list(names_databases)
    owner_catalog = {row['id']: row for row in bundle['owners']}
    wanted = {_thread_id(identity) for owner in owner_catalog.values()
              for identity in (owner['id'], owner.get('parentId'), owner.get('chatId')) if _text(identity)}
    projects, current_assignments, membership_conflicts, warnings = {}, {}, set(), []

    def add_project(identity, label, roots, source):
        if not _text(identity) or identity == UNASSIGNED_ID or not _text(label):
            return
        selected = projects.setdefault(identity, {'id': identity, 'label': label.strip(), 'kind': 'named',
                                                  'roots': [], 'source': []})
        if source not in selected['source']:
            selected['source'].append(source)
        for root in roots:
            if _path_key(root) is not None and root not in selected['roots']:
                selected['roots'].append(root)

    for path in paths:
        try:
            with closing(core.connect_readonly(path)) as db:
                tables = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                project_columns = ({row['name'] for row in db.execute('PRAGMA table_info(projects)')}
                                   if 'projects' in tables else set())
                root_columns = ({row['name'] for row in db.execute('PRAGMA table_info(project_roots)')}
                                if 'project_roots' in tables else set())
                roots = defaultdict(list)
                if {'project_id', 'path'} <= root_columns:
                    for row in db.execute('SELECT project_id, path FROM project_roots'):
                        roots[row['project_id']].append(row['path'])
                if {'id', 'name'} <= project_columns:
                    for row in db.execute('SELECT id, name FROM projects'):
                        add_project(row['id'], row['name'], roots.get(row['id'], []), 'state.projects/project_roots')
                columns = ({row['name'] for row in db.execute('PRAGMA table_info(threads)')}
                           if 'threads' in tables else set())
                if {'id', 'project_id'} <= columns:
                    identities = sorted(wanted)
                    for position in range(0, len(identities), 400):
                        chunk = identities[position:position + 400]
                        query = 'SELECT id, project_id FROM threads WHERE id COLLATE NOCASE IN (' + ','.join('?' for _ in chunk) + ')'
                        for row in db.execute(query, chunk):
                            thread = _thread_id(row['id'])
                            if row['project_id'] is not None:
                                if thread in current_assignments and current_assignments[thread] != row['project_id']:
                                    membership_conflicts.add(thread)
                                current_assignments.setdefault(thread, row['project_id'])
        except (OSError, sqlite3.Error, ReportError, TypeError):
            warnings.append('An optional local project metadata database could not be read.')

    state = {}
    if global_state_file is not None:
        state_path = Path(global_state_file).expanduser().resolve()
    elif names_databases is None:
        codex_directory = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()
        state_path = (codex_directory / '.codex-global-state.json').resolve()
    else:
        state_path = None
    if state_path is not None and state_path.is_file():
        try:
            value = json.loads(state_path.read_text(encoding='utf-8'), object_pairs_hook=core.strict_object)
            if isinstance(value, dict):
                state = value
            else:
                warnings.append('The optional local project state has an unsupported schema.')
        except (OSError, ValueError, TypeError, ReportError):
            warnings.append('The optional local project state could not be read.')

    # Select one exact local host key. Never merge identities from remote hosts.
    host_key = 'local:' + str(state_path.parent) if state_path is not None else None
    host_maps = state.get('app-server-project-id-by-legacy-project-id-by-host', {})
    migration = host_maps.get(host_key, {}) if isinstance(host_maps, dict) and host_key is not None else {}
    migration = migration if isinstance(migration, dict) else {}
    legacy = state.get('local-projects', {})
    legacy = legacy if isinstance(legacy, dict) else {}
    legacy_ids = {}
    for legacy_key, value in legacy.items():
        if not isinstance(value, dict) or value.get('id') != legacy_key:
            continue
        identity = migration.get(legacy_key, legacy_key)
        if not _text(identity):
            continue
        roots = value.get('rootPaths', [])
        roots = roots if isinstance(roots, list) else []
        add_project(identity, value.get('name'), roots, 'global-state.local-projects')
        if identity in projects:
            legacy_ids[legacy_key] = identity

    projectless = state.get('projectless-thread-ids', [])
    projectless = {_thread_id(identity) for identity in projectless if _text(identity)} if isinstance(projectless, list) else set()
    legacy_assignments = state.get('thread-project-assignments', {})
    legacy_assignments = legacy_assignments if isinstance(legacy_assignments, dict) else {}
    direct = {}
    if membership_conflicts:
        warnings.append('Conflicting current project assignments were found for ' + str(len(membership_conflicts))
                        + ' scoped owner identities; those identities are shown under No named project.')
    for identity in wanted:
        if identity in membership_conflicts:
            direct[identity] = _decision(UNASSIGNED_ID, 'conflicting-project-assignment', 'current-db')
        elif identity in current_assignments:
            current = current_assignments[identity]
            direct[identity] = _decision(current if isinstance(current, str) and current in projects else UNASSIGNED_ID,
                                         'current-project-id' if isinstance(current, str) and current in projects else 'unknown-project-id', 'current-db')
        elif identity in projectless:
            direct[identity] = _decision(UNASSIGNED_ID, 'explicit-projectless', 'global-state')
        else:
            assignment = legacy_assignments.get(identity)
            if isinstance(assignment, dict) and assignment.get('projectKind') == 'local':
                project = legacy_ids.get(assignment.get('projectId')) if isinstance(assignment.get('projectId'), str) else None
                direct[identity] = _decision(project or UNASSIGNED_ID,
                                             'legacy-project-assignment' if project else 'unknown-project-id', 'global-state')

    decisions, visiting = {}, set()

    def owner_decision(identity):
        if identity in decisions:
            return decisions[identity]
        if identity in direct:
            return direct[identity]
        owner = owner_catalog.get(identity)
        if owner is None or identity in visiting:
            return None
        visiting.add(identity)
        result = None
        if owner.get('kind') == 'agent':
            parent_id = owner.get('parentId')
            parent = owner_catalog.get(parent_id)
            if parent_id and (parent is None or parent.get('chatId') == owner.get('chatId')):
                result = owner_decision(parent_id)
                if result:
                    result = _decision(result['projectId'], 'inherited-project', 'inherited', parent_id)
            root_id = owner.get('chatId')
            if result is None and root_id != identity:
                root = owner_catalog.get(root_id)
                if root is None or (root.get('kind') == 'self' and root.get('chatId') == root_id):
                    root_result = owner_decision(root_id)
                    if root_result:
                        result = _decision(root_result['projectId'], 'inherited-project', 'inherited', root_id)
        visiting.remove(identity)
        if result is not None:
            decisions[identity] = result
        return result

    owner_assignments = {}
    for identity in owner_catalog:
        owner_assignments[identity] = owner_decision(identity) or {
            'projectId': None, 'wholeOwner': False, 'reason': 'folder-slices', 'source': 'source-folders'}

    roots = [(project['id'], root) for project in projects.values() for root in project['roots']]
    raw_keys = {ranking['id'] for ranking in bundle.get('appProjects', []) if _text(ranking.get('id'))}
    for row in bundle['records']:
        if isinstance(row.get('projects'), dict):
            raw_keys.update(row['projects'])
    folders = []
    for raw in sorted(raw_keys):
        identity, reason, matched_root = _root_match(raw, roots)
        item = {'id': raw, 'projectId': identity or UNASSIGNED_ID, 'reason': reason}
        if matched_root is not None:
            item['matchedRoot'] = matched_root
        folders.append(item)
    catalog = sorted(projects.values(), key=lambda item: (item['label'].casefold(), item['id']))
    catalog.append({'id': UNASSIGNED_ID, 'label': UNASSIGNED_LABEL, 'kind': 'unassigned', 'roots': [], 'source': []})
    return {'schemaVersion': SCHEMA_VERSION, 'mode': MODE, 'unassignedId': UNASSIGNED_ID,
            'catalog': catalog, 'ownerAssignments': owner_assignments, 'sourceFolders': folders,
            'diagnostics': {'warnings': list(dict.fromkeys(warnings))}}


def _counter(row):
    try:
        result = core.counters(row[field] for field in FIELDS)
    except (KeyError, TypeError) as exc:
        raise ReportError('Invalid project-view counters.') from exc
    if any(value > ledger.MAX_SAFE_INTEGER for value in result.values()):
        raise ReportError('A project-view counter exceeds the exact JavaScript integer limit.')
    return result


def _sum(rows):
    return _counter(core.sum_counters(list(rows)))


def _add(mapping, identity, values):
    mapping[identity] = _sum([mapping[identity], values]) if identity in mapping else _counter(values)


def _validate_metadata(metadata):
    if (not isinstance(metadata, dict) or metadata.get('schemaVersion') != SCHEMA_VERSION
            or metadata.get('mode') != MODE or metadata.get('unassignedId') != UNASSIGNED_ID):
        raise ReportError('Unsupported named project metadata schema.')
    if (not isinstance(metadata.get('catalog'), list) or not isinstance(metadata.get('ownerAssignments'), dict)
            or not isinstance(metadata.get('sourceFolders'), list)):
        raise ReportError('Invalid named project metadata structure.')
    catalog = {}
    for row in metadata['catalog']:
        if (not isinstance(row, dict) or not _text(row.get('id')) or not _text(row.get('label'))
                or row['id'] in catalog or row.get('kind') not in ('named', 'unassigned')
                or not isinstance(row.get('roots'), list) or not isinstance(row.get('source'), list)):
            raise ReportError('Invalid named project catalog.')
        catalog[row['id']] = row
    if UNASSIGNED_ID not in catalog or catalog[UNASSIGNED_ID]['kind'] != 'unassigned':
        raise ReportError('Missing named project fallback catalog.')
    folders = {}
    for row in metadata['sourceFolders']:
        if (not isinstance(row, dict) or not _text(row.get('id')) or row['id'] in folders
                or (row.get('projectId') is not None and row.get('projectId') not in catalog)):
            raise ReportError('Invalid source-folder project decision.')
        folders[row['id']] = row
    for value in metadata['ownerAssignments'].values():
        if (not isinstance(value, dict) or type(value.get('wholeOwner')) is not bool
                or (value['wholeOwner'] and value.get('projectId') is not None and value.get('projectId') not in catalog)
                or (not value['wholeOwner'] and value.get('projectId') is not None)):
            raise ReportError('Invalid owner project decision.')
    return catalog, folders


def _raw_model_parts_valid(bundle):
    if not bundle['features'].get('projectModels') or not bundle['features'].get('sessionModels'):
        return False
    try:
        model_days = defaultdict(dict)
        for row in bundle['records']:
            projects, nested, models = row['projects'], row['projectModels'], row['models']
            if not isinstance(nested, dict) or not isinstance(models, dict) or set(nested) != set(projects):
                return False
            combined = {}
            for project, values in projects.items():
                if not isinstance(nested[project], dict) or _sum(nested[project].values()) != _counter(values):
                    return False
                for model, part in nested[project].items():
                    _add(combined, model, part)
            if combined != {model: _counter(part) for model, part in models.items()} or _sum(models.values()) != _counter(row):
                return False
            for model, part in models.items():
                _add(model_days[row['date']], model, part)
        if bundle['features'].get('models'):
            independent = ledger._app_day_maps(bundle['appModels'])
            if dict(model_days) != dict(independent):
                return False
        return True
    except (KeyError, TypeError, ValueError, ReportError):
        return False


def prepare_project_view(bundle):
    """Return a grouped copy, leaving the raw numeric ledger and proof untouched."""
    if 'projectMetadata' not in bundle:
        return bundle
    metadata = bundle['projectMetadata']
    catalog, folders = _validate_metadata(metadata)
    if bundle.get('projectView', {}).get('mode') == MODE:
        return copy.deepcopy(bundle)
    view = copy.deepcopy(bundle)
    view['sourceAppProjects'] = copy.deepcopy(bundle['appProjects'])
    view['projectView'] = {'schemaVersion': SCHEMA_VERSION, 'mode': MODE,
                           'labels': {identity: row['label'] for identity, row in catalog.items()},
                           'unassignedId': UNASSIGNED_ID}
    for row in view['records']:
        row['sourceProjects'] = copy.deepcopy(row.get('projects'))
        row['sourceProjectModels'] = copy.deepcopy(row.get('projectModels'))
        row['sourceFolders'] = sorted(row['projects']) if isinstance(row.get('projects'), dict) else []
    if not (bundle['features'].get('projects') and bundle['features'].get('sessionProjects')):
        view['appProjects'] = []
        for key in ('projects', 'sessionProjects', 'projectFilter', 'projectModels'):
            view['features'][key] = False
        for row in view['records']:
            row['projects'], row['projectModels'], row['projectSources'] = None, None, {}
        view['diagnostics']['warnings'].append(UNAVAILABLE_WARNING)
        view['diagnostics']['status'] = 'incomplete'
        return view

    model_available = _raw_model_parts_valid(bundle)
    daily_parts, source_folders, provenance = defaultdict(dict), defaultdict(set), defaultdict(set)
    raw_project_days = defaultdict(dict)
    raw_days = defaultdict(list)
    for source_row, row in zip(bundle['records'], view['records']):
        original = source_row.get('projects')
        if not isinstance(original, dict) or _sum(original.values()) != _counter(source_row):
            raise ReportError('Source-folder slices disagree with their verified record counters.')
        assignment = metadata['ownerAssignments'].get(row['ownerId'], {})
        grouped, nested, details = {}, defaultdict(dict), {}
        for raw, values in original.items():
            decision = folders.get(raw)
            if assignment.get('wholeOwner') is True:
                identity = assignment.get('projectId') or UNASSIGNED_ID
                reason = assignment.get('reason') or 'owner-assignment'
            elif decision is not None:
                identity = decision.get('projectId') or UNASSIGNED_ID
                reason = decision.get('reason') or 'source-folder'
            else:
                identity, reason = UNASSIGNED_ID, 'unresolved-source-folder'
            _add(grouped, identity, values)
            _add(raw_project_days[row['date']], raw, values)
            detail = details.setdefault(identity, {'sourceFolders': set(), 'provenance': set()})
            detail['sourceFolders'].add(raw)
            detail['provenance'].add(reason)
            source_folders[identity].add(raw)
            provenance[identity].add(reason)
            if model_available:
                for model, part in source_row['projectModels'][raw].items():
                    _add(nested[identity], model, part)
        if _sum(grouped.values()) != _counter(source_row):
            raise ReportError('Grouped project slices disagree with their verified record counters.')
        row['projects'] = grouped
        row['projectModels'] = dict(nested) if model_available else None
        row['projectSources'] = {identity: {key: sorted(values) for key, values in detail.items()}
                                 for identity, detail in details.items()}
        for identity, values in grouped.items():
            _add(daily_parts[row['date']], identity, values)
        raw_days[row['date']].append(source_row)
    independent_days = {row['date']: _counter(row) for row in bundle['daily']}
    if len(independent_days) != len(bundle['daily']):
        raise ReportError('Duplicate independent app project-view day.')
    independent_raw_projects = ledger._app_day_maps(bundle['appProjects'])
    for ranking in bundle['appProjects']:
        if _sum(ranking['daily']) != _counter(ranking):
            raise ReportError('Independent source-folder ranking period counters disagree.')
    for day in raw_project_days.keys() | independent_raw_projects.keys():
        if raw_project_days.get(day, {}) != independent_raw_projects.get(day, {}):
            raise ReportError('Source-folder slice days disagree with independent app project rankings.')
    for day in independent_days.keys() | daily_parts.keys() | raw_days.keys():
        expected = independent_days.get(day, _sum([]))
        if _sum(daily_parts[day].values()) != expected or _sum(raw_days[day]) != expected:
            raise ReportError('Named project daily totals disagree with independent app counters.')
    totals = _sum(bundle['daily'])
    view['appProjects'] = ledger._rankings(daily_parts, totals)
    for row in view['appProjects']:
        row['label'] = catalog[row['id']]['label']
        row['sourceFolders'] = sorted(source_folders[row['id']])
        row['provenance'] = sorted(provenance[row['id']])
    if _sum(view['appProjects']) != totals:
        raise ReportError('Named project overall totals disagree with independent app counters.')
    view['features'].update(projects=True, sessionProjects=True, projectFilter=True, projectModels=model_available)
    if bundle['features'].get('projectModels') and not model_available:
        view['diagnostics']['warnings'].append('Named project model rankings are unavailable because independently verified nested source parts are missing.')
        view['diagnostics']['status'] = 'incomplete'
    return view


def project_report(bundle, start_date, end_date, project=None, chat=None, now=None, *, projects=None, chats=None):
    """Project the prepared copy, adding friendly names and exact source detail."""
    prepared = prepare_project_view(bundle)
    result = ledger.project_bundle(prepared, start_date, end_date, project, chat, now, projects=projects, chats=chats)
    if 'projectMetadata' not in bundle:
        return result
    metadata = prepared['projectMetadata']
    labels = prepared['projectView']['labels']
    owners = {row['id']: row for row in prepared['owners']}
    sources, reasons = defaultdict(set), defaultdict(set)
    project_ids = set(result['selection']['projects'])
    chat_ids = set(result['selection']['chats'])
    for row in prepared['records']:
        if not start_date <= row['date'] <= end_date:
            continue
        if chat_ids and owners[row['ownerId']]['chatId'] not in chat_ids:
            continue
        if project_ids and (not isinstance(row.get('projects'), dict) or not project_ids.intersection(row['projects'])):
            continue
        for identity, detail in row.get('projectSources', {}).items():
            if not project_ids or identity in project_ids:
                sources[identity].update(detail['sourceFolders'])
                reasons[identity].update(detail['provenance'])
    for row in result['projects']:
        identity = row['id']
        row.update(label=labels.get(identity, identity), sourceFolders=sorted(sources[identity]), provenance=sorted(reasons[identity]))
    all_sources = sorted(set().union(*sources.values())) if sources else []
    all_reasons = sorted(set().union(*reasons.values())) if reasons else []
    result['sourceFolders'] = all_sources
    result['projectAttribution'] = {'mode': MODE, 'unassignedId': metadata['unassignedId'],
                                    'sourceFolders': all_sources, 'provenance': all_reasons}
    result['scopeNote'] = 'Report scope: ' + result['scope']['label'] + '. ' + core.CAVEAT + (
        ' No usage records match this scope and date range.' if result['scope']['empty'] else '')
    return result
