# moon-s3-server

A small S3-compatible object storage server written in MoonBit, for local
development, CI, and private deployments. Stores data on local disk and uses
path-style URLs.

**Status:** early development (`v0.1`); not production-ready.

## Features

- Bucket and object CRUD; ListObjectsV2 with prefixes, pagination, and delimiters.
- Optional SigV4 authentication and presigned GET / HEAD / PUT URLs.
- Multipart uploads with resumable parts, pagination, abort, and expiration.
- Range downloads, conditional reads, ETags, and Content-Type metadata.
- Server-side CopyObject across buckets, with source conditions and COPY / REPLACE Content-Type.
- Write/delete transaction recovery, consistent concurrent reads, and a data directory lock.
- CORS and a `GET /healthz` endpoint.

Verified with botocore 1.44.0, including a 100 MiB multipart round trip with
SHA-256 validation. AWS CLI, MinIO `mc`, and Ceph `s3-tests` compatibility
testing is still pending.

## Quick start

Requires the [MoonBit CLI](https://www.moonbitlang.com/download/)
(tested with `moon 0.1.20261102`).

```bash
git clone https://github.com/Zz04321/moon-s3-server.git
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

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MOON_S3_ROOT` | `./data` | Data directory |
| `MOON_S3_LISTEN` | `127.0.0.1:9000` | Listen address |
| `MOON_S3_ACCESS_KEY` | Unset | Enable SigV4 authentication |
| `MOON_S3_SECRET_KEY` | Unset | Secret paired with the access key |
| `MOON_S3_CORS_ORIGIN` | `*` | Allowed CORS origin |
| `MOON_S3_MULTIPART_TTL_SECONDS` | `604800` | Upload lifetime from creation; 1–31536000 seconds |

Set both credentials to nonempty values to enable authentication; leaving both
unset allows anonymous access. Use a trusted network and a TLS reverse proxy
for deployments beyond localhost.

SDK clients should use SigV4, path-style addressing, and disable optional
checksums (`request_checksum_calculation="when_required"` and
`response_checksum_validation="when_required"` in botocore).
For temporary links, configure matching credentials and use
`python3 scripts/presign.py --help`.
Use the SDK's `copy_object` operation to copy without downloading; copy headers
must be signed when authentication is enabled.

## Limits and recovery

- Upload requests are buffered in memory; choose multipart sizes accordingly.
  Parts are numbered 1–10000 and must be at least 5 MiB except the last.
- Multipart state survives restarts. Expiration cleanup runs at startup and
  on multipart traffic; upload activity does not extend the lifetime.
- One process per data directory. Transaction recovery runs before serving
  requests; corrupt or incomplete committed records prevent startup.
- Recovery is tested on Linux for process crashes; power loss and hardware
  faults need further validation. Stop the server or use a consistent
  filesystem snapshot for backups.
- No versioning, bucket policies, IAM/ACL, encryption, events, console, or
  backup commands yet. Only single-range downloads are supported.
- No STS tokens, AWS streaming signatures, extra checksums, UploadPartCopy,
  or delimiter grouping for multipart upload listings.
- CopyObject supports sources up to 5 GiB and Content-Type metadata only;
  version IDs, tags, ACLs, and custom metadata are unsupported.

## Development

```bash
moon test --target native
moon info && moon fmt
bash scripts/smoke.sh
for suite in auth list object presigned multipart storage copy; do
  python3 "scripts/${suite}_regression.py"
done
```

Regression scripts use Python's standard library. With botocore installed,
add `--sdk` to the presigned, multipart, or copy regression script for SDK checks.

## License

[Apache-2.0](LICENSE).
