#!/usr/bin/env python3
import argparse
import concurrent.futures
import datetime
import email.utils
import hashlib
import http.client
import os
import pathlib
import socket
import subprocess
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET

from auth_regression import ACCESS_KEY, SECRET_KEY, sign
from presign import presign


PROJECT = pathlib.Path(__file__).resolve().parents[1]
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def main():
    parser = argparse.ArgumentParser(description="Test server-side copying against an authenticated temporary server.")
    parser.add_argument("--sdk", action="store_true", help="also check botocore copy and multipart-source interoperability")
    arguments = parser.parse_args()
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    host = f"127.0.0.1:{port}"
    endpoint = "http://" + host
    source_key = "folder/文件 +%?#.bin"
    source_path = "/source-bucket/" + urllib.parse.quote(source_key, safe="/")
    destination_path = "/destination-bucket/copied"

    def request(method, target, payload=b"", headers=None, authenticated=True):
        outgoing = sign(method, target, host, payload, extra_headers=headers) if authenticated else headers or {}
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            connection.request(method, target, body=payload, headers=outgoing)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def copy(target=destination_path, source=source_path, headers=None, payload=b""):
        return request("PUT", target, payload, {"x-amz-copy-source": source, **(headers or {})})

    def error(result, status, code):
        actual, _, body = result
        assert actual == status and ET.fromstring(body).findtext("Code") == code, (actual, body)

    def check_copy(result, payload):
        status, _, body = result
        assert status == 200, (status, body)
        document = ET.fromstring(body)
        assert document.findtext("s3:ETag", namespaces=NS) == '"' + hashlib.md5(payload).hexdigest() + '"'
        return datetime.datetime.fromisoformat(document.findtext("s3:LastModified", namespaces=NS).replace("Z", "+00:00"))

    with tempfile.TemporaryDirectory(prefix="moon-s3-copy-") as directory:
        environment = dict(os.environ, MOON_S3_ROOT=directory, MOON_S3_LISTEN=host,
                           MOON_S3_ACCESS_KEY=ACCESS_KEY, MOON_S3_SECRET_KEY=SECRET_KEY)
        with tempfile.TemporaryFile() as server_log:
            server = None

            def start():
                process = subprocess.Popen([str(PROJECT / "_build/native/debug/build/cmd/main/main.exe")],
                                           env=environment, stdout=server_log, stderr=server_log)
                try:
                    for attempt in range(200):
                        if process.poll() is not None:
                            raise RuntimeError("server exited before becoming healthy")
                        try:
                            if request("GET", "/healthz", authenticated=False)[0] == 200:
                                return process
                        except OSError:
                            pass
                        time.sleep(0.025)
                    raise RuntimeError("server did not become healthy")
                except BaseException:
                    process.kill()
                    process.wait()
                    raise

            try:
                server = start()
                for bucket in ["source-bucket", "destination-bucket"]:
                    assert request("PUT", "/" + bucket)[0] == 200
                payload = bytes(range(256)) * 1025
                assert request("PUT", source_path, payload, {"Content-Type": "application/source"})[0] == 200
                assert request("PUT", destination_path, b"old destination")[0] == 200
                source_headers = request("HEAD", source_path)[1]
                source_etag = source_headers["ETag"]
                modified = email.utils.parsedate_to_datetime(source_headers["Last-Modified"])
                before = email.utils.format_datetime(modified - datetime.timedelta(seconds=1), usegmt=True)
                after = email.utils.format_datetime(modified + datetime.timedelta(seconds=1), usegmt=True)

                for headers in [
                    {"x-amz-copy-source-if-match": '"missing"'},
                    {"x-amz-copy-source-if-none-match": source_etag},
                    {"x-amz-copy-source-if-unmodified-since": before},
                    {"x-amz-copy-source-if-modified-since": after},
                ]:
                    error(copy(headers=headers), 412, "PreconditionFailed")
                    assert request("GET", destination_path)[2] == b"old destination"
                for headers in [
                    {"x-amz-copy-source-if-match": source_etag, "x-amz-copy-source-if-unmodified-since": before},
                    {"x-amz-copy-source-if-none-match": '"missing"', "x-amz-copy-source-if-modified-since": after},
                    {"x-amz-copy-source-if-unmodified-since": after},
                    {"x-amz-copy-source-if-modified-since": before},
                ]:
                    check_copy(copy(headers=headers), payload)
                copied_time = check_copy(copy(headers={"Content-Type": "application/ignored"}), payload)
                status, headers, body = request("GET", destination_path)
                assert (status, body) == (200, payload)
                assert headers["Content-Type"] == "application/source"
                assert email.utils.parsedate_to_datetime(headers["Last-Modified"]) == copied_time
                assert request("GET", destination_path, headers={"Range": "bytes=65530-65550"})[2] == payload[65530:65551]
                check_copy(copy(headers={"x-amz-metadata-directive": "REPLACE", "content-type": "text/plain"}), payload)
                assert request("HEAD", destination_path)[1]["Content-Type"] == "text/plain"
                check_copy(copy(headers={"x-amz-metadata-directive": "REPLACE"}), payload)
                assert request("HEAD", destination_path)[1]["Content-Type"] == "application/octet-stream"
                error(copy(target=source_path), 400, "InvalidRequest")
                check_copy(copy(target=source_path, headers={"x-amz-metadata-directive": "REPLACE", "content-type": "application/updated"}), payload)
                assert request("HEAD", source_path)[1]["Content-Type"] == "application/updated"

                error(copy(source="/source-bucket/missing"), 404, "NoSuchKey")
                error(copy(source="/absent-bucket/source"), 404, "NoSuchBucket")
                error(copy(target="/absent-bucket/destination"), 404, "NoSuchBucket")
                for source in ["/", "/source-bucket/", source_path + "?versionId=unsupported", "https://example.test/source"]:
                    error(copy(source=source), 400, "InvalidArgument")
                error(copy(headers={"x-amz-metadata-directive": "INVALID"}), 400, "InvalidArgument")
                error(copy(payload=b"unexpected"), 400, "InvalidRequest")
                for option in ["x-amz-copy-source-range", "x-amz-meta-name", "x-amz-tagging", "x-amz-server-side-encryption", "x-amz-acl"]:
                    error(copy(headers={option: "unsupported"}), 501, "NotImplemented")
                error(request("PUT", destination_path, headers={"x-amz-copy-source": source_path}, authenticated=False), 403, "AccessDenied")
                unsigned = sign("PUT", destination_path, host)
                unsigned["x-amz-copy-source"] = source_path
                error(request("PUT", destination_path, headers=unsigned, authenticated=False), 403, "AccessDenied")
                signed = sign("PUT", destination_path, host, extra_headers={"x-amz-copy-source": source_path})
                signed["x-amz-copy-source"] = "/source-bucket/other"
                error(request("PUT", destination_path, headers=signed, authenticated=False), 403, "AccessDenied")
                link = urllib.parse.urlsplit(presign(endpoint, "destination-bucket", "copied", ACCESS_KEY, SECRET_KEY, method="PUT"))
                error(request("PUT", link.path + "?" + link.query, headers={"x-amz-copy-source": source_path}, authenticated=False), 403, "AccessDenied")

                assert request("PUT", "/source-bucket/empty")[0] == 200
                check_copy(copy(source="source-bucket/empty", target="/destination-bucket/empty"), b"")
                assert request("GET", "/destination-bucket/empty")[2] == b""
                sparse = pathlib.Path(directory) / "source-bucket/objects" / (b"too-large".hex() + ".object")
                with sparse.open("wb") as output:
                    output.truncate(5368709121)
                error(copy(source="/source-bucket/too-large"), 400, "EntityTooLarge")
                sparse.unlink()

                blocked = pathlib.Path(directory) / "destination-bucket/objects" / (b"copied".hex() + ".object.pending")
                blocked.mkdir()
                (blocked / "block").write_bytes(b"failure injection")
                error(copy(), 500, "InternalError")
                assert request("GET", destination_path)[2] == payload
                (blocked / "block").unlink()
                blocked.rmdir()
                check_copy(copy(), payload)
                assert request("GET", source_path)[2] == payload

                def concurrent_copy(number):
                    target = "/destination-bucket/concurrent-" + str(number)
                    check_copy(copy(target=target), payload)
                    assert request("GET", target)[2] == payload

                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    list(executor.map(concurrent_copy, range(8)))
                server.kill()
                server.wait(timeout=5)
                server = start()
                assert request("GET", destination_path)[2] == payload
                assert request("HEAD", destination_path)[1]["Content-Type"] == "application/updated"
                print("Copy HTTP regression passed: encoding, metadata, conditions, signatures, disk failure, concurrency and restart.")

                if arguments.sdk:
                    import botocore.config
                    import botocore.session
                    client = botocore.session.get_session().create_client(
                        "s3", endpoint_url=endpoint, region_name="us-east-1",
                        aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY,
                        config=botocore.config.Config(signature_version="s3v4", s3={"addressing_style": "path"}, proxies={},
                                                       request_checksum_calculation="when_required", response_checksum_validation="when_required"),
                    )
                    try:
                        source = {"Bucket": "source-bucket", "Key": source_key}
                        destination = {"Bucket": "destination-bucket", "Key": "sdk/copy"}
                        result = client.copy_object(**destination, CopySource=source)
                        assert result["CopyObjectResult"]["ETag"] == source_etag
                        assert client.head_object(**destination)["ContentType"] == "application/updated"
                        result = client.copy_object(**destination, CopySource=source, MetadataDirective="REPLACE", ContentType="application/sdk")
                        assert client.head_object(**destination)["ContentType"] == "application/sdk"
                        link = urllib.parse.urlsplit(client.generate_presigned_url("copy_object", Params={**destination, "CopySource": source}))
                        check_copy(request("PUT", link.path + "?" + link.query, headers={"x-amz-copy-source": source_path.lstrip("/")}, authenticated=False), payload)
                        multipart = {"Bucket": "source-bucket", "Key": "multipart"}
                        upload = client.create_multipart_upload(**multipart, ContentType="application/multipart")["UploadId"]
                        try:
                            client.upload_part_copy(**multipart, UploadId=upload, PartNumber=1, CopySource=source)
                        except client.exceptions.ClientError as failure:
                            assert failure.response["Error"]["Code"] == "NotImplemented"
                        else:
                            raise AssertionError("UploadPartCopy must not silently create an empty part")
                        assert not client.list_parts(**multipart, UploadId=upload).get("Parts")
                        first = b"a" * (5 * 1024 * 1024)
                        last = b"final part"
                        parts = []
                        for number, chunk in enumerate([first, last], 1):
                            part = client.upload_part(**multipart, UploadId=upload, PartNumber=number, Body=chunk)
                            parts.append({"PartNumber": number, "ETag": part["ETag"]})
                        completed = client.complete_multipart_upload(**multipart, UploadId=upload, MultipartUpload={"Parts": parts})
                        result = client.copy_object(**destination, CopySource=multipart)
                        expected = hashlib.md5(first + last).hexdigest()
                        assert result["CopyObjectResult"]["ETag"] == '"' + expected + '"'
                        assert result["CopyObjectResult"]["ETag"] != completed["ETag"]
                        with client.get_object(**destination)["Body"] as body:
                            assert body.read() == first + last
                        assert client.head_object(**destination)["ContentType"] == "application/multipart"
                        print("Botocore copy passed: encoded sources, metadata replacement, presigned copy and multipart sources.")
                    finally:
                        client.close()
            except BaseException:
                server_log.seek(0)
                print(server_log.read().decode(errors="replace"))
                raise
            finally:
                if server is not None:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()


if __name__ == "__main__":
    main()
