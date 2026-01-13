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

## UI Login

Default credentials:
- Username: admin
- Password: your LITELLM_MASTER_KEY

## Contributors

- Felix Boelter (@felixboelter)
