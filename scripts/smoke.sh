#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

PORT="${MOON_S3_SMOKE_PORT:-19000}"
TMP_DIR="$(mktemp -d)"
SERVER_PID=""

cleanup() {
  if [[ -n "${SERVER_PID}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
    kill "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

echo "==> Building native server"
moon build cmd/main --target native

BIN="_build/native/debug/build/cmd/main/main.exe"
if [[ ! -x "${BIN}" ]]; then
  echo "server binary not found: ${BIN}" >&2
  exit 1
fi

echo "==> Starting server on 127.0.0.1:${PORT}"
MOON_S3_ROOT="${TMP_DIR}/data" \
MOON_S3_LISTEN="127.0.0.1:${PORT}" \
"${BIN}" >"${TMP_DIR}/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 50); do
  if curl --noproxy '*' -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

if ! curl --noproxy '*' -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null; then
  echo "server did not become healthy; log follows:" >&2
  cat "${TMP_DIR}/server.log" >&2
  exit 1
fi

echo "==> Creating bucket"
curl --noproxy '*' -fsS -X PUT "http://127.0.0.1:${PORT}/smoke-bucket" >/dev/null

echo "==> Uploading and downloading object"
printf 'hello from moon-s3-server\n' >"${TMP_DIR}/payload.txt"
curl --noproxy '*' -fsS -X PUT \
  -H 'Content-Type: text/plain' \
  --data-binary "@${TMP_DIR}/payload.txt" \
  "http://127.0.0.1:${PORT}/smoke-bucket/docs/hello.txt" >/dev/null
curl --noproxy '*' -fsS \
  "http://127.0.0.1:${PORT}/smoke-bucket/docs/hello.txt" >"${TMP_DIR}/download.txt"
cmp "${TMP_DIR}/payload.txt" "${TMP_DIR}/download.txt"

echo "==> Listing objects"
LIST_XML="$(curl --noproxy '*' -fsS \
  "http://127.0.0.1:${PORT}/smoke-bucket?list-type=2&prefix=docs/")"
grep -q '<Key>docs/hello.txt</Key>' <<<"${LIST_XML}"

echo "==> Deleting object and bucket"
curl --noproxy '*' -fsS -X DELETE \
  "http://127.0.0.1:${PORT}/smoke-bucket/docs/hello.txt" >/dev/null
curl --noproxy '*' -fsS -X DELETE \
  "http://127.0.0.1:${PORT}/smoke-bucket" >/dev/null

echo "Smoke test passed."
