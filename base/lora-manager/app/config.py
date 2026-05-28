import os


def _bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY")
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm-service:4000")
ADAPTERS_BASE_PATH = os.environ.get("ADAPTERS_BASE_PATH", "/adapters")
ALLOWED_BASE_MODELS = tuple(
    s.strip()
    for s in os.environ.get("ALLOWED_BASE_MODELS", "ministral-3-14b").split(",")
    if s.strip()
)
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(4 * 1024 * 1024 * 1024)))
MAX_LORA_RANK = int(os.environ.get("MAX_LORA_RANK", "64"))

REQUIRE_IDENTITY_HEADERS = _bool(os.environ.get("REQUIRE_IDENTITY_HEADERS"), True)
