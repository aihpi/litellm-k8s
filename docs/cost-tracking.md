# Cost Tracking

Every model registered with LiteLLM carries a per-token cost, so each request
writes a `spend` value to the `LiteLLM_SpendLogs` table in Postgres. That table is
the basis for demand modelling, chargeback, and quota sizing.

The rates themselves — what they mean, where each number came from, and how to
refresh them — are in [Model Pricing](model-pricing.md). Read that before drawing
conclusions from any spend figure: the costs are **reference market rates for the
equivalent hosted model**, not our GPU cost, so `spend` is a weighted usage index
rather than euros. For plain per-model usage you don't need it — the token columns
below are logged independently of cost.

## Querying spend

`LiteLLM_SpendLogs` has one row per request. The columns that matter for demand
modelling are `model`, `prompt_tokens`, `completion_tokens`, `spend`, `startTime`,
`api_key` (hashed), `user`, and `team_id`.

Open a psql session against the in-cluster Postgres:

```bash
ssh aisc-deploy@lx04
kubectl exec -it -n litellm deploy/postgres -- psql -U litellm -d litellm
```

Spend and volume per model over the last 30 days:

```sql
SELECT model,
       COUNT(*)                        AS requests,
       SUM(prompt_tokens)              AS input_tokens,
       SUM(completion_tokens)          AS output_tokens,
       ROUND(SUM(spend)::numeric, 4)   AS usd,
       ROUND(AVG(spend)::numeric, 6)   AS usd_per_request
FROM "LiteLLM_SpendLogs"
WHERE "startTime" > NOW() - INTERVAL '30 days'
GROUP BY model
ORDER BY usd DESC;
```

Demand curve by day and model:

```sql
SELECT DATE_TRUNC('day', "startTime") AS day, model,
       SUM(prompt_tokens + completion_tokens) AS tokens,
       ROUND(SUM(spend)::numeric, 4)          AS usd
FROM "LiteLLM_SpendLogs"
WHERE "startTime" > NOW() - INTERVAL '90 days'
GROUP BY day, model
ORDER BY day, usd DESC;
```

Spend per team, for chargeback:

```sql
SELECT COALESCE(team_id, '(none)') AS team, model,
       COUNT(*) AS requests, ROUND(SUM(spend)::numeric, 4) AS usd
FROM "LiteLLM_SpendLogs"
WHERE "startTime" > NOW() - INTERVAL '30 days'
GROUP BY team, model
ORDER BY usd DESC;
```

The costs are stored as plain JSON numbers on the model row, so you can join
against the configured rate directly:

```sql
SELECT model_name,
       (litellm_params->>'input_cost_per_token')::float8  * 1e6 AS usd_per_1m_in,
       (litellm_params->>'output_cost_per_token')::float8 * 1e6 AS usd_per_1m_out
FROM "LiteLLM_ProxyModelTable";
```

This works only because floats bypass the `litellm_params` encryption path;
string fields like `api_key` in the same column are ciphertext. See
[Model Pricing](model-pricing.md#how-the-costs-reach-the-database).

## If spend is zero

A `spend` of `0` on rows that have non-zero token counts means the model has no
cost registered, or the registered cost never reached the cost calculator.

1. Check what the proxy has loaded (`0` here means the cost is genuinely missing):

   ```bash
   curl -sS "$LITELLM_URL/model/info" -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     | jq -r '.data[] | select((.litellm_params.input_cost_per_token // 0) == 0) | .model_name'
   ```

2. If the model is DB-registered, run [set-model-costs.sh](../scripts/set-model-costs.sh).
   If it is defined in [base/litellm/configmap.yaml](../base/litellm/configmap.yaml),
   add the cost there and roll out the deployment.
3. If a cost *is* loaded but spend is still `0`, the model's provider path isn't
   feeding the calculator. Known risk for `qwen-image-edit`, which routes through
   the fork's custom `aihpi-provider`.

Costs that appear as strings rather than numbers in `/model/info` mean the YAML
scientific-notation trap bit: PyYAML reads `5e-8` as a string, `5.0e-8` as a
float. Always write the decimal point.

## Prometheus metrics

LiteLLM exports `litellm_spend_metric` alongside token counters, labelled by
`model`, `api_key_alias`, and `team_alias` (see the `prometheus_metrics_config`
block in [base/litellm/configmap.yaml](../base/litellm/configmap.yaml)). Useful for
live dashboards; Postgres is the better source for historical analysis, since
high-cardinality labels are deliberately stripped from the metrics to keep the
`/metrics` payload bounded.

## GPU utilization

Token spend measures *demand*. Actual cost is GPU-hours, and the two diverge
sharply — an idle model pod costs the same as a saturated one.

- Track GPU metrics via NVIDIA DCGM or node exporter
- Alert on low utilization or runaway workloads
- Compare `litellm_spend_metric` against GPU-hours per model to find deployments
  whose demand doesn't justify their reserved GPUs
