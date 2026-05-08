"""Probe bashlex AST shape for codex's claimed bypasses."""
import sys
sys.path.insert(0, '/tmp/bashlex-spike')
import bashlex

cases = [
    "git commit -m foo",
    'git"" commit -m foo',
    "g\\it commit -m foo",
    'gi""t commit -m foo',
    "git $IFS commit -m foo",
    "git$EMPTY commit -m foo",
    ': "$(git commit -m foo)"',
    "command git commit -m foo",
    "env git commit -m foo",
    'eval "git commit -m foo"',
    'bash -c "git commit -m foo"',
    "GIT_INDEX_FILE=/tmp/idx git commit -m foo",
    "PATH=. git commit -m foo",
    "git -c user.name=x commit -m foo",
    "git -C repo commit -m foo",
    "git --git-dir=.git commit -m foo",
    "git commit -am foo",
    "git commit -amx",
    "/usr/bin/git commit -m foo",
    r"\git commit -m foo",
    "(git commit -m foo)",
    "false || git commit -m foo",
    "echo x | git commit -F -",
    "git commit -m foo &",
]


def collect_words(node, lst):
    kind = getattr(node, "kind", None)
    if kind == "command":
        argv = []
        assigns = []
        for p in getattr(node, "parts", []):
            pkind = getattr(p, "kind", None)
            pword = getattr(p, "word", None)
            if pkind == "assignment":
                assigns.append(pword)
            else:
                argv.append((pkind, pword))
        lst.append({"kind": "command", "argv": argv, "assignments": assigns})
        return
    for attr in ("parts", "list", "commands"):
        children = getattr(node, attr, None)
        if children:
            for c in children:
                collect_words(c, lst)


for c in cases:
    print(f"=== {c!r}")
    try:
        trees = bashlex.parse(c)
        all_cmds = []
        for t in trees:
            collect_words(t, all_cmds)
        for cmd in all_cmds:
            print(f"  -> argv: {cmd['argv']}")
            if cmd["assignments"]:
                print(f"     assigns: {cmd['assignments']}")
        if not all_cmds:
            print(f"  -> (no command nodes found)")
    except Exception as e:
        print(f"  -> PARSE_ERROR: {type(e).__name__}: {str(e)[:80]}")
