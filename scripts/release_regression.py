#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import time

from auth_regression import ACCESS_KEY, SECRET_KEY
from operations_regression import free_port, request


def run(*command, environment=None, cwd=None):
    return subprocess.run(command, env=environment, cwd=cwd, capture_output=True, check=True, timeout=30).stdout


def main():
    parser = argparse.ArgumentParser(description="Verify and exercise an extracted release bundle without a MoonBit installation.")
    parser.add_argument("archive", type=pathlib.Path)
    args = parser.parse_args()
    archive = args.archive.resolve()
    checked = set()
    for line in (archive.parent / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        assert pathlib.Path(name).name == name
        assert name not in checked
        checked.add(name)
        assert hashlib.sha256((archive.parent / name).read_bytes()).hexdigest() == digest
    assert checked == {archive.name, "manifest.json"}
    with tempfile.TemporaryDirectory(prefix="moon-s3-release-") as temporary:
        directory = pathlib.Path(temporary)
        with tarfile.open(archive) as source:
            members = source.getmembers()
            assert all(member.isfile() for member in members)
            assert all(not pathlib.PurePosixPath(member.name).is_absolute() and ".." not in pathlib.PurePosixPath(member.name).parts for member in members)
            source.extractall(directory, filter="data")
        bundles = list(directory.iterdir())
        assert len(bundles) == 1
        bundle = bundles[0]
        manifest = json.loads((bundle / "manifest.json").read_text())
        assert (bundle / "manifest.json").read_bytes() == (archive.parent / "manifest.json").read_bytes()
        names = {str(path.relative_to(bundle)) for path in bundle.rglob("*") if path.is_file()}
        assert names == set(manifest["files"]) | {"manifest.json"}
        for name, digest in manifest["files"].items():
            assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == digest
        assert not any(name.startswith((".git/", ".aws/", "data/")) or name == "proposal.md" for name in names)
        for script in ["moon_s3.py", "admin.py", "console.py", "presign.py"]:
            run(sys.executable, str(bundle / "scripts" / script), "--help", cwd=directory)
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith(("MOON_", "AWS_"))}
        environment.update(MOON_S3_ACCESS_KEY=ACCESS_KEY, MOON_S3_SECRET_KEY=SECRET_KEY,
                           NO_PROXY="*", no_proxy="*")
        cli = [sys.executable, str(bundle / "scripts/moon_s3.py")]
        data = directory / "data"

        def start(root):
            port = free_port()
            settings = dict(environment, MOON_S3_ROOT=str(root), MOON_S3_LISTEN=f"127.0.0.1:{port}")
            server = subprocess.Popen([str(bundle / "bin/moon-s3-server")], env=settings, cwd=directory,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for attempt in range(100):
                    if server.poll() is not None:
                        raise AssertionError("Packaged binary failed to start")
                    try:
                        if request(port, "GET", "/healthz")[0] == 200:
                            return server, port
                    except OSError:
                        pass
                    time.sleep(0.05)
                raise AssertionError("Packaged binary never became healthy")
            except BaseException:
                server.kill()
                server.wait()
                raise

        def stop(server):
            server.terminate()
            try:
                assert server.wait(timeout=15) == 0
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
                raise

        server, port = start(data)
        try:
            assert request(port, "GET", "/metrics")[0] == 403
            commands = cli + ["--endpoint", f"http://127.0.0.1:{port}"]
            run(*commands, "bucket", "create", "release-check", environment=environment, cwd=directory)
            payload = directory / "payload.bin"
            payload.write_bytes(bytes(range(256)) * 1025)
            run(*commands, "object", "upload", "release-check", "space + 中文.bin", str(payload), environment=environment, cwd=directory)
            download = directory / "download.bin"
            run(*commands, "object", "download", "release-check", "space + 中文.bin", str(download), environment=environment, cwd=directory)
            assert download.read_bytes() == payload.read_bytes()
        finally:
            stop(server)
        admin = [sys.executable, str(bundle / "scripts/admin.py")]
        run(*admin, "check", "--root", str(data), environment=environment)
        backup = directory / "backup.tar"
        restored = directory / "restored"
        run(*admin, "backup", "--root", str(data), "--output", str(backup), environment=environment)
        run(*admin, "restore", "--archive", str(backup), "--root", str(restored), environment=environment)
        server, port = start(restored)
        try:
            assert request(port, "GET", "/release-check/space%20%2B%20%E4%B8%AD%E6%96%87.bin", authenticated=True)[2] == payload.read_bytes()
        finally:
            stop(server)
    print("Release bundle passed: checksums, file inventory, packaged CLI/binary, signed Unicode transfers and offline backup/restore.")


if __name__ == "__main__":
    main()
