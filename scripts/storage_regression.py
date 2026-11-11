#!/usr/bin/env python3
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


PROJECT = pathlib.Path(__file__).resolve().parents[1]
EXECUTABLE = PROJECT / "_build/native/debug/build/cmd/main/main.exe"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def main():
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    def request(method, path="/storage-bucket/object", body=b"", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def etag(payload):
        return hashlib.md5(payload).hexdigest()

    with tempfile.TemporaryDirectory(prefix="moon-s3-storage-") as directory:
        root = pathlib.Path(directory)
        objects = root / "storage-bucket" / "objects"
        objects.mkdir(parents=True)
        environment = dict(os.environ, MOON_S3_ROOT=directory, MOON_S3_LISTEN=f"127.0.0.1:{port}")
        environment.pop("MOON_S3_ACCESS_KEY", None)
        environment.pop("MOON_S3_SECRET_KEY", None)
        server = None
        with tempfile.TemporaryFile() as server_log:
            def start():
                nonlocal port
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                environment["MOON_S3_LISTEN"] = f"127.0.0.1:{port}"
                process = subprocess.Popen([str(EXECUTABLE)], env=environment, stdout=server_log, stderr=server_log)
                try:
                    for attempt in range(200):
                        if process.poll() is not None:
                            raise RuntimeError("server exited before becoming healthy")
                        try:
                            if request("GET", "/healthz")[0] == 200:
                                return process
                        except OSError:
                            pass
                        time.sleep(0.025)
                    raise RuntimeError("server did not become healthy")
                except BaseException:
                    process.kill()
                    process.wait()
                    raise

            def stop():
                nonlocal server
                if server is not None:
                    server.kill()
                    server.wait(timeout=5)
                    server = None

            def path_for(key):
                return "/storage-bucket/" + urllib.parse.quote(key, safe="/")

            def files(key):
                content = objects / (key.encode().hex() + ".object")
                metadata = objects / (key.encode().hex() + ".meta")
                return content, metadata

            def stage(key, payload, content_type, pending=False, expected_etag=None):
                content, metadata = files(key)
                suffix = ".pending" if pending else ""
                pathlib.Path(str(content) + suffix).write_bytes(payload)
                pathlib.Path(str(metadata) + suffix).write_text(
                    f"etag={expected_etag or etag(payload)}\ncontent-type={content_type}\n"
                )

            def journal(key, text):
                return pathlib.Path(str(files(key)[0]) + ".txn").write_text(text)

            def check(key, payload, content_type, expected_etag=None):
                expected = '"' + (expected_etag or etag(payload)) + '"'
                status, headers, body = request("GET", path_for(key))
                assert (status, body) == (200, payload), (key, status, len(body))
                assert headers["ETag"] == expected, (key, headers)
                assert headers["Content-Type"] == content_type
                status, headers, _ = request("HEAD", path_for(key))
                assert status == 200 and headers["ETag"] == expected
                assert headers["Content-Length"] == str(len(payload))

            def assert_clean():
                assert not any(item.name.endswith((".pending", ".txn", ".tmp")) for item in objects.iterdir())

            try:
                old = b"original object"
                new = b"replacement object with different length"
                for key in ["uncommitted", "marker-tmp", "prepared", "content-renamed", "both-renamed", "deleted", "content-deleted", "both-deleted", "目录/a +%&"]:
                    stage(key, old, "application/old")
                for key in ["uncommitted", "marker-tmp", "prepared", "content-renamed", "both-renamed", "new-object", "目录/a +%&"]:
                    stage(key, new, "application/new", pending=True)
                pathlib.Path(str(files("marker-tmp")[0]) + ".txn.tmp").write_text("put\n")
                for key in ["prepared", "content-renamed", "both-renamed", "new-object", "目录/a +%&"]:
                    journal(key, "put\n")
                for key in ["content-renamed", "both-renamed"]:
                    content, metadata = files(key)
                    pathlib.Path(str(content) + ".pending").replace(content)
                    if key == "both-renamed":
                        pathlib.Path(str(metadata) + ".pending").replace(metadata)
                for key in ["deleted", "content-deleted", "both-deleted"]:
                    journal(key, "delete\n")
                    content, metadata = files(key)
                    if key != "deleted":
                        content.unlink()
                    if key == "both-deleted":
                        metadata.unlink()
                upload_id = "a" * 64
                upload_directory = root / "storage-bucket" / ".uploads" / upload_id
                upload_directory.mkdir(parents=True)
                (upload_directory / "part.1").write_bytes(new)
                multipart_etag = hashlib.md5(hashlib.md5(new).digest()).hexdigest() + "-1"
                stage("multipart", new, "application/multipart", pending=True, expected_etag=multipart_etag)
                journal("multipart", f"put\n{upload_id}\n")
                pathlib.Path(str(files("legacy")[0]) + ".tmp").write_bytes(b"interrupted legacy upload")
                server = start()
                for key in ["uncommitted", "marker-tmp"]:
                    check(key, old, "application/old")
                for key in ["prepared", "content-renamed", "both-renamed", "new-object", "目录/a +%&"]:
                    check(key, new, "application/new")
                for key in ["deleted", "content-deleted", "both-deleted", "legacy"]:
                    assert request("GET", path_for(key))[0] == 404
                    assert not files(key)[1].exists()
                check("multipart", new, "application/multipart", multipart_etag)
                assert not upload_directory.exists()
                assert_clean()
                stop()
                server = start()
                check("content-renamed", new, "application/new")
                assert_clean()

                second_environment = dict(environment, MOON_S3_LISTEN="127.0.0.1:0")
                second = subprocess.run([str(EXECUTABLE)], env=second_environment, capture_output=True, timeout=5)
                assert second.returncode != 0 and b"already in use" in second.stdout, second
                check("prepared", new, "application/new")

                assert request("PUT", body=old, headers={"Content-Type": "application/old"})[0] == 200
                blocked = pathlib.Path(str(files("object")[0]) + ".pending")
                blocked.mkdir()
                (blocked / "unwritable").write_bytes(b"failure injection")
                status, _, body = request("PUT", body=new, headers={"Content-Type": "application/new"})
                assert status == 500 and ET.fromstring(body).findtext("Code") == "InternalError", (status, body)
                check("object", old, "application/old")
                (blocked / "unwritable").unlink()
                blocked.rmdir()
                assert request("PUT", body=new, headers={"Content-Type": "application/new"})[0] == 200
                check("object", new, "application/new")

                assert request("PUT", path_for("retry"), old)[0] == 200
                retry_metadata = files("retry")[1]
                retry_metadata.unlink()
                retry_metadata.mkdir()
                (retry_metadata / "blocked").write_bytes(b"failure injection")
                status, _, body = request("PUT", path_for("retry"), new, {"Content-Type": "application/retry"})
                assert status == 500 and ET.fromstring(body).findtext("Code") == "InternalError"
                for method, target in [("GET", path_for("retry")), ("HEAD", path_for("retry")), ("GET", "/storage-bucket")]:
                    assert request(method, target)[0] == 500
                (retry_metadata / "blocked").unlink()
                retry_metadata.rmdir()
                check("retry", new, "application/retry")
                assert_clean()

                retry_metadata.unlink()
                retry_metadata.mkdir()
                (retry_metadata / "blocked").write_bytes(b"delete failure injection")
                assert request("DELETE", path_for("retry"))[0] == 500
                assert request("GET", path_for("retry"))[0] == 500
                (retry_metadata / "blocked").unlink()
                retry_metadata.rmdir()
                assert request("GET", path_for("retry"))[0] == 404
                assert_clean()

                payloads = [bytes([number]) * (65536 + number * 777) for number in range(1, 7)]
                expected = {'"' + etag(payload) + '"': payload for payload in payloads}
                assert request("PUT", body=payloads[0], headers={"Content-Type": "application/snapshot"})[0] == 200

                def writer():
                    for iteration in range(24):
                        payload = payloads[iteration % len(payloads)]
                        assert request("PUT", body=payload, headers={"Content-Type": "application/snapshot"})[0] == 200

                def reader():
                    for iteration in range(30):
                        status, headers, body = request("GET")
                        assert status == 200 and expected[headers["ETag"]] == body
                        assert headers["Content-Type"] == "application/snapshot"
                        status, headers, _ = request("HEAD")
                        assert status == 200 and int(headers["Content-Length"]) == len(expected[headers["ETag"]])
                        listing = ET.fromstring(request("GET", "/storage-bucket?prefix=object")[2])
                        item = listing.find("s3:Contents", NS)
                        assert int(item.findtext("s3:Size", namespaces=NS)) == len(expected[item.findtext("s3:ETag", namespaces=NS)])

                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [executor.submit(writer)] + [executor.submit(reader) for number in range(3)]
                    for future in futures:
                        future.result()
                assert_clean()

                crash_payload = b"crash recovery payload" * (1024 * 1024)
                assert request("PUT", body=old, headers={"Content-Type": "application/old"})[0] == 200
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(request, "PUT", "/storage-bucket/object", crash_payload, {"Content-Type": "application/crash"})
                    deadline = time.monotonic() + 15
                    while not blocked.exists() and not future.done() and time.monotonic() < deadline:
                        time.sleep(0.001)
                    assert blocked.exists(), "did not observe the staging file before upload completed"
                    stop()
                    try:
                        future.result()
                    except (OSError, http.client.HTTPException):
                        pass
                server = start()
                status, headers, body = request("GET")
                assert status == 200 and body in [old, crash_payload]
                assert headers["ETag"] == '"' + etag(body) + '"'
                assert headers["Content-Type"] == ("application/old" if body == old else "application/crash")
                assert_clean()

                stop()
                journal("prepared", "put\n../outside\n")
                broken = subprocess.run([str(EXECUTABLE)], env=environment, capture_output=True, timeout=5)
                assert broken.returncode != 0 and b"Storage recovery failed" in broken.stdout, broken
                assert pathlib.Path(str(files("prepared")[0]) + ".txn").exists()
                pathlib.Path(str(files("prepared")[0]) + ".txn").unlink()
                journal("missing-data", "put\n")
                broken = subprocess.run([str(EXECUTABLE)], env=environment, capture_output=True, timeout=5)
                assert broken.returncode != 0 and b"Missing transaction data" in broken.stdout, broken
                pathlib.Path(str(files("missing-data")[0]) + ".txn").unlink()
                server = start()
                check("prepared", new, "application/new")
                assert_clean()
                print("Storage regression passed: journal replay, interrupted PUT, multipart cleanup, deletion, snapshots, disk errors and process lock.")
            except BaseException:
                server_log.seek(0)
                print(server_log.read().decode(errors="replace"))
                raise
            finally:
                stop()


if __name__ == "__main__":
    main()
