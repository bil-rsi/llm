# syntax=docker/dockerfile:1
FROM python:3.13-slim-trixie AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

FROM base AS deps
RUN pip install "uv>=0.5,<1"
WORKDIR /src
COPY pyproject.toml uv.lock* ./
RUN if [ -f uv.lock ]; then uv sync --frozen --no-dev --no-install-project; else uv sync --no-dev --no-install-project; fi

FROM deps AS dev
RUN if [ -f uv.lock ]; then uv sync --frozen --no-install-project; else uv sync --no-install-project; fi
COPY src ./src
COPY tests ./tests
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/src/src
CMD ["pytest", "-q"]

FROM base AS runtime
COPY --from=deps /opt/venv /opt/venv
COPY src /app/src
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app/src
RUN groupadd -g 10001 aip && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin aip && install -d -m 1777 /run/aip
USER 10001:10001
WORKDIR /app
EXPOSE 8090
CMD ["uvicorn", "aiplatform.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8090", "--no-server-header", "--timeout-graceful-shutdown", "10"]
