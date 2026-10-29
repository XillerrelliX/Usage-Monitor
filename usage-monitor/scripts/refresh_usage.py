#!/usr/bin/env python3
"""Build the local usage dashboard, or export its code without personal data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import webbrowser
import zipfile
from pathlib import Path
from contextlib import ExitStack
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def source_candidates(platform=None, environ=None, home=None):
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    identifier = 'com.ccusage.codex.desktop'
    if platform == 'win32':
        directory = Path(environ.get('APPDATA') or home/'AppData/Roaming')
    elif platform == 'darwin':
        directory = home/'Library/Application Support'
    else:
        directory = Path(environ.get('XDG_DATA_HOME') or home/'.local/share')
    return [directory/identifier/'codex-usage-desktop.db']


def find_database(explicit=None, **options):
    if explicit is not None:
        result = Path(explicit).expanduser().resolve()
        if not result.is_file():
            raise ValueError('The selected local database does not exist: '+str(result))
        return result
    for path in source_candidates(**options):
        if path.is_file():
            return path.resolve()
    raise ValueError('Codex Usage Desktop cache not found. Install/open Codex Usage Desktop, '
                     'refresh its local usage, or run this script with --database PATH.')


def package_files(root=PACKAGE_ROOT):
    root = Path(root).resolve()
    files = []
    for name in ('SKILL.md', 'refresh.cmd', 'refresh.sh', 'setup.cmd', 'setup.sh',
                 'Start-Usage-Monitor.cmd', 'README.md', 'LICENSE', '.gitattributes', '.gitignore'):
        path = root/name
        if path.is_file():
            files.append(path)
    for directory, suffixes in (('scripts', {'.py', '.ps1', '.sh'}), ('assets', {'.html', '.js'}),
                                ('tests', {'.py', '.mjs'})):
        folder = root/directory
        if folder.is_dir():
            files.extend(p for p in folder.iterdir() if p.is_file() and p.suffix in suffixes)
    for name in ('browser-bundle.fixture.json', 'browser-oracles.fixture.json', 'project-metadata.fixture.json'):
        fixture = root/'tests/fixtures'/name
        if fixture.is_file():
            files.append(fixture)
    for name in ('OFL-PublicSans.txt', 'OFL-Sora.txt', 'font-sources.json', 'runtime-downloads.json', 'product.json'):
        asset = root/'assets'/name
        if asset.is_file():
            files.append(asset)
    reference = root/'references/accounting.md'
    if reference.is_file():
        files.append(reference)
    windows_reference = root/'references/windows-package.md'
    if windows_reference.is_file():
        files.append(windows_reference)
    query_reference = root/'references/query-meter.md'
    if query_reference.is_file():
        files.append(query_reference)
    batch_reference = root/'references/codex-report.md'
    if batch_reference.is_file():
        files.append(batch_reference)
    native = root/'native'
    if native.is_dir():
        # Exact public upstream originals plus harness source; never runtime/build state.
        manifest_file = native/'upstream-manifest.json'
        manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
        for relative, expected in manifest['files'].items():
            path = native/relative
            if not path.resolve().is_relative_to(native.resolve()) or not relative.startswith('upstream/'):
                raise ValueError('Invalid native upstream source path.')
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('Pinned upstream source changed: '+relative)
            files.append(path)
        for relative in ('.gitignore', 'Cargo.toml', 'Cargo.lock', 'build.rs', 'README.md', 'verify_sources.py',
                         'upstream-manifest.json', 'patches/offline-only.patch'):
            files.append(native/relative)
        for directory in ('src', 'tests'):
            files.extend(path for path in (native/directory).rglob('*.rs') if path.is_file())
    for path in files:
        if not path.resolve().is_relative_to(root):
            raise ValueError('Export source must remain inside the package: '+str(path))
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def export_package(destination, root=PACKAGE_ROOT):
    root = Path(root).resolve()
    destination = Path(destination).expanduser().resolve()
    files = package_files(root)
    required = {'SKILL.md', 'refresh.cmd', 'refresh.sh', 'setup.cmd', 'setup.sh',
                'scripts/refresh_usage.py', 'scripts/local_setup.py',
                'scripts/report_from_app.py', 'scripts/dashboard_data.py', 'scripts/project_metadata.py',
                'assets/dashboard.html', 'assets/browser_data.js'}
    relative = {p.relative_to(root).as_posix() for p in files}
    if not required <= relative:
        raise ValueError('The portable package is incomplete: '+', '.join(sorted(required-relative)))
    if (root/'scripts/setup_runtime.py').is_file():
        automatic = {'scripts/setup_runtime.py', 'scripts/bootstrap.ps1', 'scripts/bootstrap.sh',
                     'scripts/offline_refresh.py', 'assets/runtime-downloads.json',
                     'native/Cargo.toml', 'native/Cargo.lock', 'native/upstream-manifest.json'}
        if not automatic <= relative:
            raise ValueError('The automatic setup package is incomplete: '+', '.join(sorted(automatic-relative)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = {}
    descriptor, temporary = tempfile.mkstemp(prefix='.usage-export-', suffix='.zip', dir=destination.parent)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                name = path.relative_to(root).as_posix()
                payload = path.read_bytes()
                manifest[name] = hashlib.sha256(payload).hexdigest()
                info = zipfile.ZipInfo('codex-local-usage/'+name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
                archive.writestr(info, payload)
            archive.writestr('codex-local-usage/package-manifest.json',
                             json.dumps({'schemaVersion': 1, 'files': manifest,
                                         'privateUsageDataIncluded': False}, indent=2)+'\n')
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'export': str(destination), 'sourceFiles': len(files), 'privateUsageDataIncluded': False}


def copy_runtime(destination, root=PACKAGE_ROOT):
    destination, root = Path(destination).resolve(), Path(root).resolve()
    if destination == root:
        return
    for source in package_files(root):
        target = destination/source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _script_json(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).replace(
        '<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def selection_arguments(parser):
    """Share the exact public selection flags across source and Windows entry."""
    parser.add_argument('--from-date', help='Inclusive app calendar date YYYY-MM-DD; requires --to-date.')
    parser.add_argument('--to-date', help='Inclusive app calendar date YYYY-MM-DD; requires --from-date.')
    parser.add_argument('--project-id', action='append', help='Exact verified named-project ID; repeat for multiple projects.')
    parser.add_argument('--chat-id', action='append', help='Exact verified chat-family ID including agents; repeat for multiple chats.')
    parser.add_argument('--selection-file', type=Path, help=argparse.SUPPRESS)


def requested_selection(args, root=PACKAGE_ROOT):
    """Read only the saved dashboard's bounded filter state, never its ledger."""
    explicit = any(value is not None for value in (args.from_date, args.to_date, args.project_id, args.chat_id))
    if explicit:
        from report_from_app import selected_window
        if args.from_date is not None or args.to_date is not None:
            start, end = selected_window(args.from_date, args.to_date, None, None)
        else:
            start = end = None
        return {'startDate': start, 'endDate': end,
                'projects': list(args.project_id or []), 'chats': list(args.chat_id or [])}
    path = args.selection_file or Path(root)/'.usage-selection.json'
    if not path.exists() and args.selection_file is None:
        return None
    if path.stat().st_size > 65536:
        raise ValueError('Saved dashboard selection is too large; original bytes were preserved.')
    try:
        from report_from_app import strict_object
        value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=strict_object)
        if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'selection'} or
                type(value['schemaVersion']) is not int or value['schemaVersion'] != 1):
            raise ValueError('Unsupported saved dashboard selection.')
        selection = value['selection']
        if selection is not None and (not isinstance(selection, dict) or
                set(selection) != {'startDate', 'endDate', 'projects', 'chats'}):
            raise ValueError('Invalid saved dashboard selection.')
        return selection
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Invalid saved dashboard selection; original bytes were preserved. '+str(exc)) from exc


def validate_selection(bundle, selection, now=None):
    """Validate dates and stable IDs against the same verified project view as HTML."""
    if selection is None:
        return None
    from report_from_app import selected_window
    from project_metadata import prepare_project_view
    from dashboard_data import project_bundle
    start, end = selection['startDate'], selection['endDate']
    if start is None and end is None:
        try:
            name = bundle['source']['timeZone']
            zone = timezone.utc if name in ('UTC', 'Etc/UTC', 'GMT', 'Etc/GMT') else ZoneInfo(name)
        except (KeyError, ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError('The app timezone is unavailable; choose explicit calendar dates.') from exc
        start = end = (now or datetime.now(timezone.utc)).astimezone(zone).date().isoformat()
    start, end = selected_window(start, end, None, None)
    prepared = prepare_project_view(bundle)
    normalized = {'startDate': start, 'endDate': end}
    for dimension, catalog in (('projects', prepared['appProjects']), ('chats', prepared['chats'])):
        values = selection[dimension]
        if (not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values)):
            raise ValueError('Selected '+dimension+' must contain nonempty exact stable IDs.')
        identities = {row['id'] for row in catalog}
        missing = sorted(set(values)-identities)
        if missing:
            raise ValueError('Unknown '+dimension+' ID in the verified catalog: '+', '.join(missing))
        normalized[dimension] = sorted(set(values))
    # Exercise the production projection so unavailable attribution fails closed.
    project_bundle(prepared, start, end, projects=normalized['projects'], chats=normalized['chats'], now=now)
    return normalized


def dashboard_url(dashboard, selection=None):
    url = Path(dashboard).resolve().as_uri()
    return selected_dashboard_url(url, selection)


def selected_dashboard_url(url, selection=None):
    """Apply the exact validated filters to a file or loopback dashboard URL."""
    if selection is None:
        return url
    pairs = [('from', selection['startDate']), ('to', selection['endDate'])]
    pairs.extend(('project', value) for value in selection['projects'])
    pairs.extend(('chat', value) for value in selection['chats'])
    return url+'#'+urlencode(pairs)


def validated_refresh(refresh):
    """Keep a saved page's refresh capability on one tokenized loopback origin."""
    if refresh is None:
        return None
    if not isinstance(refresh, dict) or not {'baseUrl', 'refreshUrl'} <= set(refresh):
        raise ValueError('Invalid local dashboard refresh configuration.')
    base, target = refresh['baseUrl'], refresh['refreshUrl']
    if not isinstance(base, str) or not isinstance(target, str):
        raise ValueError('Invalid local dashboard refresh URL.')
    parsed = urlsplit(base)
    try:
        valid = (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and
                 parsed.port is not None and 1 <= parsed.port <= 65535 and
                 parsed.username is None and parsed.password is None and
                 not parsed.query and not parsed.fragment and
                 re.fullmatch(r'/[A-Za-z0-9_-]{24,128}/', parsed.path) is not None and
                 target == base + 'refresh.html')
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Dashboard refresh must use its tokenized local loopback service.')
    result = {'baseUrl': base, 'refreshUrl': target}
    if 'dashboardUrl' in refresh:
        if refresh['dashboardUrl'] != base + 'index.html':
            raise ValueError('Invalid local dashboard URL.')
        result['dashboardUrl'] = refresh['dashboardUrl']
    if 'autoRefresh' in refresh:
        if type(refresh['autoRefresh']) is not bool:
            raise ValueError('Invalid local dashboard refresh state.')
        result['autoRefresh'] = refresh['autoRefresh']
    return result


def render_dashboard(bundle, root=PACKAGE_ROOT, selection=None, refresh=None):
    root = Path(root)
    refresh = validated_refresh(refresh)
    template = (root/'assets/dashboard.html').read_text(encoding='utf-8')
    runtime = (root/'assets/browser_data.js').read_text(encoding='utf-8')
    if template.count('__DATA_JSON__') != 1 or template.count('/*__BROWSER_RUNTIME__*/') != 1:
        raise ValueError('Dashboard code must contain exactly one data and browser-runtime placeholder.')
    # Data is substituted last. Names and paths remain data, including HTML or marker-like text.
    html = template.replace('/*__BROWSER_RUNTIME__*/', runtime)
    if '/*__QUERY_METER__*/' in html:
        if html.count('/*__QUERY_METER__*/') != 1:
            raise ValueError('Dashboard query meter must have exactly one runtime placeholder.')
        html = html.replace('/*__QUERY_METER__*/', (root/'assets/query_meter.js').read_text(encoding='utf-8'))
    if '/*__REFRESH_RUNTIME__*/' in html:
        if html.count('/*__REFRESH_RUNTIME__*/') != 1:
            raise ValueError('Dashboard refresh must have exactly one runtime placeholder.')
        html = html.replace('/*__REFRESH_RUNTIME__*/', (root/'assets/refresh_dashboard.js').read_text(encoding='utf-8'))
    if html.count('__DATA_JSON__') != 1:
        raise ValueError('Browser code introduced an ambiguous data placeholder.')
    if '__INITIAL_SELECTION_JSON__' in html:
        if html.count('__INITIAL_SELECTION_JSON__') != 1:
            raise ValueError('Dashboard initial selection must have exactly one placeholder.')
    payload = _script_json({'browserBundle': bundle,
                            **({'initialSelection': selection} if selection is not None else {}),
                            **({'refresh': refresh} if refresh is not None else {})})
    # One replacement pass keeps marker-like IDs/names as inert data.
    initial_payload = _script_json(selection)
    return re.sub(r'__DATA_JSON__|__INITIAL_SELECTION_JSON__',
                  lambda match: payload if match.group() == '__DATA_JSON__' else initial_payload, html)


def write_outputs(out, bundle, proof, root=PACKAGE_ROOT, selection=None, refresh=None):
    out = Path(out).resolve()
    html = render_dashboard(bundle, root, selection=selection, refresh=refresh)
    data = json.dumps({'browserBundle': bundle, **({'initialSelection': selection} if selection is not None else {})},
                      ensure_ascii=False, indent=2, allow_nan=False)+'\n'
    evidence = {**proof, 'htmlSha256': hashlib.sha256(html.encode()).hexdigest(),
                'dataSha256': hashlib.sha256(data.encode()).hexdigest(),
                'browserFilteringLocal': True, 'modelCalls': 0, 'networkUsed': False}
    if 'projectMetadata' in bundle:
        evidence['projectMetadataSha256'] = hashlib.sha256(json.dumps(
            bundle['projectMetadata'], ensure_ascii=True, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    coverage = bundle['coverage']
    summary = ['Local Codex usage dashboard',
               'Available history: '+str(coverage['startDate'])+' to '+str(coverage['endDate']),
               'Recorded days: '+str(len(coverage['recordedDays']))+' | Chats: '+str(len(bundle['chats'])),
               'Native cache last scanned: '+str(bundle['source']['lastScanAt']),
               'Choose dates, project and chat in index.html. Graphs appear from two days.',
               'Run refresh.cmd or python3 scripts/refresh_usage.py to update saved data.',
               'Filtering and direct refresh use no model calls. Offline mode updates its own local cache.']
    summary.extend('WARNING: '+str(warning) for warning in bundle['diagnostics']['warnings'])
    contents = {'index.html': html, 'data.json': data,
                '.usage-selection.json': json.dumps({'schemaVersion': 1, 'selection': selection},
                                                    ensure_ascii=True, allow_nan=False)+'\n',
                'evidence.json': json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False)+'\n',
                'summary.txt': '\n'.join(summary)+'\n'}
    out.mkdir(parents=True, exist_ok=True)
    pending = []
    try:
        for name, content in contents.items():
            descriptor, temporary = tempfile.mkstemp(prefix='.'+name+'-', suffix='.tmp', dir=out)
            pending.append((Path(temporary), out/name))
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
                stream.write(content)
        for temporary, target in pending:
            os.replace(temporary, target)
    finally:
        for temporary, _ in pending:
            if temporary.exists():
                temporary.unlink()
    copy_runtime(out, root)
    return out/'index.html'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, help='Override automatic local Codex Usage Desktop cache discovery.')
    parser.add_argument('--names-database', type=Path, action='append', help='Optional local sidebar id/name database.')
    parser.add_argument('--project-state', type=Path, help='Optional local Codex project/workspace metadata JSON override.')
    parser.add_argument('--out', type=Path, help='Dashboard folder; default: saved folder, or this user\'s local data directory.')
    parser.add_argument('--cache-file', type=Path, help='Canonical ledger cache; default: <out>/usage-ledger.json.')
    parser.add_argument('--refresh', action='store_true', help='Revalidate source counters even when the source revision is unchanged.')
    parser.add_argument('--no-open', action='store_true', help='Generate files without opening a browser window.')
    parser.add_argument('--serve-dashboard', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--no-serve', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--config', type=Path, help='Local settings file; default: this user\'s OS configuration directory.')
    parser.add_argument('--setup', action='store_true', help='Set up this machine and generate the dashboard; safe to repeat.')
    parser.add_argument('--offline', action='store_true', help='Scan local sessions with the upstream offline parser into a separate owned cache.')
    parser.add_argument('--read-cache', action='store_true', help='Read an existing desktop cache without running a scanner; --database also selects this mode.')
    parser.add_argument('--codex-home', type=Path, help='Local Codex source directory for offline mode; defaults to CODEX_HOME or ~/.codex.')
    parser.add_argument('--timezone', help='IANA timezone for offline day boundaries; defaults to the saved or OS timezone.')
    parser.add_argument('--engine', type=Path, help='Explicit offline scanner executable, primarily for verified development fixtures.')
    parser.add_argument('--runtime-dir', type=Path, help='User-local dependency/build directory; default: saved settings directory/runtime.')
    parser.add_argument('--export', type=Path, metavar='ZIP', help='Export code and launchers only; never include personal cache or reports.')
    selection_arguments(parser)
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 10):
            raise ValueError('Python 3.10 or newer is required.')
        if args.export:
            print(json.dumps(export_package(args.export)))
            return 0
        selection = requested_selection(args, PACKAGE_ROOT)
        with ExitStack() as operations:
            # Import lazily: exporting and source discovery do not need a database or accounting run.
            from dashboard_data import build_bundle
            from project_metadata import resolve_project_metadata
            from local_setup import (default_dashboard_directory, default_runtime_directory, resolve_settings_file,
                                     load_settings, save_settings, save_dashboard_pointer, save_interpreter_hint)
            settings_file = resolve_settings_file(PACKAGE_ROOT, args.config)
            saved = load_settings(settings_file) or {}
            default_out = settings_file.parent/'dashboard' if args.config else default_dashboard_directory()
            out = Path(args.out or saved.get('outputDirectory') or default_out).expanduser().resolve()
            selected_names = args.names_database if args.names_database is not None else saved.get('namesDatabases')
            selected_names = [Path(path).expanduser().resolve() for path in selected_names] if selected_names else None
            names = selected_names
            project_state = args.project_state or saved.get('projectState')
            project_state = Path(project_state).expanduser().resolve() if project_state else None
            selected_cache = args.cache_file or saved.get('cacheFile')
            selected_cache = Path(selected_cache).expanduser().resolve() if selected_cache else None
            cache_file = selected_cache or out/'usage-ledger.json'
            from file_transaction import preserve_files
            protected = [cache_file, settings_file, out/'.usage-setup.json', out/'.usage-python',
                         settings_file.parent/'.usage-python',
                         *(out/name for name in ('index.html', 'data.json', 'evidence.json', 'summary.txt',
                                                '.usage-selection.json')),
                         *(out/path.relative_to(PACKAGE_ROOT) for path in package_files(PACKAGE_ROOT))]
            operations.enter_context(preserve_files(protected))
            offline = (not (args.database or args.read_cache) and
                       (args.offline or args.codex_home or saved.get('offline') or not saved))
            scan_details, offline_settings = None, None
            if offline:
                from offline_refresh import local_home, prepare_database
                previous = saved.get('offline', {})
                home = local_home(args.codex_home or previous.get('codexHome'))
                zone = args.timezone or previous.get('timezone')
                default_runtime = (settings_file.parent/'runtime' if args.config or
                                   (PACKAGE_ROOT/'.usage-setup.json').is_file() else default_runtime_directory())
                runtime = Path(args.runtime_dir or previous.get('runtimeDirectory') or default_runtime).expanduser().resolve()
                engine = args.engine or previous.get('engine')
                managed_engine = engine is not None and Path(engine).parent.resolve() == (runtime/'bin').resolve()
                if engine is None or not Path(engine).is_file() or (managed_engine and args.engine is None):
                    from setup_runtime import ensure_engine
                    engine = ensure_engine(PACKAGE_ROOT, runtime)['engine']
                engine = Path(engine).expanduser().resolve()
                packages = runtime/'python-packages'
                if packages.is_dir() and str(packages) not in sys.path:
                    sys.path.insert(0, str(packages))
                database = settings_file.parent/'offline-source.sqlite'
                if selected_names is None:
                    candidates = []
                    for path in home.glob('state_*.sqlite'):
                        match = re.fullmatch(r'state_(\d+)\.sqlite', path.name)
                        if match and path.is_file():
                            candidates.append((int(match.group(1)), path))
                    names = [max(candidates, key=lambda item: item[0])[1]] if candidates else []
                if any(Path(path).resolve().is_relative_to(home) for path in protected):
                    raise ValueError('Keep dashboard, settings and ledger outputs outside the Codex source home.')
                prepared = operations.enter_context(prepare_database(
                    PACKAGE_ROOT, database, home, timezone=zone, runtime_dir=runtime, engine=engine))
                # The original independent accounting gates run on unpublished data.
                staged, staged_proof, _ = build_bundle(prepared.path, names, cache_file=None)
                if selection is not None:
                    staged = {**staged, 'projectMetadata': resolve_project_metadata(
                        staged, names, global_state_file=project_state or home/'.codex-global-state.json')}
                    selection = validate_selection(staged, selection)
                render_dashboard(staged, selection=selection)  # Validate before publishing a source snapshot.
                prepared.publish()
                scan_details = prepared.details
                offline_settings = {'codexHome': str(home), 'timezone': scan_details['timezone'],
                                    'engine': str(engine), 'runtimeDirectory': str(runtime)}
            else:
                database = find_database(args.database or saved.get('database'))
            bundle, proof, meta = build_bundle(database, names,
                                             cache_file=cache_file, refresh=args.refresh)
            if offline:
                bundle = {**bundle, 'source': {**bundle['source'], 'name': 'Upstream parser (offline)',
                                               'mode': 'offline'}, 'offlineScan': scan_details}
                proof = {**proof, 'offlineScanner': {'sourceCommit': scan_details['sourceCommit'],
                                                    'scanId': scan_details['scanId'],
                                                    'nativeCachePublishedAfterValidation': True}}
            display_project_state = project_state or (home/'.codex-global-state.json' if offline else None)
            project_metadata = resolve_project_metadata(bundle, names,
                                                        global_state_file=display_project_state)
            bundle = {**bundle, 'projectMetadata': project_metadata}
            project_warnings = project_metadata.get('diagnostics', {}).get('warnings', [])
            if project_warnings:
                combined_warnings = list(dict.fromkeys([*bundle['diagnostics']['warnings'], *project_warnings]))
                bundle['diagnostics'] = {**bundle['diagnostics'], 'status': 'incomplete',
                                         'warnings': combined_warnings}
            selection = validate_selection(bundle, selection)
            dashboard = write_outputs(out, bundle, proof, selection=selection)
            url = dashboard_url(dashboard, selection)
            settings = {
                'schemaVersion': 2 if offline else 1, 'database': str(database),
                'namesDatabases': [str(path) for path in selected_names] if selected_names else None,
                'projectState': str(project_state) if project_state else None,
                'outputDirectory': str(out),
                'cacheFile': str(selected_cache) if selected_cache else None}
            if offline:
                settings['offline'] = offline_settings
            setup_status = save_settings(settings_file, settings)
            save_dashboard_pointer(out, settings_file)
            if offline:
                save_interpreter_hint(out, sys.executable)
                save_interpreter_hint(settings_file.parent, sys.executable)
            warnings = bundle['diagnostics']['warnings']
            coverage = bundle['coverage']
            result = {'dashboard': str(dashboard), 'dashboardUrl': url,
                      'initialSelection': selection, 'cacheFile': str(Path(cache_file).resolve()),
                      'coverage': {'startDate': coverage['startDate'], 'endDate': coverage['endDate'],
                                   'recordedDayCount': len(coverage['recordedDays'])}, 'chats': len(bundle['chats']),
                      'owners': len(bundle['owners']), 'records': len(bundle['records']),
                      'sourceLastScanAt': bundle['source']['lastScanAt'],
                      'cache': meta, 'setup': {'status': setup_status, 'settingsFile': str(settings_file)},
                      'warnings': warnings, 'modelCalls': 0, 'networkUsed': False}
            if scan_details is not None:
                result['offlineScan'] = scan_details
        # Startup happens only after the existing publication/rollback context
        # has committed validated source, report and settings together.
        result['fileDashboardUrl'] = url
        if args.serve_dashboard and not args.no_serve:
            try:
                from refresh_server import ensure_server
                service = ensure_server(PACKAGE_ROOT, settings_file, out)
                result['dashboardUrl'] = selected_dashboard_url(service['dashboardUrl'], selection)
                result['refreshUrl'] = service['refreshUrl']
                result['localServer'] = service
            except (OSError, ValueError, RuntimeError) as exc:
                warning = 'Dashboard saved, but local refresh is unavailable: ' + str(exc)
                warnings = [*warnings, warning]
                result['warnings'] = warnings
                result['localRefreshError'] = str(exc)
        print(json.dumps(result))
        if not args.no_open:
            webbrowser.open(result['dashboardUrl'])
        return 2 if warnings else 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc), 'modelCalls': 0, 'networkUsed': False}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
