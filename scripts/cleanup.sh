#!/bin/bash
set -euo pipefail

ENV=${1:-dev}

if [ -d "overlays/${ENV}" ]; then
  kubectl delete -k "overlays/${ENV}/" || true
fi

if [ "$ENV" = "staging" ]; then
  kubectl delete -f namespaces/litellm-staging.yaml || true
  exit 0
fi

kubectl delete -k models/ || true
kubectl delete -k base/ || true
