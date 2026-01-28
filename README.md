# LiteLLM Kubernetes Deployment

AI model gateway

## Quick Start

```bash
# 1. Clone repo
git clone https://github.com/hpi/litellm-k8s.git
cd litellm-k8s

# 2. Create secrets (see secrets/README.md)
cp secrets/example-secrets.yaml secrets/secrets.yaml
# Edit secrets/secrets.yaml with your values

# 3. Deploy
./scripts/deploy.sh dev

# 4. Port-forward
kubectl port-forward -n litellm service/litellm-service 4000:4000

# 5. Access UI
open http://localhost:4000/ui/login/
```

## Architecture

```
Internet -> Ingress -> LiteLLM (Gateway)
                          |
                          v
                    ClusterIP Services
                          |
                          v
                    vLLM Model Pods
```

## Adding Models

See docs/adding-models.md

## Infrastructure

- Cluster: HPI K8s (40x A30)
- Namespace: litellm
- GPU Scheduling: Uses GPU requests in model deployments

## Maintenance

- Logs: kubectl logs -n litellm deployment/litellm-proxy -f
- Restart: kubectl rollout restart -n litellm deployment/litellm-proxy
- Scale: kubectl scale -n litellm deployment/llama-3b --replicas=2

## Handoff / Recent Changes

- Added scripts:
  - `scripts/call_qwen_image_edit.py` (image edit via LiteLLM `/v1/images/edits`)
  - `scripts/test_octen_embedding.py` (embeddings via LiteLLM `/v1/embeddings`)
- Added `octen-embedding-8b` to LiteLLM model list (default `encoding_format: float`).
- Added `models/gpt-oss-120b` (deployment/service/pvc) with vLLM config mounted from
  `models/gpt-oss-120b/configmap.yaml` using `GPT-OSS_EAGLE3_Hopper.yaml`.
  (Note: model is not yet added to LiteLLM proxy config.)

Apply model resources:

```bash
kubectl apply -k models
```

## Calling the API (via LiteLLM)

Port-forward in dev or access via your ingress.

```bash
kubectl port-forward -n litellm service/litellm-service 4000:4000
```

### Chat/completions (example)

```bash
curl -sS -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3b","messages":[{"role":"user","content":"Hello"}]}' \
  http://localhost:4000/v1/chat/completions
```

### Embeddings (octen-embedding-8b)

```bash
curl -sS -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"octen-embedding-8b","input":"Hello from octen","encoding_format":"float"}' \
  http://localhost:4000/v1/embeddings
```

Or run:

```bash
LITELLM_API_KEY=sk-... python3 scripts/test_octen_embedding.py
```

### Image edits (qwen-image-edit)

```bash
LITELLM_API_KEY=sk-... python3 scripts/call_qwen_image_edit.py \
  --api-base http://localhost:4000 \
  --prompt "Remove the sleeves; keep fabric/lighting unchanged"
```

## UI Login

Default credentials:
- Username: admin
- Password: your LITELLM_MASTER_KEY

## Contributors

- Felix Boelter (@felixboelter)
