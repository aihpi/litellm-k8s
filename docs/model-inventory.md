# Model Inventory

Models deployed via [models/kustomization.yaml](../models/kustomization.yaml). All services listen on port 8000. Register new models with LiteLLM via [scripts/sync-models-to-db.sh](../scripts/sync-models-to-db.sh) (see [Adding Models](adding-models.md)).

| Model | Service | Notes |
| --- | --- | --- |
| llama-3b | llama-3b-service | General instruction model (in `config.yaml` model_list; manifests applied out-of-band) |
| llama-3-3-70b | llama-3-3-70b-service | Llama 3.3 70B FP8, 32K context, single H100 |
| gemma-4-31b | gemma-4-31b-service | Gemma 4 31B instruct |
| gpt-oss-120b | gpt-oss-120b-service | GPT-OSS 120B (`openai/gpt-oss-120b`, native MXFP4), 128K context, KV-cache CPU offload tier; LoRA disabled (MoE+LoRA bug, see deployment.yaml) |
| granite-4-h-tiny | granite-4-h-tiny-service | Granite 4 tiny instruct |
| ministral-3-14b | ministral-3-14b-service | 14B instruct, tensor parallel on 2x A30; LoRA adapters enabled with auto-discovery |
| qwen-3-5-9b | qwen-3-5-9b-service | Qwen 3.5 9B; thinking mode disabled proxy-wide by default |
| qwen3-vl-32b | qwen3-vl-32b-service | Qwen3 VL 32B vision-language model |
| qwen3-omni | qwen3-omni-service | Qwen3 Omni multimodal (vllm-omni image) |
| qwen-image-edit | qwen-image-edit | Image editing; routed via custom `aihpi-provider` in the LiteLLM fork |
| octen-embedding-8b | octen-embedding-8b-service | Embedding model (4096 dims) |
| qwen3-vl-embedding-8b | qwen3-vl-embedding-8b-service | Vision-language embedding model |
| minilm-embedding | minilm-embedding-service | all-MiniLM-L6-v2 on text-embeddings-inference (CPU) |
| dinov3-embeddings-api | dinov3-embeddings-api-service | Image embeddings (DINOv3); custom API (`images: [...]` payload), **not** OpenAI-compatible, not registered in LiteLLM |

Present in `models/` but not deployed (not in kustomization): `llama-70b`.
