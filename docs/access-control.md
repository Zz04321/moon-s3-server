# Bucket public-read access

With `MOON_S3_ACCESS_KEY` and `MOON_S3_SECRET_KEY` configured, buckets default
to private. The configured credential pair owns all buckets and can manage
them using SigV4. A restricted S3 Bucket Policy API grants anonymous object
reads. It is verified with botocore 1.44.0.

When both credentials are unset, the server retains its anonymous development
mode: all supported operations remain accessible, regardless of bucket policy.
Configure credentials before relying on private/public isolation.

## Make a bucket public

Use `PUT /demo?policy` with this UTF-8 JSON body, signed as the owner:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublicRead",
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::demo/*"
  }]
}
```

Using a configured botocore S3 client:

```python
import json

client.put_bucket_policy(Bucket="demo", Policy=json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": "*",
        "Action": "s3:GetObject",
        "Resource": "arn:aws:s3:::demo/*",
    }],
}))
policy = client.get_bucket_policy(Bucket="demo")["Policy"]
client.delete_bucket_policy(Bucket="demo")
```

PUT and DELETE return 204. GET returns the stored policy as application/json;
an absent policy returns 404 NoSuchBucketPolicy. DELETE is idempotent.
Deleting the policy makes the bucket private again. An empty Statement array
also grants no anonymous access. Policy changes never remove owner access.

## Permission boundaries

| Request | Anonymous access with a matching grant |
| --- | --- |
| Object GET/HEAD without versionId | Requires `s3:GetObject` |
| Object GET/HEAD with versionId, including `null` | Requires `s3:GetObjectVersion` |
| Range and conditional reads | Follow the selected object-read permission |
| Bucket listing, HEAD bucket, version listing | Always requires owner authentication |
| Upload, delete, copy, multipart operations | Always requires owner authentication |
| Policy, CORS, and versioning configuration | Always requires owner authentication |

To publish historical versions as well as current content, set Action to
`["s3:GetObject", "s3:GetObjectVersion"]`. A grant only for GetObjectVersion
allows explicit historical reads and keeps ordinary current reads private.
The usual version/delete-marker status codes still apply to authorized reads.

Anonymous object reads accept no query parameters except versionId. Requests
containing Authorization, x-amz-date, x-amz-security-token, or X-Amz-* query
authentication must pass signature validation; invalid, expired, and tampered
signatures cannot fall back to public access. Valid owner signatures and
presigned object URLs continue to work for private buckets.

CORS is independent of access control. Preflight may succeed on a private
bucket; the subsequent request still needs permission. Use bucket CORS rules
when public content needs browser access from selected origins.

## Supported policy subset

- Version must be `2012-10-17`. Optional document Id and statement Sid are strings.
- Statement is an object or an array with up to 20 entries; an empty array is private.
- Each statement must have Effect `Allow`, Principal `"*"` or `{"AWS":"*"}`,
  and Action containing only `s3:GetObject` and/or `s3:GetObjectVersion`.
  Action may be a string or a nonempty array of strings.
- Resource must be `arn:aws:s3:::BUCKET/*` for the configured bucket, as a string
  or a single-element array. Grants cover every key in that bucket.
- Policies are limited to 20 KiB. Duplicate JSON fields, unknown fields,
  conditions, Deny, other principals, wildcard actions, foreign bucket resources,
  and prefix-specific resources return 400 MalformedPolicy. An invalid PUT
  preserves the previous policy.

This is a public-read subset, not general IAM policy evaluation. ACLs,
PublicAccessBlock, policy status APIs, per-user roles, and per-key permissions
are not implemented.

## Persistence and revocation

Policy writes sync a temporary file and atomically replace the bucket's `.policy`
file. Deletion syncs the bucket directory. Startup removes unfinished temporary
policy files and rejects corrupt committed policies. The offline administration
tool validates policies and includes them in backups and restores.

Policy checks and object snapshot opening share the mutation lock with policy
updates. After a revoke completes, new anonymous reads are denied; downloads
that already opened an authorized snapshot may finish. Revocation does not
invalidate valid owner presigned URLs or copies already downloaded by clients.
Power-loss and hardware-failure behavior still needs external validation.

Run `python3 scripts/policy_regression.py`, optionally with `--sdk`, to verify
private/public switching, historical permissions, signature boundaries,
concurrency, restart, and backup/restore.
