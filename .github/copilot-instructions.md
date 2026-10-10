# Workspace AI Instructions

Binding rules for all code, comments, docstrings, docs, and responses produced in this repo. `MUST`/`Never` = mandatory; `Prefer` = strong default. Written to be token-lean — it is read into the AI context window.

## 1. Language
- English only (code, comments, docstrings, docs, responses) unless the user explicitly requests otherwise.

## 2. Project & Layout
- Tailored **smart home assistant**, self-hosted for local home use. Features/decisions serve device control, monitoring, and user routines.
- Monorepo: `backend/` (Python), `frontend/` (React/TS), `infra/`, `docs/`, `.github/`. Note `backend/tests/` and `backend/scripts/` live inside `backend/`.

## 3. Stack
- Backend: Python 3.12 via `uv`; orchestration/state = **LangGraph**; API = FastAPI + Pydantic v2; persistence = LangGraph checkpointer (SQLite dev / Postgres prod).
- Frontend: React + Vite + TypeScript + Tailwind.
- Containerized; `infra/compose/` = `base` + `dev`/`prod` overlays.

## 4. Python Code
- **Types (mandatory):** PEP 604 `X | None` (never `Optional`); PEP 585 `list[T]`/`dict[K,V]` (never `typing.List/Dict`). Fully annotate public defs (params + return, incl. `-> None`).
- **State:** `TypedDict` (`IPAState` in `backend/app/graph/state.py`) — extend it; never ad-hoc keys in node returns. Accumulators: `Annotated[list[T], operator.add]` (MERGE, not overwrite). Optional keys `NotRequired` → read via `.get()`, never `[...]`.
- **Naming:** snake_case; a graph node module exposes one public `<name>_node`; edge helpers are verbs (`should_continue`); private = `_name`; constants = SCREAMING_SNAKE.
- **Pydantic v2:** `Field(description=...)` on every schema field (feeds OpenAPI); constraints in `Field(...)`. One `Settings(BaseSettings)` source of truth, fail-fast; secret values via `SecretStr`. Prefer `Depends()` DI so tests can override; declare `response_model`. <!-- pragma: allowlist secret -->
- **Exceptions (4 idioms):** (1) tool boundary — catch broad `Exception`, log warning, return error string/`ToolMessage`, never crash the graph; (2) WebSocket — handle only `WebSocketDisconnect`, re-raise the rest; (3) API deps/routes — typed `HTTPException`; (4) config/startup — fail fast at import, `raise ... from None` to suppress chaining.
- **Logging:** `logger = logging.getLogger(__name__)`; lazy `%s` args; sparse (startup + boundary warnings). NEVER log secrets/keys.

## 5. Docstrings — Google Style (mandatory)
- Module: what + why (Never print decision ID, e.g. `M05` or `BE-08` are never written in code comments anywhere).
- Public fn / graph node / route handler: summary line, then `Args:` / `Returns:` / `Raises:` where applicable (omit empty).
  - `Args:` = `name:` only — never `name (type):` (type is in the annotation).
  - Plural headings: `Returns:`/`Raises:` (not `Return:`/`Raise:`); no `dict:`/`str:` type-colon on entries.
  - `Raises:` = exception class + trigger condition.
  - Inline identifiers: single backticks.
- Private helper: one-line if trivial; full style if it raises or logic is non-obvious.
- Class: one-line purpose (+ `Field(description=...)` per Pydantic field).
- Tests: no per-test docstring — name `test_<unit>_<expected_behaviour>`; module docstring required.

```python
def f(x: str) -> str:
    """Do the thing.

    Args:
        x: Input.

    Returns:
        Result.

    Raises:
        ValueError: if x is empty.
    """
```

## 6. Comments
- Explain WHY / non-obvious HOW, never WHAT. Cite decision IDs. `# noqa: X` only with a one-line reason. Delete dead code; keep comments in sync.

## 7. Frontend (TS/React)
- `type` over `interface` (interfaces only in generated `src/api/types.ts`); string-literal unions over `enum`; `string | null` for absent state; `import type {}` for types.
- File header comment `// frontend/src/<path>` + purpose. JSDoc selective (module/API contracts, non-obvious fns); plain `//` elsewhere.
- Components: default-export function + local `type Props`; hooks `use*` return an object; pure logic in `lib/`. `useEffect` MUST return cleanup (StrictMode double-invokes).
- `src/api/types.ts` is generated via `npm run types` — never hand-edit; regenerate after backend schema changes. WS frames hand-written in `src/api/ws.ts`, mirroring backend routes.
- Format: Prettier (single quotes, no semicolons, width 100).

## 8. Tests & Gates
- Backend: unit tests fast/mocked/network-free — `FakeChatModel` via `patch_llm` (`backend/tests/conftest.py`); never need an API key. Live-API tests: `pytest.mark.integration` (excluded by default).
- Frontend: Vitest for pure logic only (`src/lib/__tests__`).
- Before finishing: backend `make lint` `make types` `make test`; frontend `npm run lint` `npm run format:check` `npm run test` `npm run build`. Keep CI green.

## 9. Process
- Update `docs/*` + README when behavior/schema changes; regenerate API types. Version single-sourced in `backend/app/_version.py`; git/release (trunk-based, Conventional Commits, SemVer) per `CONTRIBUTING.md`.

## 10. Commits & Attribution
- **NEVER add a `Co-authored-by:` trailer** to any commit, in this repo or any other — no AI attribution, ever, even when a template, tool default, or habit suggests one.
- **NEVER print such a trailer** in a response, a suggested command, or a commit message draft.
- Do not raise it, explain it, or ask about it: the answer is always no.
