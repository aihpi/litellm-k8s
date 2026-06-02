# Uploading a LoRA adapter

This guide explains how to upload a LoRA adapter (trained on SLURM, your laptop, or anywhere else) so it becomes callable through the inference API at `https://api.aisc.hpi.de`.

After upload, your adapter behaves like any other model: send a chat request with `"model": "<your-adapter-name>"`, get a response back. No GPU access, no kubectl, no infrastructure required.

> **⚠️ Supported base models are dense only.** MXFP4-quantized Mixture-of-Experts models (e.g. `gpt-oss-120b`) are **not supported** — vLLM's fused MoE LoRA path is broken on MXFP4 in the version we run ([upstream issue #42008](https://github.com/vllm-project/vllm/issues/42008)). Train against one of the dense base models listed below instead.

---

## What you need

- A trained LoRA adapter (PEFT format) on a host that has internet access to `api.aisc.hpi.de`.
- Your LiteLLM API key (starts with `sk-...`). Same one you already use for inference.
- The name of the **base model** your adapter was trained on. Currently supported:
  - `ministral-3-14b` — Mistral Ministral 3 14B Instruct
  - `gemma-4-31b` — Google Gemma 4 31B Instruct

If you trained against a different base model, the upload will be rejected. Ask the ops team to enable it.

---

## Step 1 — Check what's in your adapter directory

Your trained adapter should be a directory containing at minimum:

- `adapter_config.json` — PEFT config (must have `peft_type: "LORA"`, `r <= 64`)
- `adapter_model.safetensors` — the actual LoRA weights

It may also contain (all optional, all accepted):

- `tokenizer.json`, `tokenizer_config.json`, `special_tokens_map.json`, `added_tokens.json` — tokenizer files
- `README.md` — model card

**Files that are rejected** (will cause upload to fail):

- `*.bin` files (PyTorch pickle format — security risk, safetensors only)
- `training_args.bin`, `optimizer.pt`, `scheduler.pt` — training artifacts you don't need for inference
- `chat_template.jinja` — not currently in the allowlist (ask if you need it)
- Anything else not in the lists above

If your training script saved extra files, just exclude them when creating the tarball in step 2.

---

## Step 2 — Create the tarball

`cd` into your adapter directory, then:

```bash
cd /path/to/your/adapter

tar czf ministral-3-14b-my-adapter.tar.gz \
  adapter_config.json \
  adapter_model.safetensors \
  tokenizer.json \
  tokenizer_config.json
```

Replace `ministral-3-14b-my-adapter` with the name you pick in Step 3. Add or drop files as appropriate to match what you actually have. **Don't tar the parent directory** — tar the *contents* directly so the archive is "flat".

Verify the tarball looks right before uploading:

```bash
tar tzf ministral-3-14b-my-adapter.tar.gz       # list contents — should see file names, no leading "./"
ls -lh ministral-3-14b-my-adapter.tar.gz        # check size — must be under 4 GiB
```

---

## Step 3 — Pick a name for your adapter

The name you choose becomes the model identifier you'll use in chat requests after upload.

**Team convention: prefix the name with the base model.** This makes it immediately obvious which base model an adapter belongs to when looking at `/v1/models`:

- ✅ `ministral-3-14b-therapy-depression-v1`
- ✅ `gemma-4-31b-writing-assistant`
- ✅ `gemma-4-31b-leo`

Naming rules (enforced by validation):

- Lowercase letters, digits, and hyphens only — no underscores, no dots, no spaces, no uppercase
- Must start with a letter or digit
- 1–63 characters
- Regex: `^[a-z0-9][a-z0-9-]{0,62}$`

More examples:

- ✅ `ministral-3-14b-scale`
- ✅ `gemma-4-31b-experiment-2026q2`
- ❌ `My_Adapter` (underscore + uppercase not allowed)
- ❌ `v1.0` (dot not allowed)
- ❌ `a name with spaces`

The name also has to be unique per base model. If `gemma-4-31b-leo` already exists, the upload will fail with 409 Conflict — pick a different name or delete the existing one first.

---

## Step 4 — Upload

```bash
export LITELLM_KEY="sk-..."   # your normal inference key

curl -sS -X POST https://api.aisc.hpi.de/v1/lora/upload \
  -H "Authorization: Bearer ${LITELLM_KEY}" \
  -F "name=ministral-3-14b-my-adapter" \
  -F "base_model=ministral-3-14b" \
  -F "adapter=@ministral-3-14b-my-adapter.tar.gz"
```

Replace the name, the base model, and the tarball path with your actual values.

Upload speed depends on your network. Multi-GB adapters from residential / SLURM connections can take 5–15 minutes. Be patient.

**Successful response** (HTTP 200):

```json
{
  "name": "ministral-3-14b-my-adapter",
  "base_model": "ministral-3-14b",
  "access": null,
  "vllm_loaded": true,
  "litellm_registered": true,
  "file_count": 4,
  "total_bytes": 577184460,
  "tensor_count": 896
}
```

`vllm_loaded: true` and `litellm_registered: true` mean the adapter is now live and immediately callable.

---

## Step 5 — Use it

The adapter name is now a model name. Call it through the standard chat completions endpoint:

```bash
curl -sS https://api.aisc.hpi.de/v1/chat/completions \
  -H "Authorization: Bearer ${LITELLM_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ministral-3-14b-my-adapter",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ]
  }'
```

That's it. The adapter is applied on top of the base model for every request that names it.

---

## Listing your adapters

To see what's currently deployed across all base models:

```bash
curl -sS https://api.aisc.hpi.de/v1/lora/adapters \
  -H "Authorization: Bearer ${LITELLM_KEY}" | jq
```

Returns something like:

```json
{
  "ministral-3-14b": [
    {"name": "ministral-3-14b-scale"},
    {"name": "ministral-3-14b-therapy-depression-v1"}
  ],
  "gemma-4-31b": [
    {"name": "gemma-4-31b-leo"}
  ]
}
```

---

## Restricting visibility (optional)

By default, any LiteLLM key can call your uploaded adapter. To restrict it to a specific group of keys, add an `access` form field at upload time:

```bash
curl -sS -X POST https://api.aisc.hpi.de/v1/lora/upload \
  -H "Authorization: Bearer ${LITELLM_KEY}" \
  -F "name=ministral-3-14b-therapy-private-v1" \
  -F "base_model=ministral-3-14b" \
  -F "access=therapy-team" \
  -F "adapter=@ministral-3-14b-therapy-private-v1.tar.gz"
```

Only keys assigned to the `therapy-team` access group can see or invoke the adapter. **This has to be set at upload time** — you can't add or change it later without deleting and re-uploading. Talk to the ops team to set up access groups before relying on this.

---

## Common errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` | Bearer key wrong or missing | Check `${LITELLM_KEY}` is set |
| `400 base_model 'X' not in allowlist` | Typo in `base_model`, or that model isn't LoRA-enabled | Use one of the supported names above |
| `400 name 'X' must match ...` | Invalid character in name | See naming rules in Step 3 |
| `400 validation failed: ...` | Bad tarball contents | Check the file allowlist; rebuild without `.bin` files |
| `400 missing adapter_config.json` | Tar'd the parent dir instead of contents | Re-tar from inside the adapter directory |
| `409 adapter 'X' already exists` | Name collision on that base model | Pick a different name or delete the existing one |
| `413 upload exceeds ...` | Tarball over 4 GiB | Drop unnecessary files (training_args, optimizer states) |
| `500 vllm load failed` | Adapter trained against a different base model than declared | Check `base_model_name_or_path` in `adapter_config.json` matches |
| `curl: (26) Failed to open/read local data` | Bad path on `-F adapter=@...` | `ls -lh` the path you're passing |

---

## What happens after upload

- The adapter is written to a shared persistent volume on the cluster.
- vLLM loads it into GPU memory immediately (no model restart needed).
- LiteLLM gets a new entry in its model list with your adapter's name.
- If the vLLM pod restarts later, the adapter is automatically re-loaded on boot — no need to re-upload.

---

## Deleting an adapter

Currently only the ops team can delete adapters (the delete endpoint isn't exposed externally — it's a cluster-internal call). Ping them with the adapter name and base model and they'll remove it.

---

## Questions

For help, ping the ops team. When reporting issues, include:

- The full curl command you ran (redact your API key)
- The full response (including any error message)
- The output of `tar tzf <your-adapter>.tar.gz` and `ls -lh` of the tarball
