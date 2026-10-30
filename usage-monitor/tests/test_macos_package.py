"""Synthetic, offline packaging checks on every host; real Macs run the CI gate."""
from __future__ import annotations

import hashlib
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'scripts'))
import macos_package as builder
import macos_runtime as prebuilt
import setup_runtime as runtime
from refresh_usage import package_files

PRIVATE = 'SYNTHETIC_' + 'PRIVATE_MAC_7d51ef'


def macho(platform):
    return struct.pack('<8I', 0xFEEDFACF, prebuilt.PLATFORMS[platform], 0, 2, 1, 8, 0, 0) + b'synthetic-not-executable'


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + '\n', encoding='utf-8')


def copy_source(source):
    names = {path.relative_to(PACKAGE).as_posix() for path in package_files(PACKAGE)}
    names.update({builder.LAUNCHER, builder.TEMPLATE})
    for name in names:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PACKAGE / name, target)


class MacPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix='usage-monitor-macos-synthetic-')
        cls.addClassCleanup(temporary.cleanup)
        cls.shared = Path(temporary.name)
        cls.source = cls.shared / 'public source ü'
        copy_source(cls.source)
        cls.notices = cls.shared / 'third-party notices'
        cls.notices.mkdir()
        (cls.notices / 'TEST-NOTICE.txt').write_text('Synthetic test-only license notice.\n', encoding='utf-8')
        cls.wheel = cls.shared / 'synthetic-tzdata.whl'
        with zipfile.ZipFile(cls.wheel, 'w') as archive:
            archive.writestr('tzdata/zoneinfo/UTC', b'Synthetic UTC fixture; never loaded')
        downloads = json.loads((cls.source / 'assets/runtime-downloads.json').read_text(encoding='utf-8'))
        downloads['timezone_data']['sha256'] = prebuilt.digest(cls.wheel)
        cls.engines, cls.pythons, cls.archives = {}, {}, {}
        for platform in prebuilt.PLATFORMS:
            cls.engines[platform] = cls.shared / (platform + '-engine')
            cls.engines[platform].write_bytes(macho(platform))
            cls.pythons[platform] = cls.shared / (platform + '-python.tar.gz')
            with tarfile.open(cls.pythons[platform], 'w:gz') as archive:
                entry = tarfile.TarInfo('python/bin/python3.14')
                payload = macho(platform)
                entry.size, entry.mode = len(payload), 0o755
                archive.addfile(entry, io.BytesIO(payload))
                link = tarfile.TarInfo('python/bin/python3')
                link.type, link.linkname = tarfile.SYMTYPE, 'python3.14'
                archive.addfile(link)
                library = tarfile.TarInfo('python/lib/python3.14/os.py')
                data = b'# synthetic standard library fixture\n'
                library.size, library.mode = len(data), 0o644
                archive.addfile(library, io.BytesIO(data))
            downloads['platforms'][platform]['python']['sha256'] = prebuilt.digest(cls.pythons[platform])
        write_json(cls.source / 'assets/runtime-downloads.json', downloads)
        cls.source_hash = runtime.source_hash(cls.source)
        for module in (builder, prebuilt):
            mocked = patch.object(module, 'NATIVE_SOURCE_SHA256', cls.source_hash)
            mocked.start()
            cls.addClassCleanup(mocked.stop)
        with (patch.object(runtime, 'download', side_effect=AssertionError('No network')),
              patch.object(runtime, 'ensure_toolchain', side_effect=AssertionError('No compiler')),
              patch.object(runtime.subprocess, 'run', side_effect=AssertionError('No executable input'))):
            for platform in prebuilt.PLATFORMS:
                destination = cls.shared / (platform + '.zip')
                builder.build_macos_package(cls.source, destination, platform=platform,
                    python_archive=cls.pythons[platform], engine=cls.engines[platform],
                    timezone_wheel=cls.wheel, notices_directory=cls.notices)
                cls.archives[platform] = destination

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='case-', dir=self.shared)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def extract(self, platform='macos-aarch64'):
        destination = self.root / (platform + ' package 漢字 with spaces')
        with zipfile.ZipFile(self.archives[platform]) as archive:
            archive.extractall(destination)
            for item in archive.infolist():
                (destination / item.filename).chmod(item.external_attr >> 16 & 0o777)
        return destination / builder.ZIP_ROOT

    def build(self, destination, *, source=None, platform='macos-aarch64', python=None, engine=None):
        return builder.build_macos_package(source or self.source, destination, platform=platform,
            python_archive=python or self.pythons.get(platform, self.pythons['macos-aarch64']),
            engine=engine or self.engines.get(platform, self.engines['macos-aarch64']),
            timezone_wheel=self.wheel, notices_directory=self.notices)

    def test_both_architectures_bind_their_engine_pinned_inputs_and_original_sources(self):
        hashes = set()
        for platform in prebuilt.PLATFORMS:
            with self.subTest(platform=platform):
                package = self.extract(platform)
                checked = builder.verify_bundle(package)
                manifest = checked['manifest']
                hashes.add(manifest['product']['engineSha256'])
                self.assertEqual(manifest['product']['engineSha256'], prebuilt.digest(self.engines[platform]))
                self.assertEqual(manifest['platform'], platform)
                self.assertFalse(manifest['privateUsageDataIncluded'])
                installed = runtime.ensure_engine(package, package / 'runtime')
                self.assertEqual(installed['status'], 'prebuilt')
                self.assertEqual(installed['python'], str(package / 'runtime' / prebuilt.PYTHON))
                for name in ('native/src/main.rs', 'scripts/dashboard_data.py', 'scripts/report_from_app.py'):
                    self.assertEqual((package / name).read_bytes(), (PACKAGE / name).read_bytes())
                self.assertEqual((package / 'runtime/python/python/bin/python3').read_bytes(), macho(platform))
                self.assertFalse((package / 'runtime/python/python/bin/python3').is_symlink())
        self.assertEqual(len(hashes), 2)
        self.assertNotIn(prebuilt.digest(PACKAGE / 'runtime/bin/codex-usage-offline.exe')
                         if (PACKAGE / 'runtime/bin/codex-usage-offline.exe').is_file() else 'unused', hashes)

    def test_zip_has_explicit_unix_modes_and_pinned_pre_python_inventory(self):
        for platform, archive_path in self.archives.items():
            with self.subTest(platform=platform), zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read(builder.ZIP_ROOT + '/bundle-manifest.json'))
                for item in archive.infolist():
                    name = item.filename.removeprefix(builder.ZIP_ROOT + '/')
                    self.assertEqual(item.create_system, 3)
                    expected = 0o755 if name in manifest['executableFiles'] else 0o644
                    self.assertEqual(item.external_attr >> 16 & 0o777, expected, name)
                launcher = archive.read(builder.ZIP_ROOT + '/' + builder.LAUNCHER)
                inventory = archive.read(builder.ZIP_ROOT + '/' + builder.INVENTORY)
                self.assertIn(hashlib.sha256(inventory).hexdigest().encode(), launcher)
                self.assertTrue(launcher.startswith(b'#!/bin/bash -p\n'))
                self.assertIn(b' -I -S -B -X utf8 ', launcher)
                self.assertNotIn(b'@@', launcher)
                self.assertEqual(inventory, builder._inventory(manifest['files']))

    def test_export_after_use_excludes_settings_history_and_reports(self):
        package = self.extract()
        for name in ('data/private.txt', 'dashboard/private.txt', 'auth.json', 'settings.json'):
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(PRIVATE, encoding='utf-8')
        destination = self.root / 'public re-export ü.zip'
        result = builder.reexport_bundle(package, destination)
        self.assertFalse(result['privateUsageDataIncluded'])
        with zipfile.ZipFile(self.archives['macos-aarch64']) as original, zipfile.ZipFile(destination) as exported:
            self.assertEqual(exported.namelist(), original.namelist())
            for name in exported.namelist():
                self.assertEqual(exported.read(name), original.read(name), name)
                self.assertNotIn(PRIVATE.encode(), exported.read(name))

    def test_damage_and_undeclared_code_fail_without_compiler_or_network_fallback(self):
        package = self.extract()
        with (patch.object(runtime, 'download', side_effect=AssertionError('No network')),
              patch.object(runtime, 'ensure_toolchain', side_effect=AssertionError('No compiler'))):
            for name in ('scripts/standalone_app.py', 'native/src/main.rs', 'runtime/' + prebuilt.ENGINE,
                         'runtime/' + prebuilt.PYTHON, builder.INVENTORY, builder.LAUNCHER):
                with self.subTest(name=name):
                    path = package / name
                    original = path.read_bytes()
                    path.write_bytes(original + b'\nCORRUPTED\n')
                    try:
                        with self.assertRaises(ValueError):
                            builder.verify_bundle(package)
                    finally:
                        path.write_bytes(original)
            extra = package / 'runtime/python/python/lib/python3.14/sitecustomize.py'
            extra.write_text('raise AssertionError("Must never execute")\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                builder.verify_bundle(package)
            with self.assertRaises(ValueError):
                runtime.ensure_engine(package, package / 'runtime')
            extra.unlink()
            held = self.root / 'held-runtime'
            (package / 'runtime').replace(held)
            with self.assertRaises((OSError, ValueError)):
                runtime.ensure_engine(package, package / 'runtime')

    def test_runtime_identity_rejects_wrong_platform_and_input_pins(self):
        package = self.extract()
        manifest_file = package / 'runtime/prebuilt-manifest.json'
        original = manifest_file.read_bytes()
        for field, value in (('platform', 'macos-x86_64'), ('engineSha256', '0' * 64),
                             ('nativeSourceHash', '0' * 64), ('pythonArchiveSha256', '0' * 64),
                             ('timezoneWheelSha256', '0' * 64)):
            with self.subTest(field=field):
                bad = json.loads(original)
                bad[field] = value
                write_json(manifest_file, bad)
                try:
                    with self.assertRaises(ValueError):
                        prebuilt.resolve_prebuilt(package, package / 'runtime')
                finally:
                    manifest_file.write_bytes(original)

    def test_incompatible_engine_or_unpinned_archive_never_publishes(self):
        for name, options in (
                ('wrong-architecture', {'engine': self.engines['macos-x86_64']}),
                ('wrong-python-pin', {'python': self.pythons['macos-x86_64']}),
                ('unsupported-platform', {'platform': 'windows-x86_64'})):
            with self.subTest(name=name):
                destination = self.root / (name + '.zip')
                with self.assertRaises(ValueError):
                    self.build(destination, **options)
                self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob('.usage-monitor-package-*')), [])

    def test_even_hash_approved_traversal_or_external_links_cannot_extract(self):
        for name, link in (('../escape.txt', None), ('python/bin/python3', '/tmp/external')):
            with self.subTest(name=name):
                bad = self.root / 'bad-python.tar.gz'
                with tarfile.open(bad, 'w:gz') as archive:
                    entry = tarfile.TarInfo(name)
                    if link:
                        entry.type, entry.linkname = tarfile.SYMTYPE, link
                        archive.addfile(entry)
                    else:
                        entry.size = 3
                        archive.addfile(entry, io.BytesIO(b'bad'))
                downloads_file = self.source / 'assets/runtime-downloads.json'
                original = downloads_file.read_bytes()
                downloads = json.loads(original)
                downloads['platforms']['macos-aarch64']['python']['sha256'] = prebuilt.digest(bad)
                write_json(downloads_file, downloads)
                try:
                    source_hash = runtime.source_hash(self.source)
                    with (patch.object(builder, 'NATIVE_SOURCE_SHA256', source_hash),
                          patch.object(prebuilt, 'NATIVE_SOURCE_SHA256', source_hash)):
                        with self.assertRaises((OSError, RuntimeError, ValueError)):
                            self.build(self.root / 'must-not-publish.zip', python=bad)
                    self.assertFalse((self.root / 'must-not-publish.zip').exists())
                    self.assertFalse((self.root / 'escape.txt').exists())
                finally:
                    downloads_file.write_bytes(original)

    def test_mac_public_entry_and_service_use_verified_private_python(self):
        import refresh_server
        import standalone_app
        package = self.extract()
        output = self.root / 'public-again.zip'
        with patch.object(sys, 'path', list(sys.path)):
            verified = refresh_server._verified_python(package)
            self.assertEqual(verified, package / 'runtime' / prebuilt.PYTHON)
            command = refresh_server._python_command(verified, package, 'refresh_server.py')
            self.assertEqual(command[1:4], ['-I', '-S', '-B'])
            self.assertIn("p+'/refresh_server.py'", command[command.index('-c') + 1])
            with patch.object(standalone_app, 'PACKAGE_ROOT', package), redirect_stdout(io.StringIO()):
                self.assertEqual(standalone_app.main(['--export', str(output)]), 0)
        self.assertTrue(output.is_file())

    def test_legacy_mac_wrapper_flags_keep_argument_validation_and_offline_pipeline(self):
        import refresh_usage
        import standalone_app
        package = self.extract()
        config = self.root / 'synthetic settings ü/settings.json'
        with (patch.object(standalone_app, 'PACKAGE_ROOT', package),
              patch.object(sys, 'path', list(sys.path)),
              patch.object(refresh_usage, 'main', return_value=0) as refresh):
            with patch.object(sys, 'platform', 'darwin'):
                self.assertEqual(standalone_app.main(['--setup', '--offline', '--config', str(config),
                                                      '--no-open', '--no-serve']), 0)
                forwarded = refresh.call_args.args[0]
                self.assertEqual(forwarded.count('--offline'), 1)
                self.assertEqual(forwarded[forwarded.index('--config') + 1], str(config.resolve()))
                self.assertNotIn('--setup', forwarded)
                refresh.reset_mock()
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as helped:
                    standalone_app.main(['--setup', '--offline', '--help'])
                self.assertEqual(helped.exception.code, 0)
                with patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as invalid:
                    standalone_app.main(['--config', '--offline'])
                self.assertEqual(invalid.exception.code, 2)
                refresh.assert_not_called()
            with patch.object(sys, 'platform', 'win32'):
                with patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as rejected:
                    standalone_app.main(['--setup', '--offline'])
                self.assertEqual(rejected.exception.code, 2)
                refresh.assert_not_called()
        self.assertFalse(config.exists())
        self.assertFalse((config.parent / 'dashboard').exists())


if __name__ == '__main__':
    unittest.main()
