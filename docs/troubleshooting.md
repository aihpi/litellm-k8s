# Troubleshooting

## LiteLLM not reachable

- Check service: kubectl get svc -n litellm
- Check logs: kubectl logs -n litellm deployment/litellm-proxy -f
- Verify port-forward: kubectl port-forward -n litellm service/litellm-service 4000:4000

## Model pod not ready

- Check GPU resources: kubectl describe pod -n litellm -l app=llama-3b
- Ensure the node has available GPUs
- Verify image pull and model download logs

## Database connection errors

- Ensure postgres pod is running
- Validate secrets in secrets/secrets.yaml
- Confirm DATABASE_URL matches service name and credentials
