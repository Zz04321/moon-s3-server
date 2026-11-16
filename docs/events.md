# Object event webhooks

The server can send JSON HTTP callbacks for object creation and deletion. This
is a server-wide webhook extension; it does not implement S3 SNS/SQS/Lambda
notification configuration APIs.

## Configuration

```bash
MOON_S3_ROOT=./data MOON_S3_LISTEN=127.0.0.1:9000 \
MOON_S3_WEBHOOK_URL=https://receiver.example/hooks/s3 \
MOON_S3_WEBHOOK_SECRET=replace-with-your-shared-secret \
  _build/native/debug/build/cmd/main/main.exe
```

Set `MOON_S3_WEBHOOK_URL` to enable event creation and delivery for every bucket.
HTTP and HTTPS endpoints with DNS names or IPv4 addresses are supported; HTTPS
uses system certificate verification. User-info URLs, fragments, whitespace,
IPv6 literals, and unescaped non-ASCII URLs are rejected at startup. Query
strings on a callback path are supported. Redirects are never followed.

`MOON_S3_WEBHOOK_TIMEOUT_MS` limits each connection-and-response attempt,
including DNS and TLS, to 100–60000 milliseconds (default 5000). The endpoint is
operator configuration and is never accepted from incoming S3 requests.

If `MOON_S3_WEBHOOK_URL` is unset, new mutations create no events and existing
queued events remain on disk. Enabling it later resumes the queue. An optional
nonempty `MOON_S3_WEBHOOK_SECRET` enables HMAC-SHA256; setting a secret without
an endpoint is a configuration error. Use HTTPS when sending to remote services.

## Event payload

Each POST contains one record in a Records array, with Content-Type
`application/json` and a stable `X-Moon-S3-Event-Id` header:

```json
{
  "Records": [{
    "eventVersion": "2.1",
    "eventSource": "moon:s3",
    "eventTime": "2026-11-16T12:00:00.000Z",
    "eventName": "ObjectCreated:Put",
    "eventId": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "s3": {
      "bucket": {"name": "demo", "arn": "arn:aws:s3:::demo"},
      "object": {
        "key": "docs%2Fhello+world.txt",
        "size": 5,
        "eTag": "5d41402abc4b2a76b9719d911017c592",
        "versionId": "null"
      }
    }
  }]
}
```

The shape resembles an S3 event, with this project's `moon:s3` source and
eventId extension. It does not claim the full AWS notification schema.
Decode object keys using form URL decoding (`urllib.parse.unquote_plus` in
Python): spaces become `+`, and literal `+`, `%`, `/`, and Unicode are escaped.
Size is in bytes. ETags have no surrounding quotes. VersionId is present for
configured versioning or explicit version deletes and is absent for ordinary
unversioned mutations. Delete-marker creation has size zero and no ETag.

| Operation | Event name |
| --- | --- |
| PutObject, including overwrites | `ObjectCreated:Put` |
| CopyObject, including version restoration | `ObjectCreated:Copy` |
| Successful multipart completion | `ObjectCreated:CompleteMultipartUpload` |
| Ordinary object removal or permanent version/marker removal | `ObjectRemoved:Delete` |
| Versioned delete that creates a marker | `ObjectRemoved:DeleteMarkerCreated` |

Multipart initiation, parts, aborts, reads, bucket changes, failed authorization,
and rejected mutations do not create events. Deleting a nonexistent object or
version creates no event. Implicit replacement of a suspended null version
creates the new write/marker event, without a separate deletion event. Deleting
a bucket does not remove its already queued notifications.

## Receiver acknowledgments and signatures

Any HTTP 2xx response acknowledges the event. Every other status, connection
failure, or timeout leaves it queued for retry. The server checks response
headers and closes the connection without buffering the response body.
Callbacks run in the background, outside the storage mutation lock.

With a signing secret, `X-Moon-S3-Signature` contains `sha256=HEX`, where HEX is
the HMAC-SHA256 of the exact UTF-8 request body. Verify before parsing:

```python
import hashlib
import hmac
import json

expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, signature_header):
    raise ValueError("Invalid webhook signature")
record = json.loads(raw_body)["Records"][0]
```

Persist eventId with the receiver's work and return 2xx only after durable
acceptance. Repeated deliveries of an already accepted ID should return 2xx
without repeating the work. Signatures use the currently configured secret;
coordinate secret changes with the receiver.

## Delivery and recovery guarantees

When enabled, object mutations stage an event before publishing the object
transaction journal. Recovery completes the object mutation and moves its event
into a durable outbox before removing the journal. Only released events become
eligible for delivery. Queue storage failure makes the mutation return a storage
error; as with other interrupted transactions, an error response can still mean
the mutation committed and will be completed during recovery.

The queue is global under `MOON_S3_ROOT/.events`, independent of bucket lifetime.
Uncommitted event staging is discarded; committed journals and released pending
entries are recovered on startup. Corrupt committed events prevent startup.
An interrupted final queue release may need a restart to complete recovery.

Delivery is at least once under the existing local filesystem/process-crash
assumptions. A receiver may accept an event before the process crashes while
acknowledging it locally, so duplicates are expected. IDs and JSON bodies remain
stable across retries, restarts, and backups. Events are not guaranteed to arrive
in mutation order, and there is no exactly-once or power-loss guarantee.

A single worker sends up to 32 events per pass. Failed passes retry with
exponential delay from 1 to 60 seconds; successful passes poll again after one
second. Retry delay resets on restart. There is no automatic expiry or retry
limit. A failed endpoint can stall the queue and grow disk usage; monitor the
queue directory and the `queued_events` count from the offline administration
check. There is no per-bucket filtering, dead-letter queue, or live queue API.

Offline backups preserve queued events. Restoring a backup can redeliver events
previously accepted by a receiver. Endpoint and secret are not stored in the
queue or backup: pending events go to the endpoint configured when delivery
runs. See [administration](administration.md) before restoring into a deployment.

Run `python3 scripts/event_regression.py` to verify event coverage, signatures,
timeouts, retries, redirect refusal, process kills, journal replay, queue
corruption, and backup/restore.
