"""Synthetic loopback tests; never discover a real source, account, or credential."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'scripts'))
import dashboard_data
import local_setup
import refresh_server as service
import refresh_usage
import test_scopes as scopes
from test_scopes import fixture, ROOT, OTHER, A, B, NOW


SELECTION = {'startDate': '2026-10-24', 'endDate': '2026-10-26',
             'projects': sorted([A, B]), 'chats': sorted([ROOT, OTHER])}


class ServerFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='usage refresh ü ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.out = self.root / 'settings.json', self.root / 'dashboard'
        self.database, self.names = fixture(self.root)
        self.bundle, self.proof, _ = dashboard_data.build_bundle(self.database, [self.names], now=NOW)
        self.publish()
        local_setup.save_settings(self.config, {
            'schemaVersion': 1, 'database': str(self.database), 'namesDatabases': [str(self.names)],
            'projectState': None, 'outputDirectory': str(self.out), 'cacheFile': None})
        self.servers = []
        self.addCleanup(self.close_servers)

    def publish(self, selection=SELECTION):
        # Production renderer/proof/atomic publication; only copying an entire
        # developer source tree is unnecessary for a loopback unit fixture.
        with patch.object(refresh_usage, 'copy_runtime'):
            refresh_usage.write_outputs(self.out, self.bundle, self.proof, selection=copy.deepcopy(selection))

    def start(self, runner=None, idle_timeout=30, metadata_path=None):
        server = service.create_server(PACKAGE, self.config, self.out,
                                       idle_timeout=idle_timeout, runner=runner or (lambda _: None))
        thread = threading.Thread(target=service.serve, args=(server, metadata_path), daemon=True)
        self.servers.append((server, thread))
        thread.start()
        return server

    def close_servers(self):
        for server, thread in self.servers:
            if thread.is_alive():
                server.shutdown()
                thread.join(5)
            server.server_close()

    def request(self, server, route, *, method='GET', body=None, headers=None, raw=False):
        supplied = {'Host': server.host}
        if method == 'POST':
            supplied.update({'Origin': server.origin, 'X-Usage-Monitor': 'refresh',
                             'Content-Type': 'application/json'})
        if headers:
            for key, value in headers.items():
                if value is None:
                    supplied.pop(key, None)
                else:
                    supplied[key] = value
        if body is not None and not isinstance(body, (bytes, str)):
            body = json.dumps(body).encode('utf-8')
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        try:
            path = route if raw else '/' + server.state.token + '/' + route
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            data = response.read()
            response_headers = dict(response.getheaders())
            return response.status, data, response_headers
        finally:
            connection.close()

    def post(self, server, body=SELECTION, **kwargs):
        status, data, headers = self.request(server, 'api/refresh', method='POST', body=body, **kwargs)
        return status, json.loads(data), headers

    def terminal(self, server, job_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, data, _ = self.request(server, 'api/jobs/' + job_id)
            self.assertEqual(status, 200)
            job = json.loads(data)
            if job['status'] != 'running':
                return job
            time.sleep(0.02)
        self.fail('Synthetic refresh did not finish within its bounded wait')


class SavedReportTests(ServerFixture):
    def test_existing_saved_report_checks_original_proof_and_selection(self):
        bundle, selection = service.verified_existing_report(self.out)
        self.assertEqual(bundle, self.bundle)
        self.assertEqual(selection, SELECTION)
        data = self.out / 'data.json'
        before = data.read_bytes()
        data.write_bytes(before + b' ')
        with self.assertRaisesRegex(service.RefreshError, '^refresh_invalid_report$'):
            service.verified_existing_report(self.out)

    def test_rehashed_but_inconsistent_numeric_report_is_refused(self):
        path = self.out / 'data.json'
        data = json.loads(path.read_bytes())
        data['browserBundle']['daily'][0]['totalTokens'] += 1
        payload = json.dumps(data).encode('utf-8')
        path.write_bytes(payload)
        evidence_path = self.out / 'evidence.json'
        evidence = json.loads(evidence_path.read_bytes())
        evidence['dataSha256'] = hashlib.sha256(payload).hexdigest()
        evidence_path.write_text(json.dumps(evidence), encoding='utf-8')
        with self.assertRaisesRegex(service.RefreshError, '^refresh_invalid_report$'):
            service.verified_existing_report(self.out)

    def test_tampered_html_is_not_served_even_with_intact_data(self):
        (self.out / 'index.html').write_text('<script>private payload</script>', encoding='utf-8')
        with self.assertRaisesRegex(service.RefreshError, '^refresh_invalid_report$'):
            self.start()

    def test_profile_requires_exact_saved_output_and_private_state(self):
        wrong = self.root / 'different dashboard'
        wrong.mkdir()
        with self.assertRaisesRegex(service.RefreshError, 'profile_changed'):
            service.create_server(PACKAGE, self.config, wrong)
        with self.assertRaisesRegex(service.RefreshError, 'outside_package'):
            service.create_server(PACKAGE, PACKAGE / 'settings.json', self.out)


class HttpBoundaryTests(ServerFixture):
    def test_html_is_dynamically_bound_without_changing_saved_snapshot(self):
        server = self.start()
        path = self.out / 'index.html'
        before = path.read_bytes(), path.stat().st_mtime_ns
        for route, auto in [('index.html', False), ('refresh.html', True)]:
            with self.subTest(route=route):
                status, data, headers = self.request(server, route)
                self.assertEqual(status, 200)
                text = data.decode('utf-8')
                self.assertIn(json.dumps(server.state.base_url), text)
                self.assertIn('"autoRefresh":' + str(auto).lower(), text)
                self.assertIn('"initialSelection":', text)
                self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(headers['X-Frame-Options'], 'DENY')
                self.assertIn("connect-src 'self'", headers['Content-Security-Policy'])
                self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_only_capability_and_explicit_routes_are_available(self):
        server = self.start()
        denied = ['data.json', 'evidence.json', 'usage-ledger.json', 'auth.json', '.usage-setup.json',
                  '.usage-selection.json', 'scripts/standalone_app.py', 'runtime/python/python.exe',
                  '../settings.json', '%2e%2e/settings.json', 'api/../ping', 'api%2fping',
                  'api\\ping', 'api/ping?command=anything', 'query-usage/latest.json',
                  'query-usage/active.json', 'query-usage/../settings.json', '//index.html']
        for route in denied:
            with self.subTest(route=route):
                status, _, _ = self.request(server, route)
                self.assertEqual(status, 404)
        for raw in ['/index.html', '/wrong-capability/index.html',
                    'http://127.0.0.1:' + str(server.server_port) + '/' + server.state.token + '/index.html']:
            self.assertEqual(self.request(server, raw, raw=True)[0], 404)

    def test_exact_host_and_origin_custom_header_are_required(self):
        launches = []
        server = self.start(runner=lambda selection: launches.append(selection))
        for host in ('localhost:' + str(server.server_port), 'example.invalid', server.host + '.evil', '127.0.0.1'):
            with self.subTest(host=host):
                self.assertEqual(self.request(server, 'api/ping', headers={'Host': host})[0], 403)
        denied = [ {'Origin': None}, {'Origin': 'null'}, {'Origin': 'https://example.invalid'},
                   {'Origin': server.origin + '/'}, {'X-Usage-Monitor': None}, {'X-Usage-Monitor': 'other'},
                   {'Origin': 'http://localhost:' + str(server.server_port)} ]
        for headers in denied:
            with self.subTest(headers=headers):
                self.assertEqual(self.post(server, headers=headers)[0], 403)
        self.assertEqual(self.request(server, 'api/refresh', method='OPTIONS')[0], 403)
        self.assertEqual(launches, [])

    def test_duplicate_host_and_origin_headers_fail_closed(self):
        server = self.start()
        payload = json.dumps(SELECTION).encode('ascii')
        for extra in ('Host: ' + server.host, 'Origin: ' + server.origin):
            raw = ('POST /' + server.state.token + '/api/refresh HTTP/1.0\r\nHost: ' + server.host +
                   '\r\nOrigin: ' + server.origin + '\r\nX-Usage-Monitor: refresh\r\n' +
                   'Content-Type: application/json\r\nContent-Length: ' + str(len(payload)) +
                   '\r\n' + extra + '\r\n\r\n').encode('ascii') + payload
            with socket.create_connection(('127.0.0.1', server.server_port), timeout=5) as connection:
                connection.sendall(raw)
                self.assertIn(b' 403 ', connection.recv(4096).split(b'\r\n')[0])

    def test_body_content_type_size_and_duplicate_fields_are_bounded(self):
        server = self.start()
        for headers, expected in [({'Content-Type': 'text/plain'}, 400),
                                  ({'Content-Type': 'application/json; charset=utf-8'}, 400),
                                  ({'Transfer-Encoding': 'chunked'}, 400),
                                  ({'Content-Length': str(service.MAX_BODY + 1)}, 413),
                                  ({'Content-Length': '999999999'}, 413),
                                  ({'Content-Length': '-1'}, 400)]:
            with self.subTest(headers=headers):
                self.assertEqual(self.post(server, headers=headers)[0], expected)
        for body in (b'{', b'\xff', b'null', b'[]', b'{"startDate":"2026-10-24","startDate":"2026-10-25"}'):
            self.assertEqual(self.post(server, body=body)[0], 400)

    def test_invalid_dates_ids_and_extra_paths_never_invoke_scanner(self):
        launches = []
        server = self.start(runner=lambda selection: launches.append(selection))
        changes = [{'startDate': '2026-02-30'}, {'startDate': '2025-01-01'},
                   {'endDate': '2026-10-23'}, {'projects': ['unknown']}, {'chats': ['unknown']},
                   {'chats': '--config=other'}, {'projects': [None]}, {'startDate': None},
                   {'command': 'arbitrary'}, {'config': str(self.config)}, {'out': str(self.root)},
                   {'projects': ['x'] * 257}, {'chats': ['x' * 1025]}]
        for changeset in changes:
            with self.subTest(changes=changeset):
                status, body, _ = self.post(server, {**SELECTION, **changeset})
                self.assertEqual(status, 400)
                self.assertEqual(body, {'error': 'refresh_invalid_selection'})
        self.assertEqual(launches, [])

    def test_query_routes_reserialize_data_and_never_serve_private_state(self):
        server = self.start()
        query_id = '12345678-1234-4234-8234-123456789abc'
        directory = self.out / 'query-usage'
        folder = directory / query_id
        folder.mkdir(parents=True)
        value = {'schemaVersion': 1, 'queryId': query_id, 'status': 'pending',
                 'sentinel': '</script><script>must remain data</script>'}
        sidecar = b'window.USAGE_QUERY_METER=' + json.dumps(value).encode() + b';\n'
        (directory / 'latest.js').write_bytes(sidecar)
        (folder / 'result.js').write_bytes(sidecar)
        (folder / 'index.html').write_text('not served as executable content', encoding='utf-8')
        (self.out / 'query-usage.html').write_text('not served as executable content', encoding='utf-8')
        for route in ('query-usage/latest.js?updated=1234', 'query-usage/' + query_id + '/result.js?t=1234'):
            status, body, headers = self.request(server, route)
            self.assertEqual(status, 200)
            self.assertNotIn(b'</script>', body)
            self.assertTrue(body.startswith(b'window.USAGE_QUERY_METER='))
            self.assertIn('javascript', headers['Content-Type'])
        self.assertEqual(self.request(server, 'query-usage/' + query_id + '/state.json')[0], 404)
        for route in ('query-usage.html', 'query-usage/' + query_id + '/index.html'):
            status, body, _ = self.request(server, route)
            self.assertEqual(status, 200)
            self.assertNotIn(b'not served', body)
        (directory / 'latest.js').write_bytes(sidecar + b'alert("never executed");')
        self.assertEqual(self.request(server, 'query-usage/latest.js')[0], 503)


class JobLifecycleTests(ServerFixture):
    def test_reused_service_adopts_new_verified_launcher_report(self):
        server = self.start()
        self.assertEqual(self.request(server, 'index.html')[0], 200)
        newer = self.root / 'newer source'
        newer.mkdir()
        doubled = {key: {counter: value * 2 for counter, value in values.items()}
                   for key, values in scopes.PARTS.items()}
        with patch.object(scopes, 'PARTS', doubled):
            database, _ = scopes.fixture(newer)
        shutil.copyfile(database, self.database)
        self.bundle, self.proof, _ = dashboard_data.build_bundle(self.database, [self.names], now=NOW)
        self.publish()
        metadata = service._metadata_path(PACKAGE, self.config, self.out)
        service._atomic(metadata, {**server.state.identity(), 'baseUrl': server.state.base_url})
        with patch.object(service, '_verified_python', return_value=Path(sys.executable)), \
             patch.object(service.subprocess, 'Popen', side_effect=AssertionError('Live service must be reused')):
            reused = service.ensure_server(PACKAGE, self.config, self.out)
        self.assertTrue(reused['reused'])
        actual = dashboard_data.project_bundle(server.state.bundle, SELECTION['startDate'], SELECTION['endDate'],
            projects=SELECTION['projects'], chats=SELECTION['chats'], now=NOW)
        self.assertEqual(actual['totals']['totalTokens'], 554)
        self.assertEqual(self.request(server, 'index.html')[0], 200)
        self.assertEqual(server.state.bundle, self.bundle)

    def test_post_validates_new_catalog_and_mixed_publication_keeps_last_good_html(self):
        server = self.start()
        self.assertEqual(self.request(server, 'index.html')[0], 200)
        newer = self.root / 'new catalog'
        newer.mkdir()
        replacement = '70000000-0000-4000-8000-000000000007'
        with patch.object(scopes, 'ROOT', replacement):
            database, _ = scopes.fixture(newer)
        shutil.copyfile(database, self.database)
        self.bundle, self.proof, _ = dashboard_data.build_bundle(self.database, [self.names], now=NOW)
        new_selection = {**SELECTION, 'chats': sorted([replacement, OTHER])}
        self.publish(new_selection)
        status, body, _ = self.post(server, SELECTION)
        self.assertEqual((status, body), (400, {'error': 'refresh_invalid_selection'}))
        status, started, _ = self.post(server, new_selection)
        self.assertEqual(status, 202)
        self.assertEqual(self.terminal(server, started['jobId'])['status'], 'complete')
        # During an external multi-file publication, HTML stays on the previous
        # verified snapshot. A scan request fails closed until evidence matches.
        self.assertEqual(self.request(server, 'index.html')[0], 200)
        before = copy.deepcopy(server.state.bundle)
        (self.out / 'data.json').write_bytes(b'{}')
        self.assertEqual(self.request(server, 'index.html')[0], 200)
        self.assertEqual(server.state.bundle, before)
        self.assertEqual(self.post(server, new_selection)[:2],
                         (409, {'error': 'refresh_invalid_report'}))
        self.publish(new_selection)
        self.assertEqual(self.request(server, 'index.html')[0], 200)

    def test_concurrent_double_clicks_share_one_job_and_preserve_union_intersection(self):
        entered, release = threading.Event(), threading.Event()
        launches = []
        def runner(selection):
            launches.append(selection)
            entered.set()
            if not release.wait(5):
                raise AssertionError('Synthetic test did not release its worker')
        server = self.start(runner=runner)
        responses = []
        def request():
            responses.append(self.post(server, {**SELECTION, 'projects': [B, A, A], 'chats': [OTHER, ROOT, ROOT]}))
        threads = [threading.Thread(target=request) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertTrue(entered.wait(3))
        self.assertEqual([item[0] for item in responses], [202] * 8)
        job_ids = {item[1]['jobId'] for item in responses}
        self.assertEqual(len(job_ids), 1)
        self.assertEqual(launches, [SELECTION])
        status, body, _ = self.post(server, {**SELECTION, 'projects': []})
        self.assertEqual((status, body), (409, {'error': 'refresh_busy'}))
        release.set()
        job = self.terminal(server, next(iter(job_ids)))
        self.assertEqual(job['status'], 'complete')
        fragment = parse_qs(urlsplit(job['dashboardUrl']).fragment)
        self.assertEqual(fragment['project'], SELECTION['projects'])
        self.assertEqual(fragment['chat'], SELECTION['chats'])
        actual = dashboard_data.project_bundle(server.state.bundle, SELECTION['startDate'], SELECTION['endDate'],
                                              projects=SELECTION['projects'], chats=SELECTION['chats'], now=NOW)
        self.assertEqual(actual['totals']['totalTokens'], 277)

    def test_failed_worker_preserves_last_valid_report_and_static_errors(self):
        paths = [self.config, *(self.out / name for name in ('index.html', 'data.json', 'evidence.json'))]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        def runner(selection):
            (self.out / 'index.html').write_text('incomplete publication', encoding='utf-8')
            self.config.write_text('invalid config', encoding='utf-8')
            raise RuntimeError('PRIVATE_FAKE_SOURCE_PATH must not reach HTTP')
        server = self.start(runner=runner)
        status, started, _ = self.post(server)
        self.assertEqual(status, 202)
        job = self.terminal(server, started['jobId'])
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error'], 'refresh_failed')
        self.assertNotIn('PRIVATE_', json.dumps(job))
        for path, expected in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected, str(path))
        self.assertEqual(self.request(server, 'index.html')[0], 200)

    def test_invalid_success_cannot_replace_verified_dashboard(self):
        before = (self.out / 'data.json').read_bytes()
        def runner(selection):
            (self.out / 'data.json').write_bytes(b'{}')
        server = self.start(runner=runner)
        _, started, _ = self.post(server)
        job = self.terminal(server, started['jobId'])
        self.assertEqual((job['status'], job['error']), ('failed', 'refresh_invalid_report'))
        self.assertEqual((self.out / 'data.json').read_bytes(), before)

    def test_profile_drift_rejects_before_any_scan_and_is_not_overwritten(self):
        launches = []
        server = self.start(runner=lambda selection: launches.append(selection))
        saved = local_setup.load_settings(self.config)
        changed = {**saved, 'database': str(self.root / 'another source.sqlite')}
        local_setup.save_settings(self.config, changed)
        before = self.config.read_bytes()
        status, body, _ = self.post(server)
        self.assertEqual((status, body), (409, {'error': 'refresh_profile_changed'}))
        self.assertEqual(launches, [])
        self.assertEqual(self.config.read_bytes(), before)

    def test_idle_shutdown_is_renewed_by_authenticated_page_heartbeat(self):
        metadata = self.root / '.usage-refresh/test-state.json'
        server = self.start(idle_timeout=0.3, metadata_path=metadata)
        thread = self.servers[-1][1]
        for _ in range(5):
            time.sleep(0.1)
            self.assertEqual(self.request(server, 'api/ping')[0], 200)
        self.assertTrue(thread.is_alive())
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(metadata.exists())

    def test_running_job_prevents_idle_shutdown_and_job_history_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        def runner(selection):
            entered.set()
            release.wait(5)
        server = self.start(runner=runner, idle_timeout=0.2)
        _, started, _ = self.post(server)
        self.assertTrue(entered.wait(3))
        time.sleep(0.35)
        self.assertTrue(self.servers[-1][1].is_alive())
        release.set()
        self.assertEqual(self.terminal(server, started['jobId'])['status'], 'complete')
        for _ in range(service.MAX_JOBS + 2):
            _, started, _ = self.post(server)
            self.assertEqual(self.terminal(server, started['jobId'])['status'], 'complete')
        self.assertEqual(len(server.state.jobs), service.MAX_JOBS)


class StartupAndProcessTests(ServerFixture):
    def test_verified_runtime_adds_timezone_path_only_after_successful_verification(self):
        import prebuilt_runtime
        import windows_package
        package = self.root / 'verified public package'
        runtime = package / 'runtime'
        python = runtime / 'python/python.exe'
        python.parent.mkdir(parents=True)
        python.write_bytes(b'Synthetic verified interpreter; never executed')
        packages = str(runtime / 'python-packages')
        Path(packages).mkdir()
        original_path = list(sys.path)
        stages = []
        def verified_bundle(root):
            self.assertEqual(root, package)
            self.assertEqual(sys.path, original_path)
            stages.append('bundle')
        def verified_runtime(root, selected_runtime):
            self.assertEqual((root, selected_runtime), (package, runtime))
            self.assertEqual(stages, ['bundle'])
            self.assertEqual(sys.path, original_path)
            stages.append('runtime')
            return {'pythonPackages': packages}
        with patch.object(sys, 'path', list(original_path)), \
             patch.object(windows_package, 'verify_bundle', side_effect=verified_bundle), \
             patch.object(prebuilt_runtime, 'resolve_prebuilt', side_effect=verified_runtime):
            self.assertEqual(service._verified_python(package), python.resolve())
            self.assertEqual(stages, ['bundle', 'runtime'])
            self.assertEqual(sys.path[0], packages)
            self.assertEqual(sys.path[1:], original_path)
        self.assertEqual(sys.path, original_path)
        # Reusing an already checked data path must not grow the import list.
        with patch.object(sys, 'path', [packages, *original_path]), \
             patch.object(windows_package, 'verify_bundle'), \
             patch.object(prebuilt_runtime, 'resolve_prebuilt', return_value={'pythonPackages': packages}):
            service._verified_python(package)
            self.assertEqual(sys.path, [packages, *original_path])
        # Neither failed public-payload verification nor a failed runtime check
        # may expose an unverified data/import path to report validation.
        for failed_stage in ('bundle', 'runtime'):
            with self.subTest(failed_stage=failed_stage), \
                 patch.object(sys, 'path', list(original_path)), \
                 patch.object(windows_package, 'verify_bundle',
                              side_effect=ValueError('Synthetic damaged bundle') if failed_stage == 'bundle' else None), \
                 patch.object(prebuilt_runtime, 'resolve_prebuilt',
                              side_effect=ValueError('Synthetic damaged runtime')) as resolver:
                with self.assertRaises(ValueError):
                    service._verified_python(package)
                self.assertEqual(sys.path, original_path)
                if failed_stage == 'bundle':
                    resolver.assert_not_called()

    def test_handshake_never_follows_redirect_or_uses_environment_proxy(self):
        received = []
        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        target = HTTPServer(('127.0.0.1', 0), Target)
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', 'http://127.0.0.1:' + str(target.server_port) + '/must-not-connect')
                self.end_headers()
            def log_message(self, *args):
                pass
        redirect = HTTPServer(('127.0.0.1', 0), Redirect)
        threads = [threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
                   for server in (target, redirect)]
        for thread in threads:
            thread.start()
        try:
            expected = {'schemaVersion': 1, 'service': service.SERVICE, 'binding': 'binding',
                        'profile': 'profile', 'revision': 'revision'}
            metadata = {**expected, 'instance': 'instance', 'pid': os.getpid(),
                        'baseUrl': 'http://127.0.0.1:' + str(redirect.server_port) + '/' + 'x' * 43 + '/'}
            with patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
                self.assertIsNone(service._handshake(metadata, expected))
            self.assertEqual(received, [])
        finally:
            for server, thread in zip((target, redirect), threads):
                server.shutdown()
                thread.join(3)
                server.server_close()

    def test_active_identity_reuse_requires_exact_profile_package_and_instance(self):
        server = self.start()
        metadata = {**server.state.identity(), 'baseUrl': server.state.base_url}
        expected = {key: metadata[key] for key in ('schemaVersion', 'service', 'binding', 'profile', 'revision')}
        found = service._handshake(metadata, expected)
        self.assertEqual(found['dashboardUrl'], server.state.base_url + 'index.html')
        for key in ('binding', 'profile', 'revision', 'instance', 'pid'):
            with self.subTest(key=key):
                self.assertIsNone(service._handshake({**metadata, key: 'different'}, expected))
        for base in ('https://127.0.0.1:1234/' + 'x' * 43 + '/',
                     'http://example.invalid:1234/' + 'x' * 43 + '/',
                     'http://127.0.0.1:1234/path/', 'http://[invalid'):
            self.assertIsNone(service._handshake({**metadata, 'baseUrl': base}, expected))

    def test_ensure_server_reuses_only_handshaken_instance_without_process_launch(self):
        server = self.start()
        metadata_path = service._metadata_path(PACKAGE, self.config, self.out)
        service._atomic(metadata_path, {**server.state.identity(), 'baseUrl': server.state.base_url})
        with patch.object(service, '_verified_python', return_value=Path(sys.executable)), \
             patch.object(service.subprocess, 'Popen', side_effect=AssertionError('An active identity should be reused')):
            found = service.ensure_server(PACKAGE, self.config, self.out)
        self.assertTrue(found['reused'])
        self.assertEqual(found['pid'], os.getpid())

    def test_concurrent_startups_share_one_hidden_fixed_child(self):
        launched = []
        fake_processes = []
        def launch(command, **options):
            launched.append((command, options))
            server = self.start()
            service._atomic(service._metadata_path(PACKAGE, self.config, self.out),
                            {**server.state.identity(), 'baseUrl': server.state.base_url})
            class Process:
                pid = os.getpid()
                def poll(self):
                    return None
            result = Process()
            fake_processes.append(result)
            return result
        results, errors = [], []
        def ensure():
            try:
                results.append(service.ensure_server(PACKAGE, self.config, self.out))
            except BaseException as exc:
                errors.append(exc)
        with patch.object(service, '_verified_python', return_value=Path(sys.executable)), \
             patch.object(service.subprocess, 'Popen', side_effect=launch):
            threads = [threading.Thread(target=ensure) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(launched), 1)
        self.assertEqual(len({result['baseUrl'] for result in results}), 1)
        command, options = launched[0]
        self.assertEqual(command[:5], [str(Path(sys.executable)), '-B', '-X', 'utf8', '-c'])
        self.assertIn("p+'/refresh_server.py'", command[5])
        self.assertEqual(command[6], str(PACKAGE / 'scripts'))
        self.assertFalse(options['shell'])
        if os.name == 'nt':
            self.assertTrue(options['creationflags'] & subprocess.CREATE_NO_WINDOW)
        self.assertEqual(sum(not result['reused'] for result in results), 1)

    def test_worker_fixed_argv_secrets_stripped_and_timeout_is_static(self):
        saved = local_setup.load_settings(self.config)
        local_setup.save_settings(self.config, {**saved, 'schemaVersion': 2, 'offline': {
            'codexHome': str(self.root / 'synthetic home'), 'timezone': 'UTC',
            'engine': str(Path(sys.executable)), 'runtimeDirectory': str(self.root / 'runtime')}})
        captured = []
        class Process:
            returncode = 0
            pid = 1234567
            stdin = io.BytesIO()
            def wait(self, timeout=None):
                captured.append(('timeout', timeout))
                return self.returncode
        process = Process()
        def launch(command, **options):
            captured.append((command, options))
            return process
        with patch.object(service, '_verified_python', return_value=Path(sys.executable)), \
             patch.object(service.subprocess, 'Popen', side_effect=launch), \
             patch.object(service, '_process_tree', side_effect=lambda _: contextlib.nullcontext()), \
             patch.dict(os.environ, {'OPENAI_API_KEY': 'PRIVATE_SECRET', 'ANTHROPIC_API_KEY': 'PRIVATE_SECRET'}):
            service._run_trusted_refresh(PACKAGE, self.config, self.out, SELECTION)
        command, options = captured[0]
        self.assertIn('--no-serve', command)
        self.assertIn('sys.stdin.buffer.read(1)', command[5])
        self.assertEqual(options['stdin'], subprocess.PIPE)
        self.assertIn('--no-open', command)
        self.assertEqual(command[command.index('--codex-home') + 1], str(self.root / 'synthetic home'))
        self.assertIn('--project-id=' + A, command)
        self.assertIn('--chat-id=' + ROOT, command)
        self.assertEqual(command[command.index('--config') + 1], str(self.config))
        self.assertEqual(command[command.index('--out') + 1], str(self.out))
        self.assertNotIn('OPENAI_API_KEY', options['env'])
        self.assertNotIn('ANTHROPIC_API_KEY', options['env'])
        self.assertFalse(options['shell'])
        self.assertIn(('timeout', service.REFRESH_TIMEOUT), captured)
        process.returncode = 1
        process.stdin = io.BytesIO()
        with patch.object(service, '_verified_python', return_value=Path(sys.executable)), \
             patch.object(service.subprocess, 'Popen', return_value=process), \
             patch.object(service, '_process_tree', side_effect=lambda _: contextlib.nullcontext()):
            with self.assertRaisesRegex(service.RefreshError, '^refresh_failed$'):
                service._run_trusted_refresh(PACKAGE, self.config, self.out, SELECTION)

    def test_legacy_cache_only_profile_never_discovers_or_launches_scanner(self):
        before = self.config.read_bytes()
        with patch.object(service, '_verified_python', side_effect=AssertionError('No offline source selected')), \
             patch.object(service.subprocess, 'Popen', side_effect=AssertionError('Legacy cache must not scan')):
            with self.assertRaisesRegex(service.RefreshError, '^refresh_unavailable$'):
                service._run_trusted_refresh(PACKAGE, self.config, self.out, SELECTION)
        self.assertEqual(self.config.read_bytes(), before)

    def test_process_tree_timeout_terminates_synthetic_descendant(self):
        heartbeat = self.root / 'child-heartbeat'
        child = self.root / 'heartbeat.py'
        child.write_text('import pathlib,time,sys\np=pathlib.Path(sys.argv[1])\n'
                         'while True:\n p.write_text(str(time.time_ns()))\n time.sleep(.02)\n', encoding='utf-8')
        parent = self.root / 'spawn.py'
        parent.write_text("import sys\nif sys.stdin.buffer.read(1)!=b'R': sys.exit(1)\nimport subprocess,time\n"
                          'subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\ntime.sleep(60)\n', encoding='utf-8')
        options = {'stdin': subprocess.PIPE, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        else:
            options['start_new_session'] = True
        process = subprocess.Popen([sys.executable, '-B', str(parent), str(child), str(heartbeat)], **options)
        # Simulate a delayed AssignProcessToJobObject. The child bridge must
        # remain blocked and unable to spawn a descendant throughout this gap.
        time.sleep(0.25)
        self.assertFalse(heartbeat.exists())
        with self.assertRaisesRegex(service.RefreshError, 'refresh_timeout'):
            with service._process_tree(process):
                self.assertFalse(heartbeat.exists())
                process.stdin.write(b'R')
                process.stdin.flush()
                process.stdin.close()
                deadline = time.monotonic() + 5
                while not heartbeat.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(heartbeat.exists())
                raise service.RefreshError('refresh_timeout')
        time.sleep(0.1)
        before = heartbeat.read_bytes()
        time.sleep(0.1)
        self.assertEqual(heartbeat.read_bytes(), before)
        self.assertIsNotNone(process.poll())


@unittest.skipUnless(os.environ.get('CODEX_USAGE_TEST_ENGINE'), 'Requires the actual offline scanner; synthetic home only.')
class NativeRefreshServerTests(unittest.TestCase):
    def test_http_refresh_rescans_real_native_fixture_and_preserves_every_filter(self):
        import test_offline_refresh as native
        with tempfile.TemporaryDirectory(prefix='native refresh ü ') as temporary:
            root = Path(temporary)
            home, paths = native.raw_fixture(root)
            out, config = root / 'dashboard', root / 'settings.json'
            names = root / 'synthetic-sidebar.sqlite'
            with contextlib.closing(sqlite3.connect(names)) as database, database:
                database.execute('CREATE TABLE threads(id TEXT,name TEXT)')
                database.execute('CREATE TABLE projects(id TEXT,name TEXT)')
                database.execute('CREATE TABLE project_roots(project_id TEXT,path TEXT)')
                database.execute('INSERT INTO projects VALUES(?,?)', (native.A, 'Synthetic project A'))
                database.execute('INSERT INTO project_roots VALUES(?,?)', (native.A, native.A))
            project_state = root / 'synthetic-projects.json'
            project_state.write_text('{}', encoding='utf-8')
            base = ['--offline', '--config', str(config), '--out', str(out), '--no-open', '--no-serve',
                    '--from-date', '2026-10-24', '--to-date', '2026-10-26',
                    '--project-id', native.A, '--chat-id', native.ROOT]
            initial_error = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(initial_error):
                status = refresh_usage.main([*base, '--codex-home', str(home), '--timezone', 'UTC',
                    '--engine', os.environ['CODEX_USAGE_TEST_ENGINE'], '--names-database', str(names),
                    '--project-state', str(project_state)])
            self.assertIn(status, (0, 2), initial_error.getvalue())
            selected = {'startDate': '2026-10-24', 'endDate': '2026-10-26',
                        'projects': [native.A], 'chats': [native.ROOT]}
            # The only alternate runner here is a real subprocess invoking the
            # source entry with this exact synthetic saved setup and real binary.
            def runner(selection):
                command = [sys.executable, '-B', '-X', 'utf8', str(PACKAGE / 'scripts/refresh_usage.py'), *base]
                completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                           timeout=30, env=service._environment())
                if completed.returncode not in (0, 2):
                    raise service.RefreshError('refresh_failed')
            server = service.create_server(PACKAGE, config, out, runner=runner, idle_timeout=30)
            thread = threading.Thread(target=service.serve, args=(server,), daemon=True)
            thread.start()
            harness = ServerFixture('runTest')
            try:
                initial = dashboard_data.project_bundle(server.state.bundle, '2026-10-24', '2026-10-26',
                    projects=[native.A], chats=[native.ROOT], now=native.NOW)
                self.assertEqual(initial['totals']['totalTokens'], 162)
                with paths['child'].open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(native.event('2026-10-26', last=(3, 1, 2, 1, 5))) + '\n')
                status, started, _ = harness.post(server, selected)
                self.assertEqual(status, 202)
                terminal = harness.terminal(server, started['jobId'], timeout=30)
                self.assertEqual(terminal['status'], 'complete', terminal)
                actual = dashboard_data.project_bundle(server.state.bundle, '2026-10-24', '2026-10-26',
                    projects=[native.A], chats=[native.ROOT], now=native.NOW)
                self.assertEqual(actual['totals']['totalTokens'], 167)
                self.assertEqual(service.verified_existing_report(out)[1], selected)
                for name in ('index.html', 'data.json', 'evidence.json'):
                    self.assertNotIn(native.PRIVATE.encode(), (out / name).read_bytes())
                # A genuine native decoder failure keeps the complete snapshot.
                before = {path: path.read_bytes() for path in (config, out / 'data.json', out / 'index.html')}
                (home / 'sessions/broken.jsonl.zst').write_bytes(b'not zstd')
                _, started, _ = harness.post(server, selected)
                terminal = harness.terminal(server, started['jobId'], timeout=30)
                self.assertEqual((terminal['status'], terminal['error']), ('failed', 'refresh_failed'))
                for path, payload in before.items():
                    self.assertEqual(path.read_bytes(), payload)
            finally:
                server.shutdown()
                thread.join(5)
                server.server_close()


if __name__ == '__main__':
    unittest.main()
