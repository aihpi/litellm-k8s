#!/bin/bash
set -euo pipefail

# Sync the full cluster model catalog into the LiteLLM DB via POST /model/new.
#
# Models registered through the UI / /model/new live in Postgres
# (LiteLLM_ProxyModelTable) and are only loaded at startup when
# general_settings.store_model_in_db is true (see base/litellm/configmap.yaml).
# This script makes the catalog reproducible: run it against a fresh DB (or
# after a wipe) and every deployed vLLM service is registered again.
#
# Idempotent: skips any model_name the proxy already knows (config.yaml or DB).
# Pass --all to also register models that only exist in config.yaml, e.g. when
# migrating to DB-as-source-of-truth; DB-backed names are still skipped so
# repeated runs never create duplicates.
#
# Usage:
#   export LITELLM_MASTER_KEY=sk-...
#   export LITELLM_URL=https://api.aisc.hpi.de   # default http://localhost:4000
#   ./scripts/sync-models-to-db.sh [--all]

LITELLM_URL=${LITELLM_URL:-http://localhost:4000}
MASTER_KEY=${LITELLM_MASTER_KEY:-}
INCLUDE_CONFIG_MODELS=false

if [ "${1:-}" = "--all" ]; then
  INCLUDE_CONFIG_MODELS=true
fi

if [ -z "${MASTER_KEY}" ]; then
  echo "Error: LITELLM_MASTER_KEY is not set." >&2
  exit 1
fi

command -v jq >/dev/null 2>&1 || { echo "Error: jq is required." >&2; exit 1; }

# The catalog — one /model/new payload per model, including per-token costs —
# lives in scripts/model-catalog.json so this script and set-model-costs.sh can't
# drift apart on prices. It mirrors base/litellm/configmap.yaml where the model is
# also listed there. Not registered here:
#   - dinov3-embeddings-api: custom request schema (images: [...]), not
#     OpenAI-compatible — cannot be routed through openai/* params.
CATALOG="$(dirname "$0")/model-catalog.json"
[ -f "${CATALOG}" ] || { echo "Error: ${CATALOG} not found." >&2; exit 1; }

# -c keeps each payload on a single line so one array element == one model.
MODELS=()
while IFS= read -r line; do MODELS+=("${line}"); done < <(jq -c '.models[]' "${CATALOG}")
[ "${#MODELS[@]}" -gt 0 ] || { echo "Error: no models in ${CATALOG}." >&2; exit 1; }

model_info=$(curl -sS -f "${LITELLM_URL}/model/info" -H "Authorization: Bearer ${MASTER_KEY}")

# db_model=true → registered in Postgres; false/absent → from config.yaml.
db_names=$(jq -r '.data[] | select(.model_info.db_model == true) | .model_name' <<<"${model_info}" | sort -u)
all_names=$(jq -r '.data[].model_name' <<<"${model_info}" | sort -u)

if [ "${INCLUDE_CONFIG_MODELS}" = true ]; then
  skip_names="${db_names}"
else
  skip_names="${all_names}"
fi

added=0 skipped=0 failed=0
for payload in "${MODELS[@]}"; do
  name=$(jq -r '.model_name' <<<"${payload}")
  if grep -qxF "${name}" <<<"${skip_names}"; then
    echo "skip:   ${name} (already registered)"
    skipped=$((skipped + 1))
    continue
  fi
  if curl -sS -f -X POST "${LITELLM_URL}/model/new" \
    -H "Authorization: Bearer ${MASTER_KEY}" \
    -H "Content-Type: application/json" \
    -d "${payload}" >/dev/null; then
    echo "added:  ${name}"
    added=$((added + 1))
  else
    echo "FAILED: ${name}" >&2
    failed=$((failed + 1))
  fi
done

echo "done: ${added} added, ${skipped} skipped, ${failed} failed"
[ "${failed}" -eq 0 ]
