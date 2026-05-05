# SHARED_ARTIFACTS — Forced-Serial Glob List (v0.7)

Any task whose `writes:` glob has any overlap with a glob below forces the
entire wave to serial regardless of declared disjointness with other tasks.

These represent files where multiple concurrent writes silently corrupt or
race even when the file appears in only one task's declared writes.

## Globs

```yaml
shared_artifacts:
  # Package manager / lockfiles
  - "**/package.json"
  - "**/package-lock.json"
  - "**/*.lock"
  - "**/yarn.lock"
  - "**/pnpm-lock.yaml"
  - "**/Cargo.lock"
  - "**/go.sum"
  - "**/go.mod"
  - "**/requirements.txt"
  - "**/poetry.lock"
  - "**/Pipfile.lock"
  
  # Flow framework state
  - ".flow/skills-map.md"
  - "claude/capabilities/*.json"
  
  # Release artifacts
  - "VERSION"
  - "CHANGELOG.md"
```

## Versioning

This list is part of the planner skill. When updated, the wave-decomposition
cache invalidates (via `planner_version` key). Bump `planner_version` in
`flow-wave-planner/SKILL.md` when adding/removing entries here.

## Adding entries

Open a PR with:
1. The glob to add (use recursive `**/` prefix unless truly root-only)
2. A pitfall doc in `.flow/pitfalls/` describing the collision observed
3. The pitfall's `trigger_paths` should include the glob
