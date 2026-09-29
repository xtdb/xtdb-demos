FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.29 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY app.py demo.py assessment.sql ./
COPY static ./static
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
