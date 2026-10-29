"""Verified local numeric ledger for a portable, offline usage dashboard.

The original report adapter remains the accounting oracle. Each nonoverlapping
366-day source chunk is checked by it before approved row projections are joined
to its verified owner identities. No rollout files or whole cached JSON values
are read here.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import report_from_app as core

ALGORITHM_VERSION = 'usage-ledger-2026-10-27.1'
MAX_SAFE_INTEGER = 9007199254740991
FIELDS = core.FIELDS
ReportError = core.ReportError


def _values(row):
    result = core.counters(row[field] for field in FIELDS)
    if any(value > MAX_SAFE_INTEGER for value in result.values()):
        raise ReportError('A recorded counter exceeds the exact JavaScript integer limit.')
    return result


def _sum(rows):
    return _values(core.sum_counters(rows))


def _now(now):
    now = now or datetime.now(core.UTC)
    if now.tzinfo is None:
        raise ReportError('The report generation time must include a timezone.')
    return now


def _rankings(day_maps, totals):
    grouped = defaultdict(list)
    for day, mapping in sorted(day_maps.items()):
        for label, values in mapping.items():
            grouped[label].append({'date': day, **_values(values)})
    result = []
    for label, daily in grouped.items():
        values = _sum(daily)
        result.append({'id': label, 'label': label, **values, 'daily': daily,
                       'sharePercent': values['totalTokens']/totals['totalTokens']*100
                       if totals['totalTokens'] else 0})
    return sorted(result, key=lambda row: (-row['totalTokens'], row['id']))


def _app_day_maps(rankings, start=None, end=None):
    grouped = defaultdict(dict)
    for rank in rankings:
        for row in rank['daily']:
            day = row['date']
            if (start is None or start <= day) and (end is None or day <= end):
                if rank['id'] in grouped[day]:
                    raise ReportError('Duplicate independent app ranking day.')
                grouped[day][rank['id']] = _values(row)
    return grouped


def _records_maps(records, kind):
    return {row['key']: row[kind] for row in records}


def _validate(bundle, proof):
    """Validate the small exported snapshot, without touching source counters."""
    if (bundle['schemaVersion'] != 1 or bundle['algorithmVersion'] != ALGORITHM_VERSION
            or proof['algorithmVersion'] != ALGORITHM_VERSION
            or proof['upstreamVersion'] != core.UPSTREAM_VERSION
            or proof['upstreamCommit'] != core.UPSTREAM_COMMIT
            or proof['reconciledFields'] != list(FIELDS)
            or proof['sourceRevisionSha256'] != bundle['source']['revisionSha256']
            or proof['coverage'] != bundle['coverage']
            or proof['numericComputedAt'] != bundle['numericComputedAt']):
        raise ReportError('Invalid ledger identity or original verification proof.')
    for field in ('readOnly', 'allAnalysisLocal', 'mappingOwnerIdsMatch'):
        if proof[field] is not True:
            raise ReportError('Incomplete original ledger proof.')
    if proof['networkUsed'] is not False:
        raise ReportError('Invalid original locality proof.')
    for chunk in proof['validatedChunks']:
        core.selected_window(chunk['startDate'], chunk['endDate'], None, None)
        if chunk['appDailySessionTotalsMatch'] is not True or chunk['appPerDaySessionTotalsMatch'] is not True:
            raise ReportError('Incomplete original chunk verification proof.')
    if proof['attribution'] != {key: bundle['features'][key] for key in
                                ('sessionModels', 'sessionProjects', 'projectModels')}:
        raise ReportError('Invalid original attribution proof.')
    core.parse_time(bundle['numericComputedAt'])
    core.parse_time(bundle['source']['lastScanAt'])
    owners = {row['id']: row for row in bundle['owners']}
    chats = {row['id']: row for row in bundle['chats']}
    if len(owners) != len(bundle['owners']) or len(chats) != len(bundle['chats']):
        raise ReportError('Duplicate ledger catalog identities.')
    for owner in owners.values():
        if (owner['chatId'] not in chats or owner['kind'] not in ('self', 'agent')
                or (owner['kind'] == 'self') != (owner['id'] == owner['chatId'])
                or (owner['kind'] == 'self' and owner['parentId'] is not None)
                or (owner['kind'] == 'agent' and not isinstance(owner['parentId'], str))):
            raise ReportError('Invalid ledger owner family.')
    daily = {}
    for row in bundle['daily']:
        day = core.parse_date(row['date'])
        if day in daily:
            raise ReportError('Duplicate ledger app days.')
        daily[day] = _values(row)
    records, by_day = {}, defaultdict(list)
    for row in bundle['records']:
        if not isinstance(row['key'], str) or row['key'] in records or row['ownerId'] not in owners:
            raise ReportError('Invalid ledger source-row ownership identity.')
        core.parse_date(row['date'])
        records[row['key']] = row
        by_day[row['date']].append(_values(row))
    for day in daily.keys() | by_day.keys():
        if _sum(by_day[day]) != daily.get(day, _sum([])):
            raise ReportError('Ledger app and session daily counters disagree.')
    _sum(daily.values())
    dates = sorted(daily.keys() | by_day.keys())
    coverage = {'startDate': dates[0] if dates else None, 'endDate': dates[-1] if dates else None,
                'recordedDays': sorted(daily), 'historyComplete': True}
    if bundle['coverage'] != coverage:
        raise ReportError('Invalid ledger source coverage.')
    for kind, name in (('models', 'appModels'), ('projects', 'appProjects')):
        available = bundle['features'][kind]
        if type(available) is not bool or (not available and bundle[name]):
            raise ReportError('Invalid ledger ranking availability.')
        if available:
            maps = _app_day_maps(bundle[name])
            if any(_sum(maps[day].values()) != daily.get(day, _sum([]))
                   for day in daily.keys() | maps.keys()):
                raise ReportError('Ledger independent app ranking counters disagree.')
            if _sum(bundle[name]) != _sum(daily.values()):
                raise ReportError('Ledger independent app ranking totals disagree.')
            for row in bundle[name]:
                if _sum(row['daily']) != _values(row):
                    raise ReportError('Ledger app ranking daily totals disagree.')
    for feature, kind, app_name in (('sessionModels', 'models', 'appModels'),
                                    ('sessionProjects', 'projects', 'appProjects')):
        if type(bundle['features'][feature]) is not bool:
            raise ReportError('Invalid ledger attribution availability.')
        if bundle['features'][feature]:
            maps = _records_maps(bundle['records'], kind)
            for mapping in maps.values():
                for values in mapping.values():
                    _values(values)
            core.checked_maps(maps, records, kind,
                              rankings=bundle[app_name] if bundle['features'][kind] else None)
    if bundle['features']['projectFilter'] is not (bundle['features']['projects'] and bundle['features']['sessionProjects']):
        raise ReportError('Invalid ledger project filter availability.')
    if bundle['features']['projectModels']:
        nested = {(*[row['key']], project): mapping for row in bundle['records']
                  for project, mapping in row['projectModels'].items()}
        # checked_project_models accepts arbitrary tuple-shaped record keys.
        keyed = {(key,): value for key, value in records.items()}
        project_maps = {(key,): row['projects'] for key, row in records.items()}
        model_maps = {(key,): row['models'] for key, row in records.items()}
        core.checked_project_models(project_maps, nested, model_maps, keyed, bundle['appModels'])
    return bundle, proof


def _read_numeric(database, metadata, now):
    """One full import. Each row is counted once, with nonoverlapping core chunks."""
    with closing(core.connect_readonly(database)) as db:
        db.execute('BEGIN')
        if db.execute('SELECT COUNT(*) FROM session_file_rollups WHERE NOT json_valid(rows_json)').fetchone()[0]:
            raise ReportError('Malformed session rows in the app cache. '+core.REFRESH_ACTION)
        all_paths = [row[0] for row in db.execute('SELECT path FROM session_file_rollups ORDER BY path')]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        metadata_ids = {}
        if 'project_session_index' in tables:
            metadata_ids = {row['path']: row['agent_id'] for row in db.execute(
                "SELECT path,json_extract(detail_json,'$.agentSessionId') AS agent_id FROM project_session_index")}
        path_ids = {}
        for position, path in enumerate(all_paths, 1):
            match = core.THREAD_ID.search(path)
            path_ids[path] = match.group(1).lower() if match else metadata_ids.get(path) or f'session-{position}'
        selection = ', '.join("json_extract(j.value,'$."+field+"') AS "+dest+
                              ",json_type(j.value,'$."+field+"') AS "+dest+'_type'
                              for field, dest in zip(core.JSON_FIELDS, FIELDS))
        raw_records = {}
        for row in db.execute("SELECT s.path,j.key AS row_index,json_extract(j.value,'$.date') AS day,"+
                              selection+" FROM session_file_rollups s,json_each(s.rows_json) j ORDER BY s.path,j.key"):
            day = core.parse_date(row['day'])
            if any(row[field+'_type'] != 'integer' for field in FIELDS):
                raise ReportError('Invalid counters in the app cache. '+core.REFRESH_ACTION)
            key = (row['path'], row['row_index'])
            if key in raw_records:
                raise ReportError('Duplicate session source row.')
            raw_records[key] = {'date': day, **_values(row)}
        source_days = [core.parse_date(row[0]) for row in db.execute('SELECT date FROM daily_usage_rollups ORDER BY date')]
        source_days = sorted(set(source_days) | {row['date'] for row in raw_records.values()})
        start = source_days[0] if source_days else now.date().isoformat()
        end = source_days[-1] if source_days else start
        project_maps = core.load_session_maps(db, 'projects', start, end)
        model_maps = core.load_session_maps(db, 'models', start, end)
        nested_maps = core.load_session_maps(db, 'models', start, end, all_projects=True)

    owners, chats, daily, app_maps = {}, {}, [], {'models': {}, 'projects': {}}
    features = {'models': True, 'projects': True}
    warnings, chunks = [], []
    cursor, last = date.fromisoformat(start), date.fromisoformat(end)
    while cursor <= last:
        chunk_end = cursor+timedelta(days=min(core.MAX_DAYS-1, (last-cursor).days))
        data, proof = core.load_cache(database, cursor.isoformat(), chunk_end.isoformat(),
                                      names_databases=(), now=now)
        chunks.append({name: proof[name] for name in ('startDate', 'endDate', 'selectedSessions',
                       'appDailySessionTotalsMatch', 'appPerDaySessionTotalsMatch')})
        daily.extend(data['daily'])
        warnings.extend(data['diagnostics']['warnings'])
        for kind in features:
            features[kind] = features[kind] and data['features'][kind]
            app_maps[kind].update(_app_day_maps(data[kind]))
        for owner in data['owners']:
            catalog = {key: owner[key] for key in ('id', 'chatId', 'parentId', 'kind', 'label', 'path', 'model', 'responses')}
            if owner['id'] in owners and owners[owner['id']] != catalog:
                raise ReportError('Core owner metadata changed between source chunks.')
            owners[owner['id']] = catalog
        for chat in data['chats']:
            chats.setdefault(chat['id'], {key: chat[key] for key in ('id', 'label', 'labelSource')})
        # Independently join exact file IDs to the core owner totals for this chunk.
        mapped = defaultdict(list)
        for key, values in raw_records.items():
            if cursor.isoformat() <= values['date'] <= chunk_end.isoformat():
                mapped[path_ids[key[0]]].append(values)
        expected = {row['id']: _values(row) for row in data['owners']}
        if {identity: _sum(rows) for identity, rows in mapped.items()} != expected:
            raise ReportError('File-to-owner mapping disagrees with independently verified core ownership.')
        if chunk_end == last:
            break
        cursor = chunk_end+timedelta(days=1)

    totals = _sum(daily)
    app_models = _rankings(app_maps['models'], totals) if features['models'] else []
    app_projects = _rankings(app_maps['projects'], totals) if features['projects'] else []
    errors = {'sessionModels': None, 'sessionProjects': None, 'projectModels': None}
    for feature, maps, kind, ranks in (('sessionModels', model_maps, 'models', app_models),
                                       ('sessionProjects', project_maps, 'projects', app_projects)):
        try:
            core.checked_maps(maps, raw_records, kind, rankings=ranks if features[kind] else None)
            features[feature] = True
        except ReportError as exc:
            features[feature], errors[feature] = False, str(exc)
    try:
        if not features['sessionProjects'] or not features['sessionModels']:
            raise ReportError('Independent session model/project attribution is unavailable.')
        core.checked_project_models(project_maps, nested_maps, model_maps, raw_records,
                                    app_models if features['models'] else None)
        features['projectModels'] = True
    except ReportError as exc:
        features['projectModels'], errors['projectModels'] = False, str(exc)
    features.update(chatFilter=True, projectFilter=features['projects'] and features['sessionProjects'])
    exported = []
    path_positions = {path: position for position, path in enumerate(all_paths, 1)}
    for key, values in raw_records.items():
        exported.append({'key': str(path_positions[key[0]])+':'+str(key[1]),
                         'ownerId': path_ids[key[0]], **values,
                         'models': model_maps[key] if features['sessionModels'] else None,
                         'projects': project_maps[key] if features['sessionProjects'] else None,
                         'projectModels': {project: nested_maps[(*key, project)] for project in project_maps[key]}
                                          if features['projectModels'] else None})
    for feature, error in errors.items():
        if error:
            warnings.append(feature+' attribution is unavailable: '+error+' '+core.REFRESH_ACTION)
    coverage = {'startDate': source_days[0] if source_days else None,
                'endDate': source_days[-1] if source_days else None,
                'recordedDays': [row['date'] for row in daily], 'historyComplete': True}
    bundle = {'schemaVersion': 1, 'algorithmVersion': ALGORITHM_VERSION,
              'generatedAt': now.isoformat(), 'numericComputedAt': now.isoformat(),
              'source': {'name': 'Codex Usage Desktop', 'version': core.UPSTREAM_VERSION,
                         'lastScanAt': metadata['lastScanAt'], 'timeZone': metadata['timeZone'],
                         'datePrecision': 'day', 'revisionSha256': core.digest(metadata['revision'])},
              'coverage': coverage, 'daily': daily, 'owners': list(owners.values()), 'chats': list(chats.values()),
              'records': exported, 'appModels': app_models, 'appProjects': app_projects, 'features': features,
              'diagnostics': {'status': 'incomplete' if warnings else 'complete',
                              'warnings': list(dict.fromkeys(warnings)), 'attribution': errors}}
    proof = {'algorithmVersion': ALGORITHM_VERSION, 'upstreamVersion': core.UPSTREAM_VERSION,
             'upstreamCommit': core.UPSTREAM_COMMIT, 'databasePath': str(Path(database).expanduser().resolve()),
             'readOnly': True, 'allAnalysisLocal': True, 'networkUsed': False,
             'reconciledFields': list(FIELDS), 'sourceRevisionSha256': core.digest(metadata['revision']),
             'coverage': coverage, 'validatedChunks': chunks, 'mappingOwnerIdsMatch': True,
             'attribution': {key: features[key] for key in errors}, 'numericComputedAt': now.isoformat()}
    return _validate(bundle, proof)


def _load_envelope(path, revision):
    if path is None or not path.exists():
        return None, 'missing_ledger'
    try:
        entry = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=core.strict_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON.')))
        body = {key: value for key, value in entry.items() if key != 'sha256'}
        if (entry['schemaVersion'] != 1 or entry['algorithmVersion'] != ALGORITHM_VERSION
                or entry['sha256'] != core.digest(body)):
            raise ValueError('Invalid ledger envelope.')
        _validate(entry['bundle'], entry['proof'])
        if (entry['bundle']['source']['revisionSha256'] != core.digest(entry['sourceRevision'])
                or entry['proof']['databasePath'] != entry['sourceRevision']['databasePath']):
            raise ValueError('Mismatched ledger source revision identity.')
        if entry['sourceRevision'] != revision:
            return None, 'source_changed'
        return entry, 'unchanged_source'
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OSError, RecursionError, OverflowError):
        return None, 'invalid_ledger'


def _save_envelope(path, entry, preserve_invalid):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(path)+'.lock')
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    temporary, recovery = None, None
    try:
        if preserve_invalid and path.exists():
            recovery = path.with_name(path.name+'.corrupt-'+datetime.now(core.UTC).strftime('%Y%m%dT%H%M%S%fZ')+'.bak')
            with recovery.open('xb') as handle:
                handle.write(path.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
        descriptor, temporary = tempfile.mkstemp(prefix='.'+path.name+'-', suffix='.tmp', dir=path.parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, allow_nan=False, indent=2)+'\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        return recovery
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def build_bundle(database, names_databases=None, *, cache_file=None, now=None, refresh=False):
    """Build/reuse one canonical all-history ledger, with fresh sidebar names."""
    now = _now(now)
    cache_path = Path(cache_file).expanduser().resolve() if cache_file is not None else None
    names_paths = core.discover_names_database() if names_databases is None else list(names_databases)
    protected = [Path(database).expanduser().resolve(), *(Path(path).expanduser().resolve() for path in names_paths)]
    if cache_path is not None and any(str(cache_path) in (str(path), str(path)+'-wal', str(path)+'-shm') for path in protected):
        raise ReportError('The ledger cache must be separate from the read-only source databases.')
    for _ in range(3):
        metadata = core.source_metadata(database)
        entry, reason = _load_envelope(cache_path, metadata['revision'])
        preserve_invalid = reason == 'invalid_ledger'
        hit = entry is not None and not refresh
        if refresh:
            reason = 'refresh_requested'
        if not hit:
            try:
                bundle, proof = _read_numeric(database, metadata, now)
            except ReportError:
                if core.source_revision(database) != metadata['revision']:
                    continue
                raise
            if core.source_revision(database) != metadata['revision']:
                continue
            body = {'schemaVersion': 1, 'algorithmVersion': ALGORITHM_VERSION,
                    'sourceRevision': metadata['revision'], 'bundle': bundle, 'proof': proof}
            entry = {**body, 'sha256': core.digest(body)}
        # Detach so current display names/cache metadata never mutate persisted proof.
        bundle, proof = json.loads(core.canonical_json(entry['bundle'])), json.loads(core.canonical_json(entry['proof']))
        names = core.safe_names(names_paths, {row['id'] for row in bundle['chats']})
        for chat in bundle['chats']:
            chat['label'] = names.get(chat['id'], 'Chat '+chat['id'][:8])
            chat['labelSource'] = 'threads.name' if chat['id'] in names else 'session_id'
        bundle['generatedAt'] = now.isoformat()
        if core.source_revision(database) != metadata['revision']:
            continue
        meta = {'status': 'hit' if hit else 'miss', 'reason': reason,
                'cachePath': str(cache_path) if cache_path is not None else None,
                'algorithmVersion': ALGORITHM_VERSION, 'numericComputedAt': bundle['numericComputedAt'],
                'sourceRevision': core.digest(metadata['revision']), 'verifiedProofSha256': core.digest(proof),
                'historySaved': bool(hit)}
        if not hit and cache_path is not None:
            try:
                recovery = _save_envelope(cache_path, entry, preserve_invalid)
                meta['historySaved'] = True
                if recovery is not None:
                    meta.update(recoveryPath=str(recovery), warning='Unreadable ledger bytes were preserved before replacement.')
            except OSError as exc:
                meta['warning'] = 'The verified ledger could not be saved: '+str(exc)
        bundle['cache'] = meta
        return bundle, proof, meta
    raise ReportError('The app cache kept changing during ledger generation; rerun local refresh.')


def _selection_ids(singular, plural, catalog, *, chats=False):
    """Canonical exact IDs; an explicit empty list means the whole dimension."""
    def identity(value):
        if not isinstance(value, str) or not value.strip():
            raise ReportError('The scope must use a nonempty exact cached identity.')
        return value.lower() if chats and core.SESSION_ID.fullmatch(value) else value

    single = identity(singular) if singular is not None else None
    if plural is None:
        return [single] if single is not None else []
    if not isinstance(plural, list):
        raise ReportError('Scope selections must be arrays of exact cached identities.')
    selected = sorted({identity(value) for value in plural})
    if single is not None and selected != [single]:
        raise ReportError('Singular and multiple scope selections disagree.')
    if any(value not in catalog for value in selected):
        raise ReportError('A selected '+('chat' if chats else 'project')+' identity is not in the saved catalog.')
    return selected


def _comparison_chat_labels(bundle):
    """Keep guardian aliases stable across dates and selected dimensions."""
    owner_chats = {row['id']: row['chatId'] for row in bundle['owners']}
    dates = defaultdict(set)
    for row in bundle['records']:
        dates[owner_chats[row['ownerId']]].add(row['date'])
    labels = {row['id']: row['label'] for row in bundle['chats']}
    reviews = sorted((row for row in bundle['chats'] if row['label'].strip().lower() == 'guardian review'),
                     key=lambda row: (min(dates[row['id']], default='9999'), row['id']))
    for index, row in enumerate(reviews, 1):
        days = sorted(dates[row['id']])
        labels[row['id']] = 'Guardian Review '+str(index)+(' · '+days[0]+(
            ' – '+days[-1] if days[-1] != days[0] else '') if days else '')
    return labels


def project_bundle(bundle, start_date, end_date, project=None, chat=None, now=None, *, projects=None, chats=None):
    """One deduplicated projection: OR within each scope, AND across scopes.

    Singular identities retain the legacy exact-empty behavior for an unknown
    identity. Canonical arrays reject unknown IDs before filtering any records.
    """
    now = _now(now)
    start_date, end_date = core.selected_window(start_date, end_date, None, None)
    owner_catalog = {row['id']: row for row in bundle['owners']}
    chat_catalog = {row['id']: row for row in bundle['chats']}
    project_catalog = {row['id']: row for row in bundle['appProjects']}
    project_ids = _selection_ids(project, projects, project_catalog)
    chat_ids = _selection_ids(chat, chats, chat_catalog, chats=True)
    project_set, chat_set = set(project_ids), set(chat_ids)
    project = project_ids[0] if len(project_ids) == 1 else None
    chat = chat_ids[0] if len(chat_ids) == 1 else None
    scoped = bool(project_ids or chat_ids)
    if project_ids and not bundle['features']['projectFilter']:
        raise ReportError('Project scope is unavailable without independently reconciled session project maps. '+core.REFRESH_ACTION)
    warnings = [warning for warning in bundle['diagnostics']['warnings']
                if not warning.startswith(('The app cache is ', 'The app scan timestamp is ahead '))]
    selected, seen = [], set()
    for row in bundle['records']:
        if row['key'] in seen:
            raise ReportError('Saved records contain a duplicate identity. '+core.REFRESH_ACTION)
        seen.add(row['key'])
        if not start_date <= row['date'] <= end_date:
            continue
        if row['ownerId'] not in owner_catalog:
            raise ReportError('Saved owner attribution is missing. '+core.REFRESH_ACTION)
        if chat_ids and owner_catalog[row['ownerId']]['chatId'] not in chat_set:
            continue
        if project_ids:
            parts = {identity: values for identity, values in (row['projects'] or {}).items() if identity in project_set}
            if not parts:
                continue
            models = defaultdict(list)
            models_available = bundle['features']['projectModels'] and all(
                isinstance((row.get('projectModels') or {}).get(identity), dict) for identity in parts)
            if models_available:
                for identity in parts:
                    for label, values in row['projectModels'][identity].items():
                        models[label].append(values)
            selected.append({**row, **_sum(parts.values()), 'projects': parts,
                             'models': {label: _sum(values) for label, values in models.items()}
                                       if models_available else None})
        else:
            selected.append(row)
    day_parts = defaultdict(list)
    owner_parts = defaultdict(list)
    for row in selected:
        day_parts[row['date']].append(row)
        owner_parts[row['ownerId']].append(row)
    if not scoped:
        daily = [{'date': row['date'], **_values(row)} for row in bundle['daily']
                 if start_date <= row['date'] <= end_date]
    else:
        daily = [{'date': day, **_sum(rows)} for day, rows in sorted(day_parts.items())]
    totals = _sum(daily)
    if totals != _sum(selected):
        raise ReportError('Saved daily and session counters disagree. '+core.REFRESH_ACTION)
    owners = [{**owner_catalog[identity], **_sum(rows)} for identity, rows in owner_parts.items()]
    # Match the original stable core catalog order for tied totals.
    positions = {row['id']: index for index, row in enumerate(bundle['owners'])}
    owners.sort(key=lambda row: (-row['totalTokens'], positions[row['id']]))
    grouped, chat_days = defaultdict(list), defaultdict(lambda: defaultdict(list))
    for owner in owners:
        grouped[owner['chatId']].append(owner)
        for row in owner_parts[owner['id']]:
            chat_days[owner['chatId']][row['date']].append(row)
    chats = []
    for identity, parts in grouped.items():
        chats.append({**chat_catalog[identity], **_sum(parts), 'owners': [row['id'] for row in parts],
                      'daily': [{'date': day, **_sum(rows)} for day, rows in sorted(chat_days[identity].items())],
                      'selfTokens': sum(row['totalTokens'] for row in parts if row['kind'] == 'self'),
                      'agentsTokens': sum(row['totalTokens'] for row in parts if row['kind'] == 'agent'),
                      'agentCount': sum(row['kind'] == 'agent' for row in parts), 'responses': None})
    chats.sort(key=lambda row: -row['totalTokens'])
    for row in [*owners, *chats]:
        row['sharePercent'] = row['totalTokens']/totals['totalTokens']*100 if totals['totalTokens'] else 0
    ranks, available = {}, {}
    for kind, app_name, session_feature in (('models', 'appModels', 'sessionModels'),
                                           ('projects', 'appProjects', 'sessionProjects')):
        if not scoped:
            available[kind] = bundle['features'][kind]
            maps = _app_day_maps(bundle[app_name], start_date, end_date)
        else:
            available[kind] = (bundle['features']['projectModels'] if kind == 'models' and project_ids
                               else True if kind == 'projects' and project_ids
                               else bundle['features'][session_feature])
            available[kind] = available[kind] and all(row.get(kind) is not None for row in selected)
            maps = defaultdict(lambda: defaultdict(list))
            if available[kind]:
                for row in selected:
                    mapping = row[kind]
                    for label, values in mapping.items():
                        maps[row['date']][label].append(values)
                maps = {day: {label: _sum(parts) for label, parts in labels.items()} for day, labels in maps.items()}
            if not selected:
                available[kind] = True  # Exact empty scope has a verified empty distribution.
        ranks[kind] = _rankings(maps, totals) if available[kind] else []
        if not available[kind]:
            warnings.append('Scoped '+kind+' rankings are unavailable because independently verified attribution is missing.')
    labels = bundle.get('projectView', {}).get('labels', {})
    for row in ranks['projects']:
        row['label'] = labels.get(row['id'], row['id'])
    chat_label = chat_catalog.get(chat, {}).get('label', 'Chat '+chat[:8] if chat else '')
    project_label = labels.get(project, project)
    scope = ({'kind': 'project_chat', 'id': None, 'projectId': project, 'chatId': chat,
              'label': project_label+' • '+chat_label} if len(project_ids) == 1 and len(chat_ids) == 1
             else {'kind': 'project', 'id': project, 'label': project_label} if len(project_ids) == 1 and not chat_ids
             else {'kind': 'chat', 'id': chat, 'label': chat_label} if len(chat_ids) == 1 and not project_ids
             else {'kind': 'selection', 'id': None, 'projectIds': project_ids, 'chatIds': chat_ids,
                   'label': ' • '.join(filter(None, [', '.join(labels.get(identity, identity) for identity in project_ids),
                                     ', '.join(chat_catalog.get(identity, {}).get('label', 'Chat '+identity[:8]) for identity in chat_ids)]))} if scoped
             else {'kind': 'all', 'id': None, 'label': 'All chats and named projects' if bundle.get('projectView') else 'All chats and project folders'})
    scope['empty'] = not bool(selected)
    comparison_kind = 'chats' if chat_ids else 'projects' if project_ids else None
    chat_labels = _comparison_chat_labels(bundle)
    comparison = []
    for identity in chat_ids or project_ids:
        parts = ([row for row in selected if owner_catalog[row['ownerId']]['chatId'] == identity]
                 if chat_ids else [dict(date=row['date'], **row['projects'][identity])
                                   for row in selected if identity in row['projects']])
        days = defaultdict(list)
        for row in parts:
            days[row['date']].append(row)
        comparison.append({'id': identity, 'label': chat_labels.get(identity, 'Chat '+identity[:8]) if chat_ids
                           else labels.get(identity, project_catalog.get(identity, {}).get('label', identity)),
                           **_sum(parts), 'daily': [{'date': day, **_sum(values)} for day, values in sorted(days.items())]})
    totals.update(responses=None, sessions=len(owners),
                  uncachedInputTokens=totals['inputTokens']-totals['cachedInputTokens']
                  if totals['cachedInputTokens'] <= totals['inputTokens'] else None)
    day_count = (date.fromisoformat(end_date)-date.fromisoformat(start_date)).days+1
    daily_by_day = {row['date']: row for row in daily}
    trend_days = []
    for offset in range(day_count):
        day = (date.fromisoformat(start_date)+timedelta(days=offset)).isoformat()
        trend_days.append({'date': day, 'recorded': day in daily_by_day,
                           **{field: daily_by_day.get(day, {}).get(field) for field in FIELDS}})
    zone = bundle['source']['timeZone']
    label = start_date if start_date == end_date else start_date+' – '+end_date
    result = {'schemaVersion': 1, 'mode': 'Codex Usage Desktop cache', 'generatedAt': now.isoformat(),
              'scope': scope, 'source': {key: value for key, value in bundle['source'].items() if key != 'revisionSha256'},
              'window': {'label': label+' • full app day'+('s' if start_date != end_date else ''),
                         'startDate': start_date, 'endDate': end_date, 'timeZone': zone, 'precision': 'day'},
              'totals': totals, 'chats': chats, 'owners': owners, 'daily': daily,
              'selection': {'projects': project_ids, 'chats': chat_ids},
              'comparison': {'kind': comparison_kind, 'series': comparison},
              'trend': {'startDate': start_date, 'endDate': end_date, 'dayCount': day_count,
                        'timeZone': zone, 'days': trend_days},
              'models': ranks['models'], 'projects': ranks['projects'], 'features': available,
              'diagnostics': {'status': 'incomplete' if warnings else 'complete', 'warnings': list(dict.fromkeys(warnings))},
              'notes': [core.CAVEAT, 'App totals are shown as recorded. Cached input and reasoning are not added again.',
                        'The app cache has calendar-day precision; exact hourly or last-eight-hours totals are unavailable.',
                        'All adapter processing is local. No account quota or credit balance is inferred.'],
              'cache': bundle.get('cache', {})}
    metadata = {key: bundle['source'][key] for key in ('lastScanAt', 'timeZone')}
    # The core supplies clock/partial-day diagnostics without any source/state read.
    # Preserve approved current catalog names after its intentionally empty name lookup.
    saved_scope = dict(scope)
    display_proof = {'generatedAt': now.isoformat(), 'scope': scope}
    core.refresh_display(result, display_proof, metadata, now, ())
    for row in result['chats']:
        row.update({key: chat_catalog[row['id']][key] for key in ('label', 'labelSource')})
    result['scope'] = saved_scope
    result['scopeNote'] = 'Report scope: '+saved_scope['label']+'. '+core.CAVEAT+(
        ' No usage records match this scope and date range.' if saved_scope['empty'] else '')
    return result
