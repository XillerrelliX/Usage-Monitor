"""Synthetic-only controller and native adapter contract tests."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import query_meter as q

ROOT = '11111111-1111-4111-8111-111111111111'
CHILD = '22222222-2222-4222-8222-222222222222'
GRANDCHILD = '33333333-3333-4333-8333-333333333333'
STAMP = '2026-10-28T10:00:00Z'


def counts(total):
    return dict(inputTokens=total * 7, cachedInputTokens=total * 2,
                outputTokens=total * 3, reasoningOutputTokens=total,
                totalTokens=total * 10)


def snapshot(session=ROOT, turn='turn-1', amount=5, parent=None, status='active', calls=None):
    return dict(schemaVersion=1, sessionId=session, parentSessionId=parent,
                agentPath='/root' if parent is None else '/root/child',
                turnId=turn, turnStatus=status, startedAt=STAMP,
                completedAt=STAMP if status != 'active' else None,
                beforeTurn=counts(10), throughTurn=counts(10 + amount),
                readBytes=1, completeLines=1, trailingPartial=False,
                delegationCalls=calls or [])


def test_windows_api():
    """Test-only SDK access; application code has no Windows security API."""
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return advapi, kernel


def test_sid_text(pointer, advapi, kernel):
    import ctypes
    from ctypes import wintypes
    text = wintypes.LPWSTR()
    try:
        if not advapi.ConvertSidToStringSidW(pointer, ctypes.byref(text)):
            raise OSError('Cannot inspect isolated test SID.')
        return text.value
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def test_execution_sid():
    import ctypes
    from ctypes import wintypes
    advapi, kernel = test_windows_api()
    token, length = wintypes.HANDLE(), wintypes.DWORD()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise OSError('Cannot inspect isolated test token.')
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, buffer, length.value, ctypes.byref(length)):
            raise OSError('Cannot inspect isolated test token.')
        return test_sid_text(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0], advapi, kernel)
    finally:
        if token:
            kernel.CloseHandle(token)


def windows_acl(path):
    """Read-only ACL inspection of an isolated synthetic test path."""
    import ctypes
    from ctypes import wintypes
    advapi, kernel = test_windows_api()
    descriptor, owner, dacl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    text = wintypes.LPWSTR()
    try:
        if advapi.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None,
                                       ctypes.byref(dacl), None, ctypes.byref(descriptor)):
            raise OSError('Cannot read isolated test ACL.')
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 5, ctypes.byref(text), None):
            raise OSError('Cannot encode isolated test ACL.')
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise OSError('Cannot inspect isolated test ACL control.')
        header = ctypes.string_at(dacl, 8)
        entries = []
        for index in range(int.from_bytes(header[4:6], 'little')):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise OSError('Cannot inspect isolated test ACE.')
            head = ctypes.string_at(ace, 8)
            entries.append({'type': head[0], 'flags': head[1],
                            'mask': int.from_bytes(head[4:8], 'little'),
                            'sid': test_sid_text(ace.value + 8, advapi, kernel)})
        return {'sddl': text.value, 'protected': bool(control.value & 0x1000), 'entries': entries}
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        if descriptor:
            kernel.LocalFree(descriptor)


def set_isolated_test_acl(path, isolated_root, principals):
    """Prepare a synthetic inherited/legacy fixture, never a production path."""
    import ctypes
    path, isolated_root = Path(path).resolve(), Path(isolated_root).resolve()
    if not path.is_relative_to(isolated_root) or not isolated_root.name.startswith('usage-query-synthetic-'):
        raise ValueError('ACL fixture leaves its isolated test root.')
    advapi, kernel = test_windows_api()
    descriptor = ctypes.c_void_p()
    inherit = 'OICI' if path.is_dir() else ''
    sddl = 'D:P' + ''.join('(A;' + inherit + ';FA;;;' + sid + ')' for sid in dict.fromkeys(principals))
    try:
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise OSError('Cannot prepare isolated ACL fixture.')
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise OSError('Cannot prepare isolated ACL fixture.')
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)


def effective_grants(acl):
    return {(entry['type'], entry['sid'], entry['mask']) for entry in acl['entries']}


def start_isolated_lock_holder(directory, root, real_sleep=time.sleep):
    """A real process holds the ordinary byte lock until a test-only release file."""
    ready, release = root / 'holder-ready', root / 'holder-release'
    script = '''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import query_meter as q
deadline=time.monotonic()+8
while True:
    try:
        with q.locked(Path(sys.argv[2])):
            Path(sys.argv[3]).write_bytes(b"ready")
            while not Path(sys.argv[4]).exists():
                if time.monotonic()>=deadline: raise SystemExit(3)
                time.sleep(.02)
        break
    except q.MeterError as exc:
        if str(exc)!="query_meter_busy" or time.monotonic()>=deadline: raise
        time.sleep(.02)
'''
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', '-c', script,
                                str(SCRIPTS), str(directory), str(ready), str(release)], **options)
    deadline = time.monotonic() + 5
    while not ready.exists():
        if process.poll() is not None or time.monotonic() >= deadline:
            process.terminate() if process.poll() is None else None
            process.communicate(timeout=5)
            raise AssertionError('Isolated lock holder did not become ready.')
        real_sleep(.02)
    return process, release


class InheritancePolicyTests(unittest.TestCase):
    def test_application_contains_no_windows_security_or_identity_runtime(self):
        source = Path(q.__file__).read_text(encoding='utf-8')
        for forbidden in ('SetFileSecurity', 'SetNamedSecurityInfo', 'Set-Acl', 'WinDLL',
                          'GetNamedSecurityInfo', 'owner_sid', 'creator_sid',
                          'private_sddl', 'privatize_parent'):
            self.assertNotIn(forbidden, source)

    def test_atomic_privatizes_only_new_temporary_file(self):
        with tempfile.TemporaryDirectory(prefix='usage-query-atomic-isolated-') as temporary:
            shared = Path(temporary)
            calls = []
            real_private = q.private
            def observe(path):
                calls.append(Path(path))
                return real_private(path)
            with patch.object(q, 'private', side_effect=observe):
                q.atomic(shared / 'query-usage.html', b'SYNTHETIC_PUBLIC_VIEW')
            self.assertNotIn(shared, calls)
            self.assertEqual(1, len(calls))

    def test_windows_private_is_noop_even_for_existing_objects(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(prefix='usage-query-private-isolated-') as temporary:
            target = Path(temporary) / 'numeric-result.json'
            target.write_bytes(b'{}')
            with patch.object(q, 'os', SimpleNamespace(name='nt')), patch.object(Path, 'chmod', side_effect=AssertionError('Windows permissions changed')):
                q.private(target)
                q.private(target.parent)
            self.assertEqual(b'{}', target.read_bytes())

    def test_posix_private_branch_uses_owner_only_modes_for_new_objects(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(prefix='usage-query-posix-branch-') as temporary:
            directory = Path(temporary) / 'new-owned'
            directory.mkdir()
            target = directory / 'new-result.json'
            target.write_bytes(b'{}')
            with patch.object(q, 'os', SimpleNamespace(name='posix')), patch.object(Path, 'chmod') as mode:
                q.private(directory)
                q.private(target)
            self.assertEqual([unittest.mock.call(0o700), unittest.mock.call(0o600)], mode.call_args_list)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='usage-query-synthetic-')
        self.root = Path(self.temporary.name)
        self.home, self.out = self.root / 'source', self.root / 'dashboard'
        (self.home / 'sessions').mkdir(parents=True)
        self.engine = self.root / 'fake-engine.exe'
        self.engine.write_bytes(b'synthetic engine placeholder')
        self.sources = {}
        self.values = {}
        self.create_source(ROOT)
        self.values[ROOT] = snapshot()
        self.calls = []
        self.process_patch = patch.object(q.subprocess, 'run', side_effect=self.adapter)
        self.process_patch.start()
        self.meter = q.Meter(self.out, self.engine)

    def tearDown(self):
        self.process_patch.stop()
        self.temporary.cleanup()

    def create_source(self, session):
        path = self.home / 'sessions' / ('rollout-2026-10-28T10-00-00-' + session + '.jsonl')
        path.write_bytes(b'synthetic bytes kept outside model parsing\n')
        self.sources[session] = path
        return path

    def adapter(self, command, **options):
        self.calls.append((command, options))
        path = Path(command[command.index('--query-snapshot') + 1])
        session = next(key for key, value in self.sources.items() if value == path)
        result = copy.deepcopy(self.values[session])
        result['readBytes'] = int(command[command.index('--byte-limit') + 1])
        return subprocess.CompletedProcess(command, 0, json.dumps(result).encode(), b'')

    def begin(self, **options):
        return self.meter.begin(self.home, ROOT, 'turn-1', **options)

    def finish(self, session=ROOT):
        self.values[session]['turnStatus'] = 'completed'
        self.values[session]['completedAt'] = STAMP
        with self.sources[session].open('ab') as stream:
            stream.write(b'synthetic completion\n')

    def test_before_turn_baseline_reconstructs_entire_selected_turn(self):
        result = self.begin()
        self.assertEqual('turn-start', result['coverage'])
        self.assertEqual(counts(5), result['subtotal'])
        self.assertFalse(result['final'])
        self.assertIsNone(result['counters'])

    def test_explicit_checkpoint_counts_only_following_interval(self):
        self.begin(checkpoint_only=True)
        self.values[ROOT]['throughTurn'] = counts(18)
        self.finish()
        result = self.meter.sample()
        self.assertEqual('checkpoint-only', result['coverage'])
        self.assertEqual(counts(3), result['counters'])
        self.assertTrue(result['final'])

    def test_completed_exact_turn_is_final_and_keeps_all_five_counters(self):
        self.begin()
        self.finish()
        result = self.meter.sample()
        self.assertEqual('complete', result['status'])
        self.assertEqual(counts(5), result['counters'])
        self.assertEqual('50', result['counterText']['totalTokens'])
        self.assertTrue(result['terminal'])

    def test_status_reads_no_source_and_never_launches_process(self):
        self.begin()
        self.sources[ROOT].unlink()
        previous = len(self.calls)
        self.assertEqual('provisional', self.meter.status()['status'])
        self.assertEqual(previous, len(self.calls))

    def test_duplicate_active_begin_preserves_first_state(self):
        first = self.begin()
        before = self.meter.state_path(first['queryId']).read_bytes()
        with self.assertRaisesRegex(q.MeterError, 'already_active'):
            self.begin()
        self.assertEqual(before, self.meter.state_path(first['queryId']).read_bytes())

    def test_historical_modification_fails_closed_and_cannot_recover(self):
        self.begin()
        original = self.sources[ROOT].read_bytes()
        self.sources[ROOT].write_bytes(original.replace(b'synthetic', b'SYNTHETIC'))
        result = self.meter.sample()
        self.assertEqual('unavailable', result['status'])
        self.assertIn('query_historical_update', result['errors'])
        self.assertIsNone(result['subtotal'])
        self.sources[ROOT].write_bytes(original)
        self.assertIsNone(self.meter.sample()['subtotal'])

    def test_truncation_is_unavailable(self):
        self.begin()
        self.sources[ROOT].write_bytes(b'x')
        self.assertIn('query_source_truncated', self.meter.sample()['errors'])

    def test_append_only_proofs_and_fixed_limits(self):
        first = self.begin()
        evidence = self.meter.state_path(first['queryId']).parent / 'evidence'
        original = {p.name: p.read_bytes() for p in evidence.glob('*.json')}
        with self.sources[ROOT].open('ab') as stream:
            stream.write(b'new synthetic bytes\n')
        self.meter.sample()
        self.assertEqual(2, len(list(evidence.glob('*.json'))))
        for name, data in original.items():
            self.assertEqual(data, (evidence / name).read_bytes())
        command, options = self.calls[-1]
        self.assertEqual(str(self.sources[ROOT].stat().st_size), command[command.index('--byte-limit') + 1])
        self.assertNotIn('OPENAI_API_KEY', options['env'])

    def test_concurrent_change_during_native_read_rejected(self):
        def changing(command, **options):
            result = self.adapter(command, **options)
            raw = self.sources[ROOT].read_bytes()
            self.sources[ROOT].write_bytes(b'X' + raw[1:])
            return result
        with patch.object(q.subprocess, 'run', side_effect=changing):
            with self.assertRaisesRegex(q.MeterError, 'concurrent_source_update'):
                self.begin()

    def test_concurrent_append_uses_fixed_prefix_and_is_allowed(self):
        def appending(command, **options):
            result = self.adapter(command, **options)
            with self.sources[ROOT].open('ab') as stream:
                stream.write(b'append during read\n')
            return result
        with patch.object(q.subprocess, 'run', side_effect=appending):
            self.assertEqual(counts(5), self.begin()['subtotal'])

    def test_missing_agent_attribution_cannot_return_complete_sum(self):
        self.values[ROOT]['delegationCalls'] = [{'id': 'call-1', 'kind': 'spawn_agent'}]
        self.finish()
        result = self.begin()
        self.assertEqual('partial', result['status'])
        self.assertEqual(1, result['unaccountedDispatches'])
        self.assertFalse(result['final'])
        self.assertIsNone(result['counters'])

    def register_child(self, child=CHILD, parent=ROOT, parent_turn='turn-1', dispatch='call-1'):
        self.create_source(child)
        self.values[child] = snapshot(child, 'child-turn', 3, parent)
        return self.meter.register(child, 'child-turn', parent, parent_turn, dispatch)

    def test_registered_child_is_claimed_subtotal_even_after_all_completions(self):
        self.values[ROOT]['delegationCalls'] = [{'id': 'call-1', 'kind': 'spawn_agent'}]
        self.begin()
        result = self.register_child()
        self.assertEqual(counts(8), result['subtotal'])
        self.assertEqual(1, result['unaccountedDispatches'])
        self.assertEqual(1, result['unverifiedRegistrations'])
        self.assertFalse(result['ownershipVerified'])
        self.finish()
        self.assertFalse(self.meter.sample()['final'])
        self.finish(CHILD)
        result = self.meter.sample()
        self.assertFalse(result['final'])
        self.assertEqual('partial', result['status'])
        self.assertTrue(result['terminal'])
        self.assertIsNone(result['counters'])
        self.assertEqual(counts(8), result['subtotal'])
        self.assertEqual(2, result['completedTurns'])

    def test_duplicate_turn_and_dispatch_rejected(self):
        self.values[ROOT]['delegationCalls'] = [{'id': 'call-1', 'kind': 'spawn_agent'}]
        self.begin()
        self.register_child()
        with self.assertRaisesRegex(q.MeterError, 'duplicate_turn'):
            self.meter.register(CHILD, 'child-turn', ROOT, 'turn-1', 'call-1')
        self.create_source(GRANDCHILD)
        self.values[GRANDCHILD] = snapshot(GRANDCHILD, 'different-turn', 3, ROOT)
        with self.assertRaisesRegex(q.MeterError, 'duplicate_dispatch'):
            self.meter.register(GRANDCHILD, 'different-turn', ROOT, 'turn-1', 'call-1')

    def test_unobserved_dispatch_and_fork_cannot_be_registered(self):
        self.begin()
        with self.assertRaisesRegex(q.MeterError, 'dispatch_not_observed'):
            self.register_child()
        self.values[ROOT]['delegationCalls'] = [{'id': 'call-1', 'kind': 'spawn_agent'}]
        self.values[CHILD]['parentSessionId'] = None  # A fork is not descendant lineage.
        with self.assertRaisesRegex(q.MeterError, 'lineage_mismatch'):
            self.meter.register(CHILD, 'child-turn', ROOT, 'turn-1', 'call-1')

    def test_registered_parent_chain_required_for_grandchild(self):
        self.values[ROOT]['delegationCalls'] = [{'id': 'call-1', 'kind': 'spawn_agent'}]
        self.begin()
        self.register_child()
        self.values[CHILD]['delegationCalls'] = [{'id': 'nested', 'kind': 'spawn_agent'}]
        result = self.register_child(GRANDCHILD, CHILD, 'child-turn', 'nested')
        self.assertEqual(counts(11), result['subtotal'])
        self.assertEqual(3, result['registeredTurns'])

    def test_failure_never_masquerades_as_completion(self):
        self.begin()
        self.values[ROOT]['turnStatus'] = 'failed'
        self.values[ROOT]['completedAt'] = STAMP
        result = self.meter.sample()
        self.assertEqual('partial', result['status'])
        self.assertIn('query_turn_not_completed', result['errors'])
        self.assertFalse(result['final'])

    def test_counter_regression_and_baseline_change_rejected(self):
        self.begin()
        self.values[ROOT]['throughTurn'] = counts(14)
        self.assertIn('query_counter_regression', self.meter.sample()['errors'])

    def test_native_arbitrary_stderr_not_copied_to_error(self):
        with patch.object(q.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', b'PRIVATE_TEXT_8372')):
            with self.assertRaisesRegex(q.MeterError, '^query_engine_failed$'):
                self.begin()

    def test_native_static_error_code_preserved(self):
        error = json.dumps({'schemaVersion': 1, 'errorCode': 'query_missing_turn_id'}).encode()
        with patch.object(q.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', error)):
            with self.assertRaisesRegex(q.MeterError, '^query_missing_turn_id$'):
                self.begin()

    def test_incomplete_native_schema_and_wrong_fixed_limit_rejected(self):
        self.values[ROOT].pop('startedAt')
        with self.assertRaisesRegex(q.MeterError, 'invalid_snapshot'):
            self.begin()

    def test_native_ids_counters_status_and_timestamps_fail_closed(self):
        for key, value in [('sessionId', CHILD), ('turnId', 'another-turn'),
                           ('turnStatus', 'idle'), ('startedAt', 'not a date'),
                           ('throughTurn', dict(counts(15), totalTokens=True)),
                           ('completedAt', STAMP)]:
            with self.subTest(key=key):
                self.values[ROOT] = snapshot()
                self.values[ROOT][key] = value
                with self.assertRaises(q.MeterError):
                    self.begin()

    def test_duplicate_source_scope_not_guessed(self):
        archive = self.home / 'archived_sessions'
        archive.mkdir()
        (archive / self.sources[ROOT].name).write_bytes(b'another synthetic copy\n')
        with self.assertRaisesRegex(q.MeterError, 'ambiguous_source'):
            self.begin()
        self.assertEqual('provisional', self.begin(source=self.sources[ROOT])['status'])

    def test_output_cannot_write_into_input_or_package(self):
        with self.assertRaisesRegex(q.MeterError, 'outside_source'):
            q.Meter(self.home / 'output', self.engine).begin(self.home, ROOT)
        with self.assertRaisesRegex(q.MeterError, 'outside_package'):
            q.Meter(q.PACKAGE_ROOT / 'output', self.engine)

    def test_timeout_writes_partial_sidecars_and_no_complete_count(self):
        first = self.begin()
        with patch.object(q.time, 'monotonic', side_effect=[0, 2]):
            result = self.meter.watch(timeout=1)
        self.assertFalse(result['final'])
        self.assertTrue(result['terminal'])
        self.assertIn('query_watch_timeout', result['errors'])
        local = json.loads((self.out / 'query-usage' / 'latest.json').read_bytes())
        self.assertEqual('partial', local['status'])
        self.assertIn(b'window.USAGE_QUERY_METER=', (self.out / 'query-usage' / 'latest.js').read_bytes())
        self.assertTrue((self.out / 'query-usage.html').is_file())

    def test_completed_watch_returns_without_sleep(self):
        self.begin()
        self.finish()
        with patch.object(q.time, 'sleep', side_effect=AssertionError('unexpected sleep')):
            self.assertTrue(self.meter.watch(timeout=1)['final'])

    def test_previous_watcher_cannot_replace_new_active_result(self):
        first = self.begin()
        self.meter.sample(stop_reason='query_watch_timeout')
        second = self.begin()
        self.meter.sample(first['queryId'])
        latest = json.loads((self.out / 'query-usage' / 'latest.json').read_bytes())
        self.assertEqual(second['queryId'], latest['queryId'])

    def test_state_tamper_and_evidence_tamper_rejected(self):
        result = self.begin()
        path = self.meter.state_path(result['queryId'])
        state = json.loads(path.read_bytes())
        state['turns'][0]['baseline']['totalTokens'] = 0
        path.write_text(json.dumps(state), encoding='utf-8')
        with self.assertRaisesRegex(q.MeterError, 'state_changed'):
            self.meter.status()

    def test_stdout_compact_and_no_synthetic_payload_persisted(self):
        result = self.begin()
        self.assertNotIn('turns', q.compact(result))
        for path in self.out.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'synthetic bytes kept', path.read_bytes())

    def test_detached_watcher_bridge_bounds_and_windows_hidden_options(self):
        result = self.begin()
        with patch.object(q.subprocess, 'Popen') as launch:
            self.meter.spawn_watch(result['queryId'], 300, 1)
        command = launch.call_args.args[0]
        self.assertIn('runpy.run_path', command[command.index('-c') + 1])
        self.assertIn('--query-id', command)
        self.assertEqual(subprocess.DEVNULL, launch.call_args.kwargs['stdout'])
        if os.name == 'nt':
            self.assertTrue(launch.call_args.kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW)

    def test_cli_accepts_engine_and_options_after_command(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = q.main(['begin', '--out', str(self.out), '--engine', str(self.engine),
                           '--codex-home', str(self.home), '--thread-id', ROOT,
                           '--turn-id', 'turn-1', '--from-turn-start',
                           '--config', str(self.root / 'absent-settings.json')])
        self.assertEqual(2, code)
        result = json.loads(stdout.getvalue())
        self.assertEqual('provisional', result['status'])
        self.assertNotIn('turns', result)

    def test_exact_large_counter_text_remains_lossless(self):
        amount = (1 << 53) + 1
        self.values[ROOT]['beforeTurn'] = {key: 0 for key in q.COUNTERS}
        self.values[ROOT]['throughTurn'] = {key: amount for key in q.COUNTERS}
        self.finish()
        result = self.begin()
        self.assertEqual(str(amount), result['counterText']['totalTokens'])

    def test_completed_at_from_failed_or_interrupted_native_marker_is_supported(self):
        for status in ('failed', 'interrupted'):
            with self.subTest(status=status):
                value = snapshot(status=status)
                value['readBytes'] = 123
                parsed = q.validate_snapshot(value, ROOT, 'turn-1', 123)
                self.assertEqual(status, parsed['turnStatus'])

    def test_old_evidence_mutation_is_detected_through_chain(self):
        result = self.begin()
        evidence = self.meter.state_path(result['queryId']).parent / 'evidence'
        oldest = next(evidence.glob('*.json'))
        self.meter.sample()
        oldest.write_bytes(b'{}')
        with self.assertRaisesRegex(q.MeterError, 'evidence_changed'):
            self.meter.status()

    def test_engine_change_invalidates_measurement_before_running(self):
        self.begin()
        before = len(self.calls)
        self.engine.write_bytes(b'changed executable')
        result = self.meter.sample()
        self.assertIn('query_engine_changed', result['errors'])
        self.assertEqual(before, len(self.calls))
        self.assertTrue(result['terminal'])

    def test_unchanged_watch_polls_do_not_repeat_native_parsing(self):
        self.begin()
        calls = len(self.calls)
        # First iteration samples; subsequent stat-equal iterations read owned state only.
        with patch.object(q.time, 'monotonic', side_effect=[0, 0, 0.2, 0.5, 2]), patch.object(q.time, 'sleep'):
            result = self.meter.watch(timeout=1, interval=0.1)
        self.assertEqual(2, len(self.calls) - calls)  # Initial sample plus final timeout proof.
        self.assertIn('query_watch_timeout', result['errors'])

    def test_sidecars_label_subtotals_and_query_links_stay_separate(self):
        first = self.begin()
        view = Path(first['viewPath']).read_text(encoding='utf-8')
        self.assertIn('captured-turn subtotal', view)
        self.assertIn('request start excluded', view)
        self.assertIn("s.src='result.js?t='", view)
        self.assertEqual(0, first['modelCalls'])
        self.assertFalse(first['networkUsed'])

    def test_manual_registration_records_claim_without_ownership_proof(self):
        self.values[ROOT]['delegationCalls'] = [{'id': 'call-1', 'kind': 'spawn_agent'}]
        self.begin()
        self.register_child()
        state = self.meter.load()
        child = state['turns'][1]
        self.assertFalse(child['ownershipVerified'])
        self.assertEqual('claimed', child['ownershipStatus'])
        result = self.meter.status()
        self.assertIn('query_agent_ownership_unverified', result['warnings'])
        self.assertEqual('captured-turns', result['subtotalScope'])

    def test_completed_marker_with_partial_tail_stays_provisional(self):
        self.finish()
        self.values[ROOT]['trailingPartial'] = True
        first = self.begin()
        self.assertFalse(first['final'])
        self.assertFalse(first['terminal'])
        self.assertEqual('provisional', first['status'])
        self.assertEqual(1, first['partialSources'])
        self.assertIn('query_trailing_partial', first['warnings'])
        self.values[ROOT]['trailingPartial'] = False
        with self.sources[ROOT].open('ab') as stream:
            stream.write(b'complete synthetic tail\n')
        self.assertTrue(self.meter.sample()['final'])

    def test_watcher_waits_for_partial_tail_to_become_complete(self):
        self.finish()
        self.values[ROOT]['trailingPartial'] = True
        self.begin()
        def complete_tail(_):
            self.values[ROOT]['trailingPartial'] = False
            with self.sources[ROOT].open('ab') as stream:
                stream.write(b'complete synthetic tail\n')
        with patch.object(q.time, 'sleep', side_effect=complete_tail) as sleeping:
            self.assertTrue(self.meter.watch(timeout=3, interval=0.1)['final'])
        self.assertGreaterEqual(sleeping.call_count, 1)

    def test_implicit_selection_rejects_preceding_completed_turn(self):
        self.finish()
        with self.assertRaisesRegex(q.MeterError, 'query_current_turn_unavailable'):
            self.meter.begin(self.home, ROOT)
        self.assertFalse((self.meter.directory / 'active.json').exists())
        self.assertTrue(self.meter.begin(self.home, ROOT, 'turn-1')['final'])

    def test_implicit_active_turn_keeps_verified_start_baseline(self):
        result = self.meter.begin(self.home, ROOT)
        self.assertEqual('provisional', result['status'])
        self.assertEqual(counts(5), result['subtotal'])


    def test_status_does_not_create_missing_scope_or_change_any_acl(self):
        with patch.object(q, 'private', side_effect=AssertionError('read-only status changed an ACL')):
            with self.assertRaises(q.MeterError):
                self.meter.status()
        self.assertFalse(self.out.exists())
        self.begin()
        before = {path: path.read_bytes() for path in self.meter.directory.rglob('*') if path.is_file()}
        with patch.object(q, 'private', side_effect=AssertionError('read-only status changed an ACL')):
            self.assertEqual('provisional', self.meter.status()['status'])
        self.assertEqual(before, {path: path.read_bytes() for path in self.meter.directory.rglob('*') if path.is_file()})






    def test_watch_retries_busy_initial_status_with_original_deadline(self):
        self.begin()
        self.finish()
        status, attempts, clock, sleeps = self.meter.status, [0], [0.0], []
        def first_busy(query_id=None):
            attempts[0] += 1
            if attempts[0] == 1:
                raise q.MeterError('query_meter_busy')
            return status(query_id)
        def sleep(delay):
            sleeps.append(delay)
            clock[0] += delay
        with patch.object(self.meter, 'status', side_effect=first_busy), \
             patch.object(q.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(q.time, 'sleep', side_effect=sleep):
            result = self.meter.watch(timeout=3, interval=.1)
        self.assertTrue(result['final'])
        self.assertEqual(counts(5), result['counters'])
        self.assertEqual([.1], sleeps)
        self.assertEqual(2, attempts[0])

    def test_watch_retries_busy_sample_without_advancing_checkpoint_or_deadline(self):
        first = self.begin()
        baseline = self.meter.load()['turns'][0]['baseline']
        self.finish()
        sample, attempts, clock, deadlines = self.meter.sample, [0], [0.0], []
        def first_busy(*args, **kwargs):
            attempts[0] += 1
            deadlines.append(kwargs.get('deadline'))
            if attempts[0] == 1:
                raise q.MeterError('query_meter_busy')
            return sample(*args, **kwargs)
        with patch.object(self.meter, 'sample', side_effect=first_busy), \
             patch.object(q.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(q.time, 'sleep', side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)):
            result = self.meter.watch(first['queryId'], timeout=3, interval=.1)
        self.assertTrue(result['final'])
        self.assertEqual([3.0, 3.0], deadlines)
        self.assertEqual(first['queryId'], result['queryId'])
        self.assertEqual(baseline, self.meter.load()['turns'][0]['baseline'])

    def test_watch_retries_busy_signature(self):
        self.begin()
        self.finish()
        signature, attempts, clock = self.meter.watch_signature, [0], [0.0]
        def first_busy(query_id):
            attempts[0] += 1
            if attempts[0] == 1:
                raise q.MeterError('query_meter_busy')
            return signature(query_id)
        with patch.object(self.meter, 'watch_signature', side_effect=first_busy), \
             patch.object(q.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(q.time, 'sleep', side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)):
            self.assertTrue(self.meter.watch(timeout=3, interval=.1)['final'])
        self.assertEqual(2, attempts[0])

    def test_watch_retries_busy_status_in_unchanged_iteration(self):
        self.begin()
        status, attempts, clock, sleeps = self.meter.status, [0], [0.0], []
        def second_busy(query_id=None):
            attempts[0] += 1
            if attempts[0] == 2:
                raise q.MeterError('query_meter_busy')
            return status(query_id)
        def sleep(delay):
            sleeps.append(delay)
            clock[0] += delay
            if len(sleeps) == 2:
                self.finish()
        with patch.object(self.meter, 'status', side_effect=second_busy), \
             patch.object(q.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(q.time, 'sleep', side_effect=sleep):
            self.assertTrue(self.meter.watch(timeout=3, interval=.1)['final'])
        self.assertEqual(2, attempts[0])
        self.assertEqual([.1, .1], sleeps)

    def test_watch_busy_expires_at_one_deadline_without_writing_around_lock(self):
        first = self.begin()
        state_path = self.meter.state_path(first['queryId'])
        before, clock, sleeps = state_path.read_bytes(), [0.0], []
        def sleep(delay):
            sleeps.append(delay)
            clock[0] += delay
        with patch.object(self.meter, 'status', side_effect=q.MeterError('query_meter_busy')), \
             patch.object(q.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(q.time, 'sleep', side_effect=sleep):
            with self.assertRaisesRegex(q.MeterError, '^query_watch_timeout$'):
                self.meter.watch(timeout=1, interval=.3)
        self.assertAlmostEqual(1.0, clock[0])
        self.assertTrue(all(0 < delay <= .3 for delay in sleeps))
        self.assertEqual(before, state_path.read_bytes())

    def test_watch_does_not_retry_nonbusy_failure(self):
        self.begin()
        with patch.object(self.meter, 'status', side_effect=q.MeterError('query_evidence_changed')), \
             patch.object(q.time, 'sleep', side_effect=AssertionError('nonbusy failure retried')):
            with self.assertRaisesRegex(q.MeterError, '^query_evidence_changed$'):
                self.meter.watch(timeout=3, interval=.1)

    @unittest.skipUnless(os.name == 'nt', 'Windows inherited ACL behavior')
    def test_existing_out_ancestor_dashboard_source_acl_unchanged(self):
        self.out.mkdir()
        dashboard = self.out / 'existing-dashboard.html'
        dashboard.write_bytes(b'SYNTHETIC_EXISTING_DASHBOARD')
        existing = [self.root, self.out, dashboard, self.sources[ROOT]]
        before = {path: windows_acl(path) for path in existing}
        first = self.begin()
        self.meter.sample()
        self.finish()
        self.assertTrue(self.meter.watch(timeout=3, interval=0.1)['final'])
        self.assertEqual(before, {path: windows_acl(path) for path in existing})
        self.assertEqual(b'SYNTHETIC_EXISTING_DASHBOARD', dashboard.read_bytes())
        self.assertIn(b'window.USAGE_QUERY_METER=', Path(first['resultPath']).with_suffix('.js').read_bytes())

    @unittest.skipUnless(os.name == 'nt', 'Windows inherited ACL behavior')
    def test_missing_out_inherits_ancestor_across_new_meter_and_completion(self):
        principals = ['S-1-5-18', test_execution_sid(), 'S-1-5-21-100-200-300-1999']
        set_isolated_test_acl(self.root, self.root, principals)
        ancestor = windows_acl(self.root)
        self.assertFalse(self.out.exists())
        first = self.begin()
        second = q.Meter(self.out, self.engine)
        second.sample()
        self.finish()
        self.assertTrue(second.watch(timeout=3, interval=0.1)['final'])
        self.assertEqual(ancestor, windows_acl(self.root))
        for path in [self.out, self.meter.directory, Path(first['resultPath']),
                     Path(first['viewPath']), self.out / 'query-usage.html']:
            acl = windows_acl(path)
            self.assertEqual(set(principals), {entry['sid'] for entry in acl['entries']})
            self.assertTrue(all(entry['flags'] & 0x10 for entry in acl['entries']))
            self.assertFalse(acl['protected'])
        self.assertFalse(hasattr(second, 'owner_sid'))
        self.assertFalse(hasattr(second, 'creator_sid'))

    @unittest.skipUnless(os.name == 'nt', 'Windows inherited ACL behavior')
    def test_atomic_replacement_inherits_parent_grants_without_changing_parent(self):
        self.out.mkdir()
        principals = ['S-1-5-18', test_execution_sid(), 'S-1-5-21-100-200-300-1999']
        set_isolated_test_acl(self.out, self.root, principals)
        before = windows_acl(self.out)
        target = self.out / 'query-usage.html'
        q.atomic(target, b'SYNTHETIC_FIRST')
        first = windows_acl(target)
        q.atomic(target, b'SYNTHETIC_SECOND')
        second = windows_acl(target)
        self.assertEqual(before, windows_acl(self.out))
        self.assertEqual(effective_grants(first), effective_grants(second))
        self.assertEqual(set(principals), {entry['sid'] for entry in second['entries']})
        self.assertFalse(second['protected'])
        self.assertEqual(b'SYNTHETIC_SECOND', target.read_bytes())

    @unittest.skipUnless(os.name == 'nt', 'Windows inherited ACL behavior')
    def test_second_writer_preserves_original_access_and_watcher_completes(self):
        # Two independent controllers exercise the replacement behavior. The
        # separate packaged host gate performs real distinct-token execution.
        self.out.mkdir()
        principals = ['S-1-5-18', test_execution_sid(), 'S-1-5-21-100-200-300-1999']
        set_isolated_test_acl(self.out, self.root, principals)
        first = self.begin()
        lock = self.meter.directory / '.lock'
        initial_lock = windows_acl(lock)
        second = q.Meter(self.out, self.engine)
        second.status()
        second.sample()
        self.assertEqual(initial_lock, windows_acl(lock))
        with lock.open('r+b') as stream:
            self.assertEqual(b'\0', stream.read(1))
        self.assertEqual('provisional', self.meter.status()['status'])
        self.assertEqual(set(principals), {entry['sid'] for entry in windows_acl(Path(first['resultPath']))['entries']})
        self.finish()
        self.assertTrue(self.meter.watch(timeout=3, interval=0.1)['final'])
        self.assertEqual(initial_lock, windows_acl(lock))

    @unittest.skipUnless(os.name == 'nt', 'Windows inherited legacy ACL behavior')
    def test_legacy_three_principal_scope_is_preserved_by_new_writers(self):
        self.meter.directory.mkdir(parents=True)
        lock = self.meter.directory / '.lock'
        lock.write_bytes(b'\0')
        principals = ['S-1-5-18', test_execution_sid(), 'S-1-5-21-100-200-300-1999']
        set_isolated_test_acl(self.meter.directory, self.root, principals)
        set_isolated_test_acl(lock, self.root, principals)
        protected = {path: windows_acl(path) for path in (self.meter.directory, lock)}
        first = self.begin()
        second = q.Meter(self.out, self.engine)
        second.status()
        second.sample()
        self.finish()
        self.assertTrue(self.meter.watch(timeout=3, interval=0.1)['final'])
        self.assertEqual(protected, {path: windows_acl(path) for path in protected})
        for path in [Path(first['resultPath']), Path(first['viewPath']), self.meter.directory / 'latest.js']:
            acl = windows_acl(path)
            self.assertEqual(set(principals), {entry['sid'] for entry in acl['entries']})
            self.assertFalse(acl['protected'])
            self.assertTrue(all(entry['flags'] & 0x10 for entry in acl['entries']))

    @unittest.skipUnless(os.name == 'nt', 'Windows inherited ACL behavior')
    def test_repeated_begin_preserves_existing_scope_and_lock_acl(self):
        first = self.begin()
        self.finish()
        self.meter.sample()
        paths = [self.meter.directory, self.meter.directory / '.lock']
        before = {path: windows_acl(path) for path in paths}
        old_result = Path(first['resultPath']).read_bytes()
        second = q.Meter(self.out, self.engine).begin(self.home, ROOT, 'turn-1')
        self.assertTrue(second['final'])
        self.assertNotEqual(first['queryId'], second['queryId'])
        self.assertEqual(before, {path: windows_acl(path) for path in paths})
        self.assertEqual(old_result, Path(first['resultPath']).read_bytes())

    @unittest.skipUnless(os.name == 'nt', 'Windows inherited ACL behavior')
    def test_normal_windows_meter_paths_never_call_security_sdk_or_chmod(self):
        import ctypes
        with patch.object(ctypes, 'WinDLL', side_effect=AssertionError('meter called Windows security SDK')), \
             patch.object(Path, 'chmod', side_effect=AssertionError('meter changed Windows object permissions')):
            self.begin()
            self.meter.status()
            self.meter.sample()
            self.finish()
            self.assertTrue(self.meter.watch(timeout=3, interval=0.1)['final'])


class NativeProductionIntegration(unittest.TestCase):
    """Run explicitly when the freshly built production binary is provided."""
    @staticmethod
    def contention_fixture(root):
        home = root / 'source'
        (home / 'sessions').mkdir(parents=True)
        source = home / 'sessions' / ('rollout-2026-10-28T10-00-00-' + ROOT + '.jsonl')
        def line(kind, payload):
            return json.dumps({'timestamp': STAMP, 'type': kind, 'payload': payload}) + '\n'
        source.write_text(line('session_meta', {'id': ROOT}) +
            line('turn_context', {'turn_id': 'root-turn', 'model': 'gpt-5'}) +
            line('event_msg', {'type': 'token_count', 'info': {'last_token_usage': {
                'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}}), encoding='utf-8')
        meter = q.Meter(root / 'dashboard', os.environ['USAGE_QUERY_TEST_ENGINE'])
        first = meter.begin(home, ROOT, 'root-turn')
        completion = line('event_msg', {'type': 'token_count', 'info': {'last_token_usage': {
            'input_tokens': 200, 'output_tokens': 30, 'total_tokens': 230}}}) + \
            line('event_msg', {'type': 'task_complete', 'turn_id': 'root-turn'})
        return meter, source, first, completion

    @unittest.skipUnless(os.environ.get('USAGE_QUERY_TEST_ENGINE'), 'requires production binary path')
    def test_real_process_lock_at_watch_start_retries_then_records_completion(self):
        with tempfile.TemporaryDirectory(prefix='usage-query-busy-start-synthetic-') as temporary:
            root = Path(temporary)
            meter, source, first, completion = self.contention_fixture(root)
            holder, release = start_isolated_lock_holder(meter.directory, root)
            timer = None
            try:
                with self.assertRaisesRegex(q.MeterError, '^query_meter_busy$'):
                    meter.status()
                with source.open('a', encoding='utf-8') as stream:
                    stream.write(completion)
                timer = threading.Timer(.4, lambda: release.write_bytes(b'release'))
                timer.start()
                result = meter.watch(first['queryId'], timeout=6, interval=.1)
                self.assertTrue(result['final'])
                self.assertEqual(350, result['counters']['totalTokens'])
                self.assertEqual(first['queryId'], result['queryId'])
                self.assertEqual(0, holder.wait(timeout=5))
            finally:
                release.write_bytes(b'release')
                if timer is not None:
                    timer.cancel()
                    timer.join(timeout=5)
                if holder.poll() is None:
                    holder.terminate()
                holder.communicate(timeout=5)

    @unittest.skipUnless(os.environ.get('USAGE_QUERY_TEST_ENGINE'), 'requires production binary path')
    def test_real_process_lock_during_running_watch_preserves_post_response_final(self):
        with tempfile.TemporaryDirectory(prefix='usage-query-busy-running-synthetic-') as temporary:
            root = Path(temporary)
            meter, source, first, completion = self.contention_fixture(root)
            results, errors = [], []
            def observe():
                try:
                    results.append(meter.watch(first['queryId'], timeout=7, interval=.1))
                except BaseException as exc:
                    errors.append(exc)
            watcher = threading.Thread(target=observe)
            holder, release = None, root / 'holder-release'
            watcher.start()
            try:
                deadline = time.monotonic() + 5
                evidence = meter.state_path(first['queryId']).parent / 'evidence'
                while len(list(evidence.glob('*.json'))) < 2:
                    if errors or time.monotonic() >= deadline:
                        self.fail('Watcher did not record its initial production sample.')
                    time.sleep(.02)
                self.assertTrue(watcher.is_alive())
                holder, release = start_isolated_lock_holder(meter.directory, root)
                with self.assertRaisesRegex(q.MeterError, '^query_meter_busy$'):
                    meter.status()
                with source.open('a', encoding='utf-8') as stream:
                    stream.write(completion)
                # Hold across several normal polling iterations.
                time.sleep(.4)
                self.assertTrue(watcher.is_alive())
                release.write_bytes(b'release')
                watcher.join(timeout=8)
                self.assertFalse(watcher.is_alive())
                self.assertEqual([], errors)
                self.assertEqual(1, len(results))
                self.assertTrue(results[0]['final'])
                self.assertEqual(350, results[0]['counters']['totalTokens'])
                saved = json.loads(Path(first['resultPath']).read_bytes())
                self.assertTrue(saved['final'])
                self.assertEqual(350, saved['counters']['totalTokens'])
                self.assertEqual(0, holder.wait(timeout=5))
            finally:
                release.write_bytes(b'release')
                watcher.join(timeout=9)
                if holder is not None:
                    if holder.poll() is None:
                        holder.terminate()
                    holder.communicate(timeout=5)
    @unittest.skipUnless(os.environ.get('USAGE_QUERY_TEST_ENGINE'), 'requires production binary path')
    def test_actual_binary_selected_turn_with_later_turn_excluded(self):
        with tempfile.TemporaryDirectory(prefix='usage-query-native-synthetic-') as temporary:
            root = Path(temporary)
            home = root / 'source'
            sessions = home / 'sessions'
            sessions.mkdir(parents=True)
            source = sessions / ('rollout-2026-10-28T10-00-00-' + ROOT + '.jsonl')
            def line(kind, payload):
                return json.dumps({'timestamp': STAMP, 'type': kind, 'payload': payload}) + '\n'
            def usage(total):
                return line('event_msg', {'type': 'token_count', 'info': {
                    'total_token_usage': {'input_tokens': total * 7, 'cached_input_tokens': total * 2,
                                          'output_tokens': total * 3, 'reasoning_output_tokens': total,
                                          'total_tokens': total * 10}}})
            source.write_text(line('session_meta', {'id': ROOT, 'timestamp': STAMP, 'cwd': str(root)}) +
                line('event_msg', {'type': 'task_started', 'turn_id': 'turn-1'}) +
                line('turn_context', {'turn_id': 'turn-1', 'cwd': str(root), 'model': 'gpt-5'}) + usage(5), encoding='utf-8')
            meter = q.Meter(root / 'dashboard', os.environ['USAGE_QUERY_TEST_ENGINE'])
            before = meter.begin(home, ROOT, 'turn-1')
            self.assertEqual(counts(5), before['subtotal'])
            with source.open('a', encoding='utf-8') as stream:
                stream.write(usage(8) + line('event_msg', {'type': 'task_complete', 'turn_id': 'turn-1'}) +
                    line('event_msg', {'type': 'task_started', 'turn_id': 'turn-2'}) +
                    line('turn_context', {'turn_id': 'turn-2', 'cwd': str(root), 'model': 'gpt-5'}) + usage(15))
            after = meter.sample()
            self.assertEqual('complete', after['status'])
            self.assertEqual(counts(8), after['counters'])
            if os.environ.get('USAGE_QUERY_TEST_EVIDENCE'):
                destination = q.local_path(os.environ['USAGE_QUERY_TEST_EVIDENCE']) / 'synthetic-native-root-query'
                folder = Path(after['resultPath']).parent
                for name in ('result.json', 'result.js', 'index.html'):
                    q.atomic(destination / name, (folder / name).read_bytes())

    @unittest.skipUnless(os.environ.get('USAGE_QUERY_TEST_ENGINE'), 'requires production binary path')
    def test_actual_binary_child_registration_exact_large_sum_and_private_output(self):
        with tempfile.TemporaryDirectory(prefix='usage-query-native-agent-synthetic-') as temporary:
            root = Path(temporary)
            home = root / 'source'
            sessions = home / 'sessions'
            sessions.mkdir(parents=True)
            private_text = 'SYNTHETIC_PRIVATE_PROMPT_ARGUMENT_OUTPUT_99831'
            def line(kind, payload):
                return json.dumps({'timestamp': STAMP, 'type': kind, 'payload': payload}) + '\n'
            paths = {}
            for ident, parent, amount, turn in [(ROOT, None, 5000000000000000, 'root-turn'),
                                                (CHILD, ROOT, 5000000000000001, 'child-turn')]:
                meta = {'id': ident, 'cwd': private_text, 'title': private_text}
                if parent:
                    meta['source'] = {'subagent': {'thread_spawn': {'parent_thread_id': parent,
                                                                   'agent_path': '/root/child'}}}
                path = sessions / ('rollout-2026-10-28T10-00-00-' + ident + '.jsonl')
                data = line('session_meta', meta) + line('turn_context', {'turn_id': turn, 'model': 'gpt-5'})
                if parent is None:
                    data += line('response_item', {'type': 'function_call', 'name': 'collaboration.spawn_agent',
                                                   'call_id': 'spawn-child', 'arguments': private_text})
                data += line('event_msg', {'type': 'token_count', 'info': {'last_token_usage': {
                    'input_tokens': amount, 'cached_input_tokens': 0, 'output_tokens': 0,
                    'reasoning_output_tokens': 0, 'total_tokens': amount}}})
                path.write_text(data, encoding='utf-8')
                paths[ident] = path
            meter = q.Meter(root / 'dashboard', os.environ['USAGE_QUERY_TEST_ENGINE'])
            first = meter.begin(home, ROOT, 'root-turn')
            self.assertEqual(1, first['unaccountedDispatches'])
            meter.register(CHILD, 'child-turn', ROOT, 'root-turn', 'spawn-child')
            for ident, turn in [(ROOT, 'root-turn'), (CHILD, 'child-turn')]:
                with paths[ident].open('a', encoding='utf-8') as stream:
                    stream.write(line('event_msg', {'type': 'task_complete', 'turn_id': turn}))
            result = meter.sample()
            self.assertFalse(result['final'])
            self.assertEqual('partial', result['status'])
            self.assertIsNone(result['counters'])
            self.assertEqual(10000000000000001, result['subtotal']['totalTokens'])
            self.assertEqual('10000000000000001', result['subtotalText']['totalTokens'])
            self.assertEqual(1, result['unverifiedRegistrations'])
            for path in meter.out.rglob('*'):
                if path.is_file():
                    self.assertNotIn(private_text.encode(), path.read_bytes())
            # Optional local numeric-only artifact for independent browser QA.
            if os.environ.get('USAGE_QUERY_TEST_EVIDENCE'):
                destination = q.local_path(os.environ['USAGE_QUERY_TEST_EVIDENCE']) / 'synthetic-native-query'
                folder = Path(result['resultPath']).parent
                for name in ('result.json', 'result.js', 'index.html'):
                    q.atomic(destination / name, (folder / name).read_bytes())

    @unittest.skipUnless(os.environ.get('USAGE_QUERY_TEST_ENGINE'), 'requires production binary path')
    def test_actual_binary_missing_telemetry_never_becomes_complete_zero(self):
        for recorded in (False, True):
            with self.subTest(recorded_zero=recorded), tempfile.TemporaryDirectory(prefix='usage-query-native-zero-') as temporary:
                root = Path(temporary)
                home = root / 'source'
                (home / 'sessions').mkdir(parents=True)
                source = home / 'sessions' / ('rollout-2026-10-28T10-00-00-' + ROOT + '.jsonl')
                def line(kind, payload):
                    return json.dumps({'timestamp': STAMP, 'type': kind, 'payload': payload}) + '\n'
                source.write_text(line('session_meta', {'id': ROOT}) +
                                  line('turn_context', {'turn_id': 'root-turn', 'model': 'gpt-5'}), encoding='utf-8')
                meter = q.Meter(root / 'dashboard', os.environ['USAGE_QUERY_TEST_ENGINE'])
                self.assertFalse(meter.begin(home, ROOT, 'root-turn')['final'])
                with source.open('a', encoding='utf-8') as stream:
                    if recorded:
                        stream.write(line('event_msg', {'type': 'token_count', 'info': {
                            'last_token_usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}))
                    stream.write(line('event_msg', {'type': 'task_complete', 'turn_id': 'root-turn'}))
                result = meter.sample()
                if recorded:
                    self.assertTrue(result['final'])
                    self.assertEqual(0, result['counters']['totalTokens'])
                else:
                    self.assertFalse(result['final'])
                    self.assertIsNone(result['counters'])
                    self.assertIn('query_missing_token_usage', result['errors'])

    @unittest.skipUnless(os.environ.get('USAGE_QUERY_TEST_ENGINE'), 'requires production binary path')
    def test_actual_binary_foreign_older_child_turn_never_proves_request_ownership(self):
        with tempfile.TemporaryDirectory(prefix='usage-query-foreign-turn-regression-') as temporary:
            root = Path(temporary)
            home = root / 'source'
            (home / 'sessions').mkdir(parents=True)
            def line(kind, payload, stamp=STAMP):
                return json.dumps({'timestamp': stamp, 'type': kind, 'payload': payload}) + '\n'
            def usage(total, stamp=STAMP):
                return line('event_msg', {'type': 'token_count', 'info': {'last_token_usage': {
                    'input_tokens': total, 'output_tokens': 0, 'total_tokens': total}}}, stamp)
            source = home / 'sessions' / ('rollout-2026-10-28T10-00-00-' + ROOT + '.jsonl')
            source.write_text(line('session_meta', {'id': ROOT}) +
                line('turn_context', {'turn_id': 'current-root-turn', 'model': 'gpt-5'}) +
                line('response_item', {'type': 'function_call', 'name': 'collaboration.followup_task',
                                       'call_id': 'current-dispatch', 'arguments': 'SYNTHETIC_PRIVATE'}) + usage(10), encoding='utf-8')
            child = home / 'sessions' / ('rollout-2026-10-27T10-00-00-' + CHILD + '.jsonl')
            old = '2026-10-27T10:00:00Z'
            child.write_text(line('session_meta', {'id': CHILD, 'source': {'subagent': {'thread_spawn': {
                'parent_thread_id': ROOT, 'agent_path': '/root/old'}}}}, old) +
                line('turn_context', {'turn_id': 'older-unrelated-child-turn', 'model': 'gpt-5'}, old) + usage(99, old) +
                line('event_msg', {'type': 'task_complete', 'turn_id': 'older-unrelated-child-turn'}, old), encoding='utf-8')
            meter = q.Meter(root / 'dashboard', os.environ['USAGE_QUERY_TEST_ENGINE'])
            meter.begin(home, ROOT, 'current-root-turn')
            meter.register(CHILD, 'older-unrelated-child-turn', ROOT, 'current-root-turn', 'current-dispatch')
            with source.open('a', encoding='utf-8') as stream:
                stream.write(line('event_msg', {'type': 'task_complete', 'turn_id': 'current-root-turn'}))
            result = meter.sample()
            self.assertFalse(result['final'])
            self.assertEqual('partial', result['status'])
            self.assertIsNone(result['counters'])
            self.assertEqual(109, result['subtotal']['totalTokens'])
            self.assertFalse(result['ownershipVerified'])
            self.assertEqual(1, result['unverifiedRegistrations'])
            self.assertEqual(1, result['unaccountedDispatches'])
            self.assertEqual('claimed', result['turns'][1]['ownershipStatus'])

    @unittest.skipUnless(os.environ.get('USAGE_QUERY_TEST_ENGINE'), 'requires production binary path')
    def test_actual_binary_completed_marker_partial_usage_tail_waits_for_full_prefix(self):
        with tempfile.TemporaryDirectory(prefix='usage-query-partial-tail-regression-') as temporary:
            root = Path(temporary)
            home = root / 'source'
            (home / 'sessions').mkdir(parents=True)
            source = home / 'sessions' / ('rollout-2026-10-28T10-00-00-' + ROOT + '.jsonl')
            def line(kind, payload):
                return json.dumps({'timestamp': STAMP, 'type': kind, 'payload': payload}) + '\n'
            def usage(total):
                return line('event_msg', {'type': 'token_count', 'info': {'last_token_usage': {
                    'input_tokens': total, 'output_tokens': 0, 'total_tokens': total}}})
            tail = usage(1)
            midpoint = len(tail) // 2
            source.write_text(line('session_meta', {'id': ROOT}) +
                line('turn_context', {'turn_id': 'root-turn', 'model': 'gpt-5'}) + usage(10) +
                line('event_msg', {'type': 'task_complete', 'turn_id': 'root-turn'}) + tail[:midpoint], encoding='utf-8')
            meter = q.Meter(root / 'dashboard', os.environ['USAGE_QUERY_TEST_ENGINE'])
            first = meter.begin(home, ROOT, 'root-turn')
            self.assertEqual('provisional', first['status'])
            self.assertFalse(first['final'])
            self.assertFalse(first['terminal'])
            self.assertEqual(1, first['partialSources'])
            self.assertEqual(10, first['subtotal']['totalTokens'])
            with source.open('a', encoding='utf-8') as stream:
                stream.write(tail[midpoint:])
            result = meter.sample()
            self.assertTrue(result['final'])
            self.assertEqual(11, result['counters']['totalTokens'])

    @unittest.skipUnless(os.environ.get('USAGE_QUERY_TEST_ENGINE'), 'requires production binary path')
    def test_actual_binary_implicit_completed_turn_is_rejected_before_query_activation(self):
        with tempfile.TemporaryDirectory(prefix='usage-query-implicit-turn-regression-') as temporary:
            root = Path(temporary)
            home = root / 'source'
            (home / 'sessions').mkdir(parents=True)
            source = home / 'sessions' / ('rollout-2026-10-28T10-00-00-' + ROOT + '.jsonl')
            def line(kind, payload):
                return json.dumps({'timestamp': STAMP, 'type': kind, 'payload': payload}) + '\n'
            source.write_text(line('session_meta', {'id': ROOT}) +
                line('turn_context', {'turn_id': 'preceding-completed-turn', 'model': 'gpt-5'}) +
                line('event_msg', {'type': 'token_count', 'info': {'last_token_usage': {
                    'input_tokens': 10, 'output_tokens': 0, 'total_tokens': 10}}}) +
                line('event_msg', {'type': 'task_complete', 'turn_id': 'preceding-completed-turn'}), encoding='utf-8')
            meter = q.Meter(root / 'dashboard', os.environ['USAGE_QUERY_TEST_ENGINE'])
            with self.assertRaisesRegex(q.MeterError, 'query_current_turn_unavailable'):
                meter.begin(home, ROOT)
            self.assertFalse((meter.directory / 'active.json').exists())
            self.assertEqual(10, meter.begin(home, ROOT, 'preceding-completed-turn')['counters']['totalTokens'])

    @unittest.skipUnless(os.environ.get('USAGE_QUERY_TEST_ENGINE'), 'requires production binary path')
    def test_actual_detached_watcher_updates_after_synthetic_response_completes(self):
        with tempfile.TemporaryDirectory(prefix='usage-query-detached-synthetic-') as temporary:
            root = Path(temporary)
            home = root / 'source'
            (home / 'sessions').mkdir(parents=True)
            source = home / 'sessions' / ('rollout-2026-10-28T10-00-00-' + ROOT + '.jsonl')
            def line(kind, payload):
                return json.dumps({'timestamp': STAMP, 'type': kind, 'payload': payload}) + '\n'
            source.write_text(line('session_meta', {'id': ROOT}) +
                              line('turn_context', {'turn_id': 'root-turn', 'model': 'gpt-5'}) +
                              line('event_msg', {'type': 'token_count', 'info': {'last_token_usage': {
                                  'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}}), encoding='utf-8')
            meter = q.Meter(root / 'dashboard', os.environ['USAGE_QUERY_TEST_ENGINE'])
            first = meter.begin(home, ROOT, 'root-turn')
            real_popen = subprocess.Popen
            processes = []
            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                processes.append(process)
                return process
            with patch.object(q.subprocess, 'Popen', side_effect=launch):
                meter.spawn_watch(first['queryId'], 10, 0.1)
            process = processes[0]
            try:
                # Simulate output/completion being written after the model-facing begin returned.
                with source.open('a', encoding='utf-8') as stream:
                    stream.write(line('event_msg', {'type': 'token_count', 'info': {'last_token_usage': {
                        'input_tokens': 200, 'output_tokens': 30, 'total_tokens': 230}}}) +
                        line('event_msg', {'type': 'task_complete', 'turn_id': 'root-turn'}))
                self.assertEqual(0, process.wait(timeout=15))
                result = json.loads(Path(first['resultPath']).read_bytes())
                self.assertTrue(result['final'])
                self.assertEqual(350, result['counters']['totalTokens'])
                self.assertIn('350', Path(first['resultPath']).with_suffix('.js').read_text(encoding='utf-8'))
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
