#!/usr/bin/env python3
import base64
import concurrent.futures
import hashlib
import http.client
import json
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET

from auth_regression import ACCESS_KEY, SECRET_KEY, sign
from operations_regression import BINARY, PROJECT, free_port, running


NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def request(port, method, path, body=b"", headers=None, authenticated=True):
    outgoing = sign(method, path, f"127.0.0.1:{port}", body,
                    extra_headers=headers) if authenticated else headers or {}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body, outgoing)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def error(response, status, code):
    assert response[0] == status, response
    assert ET.fromstring(response[2]).findtext("Code") == code, response


def object_path(bucket, key):
    return f"/{bucket}/" + urllib.parse.quote(key, safe="/")


def admin(*arguments):
    result = subprocess.run([sys.executable, str(PROJECT / "scripts/admin.py"), *arguments],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def storage_safety(directory):
    root = directory / "data"
    settings = {"root": str(root), "listen": f"127.0.0.1:{free_port()}",
                "access_key": ACCESS_KEY, "secret_key": SECRET_KEY}
    keys = ["a" * 117, "a" * 118, "a" * 1024, "界" * 341 + "a", "界" * 300 + "/folder/long"]
    with running(directory, settings) as (server, port, log):
        def call(method, path, body=b"", headers=None):
            return request(port, method, path, body, headers)

        assert call("PUT", "/safety-bucket")[0] == 200
        assert call("PUT", "/recovery-bucket")[0] == 200
        assert call("PUT", object_path("recovery-bucket", keys[-1]), b"before-crash")[0] == 200
        path = "/safety-bucket/original"
        original = b"original"
        assert call("PUT", path, original)[0] == 200
        for parameter in ["tagging", "acl", "retention", "legal-hold", "torrent", "unknown"]:
            for method in ["GET", "PUT", "DELETE"]:
                error(call(method, path + "?" + parameter, b"<Tagging/>" if method == "PUT" else b""),
                      501, "NotImplemented")
                assert call("GET", path)[2] == original
        error(call("DELETE", path + "?prefix=ignored"), 400, "InvalidRequest")
        for headers in [{"x-amz-server-side-encryption": "AES256"},
                        {"x-amz-server-side-encryption": "aws:kms"},
                        {"x-amz-server-side-encryption-customer-algorithm": "AES256"},
                        {"x-amz-acl": "public-read"}, {"x-amz-meta-owner": "demo"},
                        {"x-amz-checksum-sha256": "invalid"}, {"x-amz-tagging": "a=b"}]:
            error(call("PUT", path, b"replacement", headers), 501, "NotImplemented")
            assert call("GET", path)[2] == original
        error(call("PUT", path, b"bad", {"content-md5": "AAAAAAAAAAAAAAAAAAAAAA=="}), 400, "BadDigest")
        for digest in ["wrong", "YQ=="]:
            error(call("PUT", path, b"bad", {"content-md5": digest}), 400, "InvalidDigest")
        assert call("GET", path)[2] == original
        digest = base64.b64encode(hashlib.md5(original).digest()).decode()
        assert call("PUT", path, original, {"content-md5": digest})[0] == 200
        error(call("PUT", path, b"bad", {"x-amz-checksum-crc32": "AAAAAA=="}), 400, "BadDigest")
        error(call("PUT", path, b"bad", {"x-amz-sdk-checksum-algorithm": "CRC32"}), 400, "BadDigest")
        assert call("PUT", "/safety-bucket/crc32", b"123456789", {"x-amz-checksum-crc32": "y/Q5Jg=="})[0] == 200
        assert call("GET", path, headers={"x-amz-checksum-mode": "ENABLED"})[2] == original
        error(call("PUT", path, b"bad", {"x-amz-checksum-mode": "ENABLED"}), 501, "NotImplemented")
        etag = call("HEAD", path)[1]["ETag"]
        error(call("PUT", path, b"bad", {"if-none-match": "*"}), 412, "PreconditionFailed")
        error(call("PUT", path, b"bad", {"if-match": '"wrong"'}), 412, "PreconditionFailed")
        assert call("PUT", path, b"matched", {"if-match": etag})[0] == 200
        error(call("PUT", "/safety-bucket/missing", b"bad", {"if-match": "*"}), 412, "PreconditionFailed")
        race_path = "/safety-bucket/create-once"
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as workers:
            statuses = list(workers.map(lambda index: call("PUT", race_path, str(index).encode(),
                                                         {"if-none-match": "*"})[0], range(8)))
        assert statuses.count(200) == 1 and statuses.count(412) == 7, statuses
        for invalid in ["%FF", "%FE", "%C0%AF", "%ED%A0%80", "%ZZ", "%", "%2"]:
            error(call("PUT", "/safety-bucket/" + invalid, b"bad"), 400, "InvalidRequest")
            error(call("GET", "/safety-bucket?prefix=" + invalid), 400, "InvalidRequest")
            error(call("PUT", path, headers={"x-amz-copy-source": "/safety-bucket/" + invalid}), 400, "InvalidArgument")
        error(call("PUT", "/safety-bucket/" + "a" * 1025, b"bad"), 400, "KeyTooLongError")
        for key in keys:
            target = object_path("safety-bucket", key)
            assert call("PUT", target, key.encode())[0] == 200
            assert call("GET", target)[2] == key.encode()
        listing = ET.fromstring(call("GET", "/safety-bucket?list-type=2&encoding-type=url")[2])
        found = {urllib.parse.unquote(item.text) for item in listing.findall("s3:Contents/s3:Key", NS)}
        assert set(keys) <= found
        version_xml = b"<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>"
        assert call("PUT", "/safety-bucket?versioning", version_xml)[0] == 200
        long_path = object_path("safety-bucket", keys[-1])
        first = call("PUT", long_path, b"version-one")[1]["x-amz-version-id"]
        assert call("PUT", long_path, b"version-two")[0] == 200
        assert call("GET", long_path + "?versionId=" + first)[2] == b"version-one"
        conditional_path = "/safety-bucket/version-condition"
        historical = call("PUT", conditional_path, b"old")
        historical_id = historical[1]["x-amz-version-id"]
        assert call("PUT", conditional_path, b"new")[0] == 200
        assert call("DELETE", conditional_path + "?versionId=" + historical_id,
                    headers={"if-match": historical[1]["ETag"]})[0] == 204
        assert call("GET", conditional_path)[2] == b"new"
        copy_target = object_path("safety-bucket", "copy/" + "z" * 500)
        assert call("PUT", copy_target, headers={"x-amz-copy-source": long_path + "?versionId=" + first})[0] == 200
        assert call("GET", copy_target)[2] == b"version-one"
        for content_type in ["a" * 1025, "a" * 65536]:
            error(call("PUT", long_path, b"bad", {"content-type": content_type}), 400, "InvalidRequest")
        assert call("GET", long_path)[2] == b"version-two"
        assert not list(root.rglob("*.txn"))
        upload_path = object_path("safety-bucket", "multipart/" + "m" * 700)
        error(call("POST", upload_path + "?uploads", headers={"content-type": "a" * 1025}), 400, "InvalidRequest")
        upload = ET.fromstring(call("POST", upload_path + "?uploads")[2]).findtext("s3:UploadId", namespaces=NS)
        part_path = upload_path + "?uploadId=" + upload + "&partNumber=1"
        error(call("PUT", part_path, b"bad", {"content-md5": "AAAAAAAAAAAAAAAAAAAAAA=="}), 400, "BadDigest")
        part = call("PUT", part_path, b"multipart")[1]["ETag"]
        completion = f"<CompleteMultipartUpload><Part><PartNumber>1</PartNumber><ETag>{part}</ETag></Part></CompleteMultipartUpload>".encode()
        assert call("PUT", upload_path, b"existing")[0] == 200
        error(call("POST", upload_path + "?uploadId=" + upload, completion, {"if-none-match": "*"}), 412, "PreconditionFailed")
        assert call("POST", upload_path + "?uploadId=" + upload, completion)[0] == 200
        assert call("GET", upload_path)[2] == b"multipart"
        error(call("PUT", "/%6Detrics"), 400, "InvalidBucketName")
        error(call("PUT", "/healthz"), 400, "InvalidBucketName")
        assert call("PUT", "/empty-bucket")[0] == 200
        empty_key = object_path("empty-bucket", "q" * 700)
        assert call("PUT", empty_key, b"delete-me")[0] == 200
        assert call("DELETE", empty_key)[0] == 204
        assert call("DELETE", "/empty-bucket")[0] == 204
        server.terminate()
        assert server.wait(timeout=5) == 0
    filename = "k-" + hashlib.sha256(keys[-1].encode()).hexdigest()
    content = root / "recovery-bucket" / "objects" / (filename + ".object")
    metadata = content.with_suffix(".meta")
    pathlib.Path(str(content) + ".pending").write_bytes(b"after-crash")
    pathlib.Path(str(metadata) + ".pending").write_text(
        "etag=" + hashlib.md5(b"after-crash").hexdigest() + "\ncontent-type=text/plain\n")
    pathlib.Path(str(content) + ".txn").write_text("put\n")
    mapping_directory = root / "recovery-bucket" / ".keys"
    (mapping_directory / (filename + ".tmp")).write_bytes(b"interrupted mapping")
    with running(directory, settings) as (server, port, log):
        assert request(port, "GET", object_path("recovery-bucket", keys[-1]))[2] == b"after-crash"
        assert not pathlib.Path(str(content) + ".txn").exists()
        assert not list(mapping_directory.glob("*.tmp"))
    report = admin("check", "--root", str(root))
    assert report["ok"] and report["versions"] > 0
    archive = directory / "backup.tar"
    restored = directory / "restored"
    assert admin("backup", "--root", str(root), "--output", str(archive))["ok"]
    assert admin("restore", "--archive", str(archive), "--root", str(restored))["ok"]
    for selected in [root, restored]:
        settings["root"] = str(selected)
        with running(directory, settings) as (server, port, log):
            assert request(port, "GET", long_path)[2] == b"version-two"
            assert request(port, "GET", copy_target)[2] == b"version-one"
            assert request(port, "GET", upload_path)[2] == b"multipart"
            versions = ET.fromstring(request(port, "GET", "/safety-bucket?versions&encoding-type=url")[2])
            assert versions.findall("s3:Version", NS)
    mapped = next((restored / "safety-bucket" / ".keys").iterdir())
    mapped.write_text("mismatched mapping")
    environment = {"MOON_S3_ROOT": str(restored), "MOON_S3_LISTEN": f"127.0.0.1:{free_port()}"}
    result = subprocess.run([str(BINARY)], env=environment, capture_output=True, timeout=5)
    assert result.returncode != 0 and b"recovery_failed" in result.stdout
    print("Storage safety passed: unsafe API rejection, MD5, atomic conditions, UTF-8, 1024-byte keys, versions, copy, multipart, restart and verified backups.")


def raw_connection(port, length, body=b"", chunked=False):
    connection = socket.create_connection(("127.0.0.1", port), timeout=3)
    framing = "Transfer-Encoding: chunked" if chunked else f"Content-Length: {length}"
    connection.sendall(f"PUT /resource-bucket/key HTTP/1.1\r\nHost: localhost\r\n{framing}\r\n\r\n".encode() + body)
    return connection


def raw_status(connection):
    response = http.client.HTTPResponse(connection)
    response.begin()
    response.read()
    return response.status


def resource_safety(directory):
    settings = {"root": str(directory / "limits"), "listen": f"127.0.0.1:{free_port()}",
                "access_key": ACCESS_KEY, "secret_key": SECRET_KEY,
                "max_request_bytes": 65536, "max_buffered_bytes": 65536,
                "max_connections": 3, "request_timeout_ms": 500}
    with running(directory, settings) as (server, port, log):
        connection = raw_connection(port, 65537)
        assert raw_status(connection) == 413
        connection.close()
        connection = socket.create_connection(("127.0.0.1", port), timeout=3)
        connection.sendall(b"PUT /resource-bucket/key HTTP/1.1\r\nHost: localhost\r\nContent-Type: text/plain\norder=bad\r\nContent-Length: 0\r\n\r\n")
        assert raw_status(connection) == 400
        connection.close()
        connection = raw_connection(port, 0, b"10001\r\n" + b"a" * 65537 + b"\r\n0\r\n\r\n", chunked=True)
        assert raw_status(connection) == 413
        connection.close()
        first = raw_connection(port, 49152, b"a" * 40000)
        time.sleep(0.05)
        second = raw_connection(port, 49152, b"b" * 30000)
        assert raw_status(second) == 503
        second.close()
        first.sendall(b"a" * 9152)
        assert raw_status(first) == 403
        first.close()
        connection = raw_connection(port, 100, b"a")
        assert connection.recv(1) == b""
        connection.close()
        metrics = request(port, "GET", "/metrics")[2]
        assert b"moon_s3_buffered_request_bytes 0\n" in metrics, metrics
        held = [socket.create_connection(("127.0.0.1", port), timeout=3) for attempt in range(3)]
        time.sleep(0.05)
        extra = socket.create_connection(("127.0.0.1", port), timeout=3)
        try:
            assert extra.recv(1) == b""
        except ConnectionResetError:
            pass
        extra.close()
        for connection in held:
            connection.close()
        time.sleep(0.05)
        assert request(port, "GET", "/healthz", authenticated=False)[0] == 200
    print("Resource safety passed: fixed/chunked body limits, aggregate buffering, timeout cleanup and connection capacity.")


def main():
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with tempfile.TemporaryDirectory(prefix="moon-s3-safety-") as temporary:
        directory = pathlib.Path(temporary)
        storage_safety(directory)
        resource_safety(directory)


if __name__ == "__main__":
    main()
