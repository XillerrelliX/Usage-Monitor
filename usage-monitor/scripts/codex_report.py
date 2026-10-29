"""One bounded local report workflow for an already confirmed Codex request.

This module adds no counter/parser, account lookup, model call or fallback scan.
The public standalone entry supplies the verified shipped engine. Conversation
consent remains the caller's responsibility; this CLI cannot verify an answer.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
import io
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ReportError(ValueError):
    def __init__(self, code, detail=None):
        self.code, self.detail = code, detail
        super().__init__(code)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ReportError('codex_report_invalid_arguments', message)


def parse_arguments(arguments):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--codex-report', action='store_true', required=True)
    parser.add_argument('--scope', choices=('all', 'chat', 'project', 'selection'), required=True)
    parser.add_argument('--period', choices=('today', 'last3', 'last7'))
    parser.add_argument('--today', help='Client-confirmed current date in the app timezone, YYYY-MM-DD.')
    parser.add_argument('--from-date')
    parser.add_argument('--to-date')
    parser.add_argument('--project-id', action='append')
    parser.add_argument('--chat-id', action='append')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--timezone', help='Assert the saved app timezone; required for a first-use profile.')
    return parser.parse_args(arguments)


def _ids(values):
    if any(not isinstance(value, str) or not value.strip() or value != value.strip() or
           len(value) > 1024 or any(ord(char) < 32 for char in value) for value in values):
        raise ReportError('codex_report_exact_ids_required')
    if len(values) > 256:
        raise ReportError('codex_report_selection_too_large')
    return sorted(set(values))


def prepare_request(args):
    """Freeze supported scope/dates/settings before any meter or source work."""
    from local_setup import default_settings_file, load_settings
    from query_meter import canonical_id
    from refresh_server import _local_path, verified_existing_report
    from refresh_usage import validate_selection
    from report_from_app import parse_date, selected_window

    config = _local_path(args.config or default_settings_file().parent.parent / 'usage-monitor' / 'settings.json')
    saved = load_settings(config) or {}
    if saved and not saved.get('offline'):
        raise ReportError('codex_report_offline_setup_required',
                          'This report requires an offline setup; use the normal launcher to select it explicitly.')
    offline = saved.get('offline', {})
    if args.period and not args.timezone:
        raise ReportError('codex_report_timezone_required',
                          'Preset periods require --timezone IANA_ZONE matching the client date and saved app timezone.')
    zone = offline.get('timezone') or args.timezone
    if args.timezone and offline.get('timezone') and args.timezone != offline['timezone']:
        raise ReportError('codex_report_timezone_mismatch',
                          '--timezone must match the saved app timezone: ' + offline['timezone'])
    if not zone:
        raise ReportError('codex_report_timezone_required',
                          'Supply --timezone IANA_ZONE for this first-use profile; no app timezone is saved.')
    try:
        if zone not in ('UTC', 'Etc/UTC', 'GMT', 'Etc/GMT'):
            ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ReportError('codex_report_invalid_timezone') from exc

    try:
        if args.period:
            if not args.today or args.from_date is not None or args.to_date is not None:
                raise ReportError('codex_report_period_requires_today',
                                  'Use --period with --today, or both exact date flags without --period/--today.')
            end = parse_date(args.today)
            count = {'today': 1, 'last3': 3, 'last7': 7}[args.period]
            start = (date.fromisoformat(end) - timedelta(days=count - 1)).isoformat()
        else:
            if args.today is not None or not args.from_date or not args.to_date:
                raise ReportError('codex_report_dates_required',
                                  'Use --period with --today, or --from-date and --to-date.')
            start, end = args.from_date, args.to_date
        start, end = selected_window(start, end, None, None)
    except (ValueError, OverflowError) as exc:
        if isinstance(exc, ReportError):
            raise
        raise ReportError('codex_report_invalid_dates', str(exc)) from exc

    projects, chats = _ids(args.project_id or []), _ids(args.chat_id or [])
    if args.scope == 'all':
        if projects or chats:
            raise ReportError('codex_report_scope_conflict')
    elif args.scope == 'chat':
        if projects or chats:
            raise ReportError('codex_report_scope_conflict')
        try:
            chats = [canonical_id(os.environ.get('CODEX_THREAD_ID'))]
        except ValueError as exc:
            raise ReportError('codex_report_current_chat_required',
                              'This chat requires the actual current CODEX_THREAD_ID; no chat is guessed.') from exc
    elif args.scope == 'project':
        if len(projects) != 1 or chats:
            raise ReportError('codex_report_exact_project_required')
    elif not projects and not chats:
        raise ReportError('codex_report_explicit_selection_required')
    selection = {'startDate': start, 'endDate': end, 'projects': projects, 'chats': chats}

    out = _local_path(saved.get('outputDirectory') or config.parent / 'dashboard')
    home = _local_path(offline.get('codexHome') or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    # Validate all explicit saved paths before starting the optional meter.
    for value in [saved.get('database'), saved.get('cacheFile'), saved.get('projectState'),
                  *(saved.get('namesDatabases') or []), offline.get('engine'), offline.get('runtimeDirectory')]:
        if value:
            _local_path(value)
    cache = _local_path(saved.get('cacheFile') or out / 'usage-ledger.json')
    if any(path.is_relative_to(home) for path in (config, out, cache)):
        raise ReportError('codex_report_keep_state_outside_source')
    if args.scope in ('project', 'selection'):
        # Existing verified catalogs prove requested identities without a source
        # scan, title matching, cwd inference or fallback to a broader report.
        try:
            bundle, _ = verified_existing_report(out)
            if bundle['source']['timeZone'] != zone:
                raise ReportError('codex_report_timezone_mismatch')
            selection = validate_selection(bundle, selection)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, ReportError):
                raise
            raise ReportError('codex_report_verified_selection_required',
                              'Project/selection IDs must exist in the verified saved catalog; no scope was broadened.') from exc
    return {'scope': args.scope, 'selection': selection, 'timeZone': zone,
            'config': config, 'out': out, 'home': home}


def _unavailable(exc, query_id=None, view_path=None):
    from query_meter import MeterError
    code = str(exc) if isinstance(exc, MeterError) else 'query_local_operation_failed'
    result = {'schemaVersion': 1, 'status': 'unavailable', 'final': False,
              'errorCode': code, 'modelCalls': 0, 'networkUsed': False}
    if query_id:
        result['queryId'] = query_id
    if view_path:
        result['viewPath'] = view_path
    return result


def _refresh_once(request, package_root, engine):
    from refresh_usage import main as refresh
    import report_from_app
    selection = request['selection']
    arguments = ['--offline', '--config', str(request['config']), '--out', str(request['out']),
                 '--codex-home', str(request['home']), '--timezone', request['timeZone'],
                 '--engine', str(engine), '--runtime-dir', str(Path(package_root) / 'runtime'),
                 '--no-open', '--serve-dashboard', '--from-date', selection['startDate'],
                 '--to-date', selection['endDate']]
    for dimension, flag in (('projects', '--project-id'), ('chats', '--chat-id')):
        for identity in selection[dimension]:
            arguments.append(flag + '=' + identity)
    output, errors = io.StringIO(), io.StringIO()
    original_action = report_from_app.REFRESH_ACTION
    report_from_app.REFRESH_ACTION = ('Run Usage Monitor again; if the error persists, keep the previous data '
                                      'and re-extract the verified Windows package.')
    try:
        with redirect_stdout(output), redirect_stderr(errors):
            status = refresh(arguments)
    finally:
        report_from_app.REFRESH_ACTION = original_action
    if status not in (0, 2):
        detail = None
        try:
            detail = json.loads(errors.getvalue()).get('error')
        except (ValueError, AttributeError):
            pass
        raise ReportError('codex_report_refresh_failed', detail)
    try:
        result = json.loads(output.getvalue())
        if not isinstance(result, dict) or result['initialSelection'] != selection:
            raise ValueError('selection mismatch')
        return result, status
    except (ValueError, KeyError, TypeError) as exc:
        raise ReportError('codex_report_invalid_refresh_result') from exc


def run_report(args, package_root, engine):
    from query_meter import Meter, MeterError, canonical_id, compact
    from project_metadata import project_report
    from refresh_server import verified_existing_report
    from refresh_usage import dashboard_url, selected_dashboard_url, validate_selection, validated_refresh
    from report_from_app import FIELDS

    request = prepare_request(args)
    root = Path(package_root).resolve()
    if (request['out'].is_relative_to(root) or root.is_relative_to(request['out']) or
            request['config'].is_relative_to(root)):
        raise ReportError('codex_report_keep_state_outside_package')
    meter, began, query_usage = None, None, None
    try:
        executing_chat = canonical_id(os.environ.get('CODEX_THREAD_ID'))
        meter = Meter(request['out'], engine)
        began = meter.begin(request['home'], executing_chat)
        query_usage = compact(began)
        if not began['terminal']:
            try:
                meter.spawn_watch(began['queryId'], 3600, 2)
                query_usage['watcherStarted'] = True
            except (OSError, ValueError) as exc:
                query_usage['watcherStarted'] = False
                query_usage['watcherErrorCode'] = str(exc) if isinstance(exc, MeterError) else 'query_watch_launch_failed'
    except (OSError, ValueError, TypeError, KeyError) as exc:
        query_usage = _unavailable(exc)

    refreshed, refresh_status = _refresh_once(request, root, engine)
    selection = request['selection']
    proof_path = request['out'] / 'evidence.json'
    try:
        proof_bytes = proof_path.read_bytes()
        bundle, published_selection = verified_existing_report(request['out'])
        if (refreshed['initialSelection'] != selection or published_selection != selection or
                validate_selection(bundle, selection) != selection or
                bundle['source']['timeZone'] != request['timeZone'] or
                bundle['source']['lastScanAt'] != refreshed['sourceLastScanAt'] or
                bundle.get('offlineScan') != refreshed.get('offlineScan')):
            raise ReportError('codex_report_publication_changed')
        projection = project_report(bundle, selection['startDate'], selection['endDate'],
                                    projects=selection['projects'], chats=selection['chats'])
        if (projection['selection'] != {key: selection[key] for key in ('projects', 'chats')} or
                proof_path.read_bytes() != proof_bytes):
            raise ReportError('codex_report_publication_changed')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, ReportError):
            raise
        raise ReportError('codex_report_invalid_published_report') from exc

    # The same query is explicitly sampled once. A provisional/partial result
    # is useful structured output, not an ordinary-report failure.
    if began is not None:
        watcher = {key: value for key, value in query_usage.items() if key.startswith('watcher')}
        try:
            query_usage = {**compact(meter.sample(began['queryId'])), **watcher}
        except (OSError, ValueError, TypeError, KeyError) as exc:
            query_usage = {**_unavailable(exc, began['queryId'], began.get('viewPath')), **watcher}

    dashboard = refreshed['dashboardUrl']
    service = refreshed.get('localServer')
    if service:
        try:
            service = validated_refresh(service)
            if dashboard != selected_dashboard_url(service['dashboardUrl'], selection):
                raise ValueError('selection mismatch')
        except (ValueError, KeyError, TypeError) as exc:
            raise ReportError('codex_report_invalid_dashboard_url') from exc
        if began is not None:
            query_usage['viewUrl'] = service['baseUrl'] + 'query-usage/' + began['queryId'] + '/index.html'
    elif dashboard != dashboard_url(request['out'] / 'index.html', selection):
        raise ReportError('codex_report_invalid_dashboard_url')
    elif began is not None:
        query_usage['viewUrl'] = Path(began['viewPath']).resolve().as_uri()

    warnings = list(dict.fromkeys([*projection['diagnostics']['warnings'], *refreshed.get('warnings', [])]))
    diagnostics = {**projection['diagnostics'], 'warnings': warnings,
                   'status': 'incomplete' if warnings else 'complete',
                   'refreshExitCode': refresh_status,
                   'localDashboardAvailable': service is not None}
    result = {'schemaVersion': 1, 'scope': projection['scope'], 'selection': selection,
              'timeZone': request['timeZone'], 'cutoff': bundle['source']['lastScanAt'],
              'totals': {field: projection['totals'][field] for field in FIELDS},
              'diagnostics': diagnostics, 'dashboardUrl': dashboard, 'queryUsage': query_usage,
              'modelCalls': 0, 'networkUsed': False}
    if proof_path.read_bytes() != proof_bytes:
        raise ReportError('codex_report_publication_changed')
    return result, 2 if warnings or refresh_status == 2 else 0
