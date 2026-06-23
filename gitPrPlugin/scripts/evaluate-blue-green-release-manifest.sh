#!/bin/bash
# Evaluate blue-green release manifests (PLUGIN_MODE=blue-green).
#
# Parsing order: python3+PyYAML -> yq -> grep/awk (Harness-delegate safe).
# Harness ShellScript step MUST use shell: Bash (not Sh).

if [ -z "${BASH_VERSION:-}" ]; then
  exec /bin/bash "$0" "$@"
fi

set -eu

CHANGE_TICKET="${CHANGE_TICKET:-}"
ONLINE_CHANGE_TICKET="${ONLINE_CHANGE_TICKET:-$CHANGE_TICKET}"
OFFLINE_COLOR="${OFFLINE_COLOR:-blue}"
ONLINE_COLOR="${ONLINE_COLOR:-green}"
RELEASE_PHASE="${RELEASE_PHASE:-offline}"
MANIFEST_ROOT="${MANIFEST_ROOT:-.}"

ENABLED_ITEMS=()
SKIPPED_ITEMS=()
PARSE_METHOD=""

strip_yaml_ext() {
  local name="$1"
  name="${name%.yaml}"
  name="${name%.yml}"
  printf '%s' "$name"
}

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

to_json_array() {
  local -a items=("$@")
  if [ "${#items[@]}" -eq 0 ]; then
    echo '[]'
    return
  fi
  local out='['
  local i=0
  for item in "${items[@]}"; do
    [ $i -gt 0 ] && out+=','
    out+="\"${item//\"/\\\"}\""
    i=$((i + 1))
  done
  out+=']'
  echo "$out"
}

read_manifest_ticket() {
  local file="$1"
  local ticket=""
  ticket="$(grep -E '^(changeTicket|ChangeTicket):' "$file" 2>/dev/null | head -1 | sed -E 's/^[^:]+:[[:space:]]*//' | tr -d "\"'" || true)"
  if [ -z "$ticket" ] && command -v yq >/dev/null 2>&1; then
    ticket="$(yq e '.changeTicket // .ChangeTicket // ""' "$file" 2>/dev/null || yq r "$file" changeTicket 2>/dev/null || true)"
  fi
  printf '%s' "$ticket"
}

parse_with_python() {
  local file="$1"
  command -v python3 >/dev/null 2>&1 || return 1
  python3 - "$file" <<'PY' || return 1
import json, sys
try:
    import yaml
except ImportError:
    sys.exit(1)

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    doc = yaml.safe_load(fh) or {}

def norm(key):
    key = str(key)
    for ext in (".yml", ".yaml"):
        if key.lower().endswith(ext):
            return key[: -len(ext)]
    return key

enabled, skipped = [], []
services = doc.get("services")
if isinstance(services, dict):
    for key, val in services.items():
        stem = norm(key)
        if val is True:
            enabled.append(stem)
        elif val is False:
            skipped.append(stem)
elif isinstance(services, list):
    for item in services:
        if isinstance(item, dict):
            for key, val in item.items():
                stem = norm(key)
                if val is True:
                    enabled.append(stem)
                elif val is False:
                    skipped.append(stem)
        elif item is not None:
            enabled.append(norm(item))
else:
    sys.exit(1)

for stem in enabled:
    print(f"ENABLED\t{stem}")
for stem in skipped:
    print(f"SKIPPED\t{stem}")
PY
}

load_python_parse_result() {
  local file="$1"
  local line kind key

  ENABLED_ITEMS=()
  SKIPPED_ITEMS=()

  while IFS=$'\t' read -r kind key; do
    [ -z "$kind" ] && continue
    if [ "$kind" = "ENABLED" ]; then
      ENABLED_ITEMS+=("$key")
    elif [ "$kind" = "SKIPPED" ]; then
      SKIPPED_ITEMS+=("$key")
    fi
  done < <(parse_with_python "$file")

  [ "${#ENABLED_ITEMS[@]}" -gt 0 ] || [ "${#SKIPPED_ITEMS[@]}" -gt 0 ]
}

parse_with_yq() {
  local file="$1"
  command -v yq >/dev/null 2>&1 || return 1

  local services_type first_type key
  services_type="$(yq e '.services | type' "$file" 2>/dev/null || true)"
  [ -n "$services_type" ] || return 1

  first_type=""
  if [ "$services_type" = "!!seq" ]; then
    first_type="$(yq e '.services[0] | type' "$file" 2>/dev/null || echo "!!str")"
  fi

  ENABLED_ITEMS=()
  SKIPPED_ITEMS=()

  if [ "$services_type" = "!!map" ]; then
    while IFS= read -r key; do
      [ -z "$key" ] && continue
      ENABLED_ITEMS+=("$(strip_yaml_ext "$key")")
    done < <(yq e '.services | to_entries | .[] | select(.value == true) | .key' "$file" 2>/dev/null || true)
    while IFS= read -r key; do
      [ -z "$key" ] && continue
      SKIPPED_ITEMS+=("$(strip_yaml_ext "$key")")
    done < <(yq e '.services | to_entries | .[] | select(.value == false) | .key' "$file" 2>/dev/null || true)
  elif [ "$services_type" = "!!seq" ] && [ "$first_type" = "!!map" ]; then
    while IFS= read -r key; do
      [ -z "$key" ] && continue
      ENABLED_ITEMS+=("$(strip_yaml_ext "$key")")
    done < <(yq e '.services[] | to_entries[] | select(.value == true) | .key' "$file" 2>/dev/null || true)
    while IFS= read -r key; do
      [ -z "$key" ] && continue
      SKIPPED_ITEMS+=("$(strip_yaml_ext "$key")")
    done < <(yq e '.services[] | to_entries[] | select(.value == false) | .key' "$file" 2>/dev/null || true)
  elif [ "$services_type" = "!!seq" ]; then
    while IFS= read -r key; do
      [ -z "$key" ] && continue
      ENABLED_ITEMS+=("$(strip_yaml_ext "$key")")
    done < <(yq e '.services[]' "$file" 2>/dev/null || true)
  else
    return 1
  fi

  [ "${#ENABLED_ITEMS[@]}" -gt 0 ] || [ "${#SKIPPED_ITEMS[@]}" -gt 0 ]
}

parse_with_grep() {
  local file="$1"
  local in_services=0
  local line key

  ENABLED_ITEMS=()
  SKIPPED_ITEMS=()

  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    [ -z "${line//[[:space:]]/}" ] && continue

    if [[ "$line" =~ ^[[:space:]]*services:[[:space:]]*$ ]]; then
      in_services=1
      continue
    fi

    if [ "$in_services" -eq 1 ]; then
      if [[ "$line" =~ ^[^[:space:]] ]]; then
        break
      fi

      if [[ "$line" =~ ^[[:space:]]+-[[:space:]]+([^:]+):[[:space:]]*true[[:space:]]*$ ]]; then
        key="$(trim "${BASH_REMATCH[1]}")"
        ENABLED_ITEMS+=("$(strip_yaml_ext "$key")")
      elif [[ "$line" =~ ^[[:space:]]+-[[:space:]]+([^:]+):[[:space:]]*false[[:space:]]*$ ]]; then
        key="$(trim "${BASH_REMATCH[1]}")"
        SKIPPED_ITEMS+=("$(strip_yaml_ext "$key")")
      elif [[ "$line" =~ ^[[:space:]]+-[[:space:]]+([^[:space:]]+)[[:space:]]*$ ]]; then
        key="$(trim "${BASH_REMATCH[1]}")"
        ENABLED_ITEMS+=("$(strip_yaml_ext "$key")")
      elif [[ "$line" =~ ^[[:space:]]+([^:]+):[[:space:]]*true[[:space:]]*$ ]]; then
        key="$(trim "${BASH_REMATCH[1]}")"
        ENABLED_ITEMS+=("$(strip_yaml_ext "$key")")
      elif [[ "$line" =~ ^[[:space:]]+([^:]+):[[:space:]]*false[[:space:]]*$ ]]; then
        key="$(trim "${BASH_REMATCH[1]}")"
        SKIPPED_ITEMS+=("$(strip_yaml_ext "$key")")
      fi
    fi
  done < "$file"

  [ "${#ENABLED_ITEMS[@]}" -gt 0 ] || [ "${#SKIPPED_ITEMS[@]}" -gt 0 ]
}

parse_manifest_services() {
  local file="$1"

  if load_python_parse_result "$file"; then
    PARSE_METHOD="python"
  elif parse_with_yq "$file"; then
    PARSE_METHOD="yq"
  elif parse_with_grep "$file"; then
    PARSE_METHOD="grep"
  else
    return 1
  fi

  ENABLED_JSON="$(to_json_array ${ENABLED_ITEMS[@]+"${ENABLED_ITEMS[@]}"})"
  if [ "${#SKIPPED_ITEMS[@]}" -gt 0 ]; then
    SKIPPED_JSON="$(to_json_array "${SKIPPED_ITEMS[@]}")"
  else
    SKIPPED_JSON='[]'
  fi
  return 0
}

if [ -z "$CHANGE_TICKET" ]; then
  echo "ERROR: CHANGE_TICKET is required" >&2
  exit 1
fi

RELEASE_DIR="${MANIFEST_ROOT}/release-${CHANGE_TICKET}"

case "$RELEASE_PHASE" in
  offline)
    FILE="${RELEASE_DIR}/offline-${OFFLINE_COLOR}-services.yml"
    EXPECTED_TICKET="$CHANGE_TICKET"
    ;;
  online)
    FILE="${RELEASE_DIR}/online-${ONLINE_COLOR}-services.yml"
    EXPECTED_TICKET="$ONLINE_CHANGE_TICKET"
    ;;
  *)
    echo "ERROR: RELEASE_PHASE must be 'offline' or 'online' (got '$RELEASE_PHASE')" >&2
    exit 1
    ;;
esac

if [ ! -f "$FILE" ]; then
  echo "ERROR: manifest not found: $FILE" >&2
  exit 1
fi

echo "======================================"
echo "Processing blue-green manifest ($RELEASE_PHASE): $FILE"
echo "======================================"

MANIFEST_TICKET="$(read_manifest_ticket "$FILE")"
if [ -n "$EXPECTED_TICKET" ] && [ -n "$MANIFEST_TICKET" ] && [ "$MANIFEST_TICKET" != "$EXPECTED_TICKET" ]; then
  echo "ERROR: manifest changeTicket '$MANIFEST_TICKET' != expected '$EXPECTED_TICKET'" >&2
  exit 1
fi

if ! parse_manifest_services "$FILE"; then
  echo "ERROR: could not parse services from $FILE" >&2
  echo "Hint: install python3+PyYAML or yq on the delegate, or use map/list manifest format." >&2
  exit 1
fi

echo "Parse method: $PARSE_METHOD"
echo "--------------------------------------"
echo "Logging skipped services..."
echo "--------------------------------------"
if [ "${#SKIPPED_ITEMS[@]}" -gt 0 ]; then
  for svc in "${SKIPPED_ITEMS[@]}"; do
    echo "Skipping service: $svc"
  done
fi

echo "--------------------------------------"
echo "Final Outputs"
echo "--------------------------------------"
echo "Phase: $RELEASE_PHASE"
echo "Enabled (JSON): $ENABLED_JSON"
echo "Skipped (JSON): $SKIPPED_JSON"

echo "======================================"
echo "Exporting for Harness..."
echo "======================================"

export ENABLED_SERVICES_JSON="$ENABLED_JSON"
export SKIPPED_SERVICES_JSON="$SKIPPED_JSON"
export MANIFEST_MODE="blue-green"
export MANIFEST_FILE="$FILE"
export RELEASE_PHASE="$RELEASE_PHASE"
export MANIFEST_CHANGE_TICKET="${MANIFEST_TICKET:-$EXPECTED_TICKET}"
export OFFLINE_COLOR="$OFFLINE_COLOR"
export ONLINE_COLOR="$ONLINE_COLOR"

echo "======================================"
echo "Done"
echo "======================================"
