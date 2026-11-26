# BotTread console: read-only monitor of the bot's files + 4 request files. Build context = this folder.
FROM node:24-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:24-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:24-alpine AS run
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3300
WORKDIR /app
# standalone = server.js + only the node_modules it needs; static assets are copied next to it
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
# stamps each request with its socket address (lib/auth.ts fromLan); without it nobody is "inside the LAN"
COPY --chown=node:node socket-ip.cjs ./
# the image's `node` user is uid 1000: it must be allowed to write <BOTTREAD_ROOT>/cache/requests (the runner's user is uid 1000 too)
USER node
EXPOSE 3300
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD wget -q -O /dev/null "http://127.0.0.1:${PORT:-3300}/login" || exit 1
# Docker overwrites HOSTNAME with the container id at run time, so bind to 0.0.0.0 explicitly here
CMD ["sh", "-c", "HOSTNAME=0.0.0.0 PORT=${PORT:-3300} exec node -r ./socket-ip.cjs server.js"]
