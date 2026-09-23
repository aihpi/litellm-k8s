# Clusters: default vs. aisc

This repo now targets two Kubernetes clusters:

| | **default** (`overlays/prod`) | **aisc** (`overlays/aisc`) |
| --- | --- | --- |
| GPUs | Mixed: A30 (24GB) + H100 (80GB), selected per-model via `nodeSelector: accelerator: {a30,h100}` | A30 (24GB) only — no `h100`-selected Deployment will ever schedule |
| Models deployed | All 16 in [models/kustomization.yaml](../models/kustomization.yaml) | The 9 whose `deployment.yaml` selects `accelerator: a30` or `node-type: cpu` (see table below) |
| `litellm-config` `model_list` | Full 7 config-registered rows (`base/litellm/configmap.yaml`) | Trimmed to the 3 A30 rows via [overlays/aisc/patches/litellm-aisc-configmap.yaml](../overlays/aisc/patches/litellm-aisc-configmap.yaml) |
| Auth / networking extras | KISZ Auth Wrapper (Authentik SSO), fixed `loadBalancerIP`, ingress-metrics patch, OpenTela/Slurm tunnel for external H100 workers | None — plain `base/` stack (LiteLLM, Postgres, Redis, Qdrant, nginx, lora-manager). Add these back per-cluster if aisc needs them |
| Secrets | Sealed Secrets (`overlays/prod/sealed-secrets/`), cluster-specific | Plaintext `secrets/secrets.yaml` applied by `scripts/deploy.sh`, same as `dev` today |
| Total GPUs requested (replicas=1) | 12× A30 + 7× H100 (see below) | 12× A30 |

> **Naming note:** `docs/adding-models.md` and `docs/opentela-slurm.md` already use
> "aisc" for *this repo's existing* cluster (`ssh aisc-deploy@lx04`,
> `api.aisc.hpi.de`) — which currently **does** have H100 nodes (several
> models below select `accelerator: h100` and are documented as "single
> H100"). `overlays/aisc` in this repo is a distinct, new A30-only cluster
> per this task's request, not a rename of the existing one. If it turns out
> to be the same physical cluster, reconcile the two before deploying —
> otherwise `overlays/aisc` will point A30 model PVCs/Services at a
> namespace that already has H100 Deployments defined by `overlays/prod`,
> and you'll want to decide which overlay owns that cluster.

## Model placement

| Model | Accelerator | GPUs/replica | In `overlays/aisc`? |
| --- | --- | --- | --- |
| granite-4-h-tiny | a30 | 1 | ✅ |
| dinov3-embeddings-api | a30 | 1 | ✅ |
| octen-embedding-8b | a30 | 1 | ✅ |
| qwen3-vl-embedding-8b | a30 | 1 | ✅ |
| qwen3-reranker-4b | a30 | 1 | ✅ |
| ministral-3-14b | a30 | 2 (tensor-parallel) | ✅ |
| gemma-4-31b | a30 | 4 (tensor-parallel) | ✅ |
| qwen-3-5-9b | a30 | 1 | ✅ |
| minilm-embedding | cpu | 0 (CPU inference) | ✅ |
| llama-3-3-70b | h100 | 1 | ❌ — 70B FP8 needs the 80GB card |
| gpt-oss-120b | h100 | 1 | ❌ — 120B MXFP4 needs the 80GB card |
| muse-glimmer-30b | h100 | 1 | ❌ — pinned to Hopper via `--config .../Muse-Glimmer_Hopper.yaml`, non-portable to A30 |
| qwen3-8-27b | h100 | 1 | ❌ — 27B FP8, sized for 80GB headroom |
| qwen3-omni | h100 | 1 | ❌ — multimodal, uses the vllm-omni image sized for Hopper |
| qwen3-vl-32b | h100 | 1 | ❌ — 32B VL model |
| qwen-image-edit | h100 | 1 | ❌ — image editing, sized for Hopper |

**aisc GPU total (replicas=1):** 1+1+1+1+1+2+4+1 = **12× A30**.
**default cluster GPU total (replicas=1, excludes prod's parked 0-replica rows):** the same 12× A30 (same 8 GPU-using models) **plus** 1+1+1+1+1+1+1 = **7× H100** across the 7 H100-only models.

None of the H100-only models are re-sized for A30 here (e.g. lower
`tensor-parallel-size` to fit multiple 24GB cards) — they're excluded
outright. If aisc needs `llama-3-3-70b`-class coverage, that requires a
model-specific quantization/sharding rework (see
[docs/adding-models.md](adding-models.md#example-llama-33-70b-on-hopper-single-gpu)
for how tight the 70B FP8 KV-cache budget already is on an 80GB card —
splitting it across A30s needs `tensor-parallel-size` raised and
`max-model-len`/`max-num-seqs` re-tuned, not just a nodeSelector swap).

## Why the model list can't just be `../../models` filtered

`overlays/aisc/kustomization.yaml` lists 9 model directories individually
rather than reusing `../../models` (what `overlays/prod` and `overlays/dev`
do) plus some exclusion, because kustomize has no "include this aggregate
minus these resources" primitive, and there's no `replicas: 0` escape hatch
here either — a `nodeSelector: accelerator: h100` Deployment scaled to 0 on
an A30-only cluster is silent, but it's still applied config drift no one
asked for, unlike `overlays/prod`'s parked-at-0 rows (which *are* real
prod deployments, just paused). Each of the 9 A30/CPU model directories got
its own small `kustomization.yaml` ([example](../models/granite-4-h-tiny/kustomization.yaml))
so `overlays/aisc` can reference the directory — kustomize's default load
restrictor blocks a raw file reference that reaches outside the referencing
kustomization's own directory tree, but a directory containing its own
`kustomization.yaml` is a valid cross-boundary target (the same mechanism
`../../models` already relies on).

## Deploying

```bash
kubectl config use-context <aisc-cluster-context>
./scripts/deploy.sh aisc
```

`scripts/deploy.sh` skips the blanket `kubectl apply -k models/` step for
`ENV=aisc` specifically (it would otherwise apply all 16 models, H100 ones
included) — `overlays/aisc/` carries its own filtered model list instead.

Before the first real deploy, verify these cluster-specific values that were
left as-is (inherited from the H100+A30 default cluster) because this repo
has no way to know the aisc cluster's actual values:

- **`storageClassName: nfs-k8s-general`** on every A30 model PVC and
  `base/postgres/pvc.yaml` — confirm aisc has a StorageClass by that name,
  or add a patch overriding it.
- **`nodeSelector: accelerator: a30`** — confirm aisc's nodes actually carry
  that label (`kubectl get nodes --show-labels`), or patch the selector key/value.
- **`secrets/secrets.yaml`** — needs its own values for aisc (DB password,
  `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `HF_TOKEN`, Redis password); see
  [secrets/README.md](../secrets/README.md).
- **DB-only models** (`granite-4-h-tiny`, `ministral-3-14b`,
  `qwen3-vl-embedding-8b`, `qwen3-reranker-4b`) aren't in `config.yaml` on
  either cluster — register them against aisc's own LiteLLM/Postgres with
  `scripts/sync-models-to-db.sh` after the first deploy, same as prod (see
  [docs/adding-models.md](adding-models.md#6-register-with-litellm)).
- `nginx-proxy`'s `Service` stays `LoadBalancer` with no fixed IP (base
  default) — prod pins `loadBalancerIP` in a patch this overlay
  deliberately omits; add one once aisc's IP is known.
