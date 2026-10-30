"""Byte-verified full public test inputs with one approved notice-tree omission."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess

from common import digest, write_json


def git(*arguments, cwd):
    return subprocess.check_output(['git', *arguments], cwd=cwd)


def stage_public_fixture(source, commit, destination, map_path):
    source, destination = Path(source).resolve(), Path(destination)
    repository = Path(git('rev-parse', '--show-toplevel', cwd=source).decode().strip()).resolve()
    prefix = source.relative_to(repository).as_posix()
    for arguments in (('diff', '--quiet', commit, 'HEAD', '--', prefix),
                      ('diff', '--quiet', 'HEAD', '--', prefix)):
        if subprocess.run(['git', *arguments], cwd=repository).returncode:
            raise ValueError('Application/native/test source differs from the committed fixture revision')
    if git('ls-files', '--others', '--exclude-standard', '--', prefix, cwd=repository).strip():
        raise ValueError('Untracked application files cannot enter the public fixture')
    tree = git('ls-tree', '-r', '-z', commit, '--', prefix, cwd=repository)
    tracked = {}
    for entry in tree.split(b'\0'):
        if not entry:
            continue
        attributes, raw_name = entry.split(b'\t', 1)
        mode, kind, blob = attributes.decode().split()
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError('Only ordinary tracked public files may enter the fixture')
        name = raw_name.decode('utf-8')
        tracked[name.removeprefix(prefix + '/')] = blob
    actual = {path.relative_to(source).as_posix() for path in source.rglob('*') if path.is_file()}
    if actual != set(tracked) or any(path.is_symlink() for path in source.rglob('*')):
        raise ValueError('The public source is not the complete byte-verifiable committed file set')
    original = {}
    for relative, blob in sorted(tracked.items()):
        payload = (source / relative).read_bytes()
        actual_blob = hashlib.sha1(b'blob ' + str(len(payload)).encode() + b'\0' + payload).hexdigest()
        if actual_blob != blob:
            raise ValueError('Current public file bytes differ from the committed fixture: ' + relative)
        original[relative] = {'sha256': hashlib.sha256(payload).hexdigest(), 'gitBlobSha1': blob}
    shutil.copytree(source, destination, ignore=lambda directory, names:
                    ['licenses'] if Path(directory).resolve() == source and 'licenses' in names else [])
    expected = {name: item for name, item in original.items() if not name.startswith('licenses/')}
    staged = {path.relative_to(destination).as_posix(): digest(path)
              for path in destination.rglob('*') if path.is_file()}
    if staged != {name: item['sha256'] for name, item in expected.items()}:
        raise ValueError('Fixture changed bytes beyond the one approved licenses/ omission')
    mapping = {'sourceCommit': commit, 'fixtureOmissions': ['licenses/'], 'originalFiles': original,
               'stagedFiles': staged, 'sourceDirectory': str(source), 'fixtureDirectory': str(destination)}
    write_json(map_path, mapping)
    return {'omissions': ['licenses/'], 'originalFiles': len(original), 'stagedFiles': len(staged),
            'mapSha256': digest(map_path), 'mapFile': str(map_path), 'sourceCommit': commit}
