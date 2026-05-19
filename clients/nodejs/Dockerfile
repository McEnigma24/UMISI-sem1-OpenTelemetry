# Obraz runner: npm install --omit=dev + index.mjs z kontekstu (bez publish-docker/ z hosta).
#
#   docker compose build client-nodejs
#
FROM node:22-bookworm AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm install --omit=dev

FROM node:22-bookworm-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=deps /app/node_modules ./node_modules
COPY package.json package-lock.json ./
COPY index.mjs ./
CMD ["/bin/sh", "-c", "sleep 2 && exec node index.mjs"]
