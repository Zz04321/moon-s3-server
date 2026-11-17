# Command-line management

`scripts/moon_s3.py` manages a running Moon S3 server and provides one entry point
for the existing offline storage tools. It requires Python 3.9+ and its standard
library. No AWS CLI, botocore, or other Python packages are needed.

Commands print one JSON object on stdout on success. Service, transport, and
filesystem failures print JSON on stderr and exit 1. Invalid command syntax
prints argparse usage and exits 2. Ctrl-C exits 130. Local check/backup/restore
require Linux, as described in [administration](administration.md).

## Connect

```bash
export MOON_S3_ENDPOINT=http://127.0.0.1:9000
export MOON_S3_ACCESS_KEY=your-access-key
export MOON_S3_SECRET_KEY=your-secret-key
python3 scripts/moon_s3.py health
python3 scripts/moon_s3.py bucket list
```

The endpoint defaults to `http://127.0.0.1:9000`. Global options must precede the
command: `--endpoint URL`, `--region NAME`, `--timeout SECONDS`, and `--anonymous`.
The timeout defaults to 30 seconds per socket operation, not a whole-command
deadline. Endpoints must be HTTP(S) origins without a path, query, fragment, or
embedded credentials. HTTPS uses Python's default certificate verification;
redirects are not followed. Requests go directly to the configured endpoint.

Credentials come from MOON_S3_ACCESS_KEY/MOON_S3_SECRET_KEY, falling back per
variable to AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY. Both must be nonempty or
both unset. Region uses AWS_DEFAULT_REGION or `us-east-1`. Credentials are
never command-line arguments. Static SigV4 authentication is supported; STS
session tokens, AWS profile files, and credential-provider discovery are not.

With both credentials unset, requests are anonymous. `--anonymous` explicitly
ignores configured credentials, useful for checking a public-read bucket.
Private bucket operations require credentials when authentication is enabled.

## Buckets and files

```bash
python3 scripts/moon_s3.py bucket create demo
python3 scripts/moon_s3.py object upload demo 'docs/hello world.txt' ./hello.txt
python3 scripts/moon_s3.py object list demo --prefix docs/ --page-size 100
python3 scripts/moon_s3.py object list demo --delimiter /
python3 scripts/moon_s3.py object head demo 'docs/hello world.txt'
python3 scripts/moon_s3.py object download demo 'docs/hello world.txt' ./download.txt
python3 scripts/moon_s3.py object copy demo 'docs/hello world.txt' demo copy.txt
python3 scripts/moon_s3.py object delete demo copy.txt
python3 scripts/moon_s3.py bucket delete demo
```

Keys are literal Unicode strings, not pre-encoded URLs; the client escapes
spaces, plus signs, percent signs, slashes, and other special characters for
transport. JSON output escapes control characters. Listing follows every page
automatically, with page sizes 1–1000, and returns one combined JSON result.
Object list prefixes and delimiter groups are supported. Very large listings
accumulate in client memory; pages are not a snapshot during concurrent writes.

Uploads accept regular local files. Content-Type is inferred from the filename
or set with `--content-type application/octet-stream`. Files up to 8 MiB use
PutObject; larger files use multipart upload. `--part-size-mib 16` changes both
the threshold and part size; allowed sizes are 5–512 MiB. Multipart memory use
is bounded by the selected part size, and single PUT hashing/transfer uses
bounded reads. Do not modify the source while uploading. At most 10000 parts
are supported; increase the part size for larger files.

A failed or interrupted multipart command attempts to abort the upload it
created. Network loss or a process kill can leave an unfinished upload: inspect
and abort it explicitly with the commands below. The CLI does not resume an
existing upload or retry writes automatically.

Downloads stream into a private temporary file beside the destination, verify
Content-Length when supplied, sync, and publish using an atomic hard link.
The destination must not exist, and its parent directory must already exist.
An existing file or symlink is never overwritten; interrupted or truncated
downloads do not publish partial files. The destination filesystem must support
hard links. A process kill can leave a `.moon-s3-download-*` temporary file.
Content-Length validation detects truncation; multipart ETags are not a full
content checksum. Use an application checksum for end-to-end integrity.

Deleting a nonempty bucket fails; no recursive deletion is performed. Object
delete permanently removes unversioned data, creates a marker when versioning
is enabled, or permanently removes a version when `--version-id ID` is given.

## Version history

```bash
python3 scripts/moon_s3.py versioning enable demo
python3 scripts/moon_s3.py versioning get demo
python3 scripts/moon_s3.py versions list demo --prefix docs/ --page-size 100
python3 scripts/moon_s3.py object download demo document.txt ./old.txt --version-id VERSION_ID
python3 scripts/moon_s3.py versions restore demo document.txt VERSION_ID
python3 scripts/moon_s3.py object delete demo document.txt --version-id VERSION_ID
python3 scripts/moon_s3.py versioning suspend demo
```

Restore copies a historical version onto the same key, creating a new version
according to the bucket's current setting. It preserves older versions.
HEAD and source-side copy also accept `--version-id`. Lists include delete
markers, latest flags, version IDs, sizes, and modification times, across all
pages. See [versioning](versioning.md) for null versions and delete semantics.

## Access and CORS

```bash
python3 scripts/moon_s3.py access get demo
python3 scripts/moon_s3.py access public demo
python3 scripts/moon_s3.py --anonymous object head demo document.txt
python3 scripts/moon_s3.py access public demo --include-versions
python3 scripts/moon_s3.py access private demo
python3 scripts/moon_s3.py cors put demo ./cors.xml
python3 scripts/moon_s3.py cors get demo
python3 scripts/moon_s3.py cors delete demo
```

Public access grants current object reads by default; `--include-versions`
also publishes history. Setting public replaces the bucket's existing policy
with the corresponding supported public-read grant. Private deletes the policy.
These commands do not change owner access. Without server credentials, all
operations remain anonymous development operations regardless of policy.
See [access control](access-control.md) and [CORS](cors.md) for their boundaries.
CORS PUT reads an XML file up to 64 KiB, and GET returns XML in a JSON field.

## Temporary links and upload cleanup

```bash
python3 scripts/moon_s3.py presign demo document.txt --expires 900
python3 scripts/moon_s3.py presign demo upload.txt --method PUT --expires 300
python3 scripts/moon_s3.py multipart list demo --prefix unfinished/ --page-size 100
python3 scripts/moon_s3.py multipart abort demo unfinished/file.bin UPLOAD_ID
```

Presign supports GET, HEAD, and PUT for 1–604800 seconds and requires credentials.
The JSON response contains a URL; treat it as a temporary credential. This
command uses the existing [presign utility](../scripts/presign.py), which remains
available for scripts that expect a plain URL. Version-specific presigning is
not exposed by this CLI. Multipart listing follows all pages. Abort removes
only the explicitly named key/upload ID, not completed objects or other uploads.

## Diagnostics, backup, and restore

```bash
python3 scripts/moon_s3.py health
python3 scripts/moon_s3.py check --root ./data
python3 scripts/moon_s3.py backup --root ./data --output ./backup.tar
python3 scripts/moon_s3.py restore --archive ./backup.tar --root ./restored-data
```

Health checks HTTP liveness only. Check performs the offline integrity audit,
including objects, history, permissions, and queued events. Stop the server
before check or backup; the exclusive data lock prevents concurrent offline
administration. Restore requires a new directory. Offline commands invoke the
existing admin tool and retain its validation, limits, and JSON reports. Global
HTTP options and credentials do not affect offline operations.

Run `python3 scripts/cli_regression.py` to verify the full workflow, including
multipart failure cleanup, historical restores, permission checks, unsafe
endpoint rejection, and atomic handling of interrupted downloads.
