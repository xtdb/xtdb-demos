FROM node:22-alpine

RUN corepack enable

WORKDIR /app

COPY ui/package.json ui/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

EXPOSE 5173
CMD ["pnpm", "dev", "--port", "5173"]
