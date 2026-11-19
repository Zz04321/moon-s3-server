# CI and release bundles

Repository: <https://github.com/zzSmall6/moon-s3-server>. Git commits retain
their original author identity independently of repository ownership. The
MoonBit module name remains `Zz04321/moon-s3-server` to preserve imports.

## Automation

- **CI** runs on pushes to `main`, pull requests and manual dispatch. It checks
  formatting/interfaces, native compilation, unit tests, smoke, all 18 local
  regression suites and the extracted release bundle. Reports and the test
  bundle are retained as Actions artifacts for 14 days.
- **External compatibility** runs manually. It installs locked clients,
  runs AWS CLI, TLS `mc`, the 33 selected unmodified Ceph cases and proxy
  limits, then all six SDK suites. Logs/JSON results are retained for 30 days.
- **Release bundle** runs manually. It validates local regressions, packages
  and exercises the native release binary, and checks Docker deployment.
  It also runs the [deployment recovery drill](recovery-drill.md); JSON/log
  evidence is saved separately as `deployment-drill-COMMIT` for 30 days.
  Download its artifact for review. By default it creates no GitHub release
  or tag. Enabling `draft_release` creates a draft prerelease only after
  validation; existing releases are never overwritten. Review compatibility
  evidence for the same source commit before publishing the draft.

Workflows run with read-only repository permissions except the optional draft
publication job. They use no AWS accounts or production credentials. The
Copilot environment setup workflow remains separate from these checks.

## Toolchain

[toolchain-lock.json](../scripts/toolchain-lock.json) pins the compiler and
core archives by SHA-256. Upstream dated archive URLs were unavailable during
setup, so the lock uses upstream `latest` URLs with exact content hashes.
The locked snapshot contains `moon 0.1.20261117`, `moonc v0.10.21+06da1ed44`
and `moonrun 0.1.20261117`; these versions are printed in workflow logs.
If those URLs change, installation fails before extracting the changed archive;
it never silently adopts a new compiler. Review a toolchain update, refresh both
hashes together, rerun native/SDK/external acceptance, then commit the new lock.
Archive URLs alone are not a permanent mirror: retain the verified archives
or provide a verified local cache for long-term rebuilds.
CI caches the two archives under a key derived from the lock file and
rechecks their hashes on every installation. This lets a retained cache
continue installing the selected toolchain after upstream `latest` changes.
An expired or missing cache still requires the matching upstream archives;
it never accepts mismatching downloads. `--cache DIRECTORY` enables the
same verified download cache locally.

```bash
python3 scripts/install_toolchain.py --destination /tmp/moon-s3-toolchain
export PATH="/tmp/moon-s3-toolchain/bin:$PATH"
export MOON_HOME=/tmp/moon-s3-toolchain
moon update
```

The installer needs Python with tar extraction filters (Python 3.10.12+ or
3.12+), Linux x86-64 and a new destination. It does not replace your existing
toolchain. `--archives DIRECTORY` accepts files named `moonbit.tar.gz` and
`core.tar.gz` and verifies the same hashes before installation. CI uses
Ubuntu 22.04 with C build tools and OpenSSL development headers.

## Build and verify locally

Start with a clean checkout of the intended release commit. Update `moon.mod`
and the release notes before committing a new release version. Package names
derive from `moon.mod`, currently `v0.2.0`; no release has to be published to
build an archive.

```bash
moon check --target native
moon test --target native
python3 scripts/run_regressions.py
moon info && moon fmt
python3 scripts/package_release.py --output /tmp/moon-s3-dist
python3 scripts/release_regression.py /tmp/moon-s3-dist/*.tar.gz
```

Use a fresh output directory. `--allow-dirty` permits local package testing
before committing and records `dirty: true`; do not publish such a bundle.
The packager always rebuilds the native release binary. It includes only an
explicit inventory of runtime scripts, console assets, docs and deployment
examples, never `.git`, local credentials, data or `proposal.md`.
`manifest.json` records source revision, toolchain, libc, dirty status and
per-file SHA-256 digests. The tarball uses sorted paths, fixed ownership and
the commit timestamp; gzip omits its wall-clock timestamp. This stabilizes
packaging of identical inputs, but does not promise reproducible compiler
output across machines.

## Install a bundle

Download `moon-s3-server-v0.2.0-linux-x86_64.tar.gz`, `manifest.json` and
`SHA256SUMS` from the same release. Then:

```bash
sha256sum -c SHA256SUMS
tar -xzf moon-s3-server-v0.2.0-linux-x86_64.tar.gz
cd moon-s3-server-v0.2.0-linux-x86_64
MOON_S3_ACCESS_KEY=local-access MOON_S3_SECRET_KEY=replace-this-secret \
  MOON_S3_ROOT=./data MOON_S3_LISTEN=127.0.0.1:9000 bin/moon-s3-server
```

The binary requires Linux x86-64 and compatible glibc. Official workflow
bundles are built on Ubuntu 22.04 (glibc 2.35) and also exercised in Debian
Bookworm (glibc 2.36). Python 3.9+ is required for the bundled CLI, console
and Linux offline admin tools. Run `python3 scripts/moon_s3.py --help` or
`python3 scripts/console.py --help`. Configure real secrets outside source
control and use the TLS proxy for remote access; see [deployment](deployment.md).

The extracted archive has a runtime Dockerfile; it needs no MoonBit compiler:

```bash
docker build -t moon-s3-server:v0.2.0 .
export MOON_S3_ACCESS_KEY=local-access
export MOON_S3_SECRET_KEY=replace-this-secret
docker compose up -d
```

When building Compose from the bundle, its default image name is
`moon-s3-server:local`; use `docker compose up --build -d` to build that name,
or adjust the Compose image field to your versioned image. The repository's
source Dockerfile instead expects `_build/.../main.exe`; the packager replaces
it with [Dockerfile.release](../deploy/Dockerfile.release) inside the bundle.

## Delivery checkpoint

See [the current acceptance checkpoint](acceptance.md) for verified release
evidence and remaining operator sign-off.

Before publishing: confirm native CI and manual compatibility acceptance on
the exact release commit, inspect the manifest/checksums, and retain the
evidence outside short-lived Actions artifacts. Complete a deployment and
offline backup/restore drill on the actual delivery machine. The automated
extracted-bundle restore test is evidence for the tools, not that machine's
disk or power-loss behavior. Publishing a draft or creating a Git tag is a
separate explicit release action.
