import httpx

from .config import DEFAULT_ACCESS_GROUP, LITELLM_MASTER_KEY, LITELLM_URL


def _headers() -> dict:
    if not LITELLM_MASTER_KEY:
        raise RuntimeError("LITELLM_MASTER_KEY not set; cannot call LiteLLM admin API")
    return {
        "Authorization": f"Bearer {LITELLM_MASTER_KEY}",
        "Content-Type": "application/json",
    }


def _api_base(base_model: str) -> str:
    """The vLLM service a given base model's adapters are served from.

    Also the field that distinguishes two router entries sharing a model_name.
    """
    return f"http://{base_model}-service:8000/v1"


async def register_model(name: str, base_model: str, access: str | None) -> None:
    """Add a new model entry to LiteLLM pointing at the vLLM service.

    Access group MUST be set at registration time: model_info.access_groups is
    read only when the row is created, and LiteLLM exposes no way to retrofit
    per-key visibility afterwards — changing it means delete + re-register. The
    field name has moved across upstream releases (access_groups / team_id /
    model_access_group); access_groups is correct for the pinned tool-litellm.
    """
    body = {
        "model_name": name,
        "litellm_params": {
            "model": f"openai/{name}",
            "api_base": _api_base(base_model),
            "api_key": "dummy",
        },
    }
    effective_access = access or DEFAULT_ACCESS_GROUP
    if effective_access:
        body["model_info"] = {"access_groups": [effective_access]}

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

    The key travels in the query string because that is the endpoint's contract,
    which makes every error path a potential secret leak: httpx's own logger
    prints the request URL at INFO, and raise_for_status() embeds it in the
    exception message. So httpx logging is pinned in main.py, and any non-2xx is
    re-raised here as a message that names only the status code — never let the
    original exception escape this function.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{LITELLM_URL}/key/info", params={"key": api_key}, headers=_headers()
        )
        if r.status_code in (400, 401, 404):
            return None
        if r.status_code >= 400:
            raise RuntimeError(f"/key/info returned HTTP {r.status_code}")
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


async def delete_model(name: str, base_model: str) -> None:
    """Remove this adapter's own router entry.

    /model/delete takes the model's DB id, NOT its model_name — posting
    {"model_name": ...} returns 422 and silently leaves the entry in place. So
    resolve the id via /model/info first.

    model_name is NOT unique in the router: /upload's 409 is per (base_model,
    name), so the same adapter name can be registered against two base models,
    and config.yaml entries share the namespace too. Matching on the name alone
    would delete whichever row /model/info happened to list first — possibly a
    different base model's healthy adapter, or a config.yaml model. So match on
    api_base as well, which is what actually identifies the deployment, and
    delete every row that matches this adapter (a duplicate can exist if a
    previous pass registered it twice).
    """
    want_api_base = _api_base(base_model)
    async with httpx.AsyncClient(timeout=30.0) as client:
        ids = [
            (m.get("model_info") or {}).get("id")
            for m in await _model_info(client)
            if m.get("model_name") == name
            and (m.get("litellm_params") or {}).get("api_base") == want_api_base
        ]
        ids = [i for i in ids if i]
        if not ids:
            # Not in the router (already gone, or never registered) — nothing
            # to remove. Same tolerance the old 404 branch aimed for.
            return
        for model_id in ids:
            r = await client.post(
                f"{LITELLM_URL}/model/delete", json={"id": model_id}, headers=_headers()
            )
            if r.status_code == 404:
                continue
            r.raise_for_status()
