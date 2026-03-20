#!/bin/bash
set -euo pipefail

ENV=${1:-dev}
NAMESPACE="litellm"
NAMESPACE_MANIFEST="namespaces/litellm.yaml"

if [ "$ENV" = "staging" ]; then
  NAMESPACE="litellm-staging"
  NAMESPACE_MANIFEST="namespaces/litellm-staging.yaml"
fi

echo "Deploying LiteLLM to ${ENV} environment..."

kubectl apply -f "$NAMESPACE_MANIFEST"
if [ -f "secrets/secrets.yaml" ]; then
  kubectl apply -f <(sed '/^[[:space:]]*namespace:[[:space:]].*$/d' secrets/secrets.yaml) -n "$NAMESPACE"
fi

if [ "$ENV" = "staging" ]; then
  kubectl apply -k overlays/staging/

  echo "Waiting for PostgreSQL..."
  kubectl wait --for=condition=ready pod -l app=postgres -n "$NAMESPACE" --timeout=120s

  echo "Deployment complete!"
  echo "Port-forward: kubectl port-forward -n ${NAMESPACE} service/litellm-service 4000:4000"
  exit 0
fi

kubectl apply -k base/

echo "Waiting for PostgreSQL..."
kubectl wait --for=condition=ready pod -l app=postgres -n "$NAMESPACE" --timeout=120s

kubectl apply -k models/

if [ -d "overlays/${ENV}" ]; then
  kubectl apply -k "overlays/${ENV}/"
fi

echo "Deployment complete!"
echo "Port-forward: kubectl port-forward -n ${NAMESPACE} service/litellm-service 4000:4000"
