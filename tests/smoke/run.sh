#!/usr/bin/env bash
# Smoke test entry point. Runs all tests under tests/smoke/.
#
# Usage: bash tests/smoke/run.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

python3 -m unittest discover -s tests/smoke -p "test_*.py" -v
