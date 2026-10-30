"""CI-only evidence helpers. Never inspect the runner's Codex profile."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
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


def test_temporary_environment(evidence, source):
    """Give fresh test processes canonical, short, owned temporary paths."""
    base = Path(os.environ.get('RUNNER_TEMP') or evidence.directory).resolve()
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='umt-', dir=base)).resolve()
    if directory.is_relative_to(Path(source).resolve()):
        raise ValueError('Keep synthetic temporary fixtures outside the public source package')
    environment = {name: str(directory) for name in ('TMPDIR', 'TEMP', 'TMP')}
    evidence.value['testTemporaryDirectory'] = {
        'path': str(directory), 'environment': environment,
        'reason': 'Canonical paths avoid macOS /var aliases and short Windows paths avoid fixture MAX_PATH failures.'}
    evidence.save()
    return environment


PARITY_CASES = (
    'test_multiselect_parity.MultiselectParityTests.test_exact_safe_integer_boundary_for_multiple_record_slices',
    'test_multiselect_parity.MultiselectParityTests.test_named_project_all_unions_chat_intersections_and_sparse_dates',
)


def run_browser_projection_tests(evidence, source, node, python_evidence, *, env):
    """Run standalone scripts; bind stdin-only parity to its actual Python producers."""
    source, python_evidence = Path(source), Path(python_evidence)
    node = Path(node)
    if not node.is_file():
        raise RuntimeError('Node is required; no browser tests may be skipped')
    files = sorted((source / 'tests').glob('*.mjs'))
    if len(files) < 6:
        raise RuntimeError('Browser test discovery lost a required source test')
    evidence.value['browserProjectionTests'] = [path.name for path in files]
    results = json.loads(python_evidence.read_text(encoding='utf-8'))
    if results.get('status') != 'passed':
        raise RuntimeError('Parity coverage requires successful complete/composite Python evidence')
    cases = results.get('cases', [])
    covered = []
    for identity in PARITY_CASES:
        matches = [item for item in cases if item.get('id') == identity]
        if len(matches) != 1 or matches[0].get('status') != 'passed':
            raise RuntimeError('Required original Python/Node parity case did not pass: ' + identity)
        covered.append({'id': identity, 'sourceRun': matches[0].get('sourceRun') or results.get('sourceRun') or
                        {'sourceCommit': evidence.value['sourceCommit'], 'mode': 'current-full-python-suite'}})
    for path in files:
        if path.name != 'test_multiselect_parity.mjs':
            evidence.run(path.stem, [node, path], env=env)
            continue
        item = {'name': path.stem, 'status': 'passed', 'exitCode': 0,
                'coverageMode': 'included-in-full-python-suite', 'coveredBy': covered,
                'scriptSha256': digest(path), 'producerSha256': digest(source / 'tests/test_multiselect_parity.py'),
                'pythonEvidenceSha256': digest(python_evidence), 'node': str(node)}
        write_json(evidence.directory / 'node-parity-coverage.json', item)
        evidence.value['steps'].append(item)
        evidence.save()
