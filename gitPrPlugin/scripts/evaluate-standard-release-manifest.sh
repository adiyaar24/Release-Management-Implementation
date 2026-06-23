#!/bin/bash
# Evaluate release-manifest-<ticket>.yaml (PLUGIN_MODE=standard).
#
# Harness ShellScript step MUST use shell: Bash (not Sh).

if [ -z "${BASH_VERSION:-}" ]; then
  exec /bin/bash "$0" "$@"
fi

set -eu

CHANGE_TICKET="${CHANGE_TICKET:-}"
FILE="${FILE:-}"

if [ -z "$FILE" ] && [ -n "$CHANGE_TICKET" ]; then
  FILE="release-manifest-${CHANGE_TICKET}.yaml"
fi

if [ -z "$FILE" ]; then
  echo "ERROR: set FILE or CHANGE_TICKET" >&2
  exit 1
fi

if [ ! -f "$FILE" ]; then
  echo "ERROR: manifest not found: $FILE" >&2
  exit 1
fi

echo "======================================"
echo "Processing standard release manifest: $FILE"
echo "======================================"

MANIFEST_TICKET="$(yq e '.ChangeTicket // .changeTicket // ""' "$FILE")"
if [ -n "$CHANGE_TICKET" ] && [ -n "$MANIFEST_TICKET" ] && [ "$MANIFEST_TICKET" != "$CHANGE_TICKET" ]; then
  echo "ERROR: manifest ChangeTicket '$MANIFEST_TICKET' != expected '$CHANGE_TICKET'" >&2
  exit 1
fi

ENABLED_JSON="$(yq e -o=json -I=0 '.services | to_entries | map(select(.value == true) | .key)' "$FILE")"
SKIPPED_JSON="$(yq e -o=json -I=0 '.services | to_entries | map(select(.value == false) | .key)' "$FILE")"

echo "--------------------------------------"
echo "Logging skipped services..."
echo "--------------------------------------"

yq e '.services | to_entries | map(select(.value == false) | .key) | .[]' "$FILE" | while IFS= read -r svc; do
  [ -z "$svc" ] && continue
  echo "Skipping service: $svc"
done

echo "--------------------------------------"
echo "Final Outputs"
echo "--------------------------------------"
echo "Enabled (JSON): $ENABLED_JSON"
echo "Skipped (JSON): $SKIPPED_JSON"

echo "======================================"
echo "Exporting for Harness..."
echo "======================================"

export ENABLED_SERVICES_JSON="$ENABLED_JSON"
export SKIPPED_SERVICES_JSON="$SKIPPED_JSON"
export MANIFEST_MODE="standard"
export MANIFEST_FILE="$FILE"
export MANIFEST_CHANGE_TICKET="${MANIFEST_TICKET:-$CHANGE_TICKET}"

echo "======================================"
echo "Done"
echo "======================================"
