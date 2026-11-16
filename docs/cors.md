# Bucket CORS configuration

Each bucket can override the global `MOON_S3_CORS_ORIGIN` fallback through the
S3 `PutBucketCors`, `GetBucketCors`, and `DeleteBucketCors` operations:

| Operation | Request | Success |
| --- | --- | --- |
| PutBucketCors | `PUT /bucket?cors` with CORS XML | 200 |
| GetBucketCors | `GET /bucket?cors` | 200 with CORS XML |
| DeleteBucketCors | `DELETE /bucket?cors` | 204, including when already absent |

These operations use the same authentication as other S3 operations. With
credentials configured, use signed requests. Browser preflight `OPTIONS`
requests do not require a signature. CORS controls browser access to responses;
it does not grant object permissions or replace authentication.

## Configure through an SDK

Using a botocore client configured as described in the README:

```python
client.put_bucket_cors(
    Bucket="demo",
    CORSConfiguration={"CORSRules": [{
        "ID": "web-upload",
        "AllowedOrigins": ["https://app.example.com"],
        "AllowedMethods": ["GET", "HEAD", "PUT"],
        "AllowedHeaders": ["Content-Type", "Authorization", "x-amz-*"],
        "ExposeHeaders": ["ETag", "Last-Modified", "Content-Range"],
        "MaxAgeSeconds": 600,
    }]},
)
rules = client.get_bucket_cors(Bucket="demo")["CORSRules"]
client.delete_bucket_cors(Bucket="demo")
```

Equivalent XML for PUT:

```xml
<CORSConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <CORSRule>
    <AllowedOrigin>https://app.example.com</AllowedOrigin>
    <AllowedMethod>GET</AllowedMethod>
    <AllowedMethod>PUT</AllowedMethod>
    <AllowedHeader>Content-Type</AllowedHeader>
    <AllowedHeader>x-amz-*</AllowedHeader>
    <ExposeHeader>ETag</ExposeHeader>
    <MaxAgeSeconds>600</MaxAgeSeconds>
  </CORSRule>
</CORSConfiguration>
```

## Matching and responses

- Rules are evaluated in document order; the first rule matching origin,
  method, and all requested preflight headers wins. Fields are not combined
  across different rules.
- Origins are case-sensitive and may contain at most one `*`. Allowed header
  names match case-insensitively and may also contain one `*`. Supported methods
  are GET, HEAD, PUT, POST, and DELETE; OPTIONS is the preflight mechanism.
- Preflight requires `Origin` and `Access-Control-Request-Method`. If present,
  every `Access-Control-Request-Headers` name must be allowed. A match returns
  204, allowed methods and requested headers, plus optional max age. No matching
  rule returns `403 AccessForbidden`; malformed preflight returns 400.
- Actual requests match origin and method. A matching rule exposes only its
  configured headers. Exact or pattern origins echo the request origin and
  include `Access-Control-Allow-Credentials: true`; a literal `*` emits `*`
  without an allow-credentials header. Requests without a match or without
  Origin still run normally and receive no CORS permission headers.
- Matching applies to errors, conditional responses, Range downloads, multipart
  operations, and copies as well as ordinary success responses. Responses vary
  by Origin, Access-Control-Request-Method, and Access-Control-Request-Headers.
- Without bucket configuration, the global origin, standard exposed object
  headers, and permissive preflight behavior remain the fallback. Deleting a
  configuration restores this fallback. Use explicit restrictive bucket rules
  when you need to limit browser origins.

## Persistence and limits

Configurations are UTF-8 XML, at most 64 KiB, with 1–100 rules. Each rule needs
at least one origin and method. Optional IDs must be unique, nonempty, and at
most 255 UTF-16 code units. Max age is an integer from 0 to 2147483647.
ExposeHeader entries must be concrete header names, without wildcards.
If supplied, Content-MD5 is checked against the exact PUT body.

The supported XML subset allows an optional XML declaration, a root with no
namespace or the standard S3 default namespace, simple text fields, predefined
entities, and numeric character references. Prefixed namespaces, comments,
CDATA, DTDs, custom entities, field attributes, and unknown elements are
unsupported. Invalid requests leave the previous configuration intact.

PUT writes a synced temporary file, atomically replaces `bucket/.cors`, and
syncs the bucket directory. Startup discards `.cors.tmp` leftovers. Configurations
survive restarts and are validated and preserved by offline backup/restore.
Storage errors return 500; corrupt persisted rules fail closed for the affected
bucket. Signed PUT/DELETE `?cors` requests can replace or remove a corrupt
configuration without first loading it. A write interrupted before its response
may have committed; GET the configuration before retrying.

Run `python3 scripts/cors_regression.py`, or add `--sdk` with botocore installed,
to verify the API, preflight, response headers, restart, and backup behavior.
