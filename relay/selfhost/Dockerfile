# Build/acceptance image. Public ingress is configured separately, never by this image.
FROM node:24-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY selfhost/runtime/package.json selfhost/runtime/package-lock.json ./selfhost/runtime/
RUN npm ci --prefix selfhost/runtime --ignore-scripts --no-audit --no-fund
COPY src ./src
COPY test ./test
COPY selfhost ./selfhost
COPY tsconfig.json ./
ENV WORKERD_BIN=/app/selfhost/runtime/node_modules/@cloudflare/workerd-linux-64/bin/workerd
RUN mkdir -p dist && node node_modules/typescript/bin/tsc -p selfhost/tsconfig.json
CMD ["node", "test/selfhost-runtime.mjs"]
