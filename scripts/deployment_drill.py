#!/usr/bin/env python3
import argparse
import contextlib
import datetime
import hashlib
import json
import pathlib
import platform
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

from release_bundle import extract_verified
from s3_client import ClientError, NS, S3Client, object_path


BUCKET = "drill-bucket"
KEY = "目录/space + " + "键" * 70 + ".bin"


def docker(*arguments, timeout=120, check=True):
    result = subprocess.run(["docker", *map(str, arguments)], capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace"))
    return result


def output(*arguments, **options):
    return docker(*arguments, **options).stdout.decode().strip()


def expect_error(client, method, path, status):
    try:
        client.request(method, path)
    except ClientError as error:
        assert error.status == status, (error.status, status)
    else:
        raise AssertionError(f"Expected HTTP {status}")


def populate(client):
    client.request("PUT", object_path(BUCKET))
    client.request("PUT", object_path(BUCKET, parameters=[("versioning", "")]),
                   b"<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>")
    bodies = [bytes(range(256)) * 4097, b"second version\x00\xff"]
    versions = []
    for body in bodies:
        metadata, _ = client.request("PUT", object_path(BUCKET, KEY), body, {"Content-Type": "application/octet-stream"})
        versions.append(metadata["x-amz-version-id"])
    client.request("PUT", object_path(BUCKET, "marked"), b"retained under marker")
    metadata, _ = client.request("DELETE", object_path(BUCKET, "marked"))
    marker = metadata["x-amz-version-id"]
    cors = b'<CORSConfiguration><CORSRule><AllowedOrigin>https://drill.example.test</AllowedOrigin><AllowedMethod>GET</AllowedMethod></CORSRule></CORSConfiguration>'
    client.request("PUT", object_path(BUCKET, parameters=[("cors", "")]), cors)
    policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": "*",
              "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{BUCKET}/*"}]}
    client.request("PUT", object_path(BUCKET, parameters=[("policy", "")]), json.dumps(policy).encode())
    _, upload = client.xml("POST", object_path(BUCKET, "multipart", [("uploads", "")]))
    upload_id = upload.findtext("s3:UploadId", namespaces=NS)
    part = b"a" * (5 * 1024 * 1024)
    metadata, _ = client.request("PUT", object_path(BUCKET, "multipart", [("uploadId", upload_id), ("partNumber", "1")]), part)
    return dict(bodies=bodies, versions=versions, marker=marker, upload_id=upload_id,
                part=part, etag=metadata["etag"])


def verify_snapshot(client, expected):
    for body, version in zip(expected["bodies"], expected["versions"]):
        metadata, actual = client.request("GET", object_path(BUCKET, KEY, [("versionId", version)]))
        assert hashlib.sha256(actual).digest() == hashlib.sha256(body).digest()
        assert metadata["content-type"] == "application/octet-stream"
    assert client.request("GET", object_path(BUCKET, KEY))[1] == expected["bodies"][-1]
    expect_error(client, "GET", object_path(BUCKET, "marked"), 404)
    _, listing = client.xml("GET", object_path(BUCKET, parameters=[("versions", "")]))
    assert expected["marker"] in [item.findtext("s3:VersionId", namespaces=NS) for item in listing.findall("s3:DeleteMarker", NS)]
    _, versioning = client.xml("GET", object_path(BUCKET, parameters=[("versioning", "")]))
    assert versioning.findtext("s3:Status", namespaces=NS) == "Enabled"
    _, cors = client.xml("GET", object_path(BUCKET, parameters=[("cors", "")]))
    assert cors.findtext("s3:CORSRule/s3:AllowedOrigin", namespaces=NS) == "https://drill.example.test"
    _, policy = client.request("GET", object_path(BUCKET, parameters=[("policy", "")]))
    assert json.loads(policy)["Statement"]
    anonymous = S3Client(client.endpoint)
    assert anonymous.request("GET", object_path(BUCKET, KEY))[1] == expected["bodies"][-1]
    expect_error(anonymous, "GET", object_path(BUCKET, parameters=[("list-type", "2")] ), 403)
    expect_error(anonymous, "GET", "/metrics", 403)
    _, parts = client.xml("GET", object_path(BUCKET, "multipart", [("uploadId", expected["upload_id"])]))
    assert parts.findtext("s3:Part/s3:ETag", namespaces=NS) == expected["etag"]


def resume_upload(client, expected):
    parameters = [("uploadId", expected["upload_id"])]
    tail = b"resumed after restore"
    metadata, _ = client.request("PUT", object_path(BUCKET, "multipart", parameters + [("partNumber", "2")]), tail)
    document = ET.Element("CompleteMultipartUpload")
    for number, etag in [(1, expected["etag"]), (2, metadata["etag"])]:
        part = ET.SubElement(document, "Part")
        ET.SubElement(part, "PartNumber").text = str(number)
        ET.SubElement(part, "ETag").text = etag
    client.request("POST", object_path(BUCKET, "multipart", parameters), ET.tostring(document))
    body = client.request("GET", object_path(BUCKET, "multipart"))[1]
    assert hashlib.sha256(body).hexdigest() == hashlib.sha256(expected["part"] + tail).hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Exercise a verified release in disposable Docker volumes: backup, restore, resume and rollback.")
    parser.add_argument("archive", type=pathlib.Path)
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--admin-image", default="python:3.11.9-slim-bookworm")
    parser.add_argument("--allow-dirty", action="store_true", help="Local development bundles only")
    args = parser.parse_args()
    args.archive = args.archive.resolve()
    args.report = args.report.absolute()
    if args.report.exists():
        parser.error("report path must be new")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = dict(schema="moon-s3-deployment-drill-v1", status="running", started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  host=dict(system=platform.system(), machine=platform.machine(), kernel=platform.release()), steps=[])
    report["harness_sha256"] = {name: hashlib.sha256(pathlib.Path(__file__).with_name(name).read_bytes()).hexdigest()
                                for name in ["deployment_drill.py", "release_bundle.py", "s3_client.py"]}
    prefix = "moon-s3-drill-" + uuid.uuid4().hex[:12]
    volumes, containers, images = [], [], []

    @contextlib.contextmanager
    def step(name):
        record = dict(name=name, status="running")
        report["steps"].append(record)
        started = time.monotonic()
        try:
            yield
            record["status"] = "passed"
            print(f"{name}: passed", flush=True)
        except BaseException:
            record["status"] = "failed"
            raise
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)

    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
            directory = pathlib.Path(temporary)
            with step("verify_release"):
                bundle, manifest = extract_verified(args.archive, directory, args.allow_dirty)
                report["release"] = {name: manifest[name] for name in ["version", "revision", "dirty", "target", "toolchain"]}
                report["release"]["archive_sha256"] = hashlib.sha256(args.archive.read_bytes()).hexdigest()
                report["docker"] = json.loads(output("version", "--format", "{{json .Server}}"))
            with step("build_runtime_image"):
                image = prefix + ":runtime"
                images.append(image)
                docker("build", "-t", image, bundle, timeout=300)
                report["runtime_image_id"] = output("image", "inspect", image, "--format", "{{.Id}}")
                if docker("image", "inspect", args.admin_image, check=False).returncode:
                    docker("pull", args.admin_image, timeout=300)
                report["admin_image_id"] = output("image", "inspect", args.admin_image, "--format", "{{.Id}}")
            for suffix in ["source", "backup", "restored"]:
                volume = prefix + "-" + suffix
                output("volume", "create", volume)
                volumes.append(volume)
            source, backup, restored = volumes
            access, secret = "drill-access", uuid.uuid4().hex + uuid.uuid4().hex

            def start(volume, root="/data"):
                name = prefix + "-" + str(len(containers))
                containers.append(name)
                output("run", "-d", "--name", name, "--stop-timeout", "15", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges:true", "-p", "127.0.0.1::9000",
                       "--mount", f"type=volume,src={volume},dst=/data", "-e", "MOON_S3_ROOT=" + root,
                       "-e", "MOON_S3_ACCESS_KEY=" + access, "-e", "MOON_S3_SECRET_KEY=" + secret, image)
                port = int(output("port", name, "9000/tcp").rsplit(":", 1)[1])
                client = S3Client(f"http://127.0.0.1:{port}", access, secret)
                for attempt in range(100):
                    assert json.loads(output("inspect", name, "--format", "{{json .State.Running}}"))
                    try:
                        client.request("GET", "/healthz")
                        assert output("inspect", name, "--format", "{{.Config.User}}") == "10001:10001"
                        return name, client
                    except (OSError, ClientError):
                        time.sleep(0.1)
                raise AssertionError("Deployed server never became healthy")

            def stop(name):
                output("stop", name, timeout=30)
                assert output("inspect", name, "--format", "{{.State.ExitCode}}") == "0"
                logs = [json.loads(line) for line in output("logs", name).splitlines()]
                assert any(item.get("event") == "shutdown_drained" for item in logs)

            def admin(volume, *arguments, check=True):
                return docker("run", "--rm", "--user", "10001:10001", "--cap-drop", "ALL",
                              "--security-opt", "no-new-privileges:true", "--mount", f"type=bind,src={bundle / 'scripts'},dst=/tools,readonly",
                              "--mount", f"type=volume,src={volume},dst=/data", "--mount", f"type=volume,src={backup},dst=/backup",
                              args.admin_image, "python", "/tools/admin.py", *arguments, check=check)

            def initialize(volume):
                output("run", "--rm", "--mount", f"type=volume,src={volume},dst=/work", args.admin_image,
                       "python", "-c", "import os; os.chown('/work', 10001, 10001)")

            with step("deploy_and_populate"):
                name, client = start(source)
                expected = populate(client)
                verify_snapshot(client, expected)
            with step("reject_online_backup"):
                initialize(backup)
                result = admin(source, "backup", "--root", "/data", "--output", "/backup/online.tar", check=False)
                assert result.returncode != 0
                assert "in use" in json.loads(result.stderr)["error"], result.stderr.decode()
            with step("stop_and_offline_backup"):
                stop(name)
                initialize(backup)
                report["source_inventory"] = json.loads(admin(source, "check", "--root", "/data").stdout)
                admin(source, "backup", "--root", "/data", "--output", "/backup/snapshot.tar")
                digest = output("run", "--rm", "--mount", f"type=volume,src={backup},dst=/backup,readonly",
                                args.admin_image, "python", "-c", "import hashlib; print(hashlib.sha256(open('/backup/snapshot.tar','rb').read()).hexdigest())")
                report["backup_sha256"] = digest
            with step("write_after_snapshot"):
                name, client = start(source)
                client.request("PUT", object_path(BUCKET, "after-backup"), b"outside recovery point")
                stop(name)
            recovery_started = time.monotonic()
            with step("restore_into_new_volume"):
                initialize(restored)
                admin(restored, "restore", "--archive", "/backup/snapshot.tar", "--root", "/data/restored")
                report["restored_inventory"] = json.loads(admin(restored, "check", "--root", "/data/restored").stdout)
                assert report["source_inventory"] == report["restored_inventory"]
            with step("reject_overwrite_and_corrupt_backup"):
                assert admin(restored, "restore", "--archive", "/backup/snapshot.tar", "--root", "/data/restored", check=False).returncode != 0
                output("run", "--rm", "--user", "10001:10001", "--mount", f"type=volume,src={backup},dst=/backup", args.admin_image,
                       "python", "-c", "import tarfile; p='/backup/snapshot.tar'; t=tarfile.open(p); m=next(x for x in t if x.isfile() and x.size>1024); f=open('/backup/corrupt.tar','wb'); f.write(open(p,'rb').read()); f.close(); f=open('/backup/corrupt.tar','r+b'); f.seek(m.offset_data); b=f.read(1); f.seek(m.offset_data); f.write(bytes([b[0]^1])); f.close()")
                assert admin(restored, "restore", "--archive", "/backup/corrupt.tar", "--root", "/data/rejected", check=False).returncode != 0
                output("run", "--rm", "--mount", f"type=volume,src={restored},dst=/data", args.admin_image,
                       "python", "-c", "import os; assert not os.path.exists('/data/rejected')")
            with step("validate_restored_service"):
                name, client = start(restored, "/data/restored")
                verify_snapshot(client, expected)
                expect_error(client, "GET", object_path(BUCKET, "after-backup"), 404)
                report["recovery_to_verified_seconds"] = round(time.monotonic() - recovery_started, 3)
            with step("resume_multipart_and_write"):
                resume_upload(client, expected)
                client.request("PUT", object_path(BUCKET, "restored-only"), b"new restored service write")
                stop(name)
            with step("rollback_to_original_volume"):
                name, client = start(source)
                verify_snapshot(client, expected)
                assert client.request("GET", object_path(BUCKET, "after-backup"))[1] == b"outside recovery point"
                expect_error(client, "GET", object_path(BUCKET, "restored-only"), 404)
                stop(name)
            report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        cleanup = []
        commands = [("container", name, ["rm", "-f", name]) for name in reversed(containers)]
        commands += [("volume", name, ["volume", "rm", name]) for name in reversed(volumes)]
        commands += [("image", name, ["image", "rm", name]) for name in images]
        for kind, name, command in commands:
            try:
                if docker(*command, check=False).returncode:
                    cleanup.append(kind + ": " + name)
            except (OSError, subprocess.TimeoutExpired):
                cleanup.append(kind + ": " + name)
        report["cleanup_errors"] = cleanup
        if cleanup:
            report["status"] = "failed"
        report["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        if cleanup:
            raise RuntimeError("Drill cleanup incomplete; inspect the report")
    print(f"Deployment drill passed; report: {args.report}")


if __name__ == "__main__":
    main()
