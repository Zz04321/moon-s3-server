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
`webhook_timeout_ms`, `shutdown_timeout_ms`, `max_request_bytes`,
`max_buffered_bytes`, `max_connections` and `request_timeout_ms`. The timeout,
lifetime and resource-limit settings are JSON integers; all other settings are strings. Unknown keys,
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

Request bodies default to at most 64 MiB each, with a shared 128 MiB buffered
payload budget. Both limits can be configured up to 512 MiB; the shared budget
must be at least the per-request limit. This is a payload budget, not a process
RSS limit: parsing, hashing and buffer copies require additional memory. Fixed
Content-Length requests exceeding the per-request limit receive 413 immediately;
chunked requests are bounded while reading. Shared-budget exhaustion returns
503 SlowDown. Both responses close the connection. Limits apply before
authentication, and capacity is released on completion, error or timeout.

`max_connections` defaults to 128 (range 1–4096); excess connections are closed.
`request_timeout_ms` defaults to 30000 (range 100–3600000) and bounds each header
read, then separately bounds request handling including body reads, lock waits
and response writes. Timeout closes the connection; a write may have committed,
so clients must inspect object state before retrying a conditional operation.
Metadata rejects control characters, Content-Type over 1024 UTF-8 bytes and
parsed request headers/target over 16 KiB. The HTTP dependency parses headers
before this size check; also configure a raw header-size limit at the reverse
proxy. Upload part sizes must fit the configured request limit. Use
`python3 scripts/safety_regression.py` to verify rejection and cleanup behavior.

Unsupported S3 subresources (including tagging/ACL APIs), encryption, custom
metadata and checksum algorithms other than CRC32 receive 501 without changing objects.
Unknown query parameters are rejected; `x-id` is accepted for SDK operation IDs.
Content-MD5 is validated: malformed digests receive InvalidDigest and mismatches
receive BadDigest. Object keys and query strings require valid percent encoding
and UTF-8.
Request CRC32 uses the Base64 big-endian digest and is verified when present,
including SDK-generated required checksums on CORS and policy requests. A CRC32
algorithm declaration requires its matching digest. Response checksum generation
and checksum trailers remain unsupported.
Read requests may ask for `ChecksumMode=ENABLED`; optional response checksums
are omitted. This preserves SDK public-read behavior without claiming response
checksum generation.

Object PUT, CopyObject, DELETE and multipart completion support If-Match and
If-None-Match under the mutation lock. If-Match accepts strong ETags or `*`;
If-None-Match supports `*` only. Failed conditions return 412 without mutating
objects or consuming multipart uploads. Date conditions on writes and conditions
on multipart initiation/part upload/abort are rejected. Read conditions retain
their existing behavior.

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
`moon_s3_buffered_request_bytes` exposes the reserved body payload budget.

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
Before delivery or storage migration, run the [deployment recovery drill](recovery-drill.md)
and retain its release-bound report; see the [acceptance checkpoint](acceptance.md).

## TLS reverse proxy

[deploy/nginx.conf](../deploy/nginx.conf) is a complete Nginx configuration
tested with the image digest in [compatibility-lock.json](../scripts/compatibility-lock.json).
It expects the backend on `127.0.0.1:9000` and listens on `127.0.0.1:8443`.
Install a trusted certificate at `/etc/nginx/tls/fullchain.pem` and its key at
`/etc/nginx/tls/privkey.pem`. Change the TLS listen address to the intended
interface for remote access; keep the backend bound to loopback or a private
container network. Run `nginx -t -c /absolute/path/to/deploy/nginx.conf` before
loading it. When Nginx runs in a separate container, replace the upstream with
the S3 service address; `127.0.0.1` refers to that container unless using host
networking on Linux.

The proxy preserves the original Host (including port) and raw request URI;
do not add a URI suffix to `proxy_pass`, rewrite paths, merge slashes in the
upstream URL, or replace Host with the backend hostname. Generate presigned
URLs against the public HTTPS origin. The sample disables access logs so
temporary credentials in query strings are not logged. Adapt error-log
collection and retention to your environment; Nginx diagnostics may contain
request targets.

The proxy allocates a 1 KiB initial header buffer and at most two 4 KiB large
header buffers. A request line or individual header exceeding 4 KiB is rejected;
many smaller headers are also bounded by the available buffers. These limits
apply before the MoonBit HTTP parser, whose own check runs after parsing.
Tests cover both individual and aggregate header overflow, request line 414,
and body 413. The 64 MiB body limit matches the server's default per-request
limit. Request buffering is disabled; the backend still buffers accepted bodies
within its configured budget. Change both limits together if needed, including
multipart part sizes. Proxy timeouts measure inactivity; server deadlines
still bound each header read and request handler.

`mc cp` over HTTP uses AWS streaming signatures, which are unsupported. Use
this TLS proxy and ordinary file uploads with `--disable-multipart`; see the
[tested client workflows](compatibility.md). The regression generates a
temporary self-signed certificate and uses `mc --insecure` only for that local
test. Deployments should use trusted certificates and normal verification.
