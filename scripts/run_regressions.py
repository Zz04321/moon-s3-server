#!/usr/bin/env python3
import argparse
import os
import pathlib
import subprocess
import sys


PROJECT = pathlib.Path(__file__).resolve().parents[1]
SUITES = "auth list object presigned multipart storage copy admin cors version policy event cli console operations safety delete streaming".split()
SDK_SUITES = "presigned copy multipart cors version policy".split()


def main():
    parser = argparse.ArgumentParser(description="Run local or pinned SDK regression suites, failing on the first error.")
    parser.add_argument("--sdk", action="store_true")
    parser.add_argument("--sdk-python", type=pathlib.Path, default=pathlib.Path("/tmp/moon-s3-compat/python/bin/python"))
    args = parser.parse_args()
    environment = dict(os.environ, NO_PROXY="*", no_proxy="*")
    if not args.sdk:
        subprocess.run(["bash", "scripts/smoke.sh"], cwd=PROJECT, env=environment, check=True)
    for suite in SDK_SUITES if args.sdk else SUITES:
        print(f"Running {suite}{' SDK' if args.sdk else ''}", flush=True)
        command = [str(args.sdk_python.absolute()) if args.sdk else sys.executable,
                   f"scripts/{suite}_regression.py"] + (["--sdk"] if args.sdk else [])
        subprocess.run(command, cwd=PROJECT, env=environment, check=True, timeout=600)


if __name__ == "__main__":
    main()
