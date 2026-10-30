#!/usr/bin/env python3
"""One-time, user-local tool bootstrap. Report refresh itself never needs network.

Only this setup module downloads software. Every executable/archive has a pinned
upstream SHA256 in assets/runtime-downloads.json. rustup checks Rust component
hashes and Cargo checks the locked crate checksums. No usage files are uploaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile

SCHEMA = 1
TRUSTED_HOSTS = {"static.rust-lang.org", "github.com", "release-assets.githubusercontent.com",
                 "objects.githubusercontent.com", "www.python.org", "ziglang.org", "files.pythonhosted.org"}


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def platform_key() -> str:
    system = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}.get(platform.system())
    machine = {"AMD64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(platform.machine())
    if not system or not machine or (system == "windows" and machine != "x86_64"):
        raise RuntimeError("Automatic setup supports Windows x64 and macOS/Linux x64 or ARM64.")
    return system + "-" + machine


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class TrustedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in TRUSTED_HOSTS or parsed.username or parsed.password:
        raise RuntimeError("Refusing non-HTTPS or untrusted software download URL")


def download(entry: dict, directory: Path) -> Path:
    """Keep failed and previous downloads intact; never execute unverified bytes."""
    url, expected = entry["url"], entry["sha256"]
    validate_url(url)
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise RuntimeError("Invalid pinned software SHA256")
    directory.mkdir(parents=True, exist_ok=True)
    name = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
    destination = directory / name
    if destination.is_file() and digest(destination) == expected:
        return destination
    temporary = directory / (name + "." + uuid.uuid4().hex + ".partial")
    print("Setup: downloading " + name, file=sys.stderr, flush=True)
    opener = urllib.request.build_opener(TrustedRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": "codex-local-usage-setup/1"})
    with opener.open(request, timeout=120) as response, temporary.open("xb") as output:
        validate_url(response.geturl())
        shutil.copyfileobj(response, output, length=1024 * 1024)
    if digest(temporary) != expected:
        raise RuntimeError("SHA256 mismatch; preserved unexecuted download: " + str(temporary))
    if destination.exists():
        os.replace(destination, destination.with_name(destination.name + ".previous-" + uuid.uuid4().hex))
    os.replace(temporary, destination)
    return destination


def safe_member(name: str, destination: Path) -> Path:
    """Reject absolute, drive, parent, NUL and Windows alternate-stream paths."""
    normalized = name.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if not parts or normalized.startswith("/") or "\x00" in name or any(p == ".." or ":" in p for p in parts):
        raise RuntimeError("Unsafe archive member: " + repr(name))
    target = destination.joinpath(*parts)
    if not target.resolve().is_relative_to(destination.resolve()):
        raise RuntimeError("Archive member leaves install directory")
    return target


def safe_unpack(archive: Path, destination: Path) -> None:
    """Write only regular files/directories. No links, devices or traversal."""
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            members = [(item, safe_member(item.filename, destination)) for item in package.infolist()]
            for item, _ in members:
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise RuntimeError("Archive contains a link or special file")
            for item, target in members:
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(item) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
    else:
        with tarfile.open(archive, "r:*") as package:
            members = [(item, safe_member(item.name, destination)) for item in package.getmembers()]
            # Python standalone contains harmless relative symlinks (python -> python3).
            # Materialize each as a regular file after validating its complete target.
            entries = {item.name: item for item, _ in members}
            def regular_target(item, visited=None):
                visited = set() if visited is None else visited
                if item.name in visited:
                    raise RuntimeError("Archive contains a link cycle")
                visited.add(item.name)
                if item.isfile():
                    return item
                if not (item.issym() or item.islnk()):
                    raise RuntimeError("Archive link does not resolve to a regular file")
                reference = str(PurePosixPath(item.name).parent / item.linkname) if item.issym() else item.linkname
                # Internal relative links may contain '..'; normalize and enforce
                # containment without ever creating an OS symlink.
                if item.linkname.startswith(("/", "\\")) or ":" in item.linkname or "\x00" in item.linkname:
                    raise RuntimeError("Unsafe archive link target")
                target = destination.joinpath(*PurePosixPath(reference.replace("\\", "/")).parts).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise RuntimeError("Archive link leaves install directory")
                resolved = target.relative_to(destination.resolve()).as_posix()
                if resolved not in entries:
                    raise RuntimeError("Archive link has a missing target")
                return regular_target(entries[resolved], visited)
            for item, _ in members:
                if not (item.isdir() or item.isfile() or item.issym() or item.islnk()):
                    raise RuntimeError("Archive contains a special file")
                if item.issym() or item.islnk():
                    regular_target(item)
            for item, target in members:
                if item.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if item.issym() or item.islnk():
                    item = regular_target(item)
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(item) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod((item.mode & 0o755) | 0o600)


def run(command: list[str], *, env: dict, cwd: Path | None = None) -> None:
    subprocess.run(command, env=env, cwd=cwd, check=True, stdout=sys.stderr, stderr=sys.stderr)


def install_archive(entry: dict, runtime: Path, name: str, environment: dict) -> Path:
    destination = runtime / name
    stamp = destination / ".installed.json"
    if stamp.is_file() and json.loads(stamp.read_text(encoding="utf-8")).get("sha256") == entry["sha256"]:
        return destination
    archive = download(entry, runtime / "downloads")
    staging = runtime / (name + ".staging-" + uuid.uuid4().hex)
    staging.mkdir(parents=True)
    if entry.get("kind") == "w64devkit":
        # Only this exact SHA256-pinned upstream executable is allowed as an SFX.
        # It is trusted code, like rustup-init, and runs inside an empty staging tree.
        run([str(archive), "-y", "-o" + str(staging)], env=environment, cwd=staging)
        for item in staging.rglob("*"):
            if item.is_symlink() or (getattr(item.lstat(), "st_file_attributes", 0) & 0x400):
                raise RuntimeError("Compiler distribution contains a reparse point")
    else:
        safe_unpack(archive, staging)
    atomic_json(staging / ".installed.json", {"sha256": entry["sha256"], "url": entry["url"]})
    if destination.exists():
        os.replace(destination, destination.with_name(name + ".previous-" + uuid.uuid4().hex))
    os.replace(staging, destination)
    return destination


def source_hash(package_root: Path) -> str:
    hasher = hashlib.sha256()
    native = package_root / "native"
    paths = [native / "Cargo.toml", native / "Cargo.lock"]
    paths += [path for path in native.rglob("*") if path.is_file() and path.suffix in {".rs", ".json", ".toml"} and "target" not in path.relative_to(native).parts]
    # Preserve the published Windows identity on every host. WindowsPath sorts
    # case-insensitive components; PosixPath sorts case-sensitive components.
    # Keep original path spelling in the digest, and compare whole components
    # so a directory remains ordered before a similarly prefixed filename.
    def path_order(path):
        return tuple(part.casefold() for part in path.relative_to(package_root).parts)
    for path in sorted(set(paths), key=path_order):
        if not path.is_file():
            raise RuntimeError("Missing native source: " + str(path))
        hasher.update(path.relative_to(package_root).as_posix().encode("utf-8") + b"\0")
        hasher.update(bytes.fromhex(digest(path)))
    hasher.update(bytes.fromhex(digest(package_root / "assets" / "runtime-downloads.json")))
    return hasher.hexdigest()


def base_environment(runtime: Path, host: str, version: str) -> dict:
    environment = dict(os.environ)
    environment.update(CARGO_HOME=str(runtime / "cargo"), RUSTUP_HOME=str(runtime / "rustup"),
                       CARGO_TARGET_DIR=str(runtime / "build"), RUSTUP_TOOLCHAIN=version + "-" + host,
                       RUSTUP_INIT_SKIP_PATH_CHECK="yes", RUSTUP_NO_UPDATE_CHECK="1")
    environment["PATH"] = str(runtime / "cargo" / "bin") + os.pathsep + environment.get("PATH", "")
    return environment


def usable_c_compiler(compiler: str, environment: dict, runtime: Path) -> bool:
    """Catch missing SDKs/CLT and unusable 'cc' stubs before native dependency builds."""
    probe = runtime / "compiler-probes" / uuid.uuid4().hex
    probe.mkdir(parents=True)
    source = probe / "probe.c"
    output = probe / ("probe.exe" if os.name == "nt" else "probe")
    source.write_text("int main(void) { return 0; }\n", encoding="ascii")
    try:
        result = subprocess.run([compiler, str(source), "-o", str(output)], env=environment,
                                capture_output=True, text=True, timeout=60)
        (probe / "compiler-output.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
        return result.returncode == 0 and output.is_file()
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_toolchain(package_root: Path, runtime: Path) -> dict:
    key = platform_key()
    manifest = json.loads((package_root / "assets" / "runtime-downloads.json").read_text(encoding="utf-8"))
    entry = manifest["platforms"][key]
    host, version = entry["rustup"]["host"], manifest["rust_version"]
    environment = base_environment(runtime, host, version)
    runtime.mkdir(parents=True, exist_ok=True)
    executable = ".exe" if key.startswith("windows") else ""
    cargo = runtime / "cargo" / "bin" / ("cargo" + executable)
    rustc = runtime / "rustup" / "toolchains" / (version + "-" + host) / "bin" / ("rustc" + executable)
    if not cargo.is_file() or not rustc.is_file():
        installer = download(entry["rustup"], runtime / "downloads")
        installer.chmod(installer.stat().st_mode | stat.S_IXUSR)
        print("Setup: installing isolated Rust " + version, file=sys.stderr, flush=True)
        run([str(installer), "-y", "--no-modify-path", "--profile", "minimal", "--default-host", host,
             "--default-toolchain", version], env=environment)
    compiler = None
    if key.startswith("windows"):
        distribution = install_archive(entry["compiler"], runtime, "compiler", environment)
        compiler = next(distribution.glob("*/bin/gcc.exe"), None)
        if compiler is None:
            raise RuntimeError("Verified C compiler distribution has no gcc.exe")
        environment.update(CC=str(compiler), CXX=str(compiler.with_name("g++.exe")), AR=str(compiler.with_name("ar.exe")),
                           CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=str(compiler))
        # Rust GNU std expects its matching unwind/runtime libraries. w64devkit
        # intentionally merges libgcc_eh into libgcc, so use Rust's bundled set.
        environment["CARGO_ENCODED_RUSTFLAGS"] = "-Clink-self-contained=yes"
        environment["PATH"] = str(compiler.parent) + os.pathsep + environment["PATH"]
    else:
        cc = shutil.which("cc", path=environment["PATH"])
        clang = shutil.which("clang", path=environment["PATH"])
        archiver = shutil.which("llvm-ar", path=environment["PATH"]) or shutil.which("ar", path=environment["PATH"])
        selected = None
        if archiver:
            for candidate in dict.fromkeys((cc, clang)):
                if candidate and usable_c_compiler(candidate, environment, runtime):
                    selected = candidate
                    break
        if selected:
            compiler = Path(selected)
            cxx_name = "clang++" if selected == clang else "c++"
            environment.update(CC=selected, CXX=shutil.which(cxx_name, path=environment["PATH"]) or selected, AR=archiver)
            environment["CARGO_TARGET_" + host.upper().replace("-", "_") + "_LINKER"] = selected
        else:
            distribution = install_archive(entry["compiler"], runtime, "compiler", environment)
            zig = next(distribution.glob("*/zig"), None)
            if zig is None:
                raise RuntimeError("Verified compiler distribution has no Zig executable")
            wrappers = runtime / "compiler-wrappers"
            wrappers.mkdir(exist_ok=True)
            import shlex
            for name, argument in (("cc", "cc"), ("c++", "c++"), ("ar", "ar")):
                wrapper = wrappers / name
                wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(str(zig)) + " " + argument + ' "$@"\n', encoding="utf-8")
                wrapper.chmod(0o700)
            environment.update(CC=str(wrappers / "cc"), CXX=str(wrappers / "c++"), AR=str(wrappers / "ar"))
            environment["CARGO_TARGET_" + host.upper().replace("-", "_") + "_LINKER"] = str(wrappers / "cc")
            environment["PATH"] = str(wrappers) + os.pathsep + environment["PATH"]
    atomic_json(runtime / "toolchain.json", {"schema_version": SCHEMA, "platform": key, "rust": version,
                "host": host, "compiler": str(compiler) if compiler else "system-or-zig"})
    return {"cargo": str(cargo), "environment": environment, "host": host}


def ensure_engine(package_root, runtime_dir, prepare_only=False) -> dict:
    package_root, runtime = Path(package_root).resolve(), Path(runtime_dir).expanduser().resolve()
    if ((runtime / "prebuilt-manifest.json").is_file() or (runtime / "prebuilt-required").exists()
            or (package_root / "bundle-manifest.json").is_file()):
        from macos_runtime import is_macos_bundle
        if is_macos_bundle(package_root):
            from macos_runtime import resolve_prebuilt
        else:
            from prebuilt_runtime import resolve_prebuilt
        return resolve_prebuilt(package_root, runtime)
    python_packages = None if prepare_only else ensure_timezone_data(runtime, package_root)
    suffix = ".exe" if os.name == "nt" else ""
    engine = runtime / "bin" / ("codex-usage-offline" + suffix)
    stamp_path = runtime / "engine.json"
    if not prepare_only and stamp_path.is_file() and engine.is_file():
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        if stamp.get("schema_version") == SCHEMA and stamp.get("source_sha256") == source_hash(package_root) and stamp.get("binary_sha256") == digest(engine):
            # Fast path: no downloads, subprocesses, Rust checks or compiler access.
            return {"engine": str(engine), "environment": dict(os.environ), "cargo": str(runtime / "cargo" / "bin" / ("cargo" + suffix)), "status": "reused", "pythonPackages": python_packages}
    prepared = ensure_toolchain(package_root, runtime)
    if prepare_only:
        return {"engine": str(engine), **prepared, "status": "prepared"}
    source_before = source_hash(package_root)
    print("Setup: building the local usage engine (locked dependencies)", file=sys.stderr, flush=True)
    run([prepared["cargo"], "build", "--release", "--locked", "--manifest-path", str(package_root / "native" / "Cargo.toml")],
        env=prepared["environment"], cwd=package_root / "native")
    if source_hash(package_root) != source_before:
        raise RuntimeError("Native source changed during build; rerun setup")
    built = runtime / "build" / "release" / engine.name
    if not built.is_file():
        raise RuntimeError("Cargo did not produce the expected local engine")
    engine.parent.mkdir(parents=True, exist_ok=True)
    temporary = engine.with_name(engine.name + "." + uuid.uuid4().hex + ".tmp")
    shutil.copy2(built, temporary)
    os.replace(temporary, engine)
    atomic_json(stamp_path, {"schema_version": SCHEMA, "source_sha256": source_before,
                "binary_sha256": digest(engine), "created_at": int(time.time())})
    return {"engine": str(engine), **prepared, "status": "created", "pythonPackages": python_packages}


def ensure_timezone_data(runtime_dir, package_root=None):
    """Supply IANA timezone data when the host Python/OS has none (Windows).

    This is a verified pure-data wheel unpacked into the private runtime; pip and
    the global Python installation are never modified. Complete runs are local.
    """
    runtime = Path(runtime_dir).expanduser().resolve()
    package_root = Path(package_root) if package_root is not None else Path(__file__).resolve().parent.parent
    destination = runtime / "python-packages"
    utc = destination / "tzdata" / "zoneinfo" / "UTC"
    manifest = json.loads((package_root / "assets" / "runtime-downloads.json").read_text(encoding="utf-8"))
    entry = manifest["timezone_data"]
    stamp = destination / ".installed.json"
    if utc.is_file() and stamp.is_file():
        if json.loads(stamp.read_text(encoding="utf-8")).get("sha256") == entry["sha256"]:
            return str(destination)
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo("UTC")
        return None
    except ZoneInfoNotFoundError:
        pass
    result = install_archive(entry, runtime, "python-packages", dict(os.environ))
    if not (result / "tzdata" / "zoneinfo" / "UTC").is_file():
        raise RuntimeError("Verified timezone data does not contain UTC")
    return str(result)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    options, rest = parser.parse_known_args(argv)
    package_root = Path(__file__).resolve().parent.parent
    if options.prepare_only:
        if options.runtime_dir is None:
            raise RuntimeError("--prepare-only requires --runtime-dir")
        result = ensure_engine(package_root, options.runtime_dir, prepare_only=True)
        print(json.dumps({k: v for k, v in result.items() if k != "environment"}))
        return 0
    # refresh_usage owns config/pointer selection and calls ensure_engine only for
    # its local scanner route. Legacy explicit SQLite fixtures need no toolchain.
    if options.runtime_dir is not None:
        rest += ["--runtime-dir", str(options.runtime_dir)]
    # Isolated Windows embedded Python needs an explicit source-directory bridge.
    command = [sys.executable, "-B", "-X", "utf8", "-c",
               'import runpy,sys;p=sys.argv.pop(1);sys.path.insert(0,p);runpy.run_path(p+"/refresh_usage.py",run_name="__main__")',
               str(package_root / "scripts"), *rest]
    return subprocess.call(command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print("Setup failed: " + str(error), file=sys.stderr)
        raise SystemExit(1)
