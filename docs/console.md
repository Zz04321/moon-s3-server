# Web storage console

The console is a separate Python 3.9+ process that manages the running native
MoonBit server. It uses the standard-library S3 client shared with the CLI and
serves local HTML, CSS, and JavaScript without a frontend build or CDN.

## Start

Start the native server as described in the README, then open another terminal:

```bash
export MOON_S3_ENDPOINT=http://127.0.0.1:9000
export MOON_S3_ACCESS_KEY=your-access-key
export MOON_S3_SECRET_KEY=your-secret-key
export MOON_S3_CONSOLE_PASSWORD=choose-a-long-random-admin-password
python3 scripts/console.py
```

Open `http://127.0.0.1:9001` and enter the administrator password. The console
password must have at least 16 characters and is independent of the S3 access
and secret keys. Credentials stay in the console process; the browser receives
a session cookie; the S3 secret key never leaves the backend. Shared signed
links naturally include the access-key ID and a temporary signature. The same AWS credential and region
environment fallbacks as the [CLI](cli.md) are supported.

The console can connect to an anonymous development server without S3 keys;
signed temporary links still require keys. Setting a console password does not
enable authentication on the native S3 server. Configure S3 credentials there
to enforce private buckets and authenticated writes.

Options are `--endpoint`, `--listen` (default `127.0.0.1`), `--port` (default
9001), and `--origin`. The endpoint must be an HTTP(S) origin. Keep the console
directory alongside scripts when packaging or copying the repository.

## Workspace

- Create buckets, select a bucket, and delete empty buckets.
- Browse files by literal key prefix, with 200 objects per page and Next page.
  Refresh or a new search starts at the first page.
- Upload a file with progress, choose its object key, inspect metadata,
  download content, copy across buckets, and delete objects with confirmation.
- Generate and copy a signed download link valid for 15 minutes. Temporary
  links use the configured S3 endpoint, which must be reachable by recipients.
- Enable or suspend versioning, view historical versions and delete markers,
  download or restore old content by copy, and permanently delete selected
  versions with confirmation.
- Inspect or replace public-read/private policies, optionally publishing
  history; edit CORS XML; inspect and abort unfinished multipart uploads.
- Sign out to revoke the current console session.

Bucket policies and versioning follow the existing [access](access-control.md)
and [versioning](versioning.md) rules. A bucket with retained versions, markers,
or unfinished uploads cannot be deleted. Restoring content creates a new
current version according to the bucket's current setting. No recursive or
bulk deletion is performed.

Browser uploads are limited to 64 MiB. They stage on the console host's temporary
disk and use the S3 client's multipart upload above 8 MiB. Failed multipart
uploads attempt to abort; a killed console process can leave an unfinished
upload that can be inspected and aborted later. Use the CLI for larger files
or resumable application workflows.

Downloads stage the complete object in a private temporary file before replying
as an attachment. Allow temporary disk space for the largest download and
concurrent transfers. Object content is never rendered as a console page.
The console does not expose the local filesystem, offline backup/restore, a
generic HTTP proxy, webhook configuration, or native server settings.

## Sessions and browser boundaries

Sessions expire after one hour, are kept in process memory, and are invalidated
when the console restarts. Cookies are HttpOnly, SameSite=Strict, and scoped to
`/api/`. HTTPS origins additionally set Secure. Neither browser localStorage nor
sessionStorage stores credentials or tokens. The UI clears file listings and
open-dialog content when signing out.

Every POST, including login, requires the exact configured Origin. Authenticated
POSTs additionally require a session-specific CSRF token. API reads require a
valid session, and all requests validate Host. The console has no CORS bypass;
bucket CORS settings do not affect the console's own API. Login permits up to
10 failed attempts per client IP per minute. At most 256 active sessions are
kept. Sessions provide one administrator role, with access to every bucket
available to the console's configured S3 credentials.

Static assets and API responses use no-store, Content Security Policy, frame
blocking, and MIME-sniffing protection. Object names are inserted as text;
HTML-shaped keys cannot create elements. Downloads use an attachment response
with an encoded filename. API actions are explicitly allowlisted; browser
requests cannot choose an upstream endpoint, read a local file, invoke offline
administration, or select arbitrary CLI operations.

## Reverse proxy

The default console listener is for local use. For a shared deployment, place
it behind a TLS reverse proxy and pass the exact public browser origin:

```bash
python3 scripts/console.py --listen 127.0.0.1 --port 9001 \
  --origin https://storage-console.example
```

Forward the original Host header unchanged and proxy the site at `/`, including
`/api/`, `/app.js`, and `/styles.css`. Do not serve it from a URL path prefix.
The console does not trust X-Forwarded-* headers; its origin is explicit
operator configuration. The proxy must supply TLS for HTTPS origins. If it
changes the client IP to a common proxy address, login failures share that IP's
rate limit. The console and S3 service remain separate origins; recipients of
temporary links need access to the S3 endpoint.

The console uses Python's threaded HTTP server with a 30-second connection
timeout and in-memory sessions. It has no multi-user management, persistent
sessions, browser-side direct S3 credentials, or server lifecycle controls.
Requests already authorized before logout may finish. Logout does not revoke
previously generated S3 temporary links.

## Verification

```bash
python3 scripts/console_regression.py
```

This runs standard-library HTTP checks against real native and console servers,
including login, CSRF/Host rejection, unsupported-operation rejection, upload and
download, key encoding, object/version pagination, policies, CORS, signed links,
logout, login limits, HTTPS cookie attributes, and session expiry.

For browser checks, install Playwright and its Chromium runtime in your test
environment, then run:

```bash
python3 scripts/console_regression.py --browser
```

The optional browser suite exercises real clicks, file selection and downloads,
clipboard links, historical restoration, policy/CORS changes, HTML-shaped object
keys, mobile layout, and logout. Screenshots are written under `/tmp`.
