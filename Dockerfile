FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS builder
WORKDIR /build
COPY requirements/runtime.lock requirements/runtime.lock
RUN pip wheel --require-hashes --wheel-dir /wheels -r requirements/runtime.lock

FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/home/gateway/.local/bin:$PATH
RUN groupadd --gid 10001 gateway && useradd --uid 10001 --gid gateway --create-home gateway
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/* && rm -rf /wheels && pip uninstall -y pip && rm -rf /usr/local/lib/python3.13/site-packages/pip*
WORKDIR /app
COPY --chown=gateway:gateway alembic alembic
COPY --chown=gateway:gateway src src
COPY --chown=gateway:gateway alembic.ini pyproject.toml ./
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "src.gateway.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
