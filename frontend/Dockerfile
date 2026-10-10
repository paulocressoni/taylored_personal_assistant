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

# --- build-time auth key (prod only) ---------------------------------
# The SPA must present the backend's shared key: VITE_API_KEY must equal the
# backend's ASSISTANT_API_KEY. Vite only inlines VITE_* vars present at build
# time. .dockerignore excludes .env* from the context, so a build ARG (fed
# from a GitHub secret at publish time) is the only way it gets in.
# Plain `docker build frontend/` with no arg => empty key => no auth baked
# (the same behavior as today for dev images / PR scans).
ARG VITE_API_KEY
ENV VITE_API_KEY=$VITE_API_KEY

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
# nginx:alpine base image can lag behind alpine's package repository -- it
# still ships pcre2 10.48-r0 after 10.49-r0 was published to v3.24/main.
# `apk upgrade` installs the already-released fixes, so the shipped image
# starts clean instead of shipping a known-vulnerable base.
# The explicit pcre2 floor guards CVE-2026-103111: it keeps the base from
# regressing below the fixed release, and it makes this layer re-run instead
# of being served from the build cache with the vulnerable package inside.
# The tiff floor does the same for CVE-2026-4775: libtiff arrives with the
# base through nginx's libgd, and one `apk upgrade` is not enough because the
# cached layer keeps serving the older tiff even after the fix is published.
RUN apk upgrade --no-cache \
    && apk add --no-cache 'pcre2>=10.49' 'tiff>=4.7.2'

# Replace the stock server block with ours (serves dist/ + proxies /ws,
# /sessions, /health, /openapi.json to the backend service).
COPY nginx.conf /etc/nginx/conf.d/default.conf

# dist/ from the builder is the ONLY app content the runtime image
# carries — no source, no node_modules, no build tooling.
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

# nginx:alpine ships busybox wget (no curl). Probe 127.0.0.1, NOT localhost:
# we replaced the stock default.conf, so nginx binds IPv4 ONLY (no [::]:80
# listener) — `localhost` resolves to ::1 first, which would make this
# healthcheck report UNHEALTHY forever even though nginx is serving fine.
# Also never probe /health: that path is proxied to the backend and would
# flap whenever the backend restarts.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
