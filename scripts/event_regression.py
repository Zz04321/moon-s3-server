#!/usr/bin/env python3
import concurrent.futures
import hashlib
import hmac
import http.client
import http.server
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

from auth_regression import ACCESS_KEY, SECRET_KEY, sign


PROJECT = pathlib.Path(__file__).resolve().parents[1]
SECRET = "event-test-secret-事件"


def main():
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    attempts = []
    accepted = []
    receiver_status = 503
    receiver_delay = 0
    receiver_lock = threading.Lock()

    class Receiver(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            identity = self.headers["X-Moon-S3-Event-Id"]
            signature = self.headers.get("X-Moon-S3-Signature")
            with receiver_lock:
                status, delay = receiver_status, receiver_delay
                attempts.append((identity, body, signature, self.path))
                if 200 <= status < 300:
                    accepted.append((identity, body))
            if delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Location", "/unexpected-redirect")
            self.end_headers()

        def log_message(self, *args):
            pass

    receiver = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    receiver_thread.start()

    def wait_for(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.025)
        raise AssertionError("Timed out waiting for event state")

    try:
        with tempfile.TemporaryDirectory(prefix="moon-s3-events-") as directory:
            base = pathlib.Path(directory)
            root = base / "data"
            server = None
            port = None
            bucket = "/event-bucket"
            key = "目录/object +%.txt"
            target = bucket + "/" + urllib.parse.quote(key, safe="/")
            expected = []

            def request(method, path=target, body=b"", headers=None, authenticated=True):
                outgoing = sign(method, path, f"127.0.0.1:{port}", body, extra_headers=headers) if authenticated else headers or {}
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    connection.request(method, path, body=body, headers=outgoing)
                    response = connection.getresponse()
                    return response.status, {name.lower(): value for name, value in response.getheaders()}, response.read()
                finally:
                    connection.close()

            def put(payload, path=target, object_key=key):
                result = request("PUT", path, payload)
                assert result[0] == 200, result
                version = result[1].get("x-amz-version-id")
                expected.append(("ObjectCreated:Put", object_key, version))
                return version

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

            def queued():
                return list((root / ".events").glob("*.json"))

            with tempfile.TemporaryFile() as log:
                def start(enabled=True, secret=True, overrides=None):
                    nonlocal server, port
                    with socket.socket() as listener:
                        listener.bind(("127.0.0.1", 0))
                        port = listener.getsockname()[1]
                    environment = dict(os.environ, MOON_S3_ROOT=str(root), MOON_S3_LISTEN=f"127.0.0.1:{port}",
                                       MOON_S3_ACCESS_KEY=ACCESS_KEY, MOON_S3_SECRET_KEY=SECRET_KEY)
                    for name in ["MOON_S3_WEBHOOK_URL", "MOON_S3_WEBHOOK_SECRET", "MOON_S3_WEBHOOK_TIMEOUT_MS"]:
                        environment.pop(name, None)
                    if enabled:
                        environment.update(MOON_S3_WEBHOOK_URL=f"http://127.0.0.1:{receiver.server_port}/hook?source=s3",
                                           MOON_S3_WEBHOOK_TIMEOUT_MS="200")
                        if secret:
                            environment["MOON_S3_WEBHOOK_SECRET"] = SECRET
                    environment.update(overrides or {})
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
                    put(b"first payload")
                    copied = request("PUT", bucket + "/copy", headers={"x-amz-copy-source": target})
                    assert copied[0] == 200
                    expected.append(("ObjectCreated:Copy", "copy", None))
                    upload = ET.fromstring(request("POST", bucket + "/multipart?uploads")[2]).findtext("{*}UploadId")
                    part = request("PUT", bucket + f"/multipart?uploadId={upload}&partNumber=1", b"multipart payload")
                    assert part[0] == 200
                    xml = f'<CompleteMultipartUpload><Part><PartNumber>1</PartNumber><ETag>{part[1]["etag"]}</ETag></Part></CompleteMultipartUpload>'.encode()
                    assert request("POST", bucket + "/multipart?uploadId=" + upload, xml)[0] == 200
                    expected.append(("ObjectCreated:CompleteMultipartUpload", "multipart", None))
                    assert request("DELETE", bucket + "/copy")[0] == 204
                    expected.append(("ObjectRemoved:Delete", "copy", None))
                    assert request("DELETE", bucket + "/absent")[0] == 204
                    assert request("GET")[0] == 200
                    assert request("HEAD")[0] == 200
                    assert request("PUT", target, b"unauthorized", authenticated=False)[0] == 403
                    bad_complete = request("POST", bucket + "/multipart?uploadId=" + upload, xml)
                    assert bad_complete[0] == 404
                    assert len(queued()) == len(expected)
                    wait_for(lambda: len(attempts) >= 2)
                    assert attempts[0][0] == attempts[1][0] and attempts[0][1] == attempts[1][1]

                    assert request("PUT", bucket + "?versioning", b"<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>")[0] == 200
                    version = put(b"version content")
                    marker = request("DELETE")[1]["x-amz-version-id"]
                    expected.append(("ObjectRemoved:DeleteMarkerCreated", key, marker))
                    assert request("DELETE", target + "?versionId=" + marker)[0] == 204
                    expected.append(("ObjectRemoved:Delete", key, marker))
                    assert request("DELETE", target + "?versionId=" + version)[0] == 204
                    expected.append(("ObjectRemoved:Delete", key, version))
                    assert request("DELETE", target + "?versionId=" + "0" * 64)[0] == 204
                    assert request("PUT", bucket + "?versioning", b"<VersioningConfiguration><Status>Suspended</Status></VersioningConfiguration>")[0] == 200
                    assert put(b"suspended content") == "null"
                    assert request("DELETE")[1]["x-amz-version-id"] == "null"
                    expected.append(("ObjectRemoved:DeleteMarkerCreated", key, "null"))
                    assert request("PUT", "/removed-bucket")[0] == 200
                    assert request("PUT", "/removed-bucket/object", b"temporary")[0] == 200
                    assert request("DELETE", "/removed-bucket/object")[0] == 204
                    assert request("DELETE", "/removed-bucket")[0] == 204
                    expected += [("ObjectCreated:Put", "object", None), ("ObjectRemoved:Delete", "object", None)]
                    receiver_delay = 2
                    stop()
                    before = len(attempts)
                    start()
                    wait_for(lambda: len(attempts) > before)
                    started = time.monotonic()
                    put(b"not blocked by callback", bucket + "/slow", "slow")
                    assert time.monotonic() - started < 1.5
                    wait_for(lambda: len(attempts) >= before + 2)
                    stop()
                    receiver_delay = 0
                    assert len(queued()) == len(expected)
                    report = admin("check", "--root", root)
                    assert report["queued_events"] == len(expected)
                    admin("backup", "--root", root, "--output", base / "queued.tar")
                    root = base / "restored"
                    admin("restore", "--root", root, "--archive", base / "queued.tar")
                    restored = {path.stem: path.read_bytes() for path in queued()}
                    start(enabled=False)
                    time.sleep(0.2)
                    assert {path.stem: path.read_bytes() for path in queued()} == restored
                    assert request("PUT", bucket + "/disabled", b"no new events")[0] == 200
                    assert len(queued()) == len(expected)
                    stop()
                    receiver_status = 302
                    before = len(attempts)
                    start()
                    wait_for(lambda: len(attempts) > before)
                    stop()
                    assert len(queued()) == len(expected)
                    assert all(item[3] == "/hook?source=s3" for item in attempts)
                    receiver_status = 204
                    start()
                    wait_for(lambda: not queued())
                    stop()
                    delivered = {identity: body for identity, body in accepted}
                    assert delivered == restored
                    records = [json.loads(body)["Records"][0] for body in delivered.values()]
                    actual = [(record["eventName"], urllib.parse.unquote_plus(record["s3"]["object"]["key"]), record["s3"]["object"].get("versionId")) for record in records]
                    assert sorted(actual, key=str) == sorted(expected, key=str), (actual, expected)
                    assert any(record["s3"]["bucket"]["name"] == "removed-bucket" for record in records)
                    for identity, body, signature, path in attempts:
                        assert signature == "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
                        assert json.loads(body)["Records"][0]["eventId"] == identity
                    assert admin("check", "--root", root)["queued_events"] == 0
                    print("Event HTTP regression passed: put/copy/multipart/deletes, versions, HMAC, retries, redirects, disabled mode and queued backup/restore.")

                    receiver_status = 503
                    start()
                    assert request("PUT", "/crash-bucket")[0] == 200
                    assert request("PUT", "/crash-bucket/disk", b"before queue failure")[0] == 200
                    held = base / "held-events"
                    (root / ".events").rename(held)
                    (root / ".events").write_bytes(b"queue directory unavailable")
                    assert request("PUT", "/crash-bucket/disk", b"must not commit")[0] == 500
                    assert request("GET", "/crash-bucket/disk")[2] == b"before queue failure"
                    stop()
                    (root / ".events").unlink()
                    held.rename(root / ".events")
                    start()
                    acknowledged = []

                    def writer():
                        for number in range(100):
                            try:
                                response = request("PUT", f"/crash-bucket/object-{number}", b"crash payload" * 4096)
                                if response[0] == 200:
                                    acknowledged.append(f"object-{number}")
                            except (OSError, http.client.HTTPException):
                                return

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        task = executor.submit(writer)
                        wait_for(lambda: len(acknowledged) >= 5)
                        stop()
                        task.result()
                    start()
                    stop()
                    crash_events = [json.loads(path.read_bytes())["Records"][0] for path in queued()]
                    assert set(acknowledged) <= {urllib.parse.unquote_plus(record["s3"]["object"]["key"]) for record in crash_events}
                    admin("check", "--root", root)

                    template = records[0]
                    recovery = root / "crash-bucket/objects"
                    encoded = b"recovered".hex()
                    identity = "1" * 64

                    def event_body(identity, name="recovered"):
                        record = json.loads(json.dumps(template))
                        record["eventId"] = identity
                        record["eventName"] = "ObjectCreated:Put"
                        record["s3"] = {"bucket": {"name": "crash-bucket", "arn": "arn:aws:s3:::crash-bucket"},
                                        "object": {"key": name, "size": 7, "eTag": hashlib.md5(b"recover").hexdigest()}}
                        return json.dumps({"Records": [record]}).encode()

                    content = recovery / (encoded + ".object")
                    journal = recovery / (encoded + ".object.txn")
                    event_stage = recovery / (encoded + ".object.event.pending")
                    content.with_suffix(".object.pending").write_bytes(b"recover")
                    (recovery / (encoded + ".meta.pending")).write_text(f"etag={hashlib.md5(b'recover').hexdigest()}\ncontent-type=application/recovery\n")
                    event_stage.write_bytes(event_body(identity))
                    journal.write_text(f"event\n{identity}\nput\n")
                    start()
                    assert request("GET", "/crash-bucket/recovered")[2] == b"recover"
                    assert not journal.exists() and not event_stage.exists()
                    stop()
                    assert (root / ".events" / (identity + ".json")).exists()
                    second = "2" * 64
                    (root / ".events" / (second + ".pending")).write_bytes(event_body(second))
                    journal.write_text(f"event\n{second}\nput\n")
                    start()
                    stop()
                    assert not journal.exists() and (root / ".events" / (second + ".json")).exists()
                    third = "3" * 64
                    (root / ".events" / (third + ".pending")).write_bytes(event_body(third))
                    event_stage.write_bytes(b"uncommitted staging")
                    start(enabled=False)
                    stop()
                    assert not event_stage.exists() and (root / ".events" / (third + ".json")).exists()
                    admin("check", "--root", root)
                    bad = root / ".events" / (third + ".json")
                    good = bad.read_bytes()
                    bad.write_bytes(b'{"Records":[]}')
                    admin("check", "--root", root, success=False)
                    try:
                        start(enabled=False)
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError("Corrupt queued event allowed startup")
                    finally:
                        stop()
                        bad.write_bytes(good)
                    receiver_status = 200
                    start(secret=False)
                    wait_for(lambda: not queued())
                    stop()
                    assert attempts[-1][2] is None
                    assert admin("check", "--root", root)["queued_events"] == 0
                    print("Event recovery regression passed: process kill, staged/queued journals, committed pending release, orphan cleanup and corrupt-event refusal.")

                    for overrides in [{"MOON_S3_WEBHOOK_URL": "ftp://invalid"},
                                      {"MOON_S3_WEBHOOK_URL": "http://user:password@example.test"},
                                      {"MOON_S3_WEBHOOK_SECRET": ""}, {"MOON_S3_WEBHOOK_TIMEOUT_MS": "0"}]:
                        try:
                            start(overrides=overrides)
                        except RuntimeError:
                            pass
                        else:
                            raise AssertionError("Invalid webhook configuration allowed startup")
                        finally:
                            stop()
                    print("Event configuration regression passed: invalid endpoints, secrets and timeouts fail fast.")
                except BaseException:
                    stop()
                    log.seek(0)
                    print(log.read().decode(errors="replace"))
                    raise
                finally:
                    stop()
    finally:
        receiver.shutdown()
        receiver.server_close()
        receiver_thread.join(timeout=5)


if __name__ == "__main__":
    main()
