#!/bin/sh
# Portable interpreter bootstrap. Only first-time software setup uses HTTPS.
set -eu
usage_scripts=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
usage_root=$(dirname "$usage_scripts")
usage_system=$(uname -s)
usage_arch=$(uname -m)
if [ "$usage_system" = Darwin ] && [ -f "$usage_root/Start-Usage-Monitor.command" ]; then
  exec /bin/bash -p "$usage_root/Start-Usage-Monitor.command" "$@"
fi
case "$usage_system:$usage_arch" in
  Darwin:x86_64) usage_key=macos-x86_64 ;;
  Darwin:arm64) usage_key=macos-aarch64 ;;
  Linux:x86_64) usage_key=linux-x86_64 ;;
  Linux:aarch64|Linux:arm64) usage_key=linux-aarch64 ;;
  *) echo 'Automatic setup supports macOS/Linux x64 or ARM64.' >&2; exit 1 ;;
esac
if [ "$usage_system" = Darwin ]; then
  usage_runtime="$HOME/Library/Application Support/codex-local-usage/runtime"
  usage_config_directory="$HOME/Library/Application Support/codex-local-usage"
else
  usage_runtime="${XDG_DATA_HOME:-$HOME/.local/share}/codex-local-usage/runtime"
  usage_config_directory="${XDG_CONFIG_HOME:-$HOME/.config}/codex-local-usage"
fi
usage_explicit_runtime=
usage_explicit_config=
usage_next=
for usage_arg do
  if [ "$usage_next" = config ]; then usage_explicit_config=$usage_arg; usage_next=; continue; fi
  if [ "$usage_next" = runtime ]; then usage_explicit_runtime=$usage_arg; usage_next=; continue; fi
  case "$usage_arg" in
    --config) usage_next=config ;;
    --config=*) usage_explicit_config=${usage_arg#--config=} ;;
    --runtime-dir) usage_next=runtime ;;
    --runtime-dir=*) usage_explicit_runtime=${usage_arg#--runtime-dir=} ;;
  esac
done
if [ -n "$usage_explicit_config" ]; then
  usage_config_directory=$(dirname "$usage_explicit_config")
  usage_runtime="$usage_config_directory/runtime"
fi
if [ -n "$usage_explicit_runtime" ]; then usage_runtime=$usage_explicit_runtime; fi
usage_python=
usage_test_python() {
  [ -f "$1" ] && "$1" -B -X utf8 -c 'import sys,sqlite3,ssl;sys.exit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1
}
if usage_test_python "$usage_runtime/python/python/bin/python3"; then
  usage_python="$usage_runtime/python/python/bin/python3"
fi
# A successful setup records the actual private interpreter in the dashboard
# and settings directory. This plain local hint needs no preinstalled JSON parser.
for usage_hint in "$usage_root/.usage-python" "$usage_config_directory/.usage-python"; do
  [ -z "$usage_python" ] || break
  [ -f "$usage_hint" ] || continue
  usage_hint_python=$(cat "$usage_hint")
  usage_cr=$(printf '\r')
  case "$usage_hint_python" in
    ''|*'
'*|*"$usage_cr"*) echo 'Invalid local interpreter hint; expected one absolute filename. Original bytes preserved.' >&2; exit 1 ;;
    /*) ;;
    *) echo 'Invalid local interpreter hint; expected one absolute filename. Original bytes preserved.' >&2; exit 1 ;;
  esac
  if usage_test_python "$usage_hint_python"; then usage_python=$usage_hint_python; fi
done
for usage_candidate in python3 python; do
  [ -z "$usage_python" ] || break
  if command -v "$usage_candidate" >/dev/null 2>&1 && "$usage_candidate" -B -X utf8 -c 'import sys,sqlite3,ssl;sys.exit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
    usage_python=$usage_candidate
    break
  fi
done
if [ -z "$usage_python" ]; then
  # Manifest contains only pinned URL/hash records. Parse this narrow JSON with
  # POSIX awk while Python is unavailable, then verify before extraction/run.
  usage_record=$(awk -v platform="$usage_key" '
    $0 ~ "\"" platform "\"[[:space:]]*:" { inside=1; next }
    inside && /"python"[[:space:]]*:/ { python=1; next }
    python && /"url"[[:space:]]*:/ { sub(/^[^"]*"url"[[:space:]]*:[[:space:]]*"/, ""); sub(/".*/, ""); url=$0 }
    python && /"sha256"[[:space:]]*:/ { sub(/^[^"]*"sha256"[[:space:]]*:[[:space:]]*"/, ""); sub(/".*/, ""); print url " " $0; exit }
  ' "$usage_root/assets/runtime-downloads.json")
  usage_url=${usage_record% *}
  usage_hash=${usage_record##* }
  case "$usage_url" in https://github.com/astral-sh/python-build-standalone/releases/download/*) ;; *) echo 'Untrusted Python bootstrap URL.' >&2; exit 1 ;; esac
  [ "${#usage_hash}" -eq 64 ] || { echo 'Invalid Python SHA256.' >&2; exit 1; }
  mkdir -p "$usage_runtime/downloads"
  usage_archive="$usage_runtime/downloads/python-standalone.tar.gz"
  usage_verify() {
    if command -v sha256sum >/dev/null 2>&1; then usage_actual=$(sha256sum "$1"); else usage_actual=$(shasum -a 256 "$1"); fi
    [ "${usage_actual%% *}" = "$usage_hash" ]
  }
  if [ ! -f "$usage_archive" ] || ! usage_verify "$usage_archive"; then
    usage_partial=$(mktemp "$usage_runtime/downloads/python.XXXXXXXX.partial")
    echo 'Setup: downloading verified portable Python from Astral upstream' >&2
    if command -v curl >/dev/null 2>&1; then
      curl --fail --location --proto '=https' --proto-redir '=https' --output "$usage_partial" "$usage_url"
    elif command -v wget >/dev/null 2>&1; then
      wget --https-only -O "$usage_partial" "$usage_url"
    else echo 'Setup needs curl or wget for the one-time Python download.' >&2; exit 1; fi
    usage_verify "$usage_partial" || { echo "SHA256 mismatch; unexecuted download preserved: $usage_partial" >&2; exit 1; }
    if [ -e "$usage_archive" ]; then mv "$usage_archive" "$usage_archive.previous-$(date +%s)-$$"; fi
    mv "$usage_partial" "$usage_archive"
  fi
  usage_staging=$(mktemp -d "$usage_runtime/python.staging-XXXXXXXX")
  # Verified immutable upstream tar only. Before invoking its interpreter, reject
  # traversal, hard links and non-file types. Relative symlinks are dropped;
  # invoke python3.13 (a regular file) directly and materialize links in Python.
  tar -tzf "$usage_archive" | awk '
    /^\// || /(^|\/)\.\.(\/|$)/ || /:/ { bad=1 }
    END { exit bad ? 1 : 0 }
  ' || { echo 'Unsafe Python archive path.' >&2; exit 1; }
  tar -tvzf "$usage_archive" | awk '
    /python\/share\/terminfo\// {next}
    substr($0,1,1)!="-" && substr($0,1,1)!="d" && substr($0,1,1)!="l" {bad=1}
    substr($0,1,1)=="l" && ($NF ~ /[\/:]/ || $NF=="..") {bad=1}
    END {exit bad ? 1 : 0}
  ' || { echo 'Unsafe Python archive type or symlink target.' >&2; exit 1; }
  # Interactive terminal databases are unnecessary for this CLI. Excluding them
  # also avoids hundreds of relative directory links before Python is available.
  tar -xzf "$usage_archive" --exclude='python/share/terminfo' -C "$usage_staging"
  usage_minor=$(awk '/"python_minor"[[:space:]]*:/ {sub(/^[^"]*"python_minor"[[:space:]]*:[[:space:]]*"/, "");sub(/".*/, "");print;exit}' "$usage_root/assets/runtime-downloads.json")
  case "$usage_minor" in 3.1[0-9]) ;; *) echo 'Invalid portable Python version.' >&2; exit 1 ;; esac
  "$usage_staging/python/bin/python$usage_minor" -B -c 'import pathlib,sys,sqlite3,ssl; root=pathlib.Path(sys.argv[1]).resolve(); links=[p for p in root.rglob("*") if p.is_symlink()]; targets=[(p,p.resolve(strict=True)) for p in links]; assert all(t.is_file() and t.is_relative_to(root) for p,t in targets); blobs=[(p,t.read_bytes(),t.stat().st_mode) for p,t in targets]; [(p.unlink(),p.write_bytes(b),p.chmod(m & 0o755)) for p,b,m in blobs]' "$usage_staging"
  usage_destination="$usage_runtime/python"
  if [ -e "$usage_destination" ]; then mv "$usage_destination" "$usage_destination.previous-$(date +%s)-$$"; fi
  mv "$usage_staging" "$usage_destination"
  usage_python="$usage_destination/python/bin/python$usage_minor"
fi
exec "$usage_python" -B -X utf8 -c 'import runpy,sys;p=sys.argv.pop(1);sys.path.insert(0,p);runpy.run_path(p+"/setup_runtime.py",run_name="__main__")' "$usage_scripts" "$@"
