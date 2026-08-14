# Multi-stage build: git is only needed to fetch the fleet-day-sim
# workload package, so it stays out of the runtime image.
FROM python:3.12-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml ./
COPY simq ./simq
RUN pip install --no-cache-dir --prefix=/install ".[fleet]"

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 simq
COPY --from=builder /install /usr/local
ENV SIMQ_ARTIFACT_DIR=/data/artifacts \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data/artifacts && chown -R simq /data
USER simq
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/healthz')" || exit 1
CMD ["uvicorn", "simq.api:app", "--host", "0.0.0.0", "--port", "8000"]
