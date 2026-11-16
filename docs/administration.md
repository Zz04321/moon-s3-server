# Offline storage administration

`scripts/admin.py` provides storage checks, backups, and restores using Python
3.9+ and its standard library on Linux. Stop the server before checking or
backing up a data directory. The tool takes the same exclusive `.lock` file
lock as the native server and fails immediately if the directory is in use.

## Check and back up

```bash
python3 scripts/admin.py check --root ./data
python3 scripts/admin.py backup --root ./data --output ./backup.tar
```

Both commands print a JSON report with bucket, current object, historical version, delete marker, and unfinished upload
counts. Failures print a JSON error to stderr and exit with status 1. Checking
does not repair or delete data; it may create the `.lock` file if absent.

Checks validate the current storage layout, object/metadata pairs, encoded
keys, ETag syntax, bucket creation records, bucket CORS rules, and unfinished upload manifests.
Version checks validate bucket settings, version records, unique IDs and ordering,
historical content, and consistency between the latest version and current cache.
Ordinary object content is checked against its MD5 ETag. A completed multipart
object's ETag cannot be recomputed without its original parts; this produces a
warning, while backup SHA-256 still protects its content during transfer.
Partially written upload artifacts are preserved and reported as warnings.

Backups include empty buckets, object content and metadata, all historical versions
and delete markers, versioning settings, bucket CORS rules, creation records,
and unfinished multipart uploads. They preserve file and directory modification
times, including the fallback creation time used by older buckets. The process
lock and environment variables, including credentials, are excluded. Save server
configuration and credentials separately through your usual secure process.

The archive is an uncompressed tar containing a versioned JSON manifest and
regular data files. The manifest records each path, size, modification time in
nanoseconds, and file SHA-256. Files are copied in bounded chunks and checked
against the manifest before publishing the backup. Backup output must be
outside the source directory; existing paths are never overwritten. A temporary
archive is synced and then published with an atomic link. Backup files use
permissions `0600` and are not encrypted or authenticated; store them securely.

Unfinished object transactions and unknown storage files cause checks and
backups to fail. For transaction leftovers, start the server to run its recovery
first, then stop it and retry. A malformed committed transaction requires
investigation; the administration tool does not guess or discard it. Symlinks,
hard links, devices, and other special files are rejected.

## Restore into a new directory

```bash
python3 scripts/admin.py restore --archive ./backup.tar --root ./restored-data
python3 scripts/admin.py check --root ./restored-data
MOON_S3_ROOT=./restored-data _build/native/debug/build/cmd/main/main.exe
```

Use the same credentials and other environment settings as the original server
if you want existing signed requests to remain usable. Confirm application
reads and listings before switching clients to the restored service.

Restore requires a nonexistent destination and an existing parent directory.
It never replaces an existing data root, even an empty one or a symlink.
It stages data in a private sibling directory, verifies every size and SHA-256,
checks the storage layout, and restores modification times. Only then does it
publish the directory using Linux `renameat2(RENAME_NOREPLACE)` and sync its
parent. Validation failures remove staging data and leave the destination
absent. A missing `renameat2` implementation fails without publishing data.

Absolute paths, parent traversal, duplicate or unlisted archive entries,
symlinks, hard links, devices, missing files, and corrupt checksums are rejected.
Files restore with permissions `0600` and directories with `0700`, owned by the
user running the command; original ownership and permission bits are not copied.
Run restore as the account that will run the service.

The default maximum total restored file size is 1 TiB, and the maximum manifest
size is 16 MiB. Adjust the file limit explicitly when needed:

```bash
python3 scripts/admin.py restore --archive ./backup.tar --root ./restored-data \
  --max-bytes 2199023255552
```

Allow space for the full restored data alongside any original directory and
the archive. Multipart upload IDs and initiation times are preserved; expired
uploads remain subject to normal server cleanup when the restored server starts.
An upload's lifetime does not restart when restored.

## Scope and failure recovery

This is an offline backup of the current single-node storage format, not an
online snapshot, replication service, or repair command. Do not edit the data
directory directly while administration runs. The lock is advisory and cannot
prevent writes by unrelated tools. Checksums detect accidental corruption; they
do not prove that an archive came from a trusted source.

Data and directory syncing follow the same local Linux filesystem assumptions
as the server. An unexpected process kill may leave a private
`.moon-s3-backup-*` or `.moon-s3-restore-*` staging path; these are not completed
backups or restored data roots. After confirming no administration process is
using them, they may be removed and the command retried. A kill after publication
can leave a valid output despite no success report; inspect it before retrying.

Run the complete regression with:

```bash
python3 scripts/admin_regression.py
```
