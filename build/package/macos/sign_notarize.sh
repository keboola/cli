#!/usr/bin/env bash
# Code-sign + notarize + staple a macOS binary. Required Apple secrets:
#   APPLE_DEVELOPER_CERTIFICATE_P12_BASE64  (Developer ID Application cert, base64 .p12)
#   APPLE_DEVELOPER_CERTIFICATE_PASSWORD    (.p12 password)
#   APPLE_ACCOUNT_USERNAME                  (Apple ID email, e.g. apple@keboola.com)
#   APPLE_ACCOUNT_PASSWORD                  (app-specific password)
#   APPLE_TEAM_ID                           (e.g. 46P6KJ65M2)
# FAILS (exit 1) if the cert secret is absent — fail-closed so a real release never
# ships unsigned (pre-release tags mark this step continue-on-error). Usage: sign_notarize.sh <binary>
set -euo pipefail
BIN="$1"

for v in APPLE_DEVELOPER_CERTIFICATE_P12_BASE64 APPLE_DEVELOPER_CERTIFICATE_PASSWORD \
         APPLE_ACCOUNT_USERNAME APPLE_ACCOUNT_PASSWORD APPLE_TEAM_ID; do
  [ -n "${!v:-}" ] || { echo "::error::$v not set — refusing to ship an unsigned/un-notarized macOS binary."; exit 1; }
done

KEYCHAIN=build.keychain
security create-keychain -p actions "$KEYCHAIN"
security default-keychain -s "$KEYCHAIN"
security unlock-keychain -p actions "$KEYCHAIN"
printf '%s' "$APPLE_DEVELOPER_CERTIFICATE_P12_BASE64" | base64 -d > /tmp/cert.p12
security import /tmp/cert.p12 -k "$KEYCHAIN" -P "${APPLE_DEVELOPER_CERTIFICATE_PASSWORD:-}" -T /usr/bin/codesign
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k actions "$KEYCHAIN" >/dev/null

IDENTITY=$(security find-identity -v -p codesigning "$KEYCHAIN" | awk '/Developer ID Application/{print $2; exit}')
[ -n "$IDENTITY" ] || { echo "::error::no 'Developer ID Application' identity after import — wrong cert type/password or import failed"; exit 1; }
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$BIN"

# Notarize with the Apple ID + app-specific password (notarytool supports this; no API key needed).
ZIP=/tmp/notarize.zip
ditto -c -k "$BIN" "$ZIP"
xcrun notarytool submit "$ZIP" \
  --apple-id "$APPLE_ACCOUNT_USERNAME" \
  --password "$APPLE_ACCOUNT_PASSWORD" \
  --team-id "$APPLE_TEAM_ID" \
  --wait
# Staple must succeed on a real release (an unstapled binary needs online
# notarization checks → worse offline install UX). Pre-release tags mark the
# whole signing step continue-on-error, so a hard failure here is safe there.
xcrun stapler staple "$BIN"
codesign --verify --verbose "$BIN"
