# Client compatibility acceptance

The delivery target is a single-process, single-machine, local-disk service
using path-style URLs and SigV4. Compatibility means the explicit API subset
below with the pinned clients and settings in this document. It does not mean
all of Amazon S3, all commands of each client, or the complete Ceph suite.
New client versions need a new acceptance run.

## Pinned tools

| Tool | Version / identity | Accepted workflow |
| --- | --- | --- |
| Python SDK | botocore 1.44.0, boto3 1.44.0 | Six SDK suites; 100 MiB multipart SHA-256 round trip |
| AWS CLI | 2.22.35, official Linux x86-64 archive | Bucket CRUD, file upload/download, ListObjectsV2, copy, presign, multipart completion, batch delete |
| MinIO `mc` | RELEASE.2025-04-16T18-13-26Z, commit `b00526b153a31b36767991a4f5ce2cced435ee8e` | TLS: `mb`, `cp --disable-multipart`, `cat`, `stat`, recursive `ls`, server-side copy, individual `rm`, `rb` |
| Ceph `s3-tests` | commit `5b3987116eca0b4b8fa57d6b10f3db57bab196b6` | 33 original selected cases in `s3tests/functional/test_s3.py` |
| Nginx | 1.28.0-alpine, immutable image digest in the lock file | TLS signing, raw URI preservation, presign, Range, header/URI/body rejection |

[compatibility-lock.json](../scripts/compatibility-lock.json) records archive
SHA-256 digests, the complete Nginx image digest, the upstream commit, and all
33 Ceph node names. [compatibility-requirements.txt](../scripts/compatibility-requirements.txt)
pins the Python environment used for acceptance (Python 3.12.3 on Linux x86-64).
These are tested versions, not a claim that they are the latest releases.

## Reproduce

Requires the project's MoonBit toolchain, Python 3.12+, `uv`, Git, `unzip`,
OpenSSL, and a working Docker daemon. The TLS test uses Linux host networking;
run it on Linux x86-64. The installer downloads external tools into `/tmp`,
verifies binary archive hashes, checks out the pinned upstream source, and
creates an isolated Python environment. Existing upstream checkouts must be
clean and at the pinned revision. No client installation, credentials, or
data directory from the user's account is used.

```bash
/usr/bin/python3 scripts/install_compatibility.py
/usr/bin/python3 scripts/compatibility_regression.py \
  --report /tmp/moon-s3-compat/acceptance.json

for suite in presigned copy multipart cors version policy; do
  NO_PROXY='*' no_proxy='*' /tmp/moon-s3-compat/python/bin/python \
    "scripts/${suite}_regression.py" --sdk || break
done
python3 scripts/delete_regression.py
```

Both installer and runner accept `--tools` and `--s3-tests` to choose other
test directories. The runner also accepts `--suite aws|mc|ceph|proxy`.
It builds the native server and starts a new authenticated service on an
ephemeral loopback port with disposable storage for each suite. It verifies
the AWS CLI version, `mc` binary hash/version, the pristine upstream commit,
and SDK versions. Nonzero commands, failed assertions, skipped Ceph cases,
and missing/extra Ceph test results fail acceptance. The JSON report records
tool identities, case selection, suite outcomes, duration, source revision,
and whether the tested working tree had changes. Tests run on an uncommitted
tree must be interpreted together with that patch.

Ceph test bodies and fixtures are unmodified. Its required main, alternate,
tenant and IAM fixture credentials all refer to the service's single account.
The selected cases exercise no cross-account permissions or tenant isolation.
Upstream cleanup uses real version listing and batch deletion; its optional
IAM cleanup is rejected by this server and caught by the upstream fixture.
This configuration provides no evidence of IAM compatibility.

## API boundary

| Area | Supported subset | Boundary |
| --- | --- | --- |
| Authentication | SigV4 header and presigned GET/HEAD/PUT | Path-style; no SigV2, STS, AWS chunk signatures or signed trailers |
| Buckets | Create, list, head, delete; `GET ?location` | One local region: location is empty (`us-east-1`); no region provisioning or redirects |
| Listing | ListObjectsV2, prefix, delimiter, pagination, start-after, URL encoding, optional owner | `fetch-owner=true` returns the service owner `moonbit`; V1 marker pagination is outside the contract |
| Objects | PUT/GET/HEAD/DELETE, ETag, Content-Type, one Range, read/write conditions | No arbitrary metadata, tags, ACL, storage classes, encryption or response override parameters |
| Copy | CopyObject, source conditions, COPY/REPLACE Content-Type, versioned source | No UploadPartCopy; source limit 5 GiB; no tags/metadata propagation |
| Multipart | Initiate, upload/list parts, list uploads, complete, abort, startup recovery, expiration | 1–10000 parts; 5 MiB minimum except last; parts must fit request-body budget |
| Batch delete | `POST /bucket?delete`, 1–1000 keys, optional VersionId, Quiet, per-key errors | At most 1 MiB XML; no conditional object fields, MFA, object-lock retention or batch-wide conditions |
| Integrity | Content-MD5 and request CRC32 verification | Optional read checksum mode accepted without response checksum fields; other algorithms/trailers rejected |
| Settings | Bucket CORS, enable/suspend versioning, restricted public-read policies | General IAM, ACLs and lifecycle configuration outside the contract |

Batch deletion validates the entire document before deleting any object.
After validation, entries execute sequentially under the mutation lock using
the same durable per-object transactions and webhook events as individual
deletes. It is not one atomic transaction: a process crash may leave a prefix
of the batch committed, and per-object storage failures appear as `Error`
entries in a 200 response. Quiet suppresses successes, not errors. Retry
with explicit VersionId when exact historical deletion matters; an unversioned
request to an enabled bucket creates a new delete marker on each retry.
XML support accepts the documented SDK structure, default S3 namespace,
optional declaration and XML text entities. DTDs, prefixed namespaces,
extensions and unknown elements are rejected.

For SDKs set `signature_version="s3v4"`,
`s3={"addressing_style": "path"}`,
`request_checksum_calculation="when_required"` and
`response_checksum_validation="when_required"`. AWS CLI acceptance uses the
equivalent checksum environment variables and a private path-style config.
Do not supply encryption, metadata, storage-class or optional checksum flags.

`mc` uses streaming signatures when uploading over HTTP. Use HTTPS via
[the Nginx example](deployment.md#tls-reverse-proxy), file inputs with known
sizes, and `cp --disable-multipart`. `--checksum`, streaming stdin uploads,
mirror/sync, multipart `mc` transfers and advanced management commands are
outside this acceptance. `mc` may probe `?object-lock`; the server explicitly
rejects it with 501 and this pinned client continues ordinary file operations.
The local TLS regression uses a temporary self-signed certificate and
`--insecure`; use a trusted certificate with verification in deployments.

## Acceptance results

Verified for the implementation in this change on Linux x86-64:

- AWS CLI: upload/download bytes match, Unicode/space/plus keys round trip,
  copy, presigned GET, multipart completion and batch cleanup succeed.
- `mc`: TLS file round trip and server-side copy match bytes, listing/stat,
  individual deletion and bucket removal succeed.
- Ceph: 33 selected cases pass, with zero failures, errors or skips. They
  cover ListObjectsV2 options, ETags, empty objects, CRUD, copy, multipart
  abort and successful/unsatisfiable Range requests.
- Nginx: Host and encoded/repeated-slash paths preserve signatures, Range
  and presigned URLs work; excessive individual/aggregate headers return
  400, excessive request lines 414, and oversized body declarations 413.
- Local regression additionally covers malformed batch XML without partial
  deletion, authentication, MD5 mismatch, Quiet, long keys, version deletion,
  marker restoration and persistence after restart.

This acceptance does not establish long-duration load behavior, machine power
loss durability, complete AWS compatibility, or production readiness. Remaining
delivery work is to publish versioned release artifacts/checksums, perform a
deployment and backup/restore drill on the delivery machine, and archive the
final release's acceptance reports against that exact commit.
