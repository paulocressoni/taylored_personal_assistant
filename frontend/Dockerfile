# frontend/Dockerfile
# ------------------------------------------------------------------
# ONE file, THREE named stages — the environment selects the target
# EXPLICITLY; automation never relies on Docker's implicit default:
#
#   dev       Node + Vite dev server (hot reload). Chosen by the dev
#             compose overlay via `build.target: dev`; source is
#             bind-mounted over /app at runtime.
#   builder   Node compile stage (deps + `npm run build` -> dist/).
#             Internal ONLY — never selected as a target. Named
#             `builder` to mirror backend/Dockerfile's vocabulary.
#   prod      nginx serving dist/ + proxying API/WS to the backend.
#             The deployable artifact. CI/publish pass `--target prod`
#             explicitly; it is ALSO the last stage, so a bare
#             `docker build frontend/` still yields it — a safe
#             fallback for interactive builds only.
#
# Node version pinned to frontend/.nvmrc (24.19.0). nginx pinned to the
# current stable line (1.30.x) for reproducible production images.
# ------------------------------------------------------------------

# ============================ TARGET: dev ============================
FROM node:24.19.0-alpine AS dev

WORKDIR /app

# Layer-cache strategy: install from the manifests FIRST so dependency
# edits never re-run against changed source, and vice versa.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173

# Vite dev server (host/port live in vite.config.ts).
CMD ["npm", "run", "dev"]

# ========================= STAGE: builder ============================
# Compiles the app to static files (dist/). Node only lives here — it
# never ships in the runtime image. Not a deployment target: selecting
# `--target builder` returns a Node image with no nginx, which is NOT a
# runnable artifact.
FROM node:24.19.0-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# `npm run build` runs `tsc -b && vite build` — type-checks THEN bundles,
# so a type error already fails the image build.
RUN npm run build

# ========================= TARGET: prod ==============================
# Serve the built bundle with nginx and reverse-proxy API/WebSocket
# traffic to the backend. A STATIC SPA behind a same-origin proxy needs
# NO build-time env vars / ARGs: runtime configuration (the backend
# host) lives in nginx.conf + compose, never baked into the image. If a
# real build-time knob is ever needed, add an ARG here only then.
FROM nginx:1.30.4-alpine AS prod

# Pull the latest alpine security fixes at build time. The stock
# nginx:alpine base image can lag behind alpine's package repository.
# `apk upgrade` installs the already-released fixes, so the shipped image
# starts clean instead of shipping a known-vulnerable base.
RUN apk upgrade --no-cache

# Replace the stock server block with ours (serves dist/ + proxies /ws,
# /sessions, /health, /openapi.json to the backend service).
COPY nginx.conf /etc/nginx/conf.d/default.conf

# dist/ from the builder is the ONLY app content the runtime image
# carries — no source, no node_modules, no build tooling.
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

# nginx:alpine ships busybox wget (no curl). Check the STATIC server
# itself — never /health, which is proxied to the backend and would flap
# whenever the backend is down even though nginx is perfectly fine.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD wget -q -O /dev/null http://localhost/ || exit 1
