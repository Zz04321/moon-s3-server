#!/usr/bin/env python3
import http.client
import pathlib
import socket
import subprocess
import tempfile
import time
import os
import urllib.parse
import xml.etree.ElementTree as ET


PROJECT = pathlib.Path(__file__).resolve().parents[1]
NAMESPACE = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def main():
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    def request(method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, path, body=body)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def listing(query):
        status, body = request("GET", "/regression-bucket?list-type=2&" + query)
        assert status == 200, (status, body)
        return ET.fromstring(body)

    with tempfile.TemporaryDirectory(prefix="moon-s3-list-") as directory:
        environment = dict(os.environ)
        environment.pop("MOON_S3_ACCESS_KEY", None)
        environment.pop("MOON_S3_SECRET_KEY", None)
        environment.update(MOON_S3_ROOT=directory, MOON_S3_LISTEN=f"127.0.0.1:{port}")
        with tempfile.TemporaryFile() as server_log:
            server = subprocess.Popen(
                [str(PROJECT / "_build/native/debug/build/cmd/main/main.exe")],
                env=environment, stdout=server_log, stderr=server_log,
            )
            try:
                for attempt in range(100):
                    if server.poll() is not None:
                        raise RuntimeError("server exited before becoming healthy")
                    try:
                        if request("GET", "/healthz")[0] == 200:
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                else:
                    raise RuntimeError("server did not become healthy")

                assert request("PUT", "/regression-bucket")[0] == 200
                keys = ["z", "aa", "a+b", "a b", "头像.txt", "\ue000", "\U00010000"]
                for key in keys:
                    path = "/regression-bucket/" + urllib.parse.quote(key, safe="/")
                    assert request("PUT", path, key.encode())[0] == 200
                    assert request("GET", path) == (200, key.encode())

                expected = sorted(keys, key=lambda key: key.encode())
                found = []
                query = "max-keys=2"
                while True:
                    page = listing(query)
                    page_keys = [entry.text for entry in page.findall("s3:Contents/s3:Key", NAMESPACE)]
                    assert int(page.findtext("s3:KeyCount", namespaces=NAMESPACE)) == len(page_keys)
                    found.extend(page_keys)
                    assert len(found) <= len(keys), "pagination did not advance"
                    if page.findtext("s3:IsTruncated", namespaces=NAMESPACE) == "false":
                        break
                    token = page.findtext("s3:NextContinuationToken", namespaces=NAMESPACE)
                    assert token
                    query = "max-keys=2&continuation-token=" + urllib.parse.quote(token)
                assert found == expected, (found, expected)

                for prefix in ["a+", "a%2B"]:
                    page = listing("prefix=" + prefix)
                    assert [entry.text for entry in page.findall("s3:Contents/s3:Key", NAMESPACE)] == ["a+b"]
                for query in ["max-keys=0", "continuation-token=2147483647"]:
                    page = listing(query)
                    assert page.findtext("s3:KeyCount", namespaces=NAMESPACE) == "0"
                    assert page.findtext("s3:IsTruncated", namespaces=NAMESPACE) == "false"
                    assert page.find("s3:NextContinuationToken", NAMESPACE) is None
                page = listing("max-keys=1001")
                assert page.findtext("s3:MaxKeys", namespaces=NAMESPACE) == "1000"
                for parameter in ["max-keys", "continuation-token"]:
                    for value in ["", "-1", "abc", "2147483648", "4294967296", "99999999999999999999"]:
                        status, body = request("GET", f"/regression-bucket?list-type=2&{parameter}={value}")
                        assert status == 400, (parameter, value, status, body)
                        assert ET.fromstring(body).findtext("Code") == "InvalidArgument"
                print("List regression passed: UTF-8 ordering, encoding, pagination and invalid parameters.")
            except BaseException:
                server_log.seek(0)
                print(server_log.read().decode(errors="replace"))
                raise
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    main()
