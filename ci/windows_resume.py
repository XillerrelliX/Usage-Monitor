"""Compose a verified prior Windows suite with one complete original-test retry."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import zipfile

from common import (Evidence, digest, native_environment, pinned_input, run_browser_projection_tests,
                    test_temporary_environment, write_json)
from run_original_test import TARGET
from public_fixture import git, stage_public_fixture


def load_baseline(archive, expected_sha, commit, evidence, run_id):
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha) or digest(archive) != expected_sha:
        raise ValueError('Prior validation archive SHA256 does not match the independently pinned artifact')
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Use the full prior source commit')
    with zipfile.ZipFile(archive) as zipped:
        if len(zipped.namelist()) != len(set(zipped.namelist())):
            raise ValueError('Prior evidence archive contains duplicate members')
        # Read only these literal data members. No archive path is extracted.
        payloads = {name: zipped.read(name) for name in
                    ('validation.json', 'python-tests.json', '01-python-full-discovery.log')}
    validation = json.loads(payloads['validation.json'])
    python = json.loads(payloads['python-tests.json'])
    if validation.get('sourceCommit') != commit or validation.get('status') != 'failed':
        raise ValueError('Prior source revision/failure identity does not match the pinned run')
    cases, identities = python.get('cases', []), python.get('discoveredIds', [])
    case_ids = [item.get('id') for item in cases]
    if not (python.get('status') == 'failed' and
            python.get('discovered') == python.get('executed') == 360 and python.get('passed') == 359 and
            len(identities) == len(set(identities)) == len(cases) == len(set(case_ids)) == 360 and
            set(identities) == set(case_ids) and not python.get('errors') and not python.get('skipped') and
            not python.get('unexpectedSkips') and len(python.get('failed', [])) == 1 and
            python['failed'][0].get('id') == TARGET and
            {item['id'] for item in cases if item.get('status') == 'failed'} == {TARGET} and
            sum(item.get('status') == 'passed' for item in cases) == 359):
        raise ValueError('Prior run is not the complete360/359passed/one-explicit-failed Windows suite')
    trace = python['failed'][0].get('traceback', '')
    if ('synthetic release source' not in trace or 'licenses' not in trace or 'cargo' not in trace or
            'Standalone runs provisioned a build dependency' not in trace):
        raise ValueError('The prior failure is not the independently diagnosed copied-notice fixture failure')
    source_run = {'sourceCommit': commit, 'evidenceZipSha256': expected_sha, 'runId': run_id,
                  'mode': 'prior-full-windows-suite'}
    for name, payload in payloads.items():
        (evidence.directory / ('prior-' + name)).write_bytes(payload)
    evidence.value.update(validationMode='composed-full-suite-and-focused-retry', priorSourceRun=source_run)
    evidence.save()
    return python, source_run


def stage_full_fixture(source, commit, destination, evidence):
    evidence.value['fixture'] = stage_public_fixture(source, commit, destination,
                                                    evidence.directory / 'fixture-source-map.json')
    evidence.save()
    return destination


def compose(prior, retry, source_run, evidence):
    if not (retry.get('status') == 'passed' and retry.get('target') == TARGET and
            retry.get('executedThisRun') == retry.get('passed') == 1 and not retry.get('failed') and
            not retry.get('errors') and not retry.get('skipped') and
            len(retry.get('discoveredIds', [])) == len(set(retry['discoveredIds'])) == 360 and
            set(retry['discoveredIds']) == set(prior['discoveredIds'])):
        raise ValueError('The exact original complete retry method/discovery did not pass')
    retry_run = {'sourceCommit': evidence.value['sourceCommit'], 'mode': 'current-focused-windows-retry',
                 'evidenceSha256': digest(evidence.directory / 'focused-retry.json'),
                 'fixtureMapSha256': digest(evidence.directory / 'fixture-source-map.json')}
    cases = []
    for item in prior['cases']:
        if item['id'] == TARGET:
            cases.append({'id': TARGET, 'status': 'passed', 'sourceRun': retry_run,
                          'initialStatus': 'failed', 'initialSourceRun': source_run})
        else:
            cases.append({**item, 'sourceRun': source_run})
    document = {'schemaVersion': 1, 'status': 'passed', 'coverageMode': 'composite',
        'discovered': 360, 'discoveredIds': prior['discoveredIds'], 'covered': 360, 'passed': 360,
        'executedThisRun': 1, 'executedInInitialRun': 360, 'cases': cases,
        'skipped': [], 'errors': [], 'failed': [], 'unexpectedSkips': [],
        'initialFailureRetained': prior['failed'], 'sourceRuns': [source_run, retry_run],
        'priorEvidenceSha256': digest(evidence.directory / 'prior-python-tests.json'),
        'retryEvidenceSha256': digest(evidence.directory / 'focused-retry.json'),
        'note': 'Initial full run had359passes and1fixture failure. Coverage combines those359passes with one complete original-method retry; no clean initial full-run pass is claimed.'}
    path = evidence.directory / 'python-tests.json'
    write_json(path, document)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1] / 'usage-monitor')
    parser.add_argument('--artifacts-dir', type=Path, required=True)
    parser.add_argument('--baseline-evidence-zip', type=Path, required=True)
    parser.add_argument('--baseline-sha256', required=True)
    parser.add_argument('--baseline-source-commit', required=True)
    parser.add_argument('--baseline-run-id')
    parser.add_argument('--python-archive', type=Path)
    parser.add_argument('--timezone-wheel', type=Path)
    args = parser.parse_args()
    evidence = Evidence(args.artifacts_dir)
    try:
        if os.name != 'nt':
            raise RuntimeError('The composed Windows retry requires an actual Windows host')
        source = args.source_root.resolve()
        if evidence.directory.is_relative_to(source):
            raise ValueError('Keep retry evidence outside the public source package')
        prior, source_run = load_baseline(args.baseline_evidence_zip, args.baseline_sha256,
            args.baseline_source_commit, evidence, args.baseline_run_id)
        evidence.value['sourceCommit'] = git('rev-parse', 'HEAD', cwd=source).decode().strip()
        temporary = test_temporary_environment(evidence, source)
        fixture = stage_full_fixture(source, args.baseline_source_commit, Path(temporary['TMPDIR']) / 's', evidence)
        manifest = json.loads((source / 'assets/runtime-downloads.json').read_text(encoding='utf-8'))
        python = pinned_input(manifest['platforms']['windows-x86_64']['python'], evidence.directory / 'inputs', args.python_archive)
        timezone = pinned_input(manifest['timezone_data'], evidence.directory / 'inputs', args.timezone_wheel)
        engine = source / 'runtime/bin/codex-usage-offline.exe'
        for name, path in (('engine', engine), ('pythonInput', python), ('timezoneInput', timezone)):
            evidence.artifact(name, path)
        env = native_environment(engine, python, timezone)
        env.update(temporary)
        evidence.run('complete-original-method-retry', [sys.executable, '-B', '-X', 'utf8',
            Path(__file__).with_name('run_original_test.py'), '--source-root', fixture,
            '--output', evidence.directory / 'focused-retry.json'], env=env, timeout=600)
        retry = json.loads((evidence.directory / 'focused-retry.json').read_text(encoding='utf-8'))
        combined = compose(prior, retry, source_run, evidence)
        node = shutil.which('node')
        if not node:
            raise RuntimeError('Node is required; no browser projection may be skipped')
        run_browser_projection_tests(evidence, source, node, combined, env=env)
        evidence.value['status'] = 'passed'
        return 0
    except BaseException as exc:
        evidence.value.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        evidence.save()
        evidence.bundle()


if __name__ == '__main__':
    raise SystemExit(main())
