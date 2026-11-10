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
                extra_keys = ["docs/", "docs/a.txt", "docs/sub/b.txt", "docs/sub/c.txt", "photos/a.jpg", "组/头像.png"]
                for key in extra_keys:
                    assert request("PUT", "/regression-bucket/" + urllib.parse.quote(key, safe="/"), b"data")[0] == 200
                page = listing("prefix=docs%2F&delimiter=%2F")
                assert page.findtext("s3:Delimiter", namespaces=NAMESPACE) == "/"
                assert page.findtext("s3:KeyCount", namespaces=NAMESPACE) == "3"
                assert [entry.text for entry in page.findall("s3:Contents/s3:Key", NAMESPACE)] == ["docs/", "docs/a.txt"]
                assert [entry.text for entry in page.findall("s3:CommonPrefixes/s3:Prefix", NAMESPACE)] == ["docs/sub/"]
                page = listing("prefix=docs%2F&delimiter=%2F&start-after=docs%2Fsub%2F")
                assert page.findtext("s3:KeyCount", namespaces=NAMESPACE) == "0"
                page = listing("delimiter=%2F&start-after=docs%2F&max-keys=1000")
                assert "docs/" not in [entry.text for entry in page.findall("s3:CommonPrefixes/s3:Prefix", NAMESPACE)]

                found = []
                token = None
                base_query = "delimiter=%2F&max-keys=1&start-after=aa"
                for attempt in range(len(keys) + len(extra_keys) + 1):
                    query = base_query + ("&continuation-token=" + token if token else "")
                    page = listing(query)
                    if token:
                        assert page.findtext("s3:ContinuationToken", namespaces=NAMESPACE) == token
                    assert page.findtext("s3:StartAfter", namespaces=NAMESPACE) == "aa"
                    items = [(entry.text, False) for entry in page.findall("s3:Contents/s3:Key", NAMESPACE)]
                    items += [(entry.text, True) for entry in page.findall("s3:CommonPrefixes/s3:Prefix", NAMESPACE)]
                    assert len(items) == 1
                    assert page.findtext("s3:KeyCount", namespaces=NAMESPACE) == "1"
                    found.extend(items)
                    if page.findtext("s3:IsTruncated", namespaces=NAMESPACE) == "false":
                        break
                    token = page.findtext("s3:NextContinuationToken", namespaces=NAMESPACE)
                    assert token
                else:
                    raise AssertionError("grouped pagination did not finish")
                expected_entries = {(key.split("/")[0] + "/", True) if "/" in key else (key, False) for key in keys + extra_keys}
                expected_entries = sorted((entry for entry in expected_entries if entry[0].encode() > b"aa"), key=lambda entry: entry[0].encode())
                assert found == expected_entries, (found, expected_entries)
                encoded_keys = ["编码/空 格+&.txt", "encoded/control\x01.txt", "encoded/percent%25.txt"]
                for key in encoded_keys:
                    path = "/regression-bucket/" + urllib.parse.quote(key, safe="/")
                    assert request("PUT", path, b"data")[0] == 200
                page = listing("encoding-type=url")
                assert page.findtext("s3:EncodingType", namespaces=NAMESPACE) == "url"
                encoded_names = [entry.text for entry in page.findall("s3:Contents/s3:Key", NAMESPACE)]
                assert [urllib.parse.unquote(key) for key in encoded_names] == sorted(keys + extra_keys + encoded_keys, key=lambda key: key.encode())
                assert "encoded/control%01.txt" in encoded_names
                page = listing("encoding-type=url&prefix=" + urllib.parse.quote("编码/", safe=""))
                assert page.findtext("s3:Prefix", namespaces=NAMESPACE) == "%E7%BC%96%E7%A0%81/"
                assert page.findtext("s3:Contents/s3:Key", namespaces=NAMESPACE) == "%E7%BC%96%E7%A0%81/%E7%A9%BA%20%E6%A0%BC%2B%26.txt"
                page = listing("encoding-type=url&delimiter=%2F&start-after=" + urllib.parse.quote("头像.txt", safe=""))
                assert page.findtext("s3:StartAfter", namespaces=NAMESPACE) == urllib.parse.quote("头像.txt", safe="/")
                assert page.findtext("s3:Delimiter", namespaces=NAMESPACE) == "/"
                assert "%E7%BC%96%E7%A0%81/" in [entry.text for entry in page.findall("s3:CommonPrefixes/s3:Prefix", NAMESPACE)]
                for value in ["", "xml", "URL"]:
                    status, body = request("GET", "/regression-bucket?list-type=2&encoding-type=" + value)
                    assert status == 400
                    assert ET.fromstring(body).findtext("Code") == "InvalidArgument"
                print("List regression passed: pagination, directories, start-after and URL-encoded control characters.")
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
