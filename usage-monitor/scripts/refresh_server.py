"""Capability-protected loopback refresh for one existing local dashboard.

This service has no discovery, setup, arbitrary-file or command endpoint.  A
browser supplies only the same dates and catalog IDs accepted by the renderer;
the fixed, verified Windows entry performs the existing accounting transaction.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


SERVICE = 'usage-monitor-refresh'
MAX_BODY = 16384
MAX_JOBS = 16
REFRESH_TIMEOUT = 300
START_TIMEOUT = 25
_TOKEN = re.compile(r'^[A-Za-z0-9_-]{32,128}$')
_JOB = re.compile(r'^[A-Za-z0-9_-]{24}$')
_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$', re.ASCII)
class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, target):
        # Reject before urllib can connect to any redirect target, including a
        # foreign host supplied by a different listener on a stale local port.
        return None


_NO_PROXY = build_opener(ProxyHandler({}), _NoRedirect())


class RefreshError(ValueError):
    """A static failure code; never send exception text or child output to HTTP."""


def _local_path(value):
    path = Path(value).expanduser().absolute()
    if str(path).startswith(('\\\\', '//')):
        raise RefreshError('refresh_local_path_required')
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RefreshError('refresh_reparse_path')
    result = path.resolve()
    # Windows realpath can retain this prefix while a different startup process
    # briefly holds a file handle. A resolved local drive is still local; UNC
    # and other device namespaces remain forbidden.
    if os.name == 'nt' and re.match(r'^\\\\\?\\[A-Za-z]:\\', str(result)):
        result = Path(str(result)[4:])
    if str(result).startswith(('\\\\', '//')):
        raise RefreshError('refresh_local_path_required')
    return result


def _private(path):
    # Windows deliberately inherits the selected private profile's ACL.  Do not
    # rewrite an existing directory's DACL or turn inherited rules into grants.
    if os.name != 'nt':
        Path(path).chmod(0o700 if Path(path).is_dir() else 0o600)


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('ascii')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise RefreshError('refresh_invalid_json')
        value[key] = item
    return value


def _read_json(path, limit=1048576):
    path = _local_path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise RefreshError('refresh_invalid_report')
    return json.loads(path.read_bytes(), object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(RefreshError('refresh_invalid_json')))


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def verified_existing_report(out):
    """Read only the saved numeric report, checking its original accounting proof."""
    from dashboard_data import _validate
    from refresh_usage import validate_selection
    out = _local_path(out)
    try:
        # Evidence is published last only after the existing transaction passes.
        # Recheck it after reading to reject a concurrent mixed publication.
        evidence_path = _local_path(out / 'evidence.json')
        evidence_bytes = evidence_path.read_bytes()
        evidence = json.loads(evidence_bytes, object_pairs_hook=_unique)
        data_path, html_path = _local_path(out / 'data.json'), _local_path(out / 'index.html')
        data, html = data_path.read_bytes(), html_path.read_bytes()
        if (_digest(data) != evidence['dataSha256'] or _digest(html) != evidence['htmlSha256'] or
                evidence_path.read_bytes() != evidence_bytes):
            raise RefreshError('refresh_invalid_report')
        payload = json.loads(data, object_pairs_hook=_unique)
        if not isinstance(payload, dict) or set(payload) - {'browserBundle', 'initialSelection', 'refresh'}:
            raise RefreshError('refresh_invalid_report')
        bundle, selection = payload['browserBundle'], payload.get('initialSelection')
        _validate(bundle, evidence)
        if 'projectMetadata' in bundle:
            if _digest(_json(bundle['projectMetadata'])) != evidence.get('projectMetadataSha256'):
                raise RefreshError('refresh_invalid_report')
        if selection is not None:
            selection = _validate_selection(bundle, selection)
        else:
            validate_selection(bundle, {'startDate': None, 'endDate': None, 'projects': [], 'chats': []})
        return bundle, selection
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise RefreshError('refresh_invalid_report') from exc


def _validate_selection(bundle, selection):
    from refresh_usage import validate_selection
    if not isinstance(selection, dict) or set(selection) != {'startDate', 'endDate', 'projects', 'chats'}:
        raise RefreshError('refresh_invalid_selection')
    if any(not isinstance(selection[key], str) or not _DATE.fullmatch(selection[key])
           for key in ('startDate', 'endDate')):
        raise RefreshError('refresh_invalid_selection')
    for key in ('projects', 'chats'):
        values = selection[key]
        if (not isinstance(values, list) or len(values) > 256 or
                any(not isinstance(item, str) or not item or len(item) > 1024 for item in values)):
            raise RefreshError('refresh_invalid_selection')
    try:
        return validate_selection(bundle, selection)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise RefreshError('refresh_invalid_selection') from exc


def _profile(package_root, settings_file, out):
    from local_setup import load_settings
    package_root, settings_file, out = map(_local_path, (package_root, settings_file, out))
    if (settings_file.is_relative_to(package_root) or out.is_relative_to(package_root) or
            package_root.is_relative_to(out)):
        raise RefreshError('refresh_keep_state_outside_package')
    if not package_root.is_dir() or not settings_file.is_file() or not out.is_dir():
        raise RefreshError('refresh_setup_required')
    saved = load_settings(settings_file)
    if not saved or _local_path(saved['outputDirectory']) != out:
        raise RefreshError('refresh_profile_changed')
    # Resolve only explicit saved paths.  In particular, never discover another
    # source, another account, or a replacement setup from the browser request.
    for key in ('database', 'cacheFile', 'projectState'):
        if saved.get(key):
            _local_path(saved[key])
    for item in saved.get('namesDatabases') or []:
        _local_path(item)
    if saved.get('offline'):
        for key in ('codexHome', 'engine', 'runtimeDirectory'):
            _local_path(saved['offline'][key])
    return package_root, settings_file, out, saved


def _package_identity(package_root):
    # The manifest binds every verified public file.  Source-only tests have no
    # manifest and bind the same complete public source list instead.
    manifest = package_root / 'bundle-manifest.json'
    if manifest.is_file():
        return _digest(manifest.read_bytes())
    from refresh_usage import package_files
    return _digest(_json({path.relative_to(package_root).as_posix(): _digest(path.read_bytes())
                          for path in package_files(package_root)}))


def _binding(package_root, settings_file, out):
    paths = [os.path.normcase(str(path)) for path in (package_root, settings_file, out)]
    return _digest(_json(paths))


def _metadata_path(package_root, settings_file, out):
    path = _local_path(settings_file.parent / '.usage-refresh')
    if path.is_relative_to(package_root) or path.is_relative_to(out):
        raise RefreshError('refresh_keep_state_outside_package')
    return path / (_binding(package_root, settings_file, out) + '.json')


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.refresh-', dir=path.parent)
    temporary = Path(name)
    try:
        _private(temporary)
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = None
            stream.write(_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


@contextmanager
def _locked(path, timeout=START_TIMEOUT):
    path = _local_path(path)
    created = False
    try:
        path.parent.mkdir(parents=True)
        created = True
    except FileExistsError:
        pass
    if created:
        _private(path.parent)
    try:
        with path.open('xb') as stream:
            _private(path)
            stream.write(b'\0')
    except FileExistsError:
        pass
    with path.open('r+b') as stream:
        deadline = time.monotonic() + timeout
        while True:
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RefreshError('refresh_busy') from None
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _verified_python(package_root):
    from standalone_app import _package_api
    verify_bundle, _, resolve_prebuilt = _package_api(package_root)
    verify_bundle(package_root)
    installed = resolve_prebuilt(package_root, package_root / 'runtime')
    # A fresh embedded helper has its own isolated sys.path. Load only the
    # verified bundled timezone data before validating a non-UTC saved report.
    packages = installed['pythonPackages']
    if packages not in sys.path:
        sys.path.insert(0, packages)
    python = _local_path(installed.get('python') or package_root / 'runtime/python/python.exe')
    if not python.is_file():
        raise RefreshError('refresh_unavailable')
    return python


def _environment():
    allowed = {'PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR',
               'HOME', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'TZ', 'CODEX_USAGE_TIMEZONE'}
    return {key: value for key, value in os.environ.items() if key in allowed}


def _python_command(python, package_root, script, *, gate=False):
    # The shipped embeddable Python uses an isolated ._pth file: a script path
    # does not add its directory to sys.path.  Mirror the verified public bridge
    # with a fixed program and one trusted, explicit scripts-directory argument.
    barrier = ("import sys; permit=sys.stdin.buffer.read(1); "
               "sys.exit(1) if permit!=b'R' else None; ") if gate else ''
    bridge = (barrier + "import runpy,sys; p=sys.argv.pop(1); sys.path.insert(0,p); "
              "runpy.run_path(p+'/" + script + "',run_name='__main__')")
    from macos_runtime import is_macos_bundle
    isolated = ['-I', '-S'] if is_macos_bundle(package_root) else []
    return [str(python), *isolated, '-B', '-X', 'utf8', '-c', bridge, str(package_root / 'scripts')]


@contextmanager
def _process_tree(process):
    """Kill scanner descendants on failure/timeout, before restoring snapshots."""
    job = None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                     wintypes.DWORD, ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        class Basic(ctypes.Structure):
            _fields_ = [('processTime', ctypes.c_longlong), ('jobTime', ctypes.c_longlong),
                        ('flags', wintypes.DWORD), ('minWorkingSet', ctypes.c_size_t),
                        ('maxWorkingSet', ctypes.c_size_t), ('activeProcesses', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(key, ctypes.c_ulonglong) for key in
                        ('readOps', 'writeOps', 'otherOps', 'readBytes', 'writeBytes', 'otherBytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('processMemory', ctypes.c_size_t),
                        ('jobMemory', ctypes.c_size_t), ('peakProcessMemory', ctypes.c_size_t),
                        ('peakJobMemory', ctypes.c_size_t)]
        class Accounting(ctypes.Structure):
            _fields_ = [(key, ctypes.c_longlong) for key in ('userTime', 'kernelTime', 'periodUserTime',
                         'periodKernelTime')] + [(key, wintypes.DWORD) for key in
                         ('pageFaultCount', 'totalProcesses', 'activeProcesses', 'terminatedProcesses')]
        job = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or
                not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle)))):
            if job:
                kernel.CloseHandle(job)
            process.kill()
            process.wait(timeout=10)
            raise RefreshError('refresh_unavailable')
    try:
        yield
    finally:
        if job:
            # A successful main process must not leave any scanner descendant.
            try:
                if not kernel.TerminateJobObject(job, 1):
                    raise RefreshError('refresh_unavailable')
                deadline = time.monotonic() + 10
                while True:
                    accounting = Accounting()
                    if not kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting),
                                                             ctypes.sizeof(accounting), None):
                        raise RefreshError('refresh_unavailable')
                    if not accounting.activeProcesses:
                        break
                    if time.monotonic() >= deadline:
                        raise RefreshError('refresh_unavailable')
                    time.sleep(0.01)
            finally:
                kernel.CloseHandle(job)
        elif os.name != 'nt':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def _run_trusted_refresh(package_root, settings_file, out, selection, *, expected_settings=None):
    saved = _profile(package_root, settings_file, out)[3]
    if expected_settings is not None and saved != expected_settings:
        raise RefreshError('refresh_profile_changed')
    if saved.get('schemaVersion') != 2 or not saved.get('offline'):
        # The public Windows entry is an offline scanner. A legacy cache-only
        # profile has no verified source home, so never let it discover one.
        raise RefreshError('refresh_unavailable')
    python = _verified_python(package_root)
    command = [*_python_command(python, package_root, 'standalone_app.py', gate=True),
               '--no-serve', '--no-open', '--config', str(settings_file), '--out', str(out),
               '--codex-home', saved['offline']['codexHome'],
               '--from-date', selection['startDate'], '--to-date', selection['endDate']]
    if saved['offline']['timezone']:
        command.extend(['--timezone', saved['offline']['timezone']])
    for kind, flag in (('projects', '--project-id'), ('chats', '--chat-id')):
        for identity in selection[kind]:
            # An equals-form argument cannot become another option even if a
            # real verified project ID begins with a hyphen.
            command.append(flag + '=' + identity)
    options = {'stdin': subprocess.PIPE, 'env': _environment(), 'cwd': str(package_root), 'shell': False}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options['start_new_session'] = True
    # Child output is never served, logged, or retained.  Temporary files also
    # bound memory independently of the number of source diagnostics.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, **options)
        try:
            with _process_tree(process):
                # No package code can run or create descendants before the process
                # belongs to its kill-on-close job. EOF/failed assignment exits the
                # fixed bridge without ever importing the scanner entry.
                process.stdin.write(b'R')
                process.stdin.flush()
                process.stdin.close()
                try:
                    code = process.wait(timeout=REFRESH_TIMEOUT)
                except subprocess.TimeoutExpired as exc:
                    raise RefreshError('refresh_timeout') from exc
                if code not in (0, 2):
                    raise RefreshError('refresh_failed')
        finally:
            process.stdin.close()


class RefreshState:
    """One fixed profile, a last verified in-memory view, and one active job."""
    def __init__(self, package_root, settings_file, out, *, idle_timeout=3600, runner=None):
        self.package_root, self.settings_file, self.out, self.settings = _profile(package_root, settings_file, out)
        if not isinstance(idle_timeout, (int, float)) or not 0.1 <= idle_timeout <= 86400:
            raise RefreshError('refresh_invalid_idle_timeout')
        self.idle_timeout = idle_timeout
        self.profile = _digest(_json(self.settings))
        self.revision = _package_identity(self.package_root)
        self.binding = _binding(self.package_root, self.settings_file, self.out)
        self.instance = secrets.token_urlsafe(24)
        self.token = secrets.token_urlsafe(32)
        self.last_active = time.monotonic()
        self.lock = threading.RLock()
        self.bundle, self.selection = verified_existing_report(self.out)
        # Unknown until a consistent evidence/read pair has been established;
        # an ordinary launcher may publish another valid report at any time.
        self.report_stamp = None
        self.jobs = {}
        self.active = None
        self.base_url = None
        self.runner = runner or (lambda selection: _run_trusted_refresh(
            self.package_root, self.settings_file, self.out, selection, expected_settings=self.settings))

    def touch(self):
        with self.lock:
            self.last_active = time.monotonic()

    def identity(self):
        return {'schemaVersion': 1, 'service': SERVICE, 'binding': self.binding,
                'profile': self.profile, 'revision': self.revision, 'instance': self.instance, 'pid': os.getpid()}

    def sync_report(self, *, strict=False):
        """Adopt a new verified launcher publication; retain the last good view.

        Publication replaces several files atomically one at a time. A reader
        can see that short mixed interval, so retry a bounded number of times.
        An active service job already owns publication and updates this snapshot
        itself. No source database or raw session is ever read here.
        """
        with self.lock:
            if self.active:
                return
            for attempt in range(3):
                try:
                    path = _local_path(self.out / 'evidence.json')
                    if path.stat().st_size > 1048576:
                        raise RefreshError('refresh_invalid_report')
                    before = path.read_bytes()
                    stamp = _digest(before)
                    if stamp == self.report_stamp and not strict:
                        return
                    bundle, selection = verified_existing_report(self.out)
                    if path.read_bytes() != before:
                        raise RefreshError('refresh_invalid_report')
                    self.bundle, self.selection, self.report_stamp = bundle, selection, stamp
                    return
                except (OSError, ValueError):
                    if attempt < 2:
                        time.sleep(0.02)
            if strict:
                raise RefreshError('refresh_invalid_report')

    def _unchanged_profile(self):
        current = _profile(self.package_root, self.settings_file, self.out)[3]
        if _digest(_json(current)) != self.profile or _package_identity(self.package_root) != self.revision:
            raise RefreshError('refresh_profile_changed')

    def html(self, auto_refresh=False):
        from refresh_usage import render_dashboard
        with self.lock:
            self.sync_report()
            return render_dashboard(self.bundle, self.package_root, selection=self.selection,
                                    refresh={'baseUrl': self.base_url, 'refreshUrl': self.base_url + 'refresh.html',
                                             'autoRefresh': auto_refresh}).encode('utf-8')

    def submit(self, value):
        with self.lock:
            self._unchanged_profile()
            self.sync_report(strict=True)
            selection = _validate_selection(self.bundle, value)
            if self.active:
                job = self.jobs[self.active]
                if job['selection'] != selection:
                    raise RefreshError('refresh_busy')
                return self.public_job(job)
            job_id = secrets.token_urlsafe(18)
            job = {'jobId': job_id, 'status': 'running', 'selection': selection}
            while len(self.jobs) >= MAX_JOBS:
                del self.jobs[next(iter(self.jobs))]
            self.jobs[job_id], self.active = job, job_id
            threading.Thread(target=self._work, args=(job,), daemon=True, name='usage-refresh-job').start()
            return self.public_job(job)

    def public_job(self, job):
        from refresh_usage import selected_dashboard_url
        result = {key: job[key] for key in ('jobId', 'status')}
        result.update(statusUrl=self.base_url + 'api/jobs/' + job['jobId'],
                      dashboardUrl=selected_dashboard_url(self.base_url + 'index.html', job['selection']))
        if 'error' in job:
            result['error'] = job['error']
        return result

    def _work(self, job):
        from file_transaction import preserve_files
        metadata = _metadata_path(self.package_root, self.settings_file, self.out)
        try:
            # Also serialize separate service processes of the same profile.
            with _locked(metadata.with_suffix('.refresh.lock'), timeout=0.1):
                self._unchanged_profile()
                protected = [self.settings_file, self.settings_file.parent / '.usage-python',
                             self.settings['cacheFile'] or self.out / 'usage-ledger.json',
                             *(self.out / name for name in ('index.html', 'data.json', 'evidence.json',
                               'summary.txt', '.usage-selection.json', '.usage-setup.json', '.usage-python'))]
                if self.settings.get('offline'):
                    protected.append(self.settings['database'])
                # The inner pipeline already rolls back ordinary failures.  An
                # outer snapshot also covers a bounded process-tree timeout or
                # success output that fails the original report verification.
                with preserve_files(protected):
                    self.runner(job['selection'])
                    self._unchanged_profile()
                    bundle, selection = verified_existing_report(self.out)
                    if selection != job['selection']:
                        raise RefreshError('refresh_invalid_report')
                with self.lock:
                    self.bundle, self.selection = bundle, selection
                    self.report_stamp = None
                    job['status'] = 'complete'
        except Exception as exc:
            safe = {'refresh_busy', 'refresh_timeout', 'refresh_failed', 'refresh_invalid_report',
                    'refresh_profile_changed', 'refresh_unavailable'}
            with self.lock:
                job['status'] = 'failed'
                job['error'] = str(exc) if isinstance(exc, RefreshError) and str(exc) in safe else 'refresh_failed'
        finally:
            with self.lock:
                self.active = None
                self.last_active = time.monotonic()


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, state):
        self.state = state
        self.capacity = threading.BoundedSemaphore(16)
        super().__init__(('127.0.0.1', 0), _Handler)
        self.host = '127.0.0.1:' + str(self.server_port)
        self.origin = 'http://' + self.host
        state.base_url = self.origin + '/' + state.token + '/'

    def process_request(self, request, client_address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.capacity.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.capacity.release()

    def handle_error(self, request, client_address):
        # No URLs (capabilities), paths, exceptions or child output in logs.
        pass


class _Handler(BaseHTTPRequestHandler):
    server_version = 'UsageMonitor'
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def _send(self, code, payload, content_type='application/json; charset=utf-8'):
        if not isinstance(payload, bytes):
            payload = _json(payload)
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; font-src data:; img-src data:; connect-src 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _route(self):
        if self.client_address[0] != '127.0.0.1' or self.headers.get_all('Host') != [self.server.host]:
            self._send(403, {'error': 'refresh_forbidden'})
            return None
        origin = self.headers.get_all('Origin')
        if origin and origin != [self.server.origin]:
            self._send(403, {'error': 'refresh_forbidden'})
            return None
        # Compare the raw path, with no URL-decoding, normalization, redirect or
        # filesystem join.  Encoded separators and dot segments never match.
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self._send(404, {'error': 'refresh_not_found'})
            return None
        prefix = '/' + self.server.state.token + '/'
        if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith(prefix):
            self._send(404, {'error': 'refresh_not_found'})
            return None
        route = parsed.path[len(prefix):]
        if '%' in route or '\\' in route or any(part in {'.', '..'} for part in route.split('/')):
            self._send(404, {'error': 'refresh_not_found'})
            return None
        # Only harmless cache-busting query parameters on fixed query sidecars.
        if parsed.query and not (route.endswith('.js') and
                re.fullmatch(r'(?:updated|t)=\d{1,20}', parsed.query, flags=re.ASCII)):
            self._send(404, {'error': 'refresh_not_found'})
            return None
        return route

    def do_GET(self):
        route = self._route()
        if route is None:
            return
        state = self.server.state
        try:
            if route in ('', 'index.html', 'refresh.html'):
                self._send(200, state.html(route == 'refresh.html'), 'text/html; charset=utf-8')
            elif route == 'api/ping':
                state.sync_report()
                self._send(200, state.identity())
            elif route.startswith('api/jobs/') and _JOB.fullmatch(route[9:]):
                with state.lock:
                    job = state.jobs.get(route[9:])
                    if job is None:
                        self._send(404, {'error': 'refresh_not_found'})
                        return
                    self._send(200, state.public_job(job))
            elif self._query(route):
                pass
            else:
                self._send(404, {'error': 'refresh_not_found'})
                return
            state.touch()
        except (ValueError, OSError, KeyError, TypeError):
            self._send(503, {'error': 'refresh_unavailable'})

    def _query(self, route):
        from query_meter import VIEW
        out = self.server.state.out
        if route == 'query-usage.html':
            _local_path(out / route).stat()
            self._send(200, VIEW.encode('utf-8'), 'text/html; charset=utf-8')
            return True
        parts = route.split('/')
        latest = route == 'query-usage/latest.js'
        specific = (len(parts) == 3 and parts[0] == 'query-usage' and _UUID.fullmatch(parts[1]) and
                    parts[2] in ('index.html', 'result.js'))
        if not latest and not specific:
            return False
        if specific and parts[2] == 'index.html':
            _local_path(out.joinpath(*parts)).stat()
            self._send(200, VIEW.replace('query-usage/latest.js', 'result.js').encode('utf-8'),
                       'text/html; charset=utf-8')
            return True
        # Do not execute a saved JavaScript file as arbitrary code.  Accept only
        # the meter's one JSON assignment and reserialize it as inert safe data.
        path = _local_path(out.joinpath(*parts))
        if path.stat().st_size > 1048576:
            raise RefreshError('refresh_invalid_report')
        raw = path.read_bytes()
        marker = b'window.USAGE_QUERY_METER='
        if not raw.startswith(marker) or not raw.endswith(b';\n'):
            raise RefreshError('refresh_invalid_report')
        value = json.loads(raw[len(marker):-2], object_pairs_hook=_unique)
        if (not isinstance(value, dict) or value.get('schemaVersion') != 1 or
                not isinstance(value.get('queryId'), str) or not _UUID.fullmatch(value['queryId'])):
            raise RefreshError('refresh_invalid_report')
        payload = _json(value).replace(b'<', b'\\u003c').replace(b'>', b'\\u003e').replace(b'&', b'\\u0026')
        self._send(200, marker + payload + b';\n', 'text/javascript; charset=utf-8')
        return True

    def do_POST(self):
        route = self._route()
        if route is None:
            return
        if (self.headers.get_all('Origin') != [self.server.origin] or
                self.headers.get_all('X-Usage-Monitor') != ['refresh']):
            self._send(403, {'error': 'refresh_forbidden'})
            return
        if route != 'api/refresh':
            self._send(404, {'error': 'refresh_not_found'})
            return
        if (self.headers.get_all('Content-Type') != ['application/json'] or
                self.headers.get_all('Transfer-Encoding')):
            self._send(400, {'error': 'refresh_invalid_request'})
            return
        lengths = self.headers.get_all('Content-Length')
        if not lengths or len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,9}', lengths[0]):
            self._send(400, {'error': 'refresh_invalid_request'})
            return
        length = int(lengths[0])
        if not 1 <= length <= MAX_BODY:
            self._send(413, {'error': 'refresh_request_too_large'})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise RefreshError('refresh_invalid_selection')
            value = json.loads(raw, object_pairs_hook=_unique)
            result = self.server.state.submit(value)
            self.server.state.touch()
            self._send(202, result)
        except (ValueError, UnicodeError, KeyError, TypeError, OverflowError) as exc:
            code = str(exc) if isinstance(exc, RefreshError) else 'refresh_invalid_selection'
            if code not in {'refresh_busy', 'refresh_invalid_selection', 'refresh_profile_changed', 'refresh_invalid_report'}:
                code = 'refresh_invalid_selection'
            self._send(409 if code in {'refresh_busy', 'refresh_profile_changed', 'refresh_invalid_report'} else 400,
                       {'error': code})

    def do_OPTIONS(self):
        # No CORS preflight grant, even if a capability URL becomes known.
        self._send(403, {'error': 'refresh_forbidden'})


def create_server(package_root, settings_file, out, *, idle_timeout=3600, runner=None):
    """Construct an unstarted service; an explicit runner is for synthetic tests."""
    return _LoopbackServer(RefreshState(package_root, settings_file, out,
                                       idle_timeout=idle_timeout, runner=runner))


def serve(server, metadata_path=None):
    """Run until no active job or authenticated page heartbeat remains."""
    state = server.state
    if metadata_path:
        _atomic(metadata_path, {**state.identity(), 'baseUrl': state.base_url})
    stop = threading.Event()
    def idle_watch():
        while not stop.wait(min(1, max(0.05, state.idle_timeout / 4))):
            with state.lock:
                expired = not state.active and time.monotonic() - state.last_active >= state.idle_timeout
            if expired:
                server.shutdown()
                return
    threading.Thread(target=idle_watch, daemon=True, name='usage-refresh-idle').start()
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        stop.set()
        server.server_close()
        if metadata_path:
            try:
                if _read_json(metadata_path).get('instance') == state.instance:
                    Path(metadata_path).unlink()
            except (OSError, ValueError, AttributeError):
                pass


def _handshake(metadata, expected):
    if not isinstance(metadata, dict) or any(metadata.get(key) != value for key, value in expected.items()):
        return None
    base = metadata.get('baseUrl')
    if not isinstance(base, str):
        return None
    try:
        parsed = urlsplit(base)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password or
                parsed.query or parsed.fragment or not re.fullmatch(r'/[A-Za-z0-9_-]{32,128}/', parsed.path)):
            return None
        port = parsed.port
        if not port or parsed.netloc != '127.0.0.1:' + str(port):
            return None
        request = Request(base + 'api/ping', headers={'Host': parsed.netloc})
        with _NO_PROXY.open(request, timeout=1) as response:
            if response.status != 200 or response.geturl() != request.full_url:
                return None
            identity = json.loads(response.read(4097), object_pairs_hook=_unique)
        keys = ('schemaVersion', 'service', 'binding', 'profile', 'revision', 'instance', 'pid')
        if any(identity.get(key) != metadata.get(key) for key in keys):
            return None
        if type(identity.get('pid')) is not int or identity['pid'] <= 0:
            return None
        return {'baseUrl': base, 'dashboardUrl': base + 'index.html', 'refreshUrl': base + 'refresh.html',
                'pid': identity['pid']}
    except (OSError, ValueError, URLError, TypeError, AttributeError) as exc:
        if isinstance(exc, HTTPError):
            exc.close()
        return None


def ensure_server(package_root, settings_file, out, idle_timeout=3600):
    """Start/reuse a hidden bounded service, verified by active HTTP identity."""
    package_root, settings_file, out, saved = _profile(package_root, settings_file, out)
    if not isinstance(idle_timeout, (int, float)) or not 0.1 <= idle_timeout <= 86400:
        raise RefreshError('refresh_invalid_idle_timeout')
    python = _verified_python(package_root)
    verified_existing_report(out)
    metadata_path = _metadata_path(package_root, settings_file, out)
    expected = {'schemaVersion': 1, 'service': SERVICE, 'binding': _binding(package_root, settings_file, out),
                'profile': _digest(_json(saved)), 'revision': _package_identity(package_root)}
    with _locked(metadata_path.with_suffix('.startup.lock')):
        try:
            existing = _handshake(_read_json(metadata_path), expected)
        except (OSError, ValueError):
            existing = None
        if existing:
            return {**existing, 'reused': True}
        # CLI values come exclusively from the already validated local launcher.
        command = [*_python_command(python, package_root, 'refresh_server.py'),
                   '--package-root', str(package_root), '--config', str(settings_file), '--out', str(out),
                   '--idle-timeout', str(idle_timeout)]
        options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
                   'env': _environment(), 'cwd': str(package_root), 'shell': False, 'close_fds': True}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        else:
            options['start_new_session'] = True
        process = subprocess.Popen(command, **options)
        deadline = time.monotonic() + START_TIMEOUT
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RefreshError('refresh_unavailable')
                try:
                    metadata = _read_json(metadata_path)
                    found = _handshake(metadata, expected)
                except (OSError, ValueError):
                    found = None
                if found and found['pid'] == process.pid:
                    return {**found, 'reused': False}
                time.sleep(0.05)
            raise RefreshError('refresh_start_timeout')
        except BaseException:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--package-root', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--idle-timeout', type=float, default=3600)
    args = parser.parse_args(argv)
    try:
        package_root = _local_path(args.package_root)
        if package_root != Path(__file__).resolve().parents[1]:
            raise RefreshError('refresh_unavailable')
        _verified_python(package_root)
        server = create_server(package_root, args.config, args.out, idle_timeout=args.idle_timeout)
        serve(server, _metadata_path(server.state.package_root, server.state.settings_file, server.state.out))
        return 0
    except (OSError, ValueError, RuntimeError):
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
