#!/usr/bin/env python3
import argparse
import configparser
import contextlib
import datetime
import hashlib
import http.client
import json
import os
import pathlib
import ssl
import subprocess
import tempfile
import time
import urllib.request

from auth_regression import ACCESS_KEY, SECRET_KEY, sign
from operations_regression import PROJECT, free_port, running


LOCK = json.loads((PROJECT / "scripts/compatibility-lock.json").read_text())


def run(command, environment=None, cwd=None, timeout=120):
    result = subprocess.run([str(argument) for argument in command], env=environment,
                            cwd=cwd, capture_output=True, timeout=timeout)
    if result.returncode:
        raise AssertionError(f"{command[0]} {command[1:]} failed:\n"
                             + result.stdout.decode(errors="replace")
                             + result.stderr.decode(errors="replace"))
    return result.stdout


def client_environment(directory):
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(("AWS_", "MC_", "S3TEST_"))}
    config = directory / "aws-config"
    config.write_text("[default]\nregion = us-east-1\ns3 =\n    addressing_style = path\n")
    environment.update(AWS_ACCESS_KEY_ID=ACCESS_KEY, AWS_SECRET_ACCESS_KEY=SECRET_KEY,
                       AWS_CONFIG_FILE=str(config), AWS_SHARED_CREDENTIALS_FILE=str(directory / "no-credentials"),
                       AWS_DEFAULT_REGION="us-east-1", AWS_EC2_METADATA_DISABLED="true", AWS_PAGER="", AWS_MAX_ATTEMPTS="1",
                       AWS_REQUEST_CHECKSUM_CALCULATION="when_required",
                       AWS_RESPONSE_CHECKSUM_VALIDATION="when_required", NO_PROXY="*", no_proxy="*",
                       MC_CONFIG_DIR=str(directory / "mc"), MC_NO_COLOR="1", MC_QUIET="1")
    return environment


@contextlib.contextmanager
def tls_proxy(directory, backend_port):
    directory.mkdir()
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
         "-keyout", directory / "privkey.pem", "-out", directory / "fullchain.pem"])
    port = free_port()
    config = (PROJECT / "deploy/nginx.conf").read_text().replace(
        "127.0.0.1:9000", f"127.0.0.1:{backend_port}").replace(
        "127.0.0.1:8443", f"127.0.0.1:{port}")
    (directory / "nginx.conf").write_text(config)
    container = run(["docker", "run", "-d", "--network", "host",
                     "-v", f"{directory / 'nginx.conf'}:/etc/nginx/nginx.conf:ro",
                     "-v", f"{directory}:/etc/nginx/tls:ro", LOCK["nginx_image"]]).decode().strip()
    context = ssl.create_default_context(cafile=str(directory / "fullchain.pem"))
    try:
        for attempt in range(100):
            try:
                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=2)
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                assert response.status == 200
                response.read()
                connection.close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("TLS proxy did not start")
        yield port, context, directory / "fullchain.pem"
    except BaseException:
        print(run(["docker", "logs", container]).decode(errors="replace"))
        raise
    finally:
        run(["docker", "rm", "-f", container])


def aws_checks(binary, directory, endpoint, environment):
    assert f"aws-cli/{LOCK['aws_cli']['version']} " in run([binary, "--version"]).decode()
    aws = [binary, "--endpoint-url", endpoint, "--no-cli-pager"]
    source = directory / "source.bin"
    source.write_bytes(bytes(range(256)) * 8193)
    key = "folder/space + 中文.bin"
    run(aws + ["s3api", "create-bucket", "--bucket", "aws-compat"], environment)
    run(aws + ["s3", "cp", source, f"s3://aws-compat/{key}"], environment)
    listing = json.loads(run(aws + ["s3api", "list-objects-v2", "--bucket", "aws-compat"], environment))
    assert [item["Key"] for item in listing["Contents"]] == [key]
    run(aws + ["s3", "cp", f"s3://aws-compat/{key}", directory / "download.bin"], environment)
    assert (directory / "download.bin").read_bytes() == source.read_bytes()
    run(aws + ["s3api", "copy-object", "--bucket", "aws-compat", "--key", "copy.bin",
               "--copy-source", "aws-compat/" + key], environment)
    url = run(aws + ["s3", "presign", f"s3://aws-compat/{key}", "--expires-in", "60"], environment).decode().strip()
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=10) as response:
        assert response.read() == source.read_bytes()
    upload = json.loads(run(aws + ["s3api", "create-multipart-upload", "--bucket", "aws-compat", "--key", "multipart.bin"], environment))
    options = ["--bucket", "aws-compat", "--key", "multipart.bin", "--upload-id", upload["UploadId"]]
    part = json.loads(run(aws + ["s3api", "upload-part"] + options + ["--part-number", "1", "--body", source], environment))
    run(aws + ["s3api", "complete-multipart-upload"] + options + ["--multipart-upload", json.dumps({"Parts": [{"ETag": part["ETag"], "PartNumber": 1}]})], environment)
    run(aws + ["s3api", "get-object", "--bucket", "aws-compat", "--key", "multipart.bin", directory / "multipart.bin"], environment)
    assert (directory / "multipart.bin").read_bytes() == source.read_bytes()
    run(aws + ["s3api", "delete-objects", "--bucket", "aws-compat", "--delete",
               json.dumps({"Objects": [{"Key": key}, {"Key": "copy.bin"}, {"Key": "multipart.bin"}]})], environment)
    listing = json.loads(run(aws + ["s3api", "list-objects-v2", "--bucket", "aws-compat"], environment))
    assert not listing.get("Contents")
    run(aws + ["s3api", "delete-bucket", "--bucket", "aws-compat"], environment)


def mc_checks(binary, directory, endpoint, environment):
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == LOCK["mc"]["sha256"]
    assert LOCK["mc"]["version"] in run([binary, "--version"], environment).decode()
    mc = [binary, "--insecure", "--json"]
    run(mc + ["alias", "set", "moon", endpoint, ACCESS_KEY, SECRET_KEY, "--api", "S3v4", "--path", "on"], environment)
    run(mc + ["mb", "moon/mc-compat"], environment)
    source = directory / "mc-source.bin"
    source.write_bytes(bytes(range(256)) * 4097)
    key = "folder/space + 中文.bin"
    target = f"moon/mc-compat/{key}"
    run(mc + ["cp", "--disable-multipart", source, target], environment)
    assert run(mc + ["cat", target], environment) == source.read_bytes()
    run(mc + ["cp", target, "moon/mc-compat/copy.bin"], environment)
    assert run(mc + ["cat", "moon/mc-compat/copy.bin"], environment) == source.read_bytes()
    listing = run(mc + ["ls", "--recursive", "moon/mc-compat"], environment).decode()
    assert key in listing and "copy.bin" in listing
    run(mc + ["stat", target], environment)
    run(mc + ["cp", target, directory / "mc-download.bin"], environment)
    assert (directory / "mc-download.bin").read_bytes() == source.read_bytes()
    run(mc + ["rm", target], environment)
    run(mc + ["rm", "moon/mc-compat/copy.bin"], environment)
    run(mc + ["rb", "moon/mc-compat"], environment)


def proxy_checks(port, context):
    def request(method, path, body=b"", extra=None):
        headers = sign(method, path, f"127.0.0.1:{port}", body, extra_headers=extra)
        connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=10)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    assert request("PUT", "/proxy-compat")[0] == 200
    path = "/proxy-compat/a%2Fb//c%20%2B%E4%B8%AD"
    assert request("PUT", path, b"proxy-body")[0] == 200
    assert request("GET", path) == (200, b"proxy-body")
    assert request("GET", path, extra={"Range": "bytes=1-3"}) == (206, b"rox")
    from presign import presign
    url = presign(f"https://127.0.0.1:{port}", "proxy-compat", "a/b//c +中", ACCESS_KEY, SECRET_KEY, expires=60)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    with opener.open(url, timeout=10) as response:
        assert response.read() == b"proxy-body"
    for target, headers, expected in [
        ("/healthz", {"X-Oversized": "x" * 5000}, 400),
        ("/healthz", {f"X-Many-{index}": "x" * 200 for index in range(60)}, 400),
        ("/" + "x" * 5000, {}, 414),
        ("/proxy-compat/too-big", {"Content-Length": "67108865"}, 413),
    ]:
        connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=10)
        try:
            connection.request("PUT", target, headers=headers)
            response = connection.getresponse()
            assert response.status == expected, (response.status, response.read())
            response.read()
        finally:
            connection.close()
    assert request("GET", "/healthz")[0] == 200


def ceph_checks(python, repository, directory, port, environment):
    assert run(["git", "rev-parse", "HEAD"], cwd=repository).decode().strip() == LOCK["ceph"]["commit"]
    assert not run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repository).strip()
    versions = json.loads(run([python, "-c", "import boto3, botocore, json; print(json.dumps([boto3.__version__, botocore.__version__]))"]))
    assert versions == [LOCK["boto3"], LOCK["botocore"]]
    config = configparser.ConfigParser()
    config["DEFAULT"] = dict(host="127.0.0.1", port=str(port), is_secure="False", ssl_verify="False")
    for section in ["s3 main", "s3 alt", "s3 tenant", "iam", "iam root", "iam alt root"]:
        config[section] = dict(access_key=ACCESS_KEY, secret_key=SECRET_KEY, display_name="moonbit",
                               user_id="moonbit", email="moonbit@example.test", tenant="test", api_name="")
    config["fixtures"] = {"bucket prefix": "compat-{random}-"}
    path = directory / "ceph.conf"
    with path.open("w") as stream:
        config.write(stream)
    environment = dict(environment, S3TEST_CONF=str(path), PYTHONDONTWRITEBYTECODE="1")
    cases = [LOCK["ceph"]["module"] + "::" + case for case in LOCK["ceph"]["cases"]]
    output = run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml", directory / "ceph.xml"] + cases,
                 environment, repository, timeout=600)
    print(output.decode())
    import xml.etree.ElementTree as ET
    results = ET.parse(directory / "ceph.xml").getroot().findall(".//testcase")
    assert len(results) == len(cases)
    assert all(not any(result.find(tag) is not None for tag in ["failure", "error", "skipped"]) for result in results)


def main():
    parser = argparse.ArgumentParser(description="Fixed-version compatibility acceptance on disposable local storage (Linux x86-64).")
    parser.add_argument("--tools", type=pathlib.Path, default=pathlib.Path("/tmp/moon-s3-compat"))
    parser.add_argument("--s3-tests", type=pathlib.Path, default=pathlib.Path("/tmp/moon-s3-s3-tests"))
    parser.add_argument("--suite", choices=["all", "aws", "mc", "ceph", "proxy"], default="all")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    args.tools = args.tools.resolve()
    args.s3_tests = args.s3_tests.resolve()
    subprocess.run(["moon", "build", "cmd/main", "--target", "native"], cwd=PROJECT, check=True)
    results = {}
    suites = ["aws", "mc", "ceph", "proxy"] if args.suite == "all" else [args.suite]
    try:
        for suite in suites:
            started = time.monotonic()
            with tempfile.TemporaryDirectory(prefix=f"moon-s3-compat-{suite}-") as temporary:
                directory = pathlib.Path(temporary)
                settings = dict(root=str(directory / "data"), listen=f"127.0.0.1:{free_port()}",
                                access_key=ACCESS_KEY, secret_key=SECRET_KEY)
                environment = client_environment(directory)
                with running(directory, settings) as (_, port, _):
                    if suite == "aws":
                        aws_checks(args.tools / "aws/dist/aws", directory, f"http://127.0.0.1:{port}", environment)
                    elif suite == "ceph":
                        ceph_checks(args.tools / "python/bin/python", args.s3_tests, directory, port, environment)
                    else:
                        with tls_proxy(directory / "tls", port) as (tls_port, context, _):
                            if suite == "mc":
                                mc_checks(args.tools / "mc", directory, f"https://127.0.0.1:{tls_port}", environment)
                            else:
                                proxy_checks(tls_port, context)
            results[suite] = dict(status="passed", seconds=round(time.monotonic() - started, 2))
            print(f"{suite}: passed", flush=True)
    except BaseException as error:
        results[suite] = dict(status="failed", error=str(error))
        raise
    finally:
        if args.report:
            args.report.write_text(json.dumps(dict(timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                                  revision=run(["git", "rev-parse", "HEAD"], cwd=PROJECT).decode().strip(),
                                                  tracked_changes=bool(run(["git", "status", "--porcelain"], cwd=PROJECT).strip()),
                                                  versions=LOCK, results=results), indent=2) + "\n")


if __name__ == "__main__":
    main()
