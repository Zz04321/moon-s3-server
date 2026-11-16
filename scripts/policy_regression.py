#!/usr/bin/env python3
import argparse
import concurrent.futures
import datetime
import http.client
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET

from auth_regression import ACCESS_KEY, SECRET_KEY, sign
from presign import presign


PROJECT = pathlib.Path(__file__).resolve().parents[1]


def policy(actions="s3:GetObject", bucket="policy-bucket"):
    return {"Version": "2012-10-17", "Statement": [{"Sid": "PublicRead", "Effect": "Allow",
            "Principal": "*", "Action": actions, "Resource": f"arn:aws:s3:::{bucket}/*"}]}


def main():
    parser = argparse.ArgumentParser(description="Verify bucket public-read policies and private access boundaries.")
    parser.add_argument("--sdk", action="store_true")
    arguments = parser.parse_args()
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with tempfile.TemporaryDirectory(prefix="moon-s3-policy-") as directory:
        base = pathlib.Path(directory)
        root = base / "data"
        bucket = "/policy-bucket"
        target = bucket + "/" + urllib.parse.quote("目录/read +%.txt", safe="/")
        server = None
        port = None

        def request(method, path=target, body=b"", headers=None, authenticated=True):
            outgoing = sign(method, path, f"127.0.0.1:{port}", body, extra_headers=headers) if authenticated else headers or {}
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                connection.request(method, path, body=body, headers=outgoing)
                response = connection.getresponse()
                return response.status, {name.lower(): value for name, value in response.getheaders()}, response.read()
            finally:
                connection.close()

        def setting(value):
            body = json.dumps(value, ensure_ascii=False).encode()
            result = request("PUT", bucket + "?policy", body)
            assert result[0] == 204, result
            return body

        def error(result, status, code):
            assert result[0] == status and ET.fromstring(result[2]).findtext("Code") == code, result

        def denied(method="GET", path=target, **kwargs):
            result = request(method, path, authenticated=False, **kwargs)
            assert result[0] == 403, result
            if method != "HEAD":
                assert ET.fromstring(result[2]).findtext("Code") == "AccessDenied", result

        def stop():
            nonlocal server
            if server is not None:
                server.kill()
                server.wait(timeout=5)
                server = None

        def admin(*args, success=True):
            result = subprocess.run([sys.executable, str(PROJECT / "scripts/admin.py"), *map(str, args)],
                                    capture_output=True, text=True, timeout=60)
            assert (result.returncode == 0) == success, (result.stdout, result.stderr)
            return json.loads(result.stdout if success else result.stderr)

        with tempfile.TemporaryFile() as log:
            def start(authenticated=True):
                nonlocal server, port
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                environment = dict(os.environ, MOON_S3_ROOT=str(root), MOON_S3_LISTEN=f"127.0.0.1:{port}")
                for name in ["MOON_S3_ACCESS_KEY", "MOON_S3_SECRET_KEY"]:
                    environment.pop(name, None)
                if authenticated:
                    environment.update(MOON_S3_ACCESS_KEY=ACCESS_KEY, MOON_S3_SECRET_KEY=SECRET_KEY)
                server = subprocess.Popen([str(PROJECT / "_build/native/debug/build/cmd/main/main.exe")],
                                          env=environment, stdout=log, stderr=log)
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

            try:
                start()
                assert request("PUT", bucket)[0] == 200
                assert request("PUT", "/private-bucket")[0] == 200
                assert request("PUT", "/private-bucket/object", b"private")[0] == 200
                assert request("PUT", target, b"public content", {"content-type": "application/policy"})[0] == 200
                denied()
                denied("HEAD")
                error(request("GET", bucket + "?policy"), 404, "NoSuchBucketPolicy")
                original = setting(policy())
                response = request("GET", bucket + "?policy")
                assert response[1]["content-type"] == "application/json" and response[2] == original
                assert request("GET", authenticated=False)[2] == b"public content"
                assert request("HEAD", authenticated=False)[1]["content-length"] == "14"
                assert request("GET", headers={"range": "bytes=0-5"}, authenticated=False)[2] == b"public"
                etag = request("HEAD", authenticated=False)[1]["etag"]
                assert request("GET", headers={"if-none-match": etag}, authenticated=False)[0] == 304
                assert request("GET", headers={"if-match": '"wrong"'}, authenticated=False)[0] == 412
                assert request("GET", bucket + "/missing", authenticated=False)[0] == 404
                denied(path="/private-bucket/object")
                denied(path="/nonexistent-bucket/object")
                for method, path in [("GET", "/"), ("GET", bucket), ("HEAD", bucket),
                                     ("GET", bucket + "?versions"), ("GET", bucket + "?policy"),
                                     ("PUT", bucket + "?policy"), ("DELETE", bucket + "?policy"),
                                     ("GET", bucket + "?versioning"), ("GET", bucket + "?cors"),
                                     ("PUT", target), ("DELETE", target), ("POST", target + "?uploads"),
                                     ("GET", target + "?uploadId=" + "a" * 64),
                                     ("GET", target + "?acl"), ("GET", target + "?policy")]:
                    denied(method, path)
                for headers in [{"authorization": "invalid"}, {"authorization": ""},
                                {"x-amz-date": "20260101T000000Z"}, {"x-amz-security-token": "unsupported"}]:
                    denied(headers=headers)
                invalid_headers = sign("GET", target, f"127.0.0.1:{port}", b"")
                invalid_headers["authorization"] = "AWS4-HMAC-SHA256 invalid"
                denied(headers=invalid_headers)
                endpoint = f"http://127.0.0.1:{port}"
                link = presign(endpoint, "policy-bucket", "目录/read +%.txt", ACCESS_KEY, SECRET_KEY, method="GET", expires=60)
                parsed = urllib.parse.urlsplit(link)
                assert request("GET", parsed.path + "?" + parsed.query, authenticated=False)[0] == 200
                denied(path=parsed.path + "?" + parsed.query.replace("X-Amz-Signature=", "X-Amz-Signature=0"))
                denied(path=target + "?X-Amz-Algorithm=invalid")
                expired = presign(endpoint, "policy-bucket", "目录/read +%.txt", ACCESS_KEY, SECRET_KEY,
                                  expires=1, date=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=10))
                parsed_expired = urllib.parse.urlsplit(expired)
                denied(path=parsed_expired.path + "?" + parsed_expired.query)

                invalid = []
                for name, value in [("Action", "s3:*"), ("Action", "s3:PutObject"),
                                    ("Effect", "Deny"), ("Principal", {"AWS": "someone"}),
                                    ("Resource", "arn:aws:s3:::private-bucket/*"),
                                    ("Resource", "arn:aws:s3:::policy-bucket/prefix/*"),
                                    ("Condition", {}), ("NotAction", "s3:PutObject"),
                                    ("Action", []), ("Action", [True]), ("Sid", 1)]:
                    value_policy = policy()
                    value_policy["Statement"][0][name] = value
                    invalid.append(json.dumps(value_policy).encode())
                invalid += [b"{", b"null", b"\xff", b"{}" + b" " * 20480,
                            original.replace(b'"Effect":', b'"Effect":"Deny","Effect":'),
                            json.dumps({**policy(), "Unknown": True}).encode(),
                            json.dumps({**policy(), "Version": "2008-10-17"}).encode()]
                for body in invalid:
                    error(request("PUT", bucket + "?policy", body), 400, "MalformedPolicy")
                    assert request("GET", bucket + "?policy")[2] == original
                for path in [target + "?policy", bucket + "?policy&cors", bucket + "?policy&policy",
                             bucket + "?policy&uploadId=x", bucket + "?policy=value"]:
                    assert request("PUT", path, original)[0] == 400
                error(request("PUT", "/absent-bucket?policy", original), 404, "NoSuchBucket")
                cors = b"<CORSConfiguration><CORSRule><AllowedOrigin>https://app.test</AllowedOrigin><AllowedMethod>GET</AllowedMethod></CORSRule></CORSConfiguration>"
                assert request("PUT", bucket + "?cors", cors)[0] == 200
                assert request("OPTIONS", headers={"origin": "https://app.test", "access-control-request-method": "GET"}, authenticated=False)[0] == 204
                assert request("GET", headers={"origin": "https://app.test"}, authenticated=False)[1]["access-control-allow-origin"] == "https://app.test"

                xml = b"<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>"
                assert request("PUT", bucket + "?versioning", xml)[0] == 200
                version = request("PUT", target, b"versioned content")[1]["x-amz-version-id"]
                denied(path=target + "?versionId=" + version)
                denied(path=target + "?versionId=null")
                historical = policy("s3:GetObjectVersion")
                historical["Statement"][0]["Principal"] = {"AWS": "*"}
                historical["Statement"][0]["Resource"] = ["arn:aws:s3:::policy-bucket/*"]
                historical["Statement"] = historical["Statement"][0]
                setting(historical)
                denied()
                assert request("GET", target + "?versionId=" + version, authenticated=False)[2] == b"versioned content"
                assert request("GET", target + "?versionId=null", authenticated=False)[2] == b"public content"
                setting(policy(["s3:GetObject", "s3:GetObjectVersion"]))
                marker = request("DELETE")[1]["x-amz-version-id"]
                assert request("GET", authenticated=False)[0] == 404
                assert request("HEAD", target + "?versionId=" + marker, authenticated=False)[0] == 405
                assert request("DELETE", target + "?versionId=" + marker)[0] == 204
                assert request("GET", authenticated=False)[2] == b"versioned content"

                private_policy = {"Version": "2012-10-17", "Statement": []}
                setting(private_policy)
                denied()
                denied(headers={"origin": "https://app.test"})
                denied(path=target + "?versionId=" + version)
                assert request("GET")[0] == 200
                setting(policy())

                def toggle(number):
                    if number % 2:
                        setting(policy())
                    else:
                        assert request("DELETE", bucket + "?policy")[0] == 204

                def read_many(number):
                    for attempt in range(10):
                        result = request("GET", authenticated=False)
                        assert result[0] in (200, 403), result
                        if result[0] == 200:
                            assert result[2] == b"versioned content"

                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    tasks = [executor.submit(toggle, number) for number in range(12)]
                    tasks += [executor.submit(read_many, number) for number in range(4)]
                    for task in tasks:
                        task.result()
                assert request("DELETE", bucket + "?policy")[0] == 204
                for attempt in range(5):
                    denied()
                assert request("DELETE", bucket + "?policy")[0] == 204
                setting(policy(["s3:GetObject", "s3:GetObjectVersion"]))
                stop()
                pending = root / "policy-bucket/.policy.tmp"
                pending.write_bytes(b"incomplete")
                assert "recover" in admin("check", "--root", root, success=False)["error"]
                start()
                assert not pending.exists()
                assert request("GET", authenticated=False)[2] == b"versioned content"
                stop()
                admin("check", "--root", root)
                admin("backup", "--root", root, "--output", base / "backup.tar")
                root = base / "restored"
                admin("restore", "--root", root, "--archive", base / "backup.tar")
                start()
                assert request("GET", authenticated=False)[2] == b"versioned content"
                assert request("GET", target + "?versionId=null", authenticated=False)[2] == b"public content"
                denied(path="/private-bucket/object")
                assert request("DELETE", bucket + "?policy")[0] == 204
                denied()
                stop()
                start()
                denied()
                print("Policy HTTP regression passed: private/public reads, history grants, signature rejection, management isolation, atomic switching, restart and backup/restore.")

                if arguments.sdk:
                    import botocore.config
                    import botocore.session
                    client = botocore.session.get_session().create_client(
                        "s3", endpoint_url=f"http://127.0.0.1:{port}", region_name="us-east-1",
                        aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY,
                        config=botocore.config.Config(signature_version="s3v4", s3={"addressing_style": "path"}, proxies={},
                                                       request_checksum_calculation="when_required", response_checksum_validation="when_required"))
                    unsigned = None
                    try:
                        unsigned = botocore.session.get_session().create_client(
                            "s3", endpoint_url=f"http://127.0.0.1:{port}", region_name="us-east-1",
                            config=botocore.config.Config(signature_version=botocore.UNSIGNED, s3={"addressing_style": "path"}, proxies={}))
                        client.put_bucket_policy(Bucket="policy-bucket", Policy=json.dumps(policy()))
                        assert json.loads(client.get_bucket_policy(Bucket="policy-bucket")["Policy"]) == policy()
                        assert request("GET", authenticated=False)[0] == 200
                        parameters = {"Bucket": "policy-bucket", "Key": "目录/read +%.txt"}
                        with unsigned.get_object(**parameters)["Body"] as body:
                            assert body.read() == b"versioned content"
                        assert unsigned.head_object(**parameters)["ContentLength"] == len(b"versioned content")
                        client.delete_bucket_policy(Bucket="policy-bucket")
                        denied()
                        try:
                            unsigned.head_object(**parameters)
                        except unsigned.exceptions.ClientError as exception:
                            assert exception.response["ResponseMetadata"]["HTTPStatusCode"] == 403
                        else:
                            raise AssertionError("SDK anonymous read survived revocation")
                        try:
                            client.get_bucket_policy(Bucket="policy-bucket")
                        except client.exceptions.ClientError as exception:
                            assert exception.response["Error"]["Code"] == "NoSuchBucketPolicy"
                        else:
                            raise AssertionError("Deleted policy is still present")
                        print("Botocore policy regression passed: put/get/delete, anonymous public reads and revocation.")
                    finally:
                        if unsigned is not None:
                            unsigned.close()
                        client.close()

                setting(policy())
                stop()
                stored = root / "policy-bucket/.policy"
                good = stored.read_bytes()
                stored.write_bytes(b'{"Version":"2012-10-17","Statement":[{"Effect":"Deny"}]}')
                admin("check", "--root", root, success=False)
                try:
                    start()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Corrupt policy allowed startup")
                finally:
                    stop()
                    stored.write_bytes(good)
                start(authenticated=False)
                assert request("GET", "/private-bucket/object", authenticated=False)[2] == b"private"
                assert request("PUT", "/private-bucket/new", b"development mode", authenticated=False)[0] == 200
                print("Policy safety regression passed: corrupt policy refusal and explicit no-credential development mode.")
            except BaseException:
                stop()
                log.seek(0)
                print(log.read().decode(errors="replace"))
                raise
            finally:
                stop()


if __name__ == "__main__":
    main()
