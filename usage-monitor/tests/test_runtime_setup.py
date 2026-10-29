"""Synthetic bootstrap checks: no account, histories, downloads or compiler needed."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import setup_runtime as runtime
REAL_TIMEZONE_SETUP = runtime.ensure_timezone_data


class FakeResponse(io.BytesIO):
    def geturl(self):
        return "https://www.python.org/pinned.zip"


class RuntimeSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        timezone = patch.object(runtime, "ensure_timezone_data", return_value=None)
        timezone.start()
        self.addCleanup(timezone.stop)

    def package(self):
        package = self.root / "source with ünicode"
        (package / "native" / "src").mkdir(parents=True)
        (package / "assets").mkdir()
        (package / "native" / "Cargo.toml").write_text("[package]\nname='synthetic'\nversion='0.1.0'\n")
        (package / "native" / "Cargo.lock").write_text("# synthetic locked dependencies\n")
        (package / "native" / "src" / "main.rs").write_text('fn main() {}')
        (package / "assets" / "runtime-downloads.json").write_text("{}")
        return package

    def test_first_build_then_verified_reuse_has_no_network_or_tool_execution(self):
        package = self.package()
        install = self.root / "runtime ünicode"
        environment = dict(os.environ)
        prepared = {"cargo": str(install / "cargo"), "host": "synthetic", "environment": environment}
        calls = []

        def build(command, *, env, cwd):
            self.assertEqual(command[1:4], ["build", "--release", "--locked"])
            self.assertEqual(cwd, package / "native")
            calls.append(command)
            binary = install / "build" / "release" / ("codex-usage-offline.exe" if os.name == "nt" else "codex-usage-offline")
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(b"synthetic executable")

        with patch.object(runtime, "ensure_toolchain", return_value=prepared), patch.object(runtime, "run", side_effect=build):
            result = runtime.ensure_engine(package, install)
        self.assertEqual(result["status"], "created")
        self.assertEqual(len(calls), 1)
        with patch.object(runtime, "ensure_toolchain", side_effect=AssertionError("no compiler checks")), \
             patch.object(runtime, "download", side_effect=AssertionError("no downloads")), \
             patch.object(runtime.subprocess, "run", side_effect=AssertionError("no subprocess")), \
             patch.object(runtime.urllib.request, "build_opener", side_effect=AssertionError("no network")):
            result = runtime.ensure_engine(package, install)
        self.assertEqual(result["status"], "reused")
        self.assertEqual(Path(result["engine"]).read_bytes(), b"synthetic executable")
        stamp = json.loads((install / "engine.json").read_text())
        self.assertEqual(stamp["binary_sha256"], hashlib.sha256(b"synthetic executable").hexdigest())

    def test_modified_source_and_binary_are_not_reused(self):
        package = self.package()
        install = self.root / "runtime"
        engine = install / "bin" / ("codex-usage-offline.exe" if os.name == "nt" else "codex-usage-offline")
        engine.parent.mkdir(parents=True)
        engine.write_bytes(b"original")
        runtime.atomic_json(install / "engine.json", {"schema_version": runtime.SCHEMA,
            "source_sha256": runtime.source_hash(package), "binary_sha256": runtime.digest(engine)})
        (package / "native" / "src" / "main.rs").write_text("fn main(){println!(\"changed\");}")
        with patch.object(runtime, "ensure_toolchain", side_effect=RuntimeError("rebuild required")):
            with self.assertRaisesRegex(RuntimeError, "rebuild required"):
                runtime.ensure_engine(package, install)
        runtime.atomic_json(install / "engine.json", {"schema_version": runtime.SCHEMA,
            "source_sha256": runtime.source_hash(package), "binary_sha256": runtime.digest(engine)})
        engine.write_bytes(b"tampered")
        with patch.object(runtime, "ensure_toolchain", side_effect=RuntimeError("rebuild required")):
            with self.assertRaisesRegex(RuntimeError, "rebuild required"):
                runtime.ensure_engine(package, install)

    def test_failed_download_hash_preserves_previous_and_unexecuted_partial(self):
        directory = self.root / "downloads"
        directory.mkdir()
        original = directory / "pinned.zip"
        original.write_bytes(b"previous bytes")
        entry = {"url": "https://www.python.org/pinned.zip", "sha256": hashlib.sha256(b"expected").hexdigest()}
        class Opener:
            def open(self, *args, **kwargs):
                return FakeResponse(b"incorrect payload")
        with patch.object(runtime.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                runtime.download(entry, directory)
        self.assertEqual(original.read_bytes(), b"previous bytes")
        self.assertEqual(len(list(directory.glob("*.partial"))), 1)
        self.assertEqual(next(directory.glob("*.partial")).read_bytes(), b"incorrect payload")

    def test_verified_download_cache_is_used_without_network(self):
        directory = self.root / "downloads"
        directory.mkdir()
        cached = directory / "pinned.zip"
        cached.write_bytes(b"verified")
        entry = {"url": "https://www.python.org/pinned.zip", "sha256": runtime.digest(cached)}
        with patch.object(runtime.urllib.request, "build_opener", side_effect=AssertionError("network")):
            self.assertEqual(runtime.download(entry, directory), cached)

    def test_timezone_wheel_setup_then_no_network_reuse(self):
        package = self.package()
        wheel = self.root / "tzdata.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("tzdata/__init__.py", "# synthetic pure-data package")
            archive.writestr("tzdata/zoneinfo/UTC", b"synthetic tzif data")
        entry = {"url": "https://files.pythonhosted.org/tzdata.whl", "sha256": runtime.digest(wheel)}
        (package / "assets" / "runtime-downloads.json").write_text(json.dumps({"timezone_data": entry}))
        install = self.root / "runtime"
        from zoneinfo import ZoneInfoNotFoundError
        with patch("zoneinfo.ZoneInfo", side_effect=ZoneInfoNotFoundError("UTC")), \
             patch.object(runtime, "download", return_value=wheel) as transfer:
            result = REAL_TIMEZONE_SETUP(install, package)
        self.assertEqual(transfer.call_count, 1)
        self.assertEqual(result, str(install / "python-packages"))
        self.assertTrue((Path(result) / "tzdata/zoneinfo/UTC").is_file())
        with patch.object(runtime, "download", side_effect=AssertionError("network")), \
             patch("zoneinfo.ZoneInfo", side_effect=AssertionError("no timezone probing after install")):
            self.assertEqual(REAL_TIMEZONE_SETUP(install, package), result)

    def test_clang_without_cc_sets_explicit_native_compiler_and_linker(self):
        package = self.package()
        manifest = {"rust_version": "1.90.0", "platforms": {"linux-x86_64": {
            "rustup": {"host": "x86_64-unknown-linux-gnu"}, "compiler": {"kind": "zig"}}}}
        (package / "assets/runtime-downloads.json").write_text(json.dumps(manifest))
        install = self.root / "runtime"
        cargo = install / "cargo/bin/cargo"
        rustc = install / "rustup/toolchains/1.90.0-x86_64-unknown-linux-gnu/bin/rustc"
        for path in (cargo, rustc):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic executable")
        commands = {"clang": "/synthetic/clang", "clang++": "/synthetic/clang++", "llvm-ar": "/synthetic/llvm-ar"}
        with patch.object(runtime, "platform_key", return_value="linux-x86_64"), \
             patch.object(runtime.shutil, "which", side_effect=lambda name, **kwargs: commands.get(name)), \
             patch.object(runtime, "usable_c_compiler", return_value=True) as probe, \
             patch.object(runtime, "download", side_effect=AssertionError("no downloads")), \
             patch.object(runtime, "install_archive", side_effect=AssertionError("no installation")), \
             patch.object(runtime, "run", side_effect=AssertionError("no external execution")):
            environment = runtime.ensure_toolchain(package, install)["environment"]
        self.assertEqual(probe.call_args.args[0], "/synthetic/clang")
        self.assertEqual(environment["CC"], "/synthetic/clang")
        self.assertEqual(environment["CXX"], "/synthetic/clang++")
        self.assertEqual(environment["AR"], "/synthetic/llvm-ar")
        self.assertEqual(environment["CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER"], "/synthetic/clang")

    def test_clang_without_an_archiver_uses_verified_zig_wrappers(self):
        package = self.package()
        manifest = {"rust_version": "1.90.0", "platforms": {"linux-x86_64": {
            "rustup": {"host": "x86_64-unknown-linux-gnu"}, "compiler": {"kind": "zig"}}}}
        (package / "assets/runtime-downloads.json").write_text(json.dumps(manifest))
        install = self.root / "runtime"
        for path in (install / "cargo/bin/cargo", install / "rustup/toolchains/1.90.0-x86_64-unknown-linux-gnu/bin/rustc"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic executable")
        distribution = install / "compiler"
        zig = distribution / "zig with spaces ü/zig"
        zig.parent.mkdir(parents=True)
        zig.write_bytes(b"synthetic zig")
        with patch.object(runtime, "platform_key", return_value="linux-x86_64"), \
             patch.object(runtime.shutil, "which", side_effect=lambda name, **kwargs: "/synthetic/clang" if name == "clang" else None), \
             patch.object(runtime, "install_archive", return_value=distribution) as provision, \
             patch.object(runtime, "run", side_effect=AssertionError("no external execution")):
            environment = runtime.ensure_toolchain(package, install)["environment"]
        self.assertEqual(provision.call_count, 1)
        self.assertEqual(environment["CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER"], str(install / "compiler-wrappers/cc"))
        import shlex
        self.assertIn(shlex.quote(str(zig)) + ' cc "$@"', Path(environment["CC"]).read_text(encoding="utf-8"))
        self.assertTrue(Path(environment["AR"]).is_file())

    def test_present_but_broken_cc_stub_selects_zig_instead(self):
        package = self.package()
        manifest = {"rust_version": "1.90.0", "platforms": {"macos-aarch64": {
            "rustup": {"host": "aarch64-apple-darwin"}, "compiler": {"kind": "zig"}}}}
        (package / "assets/runtime-downloads.json").write_text(json.dumps(manifest))
        install = self.root / "runtime"
        for path in (install / "cargo/bin/cargo", install / "rustup/toolchains/1.90.0-aarch64-apple-darwin/bin/rustc"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic executable")
        distribution = install / "compiler"
        zig = distribution / "zig/zig"
        zig.parent.mkdir(parents=True)
        zig.write_bytes(b"synthetic zig")
        commands = {"cc": "/synthetic/xcode-cc-stub", "ar": "/synthetic/ar"}
        with patch.object(runtime, "platform_key", return_value="macos-aarch64"), \
             patch.object(runtime.shutil, "which", side_effect=lambda name, **kwargs: commands.get(name)), \
             patch.object(runtime, "usable_c_compiler", return_value=False) as probe, \
             patch.object(runtime, "install_archive", return_value=distribution) as provision, \
             patch.object(runtime, "run", side_effect=AssertionError("no external execution")):
            environment = runtime.ensure_toolchain(package, install)["environment"]
        self.assertEqual(probe.call_args.args[0], "/synthetic/xcode-cc-stub")
        self.assertEqual(provision.call_count, 1)
        self.assertEqual(environment["CARGO_TARGET_AARCH64_APPLE_DARWIN_LINKER"], str(install / "compiler-wrappers/cc"))

    def test_compiler_probe_requires_successful_linked_executable(self):
        install = self.root / "runtime"
        calls = []
        def result(command, **kwargs):
            calls.append(command)
            self.assertEqual(Path(command[1]).read_text(), "int main(void) { return 0; }\n")
            self.assertEqual(command[2], "-o")
            if len(calls) == 2:
                Path(command[3]).write_bytes(b"synthetic executable")
            return subprocess.CompletedProcess(command, 0 if len(calls) > 1 else 1, "", "missing SDK" if len(calls) == 1 else "")
        with patch.object(runtime.subprocess, "run", side_effect=result):
            self.assertFalse(runtime.usable_c_compiler("/synthetic/cc", {}, install))
            self.assertTrue(runtime.usable_c_compiler("/synthetic/cc", {}, install))
            # A nominally successful command that creates no executable is still
            # an unusable compiler, including wrappers/stubs which return zero.
            self.assertFalse(runtime.usable_c_compiler("/synthetic/cc", {}, install))

    def test_zip_traversal_symlink_and_alternate_stream_rejected_before_writing(self):
        for name in ("../escape", "C:/escape", "/escape", "normal/../../escape", "normal:stream"):
            with self.subTest(name=name):
                archive = self.root / "bad.zip"
                with zipfile.ZipFile(archive, "w") as package:
                    package.writestr(name, b"bad")
                with self.assertRaises(RuntimeError):
                    runtime.safe_unpack(archive, self.root / "unpack")
        archive = self.root / "link.zip"
        with zipfile.ZipFile(archive, "w") as package:
            link = zipfile.ZipInfo("link")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(link, "outside")
        with self.assertRaisesRegex(RuntimeError, "link"):
            runtime.safe_unpack(archive, self.root / "links")
        self.assertFalse((self.root / "links" / "link").exists())

    def test_tar_external_link_rejected_and_internal_link_materialized(self):
        archive = self.root / "python.tar.gz"
        with tarfile.open(archive, "w:gz") as package:
            item = tarfile.TarInfo("python/bin/python3.13")
            item.size = 6
            item.mode = 0o755
            package.addfile(item, io.BytesIO(b"python"))
            item = tarfile.TarInfo("python/bin/python3")
            item.type = tarfile.SYMTYPE
            item.linkname = "python3.13"
            package.addfile(item)
        runtime.safe_unpack(archive, self.root / "python")
        path = self.root / "python/python/bin/python3"
        self.assertFalse(path.is_symlink())
        self.assertEqual(path.read_bytes(), b"python")
        with tarfile.open(archive, "w:gz") as package:
            item = tarfile.TarInfo("escape")
            item.type = tarfile.SYMTYPE
            item.linkname = "../../outside"
            package.addfile(item)
        with self.assertRaises(RuntimeError):
            runtime.safe_unpack(archive, self.root / "bad-links")

    def test_setup_bridge_preserves_argument_values_and_exit_codes(self):
        package = self.root / "source ünicode"
        scripts = package / "scripts"
        scripts.mkdir(parents=True)
        helper = scripts / "setup_runtime.py"
        shutil.copyfile(Path(runtime.__file__), helper)
        expected = ["--config", str(self.root / "space ü/settings.json"), "--no-open", "--runtime-dir", str(self.root / "runtime ü")]
        (scripts / "refresh_usage.py").write_text("import json,sys;print(json.dumps(sys.argv[1:]));sys.exit(2)")
        result = subprocess.run([sys.executable, "-B", str(helper), *expected], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        # parse_known_args intentionally normalizes --runtime-dir to the end.
        self.assertEqual(json.loads(result.stdout), expected)

    @unittest.skipUnless(os.name == "nt", "Windows launcher integration")
    def test_windows_launcher_selects_available_python_and_preserves_unicode_args(self):
        package = self.root / "package with ünicode"
        scripts = package / "scripts"
        scripts.mkdir(parents=True)
        source = Path(runtime.__file__).parent
        shutil.copyfile(source / "bootstrap.ps1", scripts / "bootstrap.ps1")
        (scripts / "setup_runtime.py").write_text("import json,sys;print(json.dumps(sys.argv[1:]));sys.exit(2)")
        expected = ["--config", str(self.root / "space ü/settings.json"), "--no-open"]
        environment = dict(os.environ)
        environment.update(LOCALAPPDATA=str(self.root / "new profile/Local"), USERPROFILE=str(self.root / "new profile"))
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(scripts / "bootstrap.ps1"), *expected], env=environment, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)

    @unittest.skipUnless(os.name == "nt" and all(os.environ.get(name) for name in (
        "CODEX_USAGE_TEST_ENGINE", "CODEX_USAGE_TEST_PYTHON_ARCHIVE", "CODEX_USAGE_TEST_TZDATA_WHEEL")),
        "Needs explicit native engine and verified cached Python/tzdata archives; never downloads test dependencies")
    def test_no_system_python_custom_runtime_first_install_and_dashboard_config_reuse(self):
        import refresh_usage
        source = Path(runtime.__file__).resolve().parents[1]
        manifest = json.loads((source / "assets/runtime-downloads.json").read_text(encoding="utf-8"))
        python_archive = Path(os.environ["CODEX_USAGE_TEST_PYTHON_ARCHIVE"]).resolve()
        tzdata_archive = Path(os.environ["CODEX_USAGE_TEST_TZDATA_WHEEL"]).resolve()
        self.assertEqual(runtime.digest(python_archive), manifest["platforms"]["windows-x86_64"]["python"]["sha256"])
        self.assertEqual(runtime.digest(tzdata_archive), manifest["timezone_data"]["sha256"])
        archive = self.root / "source.zip"
        refresh_usage.export_package(archive)
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(self.root / "package with ünicode")
        package = self.root / "package with ünicode/codex-local-usage"
        config = self.root / "settings with ünicode/settings.json"
        install = self.root / "separate runtime ü"
        downloads = install / "downloads"
        downloads.mkdir(parents=True)
        for cached in (python_archive, tzdata_archive):
            shutil.copy2(cached, downloads / cached.name)
        home = self.root / "synthetic Codex home"
        sessions = home / "sessions"
        sessions.mkdir(parents=True)
        identity = "10000000-0000-4000-8000-000000000001"
        rows = [{"type": "session_meta", "timestamp": "2026-10-24T09:00:00Z", "payload": {"id": identity, "cwd": "/synthetic/project"}},
                {"type": "event_msg", "timestamp": "2026-10-24T10:00:00Z", "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 2, "reasoning_output_tokens": 1, "total_tokens": 12}}}}]
        (sessions / ("rollout-2026-10-24T09-00-00-" + identity + ".jsonl")).write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        environment = dict(os.environ)
        system = Path(os.environ.get("SYSTEMROOT", "C:/Windows"))
        environment.update(PATH=os.pathsep.join((str(system / "System32"), str(system / "System32/WindowsPowerShell/v1.0"))), LOCALAPPDATA=str(self.root / "profile/Local"),
                           APPDATA=str(self.root / "profile/Roaming"), USERPROFILE=str(self.root / "profile"),
                           HOME=str(self.root / "profile"), CODEX_HOME=str(home))

        def launch(command):
            shell = subprocess.list2cmdline([str(system / "System32/cmd.exe"), "/d", "/s", "/c"])
            shell += ' "' + subprocess.list2cmdline([str(item) for item in command]) + '"'
            result = subprocess.run(shell, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertIn(result.returncode, (0, 2), result.stderr + result.stdout)
            self.assertNotIn("downloading", result.stderr.lower())
            return json.loads(result.stdout)

        first = launch([package / "setup.cmd", "--runtime-dir", install, "--config", config,
                        "--codex-home", home, "--engine", Path(os.environ["CODEX_USAGE_TEST_ENGINE"]).resolve(),
                        "--timezone", "UTC", "--no-open"])
        dashboard = Path(first["dashboard"]).parent
        private_python = (install / "python/python.exe").resolve()
        self.assertTrue(private_python.is_file())
        self.assertEqual(first["offlineScan"]["filesParsed"], 1)
        fields = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningTokens", "totalTokens")
        def vector():
            records = json.loads((dashboard / "data.json").read_text(encoding="utf-8"))["browserBundle"]["records"]
            return tuple(sum(record[field] for record in records) for field in fields)
        self.assertEqual(vector(), (10, 3, 2, 1, 12))
        engine_before = runtime.digest(Path(os.environ["CODEX_USAGE_TEST_ENGINE"]))
        for directory in (dashboard, config.parent):
            self.assertEqual((directory / ".usage-python").read_text(encoding="utf-8").rstrip("\r\n"), str(private_python))
        monitored = [config, dashboard / ".usage-python", config.parent / ".usage-python", dashboard / ".usage-setup.json"]
        before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in monitored}
        # A second setup download has nowhere to find cached bytes and no working
        # network grant. Reuse must find the persisted private interpreter first.
        os.replace(downloads, install / "held-downloads")
        for command in ([dashboard / "refresh.cmd", "--no-open"],
                        [package / "refresh.cmd", "--config", config, "--no-open"]):
            result = launch(command)
            self.assertEqual(result["setup"]["status"], "reused")
            self.assertEqual(result["offlineScan"]["filesParsed"], 0)
            self.assertEqual(result["offlineScan"]["bytesRead"], 0)
            self.assertEqual(vector(), (10, 3, 2, 1, 12))
            self.assertEqual(runtime.digest(Path(os.environ["CODEX_USAGE_TEST_ENGINE"])), engine_before)
            self.assertEqual(result["modelCalls"], 0)
            self.assertFalse(result["networkUsed"])
            self.assertEqual({str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in monitored}, before)
        self.assertFalse(downloads.exists())
        self.assertFalse((self.root / "profile/Local/codex-local-usage/runtime").exists())


if __name__ == "__main__":
    unittest.main()
