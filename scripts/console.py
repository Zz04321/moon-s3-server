#!/usr/bin/env python3
import argparse
import collections
import hashlib
import hmac
import http.client
import http.cookies
import http.server
import json
import os
import pathlib
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

from moon_s3 import execute
from s3_client import BLOCK_SIZE, NS, ClientError, S3Client, endpoint_address, object_path


ASSETS = pathlib.Path(__file__).resolve().parents[1] / "console"
SESSION_SECONDS = 3600
UPLOAD_LIMIT = 64 * BLOCK_SIZE
COOKIE = "moon_s3_console"
ACTIONS = {
    ("bucket", "create"): {"bucket"}, ("bucket", "delete"): {"bucket"},
    ("object", "head"): {"bucket", "key", "version_id"},
    ("object", "delete"): {"bucket", "key", "version_id"},
    ("object", "copy"): {"source_bucket", "source_key", "bucket", "key", "version_id"},
    ("versions", "restore"): {"bucket", "key", "version_id"},
    ("versioning", "get"): {"bucket"}, ("versioning", "enable"): {"bucket"}, ("versioning", "suspend"): {"bucket"},
    ("access", "get"): {"bucket"}, ("access", "private"): {"bucket"}, ("access", "public"): {"bucket", "include_versions"},
    ("cors", "get"): {"bucket"}, ("cors", "delete"): {"bucket"}, ("cors", "put"): {"bucket", "xml"},
    ("multipart", "list"): {"bucket", "prefix"}, ("multipart", "abort"): {"bucket", "key", "upload_id"},
    ("presign", ""): {"bucket", "key", "expires"},
}


class ConsoleError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class ConsoleServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, origin, client, password):
        self.origin = origin
        self.authority = urllib.parse.urlsplit(origin).netloc
        self.secure = origin.startswith("https://")
        self.client = client
        self.password_digest = hashlib.sha256(password.encode()).digest()
        self.sessions = {}
        self.failures = collections.OrderedDict()
        self.lock = threading.Lock()
        super().__init__(address, ConsoleHandler)


class ConsoleHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def reply(self, status, body, content_type="application/json", headers=None):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def require_origin(self):
        if self.headers.get("Origin") != self.server.origin:
            raise ConsoleError(403, "Request origin is not permitted")

    def session(self, csrf=False):
        cookies = http.cookies.SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            raise ConsoleError(401, "Sign in to continue") from None
        token = cookies[COOKIE].value if COOKIE in cookies else ""
        with self.server.lock:
            session = self.server.sessions.get(token)
            if not session or session["expires"] <= time.monotonic():
                self.server.sessions.pop(token, None)
                raise ConsoleError(401, "Session expired; sign in again")
        if csrf and not hmac.compare_digest(self.headers.get("X-Console-CSRF", ""), session["csrf"]):
            raise ConsoleError(403, "Invalid CSRF token")
        return token, session

    def length(self, maximum):
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            raise ConsoleError(400, "A single Content-Length is required")
        try:
            length = int(self.headers["Content-Length"])
        except ValueError:
            raise ConsoleError(400, "Invalid Content-Length") from None
        if length < 0 or length > maximum:
            raise ConsoleError(413, "Request body is too large")
        return length

    def document(self):
        if self.headers.get_content_type() != "application/json":
            raise ConsoleError(415, "Expected application/json")
        length = self.length(65536)
        body = self.rfile.read(length)
        if len(body) != length:
            raise ConsoleError(400, "Incomplete request body")
        try:
            document = json.loads(body)
        except (ValueError, RecursionError):
            raise ConsoleError(400, "Invalid JSON") from None
        if not isinstance(document, dict):
            raise ConsoleError(400, "Expected a JSON object")
        return document

    def login(self):
        document = self.document()
        password = document.get("password")
        if not isinstance(password, str):
            raise ConsoleError(400, "Password is required")
        address = self.client_address[0]
        now = time.monotonic()
        with self.server.lock:
            failures = [stamp for stamp in self.server.failures.get(address, []) if now - stamp < 60]
            if len(failures) >= 10:
                raise ConsoleError(429, "Too many sign-in attempts; retry in one minute")
            if not hmac.compare_digest(hashlib.sha256(password.encode()).digest(), self.server.password_digest):
                self.server.failures[address] = failures + [now]
                if len(self.server.failures) > 1024:
                    self.server.failures.popitem(last=False)
                raise ConsoleError(401, "Incorrect administrator password")
            self.server.failures.pop(address, None)
            self.server.sessions = {token: session for token, session in self.server.sessions.items() if session["expires"] > now}
            if len(self.server.sessions) >= 256:
                raise ConsoleError(503, "Session limit reached; retry after an existing session expires")
            token = secrets.token_urlsafe(32)
            session = {"csrf": secrets.token_urlsafe(32), "expires": now + SESSION_SECONDS}
            self.server.sessions[token] = session
        cookie = f"{COOKIE}={token}; Path=/api/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}"
        if self.server.secure:
            cookie += "; Secure"
        self.reply(200, {"ok": True, "csrf": session["csrf"]}, headers={"Set-Cookie": cookie})

    def action(self, document):
        command, action = document.get("command"), document.get("action", "")
        if not isinstance(command, str) or not isinstance(action, str):
            raise ConsoleError(400, "Invalid operation")
        fields = ACTIONS.get((command, action))
        if fields is None or set(document) - {"command", "action"} - fields:
            raise ConsoleError(400, "Unsupported console operation or fields")
        for name, value in document.items():
            if name == "include_versions":
                if not isinstance(value, bool):
                    raise ConsoleError(400, "include_versions must be boolean")
            elif name == "expires":
                if type(value) is not int or not 1 <= value <= 604800:
                    raise ConsoleError(400, "Link lifetime must be 1-604800 seconds")
            elif not isinstance(value, str):
                raise ConsoleError(400, "Operation fields must be strings")
        required = fields - {"version_id", "include_versions", "expires", "prefix"}
        if command == "versions":
            required |= {"version_id"}
        if any(not document.get(name) for name in required):
            raise ConsoleError(400, "Missing operation fields")
        arguments = argparse.Namespace(version_id=None, include_versions=False, prefix="", page_size=1000, expires=900, method="GET")
        for name, value in document.items():
            setattr(arguments, name, value)
        if command == "cors" and action == "put":
            self.server.client.request("PUT", object_path(arguments.bucket, parameters=[("cors", "")]),
                                       arguments.xml.encode(), {"content-type": "application/xml"})
            return {"action": "put"}
        return execute(self.server.client, arguments)

    def listing(self, query, versions=False):
        allowed = {"bucket", "prefix", "token", "key_marker", "version_marker"} if versions else {"bucket", "prefix", "token"}
        if set(query) - allowed:
            raise ConsoleError(400, "Unknown listing parameter")
        parameters = [("versions", "")] if versions else [("list-type", "2")]
        parameters += [("encoding-type", "url"), ("max-keys", "200"), ("prefix", query.get("prefix", ""))]
        if versions:
            if "key_marker" in query:
                parameters.append(("key-marker", query["key_marker"]))
            if "version_marker" in query:
                parameters.append(("version-id-marker", query["version_marker"]))
        elif "token" in query:
            parameters.append(("continuation-token", query["token"]))
        _, page = self.server.client.xml("GET", object_path(query.get("bucket", ""), parameters=parameters))
        entries = []
        for item in page:
            tag = item.tag.split("}")[-1]
            if tag not in ("Contents", "Version", "DeleteMarker"):
                continue
            entries.append({"key": urllib.parse.unquote(item.findtext("s3:Key", namespaces=NS)),
                            "size": int(item.findtext("s3:Size", default="0", namespaces=NS)),
                            "modified": item.findtext("s3:LastModified", namespaces=NS),
                            "version_id": item.findtext("s3:VersionId", namespaces=NS),
                            "latest": item.findtext("s3:IsLatest", namespaces=NS) == "true",
                            "delete_marker": tag == "DeleteMarker"})
        return {"entries": entries, "truncated": page.findtext("s3:IsTruncated", namespaces=NS) == "true",
                "token": page.findtext("s3:NextContinuationToken", namespaces=NS),
                "key_marker": urllib.parse.unquote(page.findtext("s3:NextKeyMarker", default="", namespaces=NS)),
                "version_marker": page.findtext("s3:NextVersionIdMarker", namespaces=NS)}

    def upload(self, query):
        if set(query) - {"bucket", "key"}:
            raise ConsoleError(400, "Unknown upload parameter")
        bucket, key = query.get("bucket", ""), query.get("key", "")
        object_path(bucket, key)
        remaining = self.length(UPLOAD_LIMIT)
        content_type = self.headers.get("Content-Type", "application/octet-stream")
        with tempfile.TemporaryFile() as source:
            while remaining:
                block = self.rfile.read(min(remaining, BLOCK_SIZE))
                if not block:
                    raise ConsoleError(400, "Incomplete upload")
                source.write(block)
                remaining -= len(block)
            source.seek(0)
            return self.server.client.upload(bucket, key, source, content_type)

    def download(self, query):
        if set(query) - {"bucket", "key", "version_id"}:
            raise ConsoleError(400, "Unknown download parameter")
        key = query.get("key", "")
        parameters = [("versionId", query["version_id"])] if "version_id" in query else []
        with tempfile.TemporaryFile() as destination:
            self.server.client.request("GET", object_path(query.get("bucket", ""), key, parameters), output=destination)
            size = destination.tell()
            destination.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(key.rsplit("/", 1)[-1], safe=""))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
            self.end_headers()
            while block := destination.read(BLOCK_SIZE):
                self.wfile.write(block)

    def dispatch(self):
        self.connection.settimeout(30)
        if self.headers.get("Host") != self.server.authority:
            raise ConsoleError(403, "Unexpected Host header")
        address = urllib.parse.urlsplit(self.path)
        pairs = urllib.parse.parse_qsl(address.query, keep_blank_values=True)
        query = dict(pairs)
        if len(query) != len(pairs):
            raise ConsoleError(400, "Duplicate query parameters")
        if self.command in ("GET", "HEAD") and address.path in ("/", "/app.js", "/styles.css"):
            name, content_type = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                                  "/styles.css": ("styles.css", "text/css; charset=utf-8")}[address.path]
            self.reply(200, (ASSETS / name).read_bytes(), content_type)
            return
        if not address.path.startswith("/api/"):
            raise ConsoleError(404, "Not found")
        if self.command == "POST":
            self.require_origin()
            if address.path == "/api/login":
                self.login()
                return
            token, _ = self.session(csrf=True)
            if address.path == "/api/logout":
                with self.server.lock:
                    self.server.sessions.pop(token, None)
                cookie = f"{COOKIE}=; Path=/api/; HttpOnly; SameSite=Strict; Max-Age=0" + ("; Secure" if self.server.secure else "")
                self.reply(200, {"ok": True}, headers={"Set-Cookie": cookie})
                return
            if address.path == "/api/action":
                result = self.action(self.document())
            elif address.path == "/api/upload":
                result = self.upload(query)
            else:
                raise ConsoleError(404, "Not found")
            self.reply(200, {"ok": True, **result})
        elif self.command == "GET":
            _, session = self.session()
            if address.path == "/api/session":
                result = {"csrf": session["csrf"], "upload_limit": UPLOAD_LIMIT}
            elif address.path == "/api/buckets":
                result = execute(self.server.client, argparse.Namespace(command="bucket", action="list"))
            elif address.path in ("/api/objects", "/api/versions"):
                result = self.listing(query, address.path == "/api/versions")
            elif address.path == "/api/download":
                self.download(query)
                return
            else:
                raise ConsoleError(404, "Not found")
            self.reply(200, {"ok": True, **result})
        else:
            raise ConsoleError(405, "Method not allowed")

    def handle_request(self):
        try:
            self.dispatch()
        except ConsoleError as error:
            self.reply(error.status, {"ok": False, "error": str(error)})
        except ClientError as error:
            status = error.status if error.status and error.status in (400, 404, 405, 409, 412, 413, 416) else 502
            self.reply(status, {"ok": False, "error": str(error), "code": error.code})
        except (ValueError, TypeError, KeyError, ET.ParseError) as error:
            self.reply(400, {"ok": False, "error": "Invalid request or service response"})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except (OSError, http.client.HTTPException):
            self.reply(502, {"ok": False, "error": "Storage service or local staging is unavailable"})

    do_GET = handle_request
    do_HEAD = handle_request
    do_POST = handle_request
    do_PUT = handle_request
    do_DELETE = handle_request
    do_OPTIONS = handle_request


def main():
    parser = argparse.ArgumentParser(description="Local Moon S3 Web console with a separate administrator password.")
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--origin", help="exact browser origin, required for reverse proxies or non-loopback listening")
    parser.add_argument("--endpoint", default=os.environ.get("MOON_S3_ENDPOINT", "http://127.0.0.1:9000"))
    arguments = parser.parse_args()
    try:
        password = os.environ.get("MOON_S3_CONSOLE_PASSWORD", "")
        if len(password) < 16:
            raise ValueError("MOON_S3_CONSOLE_PASSWORD must contain at least 16 characters")
        if not 1 <= arguments.port <= 65535:
            raise ValueError("port must be 1-65535")
        origin = arguments.origin or f"http://127.0.0.1:{arguments.port}"
        parsed = endpoint_address(origin)
        if parsed.path or (not arguments.origin and arguments.listen != "127.0.0.1"):
            raise ValueError("set --origin to the exact browser origin for a custom listen address")
        client = S3Client(arguments.endpoint,
                          os.environ.get("MOON_S3_ACCESS_KEY", os.environ.get("AWS_ACCESS_KEY_ID", "")),
                          os.environ.get("MOON_S3_SECRET_KEY", os.environ.get("AWS_SECRET_ACCESS_KEY", "")),
                          os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
        server = ConsoleServer((arguments.listen, arguments.port), origin, client, password)
        print(f"Moon S3 console available at {origin}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
