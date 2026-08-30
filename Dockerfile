FROM python:3.12-slim AS runtime

RUN pip install --no-cache-dir uv==0.11.3
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["threegpp-kg", "serve", "--host", "0.0.0.0", "--port", "8000"]
