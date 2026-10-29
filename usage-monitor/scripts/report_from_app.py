"""Read Codex Usage Desktop v4.2.0's local cache; never scan rollout logs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
UPSTREAM_VERSION = '4.2.0'
UPSTREAM_COMMIT = '6bd0eed7b108497aa50051580387345340e3167f'
FIELDS = ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningTokens', 'totalTokens')
DB_FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')
JSON_FIELDS = ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens')
UUID_PATTERN = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
SESSION_ID = re.compile(UUID_PATTERN, re.I)
THREAD_ID = re.compile('('+UUID_PATTERN+r')\.jsonl(?:\.zst)?$', re.I)
CAVEAT = 'Stored or synced history is not verified execution on this device or by the currently selected account.'
REFRESH_ACTION = 'Open Codex Usage Desktop, refresh local usage, then rerun this report.'
MAX_DAYS = 366
HISTORY_SCHEMA_VERSION = 1
# Bump when numeric selection, attribution, validation or stored report semantics change.
REPORT_ALGORITHM_VERSION = 'usage-report-2026-10-26.1'


class ReportError(ValueError):
    pass


def parse_date(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ReportError('Dates must use YYYY-MM-DD.') from exc
    if parsed.isoformat() != value:
        raise ReportError('Dates must use YYYY-MM-DD.')
    return value


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ReportError('The app cache has an undated scan. '+REFRESH_ACTION)
    return parsed.astimezone(UTC)


def connect_readonly(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ReportError('The Codex Usage Desktop cache is missing. '+REFRESH_ACTION)
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only = ON')
    return db


def counters(values):
    values = list(values)
    if len(values) != len(FIELDS) or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
        raise ReportError('Invalid counters in the app cache. '+REFRESH_ACTION)
    # Keep the upstream total, even when it differs from input + output.
    return dict(zip(FIELDS, values))


def sum_counters(rows):
    return {field: sum(row[field] for row in rows) for field in FIELDS}


def load_rankings(db, kind, start_date, end_date, daily, warnings):
    """Read only cached group labels and their five direct counters."""
    column = kind+'_json'
    columns = {row['name'] for row in db.execute('PRAGMA table_info(daily_usage_rollups)')}
    if column not in columns:
        return [], False  # Older cache schemas have no independent group data.
    selected = ' FROM daily_usage_rollups WHERE date BETWEEN ? AND ? ORDER BY date'
    query = ('SELECT date, '+column+' IS NULL AS missing, json_valid('+column+') AS valid, '
             'CASE WHEN json_valid('+column+') THEN json_type('+column+') END AS map_type'+selected)
    for row in db.execute(query, (start_date, end_date)):
        if not row['missing'] and (not row['valid'] or row['map_type'] != 'object'):
            raise ReportError('Malformed '+kind+' map in the app cache. '+REFRESH_ACTION)

    # json_each exposes only the direct map entries. Never select an entire
    # project value: it can also contain a nested models map we do not need.
    selection = ', '.join(
        "CASE WHEN j.type='object' THEN json_extract(j.value, '$."+field+"') END AS "+dest+
        ", CASE WHEN j.type='object' THEN json_type(j.value, '$."+field+"') END AS "+dest+'_type'
        for field, dest in zip(JSON_FIELDS, FIELDS))
    query = ('SELECT d.date, j.key AS group_id, j.type AS value_type, '+selection+
             ' FROM daily_usage_rollups d, json_each(d.'+column+') j'
             ' WHERE d.date BETWEEN ? AND ? ORDER BY d.date, j.key')
    by_day, by_group = defaultdict(list), defaultdict(list)
    seen = set()
    for row in db.execute(query, (start_date, end_date)):
        identity = (row['date'], row['group_id'])
        if (not isinstance(row['group_id'], str) or not row['group_id']
                or row['value_type'] != 'object' or identity in seen
                or any(row[field+'_type'] != 'integer' for field in FIELDS)):
            raise ReportError('Invalid '+kind+' counters or identities in the app cache. '+REFRESH_ACTION)
        seen.add(identity)
        values = {'date': row['date'], **counters(row[field] for field in FIELDS)}
        by_day[row['date']].append(values)
        by_group[row['group_id']].append(values)

    missing_days = []
    for day in daily:
        entries = by_day[day['date']]
        if not entries and any(day[field] for field in FIELDS):
            missing_days.append(day['date'])
        elif sum_counters(entries) != {field: day[field] for field in FIELDS}:
            raise ReportError('App daily totals and '+kind+' totals disagree on '+day['date']+'. '+REFRESH_ACTION)
    if missing_days:
        warnings.append('Cached '+kind+' rankings are unavailable: '+str(len(missing_days))+
                        ' usage day(s) lack a group map. '+REFRESH_ACTION)
        return [], False

    totals = sum_counters(daily)
    rankings = [{'id': label, 'label': label, **sum_counters(rows),
                 'sharePercent': sum(row['totalTokens'] for row in rows)/totals['totalTokens']*100
                                 if totals['totalTokens'] else 0,
                 'daily': rows} for label, rows in by_group.items()]
    if sum_counters(rankings) != totals:
        raise ReportError('App period totals and '+kind+' totals disagree. '+REFRESH_ACTION)
    rankings.sort(key=lambda row: (-row['totalTokens'], row['id']))
    return rankings, True


def load_session_maps(db, kind, start_date, end_date, *, project=None, all_projects=False):
    """Project only group keys and five numeric counters, never whole values."""
    source = " FROM session_file_rollups s, json_each(s.rows_json) j"
    parameters = [start_date, end_date]
    if project is None and not all_projects:
        value = 'j.value'
        extra = ''
    else:
        source += ", json_each(j.value, '$.projects') p"
        value = "CASE WHEN p.type='object' THEN p.value ELSE '{}' END"
        extra = '' if all_projects else ' AND p.key=?'
        if not all_projects:
            parameters.append(project)
    condition = " WHERE json_extract(j.value, '$.date') BETWEEN ? AND ?"+extra
    prefix = 'SELECT s.path, j.key AS row_index, '+('p.key AS project_id, ' if all_projects else '')
    def identity(row):
        return (row['path'], row['row_index'])+((row['project_id'],) if all_projects else ())
    maps = {}
    for row in db.execute(prefix+"json_type("+value+", '$."+kind+"') AS map_type"+
                          source+condition, parameters):
        key = identity(row)
        maps[key] = {} if row['map_type'] == 'object' else None
    selection = ', '.join(
        "CASE WHEN g.type='object' AND json_type(g.value, '$."+field+"')='integer' "
        "THEN json_extract(g.value, '$."+field+"') END AS "+dest
        for field, dest in zip(JSON_FIELDS, FIELDS))
    query = (prefix+'g.key AS group_id, g.type AS value_type, '+selection+source+
             ", json_each("+value+", '$."+kind+"') g"+condition)
    for row in db.execute(query, parameters):
        key = identity(row)
        mapping = maps.get(key)
        if mapping is None:
            continue
        label = row['group_id']
        try:
            if (not isinstance(label, str) or not label or label in mapping
                    or row['value_type'] != 'object'):
                raise ReportError('Invalid session group identity.')
            mapping[label] = counters(row[field] for field in FIELDS)
        except ReportError:
            maps[key] = None
    return maps


def checked_project_models(project_maps, nested_models, session_models, records, app_rankings):
    """Check the nested distribution against independent file/day and app models."""
    if app_rankings is None:
        raise ReportError('Independent app model rankings are unavailable for project model attribution.')
    checked_maps(session_models, records, 'models', rankings=app_rankings)
    for key, source in records.items():
        grouped = defaultdict(list)
        for project_id, project_values in project_maps[key].items():
            mapping = nested_models.get((*key, project_id))
            if mapping is None:
                raise ReportError('The cache lacks a valid explicit nested model map for a session project day.')
            if sum_counters(mapping.values()) != project_values:
                raise ReportError('Nested model counters do not reconcile with their source session project day.')
            for label, values in mapping.items():
                grouped[label].append(values)
        if {label: sum_counters(parts) for label, parts in grouped.items()} != session_models[key]:
            raise ReportError('Nested project model labels or counters disagree with independent session day model maps.')


def checked_maps(maps, records, kind, *, rankings=None):
    """Reconcile every source row, plus independent app groups when available."""
    grouped = defaultdict(list)
    for key, source in records.items():
        mapping = maps.get(key)
        if mapping is None:
            raise ReportError('The cache lacks a valid explicit '+kind+' map for a selected session day.')
        if sum_counters(mapping.values()) != {field: source[field] for field in FIELDS}:
            raise ReportError('Session '+kind+' counters do not reconcile with their source session day.')
        for label, values in mapping.items():
            grouped[(source['date'], label)].append(values)
    if rankings is not None:
        expected = {(day['date'], rank['id']): {field: day[field] for field in FIELDS}
                    for rank in rankings for day in rank['daily']}
        actual = {key: sum_counters(rows) for key, rows in grouped.items()}
        if actual != expected:
            raise ReportError('Session '+kind+' maps and app daily group totals disagree.')
    return maps


def scoped_rankings(maps, records, kind, warnings, *, all_records=None, app_rankings=None,
                    attribution_error=None):
    try:
        if attribution_error is not None:
            raise ReportError(attribution_error)
        if all_records is not None:
            checked_maps(maps, all_records, kind, rankings=app_rankings)
        checked_maps(maps, records, kind)
    except ReportError as exc:
        warnings.append('Scoped '+kind+' rankings are unavailable: '+str(exc)+' '+REFRESH_ACTION)
        return [], False
    grouped = defaultdict(lambda: defaultdict(list))
    for key, source in records.items():
        for label, values in maps[key].items():
            grouped[label][source['date']].append(values)
    totals = sum_counters(records.values())
    rankings = []
    for label, days in grouped.items():
        daily = [{'date': day, **sum_counters(parts)} for day, parts in sorted(days.items())]
        values = sum_counters(daily)
        rankings.append({'id': label, 'label': label, **values, 'daily': daily,
                         'sharePercent': values['totalTokens']/totals['totalTokens']*100
                                         if totals['totalTokens'] else 0})
    if sum_counters(rankings) != totals:
        raise ReportError('Scoped '+kind+' daily and period totals disagree. '+REFRESH_ACTION)
    rankings.sort(key=lambda row: (-row['totalTokens'], row['id']))
    return rankings, True


def safe_names(paths, thread_ids=None):
    """Only actual sidebar names; never select title, prompt_title or messages."""
    names = {}
    for path in paths:
        try:
            with closing(connect_readonly(path)) as db:
                columns = {row['name'] for row in db.execute('PRAGMA table_info(threads)')}
                if not {'id', 'name'} <= columns:
                    continue
                if thread_ids is None:
                    queries = [('SELECT id, name FROM threads', ())]
                else:
                    ids = sorted(set(thread_ids))
                    queries = [('SELECT id, name FROM threads WHERE id COLLATE NOCASE IN ('+
                                ','.join('?' for _ in ids[i:i+400])+')', ids[i:i+400])
                               for i in range(0, len(ids), 400)]
                for row in (row for query, values in queries for row in db.execute(query, values)):
                    if isinstance(row['id'], str) and isinstance(row['name'], str) and row['name'].strip():
                        thread = row['id'].lower() if SESSION_ID.fullmatch(row['id']) else row['id']
                        names.setdefault(thread, row['name'].strip())
        except (ReportError, OSError, sqlite3.Error):
            continue  # Optional display metadata must not change numeric results.
    return names


def discover_names_database():
    home = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))).expanduser()
    try:
        candidates = [(int(match.group(1)), path) for path in home.glob('state_*.sqlite')
                      if (match := re.fullmatch(r'state_(\d+)\.sqlite', path.name)) and path.is_file()]
    except OSError:
        return []
    return [max(candidates, key=lambda item: item[0])[1]] if candidates else []


def load_cache(database, start_date=None, end_date=None, names_databases=None, now=None, *,
               days=None, project=None, chat=None):
    if project is not None and chat is not None:
        raise ReportError('Choose either a project scope or a chat scope, not both.')
    for value in (project, chat):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ReportError('The scope must use a nonempty exact cached identity.')
    if chat is not None and SESSION_ID.fullmatch(chat):
        chat = chat.lower()
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ReportError('The report generation time must include a timezone.')
    if bool(start_date) != bool(end_date):
        raise ReportError('Provide both start and end dates, or neither.')
    warnings = []
    with closing(connect_readonly(database)) as db:
        db.execute('BEGIN')  # A consistent read snapshot, including the app's WAL.
        scan = db.execute('SELECT scanned_at, timezone FROM scan_runs ORDER BY id DESC LIMIT 1').fetchone()
        if scan is None:
            raise ReportError('The app has not completed a local scan. '+REFRESH_ACTION)
        last_scan = parse_time(scan['scanned_at'])
        age = (now-last_scan).total_seconds()
        if age < -120:
            warnings.append('The app scan timestamp is ahead of this computer\'s clock; check the clock and refresh local usage.')
        elif age > 15*60:
            warnings.append(f'The app cache is {int(age//60):,} minutes old. '+REFRESH_ACTION)
        latest = db.execute('SELECT MAX(date) FROM daily_usage_rollups').fetchone()[0]
        if not start_date:
            if latest is None:
                raise ReportError('The app cache has no usage days. Choose an explicit date range for a zero-record report, or refresh local usage.')
            start_date = end_date = parse_date(latest)
            if days is not None:
                if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
                    raise ReportError(f'Days must be an integer between 1 and {MAX_DAYS}.')
                try:
                    start_date = (date.fromisoformat(end_date)-timedelta(days=days-1)).isoformat()
                except OverflowError as exc:
                    raise ReportError('The selected day range exceeds supported calendar dates.') from exc
        else:
            start_date, end_date = parse_date(start_date), parse_date(end_date)
        if start_date > end_date:
            raise ReportError('The start date must be on or before the end date.')
        day_count = (date.fromisoformat(end_date)-date.fromisoformat(start_date)).days+1
        if day_count > MAX_DAYS:
            raise ReportError(f'The date range must contain no more than {MAX_DAYS} days.')
        daily = []
        query = 'SELECT date, '+', '.join(DB_FIELDS)+' FROM daily_usage_rollups WHERE date BETWEEN ? AND ? ORDER BY date'
        for row in db.execute(query, (start_date, end_date)):
            daily.append({'date': parse_date(row['date']), **counters(row[field] for field in DB_FIELDS)})
        models, has_models = load_rankings(db, 'models', start_date, end_date, daily, warnings)
        projects, has_projects = load_rankings(db, 'projects', start_date, end_date, daily, warnings)

        # Numeric fields only: do not retrieve prompt_title, quota data, search_text,
        # or cached threadName (upstream may derive it from the opening prompt).
        bad_rows = db.execute('SELECT COUNT(*) FROM session_file_rollups WHERE NOT json_valid(rows_json)').fetchone()[0]
        if bad_rows:
            raise ReportError('Malformed session rows in the app cache. '+REFRESH_ACTION)
        selected = defaultdict(list)
        sessions_by_day = defaultdict(list)
        records = {}
        selection = ', '.join("json_extract(j.value, '$."+field+"') AS "+dest
                              for field, dest in zip(JSON_FIELDS, FIELDS))
        query = ("SELECT s.path, j.key AS row_index, json_extract(j.value, '$.date') AS day, "+selection+
                 " FROM session_file_rollups s, json_each(s.rows_json) j"
                 " WHERE json_extract(j.value, '$.date') BETWEEN ? AND ? ORDER BY s.path, day")
        for row in db.execute(query, (start_date, end_date)):
            parse_date(row['day'])
            values = {'date': row['day'], **counters(row[field] for field in FIELDS)}
            selected[row['path']].append(values)
            records[(row['path'], row['row_index'])] = values
            sessions_by_day[row['day']].append(values)

        daily_by_date = {row['date']: row for row in daily}
        for day in daily_by_date.keys() | sessions_by_day.keys():
            expected = {field: daily_by_date.get(day, {}).get(field, 0) for field in FIELDS}
            if sum_counters(sessions_by_day[day]) != expected:
                raise ReportError('App daily totals and session totals disagree on '+day+'. '+REFRESH_ACTION)

        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        metadata = {}
        if 'project_session_index' in tables:
            query = ("SELECT path, json_extract(detail_json, '$.agentSessionId') AS agent_id, "
                     "json_extract(detail_json, '$.parentSessionId') AS parent_id, "
                     "json_extract(detail_json, '$.agentPath') AS agent_path, "
                     "json_extract(detail_json, '$.agentNickname') AS nickname, "
                     "json_extract(detail_json, '$.agentRole') AS role "
                     "FROM project_session_index")
            for row in db.execute(query):
                metadata[row['path']] = dict(row)
        missing_metadata = sum(not isinstance(metadata.get(path, {}).get('agent_id'), str)
                               or not metadata[path]['agent_id'].strip() for path in selected)
        if missing_metadata:
            warnings.append(f'{missing_metadata:,} selected session(s) lack cached agent metadata; their usage is preserved as separate sessions. '+REFRESH_ACTION)

        # Need inactive parent identities too, so agents remain in their real family.
        all_paths = [row[0] for row in db.execute('SELECT path FROM session_file_rollups ORDER BY path')]
        if project is not None or chat is not None:
            project_maps = load_session_maps(db, 'projects', start_date, end_date)
            session_model_maps = load_session_maps(db, 'models', start_date, end_date)
            if project is not None:
                nested_model_maps = load_session_maps(db, 'models', start_date, end_date, all_projects=True)
                model_maps = {(path, index): mapping for (path, index, project_id), mapping
                              in nested_model_maps.items() if project_id == project}
            else:
                model_maps = session_model_maps

    names = safe_names(discover_names_database() if names_databases is None else names_databases)
    by_id, path_ids = {}, {}
    for position, path in enumerate(all_paths, 1):
        match = THREAD_ID.search(path)
        meta = metadata.get(path, {})
        # Cache rows belong to files. A user fork can retain its original
        # agentSessionId while its filename identifies a separate session.
        thread = match.group(1).lower() if match else meta.get('agent_id') or f'session-{position}'
        if not isinstance(thread, str) or thread in by_id:
            raise ReportError('Duplicate or invalid session identities in the app cache. '+REFRESH_ACTION)
        # Forked files can carry their parent's cached agent metadata. Apply
        # lineage and agent labels only when this file explicitly owns them.
        agent_id = meta.get('agent_id')
        if not match or not isinstance(agent_id, str) or agent_id.lower() != thread:
            meta = {}
        else:
            parent = meta.get('parent_id')
            if parent is not None and (not isinstance(parent, str) or not SESSION_ID.fullmatch(parent)):
                raise ReportError('Invalid agent parent in the app cache. '+REFRESH_ACTION)
            meta = {**meta, 'parent_id': parent.lower() if parent is not None else None}
        by_id[thread] = meta
        path_ids[path] = thread

    def family(thread):
        seen = set()
        while by_id.get(thread, {}).get('parent_id'):
            if thread in seen:
                raise ReportError('Cyclic agent metadata in the app cache. '+REFRESH_ACTION)
            seen.add(thread)
            thread = by_id[thread]['parent_id']
            if not isinstance(thread, str):
                raise ReportError('Invalid agent parent in the app cache. '+REFRESH_ACTION)
        return thread

    scope = {'kind': 'all', 'id': None, 'label': 'All chats and project folders'}
    model_attribution_error = None
    if project is not None or chat is not None:
        if project is not None:
            if not has_projects and records:
                raise ReportError('Project scope is unavailable without reconciled app project rankings. '+REFRESH_ACTION)
            try:
                checked_maps(project_maps, records, 'projects', rankings=projects)
            except ReportError as exc:
                raise ReportError('Project scope is unavailable: '+str(exc)+' '+REFRESH_ACTION) from exc
            scoped_records = {key: {'date': source['date'], **project_maps[key][project]}
                              for key, source in records.items() if project in project_maps[key]}
            scoped_project_maps = {key: {project: {field: source[field] for field in FIELDS}}
                                   for key, source in scoped_records.items()}
            if scoped_records:
                try:
                    checked_project_models(project_maps, nested_model_maps, session_model_maps,
                                           records, models if has_models else None)
                except ReportError as exc:
                    model_attribution_error = str(exc)
            scope = {'kind': 'project', 'id': project, 'label': project}
        else:
            scoped_records = {key: source for key, source in records.items()
                              if family(path_ids[key[0]]) == chat}
            scoped_project_maps = project_maps
            scope = {'kind': 'chat', 'id': chat,
                     'label': names.get(chat, 'Chat '+chat[:8])}
        selected = defaultdict(list)
        scoped_by_day = defaultdict(list)
        for (path, _), row in scoped_records.items():
            selected[path].append(row)
            scoped_by_day[row['date']].append(row)
        daily = [{'date': day, **sum_counters(rows)} for day, rows in sorted(scoped_by_day.items())]
        daily_by_date = {row['date']: row for row in daily}
        models, has_models = scoped_rankings(
            model_maps, scoped_records, 'models', warnings,
            all_records=records if chat is not None and scoped_records else None,
            app_rankings=models if chat is not None and has_models else None,
            attribution_error=model_attribution_error)
        projects, has_projects = scoped_rankings(
            scoped_project_maps, scoped_records, 'projects', warnings,
            all_records=records if chat is not None and scoped_records else None,
            app_rankings=projects if chat is not None and has_projects else None)
    scope['empty'] = not bool(selected)

    owners, grouped, chat_days = [], defaultdict(list), defaultdict(lambda: defaultdict(list))
    for path, rows in selected.items():
        thread = path_ids[path]
        meta = by_id[thread]
        root = family(thread)
        kind = 'self' if root == thread else 'agent'
        label = 'Main agent' if kind == 'self' else meta.get('nickname') or meta.get('role') or 'Agent '+thread[:8]
        owner = {'id': thread, 'chatId': root, 'parentId': meta.get('parent_id'), 'kind': kind,
                 'label': label, 'path': meta.get('agent_path'), 'model': 'App cache', 'responses': None,
                 **sum_counters(rows)}
        owners.append(owner)
        grouped[root].append(owner)
        for row in rows:
            chat_days[root][row['date']].append(row)

    totals = sum_counters(daily)
    if sum_counters(owners) != totals:
        raise ReportError('App daily totals and session totals disagree. '+REFRESH_ACTION)
    recorded_rows = [*daily, *owners, *models, *projects,
                     *(row for ranking in (*models, *projects) for row in ranking['daily'])]
    if any(row['inputTokens']+row['outputTokens'] != row['totalTokens'] for row in recorded_rows):
        warnings.append('The app total differs from input plus output; its recorded counters are preserved without adjustment.')
    if any(row['cachedInputTokens'] > row['inputTokens'] or row['reasoningTokens'] > row['outputTokens']
           for row in recorded_rows):
        warnings.append('A recorded subset exceeds its parent counter; app values are preserved without adjustment.')
    totals.update(responses=None, sessions=len(owners),
                  uncachedInputTokens=(totals['inputTokens']-totals['cachedInputTokens']
                                       if totals['cachedInputTokens'] <= totals['inputTokens'] else None))
    chats = []
    for thread, parts in grouped.items():
        chats.append({'id': thread, 'label': names.get(thread, 'Chat '+thread[:8]),
                      'labelSource': 'threads.name' if thread in names else 'session_id',
                      **sum_counters(parts), 'owners': [owner['id'] for owner in parts],
                      'daily': [{'date': day, **sum_counters(rows)}
                                for day, rows in sorted(chat_days[thread].items())],
                      'selfTokens': sum(owner['totalTokens'] for owner in parts if owner['kind'] == 'self'),
                      'agentsTokens': sum(owner['totalTokens'] for owner in parts if owner['kind'] == 'agent'),
                      'agentCount': sum(owner['kind'] == 'agent' for owner in parts), 'responses': None})
    for chat in chats:
        if sum_counters(chat['daily']) != {field: chat[field] for field in FIELDS}:
            raise ReportError('Chat daily and period totals disagree. '+REFRESH_ACTION)
    for group in (owners, chats):
        for row in group:
            row['sharePercent'] = row['totalTokens']/totals['totalTokens']*100 if totals['totalTokens'] else 0
        group.sort(key=lambda row: row['totalTokens'], reverse=True)
    label = start_date if start_date == end_date else start_date+' – '+end_date
    trend_days = []
    first_day = date.fromisoformat(start_date)
    for offset in range(day_count):
        day = (first_day+timedelta(days=offset)).isoformat()
        recorded = day in daily_by_date
        trend_days.append({'date': day, 'recorded': recorded,
                           **{field: daily_by_date[day][field] if recorded else None for field in FIELDS}})
    data = {'schemaVersion': 1, 'mode': 'Codex Usage Desktop cache', 'generatedAt': now.isoformat(),
            'scope': scope,
            'scopeNote': 'Report scope: '+scope['label']+'. '+CAVEAT+
                         (' No usage records match this scope and date range.' if scope['empty'] else ''),
            'source': {'name': 'Codex Usage Desktop', 'version': UPSTREAM_VERSION,
                       'lastScanAt': scan['scanned_at'], 'ageSeconds': max(0, int(age)),
                       'timeZone': scan['timezone'], 'datePrecision': 'day'},
            'window': {'label': label+' • full app day'+('s' if start_date != end_date else ''),
                       'startDate': start_date, 'endDate': end_date, 'timeZone': scan['timezone'], 'precision': 'day'},
            'totals': totals, 'chats': chats, 'owners': owners, 'daily': daily,
            'trend': {'startDate': start_date, 'endDate': end_date, 'dayCount': day_count,
                      'timeZone': scan['timezone'], 'days': trend_days},
            'models': models, 'projects': projects, 'features': {'models': has_models, 'projects': has_projects},
            'diagnostics': {'status': 'incomplete' if warnings else 'complete', 'warnings': warnings},
            'notes': [CAVEAT, 'App totals are shown as recorded. Cached input and reasoning are not added again.',
                      'The app cache has calendar-day precision; exact hourly or last-eight-hours totals are unavailable.',
                      'All adapter processing is local. No account quota or credit balance is inferred.']}
    proof = {'upstreamVersion': UPSTREAM_VERSION, 'upstreamCommit': UPSTREAM_COMMIT,
             'databasePath': str(Path(database).expanduser().resolve()), 'readOnly': True,
             'lastScanAt': scan['scanned_at'], 'timeZone': scan['timezone'],
             'startDate': start_date, 'endDate': end_date, 'selectedSessions': len(owners),
             'scope': scope,
             'reconciledFields': list(FIELDS), 'appDailySessionTotalsMatch': True,
             'appPerDaySessionTotalsMatch': True,
             'scopedDailyOwnerChatTotalsMatch': True,
             'rankingDailyAndPeriodTotalsMatch': {'models': True if has_models else None,
                                                'projects': True if has_projects else None},
             'allAnalysisLocal': True, 'networkUsed': False, 'generatedAt': now.isoformat()}
    return data, proof


def canonical_json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def source_revision(database):
    """A local SQLite revision token, without reading database records or WAL pages.

    SQLite commits change the main header or the WAL frame checksum chain. File
    identity and nanosecond metadata also invalidate replaced/touched sources.
    This is a conservative local reuse token, not authentication of arbitrary files.
    """
    database = Path(database).expanduser().resolve()
    if not database.is_file():
        raise ReportError('The Codex Usage Desktop cache is missing. '+REFRESH_ACTION)
    def describe(path, wal=False):
        try:
            stat = path.stat()
            with path.open('rb') as handle:
                header = handle.read(32 if wal else 100)
                tail = b''
                if wal and len(header) == 32:
                    page_size = int.from_bytes(header[8:12], 'big')
                    if page_size and stat.st_size >= 32+24+page_size:
                        frames = (stat.st_size-32)//(24+page_size)
                        handle.seek(32+(frames-1)*(24+page_size))
                        tail = handle.read(24)  # Frame header/checksums only; no page contents.
            return {'device': stat.st_dev, 'fileId': stat.st_ino, 'size': stat.st_size,
                    'mtimeNs': stat.st_mtime_ns, 'ctimeNs': stat.st_ctime_ns,
                    'header': header.hex(), 'lastFrameHeader': tail.hex()}
        except FileNotFoundError:
            if wal:
                return None
            raise ReportError('The Codex Usage Desktop cache disappeared. '+REFRESH_ACTION)
    return {'databasePath': str(database), 'database': describe(database),
            'wal': describe(Path(str(database)+'-wal'), wal=True)}


def source_metadata(database):
    """Read only source freshness and latest calendar identity on the cache path."""
    for _ in range(3):
        before = source_revision(database)
        with closing(connect_readonly(database)) as db:
            db.execute('BEGIN')
            scan = db.execute('SELECT scanned_at, timezone FROM scan_runs ORDER BY id DESC LIMIT 1').fetchone()
            if scan is None:
                raise ReportError('The app has not completed a local scan. '+REFRESH_ACTION)
            parse_time(scan['scanned_at'])
            if not isinstance(scan['timezone'], str) or not scan['timezone'].strip():
                raise ReportError('The app cache has no valid timezone. '+REFRESH_ACTION)
            latest = db.execute('SELECT MAX(date) FROM daily_usage_rollups').fetchone()[0]
            after = source_revision(database)
        if before == after:
            return {'revision': after, 'lastScanAt': scan['scanned_at'],
                    'timeZone': scan['timezone'], 'latestDate': latest}
    raise ReportError('The app cache changed while reading its revision; rerun this report.')


def selected_window(start_date, end_date, latest, days):
    if bool(start_date) != bool(end_date):
        raise ReportError('Provide both start and end dates, or neither.')
    if not start_date:
        if latest is None:
            raise ReportError('The app cache has no usage days. Choose an explicit date range for a zero-record report, or refresh local usage.')
        end_date = parse_date(latest)
        days = 1 if days is None else days
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
            raise ReportError(f'Days must be an integer between 1 and {MAX_DAYS}.')
        try:
            start_date = (date.fromisoformat(end_date)-timedelta(days=days-1)).isoformat()
        except OverflowError as exc:
            raise ReportError('The selected day range exceeds supported calendar dates.') from exc
    start_date, end_date = parse_date(start_date), parse_date(end_date)
    if start_date > end_date:
        raise ReportError('The start date must be on or before the end date.')
    if (date.fromisoformat(end_date)-date.fromisoformat(start_date)).days+1 > MAX_DAYS:
        raise ReportError(f'The date range must contain no more than {MAX_DAYS} days.')
    return start_date, end_date


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key in report history.')
        result[key] = value
    return result


def read_history(path):
    if not path.exists():
        return {'schemaVersion': HISTORY_SCHEMA_VERSION, 'entries': []}, 'missing_history'
    try:
        value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=strict_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON value.')))
        if (not isinstance(value, dict) or type(value.get('schemaVersion')) is not int
                or value.get('schemaVersion') != HISTORY_SCHEMA_VERSION
                or not isinstance(value.get('entries'), list)):
            raise ValueError('Unknown or malformed report history schema.')
        return value, None
    except (ValueError, UnicodeError, OSError, RecursionError):
        return {'schemaVersion': HISTORY_SCHEMA_VERSION, 'entries': []}, 'invalid_history'


def validate_snapshot(entry, key):
    """Validate stored output/proof, never reread or reconcile source counters."""
    if not isinstance(entry, dict) or entry.get('key') != key:
        raise ValueError('Mismatched report history key.')
    body = {name: value for name, value in entry.items() if name != 'sha256'}
    if entry.get('sha256') != digest(body):
        raise ValueError('Report history integrity check failed.')
    data, proof = entry['data'], entry['proof']
    selector = entry['selector']
    if digest(selector) != key or selector['algorithmVersion'] != REPORT_ALGORITHM_VERSION:
        raise ValueError('Mismatched report history algorithm or selector.')
    if (type(data['schemaVersion']) is not int or data['schemaVersion'] != 1 or proof['upstreamVersion'] != UPSTREAM_VERSION
            or proof['upstreamCommit'] != UPSTREAM_COMMIT
            or proof['databasePath'] != selector['source']['databasePath']
            or proof['reconciledFields'] != list(FIELDS)):
        raise ValueError('Invalid original report proof identity.')
    for field in ('readOnly', 'appDailySessionTotalsMatch', 'appPerDaySessionTotalsMatch',
                  'scopedDailyOwnerChatTotalsMatch', 'allAnalysisLocal'):
        if proof[field] is not True:
            raise ValueError('Incomplete original report proof.')
    if proof['networkUsed'] is not False:
        raise ValueError('Invalid original locality proof.')
    for field in ('startDate', 'endDate'):
        if data['window'][field] != selector[field] or proof[field] != selector[field]:
            raise ValueError('Mismatched original report dates.')
    if any(value != selector['timeZone'] for value in
           (proof['timeZone'], data['source']['timeZone'], data['window']['timeZone'], data['trend']['timeZone'])):
        raise ValueError('Mismatched original report timezone.')
    if (proof['lastScanAt'] != selector['lastScanAt'] or data['source']['lastScanAt'] != selector['lastScanAt']
            or proof['generatedAt'] != data['generatedAt'] or entry['createdAt'] != data['generatedAt']):
        raise ValueError('Mismatched original report freshness metadata.')
    parse_time(data['generatedAt'])
    parse_time(selector['lastScanAt'])
    scope = data['scope']
    if (proof['scope'] != scope or {name: scope[name] for name in ('kind', 'id')} != selector['scope']
            or scope['empty'] is not (not bool(data['owners']))):
        raise ValueError('Mismatched original report scope.')
    # Check only the much smaller, already exported report structure. These
    # checks preserve the original recorded values, including subset anomalies.
    all_rows = [data['totals'], *data['daily'], *data['owners'], *data['chats'],
                *(row for chat in data['chats'] for row in chat['daily']),
                *data['models'], *data['projects'],
                *(day for kind in ('models', 'projects') for row in data[kind] for day in row['daily'])]
    for row in all_rows:
        counters(row[field] for field in FIELDS)
    totals = {field: data['totals'][field] for field in FIELDS}
    for rows in (data['daily'], data['owners'], data['chats']):
        if sum_counters(rows) != totals:
            raise ValueError('Invalid stored report totals.')
    owners = {row['id']: row for row in data['owners']}
    chats = {row['id']: row for row in data['chats']}
    if (len(owners) != len(data['owners']) or len(chats) != len(data['chats'])
            or data['totals']['sessions'] != len(owners) or proof['selectedSessions'] != len(owners)):
        raise ValueError('Invalid stored ownership identities.')
    for chat in chats.values():
        parts = [row for row in owners.values() if row['chatId'] == chat['id']]
        if (len(chat['owners']) != len(set(chat['owners'])) or set(chat['owners']) != {row['id'] for row in parts}
                or sum_counters(parts) != {field: chat[field] for field in FIELDS}
                or sum_counters(chat['daily']) != {field: chat[field] for field in FIELDS}
                or chat['selfTokens'] != sum(row['totalTokens'] for row in parts if row['kind'] == 'self')
                or chat['agentsTokens'] != sum(row['totalTokens'] for row in parts if row['kind'] == 'agent')
                or chat['agentCount'] != sum(row['kind'] == 'agent' for row in parts)):
            raise ValueError('Invalid stored chat ownership.')
    for owner in owners.values():
        if owner['chatId'] not in chats or owner['kind'] not in ('self', 'agent'):
            raise ValueError('Invalid stored owner family.')
        if ((owner['kind'] == 'self') != (owner['id'] == owner['chatId'])
                or (owner['kind'] == 'self' and owner['parentId'] is not None)
                or (owner['kind'] == 'agent' and not isinstance(owner['parentId'], str))):
            raise ValueError('Invalid stored owner lineage.')
    daily = {row['date']: row for row in data['daily']}
    if len(daily) != len(data['daily']):
        raise ValueError('Duplicate stored report days.')
    expected_days = (date.fromisoformat(selector['endDate'])-date.fromisoformat(selector['startDate'])).days+1
    if data['trend']['dayCount'] != expected_days or len(data['trend']['days']) != expected_days:
        raise ValueError('Invalid stored calendar span.')
    for index, row in enumerate(data['trend']['days']):
        day = (date.fromisoformat(selector['startDate'])+timedelta(days=index)).isoformat()
        if row['date'] != day or row['recorded'] is not (day in daily):
            raise ValueError('Invalid stored calendar identity.')
        if any(row[field] != (daily[day][field] if day in daily else None) for field in FIELDS):
            raise ValueError('Invalid stored calendar counters.')
    if set(daily) - {row['date'] for row in data['trend']['days']}:
        raise ValueError('Stored daily counters exceed selected dates.')
    chat_days = defaultdict(list)
    for chat in chats.values():
        if len({row['date'] for row in chat['daily']}) != len(chat['daily']):
            raise ValueError('Duplicate stored chat days.')
        for row in chat['daily']:
            chat_days[row['date']].append(row)
    if set(chat_days)-set(daily) or any(sum_counters(chat_days[day]) != {field: row[field] for field in FIELDS}
                                      for day, row in daily.items()):
        raise ValueError('Invalid stored chat day distribution.')
    for kind in ('models', 'projects'):
        available = data['features'][kind]
        if not isinstance(available, bool) or proof['rankingDailyAndPeriodTotalsMatch'][kind] is not (True if available else None):
            raise ValueError('Invalid stored ranking proof.')
        if not available and data[kind]:
            raise ValueError('Unavailable stored ranking has values.')
        if available:
            if sum_counters(data[kind]) != totals or len({row['id'] for row in data[kind]}) != len(data[kind]):
                raise ValueError('Invalid stored ranking totals or identities.')
            for row in data[kind]:
                if sum_counters(row['daily']) != {field: row[field] for field in FIELDS}:
                    raise ValueError('Invalid stored ranking daily totals.')
            grouped = defaultdict(list)
            for row in data[kind]:
                for day in row['daily']:
                    grouped[day['date']].append(day)
            if set(grouped)-set(daily) or any(sum_counters(grouped[day]) != {field: row[field] for field in FIELDS}
                                             for day, row in daily.items()):
                raise ValueError('Invalid stored ranking day distribution.')
    if (not isinstance(data['diagnostics']['warnings'], list)
            or not all(isinstance(warning, str) for warning in data['diagnostics']['warnings'])):
        raise ValueError('Invalid stored diagnostics.')
    uncached = totals['inputTokens']-totals['cachedInputTokens'] if totals['cachedInputTokens'] <= totals['inputTokens'] else None
    if data['totals']['uncachedInputTokens'] != uncached:
        raise ValueError('Invalid stored subset display.')
    return json.loads(canonical_json(data)), json.loads(canonical_json(proof))


def append_history(path, entry):
    """Serialize local writers, preserve all entries, then atomically replace JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(path)+'.lock')
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    temporary = None
    recovery = None
    try:
        history, issue = read_history(path)
        if issue == 'invalid_history':
            # Preserve the exact unreadable bytes before replacing the file.
            suffix = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
            recovery = path.with_name(path.name+'.corrupt-'+suffix+'.bak')
            with recovery.open('xb') as saved:
                saved.write(path.read_bytes())
                saved.flush()
                os.fsync(saved.fileno())
        history['entries'].append(entry)
        descriptor, temporary = tempfile.mkstemp(prefix='.'+path.name+'-', suffix='.tmp', dir=path.parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            handle.write(json.dumps(history, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        return recovery
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def refresh_display(data, proof, metadata, now, names_databases):
    """Refresh display metadata while keeping the approved numeric snapshot."""
    warnings = [warning for warning in data['diagnostics']['warnings']
                if not warning.startswith(('The app cache is ', 'The app scan timestamp is ahead '))]
    age = (now-parse_time(metadata['lastScanAt'])).total_seconds()
    if age < -120:
        warnings.insert(0, 'The app scan timestamp is ahead of this computer\'s clock; check the clock and refresh local usage.')
    elif age > 15*60:
        warnings.insert(0, f'The app cache is {int(age//60):,} minutes old. '+REFRESH_ACTION)
    try:
        zone = UTC if metadata['timeZone'] in ('UTC', 'Etc/UTC', 'GMT', 'Etc/GMT') else ZoneInfo(metadata['timeZone'])
        today = now.astimezone(zone).date().isoformat()
        partial = data['window']['startDate'] <= today <= data['window']['endDate']
        if partial:
            warnings.append('The selected period includes the current app calendar day; that day is partial and can change after the next app refresh.')
    except (ZoneInfoNotFoundError, ValueError):
        today, partial = None, None
        warnings.append('The app timezone cannot be resolved locally; selected days may still be partial, and their current-day status is unknown.')
    data['generatedAt'] = proof['generatedAt'] = now.isoformat()
    data['source'].update(ageSeconds=max(0, int(age)), timeZone=metadata['timeZone'],
                          lastScanAt=metadata['lastScanAt'])
    data['window'].update(currentAppDate=today, includesCurrentAppDay=partial)
    # Read only relevant current sidebar names; unrelated state writes never
    # invalidate numeric results, and automatic discovery handles DB rotation.
    ids = {row['id'] for row in data['chats']}
    if data['scope']['kind'] == 'chat':
        ids.add(data['scope']['id'])
    names = safe_names(discover_names_database() if names_databases is None else names_databases, ids)
    for chat in data['chats']:
        chat['label'] = names.get(chat['id'], 'Chat '+chat['id'][:8])
        chat['labelSource'] = 'threads.name' if chat['id'] in names else 'session_id'
    if data['scope']['kind'] == 'chat':
        chat_id = data['scope']['id']
        data['scope']['label'] = names.get(chat_id, 'Chat '+chat_id[:8])
    data['scopeNote'] = 'Report scope: '+data['scope']['label']+'. '+CAVEAT+(
        ' No usage records match this scope and date range.' if data['scope']['empty'] else '')
    proof['scope'] = dict(data['scope'])
    data['diagnostics'] = {'status': 'incomplete' if warnings else 'complete', 'warnings': warnings}


def load_report(database, start_date=None, end_date=None, names_databases=None, now=None, *,
                days=None, project=None, chat=None, history_file, refresh=False):
    """Reuse only validated results tied to an unchanged local SQLite revision."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ReportError('The report generation time must include a timezone.')
    if project is not None and chat is not None:
        raise ReportError('Choose either a project scope or a chat scope, not both.')
    for value in (project, chat):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ReportError('The scope must use a nonempty exact cached identity.')
    if chat is not None and SESSION_ID.fullmatch(chat):
        chat = chat.lower()
    history_file = Path(history_file).expanduser().resolve()
    source = Path(database).expanduser().resolve()
    names_paths = discover_names_database() if names_databases is None else names_databases
    protected = [source, *(Path(path).expanduser().resolve() for path in names_paths)]
    if any(str(history_file) in (str(path), str(path)+'-wal', str(path)+'-shm') for path in protected):
        raise ReportError('The report history must be separate from the read-only source database.')
    history, issue = read_history(history_file)
    for _ in range(3):
        metadata = source_metadata(database)
        start, end = selected_window(start_date, end_date, metadata['latestDate'], days)
        selector = {'algorithmVersion': REPORT_ALGORITHM_VERSION, 'source': metadata['revision'],
                    'scope': {'kind': 'project' if project is not None else 'chat' if chat is not None else 'all',
                              'id': project if project is not None else chat},
                    'startDate': start, 'endDate': end, 'timeZone': metadata['timeZone'],
                    'lastScanAt': metadata['lastScanAt']}
        key, hit = digest(selector), False
        reason = 'refresh_requested' if refresh else issue or 'no_matching_entry'
        if not refresh:
            for entry in reversed(history['entries']):
                if not isinstance(entry, dict) or entry.get('key') != key:
                    continue
                try:
                    data, proof = validate_snapshot(entry, key)
                    hit, reason = True, 'unchanged_source'
                except (ReportError, ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
                    reason = 'invalid_entry'
                break
        if not hit:
            # ReportError from source accounting is deliberately never swallowed.
            data, proof = load_cache(database, start, end, names_databases, now,
                                     project=project, chat=chat)
            if source_revision(database) != metadata['revision']:
                continue  # The full read may have started after a newer app commit.
            body = {'key': key, 'selector': selector, 'createdAt': data['generatedAt'],
                    'data': data, 'proof': proof}
            entry = {**body, 'sha256': digest(body)}
            data, proof = validate_snapshot(entry, key)
        refresh_display(data, proof, metadata, now, names_databases)
        if source_revision(database) != metadata['revision']:
            continue  # A writer moved the source while this result was assembled.
        cache = {'status': 'hit' if hit else 'miss', 'reason': reason, 'historyPath': str(history_file),
                 'entryKey': key, 'algorithmVersion': REPORT_ALGORITHM_VERSION,
                 'numericComputedAt': entry['createdAt'], 'verifiedProofSha256': digest(entry['proof']),
                 'sourceRevision': digest(metadata['revision']), 'historySaved': hit}
        if not hit:
            try:
                recovery = append_history(history_file, entry)
                cache['historySaved'] = True
                if recovery is not None:
                    cache['warning'] = 'Unreadable report history was preserved before replacement.'
                    cache['recoveryPath'] = str(recovery)
            except OSError as exc:
                cache['warning'] = 'Report history could not be saved: '+str(exc)
        data['cache'] = cache
        proof['cache'] = dict(cache)
        return data, proof
    raise ReportError('The app cache kept changing during report generation; rerun this report.')


def write_report(data, proof, out):
    out = Path(out).resolve()
    template = (Path(__file__).resolve().parents[1]/'assets/dashboard.html').read_text(encoding='utf-8')
    if template.count('__DATA_JSON__') != 1:
        raise ReportError('The dashboard asset must have exactly one data placeholder.')
    payload = json.dumps(data, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    payload = payload.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    html = template.replace('__DATA_JSON__', payload)
    exported = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+'\n'
    proof = {**proof, 'htmlSha256': hashlib.sha256(html.encode('utf-8')).hexdigest(),
             'dataSha256': hashlib.sha256(exported.encode('utf-8')).hexdigest()}
    t = data['totals']
    lines = [f"Codex Usage Desktop cache | {data['window']['label']} | {data['window']['timeZone']}",
             'Scope: '+data['scope']['label']+(' | No matching usage records' if data['scope']['empty'] else ''),
             f"Recorded tokens: {t['totalTokens']:,} | Chats: {len(data['chats']):,} | Sessions: {t['sessions']:,}",
             f"Input: {t['inputTokens']:,} | Cached input: {t['cachedInputTokens']:,} | Output: {t['outputTokens']:,} | Reasoning: {t['reasoningTokens']:,}",
             'Cache last refreshed: '+str(data['source']['lastScanAt']), CAVEAT,
             'Precision: whole app days; an exact last-eight-hours total is unavailable.',
             'App totals are preserved; subset counters are not added again.']
    lines.extend('WARNING: '+warning for warning in data['diagnostics']['warnings'])
    if 'cache' in data:
        cache = data['cache']
        lines.append('Report result cache: '+cache['status']+' | '+cache['reason']+' | '+cache['historyPath'])
        if cache.get('warning'):
            lines.append('WARNING: '+cache['warning'])
    lines.extend(f"{chat['label']}: {chat['totalTokens']:,} tokens" for chat in data['chats'])
    contents = {'index.html': html, 'data.json': exported, 'summary.txt': '\n'.join(lines)+'\n',
                'evidence.json': json.dumps(proof, ensure_ascii=False, indent=2, allow_nan=False)+'\n'}
    out.mkdir(parents=True, exist_ok=True)
    pending = []
    try:
        for name, content in contents.items():
            descriptor, temporary = tempfile.mkstemp(prefix='.'+name+'-', suffix='.tmp', dir=out)
            pending.append((Path(temporary), out/name))
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
                handle.write(content)
        for temporary, destination in pending:
            os.replace(temporary, destination)
    finally:
        for temporary, _ in pending:
            if temporary.exists():
                temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True, help='Local Codex Usage Desktop SQLite cache.')
    parser.add_argument('--names-database', type=Path, action='append', help='Override automatic newest Codex state SQLite discovery; only threads.id/name are read.')
    parser.add_argument('--start-date', help='Inclusive app calendar date YYYY-MM-DD; requires --end-date.')
    parser.add_argument('--end-date', help='Inclusive app calendar date YYYY-MM-DD; requires --start-date.')
    parser.add_argument('--days', type=int, default=1,
                        help='Calendar days ending on the latest cached day (default: 1; paired dates override).')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--project', help='Exact full cached project key; every report section uses this scope.')
    scope.add_argument('--chat', help='Exact chat family ID, including its nested agents; every report section uses this scope.')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--history-file', type=Path,
                        help='Persistent local verified report results (default: <out>/usage-history.json).')
    parser.add_argument('--refresh', action='store_true', help='Bypass saved results and run full source accounting again.')
    args = parser.parse_args(argv)
    try:
        if bool(args.start_date) != bool(args.end_date):
            raise ReportError('Provide both --start-date and --end-date, or neither for the latest cached calendar day.')
        data, proof = load_report(args.database, args.start_date, args.end_date, args.names_database,
                                 days=args.days, project=args.project, chat=args.chat,
                                 history_file=args.history_file or args.out/'usage-history.json', refresh=args.refresh)
        write_report(data, proof, args.out)
        print(json.dumps({'recordedTokens': data['totals']['totalTokens'], 'chats': len(data['chats']),
                          'sessions': data['totals']['sessions'], 'startDate': data['window']['startDate'],
                          'scope': data['scope'],
                          'endDate': data['window']['endDate'], 'timeZone': data['window']['timeZone'],
                          'sourceLastScanAt': data['source']['lastScanAt'], 'sourceAgeSeconds': data['source']['ageSeconds'],
                          'includesCurrentAppDay': data['window']['includesCurrentAppDay'],
                          'coverage': data['diagnostics']['status'], 'warnings': data['diagnostics']['warnings'],
                          'cache': data['cache']}))
        return 2 if data['diagnostics']['warnings'] else 0
    except (ReportError, ValueError, TypeError, OSError, sqlite3.Error) as exc:
        print('Codex Usage Desktop report: '+str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
