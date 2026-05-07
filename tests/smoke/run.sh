#!/usr/bin/env bash
# Smoke test entry point. Runs all tests under tests/smoke/ AND tests/unit/.
#
# Usage: bash tests/smoke/run.sh
#
# v0.8.1 T9 added tests/unit/ as a sibling discovery root. Both must pass
# for the suite to exit 0; ``set -e`` enforces this since a non-zero exit
# from either ``unittest discover`` invocation aborts the script.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

python3 -m unittest discover -s tests/smoke -p "test_*.py" -v
python3 -m unittest discover -s tests/unit -p "test_*.py" -v
