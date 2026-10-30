"""Start the portable Usage Monitor dashboard using its shipped offline engine."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _package_api(root=PACKAGE_ROOT):
    from macos_runtime import is_macos_bundle
    if is_macos_bundle(root):
        from macos_package import verify_bundle, reexport_bundle
        from macos_runtime import resolve_prebuilt
    else:
        from windows_package import verify_bundle, reexport_bundle
        from prebuilt_runtime import resolve_prebuilt
    return verify_bundle, reexport_bundle, resolve_prebuilt


def _package_label(root):
    from macos_runtime import is_macos_bundle
    return 'macOS' if is_macos_bundle(root) else 'Windows'


def _local_path(value):
    value = Path(value).expanduser()
    if str(value).startswith('\\\\'):
        raise ValueError('Usage Monitor uses local files; choose a path on this computer instead of a network share.')
    return value.resolve()


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if '--codex-report' in arguments:
        try:
            from codex_report import parse_arguments, run_report, ReportError
            report_args = parse_arguments(arguments)
            _local_path(PACKAGE_ROOT)
            verify_bundle, _, resolve_prebuilt = _package_api(PACKAGE_ROOT)
            verify_bundle(PACKAGE_ROOT)
            installed = resolve_prebuilt(PACKAGE_ROOT, PACKAGE_ROOT / 'runtime')
            if installed['pythonPackages'] not in sys.path:
                sys.path.insert(0, installed['pythonPackages'])
            result, status = run_report(report_args, PACKAGE_ROOT, installed['engine'])
            print(json.dumps(result, ensure_ascii=True, separators=(',', ':'), allow_nan=False))
            return status
        except (OSError, ValueError, RuntimeError) as exc:
            code = exc.code if isinstance(exc, ReportError) else 'codex_report_local_operation_failed'
            result = {'schemaVersion': 1, 'errorCode': code, 'modelCalls': 0, 'networkUsed': False}
            if isinstance(exc, ReportError) and exc.detail:
                result['error'] = exc.detail
            print(json.dumps(result, ensure_ascii=True, separators=(',', ':')), file=sys.stderr)
            return 1
    if arguments[:1] == ['--query-meter']:
        try:
            _local_path(PACKAGE_ROOT)
            verify_bundle, _, resolve_prebuilt = _package_api(PACKAGE_ROOT)
            verify_bundle(PACKAGE_ROOT)
            installed = resolve_prebuilt(PACKAGE_ROOT, PACKAGE_ROOT / 'runtime')
            if installed['pythonPackages'] not in sys.path:
                sys.path.insert(0, installed['pythonPackages'])
            if '--engine' in arguments[1:] or any(value.startswith('--engine=') for value in arguments[1:]):
                raise ValueError('The ' + _package_label(PACKAGE_ROOT) + ' query meter uses its verified shipped engine.')
            from query_meter import main as query_main
            meter_arguments = arguments[1:]
            if meter_arguments and meter_arguments[0] not in {'--help', '-h'}:
                meter_arguments.extend(['--engine', installed['engine']])
            return query_main(meter_arguments)
        except (OSError, ValueError, RuntimeError) as exc:
            print(json.dumps({'error': str(exc), 'modelCalls': 0, 'networkUsed': False}), file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for name, help_text in (
            ('config', 'Local settings file; default: this profile\'s usage-monitor settings folder.'),
            ('out', 'Dashboard folder; default: <settings folder>/dashboard on first use.'),
            ('codex-home', 'Local Codex home; uses your saved choice, CODEX_HOME or ~/.codex.'),
            ('cache-file', 'Local canonical ledger cache.'),
            ('project-state', 'Optional local project metadata JSON.'),
            ('export', 'Export the public ' + _package_label(PACKAGE_ROOT) + ' bundle ZIP without personal data.')):
        parser.add_argument('--' + name, type=Path, help=help_text)
    parser.add_argument('--names-database', type=Path, action='append', help='Optional local sidebar name database.')
    parser.add_argument('--timezone', help='IANA timezone for local day boundaries.')
    parser.add_argument('--no-open', action='store_true', help='Generate files without opening the browser.')
    parser.add_argument('--serve-only', action='store_true', help='Open the existing verified dashboard without scanning local usage.')
    parser.add_argument('--no-serve', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--refresh', action='store_true', help='Revalidate source counters.')
    from refresh_usage import selection_arguments
    selection_arguments(parser)
    args = parser.parse_args(arguments)
    try:
        _local_path(PACKAGE_ROOT)
        for name in ('config', 'out', 'codex_home', 'cache_file', 'project_state', 'export', 'selection_file'):
            value = getattr(args, name)
            if value is not None:
                setattr(args, name, _local_path(value))
        if args.names_database:
            args.names_database = [_local_path(value) for value in args.names_database]
        verify_bundle, reexport_bundle, resolve_prebuilt = _package_api(PACKAGE_ROOT)
        verify_bundle(PACKAGE_ROOT)
        installed = resolve_prebuilt(PACKAGE_ROOT, PACKAGE_ROOT / 'runtime')
        if args.export:
            print(json.dumps(reexport_bundle(PACKAGE_ROOT, args.export)))
            return 0
        packages = installed['pythonPackages']
        if packages not in sys.path:
            sys.path.insert(0, packages)
        from local_setup import load_settings, default_settings_file
        default_config = default_settings_file().parent.parent / 'usage-monitor' / 'settings.json'
        config = _local_path(args.config or default_config)
        saved = load_settings(config) or {}
        for name, option in (('outputDirectory', 'out'), ('cacheFile', 'cache_file'), ('projectState', 'project_state')):
            if getattr(args, option) is None and saved.get(name):
                _local_path(saved[name])
        for value in (saved.get('namesDatabases') or []) if args.names_database is None else []:
            _local_path(value)
        arguments = ['--offline', '--config', str(config), '--engine', installed['engine'],
                     '--runtime-dir', str(PACKAGE_ROOT / 'runtime')]
        out = args.out or saved.get('outputDirectory') or config.parent / 'dashboard'
        if args.serve_only:
            if args.no_serve:
                raise ValueError('--serve-only cannot be combined with --no-serve.')
            if any(getattr(args, name) is not None for name in (
                    'codex_home', 'cache_file', 'project_state', 'timezone', 'names_database')) or args.refresh:
                raise ValueError('--serve-only uses the saved report and settings; source overrides require a normal refresh.')
            if not saved:
                raise ValueError('--serve-only requires existing local settings and a verified dashboard.')
            from refresh_server import ensure_server, verified_existing_report
            from refresh_usage import requested_selection, validate_selection, dashboard_url, selected_dashboard_url
            bundle, saved_selection = verified_existing_report(out)
            selection = requested_selection(args, root=out)
            if selection is None:
                selection = saved_selection
            selection = validate_selection(bundle, selection)
            service = ensure_server(PACKAGE_ROOT, config, out)
            dashboard = Path(out).resolve() / 'index.html'
            url = selected_dashboard_url(service['dashboardUrl'], selection)
            result = {'dashboard': str(dashboard), 'dashboardUrl': url,
                      'fileDashboardUrl': dashboard_url(dashboard, selection),
                      'initialSelection': selection, 'refreshUrl': service['refreshUrl'],
                      'localServer': service, 'scanPerformed': False,
                      'setup': {'status': 'reused', 'settingsFile': str(config)},
                      'modelCalls': 0, 'networkUsed': False}
            print(json.dumps(result))
            if not args.no_open:
                import webbrowser
                webbrowser.open(url)
            return 0
        arguments.extend(['--out', str(out)])
        for name in ('codex_home', 'cache_file', 'project_state', 'timezone', 'from_date', 'to_date', 'selection_file'):
            value = getattr(args, name)
            if value is not None:
                arguments.extend(['--' + name.replace('_', '-'), str(value)])
        for value in args.names_database or []:
            arguments.extend(['--names-database', str(value)])
        for name in ('project_id', 'chat_id'):
            for value in getattr(args, name) or []:
                arguments.extend(['--' + name.replace('_', '-'), value])
        for name in ('no_open', 'refresh'):
            if getattr(args, name):
                arguments.append('--' + name.replace('_', '-'))
        arguments.append('--no-serve' if args.no_serve else '--serve-dashboard')
        # This is the existing accounting pipeline, including unpublished-data
        # checks, conservation gates, rollback and atomic report publication.
        from refresh_usage import main as refresh
        import report_from_app
        original_action = report_from_app.REFRESH_ACTION
        report_from_app.REFRESH_ACTION = (
            'Run Usage Monitor again; if the error persists, keep the previous data '
            'and re-extract the verified ' + _package_label(PACKAGE_ROOT) + ' package.')
        try:
            return refresh(arguments)
        finally:
            report_from_app.REFRESH_ACTION = original_action
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({'error': str(exc), 'modelCalls': 0, 'networkUsed': False}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
