#!/bin/bash
# Runs the repository's smoke suite and Santa's tests in Santa's environment.
cd "$(dirname "$0")/../.." || exit 1
PY="$HOME/Library/Application Support/Santa/venv/bin/python"
mkdir -p santa-app/dev/out
{ echo "== $(date)"; "$PY" -m pytest tests/test_santa.py tests/test_smoke.py -q -p no:cacheprovider 2>&1 | tail -40; echo "== exit ${PIPESTATUS[0]}"; } > santa-app/dev/out/tests.txt
echo "finished: see santa-app/dev/out/tests.txt"
