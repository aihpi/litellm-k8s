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

This is also the safer design: a header can be forged by anything able to reach `lora-manager:8000` inside the namespace, whereas a key has to survive LiteLLM's own validation.

**Identity headers are trusted only on the in-cluster routes.** LiteLLM's pass-through relays client headers verbatim, so on a publicly reachable route `x-litellm-user-id: <victim>` would be an ownership takeover. On `/upload` and `/delete` an unresolvable key is a 401 and headers are never consulted; the in-cluster `DELETE` and `/reconcile` routes (`internal=True`) may fall back to headers, since they are network-restricted and have no ownership check to subvert. This is deliberately *not* keyed off `REQUIRE_IDENTITY` — turning that flag off must not re-open spoofing.

`_bearer()` reads `x-litellm-api-key` as well as `Authorization`, because LiteLLM accepts that header in preference to `Authorization`; reading only the latter would let a caller authenticate to the proxy while presenting no token here, skipping key resolution entirely.

The caller's key is passed to `/key/info` in the query string, so it must never reach a log: `httpx`'s logger is pinned to WARNING in `main.py` (it prints request URLs at INFO) and `key_info()` re-raises non-2xx as a status-code-only message, because `raise_for_status()` embeds the URL. There is a regression test asserting the plaintext key is absent from captured logs on both the success and 5xx paths.

`REQUIRE_IDENTITY` (default **on**) rejects a request whose identity doesn't resolve, rather than recording `anonymous` — an adapter with no owner is one nobody can delete afterwards.

A caller presenting `LITELLM_MASTER_KEY` is `master-key`, and is the only ops caller: it isn't a user, so it can't *own* anything, but it can delete anything. Adapters with no ownership record are also removable through the in-cluster `DELETE /adapters/{base_model}/{name}` route, which has no ops check at all.

**Virtual keys must be allowed onto the route.** Without it the proxy rejects them before lora-manager sees the request: `Key/team not allowed to access passthrough route /v1/lora/upload`. Set `metadata.allowed_passthrough_routes` on the key or team (admin-only field) to include `/v1/lora/upload`, `/v1/lora/adapters`, `/v1/lora/delete`. This is why early uploads were all done with the master key — and therefore why they have no owner.

## Ownership

There is no separate metadata store. `/adapters/{base_model}/.upload-log.jsonl` (written by `audit.py`) is the ownership record: `audit.latest_upload()` returns the most recent upload event for a name, which carries `user_id`, `key_alias`, and `access`. The log lives in the base-model dir, not the adapter dir, so it survives a delete.

A recorded `user_id` of `anonymous` counts as *no* owner, so pre-`REQUIRE_IDENTITY` uploads can't be deleted by whoever happens to also arrive unidentified.

Adapters with no record at all (uploaded before this service, or with the master key) are ops-only to delete.

## Reconciliation

The adapters PVC is the source of truth. `reconcile.py` runs at startup and every `RECONCILE_INTERVAL_SECONDS`, and for each adapter dir on the PVC:

- loads it into the matching vLLM pod if `GET /v1/models` doesn't list it;
- registers it in LiteLLM if `GET /model/info` doesn't list it, reusing the `access` group from the upload log so a re-registered adapter doesn't silently go public.

Metadata is read with `audit.latest_upload_event()`, **not** `audit.latest_upload()`. The latter nulls anonymously-recorded uploads because they have no *owner*; reading `access` through it would publish a restricted adapter the moment it needed re-registering. The two lookups exist precisely to keep authorization and metadata separate.

Every mutation is serialised through `reconcile.adapter_lock(base_model, name)`, shared with the upload and delete handlers, and re-checks that the directory still exists before acting. `INFLIGHT` alone is not enough: a pass filters its list once and then awaits, so a delete arriving after that snapshot could otherwise be undone by the pass re-registering an adapter whose files had just been removed — leaving a `db_model=true` row that survives restarts, is never pruned, and that neither delete route can remove (both 404 once the directory is gone). Adapters that disappear mid-pass are reported under `vanished_mid_pass`.

This requires a single process, which is why the Deployment uses `strategy: Recreate` — the locks and `INFLIGHT` are process-local, and a rolling-update surge pod would run a second reconcile loop against the same ReadWriteMany PVC.

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
| `RECONCILE_INTERVAL_SECONDS` | `300` | `0` disables the background loop; `POST /reconcile` still works. |

## Adapter contract

Uploads must be a `.tar.gz` containing a PEFT LoRA adapter:

- `adapter_config.json` with `peft_type: "LORA"` and `r <= MAX_LORA_RANK`
- One or more `*.safetensors` files (no pickled `.bin` accepted)
- Optionally `tokenizer.{json,model}`, `tokenizer_config.json`, `special_tokens_map.json`, `added_tokens.json`, `README.md`

Anything else fails validation. Safetensors are parsed header-only (no tensor data loaded into memory).

Extraction uses `tarfile.extractall(..., filter="data")` (PEP 706), which refuses `../` traversal, links pointing outside the destination, and device/FIFO members — so `validation.py` does not re-check those. One case the filter *sanitises* rather than refuses: an absolute member path like `/etc/pwned` is rewritten to `etc/pwned` inside the destination, and the filename allowlist above is what rejects it. `test_extract.py` pins both halves of that — run `python base/lora-manager/test_extract.py`. This matters because the container runs as root, so an unfiltered `extractall` would genuinely write outside the destination.

## Adapter naming

`^[a-z0-9][a-z0-9-]{0,62}$` — lowercase alphanumeric + hyphen, 1-63 chars. This is what becomes the `lora_name` in vLLM and `model_name` in LiteLLM. Whitespace would break the auto-discovery wrapper on the vLLM side.

**Team convention:** prefix with the base model name, e.g. `ministral-3-14b-therapy-depression-v1` or `gemma-4-31b-writing-assistant`. The prefix makes it obvious which base model an adapter belongs to when scanning `/v1/models`. Not enforced by validation — humans agree on it.
