# v0.1.0 preview

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
backup support. Upload bodies are buffered within configured limits.

Upgrade warning: long keys use a hash-based storage mapping. Older server and
admin versions cannot safely operate on that layout. Stop the service, take
and verify an offline backup with this version, and retain it before upgrading.
