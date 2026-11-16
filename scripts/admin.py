#!/usr/bin/env python3
import argparse
import contextlib
import ctypes
import datetime
import errno
import fcntl
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET


FORMAT = "moon-s3-backup-v1"
MANIFEST_LIMIT = 16 * 1024 * 1024
BLOCK_SIZE = 1024 * 1024


class AdminError(Exception):
    pass


class DigestReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size):
        block = self.source.read(size)
        self.digest.update(block)
        return block


def require(condition, message):
    if not condition:
        raise AdminError(message)


def safe_path(name):
    require(isinstance(name, str) and name and "\\" not in name and "\x00" not in name,
            "Invalid backup path")
    parts = name.split("/")
    require(all(part not in ("", ".", "..") for part in parts), f"Unsafe backup path: {name}")
    return pathlib.PurePosixPath(name)


def valid_key(encoded):
    if not re.fullmatch(r"(?:[0-9a-f]{2})+", encoded):
        return False
    try:
        return bytes.fromhex(encoded).decode("utf-8").encode("utf-8").hex() == encoded
    except UnicodeError:
        return False


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def locked_root(path):
    root = pathlib.Path(path).resolve(strict=True)
    require(root.is_dir(), "Data root must be a directory")
    descriptor = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        require(stat.S_ISREG(os.fstat(descriptor).st_mode), "Data lock must be a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AdminError("Data directory is in use; stop the server before administration") from None
        yield root
    finally:
        os.close(descriptor)


def scan(root):
    entries = {}

    def visit(directory):
        for path in sorted(directory.iterdir()):
            name = path.relative_to(root).as_posix()
            info = path.lstat()
            if name == ".lock":
                require(stat.S_ISREG(info.st_mode), "Data lock must be a regular file")
                continue
            require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode),
                    f"Symlinks and special files are unsupported: {name}")
            require(info.st_nlink == 1 or stat.S_ISDIR(info.st_mode), f"Hard links are unsupported: {name}")
            entries[name] = {"kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                             "mtime_ns": info.st_mtime_ns}
            if stat.S_ISDIR(info.st_mode):
                visit(path)
            else:
                entries[name]["size"] = info.st_size
    visit(root)
    return entries


def fields(path, expected, optional=()):
    require(path.stat().st_size <= 65536, f"Metadata is too large: {path.name}")
    text = path.read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        if not line:
            continue
        name, separator, value = line.partition("=")
        require(separator and name in (*expected, *optional) and name not in values, f"Invalid metadata: {path.name}")
        values[name] = value
    require(set(expected) <= set(values), f"Missing metadata fields: {path.name}")
    return values


def digest_file(path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with path.open("rb") as source:
        while block := source.read(BLOCK_SIZE):
            digest.update(block)
    return digest.hexdigest()


def validate_cors(path):
    require(path.stat().st_size <= 65536, "CORS configuration exceeds 64 KiB")
    text = path.read_text(encoding="utf-8")
    require("<!" not in text, "Unsupported CORS XML declarations")
    require(not re.search(r"</?[A-Za-z_][\w.-]*:", text), "Prefixed CORS XML is unsupported")
    try:
        document = ET.fromstring(text)
    except ET.ParseError as error:
        raise AdminError(f"Invalid CORS XML: {error}") from None
    namespace = "{http://s3.amazonaws.com/doc/2006-03-01/}" if document.tag.startswith("{") else ""
    require(document.tag == namespace + "CORSConfiguration" and not document.attrib
            and not (document.text or "").strip() and 1 <= len(document) <= 100, "Invalid CORS configuration")
    identities = set()
    for rule in document:
        require(rule.tag == namespace + "CORSRule" and not rule.attrib and not (rule.text or "").strip()
                and not (rule.tail or "").strip(), "Invalid CORS rule")
        values = {}
        for field in rule:
            name = field.tag.removeprefix(namespace)
            require(name in {"AllowedOrigin", "AllowedMethod", "AllowedHeader", "ExposeHeader", "ID", "MaxAgeSeconds"}
                    and not field.attrib and not len(field) and not (field.tail or "").strip(), "Invalid CORS field")
            values.setdefault(name, []).append(field.text or "")
        require(values.get("AllowedOrigin") and values.get("AllowedMethod"), "CORS rule needs origins and methods")
        require(all(value in {"GET", "HEAD", "PUT", "POST", "DELETE"} for value in values["AllowedMethod"]), "Invalid CORS method")
        require(all(value and len(value) <= 2048 and value.count("*") <= 1
                    and all(32 < ord(character) < 127 for character in value) for value in values["AllowedOrigin"]), "Invalid CORS origin")
        for name in ["AllowedHeader", "ExposeHeader"]:
            require(all(re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,1024}", value)
                        and value.count("*") <= (1 if name == "AllowedHeader" else 0)
                        for value in values.get(name, [])), "Invalid CORS header")
        if "MaxAgeSeconds" in values:
            require(len(values["MaxAgeSeconds"]) == 1 and re.fullmatch(r"[0-9]{1,10}", values["MaxAgeSeconds"][0])
                    and int(values["MaxAgeSeconds"][0]) <= 2147483647, "Invalid CORS max age")
        if "ID" in values:
            identity = values["ID"][0]
            require(len(values["ID"]) == 1 and 1 <= len(identity.encode("utf-16-le")) // 2 <= 255
                    and identity not in identities, "Invalid CORS rule ID")
            identities.add(identity)


def inspect_storage(root, entries):
    buckets = set()
    objects = uploads = total_bytes = 0
    warnings = []
    for name, record in entries.items():
        parts = safe_path(name).parts
        bucket = parts[0]
        require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket), f"Invalid bucket directory: {bucket}")
        buckets.add(bucket)
        if len(parts) == 1:
            require(record["kind"] == "directory", f"Bucket is not a directory: {name}")
        elif len(parts) == 2 and parts[1] in ("objects", ".uploads", ".versions"):
            require(record["kind"] == "directory", f"Expected a directory: {name}")
        elif len(parts) == 2 and parts[1] == ".created":
            require(record["kind"] == "file" and record["size"] <= 32, f"Invalid bucket creation time: {name}")
            require(re.fullmatch(r"[0-9]+", (root / name).read_text().strip()), f"Invalid bucket creation time: {name}")
        elif len(parts) == 2 and parts[1] == ".cors":
            require(record["kind"] == "file", f"Expected a CORS configuration file: {name}")
            validate_cors(root / name)
        elif len(parts) == 2 and parts[1] == ".cors.tmp":
            raise AdminError(f"Unfinished CORS update: {name}; start the server to recover, then stop it and retry")
        elif len(parts) == 2 and parts[1] == ".versioning":
            require(record["kind"] == "file" and record["size"] <= 9
                    and (root / name).read_text() in ("Enabled", "Suspended"), f"Invalid versioning status: {name}")
        elif len(parts) == 2 and parts[1] == ".versioning.tmp":
            raise AdminError(f"Unfinished versioning update: {name}; start the server to recover, then stop it and retry")
        elif len(parts) in (3, 4, 5) and parts[1] == ".versions":
            require(valid_key(parts[2]), f"Invalid version key: {name}")
            if len(parts) >= 4:
                require(re.fullmatch(r"[0-9a-f]{64}", parts[3]), f"Unfinished or invalid version directory: {name}; start the server to recover")
            require(record["kind"] == ("file" if len(parts) == 5 else "directory"), f"Invalid version storage entry: {name}")
            if len(parts) == 5:
                require(parts[4] in ("record", "content"), f"Unknown version file: {name}")
        elif len(parts) == 3 and parts[1] == "objects":
            filename = parts[2]
            require(record["kind"] == "file", f"Expected an object file: {name}")
            require(filename.endswith((".object", ".meta")),
                    f"Unfinished transaction or unknown file: {name}; start the server to recover, then stop it and retry")
            encoded, suffix = filename.rsplit(".", 1)
            require(valid_key(encoded), f"Invalid encoded object key: {name}")
            companion = f"{bucket}/objects/{encoded}." + ("meta" if suffix == "object" else "object")
            require(companion in entries, f"Missing object content or metadata: {name}")
            if suffix == "object":
                metadata = fields(root / companion, ("etag", "content-type"), ("modified", "version-id"))
                require(("modified" in metadata) == ("version-id" in metadata), f"Incomplete current version metadata: {companion}")
                etag = metadata["etag"]
                require(re.fullmatch(r"[0-9a-f]{32}(?:-[1-9][0-9]{0,4})?", etag), f"Invalid ETag: {companion}")
                if "-" in etag:
                    require(int(etag.rsplit("-", 1)[1]) <= 10000, f"Invalid multipart ETag: {companion}")
                    warnings.append(f"Multipart ETag cannot be rederived without original parts: {name}")
                else:
                    require(digest_file(root / name, "md5") == etag, f"Object MD5 mismatch: {name}")
                objects += 1
                total_bytes += record["size"]
        elif len(parts) in (3, 4) and parts[1] == ".uploads":
            require(re.fullmatch(r"[0-9a-f]{64}", parts[2]), f"Invalid upload ID: {name}")
            if len(parts) == 3:
                require(record["kind"] == "directory", f"Expected an upload directory: {name}")
                manifest = name + "/manifest"
                require(manifest in entries and entries[manifest]["kind"] == "file", f"Missing upload manifest: {name}")
                values = fields(root / manifest, ("key", "content-type", "initiated"))
                require(valid_key(values["key"]) and re.fullmatch(r"[0-9]+", values["initiated"]), f"Invalid upload manifest: {name}")
                uploads += 1
            else:
                require(record["kind"] == "file", f"Expected an upload file: {name}")
                filename = parts[3]
                if filename != "manifest":
                    part = re.fullmatch(r"part\.([1-9][0-9]{0,4})(\.tmp)?", filename)
                    require(filename in ("assembled", "assembled.meta") or (part and int(part[1]) <= 10000), f"Unknown upload file: {name}")
                    if filename.startswith("assembled") or filename.endswith(".tmp"):
                        warnings.append(f"Preserving temporary upload artifact: {name}")
        else:
            raise AdminError(f"Unknown storage path: {name}")
    for bucket in buckets:
        require(entries.get(f"{bucket}/objects", {}).get("kind") == "directory", f"Missing objects directory: {bucket}")
    version_count = marker_count = 0
    for name, entry in entries.items():
        parts = safe_path(name).parts
        if len(parts) != 3 or parts[1] != ".versions":
            continue
        bucket, _, encoded = parts
        records = []
        identities = set()
        orders = set()
        for version_path, version_entry in entries.items():
            version_parts = safe_path(version_path).parts
            if len(version_parts) != 4 or version_parts[:3] != parts:
                continue
            storage = version_parts[3]
            record_path = version_path + "/record"
            require(entries.get(record_path, {}).get("kind") == "file", f"Missing version record: {version_path}")
            values = fields(root / record_path, ("version-id", "order", "delete-marker", "etag", "content-type", "modified"))
            require(values["version-id"] in (storage, "null") and values["version-id"] not in identities, f"Invalid or duplicate version ID: {version_path}")
            require(re.fullmatch(r"[0-9]{1,19}", values["order"]) and int(values["order"]) <= 9223372036854775807
                    and str(int(values["order"])) == values["order"]
                    and int(values["order"]) not in orders, f"Invalid version order: {version_path}")
            require(re.fullmatch(r"[0-9]{1,19}", values["modified"]) and int(values["modified"]) <= 9223372036854775807, f"Invalid version time: {version_path}")
            require(values["delete-marker"] in ("true", "false"), f"Invalid delete marker: {version_path}")
            content = version_path + "/content"
            if values["delete-marker"] == "true":
                require(content not in entries and not values["etag"] and not values["content-type"], f"Invalid delete marker data: {version_path}")
                marker_count += 1
            else:
                require(entries.get(content, {}).get("kind") == "file", f"Missing version content: {version_path}")
                require(re.fullmatch(r"[0-9a-f]{32}(?:-[1-9][0-9]{0,4})?", values["etag"]), f"Invalid version ETag: {version_path}")
                if "-" not in values["etag"]:
                    require(digest_file(root / content, "md5") == values["etag"], f"Version MD5 mismatch: {version_path}")
                else:
                    require(int(values["etag"].rsplit("-", 1)[1]) <= 10000, f"Invalid version multipart ETag: {version_path}")
                    warnings.append(f"Multipart ETag cannot be rederived without original parts: {version_path}")
                version_count += 1
            identities.add(values["version-id"])
            orders.add(int(values["order"]))
            records.append((int(values["order"]), content, values))
        if not records:
            continue
        require(f"{bucket}/.versioning" in entries, f"Version history without bucket configuration: {name}")
        _, content, latest = max(records, key=lambda record: record[0])
        current = f"{bucket}/objects/{encoded}.object"
        metadata = f"{bucket}/objects/{encoded}.meta"
        if latest["delete-marker"] == "true":
            require(current not in entries and metadata not in entries, f"Delete marker has a live current object: {name}")
        else:
            require(current in entries and metadata in entries, f"Missing current version cache: {name}")
            values = fields(root / metadata, ("etag", "content-type"), ("modified", "version-id"))
            require(("modified" in values) == ("version-id" in values), f"Incomplete current version metadata: {name}")
            if "version-id" not in values:
                require(latest["version-id"] == "null" and latest["order"] == "0"
                        and (root / current).stat().st_mtime_ns // 1_000_000_000 == int(latest["modified"]),
                        f"Unexpected legacy cache for version history: {name}")
            require(all(values[field] == latest[field] for field in values), f"Current version metadata mismatch: {name}")
            require(digest_file(root / current) == digest_file(root / content), f"Current version content mismatch: {name}")
    for name in entries:
        parts = safe_path(name).parts
        if len(parts) == 3 and parts[1] == "objects" and parts[2].endswith(".meta"):
            values = fields(root / name, ("etag", "content-type"), ("modified", "version-id"))
            if "version-id" in values:
                require(any(path.startswith(f"{parts[0]}/.versions/{parts[2][:-5]}/") and path.endswith("/record") for path in entries), f"Current version has no history: {name}")
    return {"buckets": len(buckets), "objects": objects, "uploads": uploads,
            "versions": version_count, "delete_markers": marker_count, "object_bytes": total_bytes, "warnings": warnings}


def manifest_for(root, entries):
    for name, record in entries.items():
        if record["kind"] == "file":
            record["sha256"] = digest_file(root / name)
    return {"format": FORMAT, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "entries": entries}


def backup(root_path, output_path):
    with locked_root(root_path) as root:
        output = pathlib.Path(output_path).absolute()
        parent = output.parent.resolve(strict=True)
        output = parent / output.name
        require(not output.is_relative_to(root), "Backup output must be outside the data directory")
        require(not os.path.lexists(output), f"Output already exists: {output}")
        entries = scan(root)
        report = inspect_storage(root, entries)
        manifest = json.dumps(manifest_for(root, entries), sort_keys=True).encode()
        require(len(manifest) <= MANIFEST_LIMIT, "Backup manifest exceeds 16 MiB")
        descriptor, temporary = tempfile.mkstemp(prefix=".moon-s3-backup-", dir=parent)
        try:
            with os.fdopen(descriptor, "wb") as output_file:
                with tarfile.open(fileobj=output_file, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                    member = tarfile.TarInfo("manifest.json")
                    member.size = len(manifest)
                    member.mode = 0o600
                    archive.addfile(member, io.BytesIO(manifest))
                    for name, record in entries.items():
                        if record["kind"] != "file":
                            continue
                        member = tarfile.TarInfo("data/" + name)
                        member.size = record["size"]
                        member.mode = 0o600
                        with (root / name).open("rb") as source:
                            reader = DigestReader(source)
                            archive.addfile(member, reader)
                            require(reader.digest.hexdigest() == record["sha256"], f"Data changed while archiving: {name}")
                        require(digest_file(root / name) == record["sha256"], f"Data changed during backup: {name}")
                output_file.flush()
                os.fsync(output_file.fileno())
            require(scan(root) == {name: {key: value for key, value in record.items() if key != "sha256"}
                                   for name, record in entries.items()}, "Data changed during backup")
            os.link(temporary, output)
            sync_directory(parent)
        finally:
            os.unlink(temporary)
    return {"archive": str(output), **report}


def unique_json(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result, f"Duplicate manifest property: {name}")
        result[name] = value
    return result


def validate_manifest(manifest, max_bytes):
    require(isinstance(manifest, dict) and manifest.get("format") == FORMAT, "Unsupported backup format")
    entries = manifest.get("entries")
    require(isinstance(entries, dict), "Missing backup entries")
    total = 0
    for name, record in entries.items():
        path = safe_path(name)
        require(path.parts[0] != ".lock", "A backup must not contain the data lock")
        require(isinstance(record, dict) and record.get("kind") in ("file", "directory"), f"Invalid entry: {name}")
        require(type(record.get("mtime_ns")) is int and 0 <= record["mtime_ns"] <= 9223372036854775807,
                f"Invalid timestamp: {name}")
        if record["kind"] == "file":
            require(type(record.get("size")) is int and record["size"] >= 0, f"Invalid file size: {name}")
            require(isinstance(record.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]), f"Invalid checksum: {name}")
            total += record["size"]
        for parent in path.parents:
            if parent.as_posix() != ".":
                require(entries.get(parent.as_posix(), {}).get("kind") == "directory", f"Missing parent directory: {name}")
    require(total <= max_bytes, f"Backup exceeds restore limit of {max_bytes} bytes")
    return entries


def unpack(archive_path, stage, max_bytes):
    with tarfile.open(archive_path, mode="r|") as archive:
        member = archive.next()
        require(member is not None and member.name == "manifest.json" and member.isreg() and not member.issparse()
                and 0 <= member.size <= MANIFEST_LIMIT, "Backup must begin with a regular manifest.json of at most 16 MiB")
        with archive.extractfile(member) as source:
            manifest = json.load(source, object_pairs_hook=unique_json)
        entries = validate_manifest(manifest, max_bytes)
        for name, record in sorted(entries.items(), key=lambda item: len(safe_path(item[0]).parts)):
            if record["kind"] == "directory":
                (stage / name).mkdir(mode=0o700)
        expected = {"data/" + name: record for name, record in entries.items() if record["kind"] == "file"}
        seen = set()
        while member := archive.next():
            require(member.name in expected and member.name not in seen and member.isreg() and not member.issparse(),
                    f"Unexpected, duplicate, or unsafe archive member: {member.name}")
            record = expected[member.name]
            require(member.size == record["size"], f"Size mismatch: {member.name}")
            path = stage / member.name[5:]
            digest = hashlib.sha256()
            with archive.extractfile(member) as source, path.open("xb") as destination:
                os.chmod(path, 0o600)
                while block := source.read(BLOCK_SIZE):
                    digest.update(block)
                    destination.write(block)
                destination.flush()
                os.fsync(destination.fileno())
            require(digest.hexdigest() == record["sha256"], f"SHA-256 mismatch: {member.name}")
            seen.add(member.name)
        require(seen == set(expected), "Backup is missing required files")
    report = inspect_storage(stage, entries)
    for name, record in sorted(entries.items(), key=lambda item: len(safe_path(item[0]).parts), reverse=True):
        path = stage / name
        os.utime(path, ns=(record["mtime_ns"], record["mtime_ns"]))
        if record["kind"] == "directory":
            sync_directory(path)
        else:
            with path.open("rb") as source:
                os.fsync(source.fileno())
    sync_directory(stage)
    return report


def publish_restore(stage, destination):
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(stage), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise AdminError(f"Restore destination already exists: {destination}")
        raise OSError(error, os.strerror(error), str(destination))


def restore(archive_path, destination_path, max_bytes):
    destination = pathlib.Path(destination_path).absolute()
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    require(not os.path.lexists(destination), "Restore requires a new, nonexistent data directory")
    stage = pathlib.Path(tempfile.mkdtemp(prefix=".moon-s3-restore-", dir=parent))
    try:
        report = unpack(archive_path, stage, max_bytes)
        publish_restore(stage, destination)
        sync_directory(parent)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"root": str(destination), **report}


def main():
    parser = argparse.ArgumentParser(description="Offline Linux storage checks, SHA-256 backups and verified restores.")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="check object/metadata integrity while the server is stopped")
    check.add_argument("--root", required=True)
    save = commands.add_parser("backup", help="create a new backup archive outside the data root")
    save.add_argument("--root", required=True)
    save.add_argument("--output", required=True)
    load = commands.add_parser("restore", help="verify a backup and restore into a new data directory")
    load.add_argument("--archive", required=True)
    load.add_argument("--root", required=True)
    load.add_argument("--max-bytes", type=int, default=1024 ** 4, help="maximum total restored file bytes (default: 1 TiB)")
    arguments = parser.parse_args()
    try:
        require(sys.platform == "linux", "This administration tool currently supports Linux only")
        if arguments.command == "check":
            with locked_root(arguments.root) as root:
                report = inspect_storage(root, scan(root))
        elif arguments.command == "backup":
            report = backup(arguments.root, arguments.output)
        else:
            require(arguments.max_bytes >= 0, "--max-bytes must be nonnegative")
            report = restore(arguments.archive, arguments.root, arguments.max_bytes)
        print(json.dumps({"ok": True, **report}, ensure_ascii=False))
    except (AdminError, OSError, ValueError, tarfile.TarError, AttributeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
