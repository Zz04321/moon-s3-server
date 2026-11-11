#!/usr/bin/env python3
import datetime
import hashlib
import hmac
import http.client
import os
import pathlib
import socket
import subprocess
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET


PROJECT = pathlib.Path(__file__).resolve().parents[1]
ACCESS_KEY = "regression-access"
SECRET_KEY = "regression-secret"


def sign(method, path, host, body=b"", payload_hash=None, secret=SECRET_KEY,
         signed_headers=None, date=None, scope_date=None):
    date = date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scope_date = scope_date or date[:8]
    payload_hash = payload_hash or hashlib.sha256(body).hexdigest()
    headers = {"host": host, "x-amz-date": date, "x-amz-content-sha256": payload_hash}
    names = signed_headers or ["host", "x-amz-content-sha256", "x-amz-date"]
    raw_path, _, query = path.partition("?")
    pairs = []
    for part in filter(None, query.split("&")):
        name, _, value = part.partition("=")
        pairs.append((urllib.parse.quote(urllib.parse.unquote(name), safe="-_.~"),
                      urllib.parse.quote(urllib.parse.unquote(value), safe="-_.~")))
    canonical_query = "&".join(name + "=" + value for name, value in sorted(pairs))
    canonical_headers = "".join(name + ":" + headers.get(name, "").strip() + "\n" for name in names)
    canonical_request = "\n".join([method, raw_path, canonical_query, canonical_headers,
                                   ";".join(names), payload_hash])
    scope = scope_date + "/us-east-1/s3/aws4_request"
    string_to_sign = "AWS4-HMAC-SHA256\n" + date + "\n" + scope + "\n" + hashlib.sha256(canonical_request.encode()).hexdigest()
    key = ("AWS4" + secret).encode()
    for value in [scope_date, "us-east-1", "s3", "aws4_request"]:
        key = hmac.new(key, value.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={ACCESS_KEY}/{scope}, "
                                f"SignedHeaders={';'.join(names)}, Signature={signature}")
    return headers


def main():
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    host = f"127.0.0.1:{port}"

    def request(method, path, body=b"", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def signed(method, path, body=b"", **options):
        return request(method, path, body, sign(method, path, host, body, **options))

    with tempfile.TemporaryDirectory(prefix="moon-s3-auth-") as directory:
        environment = dict(os.environ, MOON_S3_ROOT=directory, MOON_S3_LISTEN=host,
                           MOON_S3_ACCESS_KEY=ACCESS_KEY, MOON_S3_SECRET_KEY=SECRET_KEY)
        with tempfile.TemporaryFile() as server_log:
            server = subprocess.Popen([str(PROJECT / "_build/native/debug/build/cmd/main/main.exe")],
                                      env=environment, stdout=server_log, stderr=server_log)
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
                assert request("GET", "/")[0] == 403
                assert signed("PUT", "/auth-bucket")[0] == 200
                path = "/auth-bucket/folder/%E5%A4%B4%E5%83%8F%2B1.txt"
                original = b"original contents"
                assert signed("PUT", path, original)[0] == 200
                assert signed("GET", path) == (200, original)
                for options in [
                    {"payload_hash": hashlib.sha256(original).hexdigest()},
                    {"payload_hash": "0" * 64},
                    {"payload_hash": "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"},
                    {"secret": "wrong-secret"},
                ]:
                    status, body = signed("PUT", path, b"tampered contents", **options)
                    assert status == 403, (options, status, body)
                    assert ET.fromstring(body).findtext("Code") == "AccessDenied"
                    assert signed("GET", path) == (200, original)
                altered = sign("GET", path, host)
                altered["authorization"] = altered["authorization"][:-1] + ("0" if altered["authorization"][-1] != "0" else "1")
                assert request("GET", path, headers=altered)[0] == 403
                assert signed("PUT", path, b"unsigned", payload_hash="UNSIGNED-PAYLOAD")[0] == 200
                assert signed("GET", path) == (200, b"unsigned")
                assert signed("GET", "/auth-bucket?list-type=2&prefix=folder%2F&max-keys=1")[0] == 200
                for names in [
                    ["x-amz-content-sha256", "x-amz-date"],
                    ["host", "x-amz-content-sha256"],
                    ["host", "x-amz-date", "x-amz-content-sha256"],
                    ["host", "host", "x-amz-date"],
                    ["host", "x-amz-date", "x-missing"],
                    ["Host", "x-amz-date"],
                ]:
                    assert signed("GET", path, signed_headers=names)[0] == 403, names
                assert signed("GET", path, signed_headers=["host", "x-amz-date"])[0] == 200
                for date in ["20261110", "20261110T120000", "20261110X120000Z", "20261110T12000xZ"]:
                    assert signed("GET", path, date=date)[0] == 403, date
                assert signed("GET", path, scope_date="20000101")[0] == 403
                now = datetime.datetime.now(datetime.timezone.utc)
                for minutes, expected in [(-16, 403), (16, 403), (-14, 200), (14, 200)]:
                    date = (now + datetime.timedelta(minutes=minutes)).strftime("%Y%m%dT%H%M%SZ")
                    assert signed("GET", path, date=date)[0] == expected
                for date in ["20260230T120000Z", "20261301T120000Z", "20261111T250000Z"]:
                    assert signed("GET", path, date=date)[0] == 403
                query_path = "/auth-bucket?list-type=2&max-keys=1"
                headers = sign("GET", query_path, host)
                assert request("GET", query_path.replace("max-keys=1", "max-keys=2"), headers=headers)[0] == 403
                assert request("GET", "/auth-bucket/another-key", headers=sign("GET", path, host))[0] == 403
                assert request("DELETE", path, headers=sign("GET", path, host))[0] == 403
                assert signed("GET", path) == (200, b"unsigned")
                print("Auth regression passed: signed requests, payload integrity and tampering rejection.")
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
