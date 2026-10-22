FROM node:24-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205
ENV PUPPETEER_SKIP_DOWNLOAD=true NODE_ENV=production WHATSAPP_HOME=/data
RUN apt-get update && apt-get install -y --no-install-recommends chromium ca-certificates fonts-liberation tini && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY src ./src
USER node
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "src/server.mjs"]
