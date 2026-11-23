# gateway: NestJS BFF. See shared/CONTRACTS.md for API contracts and env vars.
FROM node:20-alpine AS build
WORKDIR /app
COPY gateway/package*.json ./
RUN npm ci
COPY gateway/ ./
RUN npm run build

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY gateway/package*.json ./
RUN npm ci --omit=dev
COPY --from=build /app/dist ./dist

EXPOSE 3000

CMD ["node", "dist/main.js"]
