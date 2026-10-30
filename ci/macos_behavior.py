"""Real macOS public ZIP/launcher integration; fixtures contain no user data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import zipfile

from common import digest, write_json

SOURCE = Path(os.environ.get('USAGE_MAC_SOURCE', Path(__file__).resolve().parents[1] / 'usage-monitor'))
sys.path[:0] = [str(SOURCE / 'scripts'), str(SOURCE / 'tests')]
from test_offline_refresh import (A, B, C, ROOT, FORK, NESTED, CHILD, NOW, TOTAL,
                                  DAY_TOTALS, OWNER_TOTALS, PRIVATE, raw_fixture, event)
import dashboard_data as ledger
import report_from_app as core

EXPORT_PRIVATE = 'PRIVATE_EXPORT_' + 'MAC_SENTINEL_192d432a'


class MacPublicPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = Path(os.environ['USAGE_MAC_WORK']).resolve()
        cls.shared.mkdir(parents=True, exist_ok=True)
        cls.archive = Path(os.environ['USAGE_MAC_ZIP']).resolve()
        cls.package = cls.shared / 'extracted package ü with spaces/Usage-Monitor'
        subprocess.run(['/usr/bin/ditto', '-x', '-k', str(cls.archive), str(cls.package.parent)], check=True)
        cls.launcher = cls.package / 'Start-Usage-Monitor.command'
        assert cls.launcher.stat().st_mode & 0o111, 'ZIP must preserve the public launcher executable bit'
        cls.python = cls.package / 'runtime/python/python/bin/python3'
        cls.engine = cls.package / 'runtime/bin/codex-usage-offline'

    def setUp(self):
        self.root = self.shared / self._testMethodName
        self.root.mkdir()
        self.home, self.sessions = raw_fixture(self.root)
        self.profile = self.root / 'empty home ü with spaces'
        self.profile.mkdir()
        self.config = self.profile / 'settings/settings.json'
        self.out = self.profile / 'dashboard ü with spaces'
        self.names = self.root / 'approved synthetic sidebar.sqlite'
        with sqlite3.connect(self.names) as db:
            db.executescript('CREATE TABLE threads(id TEXT,name TEXT); CREATE TABLE projects(id TEXT,name TEXT);'
                             'CREATE TABLE project_roots(project_id TEXT,path TEXT);')
            db.executemany('INSERT INTO projects VALUES(?,?)', [(A, 'Synthetic Alpha'), (B, 'Synthetic Beta'), (C, 'Synthetic Gamma')])
            db.executemany('INSERT INTO project_roots VALUES(?,?)', [(A, A), (B, B), (C, C)])
        self.projects = self.root / 'approved project metadata.json'
        self.projects.write_text('{}', encoding='utf-8')
        temporary = self.root / 'tmp'
        temporary.mkdir()
        self.environment = {'HOME': str(self.profile), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                            'TMPDIR': str(temporary), 'CODEX_HOME': str(self.home),
                            'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
        self.pids = set()
        self.invocations = []
        self.addCleanup(self.cleanup_services)

    def cleanup_services(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        write_json(self.root / 'public-invocations.json', self.invocations)

    def launch(self, *arguments, success=True):
        command = [str(self.launcher), *map(str, arguments)]
        result = subprocess.run(command, env=self.environment, cwd=self.root, capture_output=True,
                                text=True, encoding='utf-8', timeout=180)
        self.invocations.append({'arguments': list(map(str, arguments)), 'exitCode': result.returncode,
                                 'stdout': result.stdout, 'stderr': result.stderr})
        self.assertNotIn(EXPORT_PRIVATE, result.stdout + result.stderr)
        if not success:
            self.assertNotIn(result.returncode, (0, 2), result.stdout + result.stderr)
            return result
        self.assertIn(result.returncode, (0, 2), result.stdout + result.stderr)
        value = json.loads(result.stdout)
        if 'modelCalls' in value:
            self.assertEqual(value['modelCalls'], 0)
            self.assertFalse(value['networkUsed'])
        pid = value.get('localServer', {}).get('pid')
        if pid:
            self.pids.add(pid)
        return value

    def generate(self, *arguments, serve=False):
        return self.launch('--config', self.config, '--out', self.out, '--codex-home', self.home,
                           '--timezone', 'UTC', '--names-database', self.names, '--project-state', self.projects,
                           '--no-open', *([] if serve else ['--no-serve']), *arguments)

    def vector(self, row):
        return tuple(row[field] for field in core.FIELDS)

    def accounting(self):
        payload = json.loads((self.out / 'data.json').read_text(encoding='utf-8'))
        bundle = payload['browserBundle']
        report = ledger.project_bundle(bundle, '2026-10-24', '2026-10-26', now=NOW)
        self.assertEqual(self.vector(report['totals']), TOTAL)
        for dimension in ('daily', 'owners', 'chats', 'models', 'projects'):
            self.assertEqual(tuple(sum(row[field] for row in report[dimension]) for field in core.FIELDS), TOTAL, dimension)
        self.assertEqual({row['date']: self.vector(row) for row in report['daily']}, DAY_TOTALS)
        self.assertEqual({row['id']: self.vector(row) for row in report['owners']}, OWNER_TOTALS)
        self.assertEqual(self.vector(ledger.project_bundle(bundle, '2026-10-24', '2026-10-26', now=NOW, chat=ROOT)['totals']),
                         (161, 76, 51, 14, 212))
        nested = next(row for row in report['owners'] if row['id'] == NESTED)
        self.assertEqual((nested['parentId'], nested['chatId']), (CHILD, ROOT))
        proof = json.loads((self.out / 'evidence.json').read_text(encoding='utf-8'))
        self.assertTrue(proof['mappingOwnerIdsMatch'])
        self.assertTrue(proof['offlineScanner']['nativeCachePublishedAfterValidation'])
        self.assertEqual(proof['reconciledFields'], list(core.FIELDS))
        for name in ('index.html', 'data.json', 'evidence.json', 'usage-ledger.json', 'summary.txt'):
            self.assertNotIn(PRIVATE.encode(), (self.out / name).read_bytes(), name)
        saved = json.loads(self.config.read_text(encoding='utf-8'))
        self.assertNotIn(PRIVATE.encode(), Path(saved['database']).read_bytes())
        for forbidden in ('.cargo', '.rustup'):
            self.assertFalse((self.profile / forbidden).exists())
        return bundle

    def state(self):
        saved = json.loads(self.config.read_text(encoding='utf-8'))
        paths = [self.config, Path(saved['database']), *(self.out / name for name in
                 ('index.html', 'data.json', 'evidence.json', 'summary.txt', 'usage-ledger.json', '.usage-selection.json'))]
        return {str(path): (digest(path), path.stat().st_mtime_ns) for path in paths}

    def request(self, url, payload=None, *, authorized=True, origin=None):
        self.assertEqual(urlsplit(url).hostname, '127.0.0.1')
        headers = {}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            if authorized:
                headers['X-Usage-Monitor'] = 'refresh'
            parsed = urlsplit(url)
            headers['Origin'] = origin or parsed.scheme + '://' + parsed.netloc
        request = Request(url, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
        return build_opener(ProxyHandler({})).open(request, timeout=30)

    def test_help_generation_repeat_and_all_five_scoped_counters(self):
        help_result = subprocess.run([str(self.launcher), '--help'], env=self.environment, capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('--codex-home', help_result.stdout)
        for wrapper, arguments in (('setup.sh', ['--help']), ('refresh.sh', ['--offline', '--help'])):
            command = ['/bin/sh', str(self.package / wrapper), *arguments]
            result = subprocess.run(command, env=self.environment, cwd=self.root, capture_output=True,
                                    text=True, encoding='utf-8', timeout=180)
            self.invocations.append({'wrapper': wrapper, 'arguments': arguments, 'exitCode': result.returncode,
                                     'stdout': result.stdout, 'stderr': result.stderr})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('--codex-home', result.stdout)
        self.assertFalse(self.config.exists(), 'Wrapper help must not save a usage profile')
        self.assertFalse(self.out.exists(), 'Wrapper help must not generate or scan usage')
        for directory in ('.cargo', '.rustup', 'Library/Application Support/codex-local-usage/runtime'):
            self.assertFalse((self.profile / directory).exists(), 'Wrapper help provisioned a fallback runtime')
        injected = self.root / 'untrusted environment modules'
        injected.mkdir()
        marker = self.root / 'ENVIRONMENT_CODE_EXECUTED'
        bash_env = injected / 'bash-env'
        bash_env.write_text('touch ' + repr(str(marker)) + '\n', encoding='utf-8')
        (injected / 'sitecustomize.py').write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n', encoding='utf-8')
        (injected / 'Poison.pm').write_text('BEGIN { open(my $f, ">", ' + json.dumps(str(marker)) + '); close($f); }\n1;\n', encoding='utf-8')
        hostile = dict(self.environment, BASH_ENV=str(bash_env), PYTHONPATH=str(injected),
                       PERL5OPT='-MPoison', PERL5LIB=str(injected), DYLD_LIBRARY_PATH=str(injected))
        guarded = subprocess.run([str(self.launcher), '--help'], env=hostile, capture_output=True, text=True)
        self.assertEqual(guarded.returncode, 0, guarded.stdout + guarded.stderr)
        self.assertFalse(marker.exists(), 'Inherited shell/Python/Perl environment executed before payload checks')
        first = self.generate('--from-date', '2026-10-24', '--to-date', '2026-10-26', '--chat-id', ROOT, '--chat-id', FORK)
        self.accounting()
        repeated = self.generate('--from-date', '2026-10-24', '--to-date', '2026-10-26', '--chat-id', ROOT, '--chat-id', FORK)
        self.assertEqual(repeated['offlineScan']['filesParsed'], 0)
        self.assertEqual(repeated['offlineScan']['bytesRead'], 0)
        self.assertEqual(repeated['initialSelection'], first['initialSelection'])
        self.accounting()
        cases = [
            ('all', [], (220, 100, 62, 17, 282)),
            ('chat', [], (161, 76, 51, 14, 212)),
            ('project', ['--project-id', A], (147, 77, 36, 11, 183)),
            ('selection', ['--project-id', A, '--project-id', B, '--chat-id', ROOT, '--chat-id', FORK], (161, 76, 51, 14, 212)),
        ]
        self.environment['CODEX_THREAD_ID'] = ROOT
        for scope, flags, expected in cases:
            result = self.launch('--codex-report', '--config', self.config, '--scope', scope,
                                 '--from-date', '2026-10-24', '--to-date', '2026-10-26', *flags)
            self.assertEqual(self.vector(result['totals']), expected, scope)
        day = self.launch('--codex-report', '--config', self.config, '--scope', 'all',
                          '--from-date', '2026-10-25', '--to-date', '2026-10-25')
        self.assertEqual(self.vector(day['totals']), DAY_TOTALS['2026-10-25'])

    def test_tamper_rejection_and_failed_scan_preserve_last_good_data(self):
        self.generate()
        before = self.state()
        marker = self.root / 'UNVERIFIED_CODE_EXECUTED'
        malicious = ('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n').encode()
        for relative in ('scripts/standalone_app.py', 'runtime/python/python/bin/python3',
                         'runtime/bin/codex-usage-offline', 'native/src/main.rs'):
            path = self.package / relative
            original = path.read_bytes()
            try:
                path.write_bytes(malicious if relative.endswith('standalone_app.py') else original + b'\nCORRUPTED\n')
                self.launch('--config', self.config, '--no-open', '--no-serve', success=False)
                self.assertFalse(marker.exists())
                self.assertEqual(self.state(), before)
            finally:
                path.write_bytes(original)
        broken = self.home / 'sessions/broken.jsonl.zst'
        broken.write_bytes(b'not Zstandard')
        self.launch('--config', self.config, '--no-open', '--no-serve', success=False)
        self.assertEqual(self.state(), before)
        broken.unlink()
        self.accounting()

    def test_serve_only_auth_guard_live_refresh_and_both_browser_engines(self):
        first = self.generate('--from-date', '2026-10-24', '--to-date', '2026-10-26')
        self.accounting()
        before = self.state()
        broken = self.home / 'sessions/no-read-during-serve.jsonl.zst'
        broken.write_bytes(b'not Zstandard')
        served = self.launch('--config', self.config, '--serve-only', '--no-open')
        self.assertFalse(served['scanPerformed'])
        self.assertEqual(before, self.state())
        base = served['localServer']['baseUrl']
        with self.request(served['dashboardUrl']) as response:
            self.assertIn(b'Usage Monitor', response.read())
        for url, kwargs in ((base + 'api/refresh', {'authorized': False}),
                            (base + 'api/refresh', {'origin': 'https://example.invalid'})):
            with self.assertRaises(HTTPError) as rejected:
                self.request(url, first['initialSelection'], **kwargs)
            self.assertIn(rejected.exception.code, (400, 403))
        for url in (base + '../settings.json', base.replace(urlsplit(base).path, '/index.html')):
            with self.assertRaises(HTTPError) as rejected:
                self.request(url)
            self.assertIn(rejected.exception.code, (400, 403, 404))
        broken.unlink()
        query_states = self.prepare_query_browser_states()
        fixture = self.root / 'browser-input.json'
        write_json(fixture, {'dashboardUrl': served['dashboardUrl'], 'file': str(self.out / 'index.html'),
                            'root': ROOT, 'fork': FORK, 'projects': [A, B], 'outputDirectory': str(self.root / 'browser'),
                            'queryStates': query_states, 'queryLatest': str(self.out / 'query-usage/latest.js'),
                            'queryView': str(self.out / 'query-usage.html')})
        subprocess.run([os.environ['USAGE_BROWSER_NODE'], str(Path(__file__).with_name('browser_smoke.mjs')), str(fixture)],
                       check=True, timeout=300)
        selection = {'startDate': '2026-10-24', 'endDate': '2026-10-26', 'projects': [], 'chats': [ROOT]}
        with self.sessions['root'].open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event('2026-10-25', last=(5, 2, 1, 0, 6), clock='13:00:00')) + '\n')
        with self.request(base + 'api/refresh', selection) as response:
            job = json.load(response)
        deadline = time.monotonic() + 120
        while job['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(.1)
            with self.request(job['statusUrl']) as response:
                job = json.load(response)
        self.assertEqual(job['status'], 'complete', job)
        bundle = json.loads((self.out / 'data.json').read_text(encoding='utf-8'))['browserBundle']
        self.assertEqual(self.vector(ledger.project_bundle(bundle, '2026-10-24', '2026-10-26', now=NOW)['totals']),
                         (225, 102, 63, 17, 288))

    def prepare_query_browser_states(self):
        """Produce real provisional/final/partial-large sidecars through the public entry."""
        home = self.root / 'browser-query-source'
        sessions = home / 'sessions'
        sessions.mkdir(parents=True)
        saved = self.root / 'query-browser-states'
        saved.mkdir()
        line = lambda row: json.dumps({'timestamp': '2026-10-28T10:00:00Z', **row}) + '\n'
        usage = lambda count: {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {
            'input_tokens': count, 'cached_input_tokens': 0, 'output_tokens': 0, 'reasoning_output_tokens': 0, 'total_tokens': count}}}}
        path = sessions / ('rollout-2026-10-28-' + ROOT + '.jsonl')
        path.write_text(line({'type': 'session_meta', 'payload': {'id': ROOT}}) +
                        line({'type': 'turn_context', 'payload': {'turn_id': 'browser-turn', 'model': 'gpt-5'}}) +
                        line(usage(20)), encoding='utf-8')
        begun = self.launch('--query-meter', 'begin', '--thread-id', ROOT, '--turn-id', 'browser-turn',
                            '--codex-home', home, '--out', self.out)
        provisional = saved / 'provisional.js'
        shutil.copyfile(Path(begun['resultPath']).with_suffix('.js'), provisional)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(line(usage(30)) + line({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'browser-turn'}}))
        completed = self.launch('--query-meter', 'sample', '--query-id', begun['queryId'], '--out', self.out)
        self.assertTrue(completed['final'])
        final = saved / 'complete.js'
        shutil.copyfile(Path(completed['resultPath']).with_suffix('.js'), final)
        large_home = self.root / 'browser-large-query-source'
        (large_home / 'sessions').mkdir(parents=True)
        paths = {}
        for identity, parent, amount, turn in ((ROOT, None, 5000000000000000, 'root-turn'),
                                               (CHILD, ROOT, 5000000000000001, 'child-turn')):
            meta = {'id': identity}
            if parent:
                meta['source'] = {'subagent': {'thread_spawn': {'parent_thread_id': parent, 'agent_path': '/root/child'}}}
            rows = [line({'type': 'session_meta', 'payload': meta}),
                    line({'type': 'turn_context', 'payload': {'turn_id': turn, 'model': 'gpt-5'}})]
            if not parent:
                rows.append(line({'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'collaboration.spawn_agent',
                                  'call_id': 'spawn-child', 'arguments': EXPORT_PRIVATE}}))
            rows.append(line(usage(amount)))
            target = large_home / 'sessions' / ('rollout-2026-10-28-' + identity + '.jsonl')
            target.write_text(''.join(rows), encoding='utf-8')
            paths[identity] = target
        large = self.launch('--query-meter', 'begin', '--thread-id', ROOT, '--turn-id', 'root-turn',
                            '--codex-home', large_home, '--out', self.out)
        self.launch('--query-meter', 'register', '--thread-id', CHILD, '--turn-id', 'child-turn',
                    '--parent-session-id', ROOT, '--parent-turn-id', 'root-turn', '--dispatch-call-id', 'spawn-child',
                    '--query-id', large['queryId'], '--out', self.out)
        for identity, turn in ((ROOT, 'root-turn'), (CHILD, 'child-turn')):
            with paths[identity].open('a', encoding='utf-8') as stream:
                stream.write(line({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': turn}}))
        partial = self.launch('--query-meter', 'sample', '--query-id', large['queryId'], '--out', self.out)
        self.assertEqual(partial['status'], 'partial')
        self.assertFalse(partial['final'])
        self.assertEqual(partial['subtotalText']['totalTokens'], '10000000000000001')
        large_sidecar = saved / 'partial-large.js'
        shutil.copyfile(Path(partial['resultPath']).with_suffix('.js'), large_sidecar)
        # Pending is a documented UI contract state; the current native producer
        # returns provisional immediately, so exercise this state as an explicit
        # UI fixture, distinct from the three actual production result sidecars.
        pending = saved / 'pending-ui-contract.js'
        pending.write_text('window.USAGE_QUERY_METER=' + json.dumps({'schemaVersion': 1, 'status': 'pending',
                           'queryId': begun['queryId'], 'coverage': 'turn-start', 'registeredTurns': 1}) + ';\n', encoding='utf-8')
        return [{'file': str(pending), 'status': 'pending', 'value': 'Measuring…', 'origin': 'UI contract fixture'},
                {'file': str(provisional), 'status': 'provisional', 'value': '20 tokens so far', 'origin': 'public packaged query entry'},
                {'file': str(final), 'status': 'complete', 'value': '50 tokens', 'origin': 'public packaged query entry'},
                {'file': str(large_sidecar), 'status': 'unavailable', 'value': 'Not separately measurable',
                 'note': '10,000,000,000,000,001', 'origin': 'public packaged query entry'}]

    def test_real_snapshot_hidden_query_watcher_and_numeric_privacy(self):
        identity = '01977e3d-d9f6-72b7-93cf-f3f2f83c382c'
        path = self.home / 'sessions' / ('rollout-2026-10-28-' + identity + '.jsonl')
        rows = [
            {'type': 'session_meta', 'payload': {'id': identity}},
            {'type': 'turn_context', 'payload': {'turn_id': 'query-turn', 'model': 'gpt-5'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'query-turn'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {
                'input_tokens': 20, 'cached_input_tokens': 5, 'output_tokens': 4, 'reasoning_output_tokens': 2, 'total_tokens': 24}}}},
        ]
        line = lambda row: json.dumps({'timestamp': '2026-10-28T10:00:00Z', **row}) + '\n'
        path.write_text(''.join(map(line, rows)), encoding='utf-8')
        snapshot = subprocess.run([str(self.engine), '--query-snapshot', str(path), '--turn-id', 'query-turn'],
                                  env=self.environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        self.assertNotIn(PRIVATE, snapshot.stdout)
        query_out = self.root / 'separate query dashboard ü'
        query_out.mkdir()
        cache = query_out / 'usage-ledger.json'
        cache.write_bytes(b'UNCHANGED_SHARED_CACHE')
        started = self.launch('--query-meter', 'begin', '--watch', '--thread-id', identity, '--turn-id', 'query-turn',
                              '--codex-home', self.home, '--out', query_out, '--timeout', '30', '--interval', '.1')
        self.assertIn(started['status'], ('pending', 'provisional'))
        with path.open('a', encoding='utf-8') as stream:
            stream.write(line({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                          'content': [{'type': 'output_text', 'text': EXPORT_PRIVATE}]}}))
            stream.write(line({'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {
                'input_tokens': 30, 'cached_input_tokens': 7, 'output_tokens': 6, 'reasoning_output_tokens': 3, 'total_tokens': 36}}}}))
            stream.write(line({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'query-turn'}}))
        latest = query_out / 'query-usage/latest.json'
        deadline = time.monotonic() + 30
        result = {}
        while time.monotonic() < deadline:
            if latest.is_file():
                result = json.loads(latest.read_text(encoding='utf-8'))
                if result.get('final'):
                    break
            time.sleep(.1)
        self.assertEqual(result.get('status'), 'complete', result)
        self.assertTrue(result['final'])
        self.assertEqual(tuple(result['counters'][field] for field in core.FIELDS), (50, 12, 10, 5, 60))
        self.assertEqual(cache.read_bytes(), b'UNCHANGED_SHARED_CACHE')
        self.assertFalse((query_out / 'index.html').exists())
        for path in query_out.rglob('*'):
            if path.is_file():
                self.assertNotIn(EXPORT_PRIVATE.encode(), path.read_bytes(), str(path))

    def test_offline_audit_and_export_exclude_used_profile_and_private_files(self):
        audit_file = self.root / 'offline-audit.json'
        script = r'''
import json,pathlib,sys
package,home,config,out,names,projects,audit_file=map(pathlib.Path,sys.argv[1:])
engine=(package/'runtime/bin/codex-usage-offline').resolve()
events={'network':[],'executables':[]}
def audit(event,args):
 if event.startswith('socket.') or event=='urllib.Request':
  events['network'].append(event)
  raise RuntimeError('Network forbidden during offline generation')
 if event=='subprocess.Popen':
  executable=pathlib.Path(args[0]).resolve()
  events['executables'].append(str(executable))
  if executable!=engine: raise RuntimeError('Only the shipped scanner may execute')
sys.addaudithook(audit)
sys.path.insert(0,str(package/'scripts'))
try:
 import standalone_app
 result=standalone_app.main(['--codex-home',str(home),'--config',str(config),'--out',str(out),
  '--names-database',str(names),'--project-state',str(projects),'--timezone','UTC','--no-open','--no-serve'])
finally: audit_file.write_text(json.dumps(events),encoding='utf-8')
raise SystemExit(result)
'''
        result = subprocess.run([str(self.python), '-I', '-S', '-B', '-X', 'utf8', '-c', script,
                                 *map(str, (self.package, self.home, self.config, self.out, self.names, self.projects, audit_file))],
                                env=self.environment, capture_output=True, text=True, timeout=180)
        self.assertIn(result.returncode, (0, 2), result.stdout + result.stderr)
        audited = json.loads(audit_file.read_text(encoding='utf-8'))
        self.assertEqual(audited['network'], [])
        self.assertEqual(audited['executables'], [str(self.engine)])
        self.accounting()
        private = self.package / 'data/private-marker.txt'
        private.parent.mkdir(exist_ok=True)
        private.write_text(EXPORT_PRIVATE, encoding='utf-8')
        exported = self.root / 'share without history ü.zip'
        try:
            self.launch('--export', exported)
            with zipfile.ZipFile(self.archive) as original, zipfile.ZipFile(exported) as fresh:
                self.assertEqual(set(fresh.namelist()), set(original.namelist()))
                for name in fresh.namelist():
                    payload = fresh.read(name)
                    self.assertEqual(payload, original.read(name), name)
                    self.assertNotIn(EXPORT_PRIVATE.encode(), payload)
                    self.assertFalse(name.startswith(('Usage-Monitor/data/', 'Usage-Monitor/dashboard/')))
        finally:
            private.unlink()


if __name__ == '__main__':
    # Reuse the evidence runner's per-case persistence without changing public discovery.
    from run_python_tests import flatten
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(MacPublicPackageTests)
    identities = [test.id() for test in flatten(suite)]
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    write_json(Path(os.environ['USAGE_MAC_WORK']) / 'macos-public-tests.json', {
        'discovered': len(identities), 'executed': result.testsRun, 'discoveredIds': identities,
        'skipped': [{'id': test.id(), 'reason': reason} for test, reason in result.skipped],
        'failures': [{'id': test.id(), 'traceback': trace} for test, trace in result.failures + result.errors],
        'status': 'passed' if result.wasSuccessful() and not result.skipped else 'failed'})
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
