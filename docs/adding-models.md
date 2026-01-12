# Adding Models

## Add a new model deployment

1. Create a new folder under models/.
2. Add deployment.yaml and service.yaml for the model.
3. Register the resources in models/kustomization.yaml.

## Register model with LiteLLM

Use scripts/add-model.sh after the model service is running:

```bash
./scripts/add-model.sh llama-3b llama-3b-service 8000
```

## Example deployment checklist

- Requests GPUs via resources.limits.nvidia.com/gpu
- Service port matches the container port
- Model name is correct for the vLLM runtime
