#!/bin/bash
set -euo pipefail

ENV=${1:-dev}

echo "Deploying LiteLLM to ${ENV} environment..."

kubectl apply -f namespaces/litellm.yaml
kubectl apply -k base/

echo "Waiting for PostgreSQL..."
kubectl wait --for=condition=ready pod -l app=postgres -n litellm --timeout=120s

kubectl apply -k models/

if [ -d "overlays/${ENV}" ]; then
  kubectl apply -k "overlays/${ENV}/"
fi

echo "Deployment complete!"
echo "Port-forward: kubectl port-forward -n litellm service/litellm-service 4000:4000"
