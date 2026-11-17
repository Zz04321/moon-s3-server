#!/usr/bin/env python3
import argparse
import json
import subprocess
import time
import uuid

from auth_regression import ACCESS_KEY, SECRET_KEY
from operations_regression import request


def docker(*arguments):
    return subprocess.run(["docker", *arguments], check=True,
                          text=True, capture_output=True).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description="Verify a built Moon S3 Docker image.")
    parser.add_argument("--image", default="moon-s3-server:local")
    options = parser.parse_args()
    name = "moon-s3-smoke-" + uuid.uuid4().hex[:12]
    volume = name + "-data"
    docker("volume", "create", volume)
    try:
        docker("run", "-d", "--name", name, "--stop-timeout", "15",
               "--health-interval", "1s", "--health-start-period", "1s",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
               "-p", "127.0.0.1::9000", "--mount", f"type=volume,src={volume},dst=/data",
               "-e", f"MOON_S3_ACCESS_KEY={ACCESS_KEY}",
               "-e", f"MOON_S3_SECRET_KEY={SECRET_KEY}", options.image)

        def healthy_port():
            for attempt in range(100):
                state = json.loads(docker("inspect", "--format", "{{json .State}}", name))
                assert state["Running"], docker("logs", name)
                if state.get("Health", {}).get("Status") == "healthy":
                    return int(docker("port", name, "9000/tcp").rsplit(":", 1)[1])
                time.sleep(0.1)
            raise AssertionError("Container did not become healthy")

        port = healthy_port()
        assert docker("inspect", "--format", "{{.Config.User}}", name) == "10001:10001"
        assert request(port, "GET", "/metrics")[0] == 403
        assert request(port, "PUT", "/docker-bucket", authenticated=True)[0] == 200
        assert request(port, "PUT", "/docker-bucket/hello", b"persistent", authenticated=True)[0] == 200
        assert request(port, "GET", "/metrics", authenticated=True)[0] == 200
        docker("stop", name)
        assert docker("inspect", "--format", "{{.State.ExitCode}}", name) == "0"
        entries = [json.loads(line) for line in docker("logs", name).splitlines()]
        assert any(entry["event"] == "shutdown_drained" for entry in entries)
        docker("start", name)
        port = healthy_port()
        assert request(port, "GET", "/docker-bucket/hello", authenticated=True)[2] == b"persistent"
        docker("stop", name)
        assert docker("inspect", "--format", "{{.State.ExitCode}}", name) == "0"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        docker("volume", "rm", volume)
    print("Docker smoke passed: nonroot health checks, signed requests, metrics, graceful stop and persistent restart.")


if __name__ == "__main__":
    main()
