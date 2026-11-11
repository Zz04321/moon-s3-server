#!/usr/bin/env python3
import datetime
import email.utils
import http.client
import os
import pathlib
import socket
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET


PROJECT = pathlib.Path(__file__).resolve().parents[1]


def main():
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    def request(method, path="/download-bucket/object", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def wait_healthy(server):
        for attempt in range(100):
            if server.poll() is not None:
                raise RuntimeError("server exited before becoming healthy")
            try:
                if request("GET", "/healthz")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.05)
        raise RuntimeError("server did not become healthy")

    def xml_time(value):
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

    with tempfile.TemporaryDirectory(prefix="moon-s3-object-") as directory:
        environment = dict(os.environ)
        environment.pop("MOON_S3_ACCESS_KEY", None)
        environment.pop("MOON_S3_SECRET_KEY", None)
        environment.update(MOON_S3_ROOT=directory, MOON_S3_LISTEN=f"127.0.0.1:{port}")
        with tempfile.TemporaryFile() as server_log:
            server = subprocess.Popen(
                [str(PROJECT / "_build/native/debug/build/cmd/main/main.exe")],
                env=environment, stdout=server_log, stderr=server_log,
            )
            try:
                wait_healthy(server)

                before_creation = int(time.time())
                assert request("PUT", "/download-bucket")[0] == 200
                namespace = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
                buckets = ET.fromstring(request("GET", "/")[2])
                creation = buckets.findtext("s3:Buckets/s3:Bucket/s3:CreationDate", namespaces=namespace)
                assert before_creation <= xml_time(creation) <= time.time()
                payload = bytes(range(256)) * 1024
                before_upload = int(time.time())
                status, uploaded, _ = request("PUT", body=payload, headers={"Content-Type": "application/octet-stream"})
                assert status == 200
                status, object_headers, _ = request("HEAD")
                last_modified = object_headers["Last-Modified"]
                assert before_upload <= email.utils.parsedate_to_datetime(last_modified).timestamp() <= time.time()
                for value, start, end in [
                    ("bytes=2-5", 2, 6),
                    ("bytes=65530-200000", 65530, 200001),
                    ("bytes=200000-", 200000, len(payload)),
                    ("bytes=-1024", len(payload) - 1024, len(payload)),
                    ("bytes=0-999999999999999999999", 0, len(payload)),
                    ("bytes=-999999999999999999999", 0, len(payload)),
                ]:
                    status, headers, body = request("GET", headers={"Range": value})
                    assert status == 206, (value, status, body)
                    assert body == payload[start:end], value
                    assert headers["Content-Length"] == str(end - start)
                    assert headers["Content-Range"] == f"bytes {start}-{end - 1}/{len(payload)}"
                    assert headers["ETag"] == uploaded["ETag"]
                    assert headers["Last-Modified"] == last_modified
                    assert headers["Accept-Ranges"] == "bytes"
                for value in [f"bytes={len(payload)}-", "bytes=-0", "bytes=999999999999999999999-"]:
                    status, headers, body = request("GET", headers={"Range": value})
                    assert (status, body) == (416, b"")
                    assert headers["Content-Range"] == f"bytes */{len(payload)}"
                for value in ["items=0-2", "bytes=a-b", "bytes=5-2", "bytes=0-1,3-4"]:
                    status, _, body = request("GET", headers={"Range": value})
                    assert (status, body) == (200, payload)
                status, headers, body = request("HEAD", headers={"Range": "bytes=2-5"})
                assert (status, body) == (200, b"")
                assert headers["Content-Length"] == str(len(payload))
                etag = uploaded["ETag"]
                for method in ["GET", "HEAD"]:
                    for condition in ["*", etag, "W/" + etag, '"other", ' + etag]:
                        status, headers, body = request(method, headers={"If-None-Match": condition})
                        assert (status, body) == (304, b""), (method, condition, status)
                        assert headers["ETag"] == etag
                    for condition in ['"other"', "W/" + etag]:
                        status, headers, body = request(method, headers={"If-Match": condition})
                        assert status == 412, (method, condition, status)
                        if method == "HEAD":
                            assert body == b""
                        else:
                            assert ET.fromstring(body).findtext("Code") == "PreconditionFailed"
                        assert headers["ETag"] == etag
                    for condition in ["*", etag, '"other", ' + etag]:
                        status, _, body = request(method, headers={"If-Match": condition})
                        assert status == 200
                        assert body == (payload if method == "GET" else b"")
                    assert request(method, headers={"If-None-Match": '"other"'})[0] == 200
                for conditions, expected in [
                    ({"If-Match": '"other"', "If-None-Match": "*"}, 412),
                    ({"If-None-Match": etag, "Range": "bytes=0-5"}, 304),
                    ({"If-Match": '"other"', "Range": "bytes=999999999-"}, 412),
                ]:
                    status, _, body = request("GET", headers=conditions)
                    assert status == expected
                    if expected == 304:
                        assert body == b""
                    else:
                        assert ET.fromstring(body).findtext("Code") == "PreconditionFailed"
                for condition in [etag, '"old"', "W/" + etag, "Wed, 21 Oct 2015 07:28:00 GMT"]:
                    status, _, body = request("GET", headers={"If-Range": condition, "Range": "bytes=2-5"})
                    assert status == (206 if condition == etag else 200)
                    assert body == (payload[2:6] if condition == etag else payload)
                request("PUT", body=b"updated")
                assert request("GET", headers={"If-None-Match": etag})[2] == b"updated"
                assert request("GET", headers={"If-Match": etag})[0] == 412
                assert request("PUT", "/download-bucket/empty", body=b"")[0] == 200
                status, headers, body = request("GET", "/download-bucket/empty", headers={"Range": "bytes=0-"})
                assert (status, body) == (416, b"")
                assert headers["Content-Range"] == "bytes */0"
                assert request("GET", "/download-bucket/empty")[2] == b""

                object_file = pathlib.Path(directory) / "download-bucket/objects" / ("object".encode().hex() + ".object")
                fixed_time = int(datetime.datetime(2024, 2, 29, 12, 34, 56, tzinfo=datetime.timezone.utc).timestamp())
                os.utime(object_file, (fixed_time, fixed_time + 0.75))
                exact_date = email.utils.formatdate(fixed_time, usegmt=True)
                before_date = email.utils.formatdate(fixed_time - 1, usegmt=True)
                after_date = email.utils.formatdate(fixed_time + 1, usegmt=True)
                etag = request("HEAD")[1]["ETag"]
                for method in ["GET", "HEAD"]:
                    for conditions, expected in [
                        ({"If-Modified-Since": before_date}, 200),
                        ({"If-Modified-Since": exact_date}, 304),
                        ({"If-Modified-Since": after_date}, 304),
                        ({"If-Unmodified-Since": before_date}, 412),
                        ({"If-Unmodified-Since": exact_date}, 200),
                        ({"If-Unmodified-Since": after_date}, 200),
                        ({"If-Match": etag, "If-Unmodified-Since": before_date}, 200),
                        ({"If-None-Match": '"other"', "If-Modified-Since": after_date}, 200),
                        ({"If-Match": '"other"', "If-Modified-Since": before_date}, 412),
                        ({"If-Modified-Since": "invalid", "If-Unmodified-Since": "invalid"}, 200),
                    ]:
                        status, headers, body = request(method, headers=conditions)
                        assert status == expected, (method, conditions, status, body)
                        assert headers["Last-Modified"] == exact_date
                        assert "Last-Modified" in headers["Access-Control-Expose-Headers"]
                        if method == "HEAD" or expected == 304:
                            assert body == b""
                        elif expected == 200:
                            assert body == b"updated"
                        else:
                            assert ET.fromstring(body).findtext("Code") == "PreconditionFailed"
                for condition, expected in [(exact_date, 206), (before_date, 200), (after_date, 200), ("invalid", 200)]:
                    status, headers, body = request("GET", headers={"If-Range": condition, "Range": "bytes=0-2"})
                    assert status == expected
                    assert body == (b"upd" if expected == 206 else b"updated")
                    assert headers["Last-Modified"] == exact_date
                status, _, body = request("GET", headers={"If-Modified-Since": exact_date, "Range": "bytes=999999-"})
                assert (status, body) == (304, b"")
                listing = ET.fromstring(request("GET", "/download-bucket?list-type=2&prefix=object")[2])
                listed_time = listing.findtext("s3:Contents/s3:LastModified", namespaces=namespace)
                assert xml_time(listed_time) == fixed_time
                assert request("GET", "/")[0] == 200
                assert ET.fromstring(request("GET", "/")[2]).findtext("s3:Buckets/s3:Bucket/s3:CreationDate", namespaces=namespace) == creation

                server.terminate()
                server.wait(timeout=5)
                server = subprocess.Popen(
                    [str(PROJECT / "_build/native/debug/build/cmd/main/main.exe")],
                    env=environment, stdout=server_log, stderr=server_log,
                )
                wait_healthy(server)
                assert request("HEAD")[1]["Last-Modified"] == exact_date
                assert request("GET", headers={"If-Modified-Since": exact_date})[0] == 304
                assert ET.fromstring(request("GET", "/")[2]).findtext("s3:Buckets/s3:Bucket/s3:CreationDate", namespaces=namespace) == creation
                assert request("PUT", body=b"replacement")[0] == 200
                assert request("HEAD")[1]["Last-Modified"] != exact_date
                assert request("GET", headers={"If-Modified-Since": exact_date})[2] == b"replacement"
                assert ET.fromstring(request("GET", "/")[2]).findtext("s3:Buckets/s3:Bucket/s3:CreationDate", namespaces=namespace) == creation
                bucket_directory = pathlib.Path(directory) / "download-bucket"
                (bucket_directory / ".created").unlink()
                fallback_time = int(bucket_directory.stat().st_mtime)
                legacy_creation = ET.fromstring(request("GET", "/")[2]).findtext("s3:Buckets/s3:Bucket/s3:CreationDate", namespaces=namespace)
                assert xml_time(legacy_creation) == fallback_time
                print("Object regression passed: ranges, ETag/date conditions, persisted times, restart and legacy buckets.")
            except BaseException:
                server_log.seek(0)
                print(server_log.read().decode(errors="replace"))
                raise
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    main()
