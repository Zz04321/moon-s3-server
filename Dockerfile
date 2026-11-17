FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libgcc-s1 libssl3 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data /etc/moon-s3 \
    && chown 10001:10001 /data

COPY _build/native/release/build/cmd/main/main.exe /usr/local/bin/moon-s3-server
RUN chmod 755 /usr/local/bin/moon-s3-server

USER 10001:10001
WORKDIR /data
ENV MOON_S3_ROOT=/data MOON_S3_LISTEN=0.0.0.0:9000
EXPOSE 9000
VOLUME ["/data"]
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl --noproxy '*' --fail --silent http://127.0.0.1:9000/healthz || exit 1
ENTRYPOINT ["/usr/local/bin/moon-s3-server"]
