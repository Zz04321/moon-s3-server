#!/usr/bin/env python3
import hashlib
import http.client
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import unittest.mock
import xml.etree.ElementTree as ET

import admin as storage_admin


PROJECT = pathlib.Path(__file__).resolve().parents[1]
ADMIN = PROJECT / "scripts/admin.py"
SERVER = PROJECT / "_build/native/debug/build/cmd/main/main.exe"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def main():
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with tempfile.TemporaryDirectory(prefix="moon-s3-admin-test-") as directory:
        base = pathlib.Path(directory)
        root = base / "data"
        archive = base / "backup.tar"
        restored = base / "restored"
        server = None
        port = None
        key = "目录/a +%?#.bin"
        target = "/backup-bucket/" + urllib.parse.quote(key, safe="/")

        def admin(*arguments, success=True):
            result = subprocess.run([sys.executable, str(ADMIN), *map(str, arguments)],
                                    capture_output=True, text=True, timeout=60)
            assert (result.returncode == 0) == success, (arguments, result.stdout, result.stderr)
            report = json.loads(result.stdout if success else result.stderr)
            assert report["ok"] == success
            return report

        def request(method, path, body=b"", headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        def stop():
            nonlocal server
            if server is not None:
                server.kill()
                server.wait(timeout=5)
                server = None

        with tempfile.TemporaryFile() as log:
            def start(data_root):
                nonlocal server, port
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                environment = dict(os.environ, MOON_S3_ROOT=str(data_root), MOON_S3_LISTEN=f"127.0.0.1:{port}",
                                   MOON_S3_MULTIPART_TTL_SECONDS="604800")
                environment.pop("MOON_S3_ACCESS_KEY", None)
                environment.pop("MOON_S3_SECRET_KEY", None)
                server = subprocess.Popen([str(SERVER)], env=environment, stdout=log, stderr=log)
                for attempt in range(200):
                    if server.poll() is not None:
                        raise RuntimeError("server exited before becoming healthy")
                    try:
                        if request("GET", "/healthz")[0] == 200:
                            return
                    except OSError:
                        pass
                    time.sleep(0.025)
                raise RuntimeError("server did not become healthy")

            def initiate(path):
                status, _, body = request("POST", path + "?uploads", headers={"Content-Type": "application/multipart"})
                assert status == 200
                return ET.fromstring(body).findtext("s3:UploadId", namespaces=NS)

            def complete(path, upload, parts):
                document = ET.Element("CompleteMultipartUpload")
                for number, etag in parts:
                    part = ET.SubElement(document, "Part")
                    ET.SubElement(part, "PartNumber").text = str(number)
                    ET.SubElement(part, "ETag").text = etag
                status, _, body = request("POST", path + "?uploadId=" + upload, ET.tostring(document))
                assert status == 200, (status, body)
                return ET.fromstring(body).findtext("s3:ETag", namespaces=NS)

            try:
                start(root)
                assert request("PUT", "/backup-bucket")[0] == 200
                assert request("PUT", "/empty-bucket")[0] == 200
                payload = bytes(range(256)) * 65536
                assert request("PUT", target, payload, {"Content-Type": "application/test"})[0] == 200
                assert request("PUT", "/backup-bucket/empty")[0] == 200
                original_headers = request("HEAD", target)[1]
                bucket_list = request("GET", "/")[2]
                upload = initiate("/backup-bucket/resume")
                first = b"first" * (1024 * 1024 + 1)
                status, headers, _ = request("PUT", f"/backup-bucket/resume?uploadId={upload}&partNumber=1", first)
                assert status == 200
                first_etag = headers["ETag"]
                completed = initiate("/backup-bucket/completed")
                status, headers, _ = request("PUT", f"/backup-bucket/completed?uploadId={completed}&partNumber=1", b"multipart contents")
                assert status == 200
                completed_etag = complete("/backup-bucket/completed", completed, [(1, headers["ETag"])])
                for command in ["check", "backup"]:
                    arguments = [command, "--root", root]
                    if command == "backup":
                        arguments += ["--output", archive]
                    failure = admin(*arguments, success=False)
                    assert "in use" in failure["error"]
                assert not archive.exists()
                stop()

                objects = root / "backup-bucket/objects"
                original = objects / (key.encode().hex() + ".object")
                report = admin("check", "--root", root)
                assert (report["buckets"], report["objects"], report["uploads"]) == (2, 3, 1)
                assert len(report["warnings"]) == 1
                manifest_before = (root / "backup-bucket/.uploads" / upload / "manifest").read_bytes()
                for transient in [".object.pending", ".object.txn", ".meta.tmp"]:
                    path = objects / ("invalid".encode().hex() + transient)
                    path.write_bytes(b"incomplete transaction")
                    assert "recover" in admin("backup", "--root", root, "--output", archive, success=False)["error"]
                    assert not archive.exists()
                    path.unlink()
                linked = root / "linked-bucket"
                linked.symlink_to(root / "backup-bucket", target_is_directory=True)
                assert "Symlinks" in admin("check", "--root", root, success=False)["error"]
                linked.unlink()
                linked_file = objects / (b"hard-link".hex() + ".object")
                os.link(original, linked_file)
                assert "Hard links" in admin("check", "--root", root, success=False)["error"]
                linked_file.unlink()
                metadata = objects / (key.encode().hex() + ".meta")
                saved_metadata = metadata.read_bytes()
                metadata.unlink()
                admin("check", "--root", root, success=False)
                metadata.write_bytes(saved_metadata)
                with original.open("r+b") as changed:
                    changed.write(b"!")
                assert "MD5 mismatch" in admin("check", "--root", root, success=False)["error"]
                with original.open("r+b") as changed:
                    changed.write(payload[:1])
                original_time = original.stat().st_mtime_ns
                legacy_bucket = root / "legacy-bucket"
                (legacy_bucket / "objects").mkdir(parents=True)
                legacy_time = 946684800123456789
                os.utime(legacy_bucket, ns=(legacy_time, legacy_time))
                temporary_part = root / "backup-bucket/.uploads" / upload / "part.2.tmp"
                temporary_part.write_bytes(b"interrupted part")
                admin("backup", "--root", root, "--output", root / "inside.tar", success=False)
                failed_archive = base / "failed.tar"
                with unittest.mock.patch.object(storage_admin.DigestReader, "read", side_effect=OSError("simulated disk failure")):
                    try:
                        storage_admin.backup(root, failed_archive)
                    except OSError:
                        pass
                    else:
                        raise AssertionError("Backup failure must propagate")
                assert not failed_archive.exists()
                assert not list(base.glob(".moon-s3-backup-*"))
                admin("backup", "--root", root, "--output", archive)
                assert archive.stat().st_mode & 0o777 == 0o600
                archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
                admin("backup", "--root", root, "--output", archive, success=False)
                assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive_hash

                admin("restore", "--archive", archive, "--root", root, success=False)
                empty_destination = base / "already-empty"
                empty_destination.mkdir()
                admin("restore", "--archive", archive, "--root", empty_destination, success=False)
                assert list(empty_destination.iterdir()) == []
                symlink_destination = base / "already-link"
                symlink_destination.symlink_to(base / "absent")
                admin("restore", "--archive", archive, "--root", symlink_destination, success=False)
                admin("restore", "--archive", archive, "--root", restored, "--max-bytes", "1", success=False)
                assert not restored.exists()
                admin("restore", "--archive", archive, "--root", restored)
                assert not (restored / ".lock").exists()
                assert (restored / "backup-bucket/objects" / original.name).stat().st_mtime_ns == original_time
                assert (restored / "backup-bucket/.uploads" / upload / "manifest").read_bytes() == manifest_before
                assert (restored / "backup-bucket/.uploads" / upload / "part.2.tmp").read_bytes() == b"interrupted part"
                assert (restored / "legacy-bucket").stat().st_mtime_ns == legacy_time
                assert not list(base.glob(".moon-s3-restore-*"))
                admin("check", "--root", restored)
                start(restored)
                listing = ET.fromstring(request("GET", "/")[2])
                creation_times = {item.findtext("s3:Name", namespaces=NS): item.findtext("s3:CreationDate", namespaces=NS)
                                  for item in listing.findall("s3:Buckets/s3:Bucket", NS)}
                for item in ET.fromstring(bucket_list).findall("s3:Buckets/s3:Bucket", NS):
                    assert creation_times[item.findtext("s3:Name", namespaces=NS)] == item.findtext("s3:CreationDate", namespaces=NS)
                assert creation_times["legacy-bucket"] == "2000-01-01T00:00:00.000Z"
                status, headers, body = request("GET", target)
                assert (status, body) == (200, payload)
                for name in ["ETag", "Content-Type"]:
                    assert headers[name] == original_headers[name]
                assert request("GET", target, headers={"Range": "bytes=65530-65550"})[2] == payload[65530:65551]
                assert request("GET", "/backup-bucket/empty")[2] == b""
                assert request("HEAD", "/backup-bucket/completed")[1]["ETag"] == completed_etag
                status, _, body = request("GET", "/backup-bucket/resume?uploadId=" + upload)
                assert status == 200 and ET.fromstring(body).findtext("s3:Part/s3:ETag", namespaces=NS) == first_etag
                last = b"resumed after restore"
                status, headers, _ = request("PUT", f"/backup-bucket/resume?uploadId={upload}&partNumber=2", last)
                assert status == 200
                complete("/backup-bucket/resume", upload, [(1, first_etag), (2, headers["ETag"])])
                assert request("GET", "/backup-bucket/resume")[2] == first + last
                stop()

                raced_destination = base / "raced-destination"
                publish = storage_admin.publish_restore

                def concurrent_destination(stage, destination):
                    destination.mkdir()
                    (destination / "unrelated").write_bytes(b"preserve existing data")
                    publish(stage, destination)

                with unittest.mock.patch.object(storage_admin, "publish_restore", side_effect=concurrent_destination):
                    try:
                        storage_admin.restore(archive, raced_destination, 1024 ** 4)
                    except storage_admin.AdminError:
                        pass
                    else:
                        raise AssertionError("Restore must not overwrite a concurrently created destination")
                assert (raced_destination / "unrelated").read_bytes() == b"preserve existing data"
                assert not list(base.glob(".moon-s3-restore-*"))

                with tarfile.open(archive) as source:
                    manifest = source.extractfile("manifest.json").read()

                def malicious(name, manifest_body=manifest, member_name=None, kind=tarfile.REGTYPE, body=b"bad"):
                    attack = base / (name + ".tar")
                    with tarfile.open(attack, "w") as output:
                        member = tarfile.TarInfo("manifest.json")
                        member.size = len(manifest_body)
                        output.addfile(member, io.BytesIO(manifest_body))
                        if member_name is not None:
                            member = tarfile.TarInfo(member_name)
                            member.type = kind
                            member.linkname = "../../outside"
                            member.size = len(body) if kind == tarfile.REGTYPE else 0
                            output.addfile(member, io.BytesIO(body) if member.size else None)
                    destination = base / name
                    admin("restore", "--archive", attack, "--root", destination, success=False)
                    assert not destination.exists()
                    assert not list(base.glob(".moon-s3-restore-*"))

                malicious("traversal", member_name="../../outside")
                malicious("absolute", member_name="/tmp/moon-s3-unexpected")
                object_record = json.loads(manifest)["entries"]["backup-bucket/objects/" + original.name]
                member_name = "data/backup-bucket/objects/" + original.name
                malicious("symlink", member_name=member_name, kind=tarfile.SYMTYPE)
                malicious("hardlink", member_name=member_name, kind=tarfile.LNKTYPE)
                malicious("device", member_name=member_name, kind=tarfile.CHRTYPE)
                malicious("wrong-size", member_name=member_name)
                malicious("checksum", member_name=member_name, body=b"!" * object_record["size"])
                malicious("missing")
                malicious("duplicate-json", manifest_body=b'{"format":"moon-s3-backup-v1","entries":{},"entries":{}}')
                malicious("unknown-format", manifest_body=b'{"format":"future","entries":{}}')
                changed = json.loads(manifest)
                changed["entries"]["../outside"] = changed["entries"].pop("empty-bucket")
                malicious("manifest-traversal", manifest_body=json.dumps(changed).encode())
                changed = json.loads(manifest)
                changed["entries"]["backup-bucket/objects/" + original.name]["size"] = -1
                malicious("negative-size", manifest_body=json.dumps(changed).encode())
                duplicate = base / "duplicate.tar"
                with tarfile.open(archive) as source, tarfile.open(duplicate, "w") as output:
                    for member in source:
                        with source.extractfile(member) as body:
                            output.addfile(member, body)
                    member = tarfile.TarInfo(member_name)
                    member.size = len(payload)
                    output.addfile(member, io.BytesIO(payload))
                admin("restore", "--archive", duplicate, "--root", base / "duplicate", success=False)
                truncated = base / "truncated.tar"
                with archive.open("rb") as source, truncated.open("wb") as output:
                    output.write(source.read(8192))
                admin("restore", "--archive", truncated, "--root", base / "truncated", success=False)
                empty_root = base / "empty-root"
                empty_root.mkdir()
                admin("backup", "--root", empty_root, "--output", base / "empty.tar")
                report = admin("restore", "--archive", base / "empty.tar", "--root", base / "empty-restore")
                assert report["buckets"] == report["objects"] == 0
                assert not (base / "outside").exists()
                assert not list(base.glob(".moon-s3-backup-*"))
                assert not list(base.glob(".moon-s3-restore-*"))
                print("Admin regression passed: locked snapshots, SHA-256 restore, metadata/timestamps, multipart resume, corruption and unsafe archive rejection.")
            except BaseException:
                log.seek(0)
                print(log.read().decode(errors="replace"))
                raise
            finally:
                stop()


if __name__ == "__main__":
    main()
