# v0.2.0 preview

Object and multipart-part uploads now stream to private disk staging with
16 KiB per-upload buffer windows. SHA-256, MD5 and requested CRC32 digests
are calculated incrementally, and verified bodies publish through the existing
transaction/version machinery. Interrupted uploads are cleaned up on error,
timeout and startup recovery. Shared disk staging is bounded by
`MOON_S3_MAX_STAGED_BYTES` (512 MiB by default); the memory budget can now be
smaller than individual upload requests. Control request bodies still buffer
in memory. Default request size remains 64 MiB, configurable up to 512 MiB.

A single-machine S3 subset implemented in MoonBit, with local disk storage,
SigV4 and presigned requests, multipart uploads, versioning, copy, batch
deletion, CORS, restricted public-read policies, durable event webhooks,
offline backup/restore, management CLI and an authenticated Web console.

The Linux x86-64 bundle includes the native server, Python management tools,
console assets, deployment examples, documentation and a source/build manifest.
Download the tarball, manifest and SHA256SUMS together; verify checksums before
extraction. See docs/releasing.md inside the archive for installation.

Compatibility is limited to the documented S3 subset and pinned clients;
see docs/compatibility.md. This preview is not production-ready. No general
IAM/ACL, encryption, distributed storage, AWS streaming signatures or online
backup support. Object/part uploads stage on disk; control bodies buffer
within configured memory limits. Keep data-root and bucket directories on
one filesystem and allow space for staging alongside committed storage.

Upgrade warning: long keys use a hash-based storage mapping. Older server and
admin versions cannot safely operate on that layout. Stop the service, take
and verify an offline backup with this version, and retain it before upgrading.
