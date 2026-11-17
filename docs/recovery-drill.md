# Deployment and recovery drill

Run the drill on the intended delivery host before cutover. The automated
drill uses a verified Linux x86-64 release, a Docker daemon and disposable
named volumes. It binds ephemeral ports to loopback and never opens an
existing deployment or its volumes. It builds the bundle's runtime image;
no MoonBit installation is needed. Host Python needs tar extraction filters
(Python 3.10.12+ or 3.12+). The backup/restore helper runs the bundled admin
tool as UID/GID 10001 in `python:3.11.9-slim-bookworm`, matching the service.
The report records the actual helper/runtime image IDs.

## Automated rehearsal

Download the release tarball, `manifest.json` and `SHA256SUMS` together.
From a source checkout:

```bash
python3 scripts/deployment_drill.py /path/to/release/*.tar.gz \
  --report /tmp/moon-s3-drill.json
```

New bundles also include `scripts/deployment_drill.py`, `release_bundle.py`
and `s3_client.py`; the same command works from an extracted bundle. The
archive is still an argument because checksums and the embedded manifest are
verified before any container starts. The report path must be new. A dirty
development bundle is rejected unless explicitly using `--allow-dirty`.
Use `--admin-image` to select an approved Python image available on the host.

The sequence checks:

1. Archive and file SHA-256, source revision and clean release identity.
2. Nonroot service startup, authenticated requests and private management.
3. Two versions of a long Unicode key, content type, delete marker, version
   listing, CORS, public-read policy, and one persisted 5 MiB upload part.
4. Refusal to back up live storage, followed by graceful shutdown and a
   consistent offline inventory/backup as the service UID.
5. An additional original-volume write after the snapshot, to make the
   recovery-point boundary observable.
6. Restore into a fresh volume. Inventory matches the snapshot; attempts to
   overwrite it and to restore corrupted archive bytes are rejected.
7. Restored object/version SHA-256, marker, CORS and policy behavior. The
   post-snapshot write is absent, as expected for offline backups.
8. Completion of the saved multipart upload and new writes on restored storage.
9. Stop the restored instance, start the original volume and verify its
   original content, unfinished upload and post-snapshot write. Writes made
   only on the restored volume remain absent from the original.

All service containers, volumes and the unique image tag created by the
drill are removed, including on ordinary exceptions. The report records
cleanup failures. A forced termination of the Python driver or Docker daemon
may leave resources named `moon-s3-drill-*`; inspect resource ownership and
running containers before removing leftovers. Downloaded helper/base images
and Docker build cache are retained. No existing image tags are overwritten.

JSON evidence includes host/kernel/Docker versions, runtime image IDs, release
revision and archive digest, harness file digests, per-step outcomes/durations,
source/restored inventories and backup digest. It excludes generated service
credentials. Recovery time measures the local restore, negative safety checks,
startup and snapshot verification; it is a small-fixture observation, not an
RTO commitment. No hardware power cut, disk failure, DNS migration, sustained
load or production traffic is simulated. Webhook replay is covered by the
event regression suite, not this drill's fixture.

The **Release bundle** workflow runs the same drill and retains its JSON/log
in `deployment-drill-COMMIT` artifacts. A failed drill prevents release bundle
publication. Retain downloaded evidence beyond Actions' 30-day expiration.

## Actual service cutover

The automated rehearsal does not choose your production volumes or move
client traffic. For an existing Docker deployment:

1. Record the current image ID/version, configuration and source volume name
   (`docker inspect CONTAINER`). Record free disk space for source, backup
   and a full restored copy. Keep credentials/configuration separately; data
   backups exclude them. Schedule an outage and stop writers.
2. Stop the service gracefully (`docker stop CONTAINER`). Confirm exit status
   0 and `shutdown_drained` in logs. Run the matching bundled `admin.py check`
   and `backup` under UID/GID 10001 with the source volume mounted read/write
   for the exclusive lock. The service must remain stopped.
3. Store the backup outside the source volume. Verify and retain its SHA-256.
   Restore with `admin.py restore` into a nonexistent directory on a new
   volume, using UID/GID 10001. Restore publishes directories as 0700 and
   files as 0600, so root-owned restores will not run under the service UID.
4. Start one isolated instance against the new root with matching credentials,
   CORS/webhook settings and resource limits. Keep the original instance
   stopped. Compare inventory, key/version hashes, policy/CORS behavior and
   a representative unfinished multipart upload before opening writes.
5. Switch the proxy/client endpoint to the restored instance, then verify
   authentication, reads, new writes, metrics and a fresh offline backup.
   A hostname change requires new presigned URLs. Monitor storage capacity,
   response errors and webhook delivery on the new service.
6. Retain the original volume and backup until sign-off. To roll back, stop
   writers and the restored instance, then switch back to the original volume
   and image. Account for writes accepted since cutover before doing so;
   they are not automatically merged into the original volume. Never run two
   instances against the same data directory.

Run helper commands using the Python image and the extracted bundle's scripts.
For example, after substituting your own volume names and while the service
is stopped:

```bash
docker run --rm --user 10001:10001 \
  --mount type=volume,src=SOURCE_VOLUME,dst=/data \
  --mount type=volume,src=BACKUP_VOLUME,dst=/backup \
  --mount "type=bind,src=$PWD/scripts,dst=/tools,readonly" \
  python:3.11.9-slim-bookworm python /tools/admin.py backup \
  --root /data --output /backup/snapshot.tar
```

The backup volume's directory must already be writable by UID 10001; create
and initialize a dedicated backup location beforehand. Existing archive paths
are never overwritten. See [administration](administration.md) for restore
limits, corruption refusal and interrupted-transaction handling.
