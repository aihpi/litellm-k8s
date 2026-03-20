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
export WRAPPER_SESSION_SECRET=$(openssl rand -hex 32)

kubectl create secret generic litellm-secret \
  --from-literal=LITELLM_MASTER_KEY="sk-${MASTER_KEY}" \
  --from-literal=DATABASE_URL="postgresql://litellm:${POSTGRES_PASSWORD}@postgres-service:5432/litellm" \
  --from-literal=POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
  --from-literal=UI_USERNAME="admin" \
  --from-literal=UI_PASSWORD="CHANGE-ME" \
  --from-literal=HF_TOKEN="hf_..." \
  --from-literal=LDAP_BIND_DN="CN=bind-user,OU=Users,DC=example,DC=org" \
  --from-literal=LDAP_BIND_PASSWORD="CHANGE-ME" \
  --dry-run=client -o yaml > secrets.yaml

printf '\n---\n' >> secrets.yaml

kubectl create secret generic kisz-auth-wrapper-secret \
  --from-literal=AUTHENTIK_ISSUER="https://auth.example.com/application/o/kisz-llm" \
  --from-literal=AUTHENTIK_CLIENT_ID="kisz-llm" \
  --from-literal=AUTHENTIK_CLIENT_SECRET="CHANGE-ME" \
  --from-literal=AUTHENTIK_REDIRECT_URI="https://llm-portal-staging.example.com/callback" \
  --from-literal=SESSION_SECRET="${WRAPPER_SESSION_SECRET}" \
  --dry-run=client -o yaml >> secrets.yaml
```

3. Apply:
```bash
kubectl apply -n litellm -f secrets.yaml
```

For staging, apply the same file to `litellm-staging` instead:

```bash
kubectl apply -n litellm-staging -f secrets.yaml
```

The wrapper reuses `litellm-secret` for `LITELLM_MASTER_KEY` and reads its OIDC/session values from `kisz-auth-wrapper-secret`.

## Hugging Face token (gated models)

The HF token is stored in the same `litellm-secret` used by the deployments.

## LDAP bind credentials

LDAP bind DN and password should be stored in `litellm-secret` and never committed.

## Better: Use External Secrets

For production, use External Secrets Operator or your platform's secret management.
