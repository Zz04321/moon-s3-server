#!/usr/bin/env python3
import base64
import hashlib
import http.client
import pathlib
import subprocess
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from auth_regression import ACCESS_KEY, SECRET_KEY, sign
from operations_regression import PROJECT, free_port, running


def request(port, method, path, body=b"", headers=None, authenticated=True):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = sign(method, path, f"127.0.0.1:{port}", body, extra_headers=headers) if authenticated else {}
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def document(objects, quiet=False):
    return ("<Delete>" + "".join("<Object><Key>" + escape(key) + "</Key>" +
            ("<VersionId>" + escape(version) + "</VersionId>" if version is not None else "") +
            "</Object>" for key, version in objects) +
            ("<Quiet>true</Quiet>" if quiet else "") + "</Delete>").encode()


def batch(port, objects, quiet=False):
    body = document(objects, quiet)
    status, result = request(port, "POST", "/delete-compat?delete", body,
                             {"Content-MD5": base64.b64encode(hashlib.md5(body).digest()).decode()})
    assert status == 200, (status, result)
    root = ET.fromstring(result)
    return [{child.tag.rsplit("}", 1)[-1]: child.text for child in item} for item in root]


def main():
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    with tempfile.TemporaryDirectory(prefix="moon-s3-delete-") as temporary:
        directory = pathlib.Path(temporary)
        settings = dict(root=str(directory / "data"), listen=f"127.0.0.1:{free_port()}",
                        access_key=ACCESS_KEY, secret_key=SECRET_KEY)
        with running(directory, settings) as (_, port, _):
            assert request(port, "PUT", "/delete-compat")[0] == 200
            assert request(port, "GET", "/delete-compat?location")[0] == 200
            assert request(port, "GET", "/missing-bucket?location")[0] == 404
            assert request(port, "POST", "/missing-bucket?delete", document([("a", None)]))[0] == 404
            for path in ["/?location", "/?delete", "/delete-compat/a?location", "/delete-compat/a?delete",
                         "/delete-compat?location&versioning", "/delete-compat?delete&uploads",
                         "/delete-compat?delete&delete", "/delete-compat?delete=wrong"]:
                assert request(port, "POST", path, document([("a", None)]))[0] == 400, path
            assert request(port, "GET", "/delete-compat?delete")[0] == 400
            assert request(port, "POST", "/delete-compat?delete", document([("a", None)]), authenticated=False)[0] == 403
            assert request(port, "PUT", "/delete-compat/keep", b"keep")[0] == 200
            assert request(port, "POST", "/delete-compat?delete", document([("keep", None)]), {"If-Match": "*"})[0] == 501
            malformed = [b"<Delete/>", b"\xff", document([("keep", None), ("", None)]),
                         document([("keep", None), ("x", "../bad")]), document([("keep", None)] * 1001),
                         b"<Delete><Object><Key>keep</Key></Object><Unknown/></Delete>"]
            for body in malformed:
                assert request(port, "POST", "/delete-compat?delete", body)[0] == 400
                assert request(port, "GET", "/delete-compat/keep")[1] == b"keep"
            body = document([("keep", None)])
            assert request(port, "POST", "/delete-compat?delete", body,
                           {"Content-MD5": base64.b64encode(b"0" * 16).decode()})[0] == 400
            assert request(port, "GET", "/delete-compat/keep")[1] == b"keep"
            for query in ["list-type=3", "list-type=2&fetch-owner=invalid"]:
                assert request(port, "GET", "/delete-compat?" + query)[0] == 400
            result = batch(port, [("keep", None), ("missing", None)])
            assert result == [{"Key": "keep"}, {"Key": "missing"}]
            assert request(port, "GET", "/delete-compat/keep")[0] == 404
            assert batch(port, [("quiet", None)], quiet=True) == []
            assert request(port, "PUT", "/delete-compat/broken", b"fail")[0] == 200
            metadata = directory / "data/delete-compat/objects" / (b"broken".hex() + ".meta")
            metadata.unlink()
            metadata.mkdir()
            obstruction = metadata / "blocked"
            obstruction.write_bytes(b"failure injection")
            assert request(port, "PUT", "/delete-compat/healthy", b"ok")[0] == 200
            result = batch(port, [("broken", None), ("healthy", None)])
            assert result[0]["Key"] == "broken" and result[0]["Code"] == "InternalError"
            assert result[1] == {"Key": "healthy"}
            assert request(port, "GET", "/delete-compat/healthy")[0] == 404
            result = batch(port, [("missing", None), ("broken", None)], quiet=True)
            assert len(result) == 1 and result[0]["Code"] == "InternalError"
            obstruction.unlink()
            metadata.rmdir()
            assert request(port, "GET", "/delete-compat/broken")[0] == 404
            long_key = "键" * 100 + "<&+ "
            path = "/delete-compat/" + urllib.parse.quote(long_key, safe="")
            assert request(port, "PUT", path, b"long")[0] == 200
            assert batch(port, [(long_key, None)]) == [{"Key": long_key}]
            assert request(port, "GET", path)[0] == 404
            versioning = b'<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>'
            assert request(port, "PUT", "/delete-compat?versioning", versioning)[0] == 200
            assert request(port, "PUT", "/delete-compat/versioned", b"v1")[0] == 200
            result = batch(port, [("versioned", None)])
            marker = result[0]["DeleteMarkerVersionId"]
            assert result[0]["DeleteMarker"] == "true"
            assert request(port, "GET", "/delete-compat/versioned")[0] == 404
            assert batch(port, [("versioned", marker)]) == [dict(Key="versioned", VersionId=marker, DeleteMarker="true", DeleteMarkerVersionId=marker)]
            assert request(port, "GET", "/delete-compat/versioned")[1] == b"v1"
            listing = ET.fromstring(request(port, "GET", "/delete-compat?versions")[1])
            versions = [(item.findtext("{*}Key"), item.findtext("{*}VersionId")) for item in listing.findall("{*}Version")]
            assert versions
            assert batch(port, versions, quiet=True) == []
        with running(directory, settings) as (_, port, _):
            assert request(port, "GET", "/delete-compat/versioned")[0] == 404
            assert request(port, "GET", path)[0] == 404
            assert request(port, "DELETE", "/delete-compat")[0] == 204
    print("batch deletion regression: passed")


if __name__ == "__main__":
    main()
