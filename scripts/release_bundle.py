import hashlib
import json
import pathlib
import re
import tarfile


def extract_verified(archive, destination, allow_dirty=False):
    archive = pathlib.Path(archive).resolve()
    checked = set()
    for line in (archive.parent / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        if pathlib.Path(name).name != name or name in checked:
            raise ValueError("Invalid or duplicate checksum entry")
        checked.add(name)
        if hashlib.sha256((archive.parent / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Release checksum mismatch: {name}")
    if checked != {archive.name, "manifest.json"}:
        raise ValueError("Expected archive and manifest checksums")
    manifest_data = (archive.parent / "manifest.json").read_bytes()
    manifest = json.loads(manifest_data)
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", manifest["version"]):
        raise ValueError("Invalid release version")
    if manifest["target"] != "linux-x86_64" or (manifest["dirty"] and not allow_dirty):
        raise ValueError("Drills require a clean Linux x86-64 release; use --allow-dirty for local development only")
    root = f"moon-s3-server-{manifest['version']}-linux-x86_64"
    for name, digest in manifest["files"].items():
        path = pathlib.PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or str(path) != name
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("Invalid manifest file entry")
    names = set()
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            path = pathlib.PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or ".." in path.parts
                    or len(path.parts) < 2 or path.parts[0] != root or str(path) != member.name or member.name in names):
                raise ValueError("Invalid release archive entry")
            names.add(member.name)
        source.extractall(destination, filter="data")
    bundle = pathlib.Path(destination) / root
    if (bundle / "manifest.json").read_bytes() != manifest_data:
        raise ValueError("Embedded manifest mismatch")
    expected = {f"{root}/{name}" for name in manifest["files"]} | {f"{root}/manifest.json"}
    if names != expected:
        raise ValueError("Release file inventory mismatch")
    for name, digest in manifest["files"].items():
        if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Release file checksum mismatch: {name}")
    return bundle, manifest
