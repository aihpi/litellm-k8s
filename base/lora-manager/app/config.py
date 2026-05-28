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

# LiteLLM's pass_through_endpoints with forward_headers:true does NOT inject
# x-litellm-user-id reliably across versions. Default to permissive so uploads
# aren't blocked on identity — audit log records "anonymous" when missing.
# Re-enable strict mode once we've confirmed which headers LiteLLM actually
# forwards (see LOG_HEADERS_ON_UPLOAD below).
REQUIRE_IDENTITY_HEADERS = _bool(os.environ.get("REQUIRE_IDENTITY_HEADERS"), False)

# Dump all incoming headers to the log on each /upload. Temporary, for
# figuring out LiteLLM's actual forwarded-header set. Disable once known.
LOG_HEADERS_ON_UPLOAD = _bool(os.environ.get("LOG_HEADERS_ON_UPLOAD"), True)
