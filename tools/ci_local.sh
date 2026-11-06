#!/usr/bin/env bash
# Runs the checks of .github/workflows/validate.yml locally, in the same order, so a push meets no surprise.
# Hassfest and the HACS action run only on GitHub. Pytest runs in parallel unless SERIAL=1 (GitHub runs it
# serially, without a virtual environment).
set -euo pipefail

cd "$(dirname "$0")/.."
bin=".venv/bin"
[ -x "$bin/python" ] || bin="$(dirname "$(command -v python3)")"

echo "== Ruff"
"$bin/ruff" check .

echo "== Pytest"
if [ "${SERIAL:-0}" = 1 ]; then
  "$bin/pytest" tests/ -q -p no:cacheprovider
else
  "$bin/pytest" tests/ -n 8 -q -p no:cacheprovider
fi

echo "== Card"
cd frontend
npm run type-check
npm test
npm run check-dist

echo "== All checks passed"
