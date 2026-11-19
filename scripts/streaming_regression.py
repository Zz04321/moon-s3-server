#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import hashlib
import http.client
import json
import pathlib
import socket
import subprocess
import tempfile
import time
import zlib

from auth_regression import ACCESS_KEY, SECRET_KEY
from operations_regression import PROJECT, free_port, running
from s3_client import ClientError, S3Client, object_path, signing_headers


def digest_headers(method, path, port, body, extra=None):
    return signing_headers(method, path, f"127.0.0.1:{port}", hashlib.sha256(body).hexdigest(),
                           extra or {}, ACCESS_KEY, SECRET_KEY, "us-east-1")


def pending(port, path, length, prefix, headers=None, chunked=False):
    connection = socket.create_connection(("127.0.0.1", port), timeout=10)
    outgoing = dict(headers or {}, host=f"127.0.0.1:{port}")
    outgoing["Transfer-Encoding" if chunked else "Content-Length"] = "chunked" if chunked else str(length)
    head = f"PUT {path} HTTP/1.1\r\n" + "".join(f"{name}: {value}\r\n" for name, value in outgoing.items()) + "\r\n"
    connection.sendall(head.encode() + prefix)
    return connection


def response(connection):
    reply = http.client.HTTPResponse(connection)
    reply.begin()
    result = reply.status, reply.read()
    connection.close()
    return result


def metrics(client):
    text = client.request("GET", "/metrics")[1].decode()
    return {name: int(value) for line in text.splitlines() if line.startswith(("moon_s3_buffered_request_bytes ", "moon_s3_staged_request_bytes "))
            for name, value in [line.split()]}


def eventually(check):
    for attempt in range(200):
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for staging state")


def safety(directory):
    settings = dict(root=str(directory / "data"), listen=f"127.0.0.1:{free_port()}",
                    access_key=ACCESS_KEY, secret_key=SECRET_KEY, max_request_bytes=8 * 1024 * 1024,
                    max_buffered_bytes=32768, max_staged_bytes=131072, request_timeout_ms=1000)
    with running(directory, settings) as (server, port, _):
        client = S3Client(f"http://127.0.0.1:{port}", ACCESS_KEY, SECRET_KEY)
        client.request("PUT", "/stream-bucket")
        path = "/stream-bucket/key"
        client.request("PUT", path, b"original")
        candidate = b"changed" * 2000
        invalid = [
            {"Content-MD5": base64.b64encode(b"0" * 16).decode()},
            {"x-amz-checksum-crc32": "AAAAAA=="},
            {"x-amz-content-sha256": "0" * 64},
        ]
        for index, headers in enumerate(invalid):
            outgoing = digest_headers("PUT", path, port, candidate, headers)
            if "x-amz-content-sha256" in headers:
                outgoing = signing_headers("PUT", path, f"127.0.0.1:{port}", "0" * 64, {}, ACCESS_KEY, SECRET_KEY, "us-east-1")
            connection = pending(port, path, len(candidate), candidate, outgoing)
            assert response(connection)[0] == (403 if index == 2 else 400)
            assert client.request("GET", path)[1] == b"original"
        stalled = pending(port, path, 100000, b"x" * 50000)
        eventually(lambda: metrics(client)["moon_s3_staged_request_bytes"] >= 50000)
        assert metrics(client)["moon_s3_buffered_request_bytes"] == 16384
        assert client.request("GET", path)[1] == b"original"
        stalled.close()
        eventually(lambda: metrics(client)["moon_s3_staged_request_bytes"] == 0)
        assert not list((directory / "data").glob(".request-*.tmp"))
        stalled = pending(port, path, 100000, b"x")
        assert stalled.recv(1) == b""
        stalled.close()
        eventually(lambda: metrics(client)["moon_s3_staged_request_bytes"] == 0)
        assert client.request("GET", path)[1] == b"original"
        first = pending(port, path, 120000, b"a" * 100000)
        eventually(lambda: metrics(client)["moon_s3_staged_request_bytes"] == 100000)
        second = pending(port, "/stream-bucket/second", 60000, b"b" * 60000)
        assert response(second)[0] == 503
        first.close()
        eventually(lambda: metrics(client)["moon_s3_staged_request_bytes"] == 0)
        first = pending(port, path, 100, b"a")
        second = pending(port, path, 100, b"b")
        eventually(lambda: metrics(client)["moon_s3_buffered_request_bytes"] == 32768)
        third = pending(port, path, 0, b"")
        assert response(third)[0] == 503
        first.close()
        second.close()
        eventually(lambda: metrics(client)["moon_s3_buffered_request_bytes"] == 0)
        body = b"chunked-upload" * 1000
        headers = digest_headers("PUT", path, port, body)
        chunks = b"".join(f"{len(part):x}\r\n".encode() + part + b"\r\n" for part in [body[:123], body[123:]]) + b"0\r\n\r\n"
        assert response(pending(port, path, len(body), chunks, headers, chunked=True))[0] == 200
        assert client.request("GET", path)[1] == body
        client.request("PUT", "/stream-bucket?versioning", b"<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>")
        _, upload = client.xml("POST", "/stream-bucket/part?uploads")
        upload_id = upload.findtext("{*}UploadId")
        target = object_path("stream-bucket", "part", [("uploadId", upload_id), ("partNumber", "1")])
        client.request("PUT", target, b"old-part")
        bad_headers = digest_headers("PUT", target, port, body, {"Content-MD5": "AAAA"})
        assert response(pending(port, target, len(body), body, bad_headers))[0] == 400
        _, parts = client.xml("GET", object_path("stream-bucket", "part", [("uploadId", upload_id)]))
        assert parts.findtext("{*}Part/{*}ETag") == '"' + hashlib.md5(b"old-part").hexdigest() + '"'
        headers = digest_headers("PUT", path, port, body, {"If-None-Match": "*"})
        assert response(pending(port, path, len(body), body, headers))[0] == 412
        stalled = pending(port, path, 100000, b"x" * 10000)
        eventually(lambda: bool(list((directory / "data").glob(".request-*.tmp"))))
        server.kill()
        server.wait()
        stalled.close()
    assert list((directory / "data").glob(".request-*.tmp"))
    with running(directory, settings) as (_, port, _):
        client = S3Client(f"http://127.0.0.1:{port}", ACCESS_KEY, SECRET_KEY)
        assert not list((directory / "data").glob(".request-*.tmp"))
        assert client.request("GET", path)[1] == body
    print("Streaming safety passed: checksums, disconnect/timeout cleanup, quotas, chunked transfer, part preservation, conditions and crash recovery.")


def memory(directory):
    size = 128 * 1024 * 1024
    settings = dict(root=str(directory / "large-data"), listen=f"127.0.0.1:{free_port()}",
                    access_key=ACCESS_KEY, secret_key=SECRET_KEY, max_request_bytes=size,
                    max_buffered_bytes=32768, request_timeout_ms=120000)
    source = directory / "large.bin"
    block = bytes(range(256)) * 4096
    digest = hashlib.sha256()
    md5 = hashlib.md5()
    crc32 = 0
    with source.open("wb") as stream:
        for index in range(size // len(block)):
            stream.write(block)
            digest.update(block)
            md5.update(block)
            crc32 = zlib.crc32(block, crc32)
    with running(directory, settings) as (server, port, _):
        client = S3Client(f"http://127.0.0.1:{port}", ACCESS_KEY, SECRET_KEY, timeout=120)
        client.request("PUT", "/stream-bucket")
        def rss():
            entries = pathlib.Path(f"/proc/{server.pid}/status").read_text().splitlines()
            return int(next(line.split()[1] for line in entries if line.startswith("VmRSS:"))) * 1024
        baseline, peak, peak_buffer, peak_staged = rss(), rss(), 0, 0
        def upload():
            with source.open("rb") as stream:
                return client.request("PUT", "/stream-bucket/large", stream, {
                    "Content-MD5": base64.b64encode(md5.digest()).decode(),
                    "x-amz-checksum-crc32": base64.b64encode(crc32.to_bytes(4, "big")).decode()})
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            task = pool.submit(upload)
            while not task.done():
                peak = max(peak, rss())
                gauges = metrics(client)
                peak_buffer = max(peak_buffer, gauges["moon_s3_buffered_request_bytes"])
                peak_staged = max(peak_staged, gauges["moon_s3_staged_request_bytes"])
                time.sleep(0.01)
            metadata, _ = task.result()
        elapsed = time.monotonic() - started
        assert metadata["etag"] == '"' + md5.hexdigest() + '"'
        assert peak_buffer <= 32768 and peak_staged > 1024 * 1024
        assert peak - baseline < 32 * 1024 * 1024, (baseline, peak)
        with (directory / "download.bin").open("wb") as stream:
            client.request("GET", "/stream-bucket/large", output=stream)
        downloaded_hash = hashlib.sha256()
        with (directory / "download.bin").open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                downloaded_hash.update(chunk)
        downloaded = downloaded_hash.hexdigest()
        assert downloaded == digest.hexdigest()
        assert metrics(client)["moon_s3_staged_request_bytes"] == 0
        def concurrent_upload(number):
            with source.open("rb") as stream:
                return client.request("PUT", f"/stream-bucket/concurrent-{number}", stream)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(concurrent_upload, number) for number in range(2)]
            while not all(task.done() for task in tasks):
                gauges = metrics(client)
                assert gauges["moon_s3_buffered_request_bytes"] <= 32768
                time.sleep(0.01)
            for task in tasks:
                assert task.result()[0]["etag"] == '"' + md5.hexdigest() + '"'
    return dict(bytes=size, seconds=round(elapsed, 3), baseline_rss_bytes=baseline, peak_rss_bytes=peak,
                peak_buffered_bytes=peak_buffer, peak_staged_bytes=peak_staged, concurrent_uploads=2, sha256=downloaded)


def main():
    parser = argparse.ArgumentParser(description="Verify streaming upload safety and Linux server memory on a 128 MiB round trip.")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with tempfile.TemporaryDirectory(prefix="moon-s3-streaming-") as temporary:
        directory = pathlib.Path(temporary)
        safety(directory)
        result = memory(directory)
    print(json.dumps(result), flush=True)
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
