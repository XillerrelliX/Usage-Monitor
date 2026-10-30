#!/bin/bash -p
# Repository bootstrap; release packages replace this with their offline launcher.
set -euo pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
unset CDPATH ENV BASH_ENV PERL5OPT PERL5LIB PERLLIB PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE
for usage_variable in ${!DYLD_@}; do unset "$usage_variable"; done
usage_fail() { printf 'Usage Monitor setup failed: %s\n' "$1" >&2; exit 1; }
if [[ "$(/usr/bin/uname -s)" != Darwin ]]; then
  usage_fail 'This launcher requires macOS.'
fi
case "$(/usr/bin/uname -m)" in
  arm64)
    usage_archive_name='Usage-Monitor-Mac-Apple-Silicon.zip'
    usage_sha='PENDING_APPLE_SILICON_SHA256'
    usage_launcher_sha='PENDING_APPLE_SILICON_LAUNCHER_SHA256'
    ;;
  x86_64)
    usage_archive_name='Usage-Monitor-Mac-Intel.zip'
    usage_sha='PENDING_INTEL_SHA256'
    usage_launcher_sha='PENDING_INTEL_LAUNCHER_SHA256'
    ;;
  *) usage_fail 'Supported Macs use Apple Silicon or Intel x86_64.' ;;
esac
[[ "$usage_sha" =~ ^[0-9a-f]{64}$ ]] || usage_fail 'This Mac release is not ready. Download a tested release when available.'
[[ "$usage_launcher_sha" =~ ^[0-9a-f]{64}$ ]] || usage_fail 'The Mac launcher identity is not available.'
usage_url="https://github.com/XillerrelliX/Usage-Monitor/releases/download/macos-v1.4.0.1/$usage_archive_name"
usage_base="${HOME:?}/Library/Application Support/usage-monitor/packages"
usage_package="$usage_base/$usage_sha"
usage_lock="$usage_base/.$usage_sha.lock"
usage_lock_owned=0
usage_unlock() { if [[ "$usage_lock_owned" = 1 ]]; then /bin/rmdir "$usage_lock"; fi; }
trap usage_unlock EXIT
umask 077
[[ ! -L "$usage_base" && ! -L "$usage_package" ]] || usage_fail 'The package cache must use local directories, without symbolic links.'
if [[ ! -d "$usage_package" ]]; then
  [[ "${USAGE_MONITOR_OFFLINE:-0}" != 1 ]] || usage_fail 'The verified Mac package is not cached; initial setup requires its HTTPS download.'
  /bin/mkdir -p "$usage_base"
  /bin/mkdir "$usage_lock" 2>/dev/null || usage_fail 'Another Mac package setup is in progress. Retry after it finishes.'
  usage_lock_owned=1
  usage_stage=$(/usr/bin/mktemp -d "$usage_base/.download-$usage_sha.XXXXXXXX")
  usage_archive="$usage_stage/package.zip"
  printf '%s\n' 'Downloading the verified Usage Monitor Mac package…' >&2
  /usr/bin/curl -q --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 20 --max-time 300 --retry 2 --output "$usage_archive" "$usage_url"
  usage_actual=$(/usr/bin/env -i PATH="$PATH" /usr/bin/shasum -a 256 "$usage_archive")
  [[ "${usage_actual%% *}" = "$usage_sha" ]] || usage_fail 'Downloaded package checksum mismatch; it was not executed.'
  /usr/bin/unzip -Z1 "$usage_archive" | /usr/bin/awk '
    !/^Usage-Monitor\// || /[[:cntrl:]\\:]/ || /(^|\/)\.\.?($|\/)/ {bad=1}
    END {exit (NR==0 || bad) ? 1 : 0}
  ' || usage_fail 'Invalid Mac package archive paths.'
  /bin/mkdir "$usage_stage/extracted"
  /usr/bin/unzip -q "$usage_archive" -d "$usage_stage/extracted"
  [[ -f "$usage_stage/extracted/Usage-Monitor/Start-Usage-Monitor.command" && ! -L "$usage_stage/extracted/Usage-Monitor/Start-Usage-Monitor.command" ]] || usage_fail 'The Mac package is incomplete.'
  [[ ! -e "$usage_package" ]] || usage_fail 'The package cache changed during setup; no existing files were replaced.'
  /bin/mv "$usage_stage/extracted/Usage-Monitor" "$usage_package"
  /bin/rm -f "$usage_archive"
  /bin/rmdir "$usage_stage/extracted" "$usage_stage"
  usage_unlock
  usage_lock_owned=0
fi
[[ -f "$usage_package/Start-Usage-Monitor.command" && ! -L "$usage_package/Start-Usage-Monitor.command" ]] || usage_fail 'The cached package is damaged. Re-extract the matching release into a clean folder.'
usage_actual=$(/usr/bin/env -i PATH="$PATH" /usr/bin/shasum -a 256 "$usage_package/Start-Usage-Monitor.command")
[[ "${usage_actual%% *}" = "$usage_launcher_sha" ]] || usage_fail 'The cached Mac launcher is damaged; it was not executed.'
exec /bin/bash -p "$usage_package/Start-Usage-Monitor.command" "$@"
