"""Build and test the public bundle on an actual matching macOS architecture."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from common import (Evidence, native_environment, pinned_input, run_browser_projection_tests,
                    test_temporary_environment, write_json)


def rust_cases(output):
    return [{'id': identity, 'status': status.split(',')[0]} for identity, status in
            re.findall(r'^test (.+?) \.\.\. (ok|FAILED|ignored[^\r\n]*)$', output, re.M)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=('macos-aarch64', 'macos-x86_64'), required=True)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1] / 'usage-monitor')
    parser.add_argument('--artifacts-dir', type=Path, required=True)
    parser.add_argument('--python-archive', type=Path)
    parser.add_argument('--timezone-wheel', type=Path)
    parser.add_argument('--python-notices-archive', type=Path)
    parser.add_argument('--browser-node', type=Path)
    args = parser.parse_args()
    evidence = Evidence(args.artifacts_dir)
    try:
        source = args.source_root.resolve()
        if evidence.directory.is_relative_to(source):
            raise ValueError('Keep CI staging and evidence outside the public source package')
        machine = platform.machine()
        actual = {'arm64': 'macos-aarch64', 'aarch64': 'macos-aarch64', 'x86_64': 'macos-x86_64'}.get(machine)
        if platform.system() != 'Darwin' or actual != args.platform:
            raise RuntimeError(f'Requires actual {args.platform} macOS host; got {platform.system()} {machine}')
        # sysctl detects Rosetta, which must not be mistaken for native Intel testing.
        translated = subprocess.run(['/usr/sbin/sysctl', '-in', 'sysctl.proc_translated'], capture_output=True, text=True)
        if translated.returncode == 0 and translated.stdout.strip() == '1':
            raise RuntimeError('Rosetta execution is not an actual native-architecture test')
        evidence.value.update(platform=args.platform, machine=machine, macOS=platform.mac_ver()[0],
                              sourceCommit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                              sourceWorktreeDirty=bool(subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip()))
        manifest = json.loads((source / 'assets/runtime-downloads.json').read_text(encoding='utf-8'))
        # Resolve pinned URLs before an expensive compiler step; HTTP/hash failures are fatal.
        python = pinned_input(manifest['platforms'][args.platform]['python'], evidence.directory / 'inputs', args.python_archive)
        timezone = pinned_input(manifest['timezone_data'], evidence.directory / 'inputs', args.timezone_wheel)
        notices_manifest = json.loads(Path(__file__).with_name('macos-notice-inputs.json').read_text(encoding='utf-8'))
        python_notices_archive = pinned_input(notices_manifest['platforms'][args.platform], evidence.directory / 'inputs', args.python_notices_archive)
        evidence.artifact('pythonInput', python)
        evidence.artifact('timezoneInput', timezone)
        evidence.artifact('pythonNoticesInput', python_notices_archive)
        node = str(args.browser_node.resolve()) if args.browser_node else shutil.which('node')
        zstd = shutil.which('zstd')
        if not node or not zstd:
            raise RuntimeError('CI requires Node + Playwright Chromium/WebKit and genuine zstd before validation')
        evidence.run('node-version', [node, '--version'])
        evidence.run('zstd-version', [zstd, '--version'])
        target = evidence.directory / 'cargo-target'
        temporary_environment = test_temporary_environment(evidence, source)
        env = dict(os.environ, CARGO_TARGET_DIR=str(target), PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
        env.update(temporary_environment)
        native = source / 'native'
        cargo = ['cargo', '--locked', '--manifest-path', str(native / 'Cargo.toml')]
        evidence.run('native-source-hashes', [sys.executable, '-B', native / 'verify_sources.py'], env=env)
        evidence.run('rust-version', ['rustc', '--version'], env=env)
        evidence.run('native-production-build', ['cargo', 'build', '--release', *cargo[1:]], env=env)
        engine = target / 'release/codex-usage-offline'
        evidence.artifact('engine', engine)
        evidence.run('engine-architecture', ['/usr/bin/file', engine], env=env)
        listing = evidence.run('native-test-discovery', ['cargo', 'test', *cargo[1:], '--', '--list'], env=env)
        discovered = re.findall(r'^(.+): test$', listing, re.M)
        output = evidence.run('native-complete-tests', ['cargo', 'test', *cargo[1:], '--', '--test-threads=1'], env=env)
        cases = rust_cases(output)
        if len(discovered) < 100 or set(discovered) != {item['id'] for item in cases}:
            raise RuntimeError('Rust test execution did not cover the complete discovered test set')
        # This fixture has one real agent and one explicit turn, as the original ignored test requires.
        fixture = evidence.directory / 'compression' / 'rollout-2026-10-28-01977e3d-d9f6-72b7-93cf-f3f2f83c382c.jsonl'
        fixture.parent.mkdir()
        rows = [
            {'type': 'session_meta', 'payload': {'id': '01977e3d-d9f6-72b7-93cf-f3f2f83c382c', 'cwd': '/synthetic/project',
              'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'synthetic-parent', 'depth': 1, 'agent_path': '/root/child'}}}}},
            {'type': 'turn_context', 'payload': {'turn_id': 'fixture-turn', 'model': 'gpt-5', 'cwd': '/synthetic/project'}},
            {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'SYNTHETIC_COMPRESSION_FIXTURE', 'turn_id': 'fixture-turn'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {
                'input_tokens': 100, 'cached_input_tokens': 20, 'output_tokens': 40, 'reasoning_output_tokens': 7, 'total_tokens': 140}}}},
        ]
        fixture.write_text(''.join(json.dumps({'timestamp': '2026-10-28T10:00:00Z', **row}) + '\n' for row in rows), encoding='utf-8')
        compressed = fixture.with_suffix('.jsonl.zst')
        evidence.run('genuine-zstd-compress', [zstd, '-f', '-q', str(fixture), '-o', str(compressed)], env=env)
        env['CODEX_USAGE_ZSTD_FIXTURE'] = str(compressed)
        ignored_output = evidence.run('native-ignored-compression-test', ['cargo', 'test', *cargo[1:], '--', '--ignored', '--test-threads=1'], env=env)
        ignored = rust_cases(ignored_output)
        expected_ignored = {item['id'] for item in cases if item['status'] == 'ignored'}
        if not expected_ignored or expected_ignored != {item['id'] for item in ignored} or any(item['status'] != 'ok' for item in ignored):
            raise RuntimeError('The normally ignored native test was not executed successfully')
        write_json(evidence.directory / 'rust-tests.json', {'discoveredIds': discovered, 'cases': cases, 'ignoredExecution': ignored,
                   'discovered': len(discovered), 'passed': sum(item['status'] == 'ok' for item in cases) + len(ignored), 'failed': 0})
        generated = sorted({path.parent for path in target.rglob('scanner_query.rs') if path.parent.name == 'out'})
        if not generated:
            raise RuntimeError('Cargo generated source could not be found for byte verification')
        for index, directory in enumerate(generated):
            evidence.run(f'native-generated-hashes-{index}', [sys.executable, '-B', native / 'verify_sources.py', '--generated-dir', directory], env=env)
        env.update(native_environment(engine, python, timezone))
        env.update(temporary_environment)
        env['CARGO_TARGET_DIR'] = str(target)
        evidence.run('python-full-discovery', [sys.executable, '-B', '-X', 'utf8', Path(__file__).with_name('run_python_tests.py'),
                     '--source-root', source, '--output', evidence.directory / 'python-tests.json'], env=env)
        run_browser_projection_tests(evidence, source, node, evidence.directory / 'python-tests.json', env=env)
        archive = evidence.directory / ('Usage-Monitor-Mac-' + ('Apple-Silicon' if args.platform.endswith('aarch64') else 'Intel') + '.zip')
        host = {'macos-aarch64': 'aarch64-apple-darwin', 'macos-x86_64': 'x86_64-apple-darwin'}[args.platform]
        metadata = evidence.run('macos-cargo-license-metadata', ['cargo', 'metadata', *cargo[1:], '--format-version', '1',
                                '--filter-platform', host], env=env)
        # Cargo diagnostics are on stderr in the combined log. Keep JSON from its
        # single stdout document, without dropping the original complete log.
        metadata_value = next(json.loads(line) for line in metadata.splitlines() if line.startswith('{"packages":'))
        metadata_path = evidence.directory / 'macos-cargo-metadata.json'
        write_json(metadata_path, metadata_value)
        notices = evidence.directory / 'macos-notices'
        evidence.run('macos-target-notices', [sys.executable, '-B', Path(__file__).with_name('macos_notices.py'),
                     '--source-root', source, '--destination', notices, '--cargo-metadata', metadata_path,
                     '--python-archive', python, '--python-notices-archive', python_notices_archive,
                     '--engine', engine, '--timezone-wheel', timezone, '--target', host], env=env)
        evidence.run('build-public-macos-zip', [sys.executable, '-B', source / 'scripts/macos_package.py', '--source-root', source,
            '--destination', archive, '--platform', args.platform, '--python-archive', python, '--engine', engine,
            '--timezone-wheel', timezone, '--notices-directory', notices], env=env)
        evidence.artifact('publicPackage', archive)
        env.update(USAGE_MAC_SOURCE=str(source), USAGE_MAC_WORK=str(evidence.directory / 'public-package-tests'),
                   USAGE_MAC_ZIP=str(archive), USAGE_BROWSER_NODE=node)
        evidence.run('macos-public-package-behavior', [sys.executable, '-B', '-X', 'utf8', Path(__file__).with_name('macos_behavior.py')],
                     env=env, timeout=1800)
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
