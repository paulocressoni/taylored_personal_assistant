# React frontend — chat UI (M10)

Guide for the browser chat UI added in M10. It streams the assistant's reply
**token-by-token** over the `/ws/chat` WebSocket, talks to the FastAPI backend on
`:8000` through a Vite dev proxy, and pins the API contract to the backend's OpenAPI
schema so the two can't silently drift apart.

## What M10 added

- A **Vite + React 19 + TypeScript** app in `frontend/` (Create React App is deprecated —
  Vite is the current standard).
- **Tailwind CSS v4** for styling — utility classes in the markup via the
  `@tailwindcss/vite` plugin; no separate CSS files to context-switch into.
- **`useChatStream`** hook — the core of the streaming UX.
- Four small components: `MessageList`, `MessageInput`, `TypingIndicator`, `LanguageBadge`.
- **`deviceId`** in `localStorage`, sent as `device_id` on every message (reserved for M17).
- **`useAlarmSound`** — a Web Audio API stub, scaffolded but **not wired up** (M17).
- Tooling: ESLint (lint) + Prettier (format), pre-commit `local` hooks, a `frontend` CI
  job, and Node pinned via `frontend/.nvmrc` + `engines` in `package.json`.

## File map

```
frontend/
├── package.json             # Manifest + npm scripts (dev, build, lint, types, format)
├── package-lock.json        # Locked deps (COMMIT this — npm ci uses it)
├── .nvmrc                   # Pinned Node version (24.19.0)
├── vite.config.ts           # Vite + Tailwind plugin + dev proxy → :8000
├── eslint.config.js         # ESLint flat config (lint, not format)
├── .prettierrc.json         # Prettier style (format)
├── index.html               # Page shell
├── tsconfig*.json           # TypeScript project references
└── src/
    ├── main.tsx             # React root + <StrictMode>
    ├── App.tsx              # Layout: header + message list + input
    ├── index.css            # Tailwind v4 entry (@import "tailwindcss")
    ├── api/
    │   ├── types.ts         # GENERATED from OpenAPI (npm run types) — commit it
    │   └── ws.ts            # Hand-written WebSocket frame types
    ├── lib/
    │   └── deviceId.ts      # Stable UUID in localStorage (M17 alarms)
    ├── hooks/
    │   ├── useChatStream.ts # WS streaming + StrictMode-safe cleanup
    │   └── useAlarmSound.ts # Web Audio stub (wired up in M17)
    └── components/
        ├── MessageList.tsx
        ├── MessageInput.tsx
        ├── TypingIndicator.tsx
        └── LanguageBadge.tsx
```

## Running it

1. Backend up first (`make dev-up` from the repo root, or `uv run uvicorn app.main:app
   --reload` from `backend/`) — the frontend is useless without it.
2. Install deps and generate the API types:

   ```powershell
   cd frontend
   npm install
   npm run types        # needs the backend on :8000 → writes src/api/types.ts
   ```

3. Start the dev server:

   ```powershell
   npm run dev          # Vite on http://localhost:5173
   ```

4. Open `http://localhost:5173` and chat. Tokens stream in visibly, one at a time.

The Vite dev server proxies `/ws` (and `/openapi.json`) to `http://localhost:8000`
(`vite.config.ts`). That's why the browser only ever talks to `:5173` — no CORS, no
hardcoded backend URL. WebSockets aren't subject to CORS anyway, but the proxy is the
pattern that will also serve the built app from the backend later (single unified package).

## How streaming works (the mental model)

- The backend runs **one turn per connection**: it accepts a `ChatRequest`, streams
  `token` frames, then a `done` frame, and closes the socket (see `docs/api.md`).
- So `useChatStream.sendMessage()` **opens a fresh `WebSocket` per message**:
  1. appends the user message + an empty assistant message,
  2. opens `ws://<host>/ws/chat`, sends the `ChatRequest` envelope (including
     `device_id` from `lib/deviceId.ts`),
  3. each `token` frame appends its `content` to the last assistant message,
  4. `done` fills in the final reply / language / route and closes the socket.
- Every `token` updates state → React re-renders → the bubble grows live. That's the
  "token-by-token" effect.

### StrictMode and cleanup (why the hook is shaped that way)

React 18/19 `StrictMode` (already in `main.tsx`) deliberately **mounts, unmounts, and
remounts** every component once in development to catch bugs. The hook's `useEffect`
returns a cleanup that closes any in-flight socket (`socketRef.current?.close()`). Because
the effect itself does no setup work, both the StrictMode "ghost" unmount and a real
unmount (navigating away) close the socket correctly. The `ref` holds the socket so the
cleanup can always find it — state would churn on every re-render and is the wrong tool
here.

## The contract (why it can't drift)

- `npm run types` runs
  `openapi-typescript http://localhost:8000/openapi.json -o src/api/types.ts`, generating
  TypeScript types for `ChatRequest` / `ChatResponse` straight from the backend's Pydantic
  models. Regenerate and **commit** it whenever the backend schemas change — a renamed
  field then fails `npm run build` in the frontend instead of breaking at runtime.
- OpenAPI documents **HTTP only** — it does not describe WebSocket traffic. The WS frame
  types (`token` / `status` / `done` / `error`) are therefore **hand-written** in
  `src/api/ws.ts`, mirroring exactly what `backend/app/api/routes.py` sends.

## deviceId (reserved for M17)

`lib/deviceId.ts` reads (or creates) a UUID in `localStorage` and returns it
deterministically. It's sent as `device_id` on every message. **It looks unused now, but
M17 (alarms) needs it to know which browser tab/device to ring later** — don't remove it.

## useAlarmSound (stub, M17)

`hooks/useAlarmSound.ts` exposes `play()` / `stop()` built on the Web Audio API (an
`AudioContext` + oscillator, lazily created on first play to satisfy the browser's
user-gesture requirement). Nothing imports it yet — M17 will call `play()` when an alarm
targeting this `device_id` fires.

## Tooling

- **Editor:** Prettier is the default formatter with format-on-save (`.vscode/settings.json`,
  including the `[typescriptreact]` language block for `.tsx`). Recommended extensions:
  ESLint, Prettier, Tailwind CSS IntelliSense, ES7+ React snippets, Error Lens.
- **Pre-commit:** `local` hooks run `eslint --fix` and `prettier --write` over the
  frontend. They need `frontend/node_modules` (`npm ci`) and a POSIX shell (Git Bash on
  Windows). See `.pre-commit-config.yaml`.
- **CI:** the `frontend` job in `.github/workflows/ci.yml` runs `npm ci` → `npm run lint`
  → `npm run format:check` → `npm run build` (the build includes `tsc -b`, so it type-checks
  too). Node comes from `frontend/.nvmrc`.
- **Node pinning:** `frontend/.nvmrc` (read by nvm/fnm and CI) + `engines` in
  `package.json` (declared requirement) + optional `engine-strict=true` in `.npmrc` to
  turn warnings into errors.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `npm`/`node` not recognized | Node.js not installed, or old terminal — install the LTS, open a **new** terminal |
| `src/api/types.ts` import fails | Run `npm run types` (backend must be up) |
| "Connection failed — is the backend running on :8000?" | Backend not up, or the Vite proxy path changed — check `make dev-up` and `vite.config.ts` |
| Two sockets open in dev tools | That's StrictMode's mount/unmount/remount — the cleanup closes the first; only real if the second one *stays open* |
| Pre-commit frontend hooks fail on Windows | Missing `bash` (need Git Bash) or missing `frontend/node_modules` (run `npm ci`) |
