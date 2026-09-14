FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.29 /uv /uvx /bin/

WORKDIR /app

# Dependencies resolve from the lockfile alone, so this layer only rebuilds when the
# lockfile does — the source arrives by bind mount at runtime.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

ENV PATH="/venv/bin:$PATH"

EXPOSE 8000
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
