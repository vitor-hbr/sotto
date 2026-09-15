#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$project_dir/Linux${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m sotto_linux "$@"
