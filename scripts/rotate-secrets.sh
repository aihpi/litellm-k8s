#!/bin/bash
set -euo pipefail

# Usage: ./scripts/rotate-secrets.sh
# Writes secrets/secrets.yaml (gitignored) and applies it.

NAMESPACE=${NAMESPACE:-litellm}
UI_USERNAME=${UI_USERNAME:-admin}
HF_TOKEN=${HF_TOKEN:-}
LDAP_BIND_DN=${LDAP_BIND_DN:-}
LDAP_BIND_PASSWORD=${LDAP_BIND_PASSWORD:-}

if [ -z "$HF_TOKEN" ]; then
  echo "HF_TOKEN is required" >&2
  exit 1
fi

POSTGRES_PASSWORD=$(openssl rand -hex 16)
MASTER_KEY=$(openssl rand -hex 32)
UI_PASSWORD=$(openssl rand -hex 16)

# LITELLM_SALT_KEY encrypts model credentials stored in the DB
# (LiteLLM_ProxyModelTable). It must stay stable across rotations — changing
# it makes every DB-stored model undecryptable, and LiteLLM silently drops
# them at startup. Reuse the live value if present; otherwise fall back to
# the *current* master key (pre-salt-key deployments encrypted rows with it);
# generate a fresh one only on a brand-new install.
EXISTING_SALT_KEY=$(kubectl get secret litellm-secret -n "$NAMESPACE" -o jsonpath='{.data.LITELLM_SALT_KEY}' 2>/dev/null | base64 --decode || true)
EXISTING_MASTER_KEY=$(kubectl get secret litellm-secret -n "$NAMESPACE" -o jsonpath='{.data.LITELLM_MASTER_KEY}' 2>/dev/null | base64 --decode || true)
if [ -n "$EXISTING_SALT_KEY" ]; then
  SALT_KEY="$EXISTING_SALT_KEY"
elif [ -n "$EXISTING_MASTER_KEY" ]; then
  SALT_KEY="$EXISTING_MASTER_KEY"
else
  SALT_KEY="sk-$(openssl rand -hex 32)"
fi

kubectl create secret generic litellm-secret \
  --from-literal=LITELLM_MASTER_KEY="sk-${MASTER_KEY}" \
  --from-literal=LITELLM_SALT_KEY="${SALT_KEY}" \
  --from-literal=DATABASE_URL="postgresql://litellm:${POSTGRES_PASSWORD}@postgres-service:5432/litellm" \
  --from-literal=POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
  --from-literal=UI_USERNAME="${UI_USERNAME}" \
  --from-literal=UI_PASSWORD="${UI_PASSWORD}" \
  --from-literal=HF_TOKEN="${HF_TOKEN}" \
  --from-literal=LDAP_BIND_DN="${LDAP_BIND_DN}" \
  --from-literal=LDAP_BIND_PASSWORD="${LDAP_BIND_PASSWORD}" \
  --dry-run=client -o yaml > secrets/secrets.yaml

kubectl apply -f secrets/secrets.yaml -n "$NAMESPACE"

kubectl rollout restart -n "$NAMESPACE" deployment/postgres
kubectl rollout restart -n "$NAMESPACE" deployment/litellm-proxy

echo "Rotated secrets and restarted postgres + litellm-proxy."
