"""Stage the pinned offline scanner's own cache before accounting validation."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager, closing
from pathlib import Path

SOURCE_COMMIT = '6bd0eed7b108497aa50051580387345340e3167f'
APPLICATION_ID = 0x43554F46


def local_home(value=None):
    home = Path(value or os.environ.get('CODEX_HOME') or Path.home()/'.codex').expanduser().resolve()
    # A network share is an explicit remote source, not this computer's files.
    if str(home).startswith('\\\\'):
        raise ValueError('Choose a local Codex home, not a network share.')
    if not home.is_dir():
        raise ValueError('The local Codex home does not exist: '+str(home))
    return home


def _canonical(value):
    value = str(value)
    if value.startswith('\\\\?\\'):
        value = value[4:]
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def _readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=5)
    db.execute('PRAGMA query_only=ON')
    return db


def ownership(path):
    """Recognize only this harness's databases, never adopt a desktop database."""
    with closing(_readonly(path)) as db:
        if db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
            raise ValueError('Refusing to update a database not owned by the offline refresher.')
        try:
            rows = db.execute('SELECT schema_version,source_commit,codex_home FROM offline_engine_meta').fetchall()
        except sqlite3.Error as exc:
            raise ValueError('Offline cache ownership metadata is missing or invalid.') from exc
        if len(rows) != 1 or rows[0][0] != 1 or rows[0][1] != SOURCE_COMMIT:
            raise ValueError('The offline cache belongs to an unsupported engine version.')
        scan = db.execute('SELECT id,scanned_at,timezone FROM scan_runs ORDER BY id DESC LIMIT 1').fetchone()
        return {'codexHome': rows[0][2], 'scan': scan}


@contextmanager
def _source_lock(database):
    """OS locks release on crashes; the persistent lock file is not a stale lock."""
    path = Path(str(database)+'.lock')
    with path.open('a+b') as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('Another local refresh is using this offline cache; retry when it finishes.') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class PreparedScan:
    def __init__(self, path, database, details):
        self.path, self.database, self.details = path, database, details
        self.published = False
        self.original = None

    def publish(self):
        if self.published:
            raise ValueError('This validated scan has already been published.')
        # The native process is closed. Collapse its SQLite WAL before moving one file.
        with closing(sqlite3.connect(self.path)) as db:
            busy, _, _ = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if busy:
                raise ValueError('The staged cache is still busy; the previous cache was preserved.')
            db.execute('PRAGMA journal_mode=DELETE')
        # Old WAL bytes must never be paired with a replaced main database.
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(self.database)+suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise ValueError('The previous offline cache is still open; close its reader and retry.')
        if self.database.is_file():
            descriptor, backup = tempfile.mkstemp(prefix='.offline-previous-', suffix='.sqlite', dir=self.database.parent)
            os.close(descriptor)
            self.original = Path(backup)
            shutil.copy2(self.database, self.original)
        os.replace(self.path, self.database)
        self.published = True

    def rollback(self):
        if self.published:
            if self.original is not None:
                os.replace(self.original, self.database)
                self.original = None
            else:
                self.database.unlink(missing_ok=True)
            self.published = False

    def finish(self):
        if self.original is not None:
            self.original.unlink(missing_ok=True)
            self.original = None


@contextmanager
def prepare_database(package_root, database, codex_home, timezone=None, runtime_dir=None, engine=None):
    """Yield unpublished data. Caller must run all accounting checks before publish()."""
    home = local_home(codex_home)
    database = Path(database).expanduser().resolve()
    if str(database).startswith('\\\\'):
        raise ValueError('The offline cache must be on a local filesystem.')
    # Never permit output to overwrite credentials, input sessions, or sidebar files.
    if database.is_relative_to(home):
        raise ValueError('Keep the owned offline cache outside the Codex source home.')
    database.parent.mkdir(parents=True, exist_ok=True)
    with _source_lock(database):
        previous = ownership(database) if database.exists() else None
        if engine is None:
            from setup_runtime import ensure_engine
            installed = ensure_engine(Path(package_root), Path(runtime_dir or database.parent/'runtime'))
            engine = installed['engine']
        engine = Path(engine).expanduser().resolve()
        if not engine.is_file():
            raise ValueError('The offline scanner is missing. Run the local setup launcher.')
        descriptor, temporary = tempfile.mkstemp(prefix='.offline-scan-', suffix='.sqlite', dir=database.parent)
        os.close(descriptor)
        staging = Path(temporary)
        staging.unlink()  # The native CLI creates and marks a new owned database itself.
        try:
            compatible = previous is not None and _canonical(previous['codexHome']) == _canonical(home)
            if timezone and previous and previous['scan'] and previous['scan'][2] != timezone:
                compatible = False
            if compatible:
                with closing(_readonly(database)) as source, closing(sqlite3.connect(staging)) as target:
                    source.backup(target)
            command = [str(engine), '--codex-home', str(home), '--database', str(staging)]
            if timezone:
                command.extend(['--timezone', timezone])
            # Only OS launch/timezone variables are needed. Do not pass account/API secrets.
            allowed = {'PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR',
                       'HOME', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'TZ', 'CODEX_USAGE_TIMEZONE'}
            environment = {key: value for key, value in os.environ.items() if key in allowed}
            process = subprocess.run(command, env=environment, capture_output=True,
                                     text=True, encoding='utf-8', timeout=1800)
            if process.returncode:
                raise ValueError('The offline scanner failed; the previous cache and dashboard were preserved '
                                 '(exit '+str(process.returncode)+').')
            try:
                result = json.loads(process.stdout)
                required = ('schemaVersion', 'sourceCommit', 'scanId', 'scannedAt', 'timezone',
                            'importedDays', 'filesScanned', 'filesParsed', 'bytesRead')
                details = {key: result[key] for key in required}
                if details['schemaVersion'] != 1 or details['sourceCommit'] != SOURCE_COMMIT:
                    raise ValueError('Unsupported offline scanner output.')
                if any(type(details[key]) is not int or details[key] < 0 for key in
                       ('scanId', 'importedDays', 'filesScanned', 'filesParsed', 'bytesRead')):
                    raise ValueError('Invalid offline scan metrics.')
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError('The offline scanner returned invalid completion evidence.') from exc
            completed = ownership(staging)
            scan = completed['scan']
            if (_canonical(completed['codexHome']) != _canonical(home) or scan is None or
                    tuple(scan) != (details['scanId'], details['scannedAt'], details['timezone']) or
                    (compatible and previous['scan'] and scan[0] <= previous['scan'][0])):
                raise ValueError('The offline scan did not commit matching completion evidence.')
            prepared = PreparedScan(staging, database, details)
            try:
                yield prepared
            except BaseException:
                try:
                    prepared.rollback()
                except OSError as exc:
                    raise OSError('Could not restore the previous cache. Recovery copy: '+str(prepared.original)) from exc
                prepared.finish()
                raise
            else:
                prepared.finish()
        except subprocess.TimeoutExpired as exc:
            raise ValueError('The offline scan timed out; the previous cache was preserved.') from exc
        finally:
            for suffix in ('', '-wal', '-shm', '-journal'):
                Path(str(staging)+suffix).unlink(missing_ok=True)
