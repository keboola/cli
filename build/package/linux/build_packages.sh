#!/usr/bin/env bash
# Build deb/rpm/apk for each Linux arch from the frozen binaries, using nfpm.
# nfpm does not reliably expand ${...} in its config, so we render it with envsubst.
# Usage: build_packages.sh <version> [artifacts-dir]
set -euo pipefail
VERSION="$1"
ART="${2:-artifacts}"

command -v envsubst >/dev/null 2>&1 || { sudo apt-get update && sudo apt-get install -y gettext-base; }
mkdir -p dist

for arch in amd64 arm64; do
  BIN="$ART/bin-linux-${arch}/kbagent"
  [ -f "$BIN" ] || BIN="$ART/bin-linux-${arch}/dist/kbagent"   # tolerate either download layout
  [ -f "$BIN" ] || { echo "missing kbagent for $arch"; ls -R "$ART/bin-linux-${arch}"; exit 1; }
  chmod +x "$BIN"

  export VERSION PKG_ARCH="$arch" BIN_PATH="$BIN"
  envsubst '${VERSION} ${PKG_ARCH} ${BIN_PATH}' < build/package/nfpm.yaml > /tmp/nfpm.yaml
  for fmt in deb rpm apk; do
    nfpm package -f /tmp/nfpm.yaml -p "$fmt" -t "dist/keboola-cli2_${VERSION}_linux_${arch}.${fmt}"
  done
done
ls -al dist/
