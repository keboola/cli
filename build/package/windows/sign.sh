#!/usr/bin/env bash
# Authenticode-sign a Windows .exe via Azure Key Vault + jsign (no PKCS#12 stored in
# GitHub). Required service-principal secrets:
#   WINDOWS_SIGNING_TENANT_ID, WINDOWS_SIGNING_CLIENT_ID, WINDOWS_SIGNING_CLIENT_SECRET
# Key Vault keystore + cert alias default to the existing ones; override via env.
# FAILS (exit 1) if signing secrets are missing — the workflow marks this step
# continue-on-error on pre-releases, so dev builds stay non-blocking while a real
# release tag refuses to ship unsigned. Usage: sign.sh <exe-path>
set -euo pipefail
EXE="$1"
KEYVAULT="${AZURE_KEYVAULT_NAME:-kbc-cli-code-signing}"
ALIAS="${AZURE_CERT_ALIAS:-codesigning}"

for v in WINDOWS_SIGNING_TENANT_ID WINDOWS_SIGNING_CLIENT_ID WINDOWS_SIGNING_CLIENT_SECRET; do
  [ -n "${!v:-}" ] || { echo "::error::$v not set — refusing to ship an unsigned Windows exe."; exit 1; }
done

# Service-principal token for the Key Vault data plane.
TOKEN=$(curl -sf -X POST "https://login.microsoftonline.com/${WINDOWS_SIGNING_TENANT_ID}/oauth2/v2.0/token" \
  --data-urlencode "grant_type=client_credentials" \
  --data-urlencode "client_id=${WINDOWS_SIGNING_CLIENT_ID}" \
  --data-urlencode "client_secret=${WINDOWS_SIGNING_CLIENT_SECRET}" \
  --data-urlencode "scope=https://vault.azure.net/.default" \
  | jq -er .access_token)
[ -n "$TOKEN" ] || { echo "::error::failed to obtain Azure access token"; exit 1; }

curl -fsSL -o /tmp/jsign.jar https://github.com/ebourg/jsign/releases/download/6.0/jsign-6.0.jar
echo "05ca18d4ab7b8c2183289b5378d32860f0ea0f3bdab1f1b8cae5894fb225fa8a  /tmp/jsign.jar" | sha256sum -c -
java -jar /tmp/jsign.jar \
  --storetype AZUREKEYVAULT \
  --keystore "$KEYVAULT" \
  --alias "$ALIAS" \
  --storepass "$TOKEN" \
  --tsaurl https://timestamp.digicert.com \
  --replace \
  "$EXE"
