#!/usr/bin/env bash
# Render the Homebrew formula from the template, substituting the version and the
# per-arch SHA256 sums (read from the *.sha256 sidecar files produced by `freeze`).
# Usage: render.sh <version> <artifacts-dir>   (prints the formula to stdout)
set -euo pipefail
VERSION="$1"
ART="$2"
TMPL="$(dirname "$0")/keboola-cli2.rb.tmpl"

sha() {
  # $1 = os, $2 = arch -> sha256 of keboola-cli2_<v>_<os>_<arch>.zip.
  # Fail hard if the sidecar is missing — never render a formula with a bad checksum.
  local f
  f=$(find "$ART" -name "keboola-cli2_${VERSION}_$1_$2.zip.sha256" | head -1)
  [ -n "$f" ] || { echo "::error::missing checksum for $1_$2 — refusing to render formula" >&2; exit 1; }
  awk '{print $1}' "$f"
}

# Only the arches the template references (macOS arm64; Linux amd64 + arm64).
sed \
  -e "s/{VERSION}/${VERSION}/g" \
  -e "s/{SHA256_DARWIN_ARM64}/$(sha darwin arm64)/g" \
  -e "s/{SHA256_LINUX_ARM64}/$(sha linux arm64)/g" \
  -e "s/{SHA256_LINUX_AMD64}/$(sha linux amd64)/g" \
  "$TMPL"
