"""Assemble or re-export a verified, entirely offline Windows x64 bundle."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

from prebuilt_runtime import (ENGINE_SHA256, NATIVE_SOURCE_SHA256, SOURCE_COMMIT,
                              digest, read_manifest, regular_files, resolve_prebuilt,
                              safe_path, verify_files)

ZIP_ROOT = 'Usage-Monitor'
LAUNCHER = 'Start-Usage-Monitor.cmd'
_CODE_DIRECTORIES = ('scripts', 'assets', 'native', 'references', 'tests', 'licenses', 'runtime')


def _product(root):
    value = json.loads(safe_path(root, 'assets/product.json').read_text(encoding='utf-8'))
    if (value.get('name') != 'Usage Monitor' or value.get('sourceCommit') != SOURCE_COMMIT or
            value.get('engineSha256') != ENGINE_SHA256 or not isinstance(value.get('version'), str) or
            not value['version']):
        raise ValueError('Product identity does not match the verified Usage Monitor engine.')
    return {key: value[key] for key in ('name', 'version', 'sourceCommit', 'engineSha256')}


def _source_files(root):
    from refresh_usage import package_files
    paths = {path.relative_to(root).as_posix() for path in package_files(root)}
    paths.update({LAUNCHER, '.gitattributes', 'README.md', 'LICENSE',
                  'assets/product.json', 'references/windows-package.md',
                  'scripts/windows_package.py', 'scripts/windows_start.ps1',
                  'scripts/standalone_app.py', 'scripts/prebuilt_runtime.py'})
    for name in paths:
        if not safe_path(root, name).is_file():
            raise ValueError('The public source package is incomplete: ' + name)
    return sorted(paths)


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def _archive(root, destination, files):
    destination = Path(destination).expanduser().resolve()
    if destination.suffix.lower() != '.zip':
        raise ValueError('Select a .zip file for the public Windows package.')
    if destination.is_relative_to(root):
        raise ValueError('Keep the exported ZIP outside its package directory.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.usage-monitor-', suffix='.zip', dir=destination.parent)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(files):
                path = safe_path(root, name)
                info = zipfile.ZipInfo(ZIP_ROOT + '/' + name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def verify_bundle(package_root):
    """Validate public bytes; private data/output directories are not exported."""
    root = Path(package_root).absolute()
    manifest = read_manifest(safe_path(root, 'bundle-manifest.json'))
    files = manifest.get('files')
    if (manifest.get('platform') != 'windows-x86_64' or
            manifest.get('privateUsageDataIncluded') is not False or
            manifest.get('product') != _product(root)):
        raise ValueError('Invalid Usage Monitor public bundle identity.')
    verify_files(root, files)
    source = set(_source_files(root))
    installed = resolve_prebuilt(root, root / 'runtime')
    runtime = read_manifest(root / 'runtime' / 'prebuilt-manifest.json')
    allowed = source | {'runtime/' + name for name in runtime['files']} | {'runtime/prebuilt-manifest.json'}
    notices = {name for name in files if name.startswith('licenses/')}
    if not notices:
        raise ValueError('The public bundle has no third-party license notices.')
    allowed |= notices
    if set(files) != allowed:
        raise ValueError('The public manifest contains missing or unsupported payload paths.')
    for directory in _CODE_DIRECTORIES:
        expected = {name for name in files if name.startswith(directory + '/')}
        actual = {directory + '/' + name for name in regular_files(root / directory)}
        if actual != expected:
            raise ValueError('Unexpected files in public payload directory: ' + directory)
    required = {LAUNCHER, 'scripts/windows_start.ps1', 'scripts/standalone_app.py',
                'runtime/python/python.exe', 'runtime/bin/codex-usage-offline.exe'}
    if not required <= set(files):
        raise ValueError('The portable Windows package is incomplete.')
    return {'manifest': manifest, 'engine': installed['engine']}


def reexport_bundle(package_root, destination):
    """Export only declared public source/runtime/notices, never local history."""
    root = Path(package_root).absolute()
    checked = verify_bundle(root)
    files = checked['manifest']['files']
    exported = _archive(root, destination, set(files) | {'bundle-manifest.json'})
    return {'export': str(exported), 'files': len(files) + 1,
            'sourceFiles': len(_source_files(root)), 'engineSha256': ENGINE_SHA256,
            'bundleManifestSha256': digest(root / 'bundle-manifest.json'),
            'privateUsageDataIncluded': False}


def build_windows_package(source_root, destination, *, python_archive, engine,
                          timezone_wheel, notices_directory):
    """Use local pinned inputs only. Never invoke a compiler or network tool."""
    from setup_runtime import safe_unpack, source_hash
    root = Path(source_root).absolute()
    product = _product(root)
    inputs = json.loads(safe_path(root, 'assets/runtime-downloads.json').read_text(encoding='utf-8'))
    python_entry = inputs['platforms']['windows-x86_64']['python']
    timezone_entry = inputs['timezone_data']
    python_archive, engine, timezone_wheel = map(Path, (python_archive, engine, timezone_wheel))
    # All artifacts are verified before any extraction or package publication.
    for path, expected in ((python_archive, python_entry['sha256']),
                           (timezone_wheel, timezone_entry['sha256']), (engine, ENGINE_SHA256)):
        if not path.is_file() or digest(path) != expected:
            raise ValueError('Pinned build input SHA256 mismatch: ' + path.name)
    if source_hash(root) != NATIVE_SOURCE_SHA256:
        raise ValueError('The native source differs from the tested prebuilt engine.')
    source_files = _source_files(root)
    notices = Path(notices_directory).absolute()
    notice_names = regular_files(notices)
    if not notice_names:
        raise ValueError('Third-party license notices are required before building the Windows ZIP.')
    destination = Path(destination).expanduser().resolve()
    if destination.suffix.lower() != '.zip':
        raise ValueError('Select a .zip file for the public Windows package.')
    if destination.is_relative_to(root) or destination.is_relative_to(notices):
        raise ValueError('Keep the exported ZIP outside source and license directories.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.usage-monitor-package-', dir=destination.parent) as temporary:
        package = Path(temporary) / ZIP_ROOT
        package.mkdir()
        for name in source_files:
            target = package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(safe_path(root, name), target)
        for name in sorted(notice_names):
            target = package / 'licenses' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(safe_path(notices, name), target)
        runtime = package / 'runtime'
        safe_unpack(python_archive, runtime / 'python')
        safe_unpack(timezone_wheel, runtime / 'python-packages')
        (runtime / 'bin').mkdir()
        shutil.copyfile(engine, runtime / 'bin' / 'codex-usage-offline.exe')
        (runtime / 'prebuilt-required').write_text('Usage Monitor Windows x64: use the verified shipped runtime.\n', encoding='utf-8')
        runtime_files = {name: digest(safe_path(runtime, name)) for name in sorted(regular_files(runtime))}
        _write_json(runtime / 'prebuilt-manifest.json', {
            'schemaVersion': 1, 'platform': 'windows-x86_64', 'sourceCommit': SOURCE_COMMIT,
            'nativeSourceHash': NATIVE_SOURCE_SHA256, 'engineSha256': ENGINE_SHA256,
            'pythonArchiveSha256': python_entry['sha256'], 'timezoneWheelSha256': timezone_entry['sha256'],
            'files': runtime_files})
        files = {name: digest(safe_path(package, name)) for name in sorted(regular_files(package))}
        _write_json(package / 'bundle-manifest.json', {
            'schemaVersion': 1, 'product': product, 'platform': 'windows-x86_64',
            'privateUsageDataIncluded': False, 'files': files})
        return reexport_bundle(package, destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--python-archive', type=Path, required=True)
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--timezone-wheel', type=Path, required=True)
    parser.add_argument('--notices-directory', type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(build_windows_package(args.source_root, args.destination,
        python_archive=args.python_archive, engine=args.engine, timezone_wheel=args.timezone_wheel,
        notices_directory=args.notices_directory)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
