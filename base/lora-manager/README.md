# lora-manager

Internal service that accepts LoRA adapter uploads, validates them, writes to the per-model adapters PVC, loads them into the running vLLM pod, and registers them in LiteLLM.

Not exposed externally. Reached via LiteLLM `pass_through_endpoints` (`/v1/lora/upload`, `/v1/lora/adapters`) routed from the existing `api.aisc.hpi.de` ingress.

## Building the image

`.github/workflows/build-lora-manager.yml` builds and pushes `ghcr.io/aihpi/tool-lora-manager:{sha,latest}` on every push to `main` that touches `base/lora-manager/{app,Dockerfile,requirements.txt}`. The deployment uses `imagePullPolicy: Always`, so `kubectl rollout restart deploy/lora-manager` picks up a new image without a manifest change.

To build locally for testing:

```bash
cd base/lora-manager
docker build -t lora-manager:dev .
docker run --rm -p 8000:8000 \
  -e LITELLM_MASTER_KEY=test \
  -e REQUIRE_IDENTITY_HEADERS=false \
  lora-manager:dev
# curl localhost:8000/docs
```

## Endpoints

Behind LiteLLM auth (caller authenticates with their normal `sk-...` key):

- `POST /v1/lora/upload` — multipart: `name`, `base_model`, `adapter` (tar.gz), optional `access` (LiteLLM access group)
- `GET  /v1/lora/adapters` — list deployed adapters per base model

Direct-on-service (admin only, requires kubectl exec or port-forward; user identity headers from LiteLLM are otherwise required):

- `DELETE /adapters/{base_model}/{name}` — remove from vLLM + LiteLLM + PVC

## Required environment variables

| Var | Default | Notes |
|---|---|---|
| `LITELLM_MASTER_KEY` | — | Required. From `litellm-secret`. Used for `/model/new` / `/model/delete`. |
| `LITELLM_URL` | `http://litellm-service:4000` | |
| `ADAPTERS_BASE_PATH` | `/adapters` | Per-model subdirs under here. |
| `ALLOWED_BASE_MODELS` | `ministral-3-14b` | Comma-separated allowlist. |
| `MAX_UPLOAD_BYTES` | `4294967296` (4 GiB) | |
| `MAX_LORA_RANK` | `64` | Matches `--max-lora-rank` on vLLM. Rejects higher-rank adapters. |

## Adapter contract

Uploads must be a `.tar.gz` containing a PEFT LoRA adapter:

- `adapter_config.json` with `peft_type: "LORA"` and `r <= MAX_LORA_RANK`
- One or more `*.safetensors` files (no pickled `.bin` accepted)
- Optionally `tokenizer.{json,model}`, `tokenizer_config.json`, `special_tokens_map.json`, `added_tokens.json`, `README.md`

Anything else fails validation. Safetensors are parsed header-only (no tensor data loaded into memory).

## Adapter naming

`^[a-z0-9][a-z0-9-]{0,62}$` — lowercase alphanumeric + hyphen, 1-63 chars. This is what becomes the `lora_name` in vLLM and `model_name` in LiteLLM. Whitespace would break the auto-discovery wrapper on the vLLM side.

**Team convention:** prefix with the base model name, e.g. `ministral-3-14b-therapy-depression-v1` or `gemma-4-31b-writing-assistant`. The prefix makes it obvious which base model an adapter belongs to when scanning `/v1/models`. Not enforced by validation — humans agree on it.
