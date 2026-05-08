"""Flow global exit-code registry — single source of truth.

Introduced in v0.8.2.1 to disambiguate AFK idle park (recoverable)
from USAGE_ERROR (argparse misuse) which both used rc=2 in v0.8.2.

Constants are typed `Final[int]` to prevent accidental rebinding and
make the contract explicit. The module has zero side effects (only
imports `typing`); `importlib.reload` is a no-op.

Registry (per codex round-2 reflection on v0.8.2 ecosystem audit):

    0 = PASS                    success / interactive fallback
    1 = GENERIC_FAIL            unspecified error
    2 = USAGE_ERROR             argparse / CLI misuse
    3 = BLOCKED                 hard-stop with blocked.md + snapshot
    4 = NESTED_ABORT            nested-autonomy attempt detected
    5 = PARKED_RECOVERABLE      AFK idle park (wait-mode timeout);
                                operator runs /flow:resume to continue
"""

from typing import Final

PASS: Final[int] = 0
GENERIC_FAIL: Final[int] = 1
USAGE_ERROR: Final[int] = 2
BLOCKED: Final[int] = 3
NESTED_ABORT: Final[int] = 4
PARKED_RECOVERABLE: Final[int] = 5
