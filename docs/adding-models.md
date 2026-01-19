# Adding Models

## Add a new model deployment

1. Create a new folder under models/.
2. Start from the template in models/_template and update placeholders.
3. Add deployment.yaml and service.yaml for the model.
4. Register the resources in models/kustomization.yaml.

Template files:

- models/_template/deployment.yaml
- models/_template/service.yaml

Required placeholders to replace:

- MODEL_NAME (matches app label and served-model-name)
- MODEL_ID (Hugging Face or local model id)
- SERVICE_NAME (Kubernetes service name)
- PORT (container/service port, default 8000)
- GPU_COUNT (number of GPUs to request)
- GPU_PRODUCT (node selector value, optional if not needed)
- MAX_MODEL_LEN (vLLM max model length)

## Register model with LiteLLM

Use scripts/add-model.sh after the model service is running:

```bash
./scripts/add-model.sh llama-3b llama-3b-service 8000
```

## Example deployment checklist

- Requests GPUs via resources.limits.nvidia.com/gpu
- Service port matches the container port
- Model name is correct for the vLLM runtime
