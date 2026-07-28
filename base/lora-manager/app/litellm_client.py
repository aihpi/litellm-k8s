import httpx

from .config import LITELLM_MASTER_KEY, LITELLM_URL


def _headers() -> dict:
    if not LITELLM_MASTER_KEY:
        raise RuntimeError("LITELLM_MASTER_KEY not set; cannot call LiteLLM admin API")
    return {
        "Authorization": f"Bearer {LITELLM_MASTER_KEY}",
        "Content-Type": "application/json",
    }


async def register_model(name: str, base_model: str, access: str | None) -> None:
    """Add a new model entry to LiteLLM pointing at the vLLM service.

    Access group MUST be set at registration time. LiteLLM cannot retrofit
    per-key visibility — see docs/plans/lora-adapter-upload-service.md.
    """
    body = {
        "model_name": name,
        "litellm_params": {
            "model": f"openai/{name}",
            "api_base": f"http://{base_model}-service:8000/v1",
            "api_key": "dummy",
        },
    }
    if access:
        body["model_info"] = {"access_groups": [access]}

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{LITELLM_URL}/model/new", json=body, headers=_headers())
        r.raise_for_status()


async def key_info(api_key: str) -> dict | None:
    """Resolve a caller's virtual key to its identity, as admin.

    This is how we attribute uploads. LiteLLM's pass_through_endpoints does NOT
    forward x-litellm-user-id / x-litellm-key-alias in the version we run (only
    `authorization` arrives), so the bearer token is the only identity signal we
    get — and unlike a header it can't be spoofed by an in-cluster caller, since
    LiteLLM validates it.

    Returns the key's info dict, or None if the proxy doesn't recognise it.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{LITELLM_URL}/key/info", params={"key": api_key}, headers=_headers()
        )
        if r.status_code in (400, 401, 404):
            return None
        r.raise_for_status()
        body = r.json()
    # Documented shape is {"key": ..., "info": {...}}; tolerate a flat response.
    info = body.get("info")
    return info if isinstance(info, dict) else body


async def _model_info(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(f"{LITELLM_URL}/model/info", headers=_headers())
    r.raise_for_status()
    return r.json().get("data", [])


async def list_registered_models() -> set[str]:
    """Every model_name the proxy currently routes — config.yaml + DB rows.

    Same endpoint scripts/sync-models-to-db.sh uses. A name missing here is a
    name that would 400 with "Invalid model name" on inference.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        return {m["model_name"] for m in await _model_info(client) if m.get("model_name")}


async def delete_model(name: str) -> None:
    """Remove a model from the router.

    /model/delete takes the model's DB id, NOT its model_name — posting
    {"model_name": ...} returns 422 and silently leaves the entry in place. So
    resolve the id via /model/info first.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        model_id = next(
            (
                (m.get("model_info") or {}).get("id")
                for m in await _model_info(client)
                if m.get("model_name") == name
            ),
            None,
        )
        if not model_id:
            # Not in the router (already gone, or never registered) — nothing
            # to remove. Same tolerance the old 404 branch aimed for.
            return
        r = await client.post(
            f"{LITELLM_URL}/model/delete", json={"id": model_id}, headers=_headers()
        )
        if r.status_code == 404:
            return
        r.raise_for_status()
