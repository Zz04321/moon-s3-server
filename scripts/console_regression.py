#!/usr/bin/env python3
import argparse
import concurrent.futures
import http.client
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

from auth_regression import ACCESS_KEY, SECRET_KEY
from console import ConsoleServer
from s3_client import S3Client, object_path


PROJECT = pathlib.Path(__file__).resolve().parents[1]
PASSWORD = "console-regression-password"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description="Exercise console sessions, authorization boundaries and management workflows.")
    parser.add_argument("--browser", action="store_true", help="also run Playwright Chromium UI checks")
    arguments = parser.parse_args()
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with tempfile.TemporaryDirectory(prefix="moon-s3-console-") as directory:
        base = pathlib.Path(directory)
        storage_port, console_port = free_port(), free_port()
        endpoint = f"http://127.0.0.1:{storage_port}"
        origin = f"http://127.0.0.1:{console_port}"
        environment = dict(os.environ, MOON_S3_ROOT=str(base / "data"), MOON_S3_LISTEN=f"127.0.0.1:{storage_port}",
                           MOON_S3_ACCESS_KEY=ACCESS_KEY, MOON_S3_SECRET_KEY=SECRET_KEY,
                           MOON_S3_CONSOLE_PASSWORD=PASSWORD, MOON_S3_ENDPOINT=endpoint)
        for name in ["MOON_S3_WEBHOOK_URL", "MOON_S3_WEBHOOK_SECRET", "MOON_S3_WEBHOOK_TIMEOUT_MS"]:
            environment.pop(name, None)
        processes = []
        cookie = ""
        csrf = ""

        def request(method, path, body=None, headers=None, authenticated=True):
            outgoing = dict(headers or {})
            if method == "POST":
                outgoing.setdefault("Origin", origin)
                if authenticated:
                    outgoing.setdefault("X-Console-CSRF", csrf)
            if isinstance(body, dict):
                outgoing.setdefault("Content-Type", "application/json")
                body = json.dumps(body).encode()
            if authenticated and cookie:
                outgoing.setdefault("Cookie", cookie)
            connection = http.client.HTTPConnection("127.0.0.1", console_port, timeout=30)
            try:
                connection.request(method, path, body=body, headers=outgoing)
                response = connection.getresponse()
                return response.status, {name.lower(): value for name, value in response.getheaders()}, response.read()
            finally:
                connection.close()

        def action(command, action, **fields):
            result = request("POST", "/api/action", {"command": command, "action": action, **fields})
            assert result[0] == 200, result
            return json.loads(result[2])

        def get(path):
            response = request("GET", path)
            assert response[0] == 200, response
            return json.loads(response[2])

        with tempfile.TemporaryFile() as log:
            def launch(command):
                process = subprocess.Popen(command, env=environment, stdout=log, stderr=log)
                processes.append(process)
                return process

            def wait_healthy(port, path="/"):
                for attempt in range(200):
                    if any(process.poll() is not None for process in processes):
                        raise RuntimeError("A service exited before becoming healthy")
                    try:
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                        connection.request("GET", path)
                        response = connection.getresponse()
                        response.read()
                        connection.close()
                        if response.status == 200:
                            return
                    except OSError:
                        pass
                    time.sleep(0.025)
                raise RuntimeError("Service did not become healthy")

            try:
                launch([str(PROJECT / "_build/native/debug/build/cmd/main/main.exe")])
                wait_healthy(storage_port, "/healthz")
                launch([sys.executable, str(PROJECT / "scripts/console.py"), "--port", str(console_port)])
                wait_healthy(console_port)
                for path in ["/", "/app.js", "/styles.css"]:
                    result = request("GET", path, authenticated=False)
                    assert result[0] == 200 and "no-store" in result[1]["cache-control"]
                    assert "frame-ancestors 'none'" in result[1]["content-security-policy"]
                    assert PASSWORD.encode() not in result[2] and SECRET_KEY.encode() not in result[2]
                assert request("GET", "/../scripts/console.py", authenticated=False)[0] == 404
                for path in ["/api/session", "/api/buckets", "/api/objects?bucket=private-bucket", "/api/download?bucket=private-bucket&key=a"]:
                    assert request("GET", path, authenticated=False)[0] == 401
                assert request("POST", "/api/login", {"password": PASSWORD}, {"Origin": "https://evil.test"}, authenticated=False)[0] == 403
                assert request("POST", "/api/login", {"password": PASSWORD}, {"Origin": ""}, authenticated=False)[0] == 403
                assert request("POST", "/api/login", {"password": "wrong"}, authenticated=False)[0] == 401
                assert request("POST", "/api/login", b"password=bad", {"Content-Type": "text/plain"}, authenticated=False)[0] == 415
                assert request("POST", "/api/login", b"{", {"Content-Type": "application/json"}, authenticated=False)[0] == 400
                assert request("POST", "/api/login", {"password": PASSWORD}, {"Host": "rebound.test"}, authenticated=False)[0] == 403
                result = request("POST", "/api/login", {"password": PASSWORD}, authenticated=False)
                assert result[0] == 200, result
                assert "HttpOnly" in result[1]["set-cookie"] and "SameSite=Strict" in result[1]["set-cookie"]
                assert "Path=/api/" in result[1]["set-cookie"]
                cookie = result[1]["set-cookie"].split(";", 1)[0]
                csrf = json.loads(result[2])["csrf"]
                assert get("/api/session")["csrf"] == csrf
                assert request("POST", "/api/action", {"command": "bucket", "action": "create", "bucket": "blocked-bucket"}, {"X-Console-CSRF": "wrong"})[0] == 403
                assert request("POST", "/api/action", {"command": "bucket", "action": "create", "bucket": "blocked-bucket"}, {"Origin": "https://evil.test"})[0] == 403
                for method in ["PUT", "DELETE", "OPTIONS"]:
                    assert request(method, "/api/action")[0] == 405
                for document in [
                    {"command": "object", "action": "upload", "file": "/etc/passwd"},
                    {"command": "check", "root": "/tmp"}, {"command": "restore", "archive": "/tmp/x"},
                    {"command": "object", "action": "download", "output": "/tmp/overwrite"},
                    {"command": "bucket", "action": "create", "bucket": "valid-bucket", "endpoint": "http://evil"},
                    {"command": "object", "action": "head"}, {"command": ["bucket"], "action": "list"},
                    {"command": "presign", "bucket": "valid-bucket", "key": "x", "expires": -1},
                ]:
                    assert request("POST", "/api/action", document)[0] == 400
                action("bucket", "create", bucket="console-bucket")
                assert get("/api/buckets")["buckets"][0]["name"] == "console-bucket"
                key = '目录/<img src=x onerror="window.injected=1"> +%.txt'
                query = urllib.parse.urlencode({"bucket": "console-bucket", "key": key})
                content = b"browser content\x00\xff"
                result = request("POST", "/api/upload?" + query, content, {"Content-Type": "application/test"})
                assert result[0] == 200 and json.loads(result[2])["size"] == len(content), result
                assert get("/api/objects?bucket=console-bucket")["entries"][0]["key"] == key
                assert action("object", "head", bucket="console-bucket", key=key)["headers"]["content-type"] == "application/test"
                result = request("GET", "/api/download?" + query)
                assert result[0] == 200 and result[2] == content
                assert result[1]["content-type"] == "application/octet-stream"
                assert result[1]["content-disposition"].startswith("attachment;")
                assert request("POST", "/api/upload?" + query, b"", {"Content-Length": str(65 * 1024 * 1024)})[0] == 413
                action("versioning", "enable", bucket="console-bucket")
                result = request("POST", "/api/upload?" + query, b"new version")
                version = json.loads(result[2])["version_id"]
                versions = get("/api/versions?bucket=console-bucket")["entries"]
                assert [item["version_id"] for item in versions] == [version, "null"]
                action("versions", "restore", bucket="console-bucket", key=key, version_id="null")
                assert request("GET", "/api/download?" + query)[2] == content
                action("object", "copy", source_bucket="console-bucket", source_key=key, bucket="console-bucket", key="copied")
                marker = action("object", "delete", bucket="console-bucket", key="copied")["version_id"]
                assert any(item["delete_marker"] for item in get("/api/versions?bucket=console-bucket")["entries"])
                action("object", "delete", bucket="console-bucket", key="copied", version_id=marker)
                action("access", "public", bucket="console-bucket", include_versions=True)
                assert action("access", "get", bucket="console-bucket")["policy"]
                action("access", "private", bucket="console-bucket")
                assert action("access", "get", bucket="console-bucket")["policy"] is None
                xml = '<CORSConfiguration><CORSRule><AllowedOrigin>*</AllowedOrigin><AllowedMethod>GET</AllowedMethod></CORSRule></CORSConfiguration>'
                action("cors", "put", bucket="console-bucket", xml=xml)
                assert "CORSRule" in action("cors", "get", bucket="console-bucket")["xml"]
                action("cors", "delete", bucket="console-bucket")
                assert request("POST", "/api/action", {"command": "cors", "action": "get", "bucket": "console-bucket"})[0] == 404
                url = action("presign", "", bucket="console-bucket", key=key, expires=60)["url"]
                parsed = urllib.parse.urlsplit(url)
                link = http.client.HTTPConnection(parsed.hostname, parsed.port)
                link.request("GET", parsed.path + "?" + parsed.query)
                response = link.getresponse()
                assert response.status == 200 and response.read() == content
                link.close()
                client = S3Client(endpoint, ACCESS_KEY, SECRET_KEY)
                _, upload = client.xml("POST", object_path("console-bucket", "unfinished", [("uploads", "")]))
                upload_id = upload.findtext("{*}UploadId")
                assert action("multipart", "list", bucket="console-bucket")["uploads"][0]["upload_id"] == upload_id
                action("multipart", "abort", bucket="console-bucket", key="unfinished", upload_id=upload_id)
                assert action("multipart", "list", bucket="console-bucket")["uploads"] == []
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(lambda number: client.request("PUT", object_path("console-bucket", f"page/{number:03d}"), b"page"), range(205)))
                first = get("/api/objects?bucket=console-bucket&prefix=page%2F")
                assert len(first["entries"]) == 200 and first["truncated"]
                second = get("/api/objects?" + urllib.parse.urlencode({"bucket": "console-bucket", "prefix": "page/", "token": first["token"]}))
                assert len(second["entries"]) == 5 and not second["truncated"]
                versions = get("/api/versions?bucket=console-bucket&prefix=page%2F")
                assert len(versions["entries"]) == 200 and versions["truncated"]
                last_versions = get("/api/versions?" + urllib.parse.urlencode({"bucket": "console-bucket", "prefix": "page/", "key_marker": versions["key_marker"], "version_marker": versions["version_marker"]}))
                assert len(last_versions["entries"]) == 5 and not last_versions["truncated"]
                assert request("GET", "/api/objects?bucket=console-bucket&bucket=other-bucket")[0] == 400
                old_cookie = cookie
                assert request("POST", "/api/logout")[0] == 200
                assert request("GET", "/api/buckets")[0] == 401
                assert request("GET", "/api/buckets", headers={"Cookie": old_cookie}, authenticated=False)[0] == 401
                for attempt in range(10):
                    assert request("POST", "/api/login", {"password": "wrong"}, authenticated=False)[0] == 401
                assert request("POST", "/api/login", {"password": PASSWORD}, authenticated=False)[0] == 429
                print("Console API regression passed: session/CSRF/Host boundaries, operation allowlist, CRUD, versions, public policies, CORS, links, pagination, uploads and logout.")

                if arguments.browser:
                    processes[-1].terminate()
                    processes[-1].wait(timeout=5)
                    processes.pop()
                    launch([sys.executable, str(PROJECT / "scripts/console.py"), "--port", str(console_port)])
                    wait_healthy(console_port)
                    browser_checks(origin, base, key)
                security_checks(client)

                result = subprocess.run([sys.executable, str(PROJECT / "scripts/console.py"), "--port", str(free_port())],
                                        env=dict(environment, MOON_S3_CONSOLE_PASSWORD="short"), capture_output=True, text=True, timeout=5)
                assert result.returncode != 0 and "at least 16" in result.stderr
            except BaseException:
                log.seek(0)
                print(log.read().decode(errors="replace"))
                raise
            finally:
                for process in reversed(processes):
                    process.terminate()
                    process.wait(timeout=5)


def browser_checks(origin, base, unsafe_key):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.grant_permissions(["clipboard-read", "clipboard-write"])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin)
        page.get_by_label("Administrator password").fill(PASSWORD)
        page.get_by_role("button", name="Open workspace").click()
        expect(page.get_by_role("button", name="console-bucket", exact=True)).to_be_visible()
        page.get_by_role("button", name="console-bucket", exact=True).click()
        expect(page.locator("#bucket-title")).to_have_text("console-bucket")
        page.get_by_label("Filter by key prefix").fill("page/")
        page.get_by_role("button", name="Search", exact=True).click()
        expect(page.locator(".object-key")).to_have_count(200)
        page.get_by_role("button", name="Next page").click()
        expect(page.locator(".object-key")).to_have_count(5)
        expect(page.locator("#result-count")).to_contain_text("201–205")
        page.get_by_label("Filter by key prefix").fill("目录/")
        page.get_by_role("button", name="Search", exact=True).click()
        expect(page.locator(".object-key")).to_have_count(1)
        expect(page.locator(".object-key")).to_contain_text(unsafe_key)
        assert page.evaluate("window.injected") is None
        assert page.locator("#objects img").count() == 0
        page.locator("#objects").get_by_role("button", name="Info", exact=True).click()
        expect(page.locator("#modal-title")).to_have_text("Object details")
        expect(page.locator("#modal-body pre")).to_contain_text("content-length")
        page.get_by_role("button", name="Close dialog").click()
        page.get_by_role("button", name="Create bucket", exact=True).first.click()
        page.get_by_label("Bucket name").fill("browser-bucket")
        page.locator("#modal-submit").click()
        expect(page.locator("#bucket-title")).to_have_text("browser-bucket")
        page.get_by_role("button", name="Versioning", exact=True).click()
        expect(page.locator("#modal-title")).to_have_text("Bucket versioning")
        page.locator("#modal-submit").click()
        expect(page.locator("#notice")).to_have_text("Versioning updated.")
        file = base / "browser.txt"
        file.write_text("uploaded from a real browser")
        page.get_by_role("button", name="Upload file", exact=True).click()
        page.locator('input[type="file"]').set_input_files(file)
        page.get_by_label("Object key", exact=True).fill("browser.txt")
        page.locator("#modal-submit").click()
        expect(page.locator(".object-key")).to_contain_text("browser.txt")
        with page.expect_download() as download:
            page.locator("#objects").get_by_role("link", name="Download").click()
        path = base / "browser-downloaded.txt"
        download.value.save_as(path)
        assert path.read_bytes() == file.read_bytes()
        page.locator("#objects").get_by_role("button", name="Link", exact=True).click()
        expect(page.get_by_label("Download link · expires in 15 minutes")).to_have_value(re.compile(".*X-Amz-Signature=.*"))
        page.locator("#modal-submit").click()
        expect(page.locator("#notice")).to_have_text("Temporary link copied.")
        assert "X-Amz-Signature=" in page.evaluate("navigator.clipboard.readText()")
        page.locator("#objects").get_by_role("button", name="Copy", exact=True).click()
        page.get_by_label("Destination key").fill("copied.txt")
        page.locator("#modal-submit").click()
        expect(page.locator(".object-key")).to_have_count(2)
        page.get_by_label("Version history").check()
        expect(page.locator("#objects").get_by_role("button", name="Restore")).to_have_count(2)
        page.locator("#objects").get_by_role("button", name="Restore").first.click()
        page.locator("#modal-submit").click()
        expect(page.locator("#notice")).to_have_text("Version restored.")
        page.get_by_label("Version history").uncheck()
        expect(page.locator("#objects").get_by_role("button", name="Restore")).to_have_count(0)
        page.get_by_role("button", name="Access", exact=True).click()
        page.get_by_label("Access mode").select_option("public")
        page.locator("#modal-submit").click()
        expect(page.locator("#notice")).to_have_text("Bucket access updated.")
        page.get_by_role("button", name="CORS rules", exact=True).click()
        page.get_by_label("CORS XML · leave empty to remove rules").fill('<CORSConfiguration><CORSRule><AllowedOrigin>*</AllowedOrigin><AllowedMethod>GET</AllowedMethod></CORSRule></CORSConfiguration>')
        page.locator("#modal-submit").click()
        expect(page.locator("#notice")).to_have_text("CORS rules updated.")
        page.get_by_role("button", name="Unfinished uploads", exact=True).click()
        expect(page.locator("#modal-body")).to_contain_text("No unfinished uploads.")
        page.get_by_role("button", name="Close dialog").click()
        page.screenshot(path="/tmp/moon-s3-console-desktop.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.get_by_role("button", name="Upload file", exact=True)).to_be_visible()
        page.screenshot(path="/tmp/moon-s3-console-mobile.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.set_viewport_size({"width": 1440, "height": 1000})
        row = page.locator("#objects tr").filter(has_text="copied.txt")
        row.get_by_role("button", name="Delete", exact=True).click()
        page.locator("#modal-submit").click()
        expect(page.locator(".object-key")).to_have_count(1)
        page.get_by_role("button", name="Delete bucket", exact=True).click()
        page.locator("#modal-submit").click()
        expect(page.locator("#modal-error")).not_to_be_empty()
        page.get_by_role("button", name="Close dialog").click()
        page.get_by_role("button", name="Sign out").click()
        expect(page.locator("#login-view")).to_be_visible()
        assert page.locator("#objects tr").count() == 0
        assert not errors, errors
        browser.close()
        print("Console browser regression passed: real login, XSS-safe keys, upload/download, copy, links, version restore, settings, responsive layout and logout.")


def security_checks(client):
    server = ConsoleServer(("127.0.0.1", 0), "https://console.example", client, PASSWORD)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        body = json.dumps({"password": PASSWORD})
        headers = {"Host": "console.example", "Origin": "https://console.example", "Content-Type": "application/json"}
        connection.request("POST", "/api/login", body, headers)
        response = connection.getresponse()
        cookie = response.getheader("Set-Cookie")
        assert response.status == 200 and "; Secure" in cookie
        response.read()
        connection.close()
        token = cookie.split(";", 1)[0].split("=", 1)[1]
        with server.lock:
            server.sessions[token]["expires"] = 0
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/api/session", headers={"Host": "console.example", "Cookie": cookie.split(";", 1)[0]})
        response = connection.getresponse()
        assert response.status == 401
        response.read()
        connection.close()
        print("Console session regression passed: HTTPS cookie attributes and absolute session expiry.")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


if __name__ == "__main__":
    main()
