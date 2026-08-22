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
