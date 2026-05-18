# Jak clients/rust: builder z volumenem (./docker_build.sh), runner tylko COPY publish-docker/.
#
#   cd clients/nodejs && ./docker_build.sh
#
FROM node:22-bookworm AS dev-env
WORKDIR /workspace

FROM dev-env AS builder
CMD ["/bin/sh", "-c", "sleep 1 && exec /usr/bin/bash /workspace/Docker_start.sh"]

FROM node:22-bookworm-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY publish-docker/ .
CMD ["/bin/sh", "-c", "sleep 2 && exec node index.mjs"]
