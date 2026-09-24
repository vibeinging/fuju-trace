FROM node:20-bookworm-slim AS console
WORKDIR /src/fuju-trace-console
COPY fuju-trace-console/package*.json ./
RUN npm config set registry https://registry.npmjs.org/ \
    && npm config set replace-registry-host always \
    && npm ci
COPY fuju-trace-console/ ./
RUN VITE_API=http npm run build

FROM rust:1.91-bookworm AS engine
WORKDIR /src
COPY fuju-trace-engine/ ./fuju-trace-engine/
COPY --from=console /src/fuju-trace-console/dist ./fuju-trace-engine/crates/fuju-trace-engine/console_dist
WORKDIR /src/fuju-trace-engine
RUN cargo build --release -p fuju-trace-engine --example server

FROM debian:bookworm-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=engine /src/fuju-trace-engine/target/release/examples/server /usr/local/bin/fuju-trace-server
ENV FUJU_TRACE_BIND=0.0.0.0:7878
EXPOSE 7878
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:7878/v1/healthz >/dev/null || exit 1
CMD ["fuju-trace-server"]
