"""Windows regression gate with the shipped engine and pinned release inputs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from common import Evidence, native_environment, pinned_input, test_temporary_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1] / 'usage-monitor')
    parser.add_argument('--artifacts-dir', type=Path, required=True)
    parser.add_argument('--python-archive', type=Path)
    parser.add_argument('--timezone-wheel', type=Path)
    args = parser.parse_args()
    evidence = Evidence(args.artifacts_dir)
    try:
        if os.name != 'nt':
            raise RuntimeError('The Windows gate requires an actual Windows host')
        source = args.source_root.resolve()
        if evidence.directory.is_relative_to(source):
            raise ValueError('Keep CI evidence outside the public source package')
        manifest = json.loads((source / 'assets/runtime-downloads.json').read_text(encoding='utf-8'))
        python = pinned_input(manifest['platforms']['windows-x86_64']['python'], evidence.directory / 'inputs', args.python_archive)
        timezone = pinned_input(manifest['timezone_data'], evidence.directory / 'inputs', args.timezone_wheel)
        engine = source / 'runtime/bin/codex-usage-offline.exe'
        evidence.artifact('engine', engine)
        evidence.artifact('pythonInput', python)
        evidence.artifact('timezoneInput', timezone)
        env = native_environment(engine, python, timezone)
        env.update(test_temporary_environment(evidence, source))
        evidence.value['sourceCommit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
        evidence.run('python-full-discovery', [sys.executable, '-B', '-X', 'utf8', Path(__file__).with_name('run_python_tests.py'),
                     '--source-root', source, '--output', evidence.directory / 'python-tests.json'], env=env, timeout=1800)
        node = shutil.which('node')
        if not node:
            raise RuntimeError('Node is required; no browser tests may be skipped')
        files = sorted((source / 'tests').glob('*.mjs'))
        if len(files) < 6:
            raise RuntimeError('Browser test discovery lost a required source test')
        evidence.value['browserProjectionTests'] = [path.name for path in files]
        for path in files:
            evidence.run(path.stem, [node, path], env=env)
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
