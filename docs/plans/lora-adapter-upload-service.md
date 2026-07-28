# Plan: LoRA Adapter Upload Service

> **Status:** approved, not yet implemented. Captured here for future execution. Paths in markdown links are repo-root-relative; adjust as needed when navigating from this file.

## Context

Following the LoRA enablement on ministral-3-14b and gpt-oss-120b (shipped in commits `72ce9c0` / `70aa572` / `ea71c62`), we hit a practical bottleneck: getting adapter weights onto the cluster. Current options are all painful:

- `kubectl cp` requires the file to already be on the kubectl-running host (lx04). The user's adapter sits on the SLURM cluster (`rx01.hpc.sci.hpi.de`), and rx01 has no direct route to lx04 — transfer falls back through the user's laptop on residential bandwidth (~440 KB/s on the test transfer).
- Mounting the underlying NFS export on SLURM directly would be cleanest but needs cluster-admin coordination outside the user's control.
- HF Hub round-trip works but means private model artifacts leave the institution.

What we want: an authenticated upload endpoint reachable over the existing public ingress (which `curl https://api.aisc.hpi.de/` from rx01 confirmed is already routed through HPI campus → Caddy → K8s nginx). The user uploads with the same `sk-...` API key they already use for inference, and the adapter becomes immediately callable through LiteLLM's standard chat-completions endpoint.

**Key constraints driving the design:**

- **No fork of LiteLLM.** Auth on `/v1/*` API calls is bearer-token only (Authentik SSO gates only `/ui/*`), so a sidecar reusing `LITELLM_MASTER_KEY` can validate the same keys without touching upstream code.
- **No new public IP.** rx01 already reaches `api.aisc.hpi.de`; we hang `/v1/adapters/*` off the same hostname by extending the existing nginx-proxy configmap.
- **Match established custom-service pattern.** `base/kisz-auth-wrapper` is the precedent: image built from a sibling `aihpi/tool-*` repo, deployed in this repo via a `base/<service>/` Kustomize directory.

**Decisions locked in (from clarifying questions):**

1. API shape: **dedicated `POST /v1/adapters/*`** namespace owned by the new service. No OpenAI `/v1/files` body-inspection routing.
2. Storage: **refactor to a single shared `model-adapters` PVC** with per-model subdirectories. Per-model `*-adapters` PVCs (`ministral-3-14b-adapters`, `gpt-oss-120b-adapters`) get retired; both are currently empty so migration cost is zero.
3. Post-upload: **save → auto-load on the matching vLLM service → auto-register in LiteLLM model_list**. One round-trip from user, immediately callable through LiteLLM.
4. **Access control on registration is a v1 field, not a follow-up.** LiteLLM's virtual keys filter `GET /v1/models` per-key based on the model's access groups, but **only when the access group is set at registration time**. Retrofitting visibility on already-registered adapters is painful (and information has already leaked by then). The upload API takes an optional `access` form field that propagates to LiteLLM's model access metadata; omitted means "public to all keys," set means "only keys in this group."

---

## Architecture

```
rx01 (SLURM)
  │
  │  curl -H "Authorization: Bearer sk-..." \
  │       -F "file=@adapter_model.safetensors" \
  │       -F "file=@adapter_config.json" \
  │       https://api.aisc.hpi.de/v1/adapters/ministral-3-14b/scale
  │
  ▼
Caddy (HPI edge) ──► K8s nginx ingress ──► nginx-proxy:4000 (litellm namespace)
                                              │
                                              │  new: location ~ ^/v1/adapters/
                                              ├──────────────────────────────────┐
                                              │                                  │
                                              ▼                                  ▼
                                  litellm-service:4000           adapter-upload-service:8000
                                  (everything else)              (path /v1/adapters/*)
                                                                      │
                                                                      │ 1. validate bearer == LITELLM_MASTER_KEY
                                                                      │ 2. write to /adapters/ministral-3-14b/scale/
                                                                      │ 3. POST ministral-3-14b-service:8000/v1/load_lora_adapter
                                                                      │ 4. POST litellm-service:4000/model/new
                                                                      │
                                                                      ▼
                                                              shared model-adapters PVC
                                                              (RWX, nfs-k8s-general)
                                                              ▲
                                                              │ mounted at /adapters by every
                                                              │ vLLM pod that has --enable-lora
```

The split point is the **nginx-proxy configmap** (`base/nginx/configmap.yaml`) — currently has a single `location /` that proxies everything to litellm. We add a more-specific `location ~ ^/v1/adapters/` that proxies to the new service. nginx prefers the more specific match, no Caddy or cluster-ingress changes needed.

---

## Components

### A. New external repo: `aihpi/tool-adapter-upload`

Following the `aihpi/tool-litellm` and `aihpi/tool-kisz-auth-wrapper` precedent, the service code lives outside this repo and ships as `ghcr.io/aihpi/tool-adapter-upload`. This repo only carries the K8s manifests pinned to a specific image digest (matching how prod overlay pins `tool-litellm` at `overlays/prod/kustomization.yaml`).

**Recommended stack: Python + FastAPI.** Small (~150-300 lines), trivially containerized, async-friendly for proxying file uploads. Same idiom as tool-litellm.

**Endpoints:**

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/v1/adapters/{model_name}/{adapter_name}` | Pre-validates `model_name` against the live backend (`GET /v1/models`, 2s timeout) **before** reading the body — typos get a fast 404. Multipart upload of `adapter_config.json` + `adapter_model.safetensors` (and optional tokenizer files). Optional form field `access=<group>` to restrict visibility (omit for public). Writes via tmp-dir + atomic rename to `/adapters/{model_name}/{adapter_name}/`. **Overwrites by default** when the path exists (unload-old → rename → load-new); see "Overwrite semantics" below. Then auto-loads + auto-registers (or PATCHes access on overwrite). Returns 201 with `{model_name, access, replaced, ...}`. |
| `GET`  | `/v1/adapters` | List all adapters across all model subdirs (read-only of the PVC plus `GET /v1/models` of each backend service for live state). Includes `access` value per adapter. |
| `GET`  | `/v1/adapters/{model_name}` | List adapters for one base model. |
| `GET`  | `/v1/adapters/{model_name}/{adapter_name}` | Metadata (size, mtime, currently-loaded?, registered in litellm?, access group). |
| `DELETE` | `/v1/adapters/{model_name}/{adapter_name}` | Unload from vLLM, delete LiteLLM model_list entry, then `rm -rf` the PVC subdir. Best-effort across the three steps; report partial-success in the response. |
| `GET`  | `/health` | Liveness + readiness. |

**Access control field.** The optional `access` form field on upload accepts a free-text group identifier (e.g. `team:kisz-scale`). The service forwards it to LiteLLM's `/model/new` payload under the access-group field that LiteLLM uses to gate `/v1/models` visibility per virtual-key. Behavior:

- **Omitted:** the adapter is registered without an access group, visible to all virtual keys (matches today's posture for unregistered models — public-by-default within the proxy).
- **Set:** the adapter is registered with that access group; only virtual keys associated with that group see it in `/v1/models` and can call inference against it. Master-key holders always see everything.

The service does not manage teams, virtual keys, or group membership — those are LiteLLM's existing concepts (created in the LiteLLM UI / admin API). The upload service only carries the label through. Validation we do perform: reject unknown groups *only if* LiteLLM `/model/new` rejects them — we don't pre-validate against a list, since that would couple the upload service to LiteLLM's team registry.

**Implementation note to verify during build:** the exact LiteLLM `/model/new` field used for access-group propagation depends on the `tool-litellm` version (upstream LiteLLM has used `model_info.access_groups`, `model_info.team_id`, and `model_access_group` in different releases). Confirm the live API shape against the pinned `tool-litellm` digest and adjust the service accordingly. Defer to LiteLLM's docs / source for the running version, not to general OpenAI-style assumptions.

**Auth (v1):** validate `Authorization: Bearer ...` exactly equals `LITELLM_MASTER_KEY` from env. This matches how `scripts/add-model.sh` authenticates today. Virtual-key support (calling `litellm-service:4000/key/info` with the user's key, checking permissions) is **explicit follow-up work**, not v1.

**Request flow for `POST /v1/adapters/{model_name}/{adapter_name}`:** the order matters. Specifically, **all cheap validations run before we accept a single byte of the file body** so a 600 MB upload over a slow link doesn't get burned on a typo:

1. **Auth check.** Validate bearer token. 401 on mismatch.
2. **Resolve backend.** Map `model_name` to `http://{model_name}-service:8000/v1` via the naming convention. (Or configmap fallback, if introduced.)
3. **Backend preflight (`GET /v1/models` with ~2s timeout).** Confirm the service exists, is reachable, and reports the base model. If unreachable or 404 → return `404 Not Found` with `{"error":"backend for model_name '...' not reachable","details":"..."}` *before* reading the request body. If the service is up but LoRA isn't enabled, the load call later will surface that — we don't pre-check the LoRA flag itself (vLLM has no clean introspection for it; defer to the load call's structured error). The cheap check still catches typos and decommissioned models.
4. **Stream the multipart body to the PVC.** Write to a temp dir under `/adapters/{model_name}/.tmp-{uuid}/` so a half-uploaded adapter never appears under its real name.
5. **Atomic rename to final path.** `os.rename('/adapters/{model}/.tmp-{uuid}/', '/adapters/{model}/{adapter_name}/')` once all parts are written. (See "Overwrite semantics" below for what happens when the target already exists.)
6. **Trigger vLLM load.** `POST {backend}/v1/load_lora_adapter` with `lora_name={base_model}-{adapter_name}` and `lora_path=/adapters/{model_name}/{adapter_name}`. Surface vLLM's error verbatim on failure (e.g. rank > max-lora-rank, base-model mismatch).
7. **Register in LiteLLM.** `POST litellm-service:4000/model/new` (with optional `model_info.access_groups`). Surface LiteLLM's error verbatim on failure.
8. **Return structured response.** Includes which steps succeeded; see "Failure handling" below.

**Overwrite semantics (default: overwrite).** When the target `/adapters/{model_name}/{adapter_name}/` already exists — the common case during iterative training — the service:

1. Accepts the upload normally (auth + preflight pass).
2. Streams the new files to a `.tmp-{uuid}` sibling.
3. Calls `POST {backend}/v1/unload_lora_adapter` with the existing `lora_name` (best-effort; ignore 404 if the runtime adapter was already evicted by a pod restart).
4. `os.rename` the new tmp dir over the old one (deleting the old). The rename is atomic, so concurrent reads of the old contents are not torn.
5. Calls `POST /v1/load_lora_adapter` with the same `lora_name` so vLLM picks up the new weights.
6. The LiteLLM model_list entry stays as-is (same name, same `api_base`); we do *not* re-POST to `/model/new`. If the upload included a different `access` value than before, we PATCH the access group via `/model/update` (or whatever the equivalent endpoint is on the running tool-litellm; verify per the LiteLLM-version note below).

This default matches the iterative training workflow — push, test, retrain, push again. Document the behavior loudly in the response (`{"replaced": true, "previous_size": ...}`) and in the user docs.

**Optional safety flag (deferred):** if conflict-protection becomes useful, we can add `?if_exists=fail` (default unset = overwrite) which returns `409 Conflict` when the target already exists and includes a hint in the body about how to override. Not needed for v1; noted here so the API surface stays cleanly extensible.

**Backend lookup:** the service needs to map `model_name → vLLM service URL` to know where to POST `/v1/load_lora_adapter`. Two options inside the service:

1. **Convention** — assume `http://{model_name}-service:8000/v1` (works for ministral-3-14b → `ministral-3-14b-service:8000`, gpt-oss-120b → `gpt-oss-120b-service:8000` — matches `models/` naming today).
2. **Configmap-driven** — read `/etc/adapter-upload/backends.yaml` listing `{model_name: api_base}` mappings. More explicit, future-proof.

Plan: start with the convention (option 1) since current naming is consistent; add a configmap fallback only if a future model breaks the pattern.

**LiteLLM registration:** on successful vLLM load, POST to `http://litellm-service:4000/model/new` (mirroring `scripts/add-model.sh`) with body:

```json
{
  "model_name": "ministral-3-14b-scale",
  "litellm_params": {
    "model": "openai/ministral-3-14b-scale",
    "api_base": "http://ministral-3-14b-service:8000/v1",
    "api_key": "dummy"
  },
  "model_info": {
    "access_groups": ["team:kisz-scale"]
  }
}
```

`model_info.access_groups` is included only when the upload's `access` field is set; if `access` was omitted, the field is omitted entirely (registered as public). Exact field name to verify against running `tool-litellm` (see implementation note above).

The LiteLLM `model_name` is `{base_model}-{adapter_name}` to avoid collisions across base models (uploading "scale" to ministral and "scale" to gpt-oss-120b yields two distinct LiteLLM entries). The `served-model-name` exposed by vLLM is also this composite, so vLLM's `lora_name` on load is set to `{base_model}-{adapter_name}`.

**Failure handling:** the three post-write steps (load on vLLM, register in LiteLLM) are not atomic. The service runs them sequentially and returns a structured response:

```json
{
  "status": "partial",
  "model_name": "ministral-3-14b-scale",
  "stored": true,
  "loaded_in_vllm": true,
  "registered_in_litellm": false,
  "errors": [{"step": "litellm_register", "message": "..."}]
}
```

The user can retry just the failing steps via the load and register endpoints (which we expose as `POST /v1/adapters/{model}/{name}/load` and `POST /v1/adapters/{model}/{name}/register` for surgical retries). If the vLLM load itself fails (rank mismatch, base-model mismatch), the file stays on the PVC for inspection; the response includes vLLM's exact error.

### B. New K8s manifests in this repo: `base/adapter-upload/`

Three files following the `base/kisz-auth-wrapper` pattern:

- `deployment.yaml` — single replica, mounts `model-adapters` PVC at `/adapters`, env vars for `LITELLM_MASTER_KEY` (from `litellm-secret`), `LITELLM_BASE_URL=http://litellm-service:4000`. Image pinned to digest in the prod overlay.
- `service.yaml` — ClusterIP, port 8000.
- `kustomization.yaml` — registers the two above.

Then in `base/kustomization.yaml`, add `- adapter-upload` under resources.

### C. nginx-proxy split: edit `base/nginx/configmap.yaml`

Add upstream + location block. Sketch:

```nginx
upstream adapter_upload {
  server adapter-upload-service.<NAMESPACE>.svc.cluster.local:8000;
}

location ~ ^/v1/adapters(/|$) {
  proxy_pass http://adapter_upload;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_request_buffering off;          # critical: stream large uploads
  proxy_buffering off;
  client_max_body_size 4G;              # accommodate large adapter weights
  proxy_read_timeout 600s;
}
```

`proxy_request_buffering off` and the bumped body size are essential — default nginx will buffer the entire upload to disk and reject anything > 1MB. Adapters can be hundreds of MB.

The namespace differs between staging (`litellm-staging`) and prod (`litellm`); current configmap already templates around this with separate staging/prod patches (`overlays/staging/patches/nginx-staging-config.yaml`). Mirror that pattern: add the new block to base, override the upstream's namespace in the staging patch.

### D. Shared PVC migration

**New file: `base/adapter-storage/pvc.yaml`** (50Gi or 100Gi, RWX, nfs-k8s-general):

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: model-adapters
spec:
  accessModes: [ReadWriteMany]
  storageClassName: nfs-k8s-general
  resources:
    requests:
      storage: 100Gi
```

**Mount changes in existing model deployments:**

- `models/ministral-3-14b/deployment.yaml`: `claimName: ministral-3-14b-adapters` → `claimName: model-adapters`. Mount stays at `/adapters`.
- `models/gpt-oss-120b/deployment.yaml`: same change with `claimName: model-adapters`.
- `models/_template/deployment.yaml`: update the commented LoRA block to reference `model-adapters` instead of per-model claim name.

**lora_path convention change:** when vLLM loads adapters from the local PVC, the path is now `/adapters/{model_name}/{adapter_name}/` rather than `/adapters/{adapter_name}/`. Update `docs/adding-models.md` §"LoRA adapters" to reflect this.

**Retiring old per-model PVCs:** they're currently empty (we never landed an adapter), so we can either:
- **Just stop referencing them** and let the cluster admin garbage-collect — simpler, no kubectl-delete needed at apply-time.
- **Explicitly remove from kustomization** and `kubectl delete pvc ministral-3-14b-adapters gpt-oss-120b-adapters -n litellm` post-rollout.

Plan: do both — remove from `models/ministral-3-14b/pvc.yaml` and `models/gpt-oss-120b/pvc.yaml`, and document the manual delete as a post-rollout cleanup step.

### E. New SealedSecret entries (if any)

No new secrets needed for v1: the service reuses `LITELLM_MASTER_KEY` from the existing `litellm-secret`. Adding virtual-key support later (a per-user upload token) would introduce a new secret, but that's follow-up work.

---

## Rollout order

1. **Build + publish `tool-adapter-upload` image** in the external repo. Get a digest.
2. **Land the K8s changes in this repo** as a single PR/branch:
   - Shared PVC (`base/adapter-storage/pvc.yaml` + register in `base/kustomization.yaml`).
   - Update model deployments (ministral, gpt-oss-120b, _template) to use `model-adapters`.
   - Remove obsolete per-model adapter PVCs from manifests.
   - New `base/adapter-upload/` directory.
   - nginx-proxy configmap + staging override patch for the new location block.
   - Pin the digest in `overlays/prod/kustomization.yaml` images.
   - Doc updates (`docs/adding-models.md` §"LoRA adapters" and a new §"Uploading adapters from outside the cluster").
3. **Apply to staging first** (`overlays/staging`) — staging doesn't run model pods, so we lose the auto-load step, but we can verify the upload + LiteLLM-register half against a stub backend or just by inspecting the PVC.
4. **Apply to prod.** Verify: ministral and gpt-oss-120b pods come back up cleanly with the new mount; `model-adapters` PVC binds; nginx-proxy reloads without dropping in-flight requests; the new path responds.
5. **End-to-end test** (verification section below).
6. **Post-rollout cleanup:** delete the orphaned `ministral-3-14b-adapters` and `gpt-oss-120b-adapters` PVCs once everything is happy.

---

## Verification

After step 4, with port-forward to nginx-proxy or via the public hostname:

1. **Health endpoint:**
   ```
   curl -fsS https://api.aisc.hpi.de/v1/adapters/health
   # expect: 200 with {"status":"ok"}
   ```

2. **Auth gating works:**
   ```
   curl -i https://api.aisc.hpi.de/v1/adapters/ministral-3-14b/test
   # expect: 401 Unauthorized

   curl -i -H "Authorization: Bearer wrong-key" https://api.aisc.hpi.de/v1/adapters/ministral-3-14b/test
   # expect: 401

   curl -i -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     https://api.aisc.hpi.de/v1/adapters/ministral-3-14b/nonexistent
   # expect: 404
   ```

2b. **Pre-upload model validation fails fast on typo:**
   ```
   curl -i -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     -F "file=@/dev/zero" \
     https://api.aisc.hpi.de/v1/adapters/ministrall-3-14b-typo/scale
   # expect: 404 within ~2s, no bytes streamed past nginx-proxy
   ```
   Watch the adapter-upload pod logs to confirm the request was rejected before a multipart parser was instantiated. This proves the preflight check fires before body buffering.

3. **Upload from rx01 with the real adapter (public):**
   ```
   curl -fsS -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     -F "file=@/sc/projects/sci-aisc/scale/ministral-3-14b/adapter_config.json" \
     -F "file=@/sc/projects/sci-aisc/scale/ministral-3-14b/adapter_model.safetensors" \
     https://api.aisc.hpi.de/v1/adapters/ministral-3-14b/scale
   ```
   Expect 201 with `{"status":"ok","loaded_in_vllm":true,"registered_in_litellm":true,"model_name":"ministral-3-14b-scale","access":null}`. This is the moment of truth: it confirms the SLURM-to-K8s upload path, the PVC write, the vLLM load, and the LiteLLM registration all work end-to-end.

3b. **Restricted-access upload + visibility test:**
   - Upload a second adapter with `access`:
     ```
     curl -fsS -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
       -F "file=@.../adapter_config.json" -F "file=@.../adapter_model.safetensors" \
       -F "access=team:kisz-scale" \
       https://api.aisc.hpi.de/v1/adapters/ministral-3-14b/private-scale
     ```
   - With a virtual key **not** in `team:kisz-scale`:
     ```
     curl -fsS -H "Authorization: Bearer $OTHER_KEY" https://api.aisc.hpi.de/v1/models | jq '.data[].id' | grep private-scale
     # expect: no match
     ```
   - With a virtual key in `team:kisz-scale`:
     ```
     curl -fsS -H "Authorization: Bearer $TEAM_KEY" https://api.aisc.hpi.de/v1/models | jq '.data[].id' | grep private-scale
     # expect: ministral-3-14b-private-scale present
     ```
   This verifies the `access` field actually closes the visibility leak end-to-end through the live LiteLLM filtering. If LiteLLM doesn't honor the access group as expected, the field name in `/model/new` is wrong for this version — adjust per the implementation note.

4. **Adapter is callable through LiteLLM:**
   ```
   curl -fsS -H "Authorization: Bearer $LITELLM_API_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"model":"ministral-3-14b-scale","messages":[{"role":"user","content":"hello"}]}' \
     https://api.aisc.hpi.de/v1/chat/completions
   ```

5. **Listing works:**
   ```
   curl -fsS -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     https://api.aisc.hpi.de/v1/adapters | jq
   # expect: { "adapters": [{"model": "ministral-3-14b", "name": "scale", "loaded": true, "registered": true}] }
   ```

6. **Delete works:**
   ```
   curl -fsS -X DELETE -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     https://api.aisc.hpi.de/v1/adapters/ministral-3-14b/scale
   curl -fsS -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     http://localhost:8000/v1/models | jq '.data[].id'  # against ministral pod directly
   # expect: "ministral-3-14b" only, no "ministral-3-14b-scale"
   ```

7. **Streaming upload doesn't OOM nginx-proxy.** Specifically test with a >500MB adapter to confirm `proxy_request_buffering off` is in effect — a 1.3GB upload should not push nginx-proxy memory above ~50MB.

7b. **Overwrite semantics.** Upload `scale` once, confirm 201. Upload `scale` again (different file, same name):
   - Response includes `"replaced": true` and `"previous_size": <bytes>`.
   - `kubectl -n litellm exec deploy/ministral-3-14b -- ls -la /adapters/ministral-3-14b/scale/` shows the **new** mtime, no `.tmp-*` siblings, old contents are gone.
   - Inference against `ministral-3-14b-scale` immediately after returns output reflecting the new weights (a quick sanity check, e.g. confirm the response differs from the first version).
   - No new LiteLLM model_list entry was created (`/v1/models` count unchanged).

8. **Pod restart loses runtime adapters (expected, documented).** After upload, `kubectl rollout restart deploy/ministral-3-14b`; the LoRA disappears from `GET /v1/models`. The user re-loads via `POST /v1/adapters/.../load` (or via a future "preload from PVC on startup" feature). Document this in the new docs section.

---

## Out of scope (intentionally)

- **Virtual-key auth on the upload endpoint itself.** v1 uses master-key only for upload — same posture as `add-model.sh`. Note this is separate from the `access` field, which controls who sees the *uploaded adapter* once it's registered. Per-user upload tokens are a follow-up.
- **Team / access-group management.** This service does not create or delete LiteLLM teams; it only labels models with an existing access group. Team creation happens through LiteLLM's UI/admin API as it does today.
- **Atomic transactions across PVC + vLLM + LiteLLM.** Best-effort with structured failure response and surgical-retry endpoints.
- **Auto-reload after pod restart.** vLLM loses runtime adapters on restart; users must re-load. A future feature could read `/adapters/{model}` on pod startup and re-issue load calls, or write `--lora-modules` into a startup-driven configmap. Out of scope for v1.
- **Adapter integrity verification** (safetensors hash, config schema check before load). The vLLM load call is the ground truth; we surface its error.
- **Rate limiting / quota.** A single uploader (Felix) for now. Add when usage justifies it.
- **OpenAI-compatible `/v1/files` shim.** User explicitly chose dedicated `/v1/adapters/*`; no compat layer.
- **Staging functional tests.** Staging has no GPU model pods, so auto-load won't work there. Staging only validates auth + upload + PVC write paths.

---

## Critical files (paths)

To create:
- `base/adapter-upload/deployment.yaml`
- `base/adapter-upload/service.yaml`
- `base/adapter-upload/kustomization.yaml`
- `base/adapter-storage/pvc.yaml`
- `base/adapter-storage/kustomization.yaml`

To modify:
- `base/kustomization.yaml` — add the two new resources
- `base/nginx/configmap.yaml` — add upstream + location block
- `overlays/staging/patches/nginx-staging-config.yaml` — mirror the new location with staging namespace
- `overlays/prod/kustomization.yaml` — pin `tool-adapter-upload` image digest under `images:`
- `models/ministral-3-14b/deployment.yaml` — `claimName` → `model-adapters`
- `models/gpt-oss-120b/deployment.yaml` — same
- `models/ministral-3-14b/pvc.yaml` — drop the `*-adapters` PVC document
- `models/gpt-oss-120b/pvc.yaml` — drop the `*-adapters` PVC document
- `models/_template/deployment.yaml` — update commented LoRA block
- `docs/adding-models.md` — update §"LoRA adapters" for new `lora_path` convention; add §"Uploading adapters from outside the cluster"

Outside this repo (separate work):
- New repo `aihpi/tool-adapter-upload` with FastAPI service code, Dockerfile, GHCR publish workflow.

---

## Open questions to revisit during implementation

- **PVC size.** 100Gi is a guess based on ministral being a small base. If many adapters or larger ones land, bump.
- **nginx upload limits in front of nginx-proxy.** Caddy at the HPI edge may also have body-size limits; need to verify a >500MB upload survives the full chain. If not, work with whoever runs Caddy to bump.
- **`tool-adapter-upload` image versioning strategy.** Follow `tool-litellm`'s pattern — `:aihpi-provider` floating tag plus a digest pin in prod kustomization. Bump digest per intentional release.
- **Exact LiteLLM access-group field name** for `/model/new`. As noted under §A, the field has shifted across LiteLLM versions (`model_info.access_groups`, `model_info.team_id`, `model_access_group`). Test against the live `tool-litellm` once code is up; the verification step §3b will fail loudly if the wrong field is used.
