#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import hashlib
import http.client
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from auth_regression import ACCESS_KEY, SECRET_KEY, sign
from presign import presign


PROJECT = pathlib.Path(__file__).resolve().parents[1]
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def configuration(rules):
    document = ET.Element("CORSConfiguration", xmlns=NS["s3"])
    fields = {"ID": "ID", "AllowedOrigins": "AllowedOrigin", "AllowedMethods": "AllowedMethod",
              "AllowedHeaders": "AllowedHeader", "ExposeHeaders": "ExposeHeader", "MaxAgeSeconds": "MaxAgeSeconds"}
    for rule in rules:
        element = ET.SubElement(document, "CORSRule")
        for key, value in rule.items():
            for item in value if isinstance(value, list) else [value]:
                ET.SubElement(element, fields[key]).text = str(item)
    return ET.tostring(document, encoding="utf-8", short_empty_elements=False)


def main():
    parser = argparse.ArgumentParser(description="Test bucket CORS configuration, preflight, persistence and backups.")
    parser.add_argument("--sdk", action="store_true", help="also verify botocore CORS operations")
    arguments = parser.parse_args()
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with tempfile.TemporaryDirectory(prefix="moon-s3-cors-") as directory:
        base = pathlib.Path(directory)
        data = base / "data"
        root = data
        port = None
        server = None
        bucket = "/cors-bucket"
        target = bucket + "/object"
        rules = [
            {"ID": "editor & uploader", "AllowedOrigins": ["https://*.example.test"], "AllowedMethods": ["PUT", "GET", "HEAD", "POST"],
             "AllowedHeaders": ["x-amz-*", "Content-Type", "Authorization"], "ExposeHeaders": ["ETag", "Content-Range"], "MaxAgeSeconds": 120},
            {"ID": "public-read", "AllowedOrigins": ["*"], "AllowedMethods": ["GET"], "ExposeHeaders": ["Last-Modified"]},
        ]
        xml = configuration(rules)

        def request(method, path, body=b"", headers=None, authenticated=True):
            outgoing = sign(method, path, f"127.0.0.1:{port}", body, extra_headers=headers) if authenticated else headers or {}
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                connection.request(method, path, body=body, headers=outgoing)
                response = connection.getresponse()
                return response.status, {name.lower(): value for name, value in response.getheaders()}, response.read()
            finally:
                connection.close()

        def preflight(origin="https://app.example.test", method="PUT", headers="X-Amz-Date, Content-Type", path=target):
            return request("OPTIONS", path, headers={"origin": origin, "access-control-request-method": method,
                                                      "access-control-request-headers": headers}, authenticated=False)

        def error(response, status, code):
            actual, _, body = response
            assert actual == status and ET.fromstring(body).findtext("Code") == code, (actual, body)

        def stop():
            nonlocal server
            if server is not None:
                server.kill()
                server.wait(timeout=5)
                server = None

        with tempfile.TemporaryFile() as log:
            def start():
                nonlocal server, port
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                environment = dict(os.environ, MOON_S3_ROOT=str(root), MOON_S3_LISTEN=f"127.0.0.1:{port}",
                                   MOON_S3_ACCESS_KEY=ACCESS_KEY, MOON_S3_SECRET_KEY=SECRET_KEY,
                                   MOON_S3_CORS_ORIGIN="https://fallback.test")
                server = subprocess.Popen([str(PROJECT / "_build/native/debug/build/cmd/main/main.exe")], env=environment,
                                          stdout=log, stderr=log)
                for attempt in range(200):
                    if server.poll() is not None:
                        raise RuntimeError("server exited before becoming healthy")
                    try:
                        if request("GET", "/healthz", authenticated=False)[0] == 200:
                            return
                    except OSError:
                        pass
                    time.sleep(0.025)
                raise RuntimeError("server did not become healthy")

            def admin(*args, success=True):
                result = subprocess.run([sys.executable, str(PROJECT / "scripts/admin.py"), *map(str, args)],
                                        text=True, capture_output=True, timeout=30)
                assert (result.returncode == 0) == success, (result.stdout, result.stderr)
                return json.loads(result.stdout if success else result.stderr)

            try:
                start()
                assert request("PUT", bucket)[0] == 200
                assert request("PUT", "/other-bucket")[0] == 200
                assert request("PUT", target, b"contents")[0] == 200
                error(request("GET", bucket + "?cors"), 404, "NoSuchCORSConfiguration")
                assert preflight()[1]["access-control-allow-origin"] == "https://fallback.test"
                error(request("PUT", bucket + "?cors", xml, authenticated=False), 403, "AccessDenied")
                for verb in ["GET", "DELETE"]:
                    error(request(verb, bucket + "?cors", authenticated=False), 403, "AccessDenied")
                digest = base64.b64encode(hashlib.md5(xml).digest()).decode()
                assert request("PUT", bucket + "?cors", xml, {"content-md5": digest})[0] == 200
                saved = request("GET", bucket + "?cors")[2]
                assert len(ET.fromstring(saved).findall("s3:CORSRule", NS)) == 2
                status, headers, body = preflight()
                assert status == 204 and body == b""
                assert headers["access-control-allow-origin"] == "https://app.example.test"
                assert headers["access-control-allow-credentials"] == "true"
                assert headers["access-control-max-age"] == "120"
                assert {item.strip().lower() for item in headers["access-control-allow-headers"].split(",")} == {"x-amz-date", "content-type"}
                assert "Origin" in headers["vary"]
                for options in [dict(origin="https://app.example.test.evil"), dict(method="DELETE"), dict(headers="X-Other")]:
                    response = preflight(**options)
                    error(response, 403, "AccessForbidden")
                    assert "access-control-allow-origin" not in response[1]
                error(preflight(headers="bad header"), 400, "InvalidRequest")
                error(preflight(method="OPTIONS"), 400, "InvalidRequest")
                error(request("OPTIONS", target, authenticated=False), 400, "InvalidRequest")
                error(preflight(path="/missing-bucket/object"), 404, "NoSuchBucket")
                assert preflight(path="/other-bucket/object")[1]["access-control-allow-origin"] == "https://fallback.test"
                other_rules = [{"AllowedOrigins": ["https://other.test"], "AllowedMethods": ["GET", "PUT"], "AllowedHeaders": ["*"], "MaxAgeSeconds": 0}]
                assert request("PUT", "/other-bucket?cors", configuration(other_rules))[0] == 200
                assert preflight(origin="https://other.test", headers="X-Custom, aUThOrIZation", path="/other-bucket/object")[1]["access-control-max-age"] == "0"
                assert preflight(path="/other-bucket/object")[0] == 403
                assert preflight()[0] == 204
                other_headers = request("GET", "/other-bucket", headers={"origin": "https://other.test"})[1]
                assert other_headers["access-control-allow-origin"] == "https://other.test"
                assert "access-control-expose-headers" not in other_headers

                for verb, path, options, expected_status in [
                    ("GET", target, {}, 200), ("HEAD", target, {}, 200),
                    ("GET", target, {"headers": {"range": "bytes=0-2"}}, 206),
                    ("GET", target, {"headers": {"range": "bytes=999-"}}, 416),
                    ("GET", target, {"headers": {"if-none-match": '"' + hashlib.md5(b"contents").hexdigest() + '"'}}, 304),
                    ("GET", bucket + "/absent", {}, 404),
                    ("GET", target, {"authenticated": False}, 403),
                    ("POST", target + "?uploads", {}, 200),
                ]:
                    outgoing = {"origin": "https://app.example.test", **options.pop("headers", {})}
                    status, headers, body = request(verb, path, headers=outgoing, **options)
                    assert status == expected_status, (verb, path, status, body)
                    assert headers["access-control-allow-origin"] == "https://app.example.test"
                    assert headers["access-control-expose-headers"] == "ETag, Content-Range"
                    if verb == "POST":
                        upload = ET.fromstring(body).findtext("s3:UploadId", namespaces=NS)
                        assert request("DELETE", target + "?uploadId=" + upload)[0] == 204
                response = request("DELETE", bucket + "/missing", headers={"origin": "https://app.example.test"})
                assert response[0] == 204 and "access-control-allow-origin" not in response[1]
                assert "access-control-allow-origin" not in request("GET", target)[1]
                public = request("GET", target, headers={"origin": "https://unrelated.test"})[1]
                assert public["access-control-allow-origin"] == "*" and public["access-control-expose-headers"] == "Last-Modified"
                assert "access-control-allow-credentials" not in public
                assert preflight(origin="https://unrelated.test", method="GET", headers="")[0] == 204
                assert preflight(origin="https://unrelated.test", method="GET", headers="authorization")[0] == 403
                keepalive = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    for origin, expected in [("https://app.example.test", "https://app.example.test"), ("https://unrelated.test", "*"), (None, None)]:
                        outgoing = {"origin": origin} if origin else {}
                        keepalive.request("GET", target, headers=sign("GET", target, f"127.0.0.1:{port}", extra_headers=outgoing))
                        response = keepalive.getresponse()
                        assert response.status == 200 and response.read() == b"contents"
                        assert response.getheader("Access-Control-Allow-Origin") == expected
                finally:
                    keepalive.close()
                response = request("PUT", target + "?uploadId=missing&partNumber=1", b"part", {"origin": "https://app.example.test"})
                assert response[0] == 404 and response[1]["access-control-allow-origin"] == "https://app.example.test"
                response = request("PUT", bucket + "/copy", headers={"x-amz-copy-source": target, "origin": "https://app.example.test"})
                assert response[0] == 200 and response[1]["access-control-allow-origin"] == "https://app.example.test"

                for document in [b"", b"<!DOCTYPE a><CORSConfiguration/>", b"<wrong/>", xml + b"trailing", xml.replace(b"<ID>", b"<Unknown>"), b"\xff"]:
                    error(request("PUT", bucket + "?cors", document), 400, "MalformedXML")
                for invalid in [[], [{"AllowedOrigins": ["*"], "AllowedMethods": ["OPTIONS"]}],
                                [{"AllowedOrigins": ["**"], "AllowedMethods": ["GET"]}],
                                [{"AllowedOrigins": ["*"], "AllowedMethods": ["GET"], "ExposeHeaders": ["*"]}]]:
                    error(request("PUT", bucket + "?cors", configuration(invalid)), 400, "InvalidRequest")
                error(request("PUT", bucket + "?cors", b" " * 65537), 400, "MalformedXML")
                error(request("PUT", bucket + "?cors", xml, {"content-md5": "wrong"}), 400, "InvalidDigest")
                assert request("GET", bucket + "?cors")[2] == saved
                too_many = [{"AllowedOrigins": ["*"], "AllowedMethods": ["GET"]}] * 101
                error(request("PUT", bucket + "?cors", configuration(too_many)), 400, "MalformedXML")
                hundred = too_many[:100]
                assert request("PUT", bucket + "?cors", configuration(hundred))[0] == 200
                assert request("PUT", bucket + "?cors", xml)[0] == 200
                expanded = configuration([{"ID": '"' * 250 + str(number), "AllowedOrigins": ["*"], "AllowedMethods": ["GET"]} for number in range(100)])
                assert len(expanded) <= 65536
                error(request("PUT", bucket + "?cors", expanded), 400, "MalformedXML")
                assert request("GET", bucket + "?cors")[2] == saved
                temporary_failure = data / "cors-bucket/.cors.tmp"
                temporary_failure.mkdir()
                (temporary_failure / "blocked").write_bytes(b"failure injection")
                error(request("PUT", bucket + "?cors", xml), 500, "InternalError")
                assert request("GET", bucket + "?cors")[2] == saved
                (temporary_failure / "blocked").unlink()
                temporary_failure.rmdir()
                for verb, path, code in [("GET", bucket + "?cors&cors", "InvalidRequest"), ("GET", target + "?cors", "InvalidRequest"),
                                         ("GET", "/?cors", "InvalidRequest"), ("GET", bucket + "?cors=invalid", "InvalidRequest"),
                                         ("GET", "/missing-bucket?cors", "NoSuchBucket"), ("POST", bucket + "?cors", "MethodNotAllowed")]:
                    error(request(verb, path), 404 if code == "NoSuchBucket" else 405 if code == "MethodNotAllowed" else 400, code)

                link = presign(f"http://127.0.0.1:{port}", "cors-bucket", "object", ACCESS_KEY, SECRET_KEY)
                path = link.split(f"http://127.0.0.1:{port}", 1)[1]
                response = request("GET", path, headers={"origin": "https://app.example.test"}, authenticated=False)
                assert response[0] == 200 and response[1]["access-control-allow-origin"] == "https://app.example.test"

                def concurrent_request(number):
                    if number % 2:
                        assert request("PUT", bucket + "?cors", xml)[0] == 200
                    else:
                        assert preflight()[1]["access-control-max-age"] == "120"

                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    list(executor.map(concurrent_request, range(20)))
                stop()
                temporary = data / "cors-bucket/.cors.tmp"
                temporary.write_bytes(b"interrupted update")
                assert "recover" in admin("backup", "--root", data, "--output", base / "backup.tar", success=False)["error"]
                start()
                assert not temporary.exists()
                assert request("GET", bucket + "?cors")[2] == saved
                assert preflight()[1]["access-control-max-age"] == "120"
                stop()
                admin("check", "--root", data)
                admin("backup", "--root", data, "--output", base / "backup.tar")
                root = base / "restored"
                admin("restore", "--archive", base / "backup.tar", "--root", root)
                start()
                assert request("GET", bucket + "?cors")[2] == saved
                assert preflight()[1]["access-control-max-age"] == "120"
                corrupt = root / "cors-bucket/.cors"
                corrupt.write_bytes(b"broken XML")
                error(request("GET", target), 500, "InternalError")
                assert "access-control-allow-origin" not in request("GET", target, headers={"origin": "https://app.example.test"})[1]
                assert request("DELETE", bucket + "?cors")[0] == 204
                error(request("GET", bucket + "?cors"), 404, "NoSuchCORSConfiguration")
                assert preflight()[1]["access-control-allow-origin"] == "https://fallback.test"
                assert request("DELETE", bucket + "?cors")[0] == 204
                assert request("PUT", bucket + "?cors", xml)[0] == 200
                corrupt.write_bytes(b"broken XML")
                assert request("PUT", bucket + "?cors", xml)[0] == 200
                assert preflight()[0] == 204
                print("CORS HTTP regression passed: authenticated CRUD, matching, preflight, errors, persistence, concurrency and backup/restore.")

                if arguments.sdk:
                    import botocore.config
                    import botocore.session
                    client = botocore.session.get_session().create_client(
                        "s3", endpoint_url=f"http://127.0.0.1:{port}", region_name="us-east-1",
                        aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY,
                        config=botocore.config.Config(signature_version="s3v4", s3={"addressing_style": "path"}, proxies={},
                                                       request_checksum_calculation="when_required", response_checksum_validation="when_required"),
                    )
                    try:
                        client.put_bucket_cors(Bucket="cors-bucket", CORSConfiguration={"CORSRules": rules})
                        assert client.get_bucket_cors(Bucket="cors-bucket")["CORSRules"] == rules
                        client.delete_bucket_cors(Bucket="cors-bucket")
                        try:
                            client.get_bucket_cors(Bucket="cors-bucket")
                        except client.exceptions.ClientError as error_result:
                            assert error_result.response["Error"]["Code"] == "NoSuchCORSConfiguration"
                        else:
                            raise AssertionError("Deleted CORS configuration should be missing")
                        assert preflight()[1]["access-control-allow-origin"] == "https://fallback.test"
                        print("Botocore CORS passed: put/get/delete and browser preflight behavior.")
                    finally:
                        client.close()
            except BaseException:
                log.seek(0)
                print(log.read().decode(errors="replace"))
                raise
            finally:
                stop()


if __name__ == "__main__":
    main()
