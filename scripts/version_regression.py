#!/usr/bin/env python3
import argparse
import concurrent.futures
import hashlib
import http.client
import json
import os
import pathlib
import subprocess
import socket
import sys
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET

from auth_regression import ACCESS_KEY, SECRET_KEY, sign


PROJECT = pathlib.Path(__file__).resolve().parents[1]
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def main():
    parser = argparse.ArgumentParser(description="Exercise bucket versioning, history, markers, recovery and backups.")
    parser.add_argument("--sdk", action="store_true")
    arguments = parser.parse_args()
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with tempfile.TemporaryDirectory(prefix="moon-s3-version-") as directory:
        base = pathlib.Path(directory)
        root = base / "data"
        server = None
        port = None
        bucket = "/version-bucket"
        key = "目录/object +%.txt"
        target = bucket + "/" + urllib.parse.quote(key, safe="/")

        def request(verb, path=target, body=b"", headers=None, authenticated=True):
            outgoing = sign(verb, path, f"127.0.0.1:{port}", body, extra_headers=headers) if authenticated else headers or {}
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                connection.request(verb, path, body=body, headers=outgoing)
                response = connection.getresponse()
                return response.status, {name.lower(): value for name, value in response.getheaders()}, response.read()
            finally:
                connection.close()

        def setting(status):
            body = f'<VersioningConfiguration xmlns="{NS["s3"]}"><Status>{status}</Status></VersioningConfiguration>'.encode()
            result = request("PUT", bucket + "?versioning", body)
            assert result[0] == 200, result

        def put(payload, path=target):
            result = request("PUT", path, payload, {"content-type": "application/version"})
            assert result[0] == 200, result
            return result[1].get("x-amz-version-id")

        def listing(extra=""):
            result = request("GET", bucket + "?versions" + extra)
            assert result[0] == 200, result
            document = ET.fromstring(result[2])
            entries = [item for item in document if item.tag in ("{" + NS["s3"] + "}Version", "{" + NS["s3"] + "}DeleteMarker")]
            return document, entries

        def ids(extra=""):
            return [item.findtext("s3:VersionId", namespaces=NS) for item in listing(extra)[1]]

        def error(result, status, code):
            assert result[0] == status and ET.fromstring(result[2]).findtext("Code") == code, result

        def stop():
            nonlocal server
            if server is not None:
                server.kill()
                server.wait(timeout=5)
                server = None

        def admin(*args):
            result = subprocess.run([sys.executable, str(PROJECT / "scripts/admin.py"), *map(str, args)], capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, (result.stdout, result.stderr)
            return json.loads(result.stdout)

        with tempfile.TemporaryFile() as log:
            def start():
                nonlocal server, port
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                environment = dict(os.environ, MOON_S3_ROOT=str(root), MOON_S3_LISTEN=f"127.0.0.1:{port}",
                                   MOON_S3_ACCESS_KEY=ACCESS_KEY, MOON_S3_SECRET_KEY=SECRET_KEY)
                server = subprocess.Popen([str(PROJECT / "_build/native/debug/build/cmd/main/main.exe")], env=environment, stdout=log, stderr=log)
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
                assert b"Status" not in request("GET", bucket + "?versioning")[2]
                assert put(b"legacy") is None
                assert ids() == ["null"]
                setting("Enabled")
                assert request("GET")[1]["x-amz-version-id"] == "null"
                first = put(b"first version")
                second = put(b"second version")
                assert first != second and len(first) == len(second) == 64
                assert ids() == [second, first, "null"]
                for version, expected in [(first, b"first version"), (second, b"second version"), ("null", b"legacy")]:
                    result = request("GET", target + "?versionId=" + version)
                    assert result[0] == 200 and result[2] == expected and result[1]["x-amz-version-id"] == version
                    assert request("HEAD", target + "?versionId=" + version)[1]["last-modified"] == result[1]["last-modified"]
                    result = request("GET", target + "?versionId=" + version, headers={"range": "bytes=0-2"})
                    assert result[0] == 206 and result[2] == expected[:3]
                restored_copy = request("PUT", bucket + "/restored-copy", headers={"x-amz-copy-source": target + "?versionId=" + first})
                assert restored_copy[0] == 200 and restored_copy[1]["x-amz-copy-source-version-id"] == first
                assert request("GET", bucket + "/restored-copy")[2] == b"first version"
                result = request("DELETE")
                marker = result[1]["x-amz-version-id"]
                assert result[0] == 204 and result[1]["x-amz-delete-marker"] == "true"
                error(request("GET"), 404, "NoSuchKey")
                assert request("GET")[1]["x-amz-delete-marker"] == "true"
                result = request("GET", target + "?versionId=" + marker)
                error(result, 405, "MethodNotAllowed")
                assert result[1]["x-amz-delete-marker"] == "true" and "last-modified" in result[1]
                assert request("HEAD", target + "?versionId=" + marker)[0] == 405
                error(request("PUT", bucket + "/marker-copy", headers={"x-amz-copy-source": target + "?versionId=" + marker}), 400, "InvalidRequest")
                ordinary = ET.fromstring(request("GET", bucket + "?list-type=2")[2])
                assert key not in [item.text for item in ordinary.findall("s3:Contents/s3:Key", NS)]
                error(request("DELETE", bucket), 409, "BucketNotEmpty")
                assert request("DELETE", target + "?versionId=" + marker)[0] == 204
                assert request("GET")[2] == b"second version"
                assert request("DELETE", target + "?versionId=" + first)[0] == 204
                assert request("DELETE", target + "?versionId=" + second)[0] == 204
                assert request("GET")[2] == b"legacy"
                assert request("DELETE", target + "?versionId=null")[0] == 204
                assert request("GET")[0] == 404

                ancestor = put(b"retained enabled version")
                setting("Suspended")
                assert put(b"null one") == "null"
                assert put(b"null two") == "null"
                filtered = "&prefix=" + urllib.parse.quote(key, safe="")
                assert ids(filtered) == ["null", ancestor]
                assert request("GET", target + "?versionId=null")[2] == b"null two"
                assert request("DELETE")[1]["x-amz-version-id"] == "null"
                assert request("GET")[0] == 404
                assert request("DELETE")[1]["x-amz-version-id"] == "null"
                assert ids(filtered) == ["null", ancestor]
                assert request("DELETE", target + "?versionId=null")[1]["x-amz-delete-marker"] == "true"
                assert request("GET")[2] == b"retained enabled version"
                setting("Enabled")
                newest = put(b"newest after resume")
                assert newest != "null"
                error(request("GET", target + "?versionId=" + "0" * 64), 404, "NoSuchVersion")
                assert request("DELETE", target + "?versionId=" + "0" * 64)[0] == 204
                for path in [target + "?versionId=../outside", target + "?versionId=null&versionId=null", target + "?versionId=null&uploadId=x"]:
                    assert request("GET", path)[0] == 400
                error(request("PUT", bucket + "?versioning", b"<wrong/>"), 400, "MalformedXML")
                error(request("PUT", bucket + "?versioning", b"<VersioningConfiguration><Status>Disabled</Status></VersioningConfiguration>"), 400, "InvalidRequest")
                error(request("GET", bucket + "?versioning", authenticated=False), 403, "AccessDenied")

                def concurrent_put(number):
                    return put(f"parallel {number}".encode(), bucket + "/parallel")

                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    parallel = list(executor.map(concurrent_put, range(8)))
                assert len(set(parallel)) == 8
                page, entries = listing("&prefix=parallel")
                assert len(entries) == 8 and sum(item.findtext("s3:IsLatest", namespaces=NS) == "true" for item in entries) == 1
                found = []
                markers = "&max-keys=2&encoding-type=url"
                for attempt in range(30):
                    page, entries = listing(markers)
                    found += [(urllib.parse.unquote(item.findtext("s3:Key", namespaces=NS)), item.findtext("s3:VersionId", namespaces=NS)) for item in entries]
                    if page.findtext("s3:IsTruncated", namespaces=NS) == "false":
                        break
                    next_key = urllib.parse.unquote(page.findtext("s3:NextKeyMarker", namespaces=NS))
                    markers = "&max-keys=2&encoding-type=url&key-marker=" + urllib.parse.quote(next_key, safe="")
                    next_version = page.findtext("s3:NextVersionIdMarker", namespaces=NS)
                    if next_version:
                        markers += "&version-id-marker=" + next_version
                assert len(found) == len(set(found)) == len(listing()[1])
                assert listing("&max-keys=0")[1] == []
                assert listing("&delimiter=/")[0].find("s3:CommonPrefixes", NS) is not None
                assert put(b"deleted before backup", bucket + "/backup-marker")
                backup_marker = request("DELETE", bucket + "/backup-marker")[1]["x-amz-version-id"]
                before = request("GET", target + "?versionId=" + ancestor)
                stop()
                start()
                assert request("GET", target + "?versionId=" + ancestor)[2] == before[2]
                assert request("HEAD", target + "?versionId=" + ancestor)[1]["last-modified"] == before[1]["last-modified"]
                assert ids(filtered) == [newest, ancestor]
                stop()
                report = admin("check", "--root", root)
                assert report["versions"] >= 11
                admin("backup", "--root", root, "--output", base / "backup.tar")
                root = base / "restored"
                admin("restore", "--archive", base / "backup.tar", "--root", root)
                start()
                assert ids(filtered) == [newest, ancestor]
                assert request("GET", target + "?versionId=" + ancestor)[2] == before[2]
                assert request("GET", bucket + "?versioning")[2].find(b"Enabled") >= 0
                assert request("GET", bucket + "/backup-marker")[1]["x-amz-delete-marker"] == "true"
                assert request("HEAD", bucket + "/backup-marker?versionId=" + backup_marker)[0] == 405
                print("Version HTTP regression passed: enable/suspend, null versions, historical reads, markers, copying, paging, concurrency, restart and backups.")

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
                        client.put_bucket_versioning(Bucket="version-bucket", VersioningConfiguration={"Status": "Enabled"})
                        assert client.get_bucket_versioning(Bucket="version-bucket")["Status"] == "Enabled"
                        parameters = {"Bucket": "version-bucket", "Key": "sdk"}
                        first_sdk = client.put_object(**parameters, Body=b"sdk old")["VersionId"]
                        second_sdk = client.put_object(**parameters, Body=b"sdk new")["VersionId"]
                        current_copy = client.copy_object(Bucket="version-bucket", Key="current-copy", CopySource=parameters)
                        assert current_copy["CopySourceVersionId"] == second_sdk
                        with client.get_object(**parameters, VersionId=first_sdk)["Body"] as body:
                            assert body.read() == b"sdk old"
                        assert client.head_object(**parameters, VersionId=second_sdk)["VersionId"] == second_sdk
                        link = client.generate_presigned_url("get_object", Params={**parameters, "VersionId": first_sdk}, ExpiresIn=60)
                        parsed = urllib.parse.urlsplit(link)
                        assert request("GET", parsed.path + "?" + parsed.query, authenticated=False)[2] == b"sdk old"
                        marker = client.delete_object(**parameters)
                        assert marker["DeleteMarker"]
                        listing_sdk = client.list_object_versions(Bucket="version-bucket", Prefix="sdk")
                        assert len(listing_sdk["Versions"]) == 2 and len(listing_sdk["DeleteMarkers"]) == 1
                        client.delete_object(**parameters, VersionId=marker["VersionId"])
                        copied = client.copy_object(**parameters, CopySource={**parameters, "VersionId": first_sdk})
                        assert copied["VersionId"] not in [first_sdk, second_sdk]
                        upload = client.create_multipart_upload(**parameters)["UploadId"]
                        part = client.upload_part(**parameters, UploadId=upload, PartNumber=1, Body=b"multipart")
                        complete = client.complete_multipart_upload(**parameters, UploadId=upload, MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": part["ETag"]}]})
                        assert complete["VersionId"]
                        with client.get_object(**parameters, VersionId=complete["VersionId"])["Body"] as body:
                            assert body.read() == b"multipart"
                        all_sdk = []
                        for page in client.get_paginator("list_object_versions").paginate(Bucket="version-bucket", Prefix="sdk", PaginationConfig={"PageSize": 1}):
                            all_sdk += page.get("Versions", [])
                        assert len(all_sdk) == 4
                        transition = {"Bucket": "version-bucket", "Key": "transition"}
                        upload = client.create_multipart_upload(**transition)["UploadId"]
                        part = client.upload_part(**transition, UploadId=upload, PartNumber=1, Body=b"completed while suspended")
                        client.put_bucket_versioning(Bucket="version-bucket", VersioningConfiguration={"Status": "Suspended"})
                        complete = client.complete_multipart_upload(**transition, UploadId=upload, MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": part["ETag"]}]})
                        assert complete["VersionId"] == "null"
                        client.put_bucket_versioning(Bucket="version-bucket", VersioningConfiguration={"Status": "Enabled"})
                        print("Botocore versioning passed: configuration, historical GET/HEAD, markers, pagination, version restore and multipart versions.")
                    finally:
                        client.close()

                assert request("PUT", "/recovery-bucket")[0] == 200
                assert put(b"legacy import interrupted", "/recovery-bucket/baseline") is None
                configuration = b"<?xml version='1.0' encoding='UTF-8'?><VersioningConfiguration xmlns='http://s3.amazonaws.com/doc/2006-03-01/'><Status> Enabled </Status></VersioningConfiguration>"
                assert request("PUT", "/recovery-bucket?versioning", configuration)[0] == 200
                stop()
                recovery = root / "recovery-bucket"
                baseline_encoded = b"baseline".hex()
                baseline_current = recovery / "objects" / (baseline_encoded + ".object")
                baseline_history = recovery / ".versions" / baseline_encoded / ("b" * 64)
                baseline_history.mkdir(parents=True)
                (baseline_history / "content").write_bytes(baseline_current.read_bytes())
                (baseline_history / "record").write_text(f"version-id=null\norder=0\ndelete-marker=false\netag={hashlib.md5(baseline_current.read_bytes()).hexdigest()}\ncontent-type=application/version\nmodified={baseline_current.stat().st_mtime_ns // 1_000_000_000}\n")
                admin("check", "--root", root)
                encoded = b"recover".hex()
                history = recovery / ".versions" / encoded
                history.mkdir(parents=True)
                current = recovery / "objects" / (encoded + ".object")
                journal = current.with_suffix(".object.txn")
                stage = history / ".pending"

                def stage_version(storage, payload, order, logical=None):
                    stage.mkdir()
                    (stage / "content").write_bytes(payload)
                    record = f"version-id={logical or storage}\norder={order}\ndelete-marker=false\netag={hashlib.md5(payload).hexdigest()}\ncontent-type=application/recovery\nmodified=1700000000\n"
                    (stage / "record").write_text(record)

                storage = "1" * 64
                upload = "a" * 64
                upload_path = recovery / ".uploads" / upload
                upload_path.mkdir(parents=True)
                (upload_path / "manifest").write_text(f"key={encoded}\ncontent-type=application/recovery\ninitiated={int(time.time())}\n")
                stage_version(storage, b"journal staged version", 1)
                journal.write_text(f"version\n{storage}\n{upload}\n")
                start()
                result = request("GET", "/recovery-bucket/recover")
                assert result[2] == b"journal staged version" and result[1]["x-amz-version-id"] == storage
                assert request("GET", "/recovery-bucket/baseline?versionId=null")[2] == b"legacy import interrupted"
                assert not upload_path.exists() and not journal.exists()
                stop()
                storage_two = "2" * 64
                stage_version(storage_two, b"already published", 2)
                stage.rename(history / storage_two)
                current.write_bytes(b"interrupted current cache")
                journal.write_text(f"version\n{storage_two}\n-\n")
                start()
                assert request("GET", "/recovery-bucket/recover")[2] == b"already published"
                assert current.read_bytes() == b"already published"
                stop()
                for entry in (history / storage_two).iterdir():
                    entry.unlink()
                (history / storage_two).rmdir()
                journal.write_text(f"remove-version\n{storage_two}\n")
                start()
                assert request("GET", "/recovery-bucket/recover")[2] == b"journal staged version"
                stop()
                stage.mkdir()
                (stage / "incomplete").write_bytes(b"uncommitted")
                (recovery / ".versioning.tmp").write_text("Suspended")
                start()
                assert not stage.exists() and not (recovery / ".versioning.tmp").exists()
                assert request("GET", "/recovery-bucket/recover")[2] == b"journal staged version"
                stop()
                stage_version("3" * 64, b"old null", 2, logical="null")
                stage.rename(history / ("3" * 64))
                stage_version("4" * 64, b"new null", 3, logical="null")
                journal.write_text("version\n" + "4" * 64 + "\n-\n")
                (recovery / ".versioning").write_text("Suspended")
                start()
                assert request("GET", "/recovery-bucket/recover?versionId=null")[2] == b"new null"
                assert not (history / ("3" * 64)).exists()
                for version in ["null", storage]:
                    assert request("DELETE", "/recovery-bucket/recover?versionId=" + version)[0] == 204
                assert request("DELETE", "/recovery-bucket/baseline?versionId=null")[0] == 204
                assert request("DELETE", "/recovery-bucket")[0] == 204
                assert put(b"", bucket + "/empty")
                assert request("GET", bucket + "/empty")[2] == b""
                stop()
                admin("check", "--root", root)
                bad_record = next((root / "version-bucket" / ".versions").glob("*/*/record"))
                valid_record = bad_record.read_text()
                bad_record.write_text(valid_record.replace("order=", "order=999999999999999999999"))
                try:
                    start()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Corrupt version record did not prevent startup")
                finally:
                    stop()
                    bad_record.write_text(valid_record)
                print("Version recovery passed: staged/published journals, upload cleanup, repeated deletion, orphan cleanup, null replacement, empty content and corrupt-record refusal.")
            except BaseException:
                log.seek(0)
                print(log.read().decode(errors="replace"))
                raise
            finally:
                stop()


if __name__ == "__main__":
    main()
