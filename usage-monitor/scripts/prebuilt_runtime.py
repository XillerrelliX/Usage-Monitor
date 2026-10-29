"""Verify the shipped Windows runtime without installing or building anything."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

ENGINE_SHA256 = 'fd6b9ab7f014e5e180c7bab14ee079763c6db07a7cccc48b59ff0da2b12adb37'
NATIVE_SOURCE_SHA256 = '914f2747432d3395b22e66d4417da9969565d65932b0eb673f62dac4463f0684'
SOURCE_COMMIT = '6bd0eed7b108497aa50051580387345340e3167f'
_HASH = re.compile(r'^[0-9a-f]{64}$')
_RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def safe_path(root, relative):
    """Resolve a canonical manifest name, rejecting links and Windows aliases."""
    root = Path(root).absolute()
    if (not isinstance(relative, str) or not relative or '\\' in relative or
            relative.startswith('/') or any(ord(c) < 32 for c in relative)):
        raise ValueError('Invalid public payload path.')
    parts = relative.split('/')
    if any(not p or p in {'.', '..'} or ':' in p or p[-1:] in {' ', '.'} or
           _RESERVED.match(p) for p in parts):
        raise ValueError('Invalid public payload path: ' + relative)
    target = root.joinpath(*parts)
    # Check every existing ancestor, including the package root, before opening.
    for path in (root, *target.relative_to(root).parents):
        candidate = path if path.is_absolute() else root / path
        if candidate.exists():
            attributes = candidate.lstat()
            if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Reparse points are not allowed in the public payload.')
    if target.exists():
        attributes = target.lstat()
        if not stat.S_ISREG(attributes.st_mode) or getattr(attributes, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Public payload must contain regular files only: ' + relative)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('Public payload leaves its package directory.')
    return target


def read_manifest(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate public manifest field: ' + key)
            value[key] = item
        return value
    with Path(path).open('r', encoding='utf-8') as stream:
        value = json.load(stream, object_pairs_hook=unique)
    if not isinstance(value, dict) or type(value.get('schemaVersion')) is not int or value['schemaVersion'] != 1:
        raise ValueError('Unsupported public payload manifest.')
    return value


def verify_files(root, files):
    if not isinstance(files, dict) or not files:
        raise ValueError('The public payload manifest has no files.')
    names = set()
    for relative, expected in files.items():
        if not isinstance(expected, str) or not _HASH.fullmatch(expected):
            raise ValueError('Invalid public payload SHA256.')
        if relative.casefold() in names:
            raise ValueError('Duplicate Windows public payload filename.')
        names.add(relative.casefold())
        path = safe_path(root, relative)
        if not path.is_file() or digest(path) != expected:
            raise ValueError('Missing or damaged Usage Monitor payload: ' + relative + '. Extract the original ZIP again.')


def regular_files(root):
    """Enumerate without traversing any link or reparse directory."""
    root = Path(root).absolute()
    pending = [root]
    found = set()
    while pending:
        directory = pending.pop()
        attributes = directory.lstat()
        if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Reparse points are not allowed in the public payload.')
        for path in directory.iterdir():
            attributes = path.lstat()
            if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Reparse points are not allowed in the public payload.')
            if stat.S_ISDIR(attributes.st_mode):
                pending.append(path)
            elif stat.S_ISREG(attributes.st_mode):
                relative = path.relative_to(root).as_posix()
                safe_path(root, relative)
                found.add(relative)
            else:
                raise ValueError('Public payload contains a special file.')
    return found


def resolve_prebuilt(package_root, runtime_dir):
    """Fail closed on a missing/mismatched shipped runtime; never bootstrap."""
    from setup_runtime import source_hash
    package_root, runtime = Path(package_root).resolve(), Path(runtime_dir).absolute()
    manifest_path = safe_path(runtime, 'prebuilt-manifest.json')
    if not manifest_path.is_file():
        raise ValueError('Usage Monitor prebuilt runtime is missing. Extract the original Windows ZIP again; no compiler installation is needed.')
    manifest = read_manifest(manifest_path)
    if (manifest.get('platform') != 'windows-x86_64' or
            manifest.get('engineSha256') != ENGINE_SHA256 or
            manifest.get('nativeSourceHash') != NATIVE_SOURCE_SHA256 or
            manifest.get('sourceCommit') != SOURCE_COMMIT or
            source_hash(package_root) != NATIVE_SOURCE_SHA256):
        raise ValueError('The prebuilt engine does not match the verified native source.')
    files = manifest.get('files')
    verify_files(runtime, files)
    required = {'bin/codex-usage-offline.exe', 'python/python.exe', 'python/python314.zip',
                'python-packages/tzdata/zoneinfo/UTC', 'prebuilt-required'}
    if not required <= set(files) or files['bin/codex-usage-offline.exe'] != ENGINE_SHA256:
        raise ValueError('The prebuilt runtime manifest is incomplete.')
    if regular_files(runtime) != set(files) | {'prebuilt-manifest.json'}:
        raise ValueError('Unexpected files in the shipped runtime. Extract the original ZIP into a clean folder.')
    packages = runtime / 'python-packages'
    return {'engine': str(runtime / 'bin' / 'codex-usage-offline.exe'),
            'status': 'prebuilt', 'pythonPackages': str(packages),
            'environment': dict(os.environ)}
