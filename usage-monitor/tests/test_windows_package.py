"""Behavioral checks for the self-contained Windows release.

Only explicit, local release inputs enable the real executable tests. All input
sessions, profiles and reports are synthetic; no test locates an account's home.
"""
from __future__ import annotations

import hashlib
import ctypes
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "scripts"))
sys.path.insert(0, str(PACKAGE / "tests"))
import dashboard_data as ledger
import report_from_app as core
import setup_runtime as runtime
from test_offline_refresh import (CHILD, DAY_TOTALS, FORK, NESTED, NOW,
                                  OWNER_TOTALS, PRIVATE, PROJECT_TOTALS, ROOT,
                                  TOTAL, raw_fixture, event)

INPUTS = ("CODEX_USAGE_TEST_ENGINE", "CODEX_USAGE_TEST_PYTHON_ARCHIVE",
          "CODEX_USAGE_TEST_TZDATA_WHEEL")
ENGINE_SHA256 = "fd6b9ab7f014e5e180c7bab14ee079763c6db07a7cccc48b59ff0da2b12adb37"
ROOT_NAME = "Usage-Monitor"
START = "Start-Usage-Monitor.cmd"
EXPORT_PRIVATE = "PRIVATE_EXPORT_" + "SENTINEL_8b74bb41"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def records_vector(bundle):
    return tuple(sum(row[field] for row in bundle["records"]) for field in core.FIELDS)


def clean_environment(root, codex_home):
    """Expose Windows builtins and a wholly separate, empty synthetic profile."""
    system = Path(os.environ.get("SYSTEMROOT", "C:/Windows"))
    environment = {name: os.environ[name] for name in (
        "SYSTEMROOT", "SystemRoot", "WINDIR", "SYSTEMDRIVE",
        "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432", "NUMBER_OF_PROCESSORS")
                   if name in os.environ}
    temporary = root / "temporary"
    temporary.mkdir(parents=True, exist_ok=True)
    profile = root / "clean profile ü"
    profile.mkdir(parents=True, exist_ok=True)
    environment.update(
        PATH=os.pathsep.join((str(system / "System32"),
                             str(system / "System32/WindowsPowerShell/v1.0"))),
        COMSPEC=str(system / "System32/cmd.exe"), PATHEXT=".COM;.EXE;.BAT;.CMD",
        LOCALAPPDATA=str(profile / "Local"), APPDATA=str(profile / "Roaming"),
        USERPROFILE=str(profile), HOME=str(profile), TEMP=str(temporary),
        TMP=str(temporary), CODEX_HOME=str(codex_home), PYTHONUTF8="1")
    return environment


@unittest.skipUnless(os.name == "nt" and all(os.environ.get(name) for name in INPUTS),
                     "Requires explicit cached Windows engine, Python and tzdata; never downloads inputs")
class WindowsPackageBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="usage-monitor-package-tests-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.shared = Path(cls.temporary.name)
        cls.builder = importlib.import_module("windows_package")
        cls.engine = Path(os.environ[INPUTS[0]]).resolve()
        cls.python_archive = Path(os.environ[INPUTS[1]]).resolve()
        cls.timezone_wheel = Path(os.environ[INPUTS[2]]).resolve()
        assert digest(cls.engine) == ENGINE_SHA256, "Native executable differs from the reviewed release input"
        cls.notices = cls.shared / "synthetic notices"
        cls.notices.mkdir()
        (cls.notices / "TEST-NOTICE.txt").write_text(
            "Synthetic test-only notice. Production notices are reviewed separately.\n", encoding="utf-8")
        cls.archive = cls.shared / "Usage-Monitor-Windows-x64.zip"
        with patch.object(runtime, "download", side_effect=AssertionError("Release assembly must stay local")), \
             patch.object(runtime, "ensure_toolchain", side_effect=AssertionError("No compiler required")), \
             patch.object(runtime.urllib.request, "build_opener", side_effect=AssertionError("No HTTP")):
            cls.build_result = cls.builder.build_windows_package(
                PACKAGE, cls.archive, python_archive=cls.python_archive, engine=cls.engine,
                timezone_wheel=cls.timezone_wheel, notices_directory=cls.notices)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=self.shared, prefix="case-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home, self.sessions = raw_fixture(self.root)
        self.environment = clean_environment(self.root, self.home)
        self.service_pids = set()

    def extract(self, directory="extracted package ü with spaces"):
        target = self.root / directory
        with zipfile.ZipFile(self.archive) as archive:
            archive.extractall(target)
        package = target / ROOT_NAME
        self.assertTrue((package / START).is_file())
        return package

    def run_launcher(self, launcher, *arguments):
        system = Path(self.environment.get("SYSTEMROOT", "C:/Windows"))
        shell = subprocess.list2cmdline([str(system / "System32/cmd.exe"), "/d", "/s", "/c"])
        shell += ' "' + subprocess.list2cmdline([str(launcher), *map(str, arguments)]) + '"'
        return subprocess.run(shell, env=self.environment, cwd=self.root,
                              capture_output=True, text=True, encoding="utf-8", timeout=180)

    def launch(self, launcher, *arguments, success=True):
        result = self.run_launcher(launcher, *arguments)
        if success:
            self.assertIn(result.returncode, (0, 2), result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["modelCalls"], 0)
            self.assertFalse(payload["networkUsed"])
            pid = payload.get('localServer', {}).get('pid')
            if pid and pid not in self.service_pids:
                self.service_pids.add(pid)
                self.addCleanup(self.stop_local_service, pid)
            return payload
        self.assertNotEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotEqual(result.returncode, 2, result.stderr + result.stdout)
        return result

    @staticmethod
    def stop_local_service(pid):
        """Stop only a PID returned by this fixture's verified local launcher."""
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.OpenProcess(0x00100001, False, pid)
        if handle:
            try:
                kernel.TerminateProcess(handle, 0)
                kernel.WaitForSingleObject(handle, 15000)
            finally:
                kernel.CloseHandle(handle)

    def local_request(self, url, payload=None):
        self.assertEqual(urlsplit(url).hostname, '127.0.0.1')
        self.assertEqual(urlsplit(url).scheme, 'http')
        request = Request(url, data=None if payload is None else json.dumps(payload).encode('utf-8'))
        if payload is not None:
            request.add_header('Content-Type', 'application/json')
            request.add_header('X-Usage-Monitor', 'refresh')
            parsed = urlsplit(url)
            request.add_header('Origin', parsed.scheme+'://'+parsed.netloc)
        with build_opener(ProxyHandler({})).open(request, timeout=20) as response:
            return response.read()

    def assert_no_toolchains(self):
        forbidden = {"cargo", "rustup", "compiler", "compiler-probes", "compiler-wrappers", "build", "downloads"}
        found = [str(path.relative_to(self.root)) for path in self.root.rglob("*")
                 if path.is_dir() and path.name.lower() in forbidden]
        self.assertEqual(found, [], "Standalone runs provisioned a build dependency")
        profile = Path(self.environment["USERPROFILE"])
        for path in (profile / "Local/codex-local-usage", profile / "Roaming/codex-local-usage",
                     profile / ".cargo", profile / ".rustup"):
            self.assertFalse(path.exists(), str(path))

    def assert_accounting(self, dashboard):
        bundle = json.loads((dashboard / "data.json").read_text(encoding="utf-8"))["browserBundle"]
        self.assertEqual(records_vector(bundle), TOTAL)
        view = ledger.project_bundle(bundle, "2026-10-24", "2026-10-26", now=NOW)
        for dimension in ("daily", "owners", "chats", "models", "projects"):
            self.assertEqual(tuple(sum(row[field] for row in view[dimension]) for field in core.FIELDS),
                             TOTAL, dimension)
        vector = lambda row: tuple(row[field] for field in core.FIELDS)
        self.assertEqual({row["date"]: vector(row) for row in view["daily"]}, DAY_TOTALS)
        self.assertEqual({row["id"]: vector(row) for row in view["owners"]}, OWNER_TOTALS)
        self.assertEqual({row["id"]: vector(row) for row in view["projects"]}, PROJECT_TOTALS)
        family = ledger.project_bundle(bundle, "2026-10-24", "2026-10-26", now=NOW, chat=ROOT)
        self.assertEqual(vector(family["totals"]), (161, 76, 51, 14, 212))
        nested = next(row for row in view["owners"] if row["id"] == NESTED)
        self.assertEqual((nested["parentId"], nested["chatId"]), (CHILD, ROOT))
        fork = next(row for row in view["owners"] if row["id"] == FORK)
        self.assertEqual((fork["parentId"], fork["chatId"], fork["kind"]), (None, FORK, "self"))
        proof = json.loads((dashboard / "evidence.json").read_text(encoding="utf-8"))
        self.assertTrue(proof["mappingOwnerIdsMatch"])
        self.assertEqual(proof["reconciledFields"], list(core.FIELDS))
        self.assertTrue(proof["offlineScanner"]["nativeCachePublishedAfterValidation"])
        for name in ("data.json", "evidence.json", "index.html", "summary.txt", "usage-ledger.json"):
            self.assertNotIn(PRIVATE.encode(), (dashboard / name).read_bytes(), name)
        return bundle

    def test_release_is_complete_verified_and_preserves_the_pinned_parser(self):
        package = self.extract()
        self.assertFalse(self.build_result["privateUsageDataIncluded"])
        manifest = json.loads((package / "bundle-manifest.json").read_text(encoding="utf-8"))
        actual = {path.relative_to(package).as_posix(): digest(path)
                  for path in package.rglob("*") if path.is_file() and path.name != "bundle-manifest.json"}
        self.assertEqual(manifest["files"], actual)
        self.assertEqual(digest(package / "runtime/bin/codex-usage-offline.exe"), ENGINE_SHA256)
        upstream = json.loads((PACKAGE / "native/upstream-manifest.json").read_text(encoding="utf-8"))
        for relative, expected in upstream["files"].items():
            self.assertEqual(digest(package / "native" / relative), expected, relative)
            self.assertEqual((package / "native" / relative).read_bytes(),
                             (PACKAGE / "native" / relative).read_bytes(), relative)
        for relative in ("native/src/main.rs", "scripts/report_from_app.py", "scripts/dashboard_data.py"):
            self.assertEqual((package / relative).read_bytes(), (PACKAGE / relative).read_bytes(), relative)
        self.assertTrue((package / "runtime/python/python.exe").is_file())
        self.assertTrue((package / "runtime/python-packages/tzdata/zoneinfo/UTC").is_file())
        self.assert_no_toolchains()

    def test_help_needs_no_install_and_undeclared_python_code_is_never_executed(self):
        package = self.extract()
        result = self.run_launcher(package / START, "--help")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse((package / "data").exists())
        self.assertFalse((package / "dashboard").exists())
        marker = self.root / "UNDECLARED_PYTHON_EXECUTED"
        extra = package / "runtime/python/sitecustomize.py"
        extra.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n", encoding="utf-8")
        self.launch(package / START, "--no-open", success=False)
        self.assertFalse(marker.exists())
        self.assertFalse((package / "data").exists())
        self.assert_no_toolchains()

    def test_first_launch_repeat_dashboard_refresh_and_fresh_unicode_relocation(self):
        package = self.extract()
        first = self.launch(package / START, "--timezone", "UTC", "--no-open")
        dashboard = Path(first["dashboard"]).parent
        self.assertEqual(first["offlineScan"]["filesParsed"], 6)
        config = Path(first["setup"]["settingsFile"])
        self.assertEqual(config, Path(self.environment["LOCALAPPDATA"]) / "usage-monitor/settings.json")
        self.assertEqual(dashboard, config.parent / "dashboard")
        self.assertEqual(Path(first["setup"]["settingsFile"]), config)
        self.assertFalse(dashboard.is_relative_to(package))
        self.assertFalse(config.is_relative_to(self.home))
        self.assertFalse((package / "data").exists())
        self.assertFalse((package / "dashboard").exists())
        self.assert_accounting(dashboard)
        settings = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(Path(settings["offline"]["engine"]), package / "runtime/bin/codex-usage-offline.exe")
        self.assertEqual(Path(settings["offline"]["runtimeDirectory"]), package / "runtime")
        original_engine = digest(package / "runtime/bin/codex-usage-offline.exe")
        for launcher in (package / START, package / "refresh.cmd", dashboard / "refresh.cmd"):
            result = self.launch(launcher, "--no-open")
            self.assertEqual(result["offlineScan"]["filesParsed"], 0)
            self.assertEqual(result["offlineScan"]["bytesRead"], 0)
            self.assertEqual(Path(result["dashboard"]), dashboard / "index.html")
            self.assert_accounting(dashboard)
        relocated = self.extract("another fresh location 漢字 ü with spaces")
        self.environment = clean_environment(self.root / "second profile", self.home)
        result = self.launch(relocated / START, "--timezone", "UTC", "--no-open")
        relocated_dashboard = Path(result["dashboard"]).parent
        self.assertEqual(relocated_dashboard, Path(self.environment["LOCALAPPDATA"]) / "usage-monitor/dashboard")
        self.assert_accounting(relocated_dashboard)
        self.assertEqual(digest(package / "runtime/bin/codex-usage-offline.exe"), original_engine)
        self.assert_no_toolchains()

    def test_cloned_skill_inside_codex_home_keeps_all_state_outside_the_source_home(self):
        codex_home = Path(self.environment["USERPROFILE"]) / ".codex"
        self.home.replace(codex_home)
        self.home = codex_home
        self.environment["CODEX_HOME"] = str(codex_home)
        raw_before = {path.relative_to(codex_home): path.read_bytes()
                      for path in codex_home.rglob("*") if path.is_file()}
        extracted = self.extract("clone staging")
        package = codex_home / "skills/usage-monitor"
        package.parent.mkdir(parents=True)
        extracted.replace(package)
        first = self.launch(package / START, "--timezone", "UTC", "--no-open")
        config = Path(first["setup"]["settingsFile"])
        dashboard = Path(first["dashboard"]).parent
        self.assertFalse(config.is_relative_to(codex_home))
        self.assertFalse(dashboard.is_relative_to(codex_home))
        saved = json.loads(config.read_text(encoding="utf-8"))
        for path in (Path(saved["database"]), Path(first["cacheFile"])):
            self.assertFalse(path.is_relative_to(codex_home), str(path))
        self.assert_accounting(dashboard)
        repeated = self.launch(dashboard / "refresh.cmd", "--no-open")
        self.assertEqual(repeated["offlineScan"]["filesParsed"], 0)
        self.assert_accounting(dashboard)
        for relative, content in raw_before.items():
            self.assertEqual((codex_home / relative).read_bytes(), content, str(relative))
        self.assertFalse((package / "data").exists())
        self.assertFalse((package / "dashboard").exists())
        self.assert_no_toolchains()

    def test_explicit_config_is_isolated_and_its_dashboard_refresh_reuses_that_choice(self):
        package = self.extract()
        config = self.root / "isolated configuration ü/settings.json"
        first = self.launch(package / START, "--config", config, "--timezone", "UTC", "--no-open")
        dashboard = Path(first["dashboard"]).parent
        self.assertEqual(Path(first["setup"]["settingsFile"]), config)
        self.assertEqual(dashboard, config.parent / "dashboard")
        self.assertFalse((Path(self.environment["LOCALAPPDATA"]) / "usage-monitor").exists())
        self.assert_accounting(dashboard)
        repeated = self.launch(dashboard / "refresh.cmd", "--no-open")
        self.assertEqual(Path(repeated["setup"]["settingsFile"]), config)
        self.assertEqual(Path(repeated["dashboard"]).parent, dashboard)
        self.assertEqual(repeated["offlineScan"]["filesParsed"], 0)
        self.assert_accounting(dashboard)
        self.assert_no_toolchains()

    def test_public_selection_entry_and_saved_dashboard_refresh_keep_exact_filters(self):
        package = self.extract()
        first = self.launch(package / START, '--timezone', 'UTC', '--no-open',
                            '--from-date', '2026-10-24', '--to-date', '2026-10-26',
                            '--project-id', '__no_named_project__', '--chat-id', ROOT, '--chat-id', FORK)
        expected = {'startDate': '2026-10-24', 'endDate': '2026-10-26',
                    'projects': ['__no_named_project__'], 'chats': [ROOT, FORK]}
        self.assertEqual(first['initialSelection'], expected)
        dashboard = Path(first['dashboard']).parent
        self.assertEqual(json.loads((dashboard/'data.json').read_text(encoding='utf-8'))['initialSelection'], expected)
        self.assertEqual(parse_qs(urlsplit(first['dashboardUrl']).fragment), {
            'from': ['2026-10-24'], 'to': ['2026-10-26'], 'project': ['__no_named_project__'], 'chat': [ROOT, FORK]})
        ledger_path = Path(first['cacheFile'])
        first_ledger = json.loads(ledger_path.read_text(encoding='utf-8'))
        # Public dashboard refresh -> bootstrap -> verified Windows entry -> standalone -> refresh.
        repeated = self.launch(dashboard/'refresh.cmd', '--no-open')
        self.assertEqual(repeated['initialSelection'], expected)
        self.assertEqual(repeated['offlineScan']['filesParsed'], 0)
        self.assertEqual(repeated['offlineScan']['bytesRead'], 0)
        # The native scanner commits a new scan_runs row even without reparsing
        # sessions. The conservative source fingerprint correctly revalidates the
        # ledger; the saved selection must preserve its all-history numeric data.
        self.assertGreater(repeated['offlineScan']['scanId'], first['offlineScan']['scanId'])
        self.assertEqual(repeated['cache']['status'], 'miss')
        self.assertEqual(repeated['cache']['reason'], 'source_changed')
        self.assertNotEqual(repeated['cache']['sourceRevision'], first['cache']['sourceRevision'])
        self.assertTrue(repeated['cache']['historySaved'])
        self.assertEqual(repeated['cacheFile'], str(ledger_path))
        repeated_ledger = json.loads(ledger_path.read_text(encoding='utf-8'))
        for component in ('daily', 'records', 'owners', 'chats', 'appModels', 'appProjects', 'coverage', 'features'):
            self.assertEqual(repeated_ledger['bundle'][component], first_ledger['bundle'][component], component)
        self.assertNotIn('initialSelection', repeated_ledger['bundle'])
        self.assert_accounting(dashboard)
        for name in ('index.html', 'data.json', '.usage-selection.json'):
            self.assertIn(ROOT.encode(), (dashboard/name).read_bytes())
        changed = self.launch(dashboard/'refresh.cmd', '--no-open', '--from-date', '2026-10-25',
                              '--to-date', '2026-10-25', '--chat-id', ROOT)
        self.assertEqual(changed['initialSelection'], {
            'startDate': '2026-10-25', 'endDate': '2026-10-25', 'projects': [], 'chats': [ROOT]})
        before = {name: ((dashboard/name).read_bytes(), (dashboard/name).stat().st_mtime_ns)
                  for name in ('index.html', 'data.json', '.usage-selection.json', 'usage-ledger.json')}
        self.launch(dashboard/'refresh.cmd', '--no-open', '--chat-id', 'missing-chat', success=False)
        self.assertEqual(before, {name: ((dashboard/name).read_bytes(), (dashboard/name).stat().st_mtime_ns) for name in before})
        public = self.launch(package/START, '--no-open')
        self.assertIsNone(public['initialSelection'])
        self.assertNotIn('#', public['dashboardUrl'])
        self.assert_accounting(dashboard)
        self.assert_no_toolchains()

    def test_prebuilt_provider_rejects_corruption_without_any_build_or_network_fallback(self):
        package = self.extract()
        target = package / "runtime"
        original_paths = {path.relative_to(target).as_posix() for path in target.rglob("*")}
        engine = package / "runtime/bin/codex-usage-offline.exe"
        manifest_path = package / "runtime/prebuilt-manifest.json"
        source = package / "native/src/main.rs"
        with patch.object(runtime, "ensure_timezone_data", side_effect=AssertionError("No timezone setup")), \
             patch.object(runtime, "ensure_toolchain", side_effect=AssertionError("No compiler")), \
             patch.object(runtime, "download", side_effect=AssertionError("No downloads")), \
             patch.object(runtime.subprocess, "run", side_effect=AssertionError("No process execution")), \
             patch.object(runtime.urllib.request, "build_opener", side_effect=AssertionError("No HTTP")):
            result = runtime.ensure_engine(package, target)
            self.assertEqual(result["status"], "prebuilt")
            self.assertEqual(Path(result["engine"]), engine)
            original_manifest = manifest_path.read_bytes()
            original_engine = engine.read_bytes()
            original_source = source.read_bytes()
            variants = ((engine, original_engine + b"corrupted"),
                        (source, original_source + b"\n// changed source\n"))
            for path, bad in variants:
                with self.subTest(path=path.name):
                    original = path.read_bytes()
                    path.write_bytes(bad)
                    try:
                        with self.assertRaises((OSError, RuntimeError, ValueError)):
                            runtime.ensure_engine(package, target)
                    finally:
                        path.write_bytes(original)
            for field, value in (("platform", "linux-x86_64"), ("nativeSourceHash", "0" * 64)):
                with self.subTest(field=field):
                    bad = json.loads(original_manifest)
                    bad[field] = value
                    manifest_path.write_text(json.dumps(bad), encoding="utf-8")
                    try:
                        with self.assertRaises((OSError, RuntimeError, ValueError)):
                            runtime.ensure_engine(package, target)
                    finally:
                        manifest_path.write_bytes(original_manifest)
            held_manifest = self.root / "held-runtime-manifest.json"
            manifest_path.replace(held_manifest)
            try:
                with self.assertRaises((OSError, RuntimeError, ValueError)):
                    runtime.ensure_engine(package, target)
            finally:
                held_manifest.replace(manifest_path)
            held_runtime = self.root / "held-complete-runtime"
            target.replace(held_runtime)
            try:
                with self.assertRaises((OSError, RuntimeError, ValueError)):
                    runtime.ensure_engine(package, target)
            finally:
                held_runtime.replace(target)
        self.assertEqual({path.relative_to(target).as_posix() for path in target.rglob("*")}, original_paths)
        self.assert_no_toolchains()

    def test_launcher_checks_code_runtime_and_source_before_execution_or_report_changes(self):
        package = self.extract()
        # This fixture deliberately rewrites the interpreter bytes below.  The
        # live-service tests cover its separate long-lived interpreter process.
        first = self.launch(package / START, "--timezone", "UTC", "--no-open", "--no-serve")
        config = Path(first["setup"]["settingsFile"])
        dashboard = Path(first["dashboard"]).parent
        saved = json.loads(config.read_text(encoding="utf-8"))
        protected = [config, Path(saved["database"]),
                     *(dashboard / name for name in (
                         "index.html", "data.json", "evidence.json", "summary.txt", "usage-ledger.json"))]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected}
        marker = self.root / "UNVERIFIED_CODE_EXECUTED"
        script = package / "scripts/standalone_app.py"
        payload = ("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n").encode()
        for relative, replacement in (("scripts/standalone_app.py", payload),
                                      ("runtime/python/python.exe", None),
                                      ("runtime/bin/codex-usage-offline.exe", None),
                                      ("native/src/main.rs", None)):
            with self.subTest(relative=relative):
                path = package / relative
                original = path.read_bytes()
                path.write_bytes(original + b"\nCORRUPTED\n" if replacement is None else replacement)
                try:
                    self.launch(package / START, "--no-open", success=False)
                    if relative == "runtime/python/python.exe":
                        self.launch(dashboard / "refresh.cmd", "--no-open", success=False)
                    self.assertFalse(marker.exists(), "Unverified Python payload was executed")
                    for saved, expected in before.items():
                        self.assertEqual((saved.read_bytes(), saved.stat().st_mtime_ns), expected, str(saved))
                finally:
                    path.write_bytes(original)
        self.assert_no_toolchains()

    def test_used_bundle_export_excludes_every_local_setting_cache_and_report(self):
        package = self.extract()
        first = self.launch(package / START, "--timezone", "UTC", "--no-open")
        dashboard = Path(first["dashboard"]).parent
        config = Path(first["setup"]["settingsFile"])
        for path in (config.parent / "PrivateSentinel.txt", dashboard / "PrivateSentinel.txt"):
            path.write_text(EXPORT_PRIVATE, encoding="utf-8")
        for relative in ("data/PrivateSentinel.txt", "dashboard/PrivateSentinel.txt", "auth.json", "config.toml"):
            path = package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(EXPORT_PRIVATE, encoding="utf-8")
        exported = self.root / "share without history ü.zip"
        result = self.builder.reexport_bundle(package, exported)
        self.assertFalse(result["privateUsageDataIncluded"])
        cli_export = self.root / "launcher export without history ü.zip"
        cli_result = self.run_launcher(package / START, "--export", cli_export)
        self.assertEqual(cli_result.returncode, 0, cli_result.stderr + cli_result.stdout)
        self.assertFalse(json.loads(cli_result.stdout)["privateUsageDataIncluded"])
        for artifact in (exported, cli_export):
            with zipfile.ZipFile(self.archive) as original, zipfile.ZipFile(artifact) as fresh:
                self.assertEqual(set(fresh.namelist()), set(original.namelist()))
                for name in fresh.namelist():
                    data = fresh.read(name)
                    self.assertEqual(data, original.read(name), name)
                    self.assertNotIn(EXPORT_PRIVATE.encode(), data, name)
                    relative = name.removeprefix(ROOT_NAME + "/")
                    if not relative.startswith("tests/"):
                        self.assertNotIn(PRIVATE.encode(), data, name)
                    self.assertFalse(relative.startswith(("data/", "dashboard/", "sessions/", "archived_sessions/")), name)
        self.assert_accounting(dashboard)
        self.assert_no_toolchains()

    def test_private_embedded_python_runs_full_refresh_with_dns_http_and_compilers_denied(self):
        package = self.extract()
        audit_path = self.root / "audit.json"
        guard = r'''
import json, pathlib, subprocess, sys
package, audit_file, home = map(pathlib.Path, sys.argv[1:4])
events = {"network": [], "executables": []}
engine = (package / "runtime/bin/codex-usage-offline.exe").resolve()
def audit(event, args):
    if event.startswith("socket.") or event == "urllib.Request":
        events["network"].append(event)
        raise RuntimeError("Network forbidden by standalone integration test")
    if event == "subprocess.Popen":
        executable = pathlib.Path(args[0]).resolve() if args[0] else None
        # Windows passes executable=None and a quoted CreateProcess command line
        # when subprocess.run receives a normal argument list with shell=False.
        if executable is None and isinstance(args[1], str):
            if args[1].startswith(subprocess.list2cmdline([str(engine)]) + " "):
                executable = engine
        events["executables"].append(str(executable) if executable else "<unrecognized>")
        if executable != engine:
            raise RuntimeError("Only the bundled parser may run")
sys.addaudithook(audit)
sys.path.insert(0, str(package / "scripts"))
try:
    import standalone_app
    status = standalone_app.main(["--codex-home", str(home), "--timezone", "UTC", "--no-open", "--no-serve"])
finally:
    audit_file.write_text(json.dumps(events), encoding="utf-8")
raise SystemExit(status)
'''
        result = subprocess.run([str(package / "runtime/python/python.exe"), "-B", "-X", "utf8", "-c", guard,
                                 str(package), str(audit_path), str(self.home)], env=self.environment,
                                cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertIn(result.returncode, (0, 2), result.stderr + result.stdout)
        audited = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(audited["network"], [])
        self.assertEqual(audited["executables"], [str(package / "runtime/bin/codex-usage-offline.exe")])
        self.assert_accounting(Path(json.loads(result.stdout)["dashboard"]).parent)
        self.assert_no_toolchains()

    def test_serve_only_opens_verified_existing_dashboard_without_a_source_scan(self):
        package = self.extract()
        # The fresh embedded service must resolve a non-UTC app timezone even
        # when the existing report has the public default (no saved selection).
        first = self.launch(package / START, '--timezone', 'Europe/Berlin', '--no-open', '--no-serve')
        self.assertIsNone(first['initialSelection'])
        dashboard = Path(first['dashboard']).parent
        config = Path(first['setup']['settingsFile'])
        settings = json.loads(config.read_text(encoding='utf-8'))
        protected = [config, Path(settings['database']), *(dashboard/name for name in (
            'index.html', 'data.json', 'evidence.json', 'summary.txt', 'usage-ledger.json', '.usage-selection.json'))]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected}
        # A real refresh of this input would fail; serving must never inspect it.
        (self.home/'sessions/never-read-during-serve-only.jsonl.zst').write_bytes(b'broken compression')
        served = self.launch(package / START, '--serve-only', '--no-open')
        self.assertFalse(served['scanPerformed'])
        self.assertEqual(served['initialSelection'], first['initialSelection'])
        self.assertEqual(urlsplit(served['dashboardUrl']).scheme, 'http')
        self.assertEqual(served['fileDashboardUrl'], first['dashboardUrl'])
        self.assertEqual(parse_qs(urlsplit(served['dashboardUrl']).fragment),
                         parse_qs(urlsplit(first['dashboardUrl']).fragment))
        html = self.local_request(served['localServer']['dashboardUrl']).decode('utf-8')
        self.assertIn('"refreshUrl":', html)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected})
        selected = self.launch(package / START, '--serve-only', '--no-open',
                               '--from-date', '2026-10-24', '--to-date', '2026-10-26', '--chat-id', ROOT)
        expected = {'startDate': '2026-10-24', 'endDate': '2026-10-26', 'projects': [], 'chats': [ROOT]}
        self.assertFalse(selected['scanPerformed'])
        self.assertEqual(selected['initialSelection'], expected)
        self.assertEqual(parse_qs(urlsplit(selected['dashboardUrl']).fragment),
                         {'from': ['2026-10-24'], 'to': ['2026-10-26'], 'chat': [ROOT]})
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected})
        self.assert_no_toolchains()

    def test_live_dashboard_refresh_runs_real_parser_and_preserves_exact_selection(self):
        package = self.extract()
        first = self.launch(package / START, '--timezone', 'UTC', '--no-open',
                            '--from-date', '2026-10-24', '--to-date', '2026-10-26',
                            '--project-id', '__no_named_project__', '--chat-id', ROOT)
        dashboard = Path(first['dashboard']).parent
        self.assertEqual(urlsplit(first['dashboardUrl']).scheme, 'http')
        self.assertEqual(urlsplit(first['fileDashboardUrl']).scheme, 'file')
        before_bundle = self.assert_accounting(dashboard)
        selected_before = ledger.project_bundle(before_bundle, '2026-10-24', '2026-10-26',
                                                now=NOW, chat=ROOT)['totals']['totalTokens']
        with self.sessions['root'].open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event('2026-10-25', last=(5, 2, 1, 0, 6), clock='13:00:00'))+'\n')
        base = first['localServer']['baseUrl']
        started = json.loads(self.local_request(base+'api/refresh', first['initialSelection']))
        self.assertEqual(started['status'], 'running')
        self.assertTrue(started['statusUrl'].startswith(base+'api/jobs/'))
        deadline = time.monotonic() + 120
        result = started
        while result.get('status') == 'running' and time.monotonic() < deadline:
            result = json.loads(self.local_request(started['statusUrl']))
            if result['status'] == 'running':
                time.sleep(0.1)
        self.assertEqual(result.get('status'), 'complete', result)
        payload = json.loads((dashboard/'data.json').read_text(encoding='utf-8'))
        self.assertEqual(payload['initialSelection'], first['initialSelection'])
        bundle = payload['browserBundle']
        self.assertEqual(records_vector(bundle), tuple(a+b for a, b in zip(TOTAL, (5, 2, 1, 0, 6))))
        selected_after = ledger.project_bundle(bundle, '2026-10-24', '2026-10-26',
                                               now=NOW, chat=ROOT)['totals']['totalTokens']
        self.assertEqual(selected_after, selected_before+6)
        self.assertEqual(parse_qs(urlsplit(started['dashboardUrl']).fragment),
                         parse_qs(urlsplit(first['dashboardUrl']).fragment))
        served_html = self.local_request(first['localServer']['dashboardUrl']).decode('utf-8')
        served_payload = json.loads(served_html.split('<script id="usage-data" type="application/json">', 1)[1].split('</script>', 1)[0])
        self.assertEqual(records_vector(served_payload['browserBundle']), records_vector(bundle))
        self.assertEqual(served_payload['browserBundle']['generatedAt'], bundle['generatedAt'])
        self.assertEqual(served_payload['initialSelection'], first['initialSelection'])
        self.assertEqual(served_payload['refresh']['baseUrl'], base)
        for name in ('data.json', 'index.html', 'evidence.json', 'usage-ledger.json'):
            self.assertNotIn(PRIVATE.encode(), (dashboard/name).read_bytes())
        self.assert_no_toolchains()

    def test_query_watcher_records_completed_response_without_shared_scan(self):
        package = self.extract()
        identifier = '01977e3d-d9f6-72b7-93cf-f3f2f83c382c'
        session = self.home / 'sessions' / ('rollout-2026-10-28-' + identifier + '.jsonl')
        entries = [
            {'type': 'session_meta', 'payload': {'id': identifier}},
            {'type': 'turn_context', 'payload': {'turn_id': 'previous', 'model': 'gpt-5'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'previous'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 100, 'output_tokens': 5, 'total_tokens': 105}}}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'previous'}},
            {'type': 'turn_context', 'payload': {'turn_id': 'query-turn', 'model': 'gpt-5'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'query-turn'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 20, 'cached_input_tokens': 5, 'output_tokens': 4, 'reasoning_output_tokens': 2, 'total_tokens': 24}}}},
        ]
        def line(entry):
            return json.dumps({'timestamp': '2026-10-28T10:00:00Z', **entry}) + '\n'
        session.write_text(''.join(map(line, entries)), encoding='utf-8')
        dashboard = self.root / 'query dashboard ü'
        dashboard.mkdir()
        cache = dashboard / 'usage-ledger.json'
        cache.write_text('SYNTHETIC_UNCHANGED_SHARED_CACHE', encoding='utf-8')
        started = self.launch(package / START, '--query-meter', 'begin', '--watch',
                              '--thread-id', identifier, '--turn-id', 'query-turn',
                              '--codex-home', self.home, '--out', dashboard,
                              '--timeout', '30', '--interval', '0.1')
        self.assertIn(started['status'], {'pending', 'provisional'})
        with session.open('a', encoding='utf-8') as stream:
            stream.write(line({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                         'content': [{'type': 'output_text', 'text': EXPORT_PRIVATE}]}}))
            stream.write(line({'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {
                'input_tokens': 30, 'cached_input_tokens': 7, 'output_tokens': 6,
                'reasoning_output_tokens': 3, 'total_tokens': 36}}}}))
            stream.write(line({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'query-turn'}}))
        latest = dashboard / 'query-usage/latest.json'
        deadline = time.monotonic() + 30
        result = {}
        while time.monotonic() < deadline:
            if latest.is_file():
                result = json.loads(latest.read_text(encoding='utf-8'))
                if result.get('final'):
                    break
            time.sleep(0.1)
        self.assertEqual(result.get('status'), 'complete', result)
        self.assertTrue(result['final'])
        self.assertEqual(result['counters'], {'inputTokens': 50, 'cachedInputTokens': 12,
                         'outputTokens': 10, 'reasoningOutputTokens': 5, 'totalTokens': 60})
        self.assertEqual(result['counterText']['totalTokens'], '60')
        self.assertEqual(result['coverage'], 'turn-start')
        self.assertFalse((dashboard / 'index.html').exists(), 'Query meter must not launch an ordinary refresh')
        self.assertEqual(cache.read_text(encoding='utf-8'), 'SYNTHETIC_UNCHANGED_SHARED_CACHE')
        self.assertTrue((dashboard / 'query-usage.html').is_file())
        for path in dashboard.rglob('*'):
            if path.is_file():
                self.assertNotIn(EXPORT_PRIVATE.encode(), path.read_bytes())
        self.assert_no_toolchains()

    def _batch_saved_profile(self):
        config = Path(self.environment['LOCALAPPDATA']) / 'usage-monitor/settings.json'
        saved = json.loads(config.read_text(encoding='utf-8'))
        for metadata in (config.parent / '.usage-refresh').glob('*.json'):
            pid = json.loads(metadata.read_text(encoding='utf-8')).get('pid')
            if pid and pid not in self.service_pids:
                self.service_pids.add(pid)
                self.addCleanup(self.stop_local_service, pid)
        return config, saved, Path(saved['outputDirectory'])

    def test_codex_batch_public_entry_one_scan_exact_report_and_late_query_completion(self):
        package = self.extract()
        identifier = '01977e3d-d9f6-72b7-93cf-f3f2f83c382c'
        self.environment['CODEX_THREAD_ID'] = identifier
        session = self.home / 'sessions' / ('rollout-2026-10-28-' + identifier + '.jsonl')
        entries = [
            {'type': 'session_meta', 'payload': {'id': identifier}},
            {'type': 'turn_context', 'payload': {'turn_id': 'previous', 'model': 'gpt-5'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'previous'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 100, 'output_tokens': 5, 'total_tokens': 105}}}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'previous'}},
            {'type': 'turn_context', 'payload': {'turn_id': 'batch-turn', 'model': 'gpt-5'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'batch-turn'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 20, 'cached_input_tokens': 5, 'output_tokens': 4, 'reasoning_output_tokens': 2, 'total_tokens': 24}}}},
        ]
        def line(entry):
            return json.dumps({'timestamp': '2026-10-28T10:00:00Z', **entry}) + '\n'
        session.write_text(''.join(map(line, entries)), encoding='utf-8')
        launched = self.run_launcher(package / START, '--codex-report', '--scope', 'chat',
                                     '--period', 'today', '--today', '2026-10-28', '--timezone', 'UTC')
        self.assertIn(launched.returncode, (0, 2), launched.stderr + launched.stdout)
        self.assertEqual(len(launched.stdout.splitlines()), 1, launched.stdout)
        result = json.loads(launched.stdout)
        config, saved, dashboard = self._batch_saved_profile()
        self.assertEqual(result['selection'], {'startDate': '2026-10-28', 'endDate': '2026-10-28',
                                               'projects': [], 'chats': [identifier]})
        self.assertEqual(tuple(result['totals'][field] for field in core.FIELDS), (120, 5, 9, 2, 129))
        self.assertEqual(result['timeZone'], 'UTC')
        self.assertEqual(parse_qs(urlsplit(result['dashboardUrl']).fragment),
                         {'from': ['2026-10-28'], 'to': ['2026-10-28'], 'chat': [identifier]})
        query = result['queryUsage']
        self.assertEqual(query['status'], 'provisional', query)
        self.assertFalse(query['final'])
        self.assertEqual(query['subtotal'], {'inputTokens': 20, 'cachedInputTokens': 5,
                         'outputTokens': 4, 'reasoningOutputTokens': 2, 'totalTokens': 24})
        self.assertTrue(query['watcherStarted'])
        self.assertIn('/query-usage/' + query['queryId'] + '/index.html', query['viewUrl'])
        self.assertEqual(urlsplit(query['viewUrl']).netloc, urlsplit(result['dashboardUrl']).netloc)
        self.assertIn(b'result.js', self.local_request(query['viewUrl']))
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(saved['database'])) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM scan_runs').fetchone()[0], 1)
        evidence = json.loads((dashboard / 'evidence.json').read_text(encoding='utf-8'))
        self.assertEqual(evidence['offlineScanner']['scanId'], 1)
        shared_hashes = {name: digest(dashboard / name) for name in ('data.json', 'index.html', 'evidence.json', 'usage-ledger.json')}
        with session.open('a', encoding='utf-8') as stream:
            stream.write(line({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                         'content': [{'type': 'output_text', 'text': EXPORT_PRIVATE}]}}))
            stream.write(line({'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {
                'input_tokens': 30, 'cached_input_tokens': 7, 'output_tokens': 6,
                'reasoning_output_tokens': 3, 'total_tokens': 36}}}}))
            stream.write(line({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'batch-turn'}}))
        deadline = time.monotonic() + 30
        measured = {}
        result_path = dashboard / 'query-usage' / query['queryId'] / 'result.json'
        while time.monotonic() < deadline:
            measured = json.loads(result_path.read_text(encoding='utf-8'))
            if measured.get('final'):
                break
            time.sleep(0.1)
        self.assertEqual(measured.get('status'), 'complete', measured)
        self.assertEqual(measured['counters']['totalTokens'], 60)
        self.assertEqual({name: digest(dashboard / name) for name in shared_hashes}, shared_hashes)
        self.assertIn(b'"final":true', self.local_request(query['viewUrl'].replace('index.html', 'result.js')))
        self.assertNotIn(EXPORT_PRIVATE, launched.stdout + launched.stderr)
        self.assert_no_toolchains()

    def test_codex_batch_optional_meter_failure_and_preflight_rejection_preserve_last_report(self):
        package = self.extract()
        self.environment.pop('CODEX_THREAD_ID', None)
        result = self.launch(package / START, '--codex-report', '--scope', 'all', '--period', 'last3',
                             '--today', '2026-10-26', '--timezone', 'UTC')
        _, saved, dashboard = self._batch_saved_profile()
        self.assertEqual(tuple(result['totals'][field] for field in core.FIELDS), TOTAL)
        self.assertEqual(result['queryUsage']['status'], 'unavailable')
        self.assertEqual(result['queryUsage']['errorCode'], 'query_invalid_session_id')
        self.assertNotIn('viewUrl', result['queryUsage'])
        before = {name: digest(dashboard / name) for name in ('data.json', 'index.html', 'evidence.json', 'usage-ledger.json')}
        for arguments in (
                ('--scope', 'chat', '--period', 'today', '--today', '2026-10-26', '--timezone', 'UTC'),
                ('--scope', 'all', '--period', 'today', '--today', '2026-02-30', '--timezone', 'UTC'),
                ('--scope', 'all', '--period', 'today', '--today', '2026-10-26', '--timezone', 'Europe/Berlin'),
                ('--scope', 'project', '--project-id', 'unverified-project', '--period', 'today', '--today', '2026-10-26', '--timezone', 'UTC'),
                ('--scope', 'all', '--export', self.root / 'must-not-exist.zip')):
            with self.subTest(arguments=arguments):
                failed = self.launch(package / START, '--codex-report', *arguments, success=False)
                self.assertEqual(failed.stdout.strip(),
                                 'Usage Monitor could not start. The error above explains how to recover.')
                self.assertIn('errorCode', json.loads(failed.stderr))
                self.assertEqual({name: digest(dashboard / name) for name in before}, before)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(saved['database'])) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM scan_runs').fetchone()[0], 1)
        self.assertFalse((self.root / 'must-not-exist.zip').exists())
        self.assert_no_toolchains()

    def test_invalid_archive_hash_is_rejected_without_execution_or_export(self):
        archive = self.root / "untrusted-python.zip"
        marker = self.root / "UNVERIFIED_ARCHIVE_EXECUTED"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("../escape.py", "from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n")
        destination = self.root / "must-not-exist.zip"
        with patch.object(runtime, "download", side_effect=AssertionError("No downloads")), \
             patch.object(runtime, "ensure_toolchain", side_effect=AssertionError("No compiler")), \
             patch.object(runtime.subprocess, "run", side_effect=AssertionError("No executable input")):
            with self.assertRaises((OSError, RuntimeError, ValueError)):
                self.builder.build_windows_package(
                    PACKAGE, destination, python_archive=archive, engine=self.engine,
                    timezone_wheel=self.timezone_wheel, notices_directory=self.notices)
        self.assertFalse(destination.exists())
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "escape.py").exists())
        self.assert_no_toolchains()

    def test_even_a_hash_approved_malformed_archive_cannot_escape_extraction(self):
        # Isolate the extraction gate from the separate fixed-input-hash gate.
        # The source copy and its expected native hash exist only in this test.
        source = self.root / "synthetic release source"
        shutil.copytree(PACKAGE, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        archive = self.root / "malformed-but-hash-approved.zip"
        escape = self.root / "ARCHIVE_ESCAPE_MARKER"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("ordinary.txt", "Nothing may be extracted before every member is checked.")
            zipped.writestr(str(escape).replace("\\", "/"), "untrusted payload")
        download_manifest = source / "assets/runtime-downloads.json"
        downloads = json.loads(download_manifest.read_text(encoding="utf-8"))
        downloads["platforms"]["windows-x86_64"]["python"]["sha256"] = digest(archive)
        download_manifest.write_text(json.dumps(downloads), encoding="utf-8")
        destination = self.root / "malformed-not-exported.zip"
        with patch.object(self.builder, "NATIVE_SOURCE_SHA256", runtime.source_hash(source)), \
             patch.object(runtime, "download", side_effect=AssertionError("No downloads")), \
             patch.object(runtime.subprocess, "run", side_effect=AssertionError("No executable input")):
            with self.assertRaises((OSError, RuntimeError, ValueError)):
                self.builder.build_windows_package(
                    source, destination, python_archive=archive, engine=self.engine,
                    timezone_wheel=self.timezone_wheel, notices_directory=self.notices)
        self.assertFalse(destination.exists())
        self.assertFalse(escape.exists())
        self.assertEqual(list(self.root.glob(".usage-monitor-package-*")), [])
        self.assert_no_toolchains()


if __name__ == "__main__":
    unittest.main()
