# Model Pricing

Per-token costs registered with LiteLLM so that every request writes a real
`spend` value to `LiteLLM_SpendLogs`. That spend column is what demand and
cost-recovery modelling reads — without costs, every row is `0` and usage data is
untyped token counts only.

**These are reference market rates, not our cost.** Each figure is what the
equivalent model costs on commercial serverless APIs. We run these models on our
own GPUs, so the true marginal cost is GPU-hours, not tokens. Nobody invoices us
these amounts.

### Read `spend` as a relative index, not euros

What the rates actually do is **weight** token counts so a 70B request and a 3B
request are comparable in a single column. The ordering is the payload; the
absolute value is arbitrary. `SUM(spend) = €47` is not a number to put in a
budget request — it means "this much weighted usage", nothing more.

Two consequences worth knowing:

- **For per-model usage you don't need these rates at all.** `LiteLLM_SpendLogs`
  logs `model`, `prompt_tokens` and `completion_tokens` per request regardless of
  cost. The rates only matter when collapsing *several* models into one number —
  per team, per key, per user.
- **Budgets are denominated in this unit.** LiteLLM compares a key's or team's
  `max_budget` against accumulated `spend`. At cost `0` a key cannot be capped, so
  having non-zero rates is what makes quota enforcement possible at all.

If real cost recovery ever becomes the goal, the better basis is GPU-time —
`endTime - startTime` is already logged per request, so
`duration × GPUs × €/GPU-hour` yields actual euros without token prices. The
plumbing is identical to what's here; only the constants change. (Caveat:
continuous batching means concurrent requests share a GPU, so summing per-request
durations overcounts under load.)

Survey date: **2026-07-30**. See [Refreshing the numbers](#refreshing-the-numbers).

## Rates

USD per 1M tokens. `n` = number of serverless providers surveyed.

| Our model | Reference model priced | In | Out | n | Basis |
| --- | --- | --: | --: | --: | --- |
| `llama-3b` | meta-llama/Llama-3.2-3B-Instruct | 0.05 | 0.33 | 2 | median |
| `llama-3-3-70b` | meta-llama/Llama-3.3-70B-Instruct | 0.293 | 0.72 | 13 | median (in 0.10–1.04, out 0.32–2.25) |
| `gemma-4-31b` | google/gemma-4-31b-it | 0.14 | 0.40 | 18 | median (in 0.09–0.99, out 0.34–1.49) |
| `gpt-oss-120b` | openai/gpt-oss-120b | 0.14 | 0.60 | 19 | median (in 0.03–0.35, out 0.17–0.95) |
| `granite-4-h-tiny` | ibm-granite/granite-4.1-8b | 0.05 | 0.10 | 1 | **proxy** — see note |
| `ministral-3-14b` | mistralai/ministral-14b-2512 | 0.20 | 0.20 | 1 | sole provider |
| `qwen-3-5-9b` | qwen/qwen3.5-9b | 0.10 | 0.15 | 5 | median (in 0.10–0.17, out 0.15–0.25) |
| `qwen3-vl-32b` | qwen/qwen3-vl-32b-instruct | 0.104 | 0.416 | 1 | sole provider |
| `qwen3-omni` | qwen/qwen3-omni-30b-a3b-instruct | 0.90 | 0.90 | 1 | **weakest figure** — see note |
| `octen-embedding-8b` | qwen/qwen3-embedding-8b | 0.04 | 0 | 5 | median (0.01–0.10) |
| `qwen3-vl-embedding-8b` | qwen/qwen3-embedding-8b | 0.04 | 0 | 5 | **proxy** — same as above |
| `minilm-embedding` | sentence-transformers/all-MiniLM-L6-v2 | 0.005 | 0 | 1 | sole provider |
| `qwen-image-edit` | image gen/edit market average | $0.04/image | — | 18 | mean of mainstream image APIs |

`dinov3-embeddings-api` is unpriced because it is not registered with LiteLLM at
all — its `images: [...]` request schema isn't OpenAI-compatible, so it can't be
routed through `openai/*` params. It is deployed and serving, but **all of its
usage is invisible**: no spend rows, and no request or token rows either. Any
traffic to it is missing from every query in [Cost Tracking](cost-tracking.md).
Fixing that needs an OpenAI-shaped adapter in front of it, not a price.

### Keep config.yaml and the catalog in sync

Seven models are defined in *both* `base/litellm/configmap.yaml` and the DB. The
router de-duplicates deployments by `model_info.id`, **not** by `model_name`, so
these arrive as two separate deployments sharing one name — and the router
load-balances across them. Both point at the same `api_base`, so responses are
identical, but each carries its own cost fields.

The consequence: if the config and DB costs disagree, spend for that model varies
per request depending on which deployment the router picked. That's why both
sources carry identical values here, and why the drift check in
[Refreshing the numbers](#refreshing-the-numbers) is worth running after any edit.

For the same reason, don't add the six DB-only models (`granite-4-h-tiny`,
`minilm-embedding`, `ministral-3-14b`, `qwen3-omni`, `qwen3-vl-32b`,
`qwen3-vl-embedding-8b`) to the configmap. They are fully priced via
[model-catalog.json](../scripts/model-catalog.json); listing them in both places
would only create duplicate deployments and a second place for costs to drift.

### Method

Median across every serverless provider hosting the *same open-weight
checkpoint*, taken from OpenRouter's per-provider endpoint API. Median rather
than mean because provider spreads are wide and skewed — `llama-3-3-70b` ranges
$0.10–$1.04 on input across 13 providers, where the mean ($0.42) sits above 9 of
them. Where only one provider lists a model, that price is used as-is and `n=1`
flags the thinner evidence.

Our deployments are mostly quantized (FP8, or AWQ 4-bit for `qwen-3-5-9b`) while
the reference prices are for the full-precision model. That is deliberate: the
question is what the *model* costs to rent, not what our specific quantization
costs to serve.

### Caveats worth knowing before you trust a number

- **`granite-4-h-tiny`** — `ibm-granite/granite-4.0-h-tiny` has no serverless
  listing anywhere we could find. Priced off `granite-4.1-8b`, its nearest
  sibling with a published rate. For reference, `granite-4.0-h-micro` (smaller)
  is $0.017/$0.112, so this is likely an over-estimate on input.
- **`qwen3-omni`** — single source, and the least reliable entry in the table.
  $0.90/$0.90 is a steep premium over the non-omni `qwen3-30b-a3b`
  ($0.12/$0.50); the reasoning variant is listed at $0.25/$0.97 elsewhere.
  Separately, omni models bill **audio and video far above text**, and a flat
  per-token rate cannot express that — audio-heavy usage will be under-counted.
  Revisit if `qwen3-omni` becomes a significant share of traffic.
- **Embeddings** — the $0.01–$0.10 spread across providers is 10x, so the $0.04
  median is a weak central estimate. `qwen3-vl-embedding-8b` has no listing of
  its own and borrows the text-embedding rate, which likely understates the cost
  of image inputs.
- **`qwen-image-edit`** — billed per image, so it has no input/output token
  costs. $0.04/image is the market average across 18 mainstream image gen/edit
  APIs (mean $0.0438, median $0.0375, range $0.005–$0.14) rather than a single
  vendor's rate for Qwen-Image-Edit specifically — the spread across providers is
  far wider than the difference between models, so an average is the more stable
  estimate. Set via `input_cost_per_image`, which LiteLLM checks *before*
  `input_cost_per_pixel` and which needs no height/width from the response — that
  matters because this model routes through the fork's custom `aihpi-provider`.
  **Whether cost tracking fires for it at all is still unverified** — confirm
  against a real `LiteLLM_SpendLogs` row before relying on its spend figures.

## How the costs reach the database

Costs live in **`litellm_params`**, not `model_info`. Both placements work for
config-defined models, but `POST /model/update` — the only way to backfill a
model already registered in Postgres — persists `litellm_params` only; it reads
`model_info` purely to resolve `model_info.id`. One placement for both paths
keeps the config and the DB catalog directly comparable.

LiteLLM's router copies every `CustomPricingLiteLLMParams` field out of
`litellm_params` into its in-memory model cost map at load time, which is what
the cost calculator consults when writing spend.

Two properties that matter operationally:

- **Floats are stored unencrypted.** `litellm_params` values are normally
  encrypted with `LITELLM_SALT_KEY`, but the encryption helper passes non-string
  values through untouched. Costs therefore land in Postgres as plain JSON
  numbers — queryable by SQL, and unaffected by a salt-key rotation (unlike
  `api_key`, see [Adding Models](adding-models.md)).
- **Write a decimal point.** PyYAML parses `5e-8` as the *string* `"5e-8"` and
  only `5.0e-8` as a float. The catalog and configmap both use the
  decimal-point form throughout; keep it that way.

There are two sources of truth, matching the two ways a model can be registered:

| Where the model is defined | Where its cost goes | Applied by |
| --- | --- | --- |
| [base/litellm/configmap.yaml](../base/litellm/configmap.yaml) `model_list` | same entry's `litellm_params` | `kubectl apply -k base` + pod restart |
| Postgres (`/model/new`, UI) | [scripts/model-catalog.json](../scripts/model-catalog.json) | [scripts/set-model-costs.sh](../scripts/set-model-costs.sh) |

`model-catalog.json` is also what [sync-models-to-db.sh](../scripts/sync-models-to-db.sh)
registers from, so newly-added models arrive with costs already attached.

### Applying costs to already-registered models

The 13 catalog models are already in Postgres, so `/model/new` skips them —
`set-model-costs.sh` backfills via `/model/update`:

```bash
export LITELLM_MASTER_KEY=<master-key>
export LITELLM_URL=https://api.aisc.hpi.de
./scripts/set-model-costs.sh --dry-run   # preview
./scripts/set-model-costs.sh
```

It reports three outcomes per model: `update` (DB row rewritten), `config`
(config.yaml-backed — `/model/update` rejects these, set the cost in the
configmap instead), and `skip` (not registered with the proxy). A model listed in
both places has two rows and only the DB one is updated — but the router
load-balances across both deployments and each prices with its own fields, so
the configmap value must be updated to match or that model's spend will vary
per request. See [Keep config.yaml and the catalog in sync](#keep-configyaml-and-the-catalog-in-sync).

Restart is not required — `/model/update` refreshes the router in place. Config
map changes do need a rollout:

```bash
kubectl apply -k base && kubectl rollout restart deploy/litellm -n litellm
```

### Verifying it worked

```bash
# Costs the proxy currently has loaded
curl -sS https://api.aisc.hpi.de/model/info -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  | jq -r '.data[] | [.model_name,
      ((.litellm_params.input_cost_per_token  // .model_info.input_cost_per_token  // 0) * 1e6),
      ((.litellm_params.output_cost_per_token // .model_info.output_cost_per_token // 0) * 1e6)] | @tsv'
```

Then make one real request and confirm the spend row is non-zero — this is the
only check that proves end-to-end tracking, since a loaded cost that never
reaches the calculator still yields `spend = 0`:

```sql
SELECT model, prompt_tokens, completion_tokens, spend, "startTime"
FROM "LiteLLM_SpendLogs"
ORDER BY "startTime" DESC
LIMIT 5;
```

## Refreshing the numbers

Provider pricing moves; re-survey when you need current figures. OpenRouter's
API needs no key and lists every provider for a model:

```bash
# All providers for one model, priced per 1M tokens
curl -sS https://openrouter.ai/api/v1/models/openai/gpt-oss-120b/endpoints \
  | jq -r '.data.endpoints[] | "\(.provider_name)\tin=\((.pricing.prompt|tonumber)*1e6)\tout=\((.pricing.completion|tonumber)*1e6)"'

# Find the reference model id for a checkpoint
curl -sS https://openrouter.ai/api/v1/models \
  | jq -r '.data[] | select(.id|test("gemma";"i")) | .id'
```

Take the median of the input and output columns, divide by 1e6, and update
`model-catalog.json` **and** the configmap entry for that model — they must agree.
The check below fails loudly if they drift:

```bash
python3 -c '
import json, yaml, pathlib
cfg = yaml.safe_load(yaml.safe_load(pathlib.Path("base/litellm/configmap.yaml").read_text())["data"]["config.yaml"])
cat = {m["model_name"]: {k: v for k, v in m["litellm_params"].items() if "cost" in k}
       for m in json.loads(pathlib.Path("scripts/model-catalog.json").read_text())["models"]}
bad = [m["model_name"] for m in cfg["model_list"]
       if {k: v for k, v in m["litellm_params"].items() if "cost" in k} != cat.get(m["model_name"])]
print("drifted:", bad or "none")
assert not bad'
```

Also update the survey date and any `n`/range figures in the table above, so the
next person can tell how stale the numbers are.

## Sources

- [OpenRouter models API](https://openrouter.ai/api/v1/models) — primary source; per-provider rates via `/api/v1/models/{author}/{slug}/endpoints`
- [Qwen3.5 9B pricing](https://pricepertoken.com/pricing-page/model/qwen-qwen3.5-9b) and [DeepInfra's Qwen3.5 9B benchmarks](https://deepinfra.com/blog/qwen3-5-9b-api-benchmarks)
- [Qwen3 Omni 30B A3B Instruct pricing](https://pricepertoken.com/pricing-page/model/qwen-qwen3-omni-30b-a3b-instruct)
- [Granite 4.0 H Micro on OpenRouter](https://openrouter.ai/ibm-granite/granite-4.0-h-micro) and [Granite 4.0 H Small analysis](https://artificialanalysis.ai/models/granite-4-0-h-small)
- [Qwen3 Embedding 8B on OpenRouter](https://openrouter.ai/qwen/qwen3-embedding-8b); [Novita](https://www.getmaxim.ai/bifrost/llm-cost-calculator/provider/novita/model/qwen3-embedding-8b) and [Scaleway](https://custom.typingmind.com/tools/estimate-llm-usage-costs/scaleway/qwen3-embedding-8b) rates
- [all-MiniLM-L6-v2 on OpenRouter](https://openrouter.ai/sentence-transformers/all-minilm-l6-v2)
- [Qwen Image Edit on fal.ai](https://fal.ai/models/fal-ai/qwen-image-edit)
- [Octen-Embedding-8B model card](https://huggingface.co/Octen/Octen-Embedding-8B) — confirms it is fine-tuned from Qwen3-Embedding-8B
- [LiteLLM custom pricing docs](https://docs.litellm.ai/docs/proxy/custom_pricing)
