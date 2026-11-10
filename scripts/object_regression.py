#!/usr/bin/env python3
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
                for attempt in range(100):
                    if server.poll() is not None:
                        raise RuntimeError("server exited before becoming healthy")
                    try:
                        if request("GET", "/healthz")[0] == 200:
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                else:
                    raise RuntimeError("server did not become healthy")

                assert request("PUT", "/download-bucket")[0] == 200
                payload = bytes(range(256)) * 1024
                status, uploaded, _ = request("PUT", body=payload, headers={"Content-Type": "application/octet-stream"})
                assert status == 200
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
                print("Object regression passed: ranges, ETag conditions, If-Range, HEAD and empty objects.")
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
