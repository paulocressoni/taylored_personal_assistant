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

The pre-commit config also lints/formats the frontend (ESLint + Prettier via `local`
hooks). Those hooks need `frontend/node_modules` installed (`cd frontend && npm ci`) and a
POSIX shell (`bash`) — use Git Bash on Windows.

## Version bump mapping (SemVer)
- fix:  -> PATCH  (1.2.3 -> 1.2.4)
- feat: -> MINOR  (1.2.3 -> 1.3.0)
- feat! / BREAKING CHANGE: -> MAJOR (1.2.3 -> 2.0.0)

## Releasing a new version (automated with release-please)

Version numbers and CHANGELOG entries are generated FROM commit history — no
manual version bumps, no hand-pushed tags. release-please owns the release PR,
the git tag and the GitHub Release.

The single version is mirrored across three files that release-please keeps in
sync inside the release PR it opens:
  - `backend/pyproject.toml` -> `[project].version` (authoritative value)
  - `backend/app/_version.py` -> `__version__` (read at runtime by `app.main`,
    `/health`, and the frontend footer; the Makefile derives `APP_VERSION` from
    it for image tags)
  - `frontend/package.json` + `frontend/package-lock.json` -> display-only mirror

How a release happens:
1. Merge conventional-commit PRs into `main` as usual
   (`fix:` -> PATCH, `feat:` -> MINOR, `feat!:`/`BREAKING CHANGE:` -> MAJOR).
2. On the next push to `main`, the release-please GitHub Action
   (`.github/workflows/release-please.yml`) scans commits since the last
   `vX.Y.Z` tag and opens a "release PR" proposing the next SemVer version,
   an auto-generated `CHANGELOG.md` entry, and the version bumps above.
3. Merge that release PR. release-please creates the `vX.Y.Z` git tag and a
   matching GitHub Release.

Config:
  - `release-please-config.json` — single unified version via the root "."
    package; `extra-files` lists every file release-please rewrites.
  - `.release-please-manifest.json` — last released version (currently 0.2.0).
  - `.github/workflows/release-please.yml` — the automation itself.

Notes:
- The `# x-release-please-version` marker on the `__version__` line in
  `backend/app/_version.py` tells release-please which line to rewrite.
- No release PR opens for `chore`/`docs`/`ci` commits — only feat/fix/breaking
  changes drive a bump.
- The workflow needs the `RELEASE_PLEASE_TOKEN` secret (fine-grained PAT with
  Contents + Pull requests write access) so CI runs on the release PR and it can
  merge into the protected `main` branch.
- Merging the release PR also **publishes the container images automatically**: when
  release-please creates the `vX.Y.Z` git tag, the `publish-docker-images` workflow
  (`.github/workflows/docker-publish.yml`) builds, Trivy-scans (fails on fixable
  HIGH/CRITICAL), and pushes **both** images to GHCR —
  `ghcr.io/paulocressoni/taylored-personal-assistant-backend` and `...-frontend` —
  tagged `X.Y.Z`, `sha-<sha>`, plus `latest` for browsing only. No manual step:
  the git tag IS the image tag, and prod never deploys `:latest` (golden rule).

## Workflow
1. git checkout -b feat/your-feature
2. Commit with conventional messages (hook enforces).
3. git push -u origin feat/your-feature
4. Open a PR into main; CI must pass.
5. Squash-merge; delete the branch.


### Standard Commit Patterns & Scope Usage

| Commit Type | When to Use This Pattern | How to Use Scope (Examples) | Full Message Example |
| :--- | :--- | :--- | :--- |
| **`feat`** | Adding a brand new feature, capability, or user-facing option. | Use the specific module, page, or service being added. | `feat(auth): add OAuth2 Google login support` |
| **`fix`** | Patching a bug, crash, or unexpected behavior in the application. | Use the component, API route, or database layer causing the issue. | `fix(api): resolve memory leak on user query` |
| **`refactor`** | Rewriting code to improve readability or structure without changing external behavior. | Use the specific class, utility file, or hook being restructured. | `refactor(utils): simplify date formatting logic` |
| **`perf`** | Making a code change specifically targeting execution speed, memory footprint, or optimization. | Use the specific bottleneck layer or asset format. | `perf(images): compress hero landing assets` |
| **`docs`** | Modifying documentation only, such as READMEs, inline code comments, or JSDocs. | Use the specific guide name, config file, or API documentation. | `docs(readme): update environment setup guide` |
| **`style`** | Adjusting code formatting, linting rules, white spaces, or semi-colons without logic modifications. | Use global configurations or the affected codebase directory. | `style(eslint): enforce trailing commas` |
| **`test`** | Adding missing unit/integration tests or updating broken test suites. | Use the testing framework or the exact component under test. | `test(cypress): fix flaky checkout pipeline` |
| **`build`** | Changing build scripts, package dependencies, external bundles, or project versions. | Use the specific package manager or build tool configuration. | `build(npm): upgrade React to version 19` |
| **`ci`** | Updating configuration files and scripts for automated pipelines, runners, or workflows. | Use the automation platform or the name of the workflow pipeline. | `ci(github-actions): add security scanning step` |
| **`chore`** | Handling routine tasks that do not modify source or test files (e.g., updating `.gitignore`). | Use the configuration file name or system component. | `chore(gitignore): exclude local .env files` |

---

### Core Guidelines for Scope Selection

* **Keep it Contextual:** The scope must be a simple **noun** wrapped in parentheses that immediately identifies the target subsystem or section of the codebase.
* **Avoid Ticket Numbers:** Do not use JIRA, GitHub, or internal issue trackers as the scope (e.g., avoid `feat(PROJ-123): change color`). Instead, link issue IDs inside the optional **commit footer**.
* **Be Consistent:** Establish a shared dictionary of valid scopes across your development team to prevent duplicate names like `(user-profile)` vs `(profile-page)`.
* **Omit When Global:** If your changes affect the entire repository broad-scale, skip the scope entirely (e.g., `style: run prettier formatter on all files`).
