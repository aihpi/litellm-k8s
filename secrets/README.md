# Secrets Management

NEVER commit actual secrets to Git.

## Create Secrets

1. Copy example:
```bash
cp example-secrets.yaml secrets.yaml
```

2. Edit with real values:
```bash
# Use strong random keys!
export MASTER_KEY=$(openssl rand -hex 32)
export POSTGRES_PASSWORD=$(openssl rand -hex 16)

kubectl create secret generic litellm-secret \
  --from-literal=LITELLM_MASTER_KEY="sk-${MASTER_KEY}" \
  --from-literal=DATABASE_URL="postgresql://litellm:${POSTGRES_PASSWORD}@postgres-service:5432/litellm" \
  --from-literal=POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
  --from-literal=UI_USERNAME="admin" \
  --from-literal=UI_PASSWORD="CHANGE-ME" \
  --from-literal=HF_TOKEN="hf_..." \
  --dry-run=client -o yaml > secrets.yaml
```

3. Apply:
```bash
kubectl apply -f secrets.yaml
```

## Hugging Face token (gated models)

The HF token is stored in the same `litellm-secret` used by the deployments.

## Better: Use External Secrets

For production, use External Secrets Operator or your platform's secret management.
