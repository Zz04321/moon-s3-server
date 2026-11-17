#!/usr/bin/env python3
import hashlib
import http.client
import http.server
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

from auth_regression import ACCESS_KEY, SECRET_KEY, sign
from s3_client import ClientError, S3Client, object_path


PROJECT = pathlib.Path(__file__).resolve().parents[1]
CLI = PROJECT / "scripts/moon_s3.py"


def main():
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with tempfile.TemporaryDirectory(prefix="moon-s3-cli-") as directory:
        base = pathlib.Path(directory)
        root = base / "data"
        endpoint = None
        server = None
        environment = dict(os.environ, MOON_S3_ACCESS_KEY=ACCESS_KEY, MOON_S3_SECRET_KEY=SECRET_KEY)
        for name in ["MOON_S3_WEBHOOK_URL", "MOON_S3_WEBHOOK_SECRET", "MOON_S3_WEBHOOK_TIMEOUT_MS"]:
            environment.pop(name, None)
        key = "目录/..//file +%?#.txt"

        def cli(*args, success=True, env=None):
            process = subprocess.run([sys.executable, str(CLI), "--endpoint", endpoint, *map(str, args)],
                                     env=environment if env is None else env, capture_output=True, text=True, timeout=60)
            assert (process.returncode == 0) == success, (args, process.stdout, process.stderr)
            result = json.loads(process.stdout if success else process.stderr)
            assert result["ok"] == success, result
            return result

        def raw(method, path, body=b"", headers=None):
            address = urllib.parse.urlsplit(endpoint)
            connection = http.client.HTTPConnection(address.hostname, address.port, timeout=10)
            try:
                outgoing = sign(method, path, address.netloc, body, extra_headers=headers)
                connection.request(method, path, body=body, headers=outgoing)
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        def stop():
            nonlocal server
            if server is not None:
                server.kill()
                server.wait(timeout=5)
                server = None

        with tempfile.TemporaryFile() as log:
            def start():
                nonlocal server, endpoint
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                endpoint = f"http://127.0.0.1:{port}"
                settings = dict(environment, MOON_S3_ROOT=str(root), MOON_S3_LISTEN=f"127.0.0.1:{port}")
                server = subprocess.Popen([str(PROJECT / "_build/native/debug/build/cmd/main/main.exe")],
                                          env=settings, stdout=log, stderr=log)
                for attempt in range(200):
                    if server.poll() is not None:
                        raise RuntimeError("Server exited before becoming healthy")
                    try:
                        if raw("GET", "/healthz")[0] == 200:
                            return
                    except OSError:
                        pass
                    time.sleep(0.025)
                raise RuntimeError("Server did not become healthy")

            try:
                start()
                assert cli("health")["healthy"]
                cli("bucket", "create", "cli-bucket")
                cli("bucket", "create", "copy-bucket")
                assert {item["name"] for item in cli("bucket", "list")["buckets"]} == {"cli-bucket", "copy-bucket"}
                assert cli("versioning", "get", "cli-bucket")["status"] == "Unconfigured"
                source = base / "source.txt"
                source.write_bytes(b"first content\x00\xff")
                uploaded = cli("object", "upload", "cli-bucket", key, source, "--content-type", "application/cli")
                assert uploaded["etag"] == '"' + hashlib.md5(source.read_bytes()).hexdigest() + '"'
                assert uploaded["version_id"] is None
                assert cli("object", "head", "cli-bucket", key)["headers"]["content-type"] == "application/cli"
                output = base / "download.txt"
                cli("object", "download", "cli-bucket", key, output)
                assert output.read_bytes() == source.read_bytes()
                assert "already exists" in cli("object", "download", "cli-bucket", key, output, success=False)["error"]
                assert output.read_bytes() == source.read_bytes()
                absent = base / "absent.txt"
                assert cli("object", "download", "cli-bucket", "missing", absent, success=False)["code"] == "NoSuchKey"
                assert not absent.exists() and not list(base.glob(".moon-s3-download-*"))
                empty = base / "empty.bin"
                empty.write_bytes(b"")
                cli("object", "upload", "copy-bucket", "empty", empty)
                empty_download = base / "empty-download.bin"
                cli("object", "download", "copy-bucket", "empty", empty_download)
                assert empty_download.read_bytes() == b""
                cli("object", "delete", "copy-bucket", "empty")
                for number in range(4):
                    cli("object", "upload", "cli-bucket", f"prefix/{number}", source)
                listing = cli("object", "list", "cli-bucket", "--page-size", "1")["objects"]
                assert len(listing) == 5 and key in {item["key"] for item in listing}
                prefixes = cli("object", "list", "cli-bucket", "--delimiter", "/", "--page-size", "1")["objects"]
                assert {item["prefix"] for item in prefixes} == {"prefix/", "目录/"}
                assert len(cli("object", "list", "cli-bucket", "--prefix", "prefix/", "--page-size", "2")["objects"]) == 4
                cli("object", "copy", "cli-bucket", key, "copy-bucket", "copy")
                assert cli("object", "head", "copy-bucket", "copy")["headers"]["content-length"] == str(len(source.read_bytes()))
                cli("object", "delete", "copy-bucket", "copy")
                cli("bucket", "delete", "copy-bucket")
                assert cli("bucket", "delete", "cli-bucket", success=False)["code"] == "BucketNotEmpty"

                assert cli("versioning", "enable", "cli-bucket")["status"] == "Enabled"
                source.write_bytes(b"second version")
                version = cli("object", "upload", "cli-bucket", key, source)["version_id"]
                source.write_bytes(b"third version")
                newer = cli("object", "upload", "cli-bucket", key, source)["version_id"]
                history = cli("versions", "list", "cli-bucket", "--prefix", "目录/", "--page-size", "1")["versions"]
                assert [item["version_id"] for item in history] == [newer, version, "null"]
                assert cli("object", "head", "cli-bucket", key, "--version-id", version)["headers"]["x-amz-version-id"] == version
                output = base / "old.txt"
                cli("object", "download", "cli-bucket", key, output, "--version-id", version)
                assert output.read_bytes() == b"second version"
                restored = cli("versions", "restore", "cli-bucket", key, version)["version_id"]
                assert restored not in [version, newer]
                marker = cli("object", "delete", "cli-bucket", key)
                assert marker["delete_marker"]
                history = cli("versions", "list", "cli-bucket", "--prefix", "目录/", "--page-size", "1")["versions"]
                assert history[0]["delete_marker"] and history[0]["latest"]
                cli("object", "delete", "cli-bucket", key, "--version-id", marker["version_id"])
                cli("object", "delete", "cli-bucket", key, "--version-id", newer)
                assert cli("versioning", "suspend", "cli-bucket")["status"] == "Suspended"
                assert cli("object", "upload", "cli-bucket", key, source)["version_id"] == "null"
                cli("versioning", "enable", "cli-bucket")
                assert cli("access", "get", "cli-bucket")["policy"] is None
                assert cli("--anonymous", "object", "head", "cli-bucket", key, success=False)["status"] == 403
                cli("access", "public", "cli-bucket")
                cli("--anonymous", "object", "head", "cli-bucket", key)
                cli("--anonymous", "object", "head", "cli-bucket", key, "--version-id", version, success=False)
                cli("access", "public", "cli-bucket", "--include-versions")
                cli("--anonymous", "object", "head", "cli-bucket", key, "--version-id", version)
                cli("access", "private", "cli-bucket")
                cli("--anonymous", "object", "head", "cli-bucket", key, success=False)
                cors = base / "cors.xml"
                cors.write_text('<CORSConfiguration><CORSRule><AllowedOrigin>*</AllowedOrigin><AllowedMethod>GET</AllowedMethod></CORSRule></CORSConfiguration>')
                cli("cors", "put", "cli-bucket", cors)
                assert "AllowedOrigin" in cli("cors", "get", "cli-bucket")["xml"]
                cli("cors", "delete", "cli-bucket")
                assert cli("cors", "get", "cli-bucket", success=False)["code"] == "NoSuchCORSConfiguration"
                for method in ["GET", "HEAD", "PUT"]:
                    url = cli("presign", "cli-bucket", key, "--method", method, "--expires", "60")["url"]
                    address = urllib.parse.urlsplit(url)
                    connection = http.client.HTTPConnection(address.hostname, address.port, timeout=10)
                    connection.request(method, address.path + "?" + address.query, body=b"presigned" if method == "PUT" else b"")
                    response = connection.getresponse()
                    assert response.status == 200
                    response.read()
                    connection.close()

                large = base / "large.bin"
                payload = bytes(range(256)) * 45056
                large.write_bytes(payload)
                result = cli("object", "upload", "cli-bucket", "large", large, "--part-size-mib", "5")
                assert result["size"] == len(payload) and result["etag"].endswith('-3"')
                downloaded = base / "large-download.bin"
                cli("object", "download", "cli-bucket", "large", downloaded)
                assert hashlib.sha256(downloaded.read_bytes()).digest() == hashlib.sha256(payload).digest()
                assert cli("multipart", "list", "cli-bucket")["uploads"] == []
                upload_ids = []
                for number in range(3):
                    path = object_path("cli-bucket", f"unfinished {number}", [("uploads", "")])
                    document = ET.fromstring(raw("POST", path)[2])
                    upload_ids.append(document.findtext("{*}UploadId"))
                uploads = cli("multipart", "list", "cli-bucket", "--prefix", "unfinished", "--page-size", "1")["uploads"]
                assert {item["upload_id"] for item in uploads} == set(upload_ids)
                for item in uploads:
                    cli("multipart", "abort", "cli-bucket", item["key"], item["upload_id"])
                assert cli("multipart", "list", "cli-bucket")["uploads"] == []

                class FailingClient(S3Client):
                    def request(self, method, path, body=b"", headers=None, output=None):
                        if "partNumber=2" in path:
                            raise ClientError("Injected failure during multipart upload")
                        return super().request(method, path, body, headers, output)

                failing = FailingClient(endpoint, ACCESS_KEY, SECRET_KEY)
                try:
                    with large.open("rb") as source_file:
                        failing.upload("cli-bucket", "failed-upload", source_file, "application/octet-stream", 5 * 1024 * 1024)
                except ClientError:
                    pass
                else:
                    raise AssertionError("Injected multipart failure was not reported")
                assert cli("multipart", "list", "cli-bucket")["uploads"] == []
                assert cli("object", "head", "cli-bucket", "failed-upload", success=False)["status"] == 404
                wrong = dict(environment, MOON_S3_SECRET_KEY="wrong-secret")
                assert cli("bucket", "list", env=wrong, success=False)["code"] == "AccessDenied"
                missing = dict(environment)
                missing.pop("MOON_S3_SECRET_KEY")
                missing.pop("AWS_SECRET_ACCESS_KEY", None)
                assert "both" in cli("bucket", "list", env=missing, success=False)["error"]
                assert cli("--anonymous", "health", env=missing)["healthy"]
                fallback = dict(environment, AWS_ACCESS_KEY_ID=ACCESS_KEY, AWS_SECRET_ACCESS_KEY=SECRET_KEY)
                fallback.pop("MOON_S3_ACCESS_KEY")
                fallback.pop("MOON_S3_SECRET_KEY")
                cli("bucket", "list", env=fallback)
                cli("--region", "custom-region", "bucket", "list")
                for invalid in ["http://user:pass@localhost:9000", endpoint + "/prefix", "file:///tmp/data"]:
                    cli("--endpoint", invalid, "health", success=False)
                cli("object", "upload", "cli-bucket", "invalid-part-size", source, "--part-size-mib", "1", success=False)
                assert "in use" in cli("check", "--root", root, success=False)["error"]
                stop()
                assert cli("check", "--root", root)["objects"] > 0
                archive = base / "backup.tar"
                cli("backup", "--root", root, "--output", archive)
                root = base / "restored"
                cli("restore", "--archive", archive, "--root", root)
                start()
                cli("object", "head", "cli-bucket", "large")
                assert cli("versioning", "get", "cli-bucket")["status"] == "Enabled"
                print("CLI regression passed: signed CRUD, Unicode keys, automatic pagination, versions, permissions, CORS, links, multipart SHA-256 and cleanup, diagnostics and offline restore.")
            except BaseException:
                stop()
                log.seek(0)
                print(log.read().decode(errors="replace"))
                raise
            finally:
                stop()

    class TruncatedServer(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/paging-bucket"):
                body = b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>true</IsTruncated><NextContinuationToken>repeated</NextContinuationToken></ListBucketResult>'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"incomplete")
            self.close_connection = True

        def log_message(self, *args):
            pass

    fixture = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TruncatedServer)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="moon-cli-truncated-") as directory:
            output = pathlib.Path(directory) / "download"
            response = subprocess.run([sys.executable, str(CLI), "--anonymous", "--endpoint",
                                       f"http://127.0.0.1:{fixture.server_port}", "object", "download", "fixture-bucket", "key", str(output)],
                                      capture_output=True, text=True, timeout=10)
            assert response.returncode == 1 and not json.loads(response.stderr)["ok"]
            assert not output.exists() and not list(output.parent.glob(".moon-s3-download-*"))
            response = subprocess.run([sys.executable, str(CLI), "--anonymous", "--endpoint",
                                       f"http://127.0.0.1:{fixture.server_port}", "object", "list", "paging-bucket"],
                                      capture_output=True, text=True, timeout=10)
            assert response.returncode == 1 and "repeated" in json.loads(response.stderr)["error"]
            print("CLI download safety passed: truncated responses never publish partial content.")
    finally:
        fixture.shutdown()
        fixture.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
