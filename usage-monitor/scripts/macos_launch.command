#!/bin/bash -p
# Generated package launcher: verify all public code before starting private Python.
set -euo pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
unset CDPATH ENV BASH_ENV PERL5OPT PERL5LIB PERLLIB PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE
unset DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH DYLD_FALLBACK_LIBRARY_PATH DYLD_FALLBACK_FRAMEWORK_PATH
for usage_variable in ${!DYLD_@}; do unset "$usage_variable"; done
usage_fail() { printf 'Usage Monitor failed: %s\n' "$1" >&2; exit 1; }
usage_root=$(CDPATH= cd -P -- "$(/usr/bin/dirname -- "$0")" && pwd)
cd -- "$usage_root"
[ "$(/usr/bin/uname -s)" = Darwin ] || usage_fail 'This package requires macOS.'
case "$(/usr/bin/uname -m)" in
  @@MACHINE@@) ;;
  *) usage_fail 'This package requires @@PLATFORM@@. Download the matching macOS ZIP.' ;;
esac
for usage_name in macos-payload.sha256 bundle-manifest.json Start-Usage-Monitor.command; do
  [ -f "$usage_name" ] && [ ! -L "$usage_name" ] || usage_fail 'Extract the original ZIP into a clean local folder.'
done
usage_actual=$(/usr/bin/env -i PATH="$PATH" LC_ALL=C /usr/bin/shasum -a 256 -- macos-payload.sha256)
[ "${usage_actual%% *}" = '@@PAYLOAD_SHA256@@' ] || usage_fail 'Damaged payload inventory. Extract the original ZIP again.'
# Names are generated from canonical, control-free paths. Never evaluate them.
/usr/bin/awk '
  length($0)<67 || substr($0,65,2)!="  " {exit 1}
  substr($0,1,64) ~ /[^0-9a-f]/ {exit 1}
  {name=substr($0,67); if (name ~ /[[:cntrl:]\\]/ || name ~ /^\// || name ~ /(^|\/)\.\.?($|\/)/ || name ~ /:/ || seen[name]++) exit 1}
  END {if (NR==0) exit 1}
' macos-payload.sha256 || usage_fail 'Invalid public payload inventory.'
for usage_directory in scripts assets native references tests licenses runtime; do
  [ -d "$usage_directory" ] && [ ! -L "$usage_directory" ] || usage_fail 'Incomplete public payload.'
done
usage_special=$(/usr/bin/find -P scripts assets native references tests licenses runtime ! -type d ! -type f -print)
[ -z "$usage_special" ] || usage_fail 'Links and special files are not allowed in the public payload.'
usage_extra=$(LC_ALL=C /usr/bin/comm -3 \
  <(/usr/bin/awk '{name=substr($0,67); if (name ~ /^(scripts|assets|native|references|tests|licenses|runtime)\//) print name}' macos-payload.sha256 | LC_ALL=C /usr/bin/sort) \
  <(/usr/bin/find -P scripts assets native references tests licenses runtime -type f -print | LC_ALL=C /usr/bin/sort))
[ -z "$usage_extra" ] || usage_fail 'Unexpected public code or runtime files. Extract the original ZIP into a clean folder.'
while IFS= read -r usage_line; do
  usage_name=${usage_line:66}
  [ -f "$usage_name" ] && [ ! -L "$usage_name" ] || usage_fail 'Links and missing files are not allowed in the public payload.'
done < macos-payload.sha256
/usr/bin/env -i PATH="$PATH" LC_ALL=C /usr/bin/shasum -a 256 -c macos-payload.sha256 >/dev/null || usage_fail 'Missing or damaged payload. Extract the original ZIP again.'
[ -x runtime/python/python/bin/python3 ] && [ -x runtime/bin/codex-usage-offline ] || usage_fail 'Executable permissions are missing. Extract the original ZIP with Archive Utility.'
usage_bridge="import runpy,sys; p=sys.argv.pop(1); sys.path.insert(0,p); runpy.run_path(p+'/standalone_app.py',run_name='__main__')"
exec "$usage_root/runtime/python/python/bin/python3" -I -S -B -X utf8 -c "$usage_bridge" "$usage_root/scripts" "$@"
