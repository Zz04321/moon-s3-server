# Deployment and operations

The delivery target is one native process on one machine, storing objects on a
local disk and serving path-style S3 URLs. Authentication, storage recovery and
the data-directory lock apply equally to native and container deployments.

## Configuration files

Copy `deploy/config.example.json` to a local file, then start the server:

```bash
moon build cmd/main --target native --release
MOON_S3_CONFIG=./deploy/config.example.json \
  MOON_S3_ACCESS_KEY=local-access MOON_S3_SECRET_KEY=replace-this-secret \
  _build/native/release/build/cmd/main/main.exe
```

`MOON_S3_CONFIG` explicitly selects a UTF-8 JSON file of at most 64 KiB. With no
file selected, the existing defaults apply. Keys are lowercase versions of the
environment variable suffix: `root`, `listen`, `access_key`, `secret_key`,
`cors_origin`, `multipart_ttl_seconds`, `webhook_url`, `webhook_secret`,
`webhook_timeout_ms` and `shutdown_timeout_ms`. The three timeout/lifetime
settings are JSON integers; all other settings are strings. Unknown keys,
duplicate keys, invalid types and unreadable files fail startup.

Environment variables override matching file values, including empty strings;
omitted file keys use the defaults documented in the README. Relative paths
are resolved against the process working directory. Credentials must both be
nonempty or both absent. A file can contain credentials, but environment or
platform secret injection is preferable; restrict file permissions if used.
Configuration is loaded once at startup; restart to apply changes.

`MOON_S3_SHUTDOWN_TIMEOUT_MS` defaults to `10000` and accepts 100–300000 ms.
`MOON_S3_WEBHOOK_TIMEOUT_MS` accepts 100–60000 ms; multipart TTL accepts
1–31536000 seconds. Invalid ranges fail startup.

## Shutdown and restart

Send SIGTERM or SIGINT to begin draining. The server closes its listener and
idle connections, completes requests already being handled, and closes their
connections instead of accepting another keep-alive request. After the drain
deadline, remaining requests are cancelled. Interrupted storage transactions
are handled by recovery on the next startup. A successful shutdown exits with
status 0, including when the drain deadline expires; inspect `shutdown_timeout`
logs to detect interrupted requests. Clients should retry interrupted operations
using their usual S3 retry rules and check the resulting object state.

Webhook delivery stops during shutdown; unacknowledged events stay in the
durable queue and may be delivered again after restart. Receivers must deduplicate
by event ID. The data-directory lock remains held until request tasks and the
webhook worker have stopped. Do not start an offline backup before the process
has exited. The deadline bounds request draining; filesystem cleanup or blocked
disk I/O can extend total shutdown time. Configure an external stop timeout with
headroom (Compose uses 15 seconds for a 10-second drain).

## Logs and metrics

The server writes one flushed JSON record per line to stdout. Every record has
`timestamp_ms` (Unix milliseconds), `level` and `event`. Startup and shutdown
records carry a message. `request_completed` records include `request_id`,
`method`, HTTP `status`, `duration_ms` and `cancelled`. The response header
`x-moon-request-id` allows correlation within one process lifetime. IDs reset
on restart. Status 0 means no response headers were successfully sent.
Request paths, query strings, authorization headers, bodies and credentials
are excluded from request logs. Lifecycle diagnostics can include configured
listen/data paths and storage recovery errors. Route stdout to your log
collector and configure retention there.

`GET /healthz` is an unauthenticated liveness check. It becomes available after
recovery completes. During draining, the listener is closed.
`GET /metrics` (or `HEAD`) exposes Prometheus text and uses the same SigV4
authentication as S3 requests. With authentication disabled, metrics are public.
Query parameters and other methods are unsupported. A scraper must sign its
requests or use a trusted signing proxy; ordinary unauthenticated Prometheus
scraping is rejected when credentials are enabled.
The top-level paths `/healthz` and `/metrics` are reserved for operations;
use other names for S3 buckets.

Metrics include uptime, draining state, open connections, active requests,
completed requests by HTTP status, request exception count, and total/count of
request durations. Counters reset on restart. The metrics request itself is
active while rendering and becomes completed after its response. Durations
include body reads, lock waits and response writes. These metrics describe the
HTTP process; they do not report disk capacity or persisted object inventory.
Labels contain only HTTP statuses, never bucket names or object keys.

## Docker

The Dockerfile packages a native **Linux** release binary built locally with
the tested MoonBit toolchain (`moon 0.1.20261102`). Build on the same CPU
architecture as the image target, with glibc compatible with Debian Bookworm
(2.36 or older). This keeps compiler installation separate from deployment;
the image contains only the server and runtime dependencies. Cross-compilation
and macOS/Windows host binaries are not supported by this packaging workflow.

```bash
moon build cmd/main --target native --release
docker build -t moon-s3-server:local .
docker run -d --name moon-s3 --stop-timeout 15 \
  -p 127.0.0.1:9000:9000 -v moon-s3-data:/data \
  -e MOON_S3_ACCESS_KEY=local-access \
  -e MOON_S3_SECRET_KEY=replace-this-secret moon-s3-server:local
docker logs moon-s3
docker stop moon-s3
```

Run `python3 scripts/docker_smoke.py` after building the image to check its
health check, nonroot execution, signed requests, shutdown and persistence.

Or export both credential variables and use `docker compose up --build -d`
after building the binary. Compose requires credentials, uses a named volume
and binds the host port to localhost. The image runs as UID/GID 10001 and uses
SIGTERM as its stop signal. Ensure bind-mounted data directories are writable
by that UID. The health check expects the default container port 9000; override
it if changing the container listen address. HTTPS webhook delivery requires
trusted CA certificates, included in the image.

To load a file inside Docker, mount it read-only under `/etc/moon-s3/config.json`
and set `MOON_S3_CONFIG` to that path. The image's `MOON_S3_ROOT=/data` and
`MOON_S3_LISTEN=0.0.0.0:9000` override file settings unless explicitly changed.
The Docker build context includes only the compiled binary; local proposals,
credentials, configuration files and storage data are excluded.

For access beyond localhost, place a TLS reverse proxy in front of the server
and preserve Host, path, query and signed headers. Set request size/time limits
to match multipart usage. Uploads are currently buffered in memory, so bound
concurrent uploads and part sizes through the proxy and container resource
limits. Keep data on a durable local filesystem and follow
[offline backup and restore](administration.md). The Web console runs as a
separate process; see [console deployment](console.md).
