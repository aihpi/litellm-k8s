import httpx


def _service_url(base_model: str) -> str:
    return f"http://{base_model}-service:8000"


async def load_adapter(base_model: str, lora_name: str, lora_path: str) -> None:
    """Tell vLLM to load a LoRA adapter from a local path.

    `lora_path` is the path inside the vLLM pod (always /adapters/{name}, since
    vLLM mounts its per-model PVC at /adapters and lora-manager wrote there).
    """
    url = f"{_service_url(base_model)}/v1/load_lora_adapter"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, json={"lora_name": lora_name, "lora_path": lora_path})
        r.raise_for_status()


async def unload_adapter(base_model: str, lora_name: str) -> None:
    url = f"{_service_url(base_model)}/v1/unload_lora_adapter"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json={"lora_name": lora_name})
        # Be tolerant on unload: 404 = already gone, treat as success.
        if r.status_code == 404:
            return
        r.raise_for_status()


async def list_loaded_models(base_model: str) -> list[str]:
    url = f"{_service_url(base_model)}/v1/models"
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    return [item["id"] for item in data.get("data", [])]
