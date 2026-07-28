import os


def _bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def _csv(v: str | None) -> tuple[str, ...]:
    return tuple(s.strip() for s in (v or "").split(",") if s.strip())


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

# Callers allowed to delete any adapter (not just their own) and to trigger a
# manual reconcile. Matched against the LiteLLM identity headers. Both empty
# means nobody is ops, so adapters with no ownership record can only be removed
# via the in-cluster DELETE route.
ADMIN_USER_IDS = _csv(os.environ.get("ADMIN_USER_IDS"))
ADMIN_KEY_ALIASES = _csv(os.environ.get("ADMIN_KEY_ALIASES"))

# Reconciliation: the adapters PVC is the source of truth. Every pass re-loads
# adapters missing from vLLM and re-registers ones missing from LiteLLM's
# router (the gemma-4-31b-leo failure mode). 0 disables the background loop but
# keeps POST /reconcile.
RECONCILE_INTERVAL_SECONDS = int(os.environ.get("RECONCILE_INTERVAL_SECONDS", "300"))

# Register adapter dirs that have no upload record in the audit log (uploaded
# before this service existed, or by hand). They get no access group, so turn
# this off if an adapter ever needs to be private-by-default.
RECONCILE_ADOPT_UNKNOWN = _bool(os.environ.get("RECONCILE_ADOPT_UNKNOWN"), True)

# Confirmed empirically (July 2026): pass_through_endpoints with
# forward_headers:true forwards `authorization` but NOT x-litellm-user-id or
# x-litellm-key-alias. So identity is resolved from the caller's bearer token
# via GET /key/info, and the headers are only a fallback for direct in-cluster
# callers. Refuse the request when nothing resolves — an upload with no owner is
# an adapter nobody can delete afterwards.
REQUIRE_IDENTITY = _bool(os.environ.get("REQUIRE_IDENTITY"), True)

# Dump all incoming headers to the log on each /upload. Answered its question
# (see above) — off by default now; flip on to debug a new proxy version.
LOG_HEADERS_ON_UPLOAD = _bool(os.environ.get("LOG_HEADERS_ON_UPLOAD"), False)
