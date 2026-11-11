#!/usr/bin/env python3
import argparse
import concurrent.futures
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


PROJECT = pathlib.Path(__file__).resolve().parents[1]
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def main():
    parser = argparse.ArgumentParser(description="Exercise multipart uploads, recovery and expiration.")
    parser.add_argument("--sdk", action="store_true", help="also verify botocore interoperability")
    arguments = parser.parse_args()
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    host = f"127.0.0.1:{port}"
    bucket = "multipart-bucket"
    key = "文件/large +%&.bin"
    path = "/" + bucket + "/" + urllib.parse.quote(key, safe="/")

    def request(method, target, body=b"", headers=None, authenticated=True):
        outgoing = sign(method, target, host, body) if authenticated else {}
        outgoing.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            connection.request(method, target, body=body, headers=outgoing)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def initiate(target=path):
        status, _, body = request("POST", target + "?uploads", headers={"Content-Type": "application/x-multipart-test"})
        assert status == 200, (status, body)
        upload = ET.fromstring(body).findtext("s3:UploadId", namespaces=NS)
        assert upload and len(upload) == 64
        return upload

    def part(upload, number, payload, target=path):
        status, headers, body = request("PUT", target + f"?uploadId={upload}&partNumber={number}", payload)
        assert status == 200, (status, body)
        etag = headers["ETag"]
        assert etag == '"' + hashlib.md5(payload).hexdigest() + '"'
        return etag

    def complete(upload, parts, target=path):
        document = ET.Element("CompleteMultipartUpload")
        for number, etag in parts:
            entry = ET.SubElement(document, "Part")
            ET.SubElement(entry, "PartNumber").text = str(number)
            ET.SubElement(entry, "ETag").text = etag
        return request("POST", target + "?uploadId=" + upload, ET.tostring(document))

    def error(response, status, code):
        actual, _, body = response
        assert actual == status, (actual, body)
        assert ET.fromstring(body).findtext("Code") == code, body

    def list_parts(upload, query="", target=path):
        status, _, body = request("GET", target + "?uploadId=" + upload + query)
        assert status == 200, (status, body)
        return ET.fromstring(body)

    def list_uploads(query=""):
        status, _, body = request("GET", "/" + bucket + "?uploads" + query)
        assert status == 200, (status, body)
        return ET.fromstring(body)

    with tempfile.TemporaryDirectory(prefix="moon-s3-multipart-") as directory:
        environment = dict(os.environ, MOON_S3_ROOT=directory, MOON_S3_LISTEN=host,
                           MOON_S3_ACCESS_KEY=ACCESS_KEY, MOON_S3_SECRET_KEY=SECRET_KEY,
                           MOON_S3_MULTIPART_TTL_SECONDS="604800")
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
                        time.sleep(0.05)
                    raise RuntimeError("server did not become healthy")
                except BaseException:
                    process.kill()
                    process.wait()
                    raise

            try:
                server = start()
                assert request("PUT", "/" + bucket)[0] == 200
                assert request("PUT", "/pending-only")[0] == 200
                pending_only = initiate("/pending-only/object")
                error(request("DELETE", "/pending-only"), 409, "BucketNotEmpty")
                assert request("DELETE", "/pending-only/object?uploadId=" + pending_only)[0] == 204
                assert request("DELETE", "/pending-only")[0] == 204
                error(request("POST", path + "?uploads", authenticated=False), 403, "AccessDenied")
                error(request("POST", "/absent-bucket/object?uploads"), 404, "NoSuchBucket")
                assert request("PUT", path, b"original")[0] == 200
                upload = initiate()
                error(request("DELETE", "/" + bucket), 409, "BucketNotEmpty")
                assert request("GET", path)[2] == b"original"
                error(request("GET", path + "?uploadId=" + "0" * 64), 404, "NoSuchUpload")
                error(request("GET", path + "?uploadId=..%2F..%2Fobjects"), 404, "NoSuchUpload")
                for number in ["0", "10001", "-1", "2147483648", "abc"]:
                    error(request("PUT", path + f"?uploadId={upload}&partNumber={number}", b"bad"), 400, "InvalidArgument")
                error(request("PUT", path + f"?uploadId={upload}&partNumber=1&partNumber=2", b"bad"), 400, "InvalidArgument")
                small_etag = part(upload, 1, b"small")
                last_etag = part(upload, 2, b"last")
                error(complete(upload, [(1, small_etag), (2, last_etag)]), 400, "EntityTooSmall")
                error(complete(upload, [(2, last_etag), (1, small_etag)]), 400, "InvalidPartOrder")
                error(complete(upload, [(1, small_etag), (1, small_etag)]), 400, "InvalidPartOrder")
                error(complete(upload, [(3, last_etag)]), 400, "InvalidPart")
                error(complete(upload, [(1, '"' + "0" * 32 + '"')]), 400, "InvalidPart")
                for document in [b"", b"<CompleteMultipartUpload/>", b"<other/>", b"<!DOCTYPE x><CompleteMultipartUpload/>"]:
                    error(request("POST", path + "?uploadId=" + upload, document), 400, "MalformedXML")
                assert request("GET", path)[2] == b"original"
                error(request("GET", "/" + bucket + "/other?uploadId=" + upload), 404, "NoSuchUpload")
                error(request("DELETE", "/" + bucket + "/other?uploadId=" + upload), 404, "NoSuchUpload")
                error(request("GET", path + f"?uploadId={upload}&max-parts=-1"), 400, "InvalidArgument")

                first = bytes(range(256)) * (5 * 1024 * 1024 // 256)
                first_etag = part(upload, 1, first)
                page = list_parts(upload, "&max-parts=1")
                assert page.findtext("s3:IsTruncated", namespaces=NS) == "true"
                assert page.findtext("s3:NextPartNumberMarker", namespaces=NS) == "1"
                assert page.findtext("s3:Part/s3:Size", namespaces=NS) == str(len(first))
                page = list_parts(upload, "&max-parts=1&part-number-marker=1")
                assert page.findtext("s3:Part/s3:PartNumber", namespaces=NS) == "2"
                assert page.findtext("s3:IsTruncated", namespaces=NS) == "false"
                assert list_parts(upload, "&max-parts=0").find("s3:Part", NS) is None
                assert upload in [item.text for item in list_uploads().findall("s3:Upload/s3:UploadId", NS)]
                upload_directory = pathlib.Path(directory) / bucket / ".uploads" / upload
                (upload_directory / "part.2.tmp").write_bytes(b"interrupted write")
                (upload_directory / "assembled").write_bytes(b"interrupted merge")
                server.kill()
                server.wait(timeout=5)
                server = start()
                assert len(list_parts(upload).findall("s3:Part", NS)) == 2
                assert request("GET", path)[2] == b"original"
                last = b"resumed final part"
                last_etag = part(upload, 2, last)
                status, _, body = complete(upload, [(1, first_etag), (2, last_etag)])
                assert status == 200, (status, body)
                completed_etag = ET.fromstring(body).findtext("s3:ETag", namespaces=NS)
                expected_etag = '"' + hashlib.md5(hashlib.md5(first).digest() + hashlib.md5(last).digest()).hexdigest() + '-2"'
                assert completed_etag == expected_etag
                status, metadata, body = request("GET", path)
                assert status == 200 and body == first + last
                assert metadata["ETag"] == expected_etag
                assert metadata["Content-Type"] == "application/x-multipart-test"
                assert request("GET", path, headers={"Range": f"bytes={len(first)-3}-{len(first)+4}"})[2] == first[-3:] + last[:5]
                assert not upload_directory.exists()
                error(request("GET", path + "?uploadId=" + upload), 404, "NoSuchUpload")
                server.kill()
                server.wait(timeout=5)
                server = start()
                assert request("HEAD", path)[1]["ETag"] == expected_etag
                assert request("GET", path)[2] == first + last

                aborted = initiate()
                part(aborted, 1, b"discarded")
                assert request("DELETE", path + "?uploadId=" + aborted)[0] == 204
                assert not (pathlib.Path(directory) / bucket / ".uploads" / aborted).exists()
                error(request("GET", path + "?uploadId=" + aborted), 404, "NoSuchUpload")
                assert request("GET", path)[2] == first + last
                empty = initiate("/" + bucket + "/empty")
                empty_etag = part(empty, 1, b"", "/" + bucket + "/empty")
                assert complete(empty, [(1, empty_etag)], "/" + bucket + "/empty")[0] == 200
                assert request("GET", "/" + bucket + "/empty")[2] == b""

                pending = [initiate(), initiate(), initiate("/" + bucket + "/z")]
                found = []
                markers = "&max-uploads=1"
                for attempt in range(10):
                    page = list_uploads(markers)
                    found += [item.text for item in page.findall("s3:Upload/s3:UploadId", NS)]
                    if page.findtext("s3:IsTruncated", namespaces=NS) == "false":
                        break
                    markers = "&max-uploads=1&key-marker=" + urllib.parse.quote(page.findtext("s3:NextKeyMarker", namespaces=NS), safe="")
                    markers += "&upload-id-marker=" + page.findtext("s3:NextUploadIdMarker", namespaces=NS)
                assert sorted(found) == sorted(pending)
                assert len(list_uploads("&prefix=z").findall("s3:Upload", NS)) == 1
                for target, pending_id in [(path, pending[0]), (path, pending[1]), ("/" + bucket + "/z", pending[2])]:
                    assert request("DELETE", target + "?uploadId=" + pending_id)[0] == 204

                concurrent_upload = initiate()
                def concurrent_part(number):
                    return part(concurrent_upload, number, str(number).encode())
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    assert len(list(executor.map(concurrent_part, [1, 2, 3, 4]))) == 4
                assert len(list_parts(concurrent_upload).findall("s3:Part", NS)) == 4
                assert request("DELETE", path + "?uploadId=" + concurrent_upload)[0] == 204

                expired = initiate()
                part(expired, 1, b"expired")
                expired_directory = pathlib.Path(directory) / bucket / ".uploads" / expired
                manifest = expired_directory / "manifest"
                manifest.write_text("\n".join("initiated=1" if line.startswith("initiated=") else line for line in manifest.read_text().split("\n")))
                error(request("GET", path + "?uploadId=" + expired), 404, "NoSuchUpload")
                assert not expired_directory.exists()
                expired = initiate()
                expired_directory = pathlib.Path(directory) / bucket / ".uploads" / expired
                manifest = expired_directory / "manifest"
                manifest.write_text("\n".join("initiated=1" if line.startswith("initiated=") else line for line in manifest.read_text().split("\n")))
                orphan_directory = pathlib.Path(directory) / bucket / ".uploads" / ("f" * 64)
                orphan_directory.mkdir()
                (orphan_directory / "manifest").write_bytes(b"interrupted manifest")
                os.utime(orphan_directory, (1, 1))
                server.kill()
                server.wait(timeout=5)
                server = start()
                assert not expired_directory.exists()
                assert not orphan_directory.exists()
                assert not list_uploads().findall("s3:Upload", NS)
                assert request("GET", path)[2] == first + last
                print("Multipart HTTP regression passed: lifecycle, recovery, ETags, paging, concurrency, abort and expiry.")

                if arguments.sdk:
                    import botocore.config
                    import botocore.session
                    client = botocore.session.get_session().create_client(
                        "s3", endpoint_url="http://" + host, region_name="us-east-1",
                        aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY,
                        config=botocore.config.Config(signature_version="s3v4", s3={"addressing_style": "path"}, proxies={},
                                                       request_checksum_calculation="when_required", response_checksum_validation="when_required"),
                    )
                    try:
                        sdk_key = "sdk/100MiB.bin"
                        parameters = {"Bucket": bucket, "Key": sdk_key}
                        sdk_upload = client.create_multipart_upload(**parameters, ContentType="application/octet-stream")["UploadId"]
                        chunk = bytes(range(256)) * (10 * 1024 * 1024 // 256)
                        parts = []
                        expected_hash = hashlib.sha256()
                        for number in range(1, 11):
                            etag = client.upload_part(**parameters, UploadId=sdk_upload, PartNumber=number, Body=chunk)["ETag"]
                            parts.append({"PartNumber": number, "ETag": etag})
                            expected_hash.update(chunk)
                        assert len(client.list_parts(**parameters, UploadId=sdk_upload)["Parts"]) == 10
                        assert sdk_upload in [item["UploadId"] for item in client.list_multipart_uploads(Bucket=bucket).get("Uploads", [])]
                        result = client.complete_multipart_upload(**parameters, UploadId=sdk_upload, MultipartUpload={"Parts": parts})
                        assert result["ETag"].endswith('-10"')
                        assert client.head_object(**parameters)["ContentLength"] == 100 * 1024 * 1024
                        downloaded = hashlib.sha256()
                        with client.get_object(**parameters)["Body"] as body:
                            while block := body.read(65536):
                                downloaded.update(block)
                        assert downloaded.digest() == expected_hash.digest()
                        with client.get_object(**parameters, Range="bytes=10485750-10485780")["Body"] as body:
                            assert body.read() == chunk[-10:] + chunk[:21]
                        sdk_abort = client.create_multipart_upload(**parameters)["UploadId"]
                        client.abort_multipart_upload(**parameters, UploadId=sdk_abort)
                        assert not client.list_multipart_uploads(Bucket=bucket).get("Uploads")
                        client.delete_object(**parameters)
                        print("Botocore multipart passed: lifecycle and 100 MiB SHA-256 round trip with cross-part Range.")
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
