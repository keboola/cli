#!/usr/bin/env bash
# Build/refresh the SIGNED apt(deb), yum(rpm) and apk repositories on the CLI dist
# S3 bucket so `apt-get install keboola-cli2` (etc.) works out of the box.
#
# Signing keys (separate per format):
#   DEB_KEY_PRIVATE   — GPG, signs the apt repo. The public keyring apt downloads is
#                       derived from it (dearmored binary), so there's no DEB_KEY_PUBLIC.
#   RPM_KEY_PUBLIC    — public half of the SEPARATE rpm signing key (nfpm signs the rpm
#                       packages with RPM_KEY_PRIVATE); published for yum clients.
#   APK_KEY_PRIVATE / APK_KEY_PUBLIC   — abuild RSA keypair, signs the apk index
# Requires AWS creds already configured (OIDC).
# Usage: index.sh <s3-bucket> <prefix>   →  s3://<bucket>/<prefix>/{deb,rpm,apk}/
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
# Only deb/rpm tooling on the host; the apk index is built in Alpine (see index_apk).
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
  aws s3 sync "$dir/" "s3://$BUCKET/$PREFIX/$fmt/"
}

index_deb() {
  dpkg-scanpackages . /dev/null > Packages && gzip -kf Packages
  apt-ftparchive release . > Release
  gpg --batch --yes --default-key "$KEYID" -abs -o Release.gpg Release
  gpg --batch --yes --default-key "$KEYID" --clearsign -o InRelease Release
  # Publish the DEARMORED (binary) keyring — apt's /etc/apt/trusted.gpg.d expects a
  # binary keyring, not an ASCII-armored block, so `gpg --export` WITHOUT --armor.
  gpg --export "$KEYID" > keboola.gpg
}
index_rpm() { createrepo_c .; }

# apk has its own publisher (not publish_repo): repos are PER-ARCH — clients fetch
# <repo>/<arch>/APKINDEX.tar.gz — so packages + signed index live under apk/<arch>/
# (deb arch name -> apk arch name). Everything agrees on the name "keboola": nfpm signs
# each package as .SIGN.RSA.keboola.rsa.pub (apk.key_name in nfpm.yaml); importing the
# pubkey as /etc/apk/keys/keboola.rsa.pub makes the container trust the packages (no
# --allow-untrusted); signing each index with keboola.rsa yields a signature clients
# verify against the published keboola.rsa.pub. apk/abuild-sign are Alpine-only (not in
# Ubuntu apt), so index + sign inside Alpine.
publish_apk() {
  local root="$WORK/apk"
  printf '%s' "$APK_KEY_PRIVATE" > "$WORK/keboola.rsa" && chmod 600 "$WORK/keboola.rsa"
  printf '%s' "$APK_KEY_PUBLIC"  > "$WORK/keboola.rsa.pub"
  local pair deb_arch apk_arch
  for pair in amd64:x86_64 arm64:aarch64; do
    deb_arch="${pair%%:*}"; apk_arch="${pair##*:}"
    mkdir -p "$root/$apk_arch"
    aws s3 sync "s3://$BUCKET/$PREFIX/apk/$apk_arch/" "$root/$apk_arch/" --exclude '*' --include '*.apk' || true
    find . -path ./.git -prune -o -name "*_linux_${deb_arch}.apk" -exec cp {} "$root/$apk_arch/" \;
  done
  # Publish the pubkey at the apk root; clients install it into /etc/apk/keys/keboola.rsa.pub.
  printf '%s' "$APK_KEY_PUBLIC" > "$root/keboola.rsa.pub"
  docker run --rm \
    -v "$root:/work" \
    -v "$WORK/keboola.rsa:/keboola.rsa:ro" \
    -v "$WORK/keboola.rsa.pub:/etc/apk/keys/keboola.rsa.pub:ro" \
    -w /work alpine:3 \
    sh -ceu 'apk add --no-cache abuild >/dev/null
             for d in */; do
               ls "$d"*.apk >/dev/null 2>&1 || continue
               ( cd "$d" && apk index -o APKINDEX.tar.gz ./*.apk && abuild-sign -k /keboola.rsa APKINDEX.tar.gz )
             done'
  aws s3 sync "$root/" "s3://$BUCKET/$PREFIX/apk/"
}

# deb: index_deb writes its own (dearmored) keboola.gpg, so pass no pub-key content.
publish_repo deb index_deb "" ""
publish_repo rpm index_rpm keboola.gpg "${RPM_KEY_PUBLIC:-}"
if [ -z "${APK_KEY_PRIVATE:-}" ]; then
  # No apk signing key configured — the apk index is genuinely opt-out, so skip it.
  echo "::warning::APK_KEY_PRIVATE not set — skipping apk index (deb/rpm done)."
else
  publish_apk
fi

echo "Repositories indexed and published under s3://$BUCKET/$PREFIX/{deb,rpm,apk}/"
