#!/usr/bin/env python3
import argparse
import http.client
import json
import mimetypes
import os
import pathlib
import subprocess
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET

from presign import presign
from s3_client import BLOCK_SIZE, NS, ClientError, S3Client, object_path


def page_size(value):
    number = int(value)
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("page size must be 1-1000")
    return number


def version_parameters(arguments):
    version = getattr(arguments, "version_id", None)
    return [("versionId", version)] if version is not None else []


def download(client, arguments):
    destination = pathlib.Path(arguments.output)
    if destination.exists() or destination.is_symlink():
        raise ValueError("download destination already exists; choose a new path")
    descriptor, staging = tempfile.mkstemp(prefix=".moon-s3-download-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            metadata, _ = client.request("GET", object_path(arguments.bucket, arguments.key, version_parameters(arguments)), output=output)
            output.flush()
            os.fsync(output.fileno())
        os.link(staging, destination)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return {"path": str(destination), "size": os.stat(staging).st_size,
                "etag": metadata.get("etag"), "version_id": metadata.get("x-amz-version-id")}
    finally:
        os.unlink(staging)


def copy_object(client, source_bucket, source_key, destination_bucket, destination_key, version=None):
    source = object_path(source_bucket, source_key, [("versionId", version)] if version is not None else [])
    metadata, document = client.xml("PUT", object_path(destination_bucket, destination_key), headers={"x-amz-copy-source": source})
    return {"etag": document.findtext("s3:ETag", namespaces=NS),
            "version_id": metadata.get("x-amz-version-id"),
            "source_version_id": metadata.get("x-amz-copy-source-version-id")}


def execute(client, arguments):
    if arguments.command == "health":
        _, body = client.request("GET", "/healthz")
        if body != b"ok\n":
            raise ClientError("Health endpoint returned an unexpected response")
        return {"healthy": True}
    if arguments.command == "bucket":
        if arguments.action == "list":
            _, document = client.xml("GET", "/")
            return {"buckets": [{"name": item.findtext("s3:Name", namespaces=NS),
                                 "created": item.findtext("s3:CreationDate", namespaces=NS)}
                                for item in document.findall("s3:Buckets/s3:Bucket", NS)]}
        client.request("PUT" if arguments.action == "create" else "DELETE", object_path(arguments.bucket))
        return {"bucket": arguments.bucket, "action": arguments.action}
    if arguments.command == "object":
        if arguments.action == "list":
            return {"objects": list(client.list_objects(arguments.bucket, arguments.prefix, arguments.delimiter, arguments.page_size))}
        if arguments.action == "upload":
            part_size = arguments.part_size_mib * BLOCK_SIZE
            if not 5 * BLOCK_SIZE <= part_size <= 512 * BLOCK_SIZE:
                raise ValueError("part size must be 5-512 MiB")
            content_type = arguments.content_type or mimetypes.guess_type(arguments.file)[0] or "application/octet-stream"
            with open(arguments.file, "rb") as source:
                if not source.seekable() or not pathlib.Path(arguments.file).is_file():
                    raise ValueError("upload source must be a regular, seekable file")
                return client.upload(arguments.bucket, arguments.key, source, content_type, part_size)
        if arguments.action == "download":
            return download(client, arguments)
        if arguments.action == "head":
            metadata, _ = client.request("HEAD", object_path(arguments.bucket, arguments.key, version_parameters(arguments)))
            return {"headers": metadata}
        if arguments.action == "delete":
            metadata, _ = client.request("DELETE", object_path(arguments.bucket, arguments.key, version_parameters(arguments)))
            return {"version_id": metadata.get("x-amz-version-id"), "delete_marker": metadata.get("x-amz-delete-marker") == "true"}
        return copy_object(client, arguments.source_bucket, arguments.source_key,
                           arguments.bucket, arguments.key, arguments.version_id)
    if arguments.command == "versions":
        if arguments.action == "list":
            return {"versions": list(client.list_versions(arguments.bucket, arguments.prefix, arguments.page_size))}
        return copy_object(client, arguments.bucket, arguments.key, arguments.bucket, arguments.key, arguments.version_id)
    if arguments.command == "versioning":
        path = object_path(arguments.bucket, parameters=[("versioning", "")])
        if arguments.action == "get":
            _, document = client.xml("GET", path)
            return {"status": document.findtext("s3:Status", default="Unconfigured", namespaces=NS)}
        document = ET.Element("VersioningConfiguration", xmlns=NS["s3"])
        ET.SubElement(document, "Status").text = "Enabled" if arguments.action == "enable" else "Suspended"
        client.request("PUT", path, ET.tostring(document))
        return {"status": document.findtext("Status")}
    if arguments.command == "access":
        path = object_path(arguments.bucket, parameters=[("policy", "")])
        if arguments.action == "get":
            try:
                _, body = client.request("GET", path)
            except ClientError as error:
                if error.code == "NoSuchBucketPolicy":
                    return {"policy": None}
                raise
            return {"policy": json.loads(body)}
        if arguments.action == "private":
            client.request("DELETE", path)
            return {"access": "private"}
        actions = ["s3:GetObject"]
        if arguments.include_versions:
            actions.append("s3:GetObjectVersion")
        policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": "*", "Action": actions,
                  "Resource": f"arn:aws:s3:::{arguments.bucket}/*"}]}
        client.request("PUT", path, json.dumps(policy).encode(), {"content-type": "application/json"})
        return {"access": "public-read", "include_versions": arguments.include_versions}
    if arguments.command == "cors":
        path = object_path(arguments.bucket, parameters=[("cors", "")])
        if arguments.action == "get":
            _, body = client.request("GET", path)
            return {"xml": body.decode("utf-8")}
        if arguments.action == "delete":
            client.request("DELETE", path)
        else:
            with open(arguments.file, "rb") as source:
                body = source.read(65537)
            if len(body) > 65536:
                raise ValueError("CORS configuration exceeds 64 KiB")
            client.request("PUT", path, body, {"content-type": "application/xml"})
        return {"action": arguments.action}
    if arguments.command == "multipart":
        if arguments.action == "abort":
            client.request("DELETE", object_path(arguments.bucket, arguments.key, [("uploadId", arguments.upload_id)]))
            return {"aborted": arguments.upload_id}
        uploads = []
        marker = None
        seen = set()
        while True:
            parameters = [("uploads", ""), ("prefix", arguments.prefix), ("max-uploads", str(arguments.page_size))]
            if marker:
                parameters += [("key-marker", marker[0]), ("upload-id-marker", marker[1])]
            _, page = client.xml("GET", object_path(arguments.bucket, parameters=parameters))
            for item in page.findall("s3:Upload", NS):
                uploads.append({"key": item.findtext("s3:Key", namespaces=NS),
                                "upload_id": item.findtext("s3:UploadId", namespaces=NS),
                                "initiated": item.findtext("s3:Initiated", namespaces=NS)})
            if page.findtext("s3:IsTruncated", namespaces=NS) != "true":
                return {"uploads": uploads}
            marker = (page.findtext("s3:NextKeyMarker", namespaces=NS), page.findtext("s3:NextUploadIdMarker", namespaces=NS))
            if not all(marker) or marker in seen:
                raise ClientError("Server returned missing or repeated upload markers")
            seen.add(marker)
    if arguments.command == "presign":
        return {"url": presign(client.endpoint, arguments.bucket, arguments.key, client.access_key, client.secret_key,
                               arguments.method, arguments.expires, client.region)}
    raise ValueError("unsupported command")


def build_parser():
    parser = argparse.ArgumentParser(description="Manage Moon S3 buckets, objects, versions and offline storage. Outputs JSON.")
    parser.add_argument("--endpoint", default=os.environ.get("MOON_S3_ENDPOINT", "http://127.0.0.1:9000"))
    parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    parser.add_argument("--timeout", type=float, default=30, help="socket timeout in seconds (default: 30)")
    parser.add_argument("--anonymous", action="store_true", help="send requests without configured credentials")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="check the HTTP health endpoint")
    bucket = commands.add_parser("bucket", help="list, create or delete buckets").add_subparsers(dest="action", required=True)
    bucket.add_parser("list")
    for action in ["create", "delete"]:
        bucket.add_parser(action).add_argument("bucket")
    objects = commands.add_parser("object", help="manage object content").add_subparsers(dest="action", required=True)
    listing = objects.add_parser("list")
    listing.add_argument("bucket")
    listing.add_argument("--prefix", default="")
    listing.add_argument("--delimiter", default="")
    listing.add_argument("--page-size", type=page_size, default=1000)
    upload = objects.add_parser("upload")
    upload.add_argument("bucket")
    upload.add_argument("key")
    upload.add_argument("file")
    upload.add_argument("--content-type")
    upload.add_argument("--part-size-mib", type=int, default=8, help="multipart threshold and part size, 5-512 MiB")
    for action in ["download", "head", "delete"]:
        command = objects.add_parser(action)
        command.add_argument("bucket")
        command.add_argument("key")
        command.add_argument("--version-id")
        if action == "download":
            command.add_argument("output")
    copying = objects.add_parser("copy")
    for name in ["source_bucket", "source_key", "bucket", "key"]:
        copying.add_argument(name)
    copying.add_argument("--version-id", help="source version ID")
    versions = commands.add_parser("versions", help="list history or restore a version by copy").add_subparsers(dest="action", required=True)
    listing = versions.add_parser("list")
    listing.add_argument("bucket")
    listing.add_argument("--prefix", default="")
    listing.add_argument("--page-size", type=page_size, default=1000)
    restoring = versions.add_parser("restore")
    for name in ["bucket", "key", "version_id"]:
        restoring.add_argument(name)
    versioning = commands.add_parser("versioning").add_subparsers(dest="action", required=True)
    for action in ["get", "enable", "suspend"]:
        versioning.add_parser(action).add_argument("bucket")
    access = commands.add_parser("access", help="inspect policy or switch private/public reads").add_subparsers(dest="action", required=True)
    for action in ["get", "private", "public"]:
        command = access.add_parser(action)
        command.add_argument("bucket")
        if action == "public":
            command.add_argument("--include-versions", action="store_true", help="also publish historical versions")
    cors = commands.add_parser("cors").add_subparsers(dest="action", required=True)
    for action in ["get", "put", "delete"]:
        command = cors.add_parser(action)
        command.add_argument("bucket")
        if action == "put":
            command.add_argument("file")
    multipart = commands.add_parser("multipart", help="inspect or abort unfinished uploads").add_subparsers(dest="action", required=True)
    listing = multipart.add_parser("list")
    listing.add_argument("bucket")
    listing.add_argument("--prefix", default="")
    listing.add_argument("--page-size", type=page_size, default=1000)
    aborting = multipart.add_parser("abort")
    for name in ["bucket", "key", "upload_id"]:
        aborting.add_argument(name)
    links = commands.add_parser("presign", help="generate a temporary object URL")
    links.add_argument("bucket")
    links.add_argument("key")
    links.add_argument("--method", choices=["GET", "HEAD", "PUT"], default="GET")
    links.add_argument("--expires", type=int, default=900)
    check = commands.add_parser("check", help="offline storage integrity check; stop server first")
    check.add_argument("--root", required=True)
    backup = commands.add_parser("backup", help="offline backup; stop server first")
    backup.add_argument("--root", required=True)
    backup.add_argument("--output", required=True)
    restore = commands.add_parser("restore", help="verify and restore into a new directory")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--root", required=True)
    restore.add_argument("--max-bytes", type=int, default=1024 ** 4)
    return parser


def main():
    arguments = build_parser().parse_args()
    if arguments.command in ("check", "backup", "restore"):
        command = [sys.executable, str(pathlib.Path(__file__).with_name("admin.py")), arguments.command, "--root", arguments.root]
        if arguments.command == "backup":
            command += ["--output", arguments.output]
        if arguments.command == "restore":
            command += ["--archive", arguments.archive, "--max-bytes", str(arguments.max_bytes)]
        return subprocess.run(command).returncode
    try:
        access_key = os.environ.get("MOON_S3_ACCESS_KEY", os.environ.get("AWS_ACCESS_KEY_ID", ""))
        secret_key = os.environ.get("MOON_S3_SECRET_KEY", os.environ.get("AWS_SECRET_ACCESS_KEY", ""))
        if arguments.anonymous:
            access_key = secret_key = ""
        client = S3Client(arguments.endpoint, access_key, secret_key, arguments.region, arguments.timeout)
        result = execute(client, arguments)
        print(json.dumps({"ok": True, **result}, ensure_ascii=True))
        return 0
    except (ClientError, OSError, ValueError, http.client.HTTPException) as error:
        result = {"ok": False, "error": str(error)}
        if isinstance(error, ClientError):
            result.update(status=error.status, code=error.code)
        print(json.dumps(result, ensure_ascii=True), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "Interrupted"}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
