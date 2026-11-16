# Bucket versioning

Versioning preserves overwritten objects and makes ordinary deletes reversible.
It uses the same single credential pair and path-style addressing as other APIs.
The supported subset is verified with botocore 1.44.0.

## Enable and suspend

Use `PUT /bucket?versioning` with:

```xml
<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Status>Enabled</Status>
</VersioningConfiguration>
```

`GET /bucket?versioning` returns the current configuration. A bucket that has
never used versioning returns no Status. Once configured, use `Suspended` to
pause version creation; there is no transition back to the unconfigured state.
Configuration XML supports the default S3 namespace or no namespace, an optional
XML declaration, and one Status element, up to 4 KiB. Prefixed namespaces,
arbitrary XML extensions, and MFA Delete are unsupported.

| Bucket state | PUT, CopyObject, multipart completion | DELETE without versionId |
| --- | --- | --- |
| Never configured | Replaces the ordinary object | Permanently removes the ordinary object |
| Enabled | Creates a unique version | Creates a new delete marker |
| Suspended | Replaces the `null` version | Replaces `null` with a delete marker |

Objects created before versioning retain the logical version ID `null`.
Enabling versioning does not rewrite their content. The first mutation preserves
the original null version before committing the new operation. Suspension keeps
existing unique versions; replacing a null version permanently removes that null
version. Multipart completion uses the bucket setting at completion time.

## Read, delete, and restore

- PUT, CopyObject, and multipart completion return `x-amz-version-id` for
  configured buckets.
- `GET /bucket/key?versionId=ID` and HEAD select a historical version, including
  `null`. Historical reads support ranges, conditions, and the original
  Last-Modified. SigV4 and presigned URLs sign the versionId query parameter.
- An ordinary GET/HEAD uses the latest version. A latest delete marker returns
  404 with `x-amz-delete-marker: true` and its version ID. A request selecting
  a delete marker returns 405 with the marker headers and Last-Modified.
- `DELETE /bucket/key?versionId=ID` permanently removes that version or marker.
  Removing the latest version exposes its predecessor. Missing IDs return 204;
  GET/HEAD of a missing ID returns NoSuchVersion.
- CopyObject accepts `x-amz-copy-source: /bucket/key?versionId=ID` to restore
  historical content as a new destination version, including to the same key.
  The response includes the selected source and destination version IDs.

This server issues opaque 64-character lowercase hexadecimal version IDs and
accepts those IDs or `null`. It does not import foreign S3 version identifiers.
Ordinary object listings hide objects whose latest version is a delete marker.
A bucket cannot be deleted until all versions and delete markers are removed.

## List versions

`GET /bucket?versions` returns Version and DeleteMarker entries with Key,
VersionId, IsLatest, and LastModified. Live versions also include ETag, Size,
and StorageClass. Keys sort by UTF-8 bytes, with versions newest first per key.

Supported parameters are `prefix`, `delimiter`, `max-keys` (0–1000),
`key-marker`, `version-id-marker`, and `encoding-type=url`. Use NextKeyMarker
and NextVersionIdMarker to resume a truncated listing. A version marker requires
a matching key marker and must identify an existing version in the listing.
CommonPrefixes count against max-keys. Pages are not a snapshot across concurrent
mutations; clients should avoid changing versions while traversing a listing.

## Storage and recovery

Historical data and records live under `.versions` inside each bucket. Current
object files remain a cache for ordinary listings and backward-compatible
storage inspection. The latest live content is stored twice, so plan disk
capacity for all history plus the current cache. Reads use immutable historical
files, and mutations are serialized within the single server process.

Version publication and permanent deletion use durable journals. Startup replays
committed operations, restores the current cache, and removes uncommitted staging
directories. Invalid committed records prevent startup. Regression coverage
includes staged and already-published journal replay, interrupted deletion,
suspended null replacement, multipart cleanup, restart, and backup/restore.
Power-loss and hardware-failure guarantees still require external validation.

The offline [administration tool](administration.md) includes historical content,
delete markers, and bucket settings in checks, backups, and restores. Version
retention can consume disk indefinitely: lifecycle expiration, object lock,
replication, MFA Delete, and bulk version deletion are not implemented.

Run `python3 scripts/version_regression.py`, optionally with `--sdk`, to verify
the supported workflows.
