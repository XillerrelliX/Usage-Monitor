"""Verify an architecture-specific shipped macOS runtime without provisioning."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import struct

from prebuilt_runtime import (NATIVE_SOURCE_SHA256, SOURCE_COMMIT, digest,
                              read_manifest, regular_files, safe_path, verify_files)

PLATFORMS = {'macos-aarch64': 0x0100000C, 'macos-x86_64': 0x01000007}
PYTHON = 'python/python/bin/python3'
ENGINE = 'bin/codex-usage-offline'
_HASH = re.compile(r'^[0-9a-f]{64}$')


def is_macos_bundle(root):
    """Identify a Mac release without changing legacy/source runtime selection."""
    path = Path(root) / 'assets/product.json'
    if not path.is_file():
        return False
    return json.loads(path.read_text(encoding='utf-8')).get('platform') in PLATFORMS


def verify_macho(path, platform):
    """Require a thin, 64-bit executable for the selected release architecture."""
    if platform not in PLATFORMS:
        raise ValueError('Select macos-aarch64 or macos-x86_64.')
    with Path(path).open('rb') as stream:
        header = stream.read(32)
    if len(header) != 32 or header[:4] != b'\xcf\xfa\xed\xfe':
        raise ValueError('The macOS runtime requires a thin 64-bit Mach-O executable.')
    _, cpu, _, kind, commands, size, _, _ = struct.unpack('<8I', header)
    if cpu != PLATFORMS[platform] or kind != 2 or not commands or not size:
        raise ValueError('The Mach-O executable does not match the selected macOS architecture.')


def product_identity(root):
    product = json.loads(safe_path(root, 'assets/product.json').read_text(encoding='utf-8'))
    if (product.get('name') != 'Usage Monitor' or product.get('sourceCommit') != SOURCE_COMMIT or
            product.get('platform') not in PLATFORMS or
            not isinstance(product.get('version'), str) or not product['version'] or
            not isinstance(product.get('engineSha256'), str) or
            not _HASH.fullmatch(product['engineSha256'])):
        raise ValueError('Invalid Usage Monitor macOS product identity.')
    return {key: product[key] for key in ('name', 'version', 'sourceCommit', 'engineSha256', 'platform')}


def resolve_prebuilt(package_root, runtime_dir):
    """Fail closed on corruption; a packaged launch never downloads or compiles."""
    from setup_runtime import source_hash
    package_root, runtime = Path(package_root).absolute(), Path(runtime_dir).absolute()
    manifest_path = safe_path(runtime, 'prebuilt-manifest.json')
    if not manifest_path.is_file():
        raise ValueError('Usage Monitor prebuilt runtime is missing. Extract the original macOS ZIP again.')
    manifest = read_manifest(manifest_path)
    product = product_identity(package_root)
    platform = product['platform']
    inputs = json.loads(safe_path(package_root, 'assets/runtime-downloads.json').read_text(encoding='utf-8'))
    if (manifest.get('platform') != platform or
            manifest.get('engineSha256') != product['engineSha256'] or
            manifest.get('nativeSourceHash') != NATIVE_SOURCE_SHA256 or
            manifest.get('sourceCommit') != SOURCE_COMMIT or
            manifest.get('pythonArchiveSha256') != inputs['platforms'][platform]['python']['sha256'] or
            manifest.get('timezoneWheelSha256') != inputs['timezone_data']['sha256'] or
            source_hash(package_root) != NATIVE_SOURCE_SHA256):
        raise ValueError('The prebuilt macOS engine does not match its verified source and runtime inputs.')
    files = manifest.get('files')
    verify_files(runtime, files)
    required = {ENGINE, PYTHON, 'python-packages/tzdata/zoneinfo/UTC', 'prebuilt-required'}
    if not required <= set(files) or files[ENGINE] != product['engineSha256']:
        raise ValueError('The prebuilt macOS runtime manifest is incomplete.')
    if regular_files(runtime) != set(files) | {'prebuilt-manifest.json'}:
        raise ValueError('Unexpected files in the shipped runtime. Extract the original ZIP into a clean folder.')
    executables = manifest.get('executableFiles')
    if (not isinstance(executables, list) or not all(isinstance(name, str) for name in executables) or
            len(executables) != len(set(executables)) or
            not {ENGINE, PYTHON} <= set(executables) or not set(executables) <= set(files)):
        raise ValueError('The macOS runtime executable inventory is incomplete.')
    if os.name != 'nt':
        for name in files:
            if bool(safe_path(runtime, name).stat().st_mode & 0o111) != (name in executables):
                raise ValueError('The macOS runtime executable permissions changed: ' + name)
    verify_macho(safe_path(runtime, ENGINE), platform)
    verify_macho(safe_path(runtime, PYTHON), platform)
    return {'engine': str(runtime / ENGINE), 'python': str(runtime / PYTHON),
            'status': 'prebuilt', 'platform': platform,
            'pythonPackages': str(runtime / 'python-packages'), 'environment': dict(os.environ)}
