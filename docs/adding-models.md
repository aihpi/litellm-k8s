# Adding Models

How to deploy a new vLLM model and register it with the LiteLLM gateway.

## 1. Create model manifests

Create a new folder under `models/<model-name>/`. Start from `models/_template/` and update the placeholders.

Each model directory should contain:

| File              | Purpose                                                            |
| ----------------- | ------------------------------------------------------------------ |
| `deployment.yaml` | vLLM container spec with GPU requests                              |
| `service.yaml`    | ClusterIP service exposing the vLLM API (default port 8000)        |
| `configmap.yaml`  | vLLM serving args: model ID, `max-model-len`, `max-num-seqs`, etc. |
| `pvc.yaml`        | PersistentVolumeClaim for HuggingFace model cache                  |

**Placeholders to replace:**

| Placeholder     | Description                       | Example                             |
| --------------- | --------------------------------- | ----------------------------------- |
| `MODEL_NAME`    | App label and `served-model-name` | `llama-3-3-70b`                     |
| `MODEL_ID`      | HuggingFace repo ID               | `nvidia/Llama-3.3-70B-Instruct-FP8` |
| `SERVICE_NAME`  | Kubernetes service name           | `llama-3-3-70b-service`             |
| `PORT`          | Container/service port            | `8000`                              |
| `GPU_COUNT`     | `nvidia.com/gpu` resource limit   | `1`                                 |
| `GPU_PRODUCT`   | Node selector value (optional)    | —                                   |
| `MAX_MODEL_LEN` | vLLM `--max-model-len`            | `32000`                             |

**Important:** Always set `MAX_MODEL_LEN` explicitly. The model's default context length often exceeds available GPU memory for KV cache, causing the pod to crash on startup (see [Troubleshooting](#troubleshooting)).

Register the new directory in `models/kustomization.yaml`, commit, and push.

## 2. Deploy

```bash
ssh aisc-deploy@lx04
cd ~/k8-deployments/litellm-k8s
git pull
kubectl apply -k models
```

This creates/updates the ConfigMap, Service, PVC, and Deployment for all models.

## 3. Check pod status

```bash
kubectl get pods -n litellm
```

If the new pod shows `Running` with `1/1` ready, skip to [step 5](#5-register-with-litellm).

## 4. Debug failures

If the pod shows `Error` or `CrashLoopBackOff`:

```bash
kubectl logs <pod-name> -n litellm
```

### Wrong HuggingFace model ID

```
RepositoryNotFoundError: 404 Client Error.
Repository Not Found for url: https://huggingface.co/openai/llama-3-3-70b/resolve/main/config.json
```

The `MODEL_ID` in the deployment or configmap doesn't exist on HuggingFace. Fix the model ID (e.g., `nvidia/Llama-3.3-70B-Instruct-FP8`), push, then:

```bash
git pull
kubectl apply -k models
```

### KV cache out of memory

```
ValueError: To serve at least one request with the model's max seq len (131072),
20.0 GiB KV cache is needed, which is larger than the available KV cache memory (5.11 GiB).
```

The default context length is too large for available VRAM after loading weights. Add or lower `--max-model-len` in the configmap (e.g., `32000`) and reduce `--max-num-seqs` if needed (e.g., `8`). Push the fix, then:

```bash
git pull
kubectl apply -k models
```

### ConfigMap changes not taking effect

If only the ConfigMap changed but the Deployment spec didn't, Kubernetes won't restart the pod. Force it:

```bash
kubectl rollout restart deploy/<model-name> -n litellm
```

## 5. Verify vLLM startup

Follow the logs until the server is ready:

```bash
kubectl logs <pod-name> -n litellm -f
```

Look for these lines (in order):

```
Loading weights took X seconds
Model loading took X GiB memory
Available KV cache memory: X GiB
GPU KV cache size: X tokens
Maximum concurrency for X tokens per request: X.XXx
Starting vLLM API server on http://0.0.0.0:8000
```

If the model weights are not yet cached on the PVC, the initial download can take 5–30 minutes depending on model size. Subsequent restarts use the cache.

Confirm the service is reachable:

```bash
kubectl get svc -n litellm
```

## 6. Register with LiteLLM

The vLLM pod is now serving, but the LiteLLM gateway doesn't know about it yet. Register the model using one of these methods:

### Option A: LiteLLM UI (recommended)

1. Get the UI password:
   ```bash
   kubectl get secret -n litellm litellm-secret -o jsonpath='{.data.UI_PASSWORD}' | base64 --decode
   ```
2. Go to `http://api.aisc.hpi.de/ui/login/` and log in.
3. Add the model through the UI.

### Option B: `add-model.sh` script

```bash
export LITELLM_MASTER_KEY=<your-master-key>
export LITELLM_URL=http://api.aisc.hpi.de
./scripts/add-model.sh <model-name> <service-name> [port]
```

Example:

```bash
./scripts/add-model.sh llama-3-3-70b llama-3-3-70b-service 8000
```

The script calls `/model/new` on the LiteLLM proxy, which stores the registration in Postgres. This is a **one-time step** per model — you don't need to re-register after redeploying or restarting the vLLM pod.

## 7. Test

```bash
curl -sS -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3-3-70b","messages":[{"role":"user","content":"Hello"}]}' \
  http://api.aisc.hpi.de/v1/chat/completions
```

## Example: Llama 3.3 70B on Hopper (single GPU)

This example walks through the actual deployment of `llama-3-3-70b` on a single H100 GPU, based on the [vLLM Hopper recipe](https://docs.vllm.ai/projects/recipes/en/latest/Llama/Llama3.3-70B.html).

### vLLM recipe baseline

The official recipe provides a `Llama3.3_Hopper.yaml` config:

```yaml
kv-cache-dtype: fp8
async-scheduling: true
no-enable-prefix-caching: true
max-num-batched-tokens: 8192
```

With model `nvidia/Llama-3.3-70B-Instruct-FP8` and `tensor-parallel-size: 1`. This assumes enough VRAM for the full 131K context window.

### What we had to change

The model weights consume ~67.7 GiB, leaving only ~5.1 GiB for KV cache on an 80 GiB GPU. The default `max_seq_len` of 131,072 requires ~20 GiB of KV cache — so the pod crashed on startup with:

```
ValueError: To serve at least one request with the model's max seq len (131072),
20.0 GiB KV cache is needed, which is larger than the available KV cache memory (5.11 GiB).
```

Fix: explicitly set `max-model-len` and reduce `max-num-seqs` in the configmap:

```yaml
# models/llama-3-3-70b/configmap.yaml
kv-cache-dtype: fp8
async-scheduling: true
no-enable-prefix-caching: true
max-num-batched-tokens: 8192
max-model-len: 32000
max-num-seqs: 8
```

### Result

With these settings, the engine reports:

```
Model loading took 67.7 GiB memory and 30.47 seconds
Available KV cache memory: 5.11 GiB
GPU KV cache size: 33,504 tokens
Maximum concurrency for 32,000 tokens per request: 1.05x
```

This means the deployment can handle roughly one full-context (32K) request at a time. For higher concurrency, either lower `max-model-len` further (e.g., 16K gives ~2x concurrency) or use `tensor-parallel-size: 2` across two GPUs to double the available KV cache memory.

### Deployment timeline

The first deploy took ~30 minutes because the model weights (~70 GiB) had to be downloaded from HuggingFace. After that, the PVC cache cut subsequent cold starts to ~30 seconds for weight loading plus ~26 seconds for `torch.compile` warmup.

### Mistakes made along the way

1. **Wrong model ID**: initially configured as `openai/llama-3-3-70b` (doesn't exist). Fixed to `nvidia/Llama-3.3-70B-Instruct-FP8`.
2. **Missing `max-model-len`**: used the recipe as-is without accounting for limited KV headroom. Added `max-model-len: 32000`.
3. **ConfigMap update didn't restart the pod**: had to run `kubectl rollout restart` manually after the configmap fix.

## Deployment checklist

- [ ] Model directory created under `models/` with deployment, service, configmap, and PVC
- [ ] Registered in `models/kustomization.yaml`
- [ ] `MODEL_ID` is a valid HuggingFace repo (or local path)
- [ ] `--max-model-len` set explicitly in configmap
- [ ] GPU requests match available hardware
- [ ] Service port matches container port
- [ ] `kubectl apply -k models` succeeds
- [ ] Pod reaches `Running 1/1`
- [ ] vLLM logs show `Starting vLLM API server`
- [ ] Model registered with LiteLLM (UI or script)
- [ ] `curl` test returns a valid response

## Troubleshooting

| Symptom                           | Cause                                | Fix                                              |
| --------------------------------- | ------------------------------------ | ------------------------------------------------ |
| `RepositoryNotFoundError: 404`    | Wrong HuggingFace model ID           | Fix `MODEL_ID` in deployment/configmap           |
| `KV cache memory` ValueError      | `max_seq_len` too large for GPU VRAM | Lower `--max-model-len`, reduce `--max-num-seqs` |
| Pod in `ContainerCreating`        | PVC not bound or image pull issue    | `kubectl describe pod <pod> -n litellm`          |
| `CrashLoopBackOff`                | Repeated startup failures            | Check logs, fix config, rollout restart          |
| ConfigMap change ignored          | Pod not restarted                    | `kubectl rollout restart deploy/<n> -n litellm`  |
| Slow first startup (10–30 min)    | Downloading weights from HuggingFace | Wait; PVC caches for next time                   |
| Model not in LiteLLM `/v1/models` | Not registered with gateway          | Register via UI or `add-model.sh`                |

## Useful commands

```bash
kubectl get pods -n litellm
kubectl get deploy -n litellm
kubectl get svc -n litellm
kubectl describe pod <pod-name> -n litellm
kubectl logs <pod-name> -n litellm -f
kubectl rollout restart deploy/<name> -n litellm
kubectl scale deploy/<name> -n litellm --replicas=N
kubectl get secret -n litellm litellm-secret -o jsonpath='{.data.UI_PASSWORD}' | base64 --decode
```
