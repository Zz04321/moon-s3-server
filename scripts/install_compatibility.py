#!/usr/bin/env python3
import argparse
import hashlib
import json
import pathlib
import platform
import subprocess
import sys
import urllib.request


PROJECT = pathlib.Path(__file__).resolve().parents[1]
LOCK = json.loads((PROJECT / "scripts/compatibility-lock.json").read_text())


def download(name, destination):
    entry = LOCK[name]
    if not destination.exists():
        print(f"Downloading {entry['url']}", flush=True)
        with urllib.request.urlopen(entry["url"], timeout=120) as response, destination.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != entry["sha256"]:
        raise ValueError(f"SHA-256 mismatch: {destination}; remove this incomplete or unexpected download and retry")


def main():
    parser = argparse.ArgumentParser(description="Install pinned compatibility tools into local test directories.")
    parser.add_argument("--tools", type=pathlib.Path, default=pathlib.Path("/tmp/moon-s3-compat"))
    parser.add_argument("--s3-tests", type=pathlib.Path, default=pathlib.Path("/tmp/moon-s3-s3-tests"))
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64" or sys.version_info < (3, 12):
        parser.error("requires Linux x86-64 and Python 3.12+; tested with Python 3.12.3")
    args.tools = args.tools.resolve()
    args.s3_tests = args.s3_tests.resolve()
    args.tools.mkdir(parents=True, exist_ok=True)
    download("aws_cli", args.tools / "aws.zip")
    subprocess.run(["unzip", "-oq", args.tools / "aws.zip", "-d", args.tools], check=True)
    download("mc", args.tools / "mc")
    (args.tools / "mc").chmod(0o755)
    if not args.s3_tests.exists():
        subprocess.run(["git", "clone", "--no-checkout", LOCK["ceph"]["repository"], args.s3_tests], check=True)
        subprocess.run(["git", "checkout", "--detach", LOCK["ceph"]["commit"]], cwd=args.s3_tests, check=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.s3_tests).decode().strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=args.s3_tests).strip()
    if revision != LOCK["ceph"]["commit"] or dirty:
        raise ValueError("Existing s3-tests checkout must be clean and match the locked commit; choose a new --s3-tests directory")
    python = args.tools / "python/bin/python"
    if not python.exists():
        subprocess.run(["uv", "venv", "--python", sys.executable, args.tools / "python"], check=True)
    subprocess.run(["uv", "pip", "sync", "--python", python,
                    PROJECT / "scripts/compatibility-requirements.txt"], check=True)
    subprocess.run(["docker", "pull", LOCK["nginx_image"]], check=True)
    print("Pinned tools ready. Run scripts/compatibility_regression.py with the same directory arguments.")


if __name__ == "__main__":
    main()
