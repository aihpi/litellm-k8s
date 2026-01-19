#!/bin/bash
set -euo pipefail

MODEL_NAME=${1:-}
SERVICE_NAME=${2:-}
PORT=${3:-8000}

if [ -z "${MODEL_NAME}" ] || [ -z "${SERVICE_NAME}" ]; then
  echo "Usage: ./add-model.sh <model-name> <service-name> [port]"
  exit 1
fi

LITELLM_URL=${LITELLM_URL:-http://localhost:4000}
MASTER_KEY=${LITELLM_MASTER_KEY:-}

if [ -z "${MASTER_KEY}" ]; then
  echo "Error: LITELLM_MASTER_KEY is not set."
  exit 1
fi

response=$(curl -sS -f -X POST "${LITELLM_URL}/model/new" \
  -H "Authorization: Bearer ${MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\
    \"model_name\": \"${MODEL_NAME}\",\
    \"litellm_params\": {\
      \"model\": \"openai/${MODEL_NAME}\",\
      \"api_base\": \"http://${SERVICE_NAME}:${PORT}/v1\",\
      \"api_key\": \"dummy\"\
    }\
  }")

echo "Added model: ${MODEL_NAME}"
echo "${response}"
