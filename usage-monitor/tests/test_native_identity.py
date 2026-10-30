"""Keep the published native source identity stable on Windows and macOS."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'scripts'))
import prebuilt_runtime
import setup_runtime

PINNED_NATIVE_SOURCE_SHA256 = '914f2747432d3395b22e66d4417da9969565d65932b0eb673f62dac4463f0684'


class NativeSourceIdentityTests(unittest.TestCase):
    def test_real_public_source_keeps_the_published_native_identity(self):
        self.assertEqual(prebuilt_runtime.NATIVE_SOURCE_SHA256, PINNED_NATIVE_SOURCE_SHA256)
        self.assertEqual(setup_runtime.source_hash(PACKAGE), PINNED_NATIVE_SOURCE_SHA256)

    def test_component_order_case_and_original_path_bytes_are_preserved(self):
        # Component comparison places alpha/z.rs before alpha.rs. Sorting a
        # slash-joined string reverses that pair; a POSIX case-sensitive sort
        # also moves Cargo.lock ahead of the lowercase build.rs.
        ordered = ['native/alpha/z.rs', 'native/alpha.rs', 'native/alpha0.rs',
                   'native/build.rs', 'native/Cargo.lock', 'native/Cargo.toml',
                   'native/zed/config.json']
        sources = {name: ('Synthetic source: ' + name + '\n').encode('utf-8') for name in ordered}
        manifest = b'{"syntheticPinnedInput": true}\n'
        expected = hashlib.sha256()
        for name in ordered:
            expected.update(name.encode('utf-8') + b'\0')
            expected.update(hashlib.sha256(sources[name]).digest())
        expected.update(hashlib.sha256(manifest).digest())
        with tempfile.TemporaryDirectory(prefix='usage-native-identity-') as temporary:
            root = Path(temporary) / 'synthetic source ü with spaces'
            for name, data in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            inputs = root / 'assets/runtime-downloads.json'
            inputs.parent.mkdir()
            inputs.write_bytes(manifest)
            for name in ('native/target/ignored.rs', 'native/ignored.txt'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'Excluded synthetic build output or non-source file.\n')
            with patch.object(setup_runtime, 'digest', wraps=setup_runtime.digest) as hashed:
                self.assertEqual(setup_runtime.source_hash(root), expected.hexdigest())
            actual = [call.args[0].relative_to(root).as_posix() for call in hashed.call_args_list]
            self.assertEqual(actual, ordered + ['assets/runtime-downloads.json'])


if __name__ == '__main__':
    unittest.main()
