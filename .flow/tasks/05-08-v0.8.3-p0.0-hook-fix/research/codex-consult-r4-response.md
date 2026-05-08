codex
| Round 3 ref | Status |
|---|---|
| A.1 | not closed (Step 5c: `argv[0] != "git"` exits PASS, so wrapper root commands are not blocked) |
| A.2 | closed |
| A.3 | closed |
| B.1 | closed |
| B.2 | not closed (Step 5c: `argv[0] != "git"` exits PASS, so wrappers are not blocked) |
| B.3 | closed |
| B.4 | closed |
| C.1 | closed |
| C.2 | closed |

R

Fails at Step 5c: non-`git` argv0 is treated as PASS, which does not close the accepted wrapper finding.
tokens used
