# Two-stage build: Node builds the dashboard, uv runs the backend.
FROM node:24-alpine AS web
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv sync --frozen --no-dev
COPY --from=web /app/frontend/dist frontend/dist
VOLUME ["/app/data"]
EXPOSE 8080
CMD ["uv", "run", "--no-sync", "spacebrains"]
