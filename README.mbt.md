# moon-s3-server

A small S3-compatible object storage server written in MoonBit, for local
development, CI, and private deployments. Stores data on local disk and uses
path-style URLs.

**Status:** early development (`v0.2`); not production-ready.

## Features

- Bucket and object CRUD; batch deletion; ListObjectsV2 with prefixes, pagination, and delimiters.
- Optional SigV4 authentication and presigned GET / HEAD / PUT URLs.
- Multipart uploads with resumable parts, pagination, abort, and expiration.
- Stream object/part uploads to disk with bounded buffers and incremental integrity checks.
- Range downloads, conditional reads/writes, ETags, and Content-Type metadata.
- Server-side CopyObject across buckets, with source conditions and COPY / REPLACE Content-Type.
- Write/delete transaction recovery, consistent concurrent reads, and a data directory lock.
- Persistent bucket CORS rules with preflight matching; a `GET /healthz` endpoint.
- Offline storage checks, SHA-256 backups, and verified restores with multipart resume.
- Bucket versioning, historical reads, delete markers, version listing, and version restore by copy.
- Bucket public-read/private switching through a restricted S3 Bucket Policy API.
- Durable JSON webhooks for object creation and deletion, with retries and optional HMAC signatures.
- Standard-library management CLI for files, versions, access, CORS, multipart cleanup, and offline administration.
- Web console with a separate admin login, file browsing, transfers, version restore, and bucket settings.
- JSON configuration, graceful shutdown, structured logs, authenticated metrics, and Docker packaging.

Fixed-version client acceptance covers botocore 1.44.0 (including a 100 MiB
multipart round trip), AWS CLI 2.22.35, MinIO `mc` over TLS, and selected Ceph
`s3-tests`. See the [compatibility matrix and reproduction steps](docs/compatibility.md).

## Quick start

Requires the [MoonBit CLI](https://www.moonbitlang.com/download/)
(tested with `moon 0.1.20261102`; CI uses the hash-locked `0.1.20261117` snapshot).

```bash
git clone https://github.com/zzSmall6/moon-s3-server.git
cd moon-s3-server
moon build cmd/main --target native

MOON_S3_ROOT=./data MOON_S3_LISTEN=127.0.0.1:9000 \
  _build/native/debug/build/cmd/main/main.exe
```

In another terminal, create a bucket and upload a file:

```bash
curl --noproxy '*' -X PUT http://127.0.0.1:9000/demo
printf 'Hello MoonBit!\n' > hello.txt
curl --noproxy '*' -X PUT --data-binary @hello.txt http://127.0.0.1:9000/demo/hello.txt
curl --noproxy '*' http://127.0.0.1:9000/demo/hello.txt
curl --noproxy '*' 'http://127.0.0.1:9000/demo?list-type=2'
```

Or run `bash scripts/smoke.sh` for a self-contained example.
For the management CLI, run `python3 scripts/moon_s3.py --help`;
see [command-line management](docs/cli.md) for examples.
To open the [Web console](docs/console.md), set `MOON_S3_CONSOLE_PASSWORD`
and run `python3 scripts/console.py`; visit `http://127.0.0.1:9001`.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MOON_S3_CONFIG` | Unset | JSON configuration file; environment variables override file values |
| `MOON_S3_ROOT` | `./data` | Data directory |
| `MOON_S3_LISTEN` | `127.0.0.1:9000` | Listen address |
| `MOON_S3_ACCESS_KEY` | Unset | Enable SigV4 authentication |
| `MOON_S3_SECRET_KEY` | Unset | Secret paired with the access key |
| `MOON_S3_CORS_ORIGIN` | `*` | Fallback origin for buckets without CORS rules |
| `MOON_S3_MULTIPART_TTL_SECONDS` | `604800` | Upload lifetime from creation; 1–31536000 seconds |
| `MOON_S3_WEBHOOK_URL` | Unset | Enable object-event delivery to an HTTP(S) endpoint |
| `MOON_S3_WEBHOOK_SECRET` | Unset | Sign webhook bodies with HMAC-SHA256 |
| `MOON_S3_WEBHOOK_TIMEOUT_MS` | `5000` | Callback timeout; 100–60000 milliseconds |
| `MOON_S3_SHUTDOWN_TIMEOUT_MS` | `10000` | Drain active requests on SIGINT/SIGTERM; 100–300000 milliseconds |
| `MOON_S3_MAX_REQUEST_BYTES` | `67108864` | Maximum request body; 1–536870912 bytes |
| `MOON_S3_MAX_BUFFERED_BYTES` | `134217728` | Shared upload windows and control-body buffers; 16384–536870912 bytes |
| `MOON_S3_MAX_STAGED_BYTES` | `536870912` | Shared in-flight upload payload on disk; 1–536870912 bytes |
| `MOON_S3_MAX_CONNECTIONS` | `128` | Maximum simultaneous connections; 1–4096 |
| `MOON_S3_REQUEST_TIMEOUT_MS` | `30000` | Header-read and request-handling deadlines; 100–3600000 ms |

Set both credentials to nonempty values to enable authentication; leaving both
unset allows anonymous access. Use a trusted network and a TLS reverse proxy
for deployments beyond localhost.
See [deployment and operations](docs/deployment.md) for configuration files,
Docker/Compose, shutdown behavior, JSON logs, and authenticated `GET /metrics`.
With credentials enabled, buckets default to private. [Bucket policies](docs/access-control.md)
can grant anonymous object reads while keeping writes and management authenticated.

SDK clients should use SigV4, path-style addressing, and disable optional
checksums (`request_checksum_calculation="when_required"` and
`response_checksum_validation="when_required"` in botocore).
For temporary links, configure matching credentials and use
`python3 scripts/presign.py --help`.
Use the SDK's `copy_object` operation to copy without downloading; copy headers
must be signed when authentication is enabled.
Use `put_bucket_cors` / `get_bucket_cors` / `delete_bucket_cors` for bucket rules;
see [CORS configuration](docs/cors.md) for matching and browser behavior.
See [versioning](docs/versioning.md) for enable/suspend behavior and historical object operations.
See [event webhooks](docs/events.md) for payloads, retries, and receiver deduplication.

## Limits and recovery

- Object keys support 1–1024 UTF-8 bytes; invalid URL encoding is rejected.
- Object/part uploads use 16 KiB buffer windows and disk staging; control bodies stay in memory.
  Per-request and shared memory/disk limits apply; choose multipart sizes accordingly.
  Parts are numbered 1–10000 and must be at least 5 MiB except the last.
- Multipart state survives restarts. Expiration cleanup runs at startup and
  on multipart traffic; upload activity does not extend the lifetime.
- One process per data directory. Transaction recovery runs before serving
  requests; corrupt or incomplete committed records prevent startup.
- Recovery is tested on Linux for process crashes; power loss and hardware
  faults need further validation.
- Bucket policies support public object-read grants only; no general IAM/ACL,
  encryption, or online backups yet. Only single-range downloads are supported.
- No STS tokens, AWS streaming signatures, checksums beyond MD5/CRC32, UploadPartCopy,
  or delimiter grouping for multipart upload listings.
- CopyObject supports sources up to 5 GiB and Content-Type metadata only;
  tags, ACLs, and custom metadata are unsupported.
- Unsupported subresources, encryption and extra checksum options are rejected.
  Content-MD5 and request CRC32 are verified; Content-Type is limited to 1024 bytes.

## Backup and restore

Stop the server first. On Linux with Python 3.9+, run:

```bash
python3 scripts/admin.py check --root ./data
python3 scripts/admin.py backup --root ./data --output ./backup.tar
python3 scripts/admin.py restore --archive ./backup.tar --root ./restored-data
```

Restore requires a new directory and verifies all files before publishing it.
See [administration](docs/administration.md) for guarantees, limits, and recovery.

## Development

```bash
moon test --target native
moon info && moon fmt
bash scripts/smoke.sh
for suite in auth list object presigned multipart storage copy admin cors version policy event cli console operations safety delete streaming; do
  python3 "scripts/${suite}_regression.py"
done
```

Regression scripts use Python's standard library. With botocore installed,
add `--sdk` to the presigned, multipart, copy, CORS, version, or policy regression script for SDK checks.
The console regression also supports `--browser` with Playwright Chromium installed.

Pushes to `main` and pull requests run native CI and all local regression suites.
External client acceptance and release packaging are manual Actions workflows.
See [release bundles and validation](docs/releasing.md) for installation and publishing.
Run the [deployment recovery drill](docs/recovery-drill.md) before delivery or storage migration.

## License

[Apache-2.0](LICENSE).
