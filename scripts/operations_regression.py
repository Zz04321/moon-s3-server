#!/usr/bin/env python3
import contextlib
import http.client
import json
import os
import pathlib
import signal
import socket
import subprocess
import tempfile
import time

from auth_regression import ACCESS_KEY, SECRET_KEY, sign


PROJECT = pathlib.Path(__file__).resolve().parents[1]
BINARY = PROJECT / "_build/native/debug/build/cmd/main/main.exe"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def request(port, method, path, body=b"", authenticated=False):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    host = f"127.0.0.1:{port}"
    headers = sign(method, path, host, body) if authenticated else {}
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@contextlib.contextmanager
def running(directory, settings, overrides=None):
    config = directory / "config.json"
    config.write_text(json.dumps(settings))
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith("MOON_S3_")}
    environment["MOON_S3_CONFIG"] = str(config)
    environment.update(overrides or {})
    with tempfile.TemporaryFile() as log:
        server = subprocess.Popen([str(BINARY)], env=environment, stdout=log, stderr=log)
        try:
            host = environment.get("MOON_S3_LISTEN", settings["listen"])
            port = int(host.rsplit(":", 1)[1])
            for attempt in range(100):
                if server.poll() is not None:
                    raise AssertionError("Server stopped before becoming healthy")
                try:
                    if request(port, "GET", "/healthz")[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("Server did not become healthy")
            yield server, port, log
        except BaseException:
            log.seek(0)
            print(log.read().decode(errors="replace"))
            raise
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


def records(log):
    log.seek(0)
    result = [json.loads(line) for line in log.read().decode().splitlines()]
    assert all("timestamp_ms" in record and "level" in record for record in result)
    return result


def wait_event(log, event):
    for attempt in range(100):
        if any(record["event"] == event for record in records(log)):
            return
        time.sleep(0.02)
    raise AssertionError(f"Missing event: {event}")


def slow_put(port, path, body=b"done", authenticated=False):
    connection = socket.create_connection(("127.0.0.1", port), timeout=5)
    host = f"127.0.0.1:{port}"
    headers = sign("PUT", path, host, body) if authenticated else {"host": host}
    headers["Content-Length"] = str(len(body))
    head = f"PUT {path} HTTP/1.1\r\n" + "".join(
        f"{name}: {value}\r\n" for name, value in headers.items()) + "\r\n"
    connection.sendall(head.encode() + body[:1])
    return connection


def main():
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with tempfile.TemporaryDirectory(prefix="moon-s3-operations-") as temporary:
        directory = pathlib.Path(temporary)
        root = directory / "data"
        settings = {"root": str(root), "listen": f"127.0.0.1:{free_port()}",
                    "access_key": ACCESS_KEY, "secret_key": SECRET_KEY,
                    "shutdown_timeout_ms": 2500, "cors_origin": "https://file.example"}
        override_port = free_port()
        with running(directory, settings, {"MOON_S3_LISTEN": f"127.0.0.1:{override_port}",
                                           "MOON_S3_CORS_ORIGIN": "https://env.example"}) as (server, port, log):
            assert port == override_port
            assert request(port, "GET", "/metrics")[0] == 403
            status, headers, body = request(port, "GET", "/metrics", authenticated=True)
            assert status == 200 and b"moon_s3_requests_total{status=\"403\"} 1" in body
            assert headers["Access-Control-Allow-Origin"] == "https://env.example"
            assert headers["x-moon-request-id"]
            assert request(port, "HEAD", "/metrics", authenticated=True)[2] == b""
            assert request(port, "POST", "/metrics", authenticated=True)[0] == 405
            assert request(port, "PUT", "/ops-bucket", authenticated=True)[0] == 200
            idle = socket.create_connection(("127.0.0.1", port), timeout=5)
            connection = slow_put(port, "/ops-bucket/private-key?x-id=never-log-this", authenticated=True)
            time.sleep(0.1)
            server.send_signal(signal.SIGTERM)
            wait_event(log, "shutdown_started")
            assert server.poll() is None
            with socket.socket() as rejected:
                assert rejected.connect_ex(("127.0.0.1", port)) != 0
            assert idle.recv(1) == b""
            idle.close()
            connection.sendall(b"oneGET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
            response = http.client.HTTPResponse(connection)
            response.begin()
            assert response.status == 200, response.read()
            response.read()
            try:
                assert connection.recv(1) == b""
            except ConnectionResetError:
                pass
            connection.close()
            assert server.wait(timeout=5) == 0
            entries = records(log)
            assert any(entry["event"] == "shutdown_drained" for entry in entries)
            assert not any(entry["event"] == "shutdown_timeout" for entry in entries)
            serialized = json.dumps(entries)
            for secret in [SECRET_KEY, ACCESS_KEY, "never-log-this", "private-key", "Authorization"]:
                assert secret not in serialized, secret
        with running(directory, settings) as (server, port, log):
            assert request(port, "GET", "/ops-bucket/private-key", authenticated=True)[2] == b"done"
            server.send_signal(signal.SIGINT)
            assert server.wait(timeout=5) == 0
        settings["shutdown_timeout_ms"] = 200
        with running(directory, settings) as (server, port, log):
            connection = slow_put(port, "/ops-bucket/incomplete", authenticated=True)
            time.sleep(0.1)
            started = time.monotonic()
            server.terminate()
            assert server.wait(timeout=3) == 0
            assert time.monotonic() - started < 2
            assert any(entry["event"] == "shutdown_timeout" for entry in records(log))
            connection.close()
        with running(directory, settings) as (server, port, log):
            assert request(port, "GET", "/ops-bucket/incomplete", authenticated=True)[0] == 404
        config = directory / "bad.json"
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith("MOON_S3_")}
        environment["MOON_S3_CONFIG"] = str(config)
        invalid = ['[]', '{"unknown": true}', '{"root": 1}',
                   '{"root": "a", "root": "b"}', '{"shutdown_timeout_ms": 1.5}',
                   '{"shutdown_timeout_ms": "1000"}', '{"shutdown_timeout_ms": 0}',
                   '{"webhook_timeout_ms": 21474836485000}', '{"root": ""}',
                   '{"access_key": "a"}', '{"max_request_bytes": 0}',
                   '{"max_request_bytes": 536870913}', '{"max_connections": 4097}',
                   '{"max_buffered_bytes": 1}', '{"max_buffered_bytes": 16383}', '{"max_staged_bytes": 0}', '{"max_staged_bytes": 536870913}', '{"request_timeout_ms": 99}',
                   '{"request_timeout_ms": 3600001}', ' ' * 65537, '\xff']
        for content in invalid:
            config.write_bytes(content.encode("latin1"))
            result = subprocess.run([str(BINARY)], env=environment, capture_output=True, timeout=5)
            assert result.returncode != 0, content[:50]
            assert json.loads(result.stdout)["event"] == "configuration_invalid", result.stdout
        config.unlink()
        result = subprocess.run([str(BINARY)], env=environment, capture_output=True, timeout=5)
        assert result.returncode != 0 and json.loads(result.stdout)["event"] == "configuration_invalid"
    print("Operations regression passed: config overrides, authenticated metrics, JSON logs, signal drain, deadline and restart.")


if __name__ == "__main__":
    main()
