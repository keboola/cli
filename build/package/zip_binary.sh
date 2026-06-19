#!/usr/bin/env bash
# Zip the frozen binary in ./dist and write a .sha256 sidecar.
# Usage: zip_binary.sh <pkg_name> <version> <goos> <arch> <bin_name>
set -euo pipefail
PKG="$1"; VERSION="$2"; GOOS="$3"; ARCH="$4"; BIN_NAME="$5"

cd dist
BIN="$BIN_NAME"
[ "$GOOS" = "windows" ] && BIN="${BIN_NAME}.exe"
ZIP="${PKG}_${VERSION}_${GOOS}_${ARCH}.zip"

# Explicit per-OS archiver (7z ships on the Windows runner; zip on *nix).
if [ "$GOOS" = "windows" ]; then
  7z a "$ZIP" "$BIN" >/dev/null
else
  zip -q "$ZIP" "$BIN"
fi

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$ZIP" > "$ZIP.sha256"
else
  shasum -a 256 "$ZIP" > "$ZIP.sha256"
fi
echo "packaged dist/$ZIP"
