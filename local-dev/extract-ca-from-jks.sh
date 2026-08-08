#!/usr/bin/env bash
# Extract CA certificates from a JKS truststore to PEM (LLM-S04.1).
# Requires keytool (JDK). Does not echo the password.
#
# Usage:
#   TRUSTSTORE_PATH=./aitruststore.jks TRUSTSTORE_PASSWORD=... ./local-dev/extract-ca-from-jks.sh
#   → local-dev/.runtime/corp-ca-bundle.pem
#   export SSL_CERT_FILE=... REQUESTS_CA_BUNDLE=...
set -euo pipefail
LOCAL_DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="${LOCAL_DEV}/.runtime"
mkdir -p "$RUNTIME"

STORE="${TRUSTSTORE_PATH:?set TRUSTSTORE_PATH to the .jks truststore}"
PASS="${TRUSTSTORE_PASSWORD:?set TRUSTSTORE_PASSWORD}"
OUT="${CA_BUNDLE_OUT:-${RUNTIME}/corp-ca-bundle.pem}"

if ! command -v keytool >/dev/null 2>&1; then
  echo "ERROR: keytool not found. Install a JDK or provide an existing PEM bundle." >&2
  exit 1
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# Portable alias listing (bash 3.2 / macOS)
aliases="$(
  keytool -list -keystore "$STORE" -storepass "$PASS" 2>/dev/null \
    | awk -F, '/^[a-zA-Z0-9].*, /{print $1}' || true
)"

: >"$OUT"
count=0
while IFS= read -r alias; do
  [[ -z "$alias" ]] && continue
  pem="${tmpdir}/cert.pem"
  if keytool -exportcert -rfc -keystore "$STORE" -storepass "$PASS" \
      -alias "$alias" -file "$pem" 2>/dev/null; then
    cat "$pem" >>"$OUT"
    printf '\n' >>"$OUT"
    count=$((count + 1))
  fi
done <<< "$aliases"

if [[ ! -s "$OUT" || "$count" -eq 0 ]]; then
  echo "ERROR: could not export certificates from $STORE" >&2
  exit 1
fi

echo "Wrote $OUT ($count certs)"
echo "  export SSL_CERT_FILE=$OUT"
echo "  export REQUESTS_CA_BUNDLE=$OUT"
