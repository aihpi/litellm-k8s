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
  -e REQUIRE_IDENTITY=false \
  lora-manager:dev
# curl localhost:8000/docs
```

## Endpoints

Behind LiteLLM auth (caller authenticates with their normal `sk-...` key):

- `POST /v1/lora/upload` — multipart: `name`, `base_model`, `adapter` (tar.gz), optional `access` (LiteLLM access group)
- `GET  /v1/lora/adapters` — list deployed adapters per base model, with `owner` and `access`
- `POST /v1/lora/delete` — form: `name`, `base_model`. Removes from vLLM + LiteLLM + PVC. Allowed for the uploader or an ops caller only; fixed path (not `DELETE /.../{name}`) because `pass_through_endpoints` matches exact paths.

Direct-on-service (requires kubectl exec or port-forward):

- `DELETE /adapters/{base_model}/{name}` — same teardown, no ownership check. This is the escape hatch for adapters with no upload record.
- `GET  /reconcile/status` — result of the last reconciliation pass
- `POST /reconcile` — trigger a pass now (ops identity required)

## Identity

**Identity comes from the caller's API key, not from headers.** Verified against the live proxy (July 2026): `pass_through_endpoints` with `forward_headers: true` forwards `authorization` but **not** `x-litellm-user-id` or `x-litellm-key-alias`. So `_identity()` takes the bearer token and resolves it via `GET /key/info?key=…` using the master key, reading `user_id` (falling back to `key_alias`).

This is also the safer design: a header can be forged by anything able to reach `lora-manager:8000` inside the namespace, whereas a key has to survive LiteLLM's own validation. Headers remain a fallback for in-cluster callers with no key.

`REQUIRE_IDENTITY` (default **on**) rejects a request whose identity doesn't resolve, rather than recording `anonymous` — an adapter with no owner is one nobody can delete afterwards.

A caller presenting `LITELLM_MASTER_KEY` is `master-key`, and counts as ops: it isn't a user, so it can't *own* anything, but it can delete anything. That makes `ADMIN_KEY_ALIASES` / `ADMIN_USER_IDS` purely for delegating ops to a personal key.

**Virtual keys must be allowed onto the route.** Without it the proxy rejects them before lora-manager sees the request: `Key/team not allowed to access passthrough route /v1/lora/upload`. Set `metadata.allowed_passthrough_routes` on the key or team (admin-only field) to include `/v1/lora/upload`, `/v1/lora/adapters`, `/v1/lora/delete`. This is why early uploads were all done with the master key — and therefore why they have no owner.

## Ownership

There is no separate metadata store. `/adapters/{base_model}/.upload-log.jsonl` (written by `audit.py`) is the ownership record: `audit.latest_upload()` returns the most recent upload event for a name, which carries `user_id`, `key_alias`, and `access`. The log lives in the base-model dir, not the adapter dir, so it survives a delete.

A recorded `user_id` of `anonymous` counts as *no* owner, so pre-`REQUIRE_IDENTITY` uploads can't be deleted by whoever happens to also arrive unidentified.

Adapters with no record at all (uploaded before this service, or with the master key) are ops-only to delete.

## Reconciliation

The adapters PVC is the source of truth. `reconcile.py` runs at startup and every `RECONCILE_INTERVAL_SECONDS`, and for each adapter dir on the PVC:

- loads it into the matching vLLM pod if `GET /v1/models` doesn't list it;
- registers it in LiteLLM if `GET /model/info` doesn't list it, reusing the `access` group from the upload log so a re-registered adapter doesn't silently go public.

This exists because a litellm-proxy restart rebuilds its router from `config.yaml` plus Postgres rows flagged `db_model=true`. Adapters whose row lacks that flag disappear from the router while their files and vLLM slot stay healthy, and inference fails with `400: Invalid model name` (`gemma-4-31b-leo`, Jul 2026). vLLM's side already self-heals through the `--lora-modules` auto-discovery wrapper in the model deployments; LiteLLM's did not.

**Reconciliation never removes anything.** LiteLLM also routes base models and non-adapter models from `config.yaml`, so pruning there risks unrelated traffic. Registered adapters with no files left are reported under `stale_in_litellm` in `/reconcile/status` and otherwise left alone.

Assumes `replicas: 1`. If this is ever scaled up, passes need leader election (or `/model/new` conflicts need treating as success).

## Required environment variables

| Var | Default | Notes |
|---|---|---|
| `LITELLM_MASTER_KEY` | — | Required. From `litellm-secret`. Used for `/model/new` / `/model/delete`. |
| `LITELLM_URL` | `http://litellm-service:4000` | |
| `ADAPTERS_BASE_PATH` | `/adapters` | Per-model subdirs under here. |
| `ALLOWED_BASE_MODELS` | `ministral-3-14b` | Comma-separated allowlist. |
| `MAX_UPLOAD_BYTES` | `4294967296` (4 GiB) | |
| `MAX_LORA_RANK` | `64` | Matches `--max-lora-rank` on vLLM. Rejects higher-rank adapters. |
| `REQUIRE_IDENTITY` | `true` | Reject requests whose caller can't be resolved via `/key/info`. Off allows unattributed (undeletable) uploads. |
| `ADMIN_KEY_ALIASES` | (empty) | Extra key aliases counting as ops. The master key already does, so this is only for delegation. |
| `ADMIN_USER_IDS` | (empty) | Same, matched on the resolved `user_id`. |
| `RECONCILE_INTERVAL_SECONDS` | `300` | `0` disables the background loop; `POST /reconcile` still works. |
| `RECONCILE_ADOPT_UNKNOWN` | `true` | Register adapter dirs with no upload record (no access group applied). Turn off if an adapter must be private-by-default. |

## Adapter contract

Uploads must be a `.tar.gz` containing a PEFT LoRA adapter:

- `adapter_config.json` with `peft_type: "LORA"` and `r <= MAX_LORA_RANK`
- One or more `*.safetensors` files (no pickled `.bin` accepted)
- Optionally `tokenizer.{json,model}`, `tokenizer_config.json`, `special_tokens_map.json`, `added_tokens.json`, `README.md`

Anything else fails validation. Safetensors are parsed header-only (no tensor data loaded into memory).

## Adapter naming

`^[a-z0-9][a-z0-9-]{0,62}$` — lowercase alphanumeric + hyphen, 1-63 chars. This is what becomes the `lora_name` in vLLM and `model_name` in LiteLLM. Whitespace would break the auto-discovery wrapper on the vLLM side.

**Team convention:** prefix with the base model name, e.g. `ministral-3-14b-therapy-depression-v1` or `gemma-4-31b-writing-assistant`. The prefix makes it obvious which base model an adapter belongs to when scanning `/v1/models`. Not enforced by validation — humans agree on it.
