# Contributing

## Branching model: trunk-based development
- `main` is the only long-lived branch; it is protected (PRs only, CI must pass).
- Work on short-lived branches: `feat/...`, `fix/...`, `chore/...`
- Branches are squash-merged into `main` via a Pull Request, then deleted.

## Conventional Commits
Every commit message must follow:

    <type>[optional scope]: <description>

    [optional body]
    BREAKING CHANGE: <description>   # only for breaking changes

Common types: feat, fix, chore, docs, test, refactor, style, build, ci, perf.
A breaking change uses `feat!:` or a `BREAKING CHANGE:` body line.

Examples:
- feat(backend): add DeepSeek LLM client factory
- fix: correct timezone parsing in config
- feat!: drop support for Python 3.11

Messages are validated by the commit-msg hook (commitizen). Install hooks:
    cd backend && uv run pre-commit install --hook-type pre-commit --hook-type commit-msg

## Version bump mapping (SemVer)
- fix:  -> PATCH  (1.2.3 -> 1.2.4)
- feat: -> MINOR  (1.2.3 -> 1.3.0)
- feat! / BREAKING CHANGE: -> MAJOR (1.2.3 -> 2.0.0)

## Workflow
1. git checkout -b feat/your-feature
2. Commit with conventional messages (hook enforces).
3. git push -u origin feat/your-feature
4. Open a PR into main; CI must pass.
5. Squash-merge; delete the branch.
