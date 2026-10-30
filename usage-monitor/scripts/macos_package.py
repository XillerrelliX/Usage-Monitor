"""Assemble and re-export complete offline macOS Apple Silicon or Intel ZIPs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import posixpath
import shutil
import tarfile
import tempfile
import zipfile

from macos_runtime import (ENGINE, NATIVE_SOURCE_SHA256, PLATFORMS, PYTHON,
                           SOURCE_COMMIT, digest, product_identity, read_manifest,
                           regular_files, resolve_prebuilt, safe_path, verify_files,
                           verify_macho)

ZIP_ROOT = 'Usage-Monitor'
LAUNCHER = 'Start-Usage-Monitor.command'
INVENTORY = 'macos-payload.sha256'
TEMPLATE = 'scripts/macos_launch.command'
_CODE_DIRECTORIES = ('scripts', 'assets', 'native', 'references', 'tests', 'licenses', 'runtime')


def _source_files(root):
    from refresh_usage import package_files
    paths = {path.relative_to(root).as_posix() for path in package_files(root)}
    paths.update({LAUNCHER, TEMPLATE, '.gitattributes', 'README.md', 'LICENSE',
                  'assets/product.json', 'scripts/macos_package.py', 'scripts/macos_runtime.py',
                  'scripts/standalone_app.py', 'scripts/prebuilt_runtime.py'})
    for name in paths:
        if not safe_path(root, name).is_file():
            raise ValueError('The public source package is incomplete: ' + name)
    return sorted(paths)


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def _python_executables(archive):
    """Read pinned tar modes explicitly, including materialized internal links.

    Windows cannot represent Unix executable bits. Keeping the original modes
    here also lets cross-platform assembly and synthetic tests retain them.
    safe_unpack has already rejected traversal, link cycles and special files.
    """
    with tarfile.open(archive, 'r:*') as package:
        entries = {item.name: item for item in package.getmembers()}
        def executable(item):
            while item.issym() or item.islnk():
                target = (posixpath.join(posixpath.dirname(item.name), item.linkname)
                          if item.issym() else item.linkname)
                item = entries[posixpath.normpath(target)]
            return item.isfile() and bool(item.mode & 0o111)
        return {'python/' + name for name, item in entries.items() if not item.isdir() and executable(item)}


def _inventory(files):
    # The launcher pins this list before private Python runs. The launcher and
    # list are then covered by bundle-manifest.json, avoiding a checksum cycle.
    return ''.join(files[name] + '  ' + name + '\n' for name in sorted(files)
                   if name not in {LAUNCHER, INVENTORY, 'bundle-manifest.json'}).encode('utf-8')


def _launcher(root, platform, inventory):
    machine = {'macos-aarch64': 'arm64', 'macos-x86_64': 'x86_64'}[platform]
    template = safe_path(root, TEMPLATE).read_text(encoding='utf-8')
    for token, value in (('@@PLATFORM@@', platform), ('@@MACHINE@@', machine),
                         ('@@PAYLOAD_SHA256@@', hashlib.sha256(inventory).hexdigest())):
        if token not in template:
            raise ValueError('The macOS package launcher template is incomplete.')
        template = template.replace(token, value)
    return template.encode('utf-8')


def _archive(root, destination, files, executables):
    destination = Path(destination).expanduser().resolve()
    if destination.suffix.lower() != '.zip':
        raise ValueError('Select a .zip file for the public macOS package.')
    if destination.is_relative_to(root):
        raise ValueError('Keep the exported ZIP outside its package directory.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.usage-monitor-', suffix='.zip', dir=destination.parent)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(files):
                info = zipfile.ZipInfo(ZIP_ROOT + '/' + name)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100755 if name in executables else 0o100644) << 16
                archive.writestr(info, safe_path(root, name).read_bytes())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def verify_bundle(package_root):
    """Validate declared public bytes and executable modes, excluding local data."""
    root = Path(package_root).absolute()
    manifest = read_manifest(safe_path(root, 'bundle-manifest.json'))
    product = product_identity(root)
    files = manifest.get('files')
    if (manifest.get('platform') != product['platform'] or
            manifest.get('privateUsageDataIncluded') is not False or
            manifest.get('product') != product):
        raise ValueError('Invalid Usage Monitor macOS public bundle identity.')
    verify_files(root, files)
    source = set(_source_files(root))
    installed = resolve_prebuilt(root, root / 'runtime')
    runtime = read_manifest(root / 'runtime/prebuilt-manifest.json')
    notices = {name for name in files if name.startswith('licenses/')}
    allowed = (source | {'runtime/' + name for name in runtime['files']} | notices |
               {'runtime/prebuilt-manifest.json', INVENTORY})
    if not notices or set(files) != allowed:
        raise ValueError('The public macOS manifest contains missing or unsupported payload paths.')
    for directory in _CODE_DIRECTORIES:
        expected = {name for name in files if name.startswith(directory + '/')}
        actual = {directory + '/' + name for name in regular_files(root / directory)}
        if actual != expected:
            raise ValueError('Unexpected files in public payload directory: ' + directory)
    required = {LAUNCHER, TEMPLATE, 'scripts/standalone_app.py',
                'runtime/' + PYTHON, 'runtime/' + ENGINE, INVENTORY}
    if not required <= set(files):
        raise ValueError('The portable macOS package is incomplete.')
    inventory = _inventory(files)
    if (safe_path(root, INVENTORY).read_bytes() != inventory or
            safe_path(root, LAUNCHER).read_bytes() != _launcher(root, product['platform'], inventory)):
        raise ValueError('The macOS launcher does not match the public payload inventory.')
    executables = manifest.get('executableFiles')
    if (not isinstance(executables, list) or not all(isinstance(name, str) for name in executables) or
            len(executables) != len(set(executables)) or
            not {LAUNCHER, 'runtime/' + PYTHON, 'runtime/' + ENGINE} <= set(executables) or
            not set(executables) <= set(files) or
            {name.removeprefix('runtime/') for name in executables if name.startswith('runtime/')} !=
            set(runtime['executableFiles'])):
        raise ValueError('The public macOS executable inventory is incomplete.')
    if os.name != 'nt':
        for name in files:
            if bool(safe_path(root, name).stat().st_mode & 0o111) != (name in executables):
                raise ValueError('The public macOS executable permissions changed: ' + name)
    return {'manifest': manifest, 'engine': installed['engine'], 'python': installed['python']}


def reexport_bundle(package_root, destination):
    """Copy declared public files only; settings, caches and history stay local."""
    root = Path(package_root).absolute()
    checked = verify_bundle(root)
    manifest = checked['manifest']
    exported = _archive(root, destination, set(manifest['files']) | {'bundle-manifest.json'},
                        set(manifest['executableFiles']))
    return {'export': str(exported), 'files': len(manifest['files']) + 1,
            'sourceFiles': len(_source_files(root)), 'platform': manifest['platform'],
            'engineSha256': manifest['product']['engineSha256'],
            'bundleManifestSha256': digest(root / 'bundle-manifest.json'),
            'privateUsageDataIncluded': False}


def build_macos_package(source_root, destination, *, python_archive, engine,
                        timezone_wheel, notices_directory, platform=None):
    """Consume pinned local artifacts, without invoking compilers or the network."""
    from setup_runtime import platform_key, safe_unpack, source_hash
    platform = platform or platform_key()
    if platform not in PLATFORMS:
        raise ValueError('Select macos-aarch64 or macos-x86_64.')
    root = Path(source_root).absolute()
    product = json.loads(safe_path(root, 'assets/product.json').read_text(encoding='utf-8'))
    if (product.get('name') != 'Usage Monitor' or product.get('sourceCommit') != SOURCE_COMMIT or
            not isinstance(product.get('version'), str) or not product['version']):
        raise ValueError('Invalid Usage Monitor public product identity.')
    inputs = json.loads(safe_path(root, 'assets/runtime-downloads.json').read_text(encoding='utf-8'))
    python_entry, timezone_entry = inputs['platforms'][platform]['python'], inputs['timezone_data']
    python_archive, engine, timezone_wheel = map(Path, (python_archive, engine, timezone_wheel))
    for path, expected in ((python_archive, python_entry['sha256']), (timezone_wheel, timezone_entry['sha256'])):
        if not path.is_file() or digest(path) != expected:
            raise ValueError('Pinned build input SHA256 mismatch: ' + path.name)
    if source_hash(root) != NATIVE_SOURCE_SHA256:
        raise ValueError('The native source differs from the tested prebuilt engine.')
    verify_macho(engine, platform)
    engine_hash = digest(engine)
    source_files = _source_files(root)
    notices = Path(notices_directory).absolute()
    notice_names = regular_files(notices)
    if not notice_names:
        raise ValueError('Third-party license notices are required before building the macOS ZIP.')
    destination = Path(destination).expanduser().resolve()
    if destination.suffix.lower() != '.zip':
        raise ValueError('Select a .zip file for the public macOS package.')
    if destination.is_relative_to(root) or destination.is_relative_to(notices):
        raise ValueError('Keep the exported ZIP outside source and license directories.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.usage-monitor-package-', dir=destination.parent) as temporary:
        package = Path(temporary) / ZIP_ROOT
        package.mkdir()
        executables = set()
        for name in source_files:
            target = package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(safe_path(root, name), target)
            executable = target.suffix in {'.sh', '.command'}
            target.chmod(0o755 if executable else 0o644)
            if executable:
                executables.add(name)
        for name in sorted(notice_names):
            target = package / 'licenses' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(safe_path(notices, name), target)
            target.chmod(0o644)
        product.update(platform=platform, engineSha256=engine_hash)
        product.pop('windowsPackageRoot', None)
        product['macosPackageRoot'] = ZIP_ROOT
        _write_json(package / 'assets/product.json', product)
        runtime = package / 'runtime'
        safe_unpack(python_archive, runtime / 'python')
        safe_unpack(timezone_wheel, runtime / 'python-packages')
        (runtime / 'bin').mkdir()
        shutil.copyfile(engine, runtime / ENGINE)
        (runtime / ENGINE).chmod(0o755)
        verify_macho(safe_path(runtime, PYTHON), platform)
        (runtime / 'prebuilt-required').write_text('Usage Monitor macOS: use the verified shipped runtime.\n', encoding='utf-8')
        runtime_files = {name: digest(safe_path(runtime, name)) for name in sorted(regular_files(runtime))}
        runtime_executables = sorted(_python_executables(python_archive) | {ENGINE})
        if PYTHON not in runtime_executables:
            raise ValueError('The pinned Python archive has no executable private interpreter.')
        for name in runtime_files:
            safe_path(runtime, name).chmod(0o755 if name in runtime_executables else 0o644)
        executables.update('runtime/' + name for name in runtime_executables)
        _write_json(runtime / 'prebuilt-manifest.json', {
            'schemaVersion': 1, 'platform': platform, 'sourceCommit': SOURCE_COMMIT,
            'nativeSourceHash': NATIVE_SOURCE_SHA256, 'engineSha256': engine_hash,
            'pythonArchiveSha256': python_entry['sha256'], 'timezoneWheelSha256': timezone_entry['sha256'],
            'executableFiles': runtime_executables, 'files': runtime_files})
        files = {name: digest(safe_path(package, name)) for name in sorted(regular_files(package))}
        inventory = _inventory(files)
        (package / INVENTORY).write_bytes(inventory)
        (package / LAUNCHER).write_bytes(_launcher(package, platform, inventory))
        files.update({INVENTORY: digest(package / INVENTORY), LAUNCHER: digest(package / LAUNCHER)})
        _write_json(package / 'bundle-manifest.json', {
            'schemaVersion': 1, 'product': product_identity(package), 'platform': platform,
            'privateUsageDataIncluded': False, 'executableFiles': sorted(executables), 'files': files})
        return reexport_bundle(package, destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--python-archive', type=Path, required=True)
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--timezone-wheel', type=Path, required=True)
    parser.add_argument('--notices-directory', type=Path, required=True)
    parser.add_argument('--platform', choices=sorted(PLATFORMS))
    args = parser.parse_args(argv)
    print(json.dumps(build_macos_package(args.source_root, args.destination, platform=args.platform,
        python_archive=args.python_archive, engine=args.engine, timezone_wheel=args.timezone_wheel,
        notices_directory=args.notices_directory)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
