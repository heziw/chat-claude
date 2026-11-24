# client: Angular SPA, built then served statically via nginx. apiBaseUrl
# (see src/environments/environment.ts) should be overridden at build time
# for a non-default gateway location -- see client/README.md "Env / config".
FROM node:26-alpine AS build
WORKDIR /app
COPY client/package*.json ./
RUN npm ci
COPY client/ ./
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist/client/browser /usr/share/nginx/html
COPY infra/docker/client.nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
