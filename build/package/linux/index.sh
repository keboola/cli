#!/usr/bin/env bash
# Build/refresh the SIGNED apt(deb) and yum(rpm) repositories on the CLI dist
# S3 bucket so `apt-get install keboola-cli2` (etc.) works out of the box.
# (No apk: the frozen binary is glibc-linked and won't run on musl Alpine.)
#
# Signing keys (separate per format):
#   DEB_KEY_PRIVATE   — GPG, signs the apt repo. The public keyring apt downloads is
#                       derived from it (dearmored binary), so there's no DEB_KEY_PUBLIC.
#   RPM_KEY_PUBLIC    — public half of the SEPARATE rpm signing key (nfpm signs the rpm
#                       packages with RPM_KEY_PRIVATE); published for yum clients.
# Requires AWS creds already configured (OIDC).
# Usage: index.sh <s3-bucket> <prefix>   →  s3://<bucket>/<prefix>/{deb,rpm}/
set -euo pipefail
BUCKET="$1"
PREFIX="$2"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT  # clean up temp dir + any key material on exit/failure

if [ -z "${DEB_KEY_PRIVATE:-}" ]; then
  # publish-s3 only runs on real (non-pre-release) tags, where the repo + key must
  # exist for the downstream test-install job. Fail loudly rather than silently
  # skipping and leaving test-install to fail with an obscure root cause.
  echo "::error::DEB_KEY_PRIVATE not set — cannot sign/index the apt repo for a real release."
  exit 1
fi

sudo apt-get update -y
sudo apt-get install -y dpkg-dev apt-utils createrepo-c gnupg

# Import GPG signing key (deb + rpm metadata).
printf '%s' "$DEB_KEY_PRIVATE" | gpg --batch --import
KEYID=$(gpg --list-secret-keys --with-colons | awk -F: '/^sec/{print $5; exit}')

# publish_repo <fmt> <indexer-fn> <public-key-file> <public-key-content>
# Common pull-existing → add-new → index → publish flow; the per-format index
# command is the only thing that differs (passed as a function name).
publish_repo() {
  local fmt="$1" indexer="$2" pub_file="$3" pub_content="$4"
  local dir="$WORK/$fmt"
  mkdir -p "$dir"
  aws s3 sync "s3://$BUCKET/$PREFIX/$fmt/" "$dir/" --exclude '*' --include "*.$fmt" || true
  find . -path ./.git -prune -o -name "*.$fmt" -exec cp {} "$dir/" \;
  ( cd "$dir" && "$indexer" )
  [ -n "$pub_content" ] && printf '%s' "$pub_content" > "$dir/$pub_file"
  # Index/metadata files (Packages, Release, repodata/*, keyrings) sit at STABLE paths
  # that are overwritten each release, so they must NOT be cached long by CloudFront
  # (default_ttl is 24h) or clients install stale versions. Upload them no-cache; the
  # packages themselves have version-unique names, so cache them immutably.
  aws s3 sync "$dir/" "s3://$BUCKET/$PREFIX/$fmt/" --exclude "*.$fmt" --cache-control "no-cache"
  aws s3 sync "$dir/" "s3://$BUCKET/$PREFIX/$fmt/" --exclude "*" --include "*.$fmt" --cache-control "public, max-age=31536000, immutable"
}

index_deb() {
  # -m/--multiversion: keep ALL packages, not just the newest per name. Without it
  # dpkg-scanpackages dedups on name+version IGNORING architecture, so the amd64 and
  # arm64 debs collide and one is dropped ("is repeat; ignored"), leaving apt on the
  # other arch with no install candidate. -m emits a stanza per file (both arches).
  # Strip the leading "./" from Filename: apt would fetch <repo>/deb/./pkg.deb, and S3
  # (behind CloudFront) treats the literal "/./" as a missing key -> 404. A clean
  # Filename: pkg.deb yields <repo>/deb/pkg.deb, which exists.
  dpkg-scanpackages -m . /dev/null | sed -E 's|^Filename: \./|Filename: |' > Packages
  gzip -kf Packages
  apt-ftparchive release . > Release
  gpg --batch --yes --default-key "$KEYID" -abs -o Release.gpg Release
  gpg --batch --yes --default-key "$KEYID" --clearsign -o InRelease Release
  # Publish the DEARMORED (binary) keyring — apt's /etc/apt/trusted.gpg.d expects a
  # binary keyring, not an ASCII-armored block, so `gpg --export` WITHOUT --armor.
  gpg --export "$KEYID" > keboola.gpg
}
index_rpm() { createrepo_c .; }

# deb: index_deb writes its own (dearmored) keboola.gpg, so pass no pub-key content.
publish_repo deb index_deb "" ""
publish_repo rpm index_rpm keboola.gpg "${RPM_KEY_PUBLIC:-}"

echo "Repositories indexed and published under s3://$BUCKET/$PREFIX/{deb,rpm}/"
