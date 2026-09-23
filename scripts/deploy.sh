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
  echo "Waiting for LiteLLM..."
  kubectl rollout status deployment/litellm-proxy -n "$NAMESPACE" --timeout=180s
  echo "Waiting for KISZ Auth Wrapper..."
  kubectl rollout status deployment/kisz-auth-wrapper -n "$NAMESPACE" --timeout=180s

  echo "Deployment complete!"
  echo "Port-forward: kubectl port-forward -n ${NAMESPACE} service/litellm-service 4000:4000"
  echo "Wrapper service: kubectl get svc -n ${NAMESPACE} kisz-auth-wrapper-service"
  exit 0
fi

kubectl apply -k base/

echo "Waiting for PostgreSQL..."
kubectl wait --for=condition=ready pod -l app=postgres -n "$NAMESPACE" --timeout=120s
echo "Waiting for LiteLLM..."
kubectl rollout status deployment/litellm-proxy -n "$NAMESPACE" --timeout=180s

# aisc has no H100 nodes, so its overlay lists an A30-only model subset
# directly (see overlays/aisc/kustomization.yaml) instead of the full
# models/ aggregate applied here for every other env.
if [ "$ENV" != "aisc" ]; then
  kubectl apply -k models/
fi

if [ -d "overlays/${ENV}" ]; then
  kubectl apply -k "overlays/${ENV}/"
fi

echo "Deployment complete!"
echo "Port-forward: kubectl port-forward -n ${NAMESPACE} service/litellm-service 4000:4000"
