# Delivery acceptance checkpoint

This is an evidence checkpoint for the v0.1.0 preview, not final production
sign-off. The release source tested here is
`738a6c59c1064f9505fdcebb1019c6becba6b618`; later source or toolchain changes
need their own reports. The deployment harness was developed after that source
commit; its three file hashes are recorded in the JSON evidence.

## Verified evidence

| Gate | Result | Evidence |
| --- | --- | --- |
| Native CI | Passed | [CI run](https://github.com/zzSmall6/moon-s3-server/actions/runs/44037872194): 35 unit tests, 17 local suites, release extraction validation |
| External clients | Passed | [Compatibility run](https://github.com/zzSmall6/moon-s3-server/actions/runs/44037935669): AWS CLI, TLS `mc`, 33 Ceph cases, proxy limits, six SDK suites including 100 MiB multipart |
| Release build | Passed | [Release workflow](https://github.com/zzSmall6/moon-s3-server/actions/runs/44037943265): clean source, packaged binary/CLI, checksum validation, Docker smoke |
| Deployment recovery rehearsal | Passed on the local Linux host | [Machine-readable report](acceptance/deployment-738a6c5.json): 11 steps, source/restored inventory equality, resumable upload, corruption refusal and original-volume rollback |

The deployment rehearsal uses the downloaded artifact from the Release build
run above, not a locally rebuilt substitute. Its tarball SHA-256 is
`ac25a5037565f1252d26320c294152e8964e23b040aab79f991bd186251bfbc2`.
The original source and restored snapshot both contain one bucket, one current
object, three historical versions, one delete marker and one unfinished
multipart upload. The harness compares bytes/hashes and Content-Type for two
versions of a long Unicode key, policy/CORS behavior and multipart part ETag.
It completes that upload after restore and confirms rollback leaves restored
writes out of the original volume.

The observed local restore-to-verified duration was 2.503 seconds for this
small fixture, including negative restore checks. This is not an RTO promise
or a benchmark. Kernel, Docker and image IDs, archive/backup SHA-256 digests,
per-step timings and cleanup status are in the JSON. Generated credentials,
object contents and backup archives are not committed.

## Remaining delivery gates

- Run [the drill](recovery-drill.md) on the actual delivery host with its
  intended filesystem, capacity, user permissions, Docker/network settings
  and operational backup destination. Current evidence is a local rehearsal;
  no production host or client traffic has been changed.
- Validate the intended TLS endpoint, secrets management, monitoring, event
  receiver deduplication and restore responsibility with the operator. Confirm
  the accepted [API boundary](compatibility.md), downtime and recovery point.
- Choose the final source commit and rerun CI, compatibility and release
  gates on that exact commit. Download and retain artifacts/reports before
  Actions retention expires. Include the new workflow's recovery report.
- Review the preview release notes and publish the versioned GitHub Release
  with the matching tarball, manifest, checksums and acceptance evidence.

Power loss, real disk failure, distributed durability and sustained production
load remain outside the evidence. Full S3 compatibility is outside the target
scope. The preview remains explicitly not production-ready.
