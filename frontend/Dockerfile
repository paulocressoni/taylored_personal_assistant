# frontend/Dockerfile  (M12)
# Single-stage DEV image for the React frontend.
#   - Node version pinned to match frontend/.nvmrc (24.19.0)
#   - In dev, docker-compose.dev.yml bind-mounts the source on top and
#     masks /app/node_modules so the host's Windows deps never leak in.
# NOTE: bundling the BUILT app into the Python backend (the "unified
# package" goal) is a later milestone — this image ships the Vite dev
# server, which is exactly what M12 needs.

FROM node:24.19.0-alpine

WORKDIR /app

# Layer-cache strategy: copy manifests first, install, then source.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173

# Vite dev server (host/port live in vite.config.ts).
CMD ["npm", "run", "dev"]
