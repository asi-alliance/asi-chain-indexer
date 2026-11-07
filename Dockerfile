# ==============================
# Stage 1: Build Python dependencies
# ==============================
FROM python:3.13.18-slim-bookworm AS python-builder

RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock requirements-dev.lock ./
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.lock

# regenerates the gRPC client from protos/; installed without --user so it never reaches runtime
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.lock
COPY protos/ /protos/
RUN mkdir -p /stubs/scalapb && python -m grpc_tools.protoc \
    -I /protos \
    --python_out=/stubs --grpc_python_out=/stubs \
    /protos/DeployServiceV1.proto \
    /protos/DeployServiceCommon.proto \
    /protos/CasperMessage.proto \
    /protos/RhoTypes.proto \
    /protos/ServiceError.proto \
    /protos/scalapb/scalapb.proto


# ==============================
# Stage 2: Runtime image
# ==============================
FROM python:3.13.18-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 indexer

WORKDIR /app

COPY --from=python-builder /install /usr/local

COPY . .
COPY --from=python-builder /stubs/. src/grpc_stubs/

RUN chown -R indexer:indexer /app

USER indexer

ENV PYTHONPATH=/app
ENV PATH="/home/indexer/.local/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:9090/health || exit 1

CMD ["python", "-m", "src.main"]

# Explicit test target; the final/default production image contains runtime packages only.
FROM runtime AS test
USER root
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.lock
USER indexer

FROM runtime AS production
