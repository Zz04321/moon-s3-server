#!/usr/bin/env python3
import argparse
import gzip
import hashlib
import io
import json
import pathlib
import platform
import re
import subprocess
import tarfile


PROJECT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_SCRIPTS = ["admin.py", "console.py", "moon_s3.py", "presign.py", "s3_client.py"]


def command(*arguments):
    return subprocess.check_output(arguments, cwd=PROJECT).decode().strip()


def main():
    parser = argparse.ArgumentParser(description="Build a Linux x86-64 release bundle with explicit runtime files and SHA-256 checksums.")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--allow-dirty", action="store_true", help="Local validation only; record the uncommitted tree in the manifest")
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("release target is Linux x86-64")
    dirty = bool(command("git", "status", "--porcelain"))
    if dirty and not args.allow_dirty:
        parser.error("release requires a clean working tree; --allow-dirty is for local validation only")
    version = re.search(r'^version\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', (PROJECT / "moon.mod").read_text(), re.MULTILINE)
    if not version:
        parser.error("moon.mod must contain a numeric major.minor.patch version")
    version = "v" + version.group(1)
    revision = command("git", "rev-parse", "HEAD")
    epoch = int(command("git", "show", "-s", "--format=%ct", "HEAD"))
    subprocess.run(["moon", "build", "cmd/main", "--target", "native", "--release"], cwd=PROJECT, check=True)
    files = {}
    files["bin/moon-s3-server"] = ((PROJECT / "_build/native/release/build/cmd/main/main.exe").read_bytes(), 0o755)
    sources = ["LICENSE", "README.mbt.md", "compose.yaml", "deploy/config.example.json", "deploy/nginx.conf", "deploy/Dockerfile.release",
               "scripts/compatibility-lock.json", "scripts/toolchain-lock.json"]
    sources += [f"scripts/{name}" for name in RUNTIME_SCRIPTS]
    sources += [str(path.relative_to(PROJECT)) for folder in ["docs", "console"]
                for path in sorted((PROJECT / folder).rglob("*")) if path.is_file()]
    tracked = set(command("git", "ls-files").splitlines())
    for name in sources:
        path = PROJECT / name
        if name not in tracked and not args.allow_dirty:
            raise ValueError(f"Untracked release input: {name}")
        if path.is_symlink():
            raise ValueError(f"Unexpected symlink: {name}")
        files[name] = (path.read_bytes(), 0o644)
    files["Dockerfile"] = ((PROJECT / "deploy/Dockerfile.release").read_bytes(), 0o644)
    files[".dockerignore"] = (b"**\n!bin/moon-s3-server\n", 0o644)
    manifest = dict(version=version, revision=revision, dirty=dirty, target="linux-x86_64",
                    toolchain=command("moon", "version", "--all"),
                    libc=platform.libc_ver(), source_date_epoch=epoch,
                    files={name: hashlib.sha256(data).hexdigest() for name, (data, _) in sorted(files.items())})
    manifest_data = (json.dumps(manifest, indent=2) + "\n").encode()
    files["manifest.json"] = (manifest_data, 0o644)
    name = f"moon-s3-server-{version}-linux-x86_64"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / (name + ".tar.gz")
    if archive.exists() or (output / "manifest.json").exists() or (output / "SHA256SUMS").exists():
        parser.error("output already contains release files; choose a new output directory")
    with archive.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as bundle:
            for filename, (data, mode) in sorted(files.items()):
                info = tarfile.TarInfo(f"{name}/{filename}")
                info.size, info.mode, info.mtime = len(data), mode, epoch
                bundle.addfile(info, io.BytesIO(data))
    (output / "manifest.json").write_bytes(manifest_data)
    (output / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in [archive, output / "manifest.json"]))
    print(archive)


if __name__ == "__main__":
    main()
