#!/bin/sh
usage_dir=$(CDPATH= cd -P "$(dirname "$0")" && pwd) || exit 1
exec sh "$usage_dir/refresh.sh" --setup --offline "$@"
