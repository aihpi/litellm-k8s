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


async def delete_model(name: str) -> None:
    body = {"model_name": name}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{LITELLM_URL}/model/delete", json=body, headers=_headers()
        )
        if r.status_code == 404:
            return
        r.raise_for_status()
