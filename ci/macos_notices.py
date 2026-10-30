"""Build target-specific notices from the actual locked inputs, without downloads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tomllib

from common import digest, write_json


def is_notice(path):
    name = Path(path).name.lower()
    return name.startswith(('license', 'licence', 'copying', 'copyright', 'notice', 'unlicense'))


def assemble(source, destination, metadata_path, python_archive, python_notices_archive, engine, timezone_wheel, target):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('Use a new notices staging directory')
    destination.mkdir(parents=True)
    # Original shared component notices remain byte-identical. The Windows
    # inventory and GNU runtime are not claims about this Mac build.
    existing = source / 'licenses/licenses'
    for path in existing.rglob('*'):
        relative = path.relative_to(existing)
        if path.is_file() and not path.is_symlink() and relative.parts[0] not in ('gnu-runtime', 'cargo'):
            output = destination / 'licenses' / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, output)
    metadata = json.loads(Path(metadata_path).read_text(encoding='utf-8'))
    packages = {item['id']: item for item in metadata['packages']}
    nodes = {item['id']: item for item in metadata['resolve']['nodes']}
    root = metadata['resolve']['root']
    if root is None:
        raise ValueError('Cargo metadata must identify the native production root')
    selected, pending = set(), [root]
    while pending:
        identity = pending.pop()
        if identity in selected:
            continue
        selected.add(identity)
        pending.extend(item['pkg'] for item in nodes[identity]['deps'])
    lock = tomllib.loads((source / 'native/Cargo.lock').read_text(encoding='utf-8'))
    checksums = {(item['name'], item['version']): item.get('checksum') for item in lock['package']}
    components = []
    for identity in sorted(selected - {root}):
        package = packages[identity]
        name, version = package['name'], package['version']
        crate = Path(package['manifest_path']).parent
        licenses = [path for path in crate.rglob('*') if path.is_file() and not path.is_symlink() and is_notice(path)]
        if package.get('license_file'):
            explicit = (crate / package['license_file']).resolve()
            if not explicit.is_relative_to(crate.resolve()) or not explicit.is_file():
                raise ValueError('Cargo license_file is outside its verified crate: ' + name)
            licenses.append(explicit)
        if not licenses or not checksums.get((name, version)):
            raise ValueError('Missing locked crate provenance/license text: ' + name + '-' + version)
        notice_paths = []
        for path in sorted(set(licenses)):
            relative = Path('licenses/cargo') / (name + '-' + version) / path.relative_to(crate)
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, output)
            notice_paths.append(relative.as_posix())
        components.append({'name': name, 'version': version, 'license': package.get('license'),
                           'crateSha256': checksums[(name, version)], 'source': package.get('source'),
                           'notices': notice_paths})
    python_notices = []
    with tarfile.open(python_archive, 'r:*') as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if not member.isfile() or not (is_notice(relative) or 'licenses' in relative.parts):
                continue
            if relative.is_absolute() or '..' in relative.parts or '\\' in member.name:
                raise ValueError('Unsafe Python license archive member')
            output = destination / 'licenses/python-build-standalone' / Path(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as stream:
                output.write_bytes(stream.read())
            python_notices.append(output.relative_to(destination).as_posix())
    if not python_notices:
        raise ValueError('The pinned standalone Python archive contains no distributable license notices')
    full_notices = []
    build_metadata = None
    # Python 3.14 tarfile supports genuine Zstandard archives. Read members as
    # bytes, never extract arbitrary full-build paths into the filesystem.
    with tarfile.open(python_notices_archive, 'r:*') as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if not member.isfile() or not (is_notice(relative) or relative.name == 'PYTHON.json'):
                continue
            if relative.is_absolute() or '..' in relative.parts or '\\' in member.name:
                raise ValueError('Unsafe Python full-build notice archive member')
            with archive.extractfile(member) as stream:
                payload = stream.read()
            output = destination / 'licenses/python-standalone-build' / Path(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(payload)
            full_notices.append(output.relative_to(destination).as_posix())
            if relative.name == 'PYTHON.json':
                build_metadata = json.loads(payload)
    if not full_notices or build_metadata is None:
        raise ValueError('Full standalone build lacks original notices or PYTHON.json provenance')
    if build_metadata.get('target_triple') != target:
        raise ValueError('The Python full-build metadata has a different architecture')
    required_notices = set()
    def require_paths(value):
        if isinstance(value, dict):
            required_notices.update(value.get('license_paths', []))
            if value.get('license_path'):
                required_notices.add(value['license_path'])
            for item in value.values():
                require_paths(item)
        elif isinstance(value, list):
            for item in value:
                require_paths(item)
    require_paths(build_metadata)
    for relative in required_notices:
        path = destination / 'licenses/python-standalone-build/python' / relative
        if not path.is_file():
            raise ValueError('A standalone Python dependency notice is missing: ' + relative)
    text = ['Usage Monitor — macOS third-party notices', '',
            'This inventory describes the actual macOS build for ' + target + '.',
            'The shipped engine uses locked upstream Rust crates, embedded SQLite and Zstandard.',
            'The standalone Python archive supplies its original interpreter/dependency notices below.',
            'Original upstream, fonts, Rust, tzdata and CPython notice texts are preserved without edits.',
            'The companion manifest records exact input hashes and Cargo crate checksums.', '', 'Cargo components:']
    for item in sorted(components, key=lambda item: (item['name'], item['version'])):
        text.extend(['', item['name'] + ' ' + item['version'] + ' — ' + str(item['license']),
                     *('  ' + path for path in item['notices'])])
    text.extend(['', 'Standalone Python original notices:', *('  ' + path for path in python_notices),
                 '', 'Standalone Python build/dependency original notices and metadata:', *('  ' + path for path in full_notices)])
    (destination / 'THIRD-PARTY-NOTICES.txt').write_text('\n'.join(text) + '\n', encoding='utf-8')
    files = {path.relative_to(destination).as_posix(): digest(path) for path in sorted(destination.rglob('*')) if path.is_file()}
    document = {'schemaVersion': 1, 'platform': target, 'privateUsageDataIncluded': False,
                'engineSha256': digest(engine), 'pythonArchiveSha256': digest(python_archive),
                'pythonFullBuildArchiveSha256': digest(python_notices_archive),
                'timezoneWheelSha256': digest(timezone_wheel), 'cargoLockSha256': digest(source / 'native/Cargo.lock'),
                'cargoComponents': components, 'pythonArchiveNotices': python_notices,
                'pythonFullBuildNotices': full_notices, 'pythonBuildMetadata': build_metadata, 'files': files}
    write_json(destination / 'THIRD-PARTY-MANIFEST.json', document)
    return document


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('source-root', 'destination', 'cargo-metadata', 'python-archive', 'python-notices-archive', 'engine', 'timezone-wheel'):
        parser.add_argument('--' + flag, type=Path, required=True)
    parser.add_argument('--target', required=True)
    args = parser.parse_args()
    result = assemble(args.source_root, args.destination, args.cargo_metadata,
                      args.python_archive, args.python_notices_archive, args.engine, args.timezone_wheel, args.target)
    print(json.dumps({'cargoComponents': len(result['cargoComponents']), 'files': len(result['files']), 'platform': args.target}))
