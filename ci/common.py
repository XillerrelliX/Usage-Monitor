"""CI-only evidence helpers. Never inspect the runner's Codex profile."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import zipfile
from urllib.request import Request, urlopen


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temporary.replace(path)


class Evidence:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.value = {'schemaVersion': 1, 'status': 'running', 'steps': [], 'artifacts': {}}
        self.save()

    def save(self):
        write_json(self.directory / 'validation.json', self.value)

    def run(self, label, command, *, env=None, cwd=None, timeout=1800, accepted=(0,)):
        command = list(map(str, command))
        number = len(self.value['steps']) + 1
        log = self.directory / f'{number:02d}-{label}.log'
        item = {'name': label, 'command': command, 'log': log.name, 'status': 'running'}
        self.value['steps'].append(item)
        self.save()
        print(f'[{number}] {label}', flush=True)
        started = time.monotonic()
        try:
            # Write while running, so uploaded failure evidence includes partial output.
            with log.open('w', encoding='utf-8') as stream:
                completed = subprocess.run(command, env=env, cwd=cwd, stdout=stream,
                                           stderr=subprocess.STDOUT, timeout=timeout)
            item.update(exitCode=completed.returncode,
                        status='passed' if completed.returncode in accepted else 'failed')
            if completed.returncode not in accepted:
                raise RuntimeError(f'{label} failed ({completed.returncode}); see {log}')
        except BaseException as exc:
            item.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            raise
        finally:
            item['seconds'] = round(time.monotonic() - started, 3)
            self.save()
        return log.read_text(encoding='utf-8')

    def artifact(self, name, path):
        path = Path(path).resolve()
        self.value['artifacts'][name] = {'file': path.name, 'sha256': digest(path),
                                         'bytes': path.stat().st_size}
        self.save()

    def bundle(self):
        """Small downloadable evidence; omit compilers, caches, and extracted runtimes."""
        selected = set(self.directory.glob('*.log')) | set(self.directory.glob('*.json'))
        public = self.directory / 'public-package-tests'
        for pattern in ('public-invocations.json', 'macos-public-tests.json', 'offline-audit.json',
                        'browser-results.json', 'query-browser-states/*.js', '*.png', '*.csv'):
            selected.update(path for path in public.rglob(pattern)
                            if 'extracted package ü with spaces' not in path.relative_to(public).parts)
        destination = self.directory / 'validation-evidence.zip'
        with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(selected):
                archive.write(path, path.relative_to(self.directory).as_posix())
        publish = self.directory / 'publish'
        publish.mkdir(exist_ok=True)
        shutil_paths = [destination]
        package = self.value['artifacts'].get('publicPackage')
        if package:
            shutil_paths.append(self.directory / package['file'])
        import shutil
        for path in shutil_paths:
            shutil.copyfile(path, publish / path.name)
        return destination


def pinned_input(entry, directory, supplied=None):
    """Verify before reuse; downloads are setup-only and strictly HTTPS."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = Path(supplied).resolve() if supplied else directory / entry['url'].rsplit('/', 1)[-1].replace('%2B', '+')
    if not target.exists():
        if supplied:
            raise ValueError('Explicit pinned input is missing: ' + str(target))
        if not entry['url'].startswith('https://'):
            raise ValueError('Pinned inputs must use HTTPS')
        temporary = target.with_suffix(target.suffix + '.download')
        try:
            request = Request(entry['url'], headers={'User-Agent': 'Usage-Monitor-release-validation/1'})
            with urlopen(request, timeout=120) as response, temporary.open('wb') as stream:
                if not response.url.startswith('https://'):
                    raise ValueError('Insecure software redirect')
                while block := response.read(1024 * 1024):
                    stream.write(block)
            if digest(temporary) != entry['sha256']:
                raise ValueError('Pinned download SHA256 mismatch: ' + entry['url'])
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    if digest(target) != entry['sha256']:
        raise ValueError('Pinned input SHA256 mismatch: ' + str(target))
    return target


def native_environment(engine, python_archive, timezone_wheel):
    env = dict(os.environ)
    env.update(CODEX_USAGE_TEST_ENGINE=str(engine), USAGE_QUERY_TEST_ENGINE=str(engine),
               CODEX_USAGE_TEST_PYTHON_ARCHIVE=str(python_archive),
               CODEX_USAGE_TEST_TZDATA_WHEEL=str(timezone_wheel), PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    env['PYTHONPATH'] = str(timezone_wheel) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    return env
