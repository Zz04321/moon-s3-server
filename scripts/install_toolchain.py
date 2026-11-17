#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import pathlib
import platform
import subprocess
import tarfile
import tempfile
import urllib.request


PROJECT = pathlib.Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Install the hash-locked MoonBit toolchain into a new directory.")
    parser.add_argument("--destination", required=True, type=pathlib.Path)
    parser.add_argument("--archives", type=pathlib.Path, help="Use already downloaded archives from this directory")
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("the locked toolchain supports Linux x86-64")
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    lock = json.loads((PROJECT / "scripts/toolchain-lock.json").read_text())
    with tempfile.TemporaryDirectory(prefix="moon-s3-toolchain-") as temporary:
        for entry in lock["archives"]:
            archive = args.archives / entry["name"] if args.archives else pathlib.Path(temporary) / entry["name"]
            if not args.archives:
                with urllib.request.urlopen(entry["url"], timeout=120) as response, archive.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError(f"Toolchain hash mismatch: {entry['name']}. Upstream changed; review and update the lock before installing.")
            target = destination / entry["directory"]
            target.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive) as source:
                source.extractall(target, filter="data")
    for binary in (destination / "bin").iterdir():
        if binary.is_file():
            binary.chmod(binary.stat().st_mode | 0o111)
    compiler = destination / "bin/internal/tcc"
    compiler.chmod(compiler.stat().st_mode | 0o111)
    environment = dict(os.environ, MOON_HOME=str(destination), PATH=str(destination / "bin") + os.pathsep + os.environ["PATH"])
    subprocess.run([destination / "bin/moon", "version", "--all"], env=environment, check=True)
    subprocess.run([destination / "bin/moon", "bundle", "--all"], cwd=destination / "lib/core", env=environment, check=True)


if __name__ == "__main__":
    main()
