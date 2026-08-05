#!/bin/bash
set -euo pipefail

# Push per-token costs from scripts/model-catalog.json onto models that are
# ALREADY registered in the LiteLLM DB, via POST /model/update.
#
# Why a separate script from sync-models-to-db.sh: /model/new refuses to touch a
# model_name the proxy already knows, so it can't backfill prices onto the
# existing catalog. /model/update can, but only for DB-backed models — rows that
# come from base/litellm/configmap.yaml return 400 ("Model in config"). Those get
# their costs from the configmap's litellm_params instead; this script reports
# them so you can tell the two cases apart.
#
# Placement note: costs go in litellm_params, not model_info. /model/update
# persists litellm_params only — it reads model_info purely to resolve
# model_info.id. LiteLLM's router copies every CustomPricingLiteLLMParams field
# out of litellm_params into the model cost map at load time, so spend tracking
# picks them up. Sending only the cost keys is safe: the endpoint merges against
# the existing row, and any field left unset falls back to its stored value.
#
# Usage:
#   export LITELLM_MASTER_KEY=sk-...
#   export LITELLM_URL=https://api.aisc.hpi.de   # default http://localhost:4000
#   ./scripts/set-model-costs.sh --dry-run       # preview, no writes
#   ./scripts/set-model-costs.sh

LITELLM_URL=${LITELLM_URL:-http://localhost:4000}
MASTER_KEY=${LITELLM_MASTER_KEY:-}
DRY_RUN=false

if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=true
fi

if [ -z "${MASTER_KEY}" ]; then
  echo "Error: LITELLM_MASTER_KEY is not set." >&2
  exit 1
fi

command -v jq >/dev/null 2>&1 || { echo "Error: jq is required." >&2; exit 1; }

CATALOG="$(dirname "$0")/model-catalog.json"
[ -f "${CATALOG}" ] || { echo "Error: ${CATALOG} not found." >&2; exit 1; }

model_info=$(curl -sS -f "${LITELLM_URL}/model/info" -H "Authorization: Bearer ${MASTER_KEY}")

updated=0 skipped=0 config_only=0 failed=0

# One "<model_name>\t<cost_json>" line per catalog entry that defines any *cost*
# field. Not every model is priced per token: qwen-image-edit carries
# input_cost_per_image, and LiteLLM accepts per-second and per-pixel fields too.
# So match on the "cost" substring and forward whatever the catalog defines,
# rather than looking for input/output_cost_per_token specifically.
while IFS=$'\t' read -r name costs; do
  [ -n "${name}" ] || continue

  # A model_name can appear twice in /model/info (once from config.yaml, once
  # from the DB). Only DB-backed rows are updatable, and each has its own id.
  ids=$(jq -r --arg n "${name}" \
    '.data[] | select(.model_name == $n and .model_info.db_model == true) | .model_info.id' \
    <<<"${model_info}")

  if [ -z "${ids}" ]; then
    if jq -e --arg n "${name}" '.data[] | select(.model_name == $n)' <<<"${model_info}" >/dev/null; then
      echo "config: ${name} (config.yaml model — set costs in base/litellm/configmap.yaml)"
      config_only=$((config_only + 1))
    else
      echo "skip:   ${name} (not registered with the proxy)"
      skipped=$((skipped + 1))
    fi
    continue
  fi

  while IFS= read -r id; do
    [ -n "${id}" ] || continue
    payload=$(jq -cn --arg n "${name}" --arg id "${id}" --argjson c "${costs}" \
      '{model_name: $n, litellm_params: $c, model_info: {id: $id}}')

    if [ "${DRY_RUN}" = true ]; then
      echo "dry:    ${name} [${id}] <- ${costs}"
      updated=$((updated + 1))
      continue
    fi

    if curl -sS -f -X POST "${LITELLM_URL}/model/update" \
      -H "Authorization: Bearer ${MASTER_KEY}" \
      -H "Content-Type: application/json" \
      -d "${payload}" >/dev/null; then
      echo "update: ${name} [${id}] <- ${costs}"
      updated=$((updated + 1))
    else
      echo "FAILED: ${name} [${id}]" >&2
      failed=$((failed + 1))
    fi
  done <<<"${ids}"
done < <(jq -r '
  .models[]
  | select(.litellm_params | to_entries | any(.key | test("cost")))
  | [.model_name, (.litellm_params | with_entries(select(.key | test("cost"))) | tojson)]
  | @tsv' "${CATALOG}")

if [ "${DRY_RUN}" = true ]; then verb="would update"; else verb="updated"; fi
echo "done: ${updated} ${verb}, ${skipped} skipped, ${config_only} config-only, ${failed} failed"

if [ "${DRY_RUN}" = false ] && [ "${failed}" -eq 0 ]; then
  echo
  echo "Costs now visible to the proxy:"
  # Image models price per image, not per token, so report them on their own line
  # instead of showing a misleading in=$0.0000 out=$0.0000.
  curl -sS -f "${LITELLM_URL}/model/info" -H "Authorization: Bearer ${MASTER_KEY}" \
    | jq -r '.data[]
      | [.model_name,
         ((.litellm_params.input_cost_per_token  // .model_info.input_cost_per_token  // 0) * 1000000),
         ((.litellm_params.output_cost_per_token // .model_info.output_cost_per_token // 0) * 1000000),
         (.litellm_params.input_cost_per_image   // .model_info.input_cost_per_image   // 0)]
      | @tsv' \
    | sort -u \
    | awk -F'\t' '$4 > 0 { printf "  %-24s $%.4f / image\n", $1, $4; next }
                         { printf "  %-24s in=$%-8.4f out=$%-8.4f / 1M tokens\n", $1, $2, $3 }'
fi

[ "${failed}" -eq 0 ]
