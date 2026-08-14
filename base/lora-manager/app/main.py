import asyncio
import hashlib
import logging
import shutil
import tarfile
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NamedTuple

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from . import audit, litellm_client, reconcile, validation, vllm_client
from .config import (
    ADAPTERS_BASE_PATH,
    ALLOWED_BASE_MODELS,
    LITELLM_MASTER_KEY,
    MAX_UPLOAD_BYTES,
    REQUIRE_IDENTITY,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("lora-manager")

# httpx logs every request URL at INFO. /key/info carries the caller's plaintext
# virtual key in its query string, so leaving this at INFO writes usable user
# keys into the pod log on the ordinary success path — a far weaker grant than
# read access to litellm-secret would be. Verified with a log-capture test.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fire and forget: /health backs the readiness probe and must answer while
    # the first pass is still retrying LiteLLM (which may be restarting too).
    task = asyncio.create_task(reconcile.reconcile_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="lora-manager", version="0.2.0", lifespan=lifespan)


class Identity(NamedTuple):
    user_id: str
    key_alias: str
    # True when the caller authenticated with LITELLM_MASTER_KEY, which is not a
    # user and therefore can't own anything — but does count as ops.
    is_master: bool


def _bearer(request: Request) -> str | None:
    """The caller's LiteLLM key.

    x-litellm-api-key is checked too, because LiteLLM accepts it *in preference
    to* Authorization. Reading only Authorization would let a caller authenticate
    to the proxy via x-litellm-api-key while presenting no token here, so key
    resolution would be skipped entirely.
    """
    for raw in (
        request.headers.get("x-litellm-api-key"),
        request.headers.get("authorization"),
    ):
        if not raw:
            continue
        scheme, _, rest = raw.partition(" ")
        token = (rest if scheme.lower() == "bearer" else raw).strip()
        if token:
            return token
    return None


async def _identity(
    request: Request,
    x_litellm_user_id: str | None,
    x_litellm_key_alias: str | None,
    internal: bool = False,
) -> Identity:
    """Who is calling.

    The bearer token is the authoritative source: LiteLLM's pass-through
    forwards `authorization` but not the identity headers, and a header could be
    forged by anything able to reach this service inside the namespace whereas a
    key has to survive LiteLLM's own validation. Headers remain a fallback for
    direct in-cluster callers that have no key.

    `internal=True` marks the in-cluster routes (kubectl exec / port-forward).
    Those may fall back to identity headers and are never refused for lacking an
    identity: refusing there would close the ops escape hatch for adapters that
    have no owner, which is the one reason those routes exist. They still get
    Identity("anonymous", ...), which is never ops.
    """
    token = _bearer(request)
    if token and LITELLM_MASTER_KEY and token == LITELLM_MASTER_KEY:
        return Identity("master-key", "master-key", True)

    if token:
        try:
            info = await litellm_client.key_info(token)
        except Exception as e:
            # Never log the exception message: httpx builds it from the request
            # URL, which carries the caller's plaintext key.
            log.warning("key_info lookup failed: %s", type(e).__name__)
            info = None
        if info:
            user_id = info.get("user_id") or info.get("key_alias")
            key_alias = info.get("key_alias") or info.get("user_id")
            if user_id:
                return Identity(str(user_id), str(key_alias or user_id), False)
            log.warning("key resolved but carries neither user_id nor key_alias")

    # Identity headers are caller-controlled — LiteLLM's pass-through relays client
    # headers verbatim, so anyone who can reach /v1/lora/* could send
    # x-litellm-user-id: <victim> and inherit their adapters. So they are trusted
    # ONLY on the in-cluster routes, which are network-restricted and have no
    # ownership check to subvert anyway. Note this is deliberately not keyed off
    # REQUIRE_IDENTITY: turning that flag off must not re-open spoofing.
    if not internal:
        if REQUIRE_IDENTITY:
            raise HTTPException(
                status_code=401,
                detail="could not determine caller identity from the API key — an "
                "adapter with no owner cannot be deleted afterwards. Use a "
                "personal sk- key that has a user_id or key_alias assigned "
                "(set REQUIRE_IDENTITY=false to allow unattributed uploads)",
            )
        return Identity("anonymous", "anonymous", False)

    user_id = (
        x_litellm_user_id
        or request.headers.get("x-litellm-user-id")
        or request.headers.get("x-litellm-user")
        or request.headers.get("x-user-id")
        or "anonymous"
    )
    key_alias = (
        x_litellm_key_alias
        or request.headers.get("x-litellm-key-alias")
        or request.headers.get("x-litellm-key-name")
        or "anonymous"
    )
    return Identity(user_id, key_alias, False)


def _check_base_model(base_model: str) -> None:
    if base_model not in ALLOWED_BASE_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"base_model {base_model!r} not in allowlist {list(ALLOWED_BASE_MODELS)}",
        )


def _adapter_dir(base_model: str, name: str) -> Path:
    return Path(ADAPTERS_BASE_PATH) / base_model / name


def _is_ops(ident: Identity) -> bool:
    """Ops callers can delete any adapter and trigger a manual reconcile.

    The master key is the admin credential. Adapters with no ownership record
    are also removable via the in-cluster DELETE route, which has no ops check.
    """
    return ident.is_master


def _require_ops(ident: Identity) -> None:
    if not _is_ops(ident):
        raise HTTPException(status_code=403, detail="ops-only endpoint")


@app.get("/livez")
async def livez() -> dict:
    """Liveness only. Never fails on a dependency — a restart can't fix LiteLLM."""
    return {"status": "ok"}


@app.get("/health")
async def health() -> JSONResponse:
    """Readiness. 503 when the proxy rejects our master key, which a restart
    *does* fix (it re-reads litellm-secret), so it belongs in readiness rather
    than being discovered on the first failed upload."""
    body = {
        "status": "ok",
        "allowed_base_models": list(ALLOWED_BASE_MODELS),
        # True/False once checked, null before the first attempt or while
        # LiteLLM is simply unreachable.
        "litellm_auth_ok": reconcile.AUTH_OK,
    }
    if reconcile.AUTH_OK is False:
        body["status"] = "unhealthy"
        body["detail"] = (
            "LiteLLM rejected LITELLM_MASTER_KEY — litellm-secret was likely "
            "rotated after this pod started (env from secretKeyRef is not "
            "refreshed). Fix: kubectl rollout restart deploy/lora-manager"
        )
        return JSONResponse(status_code=503, content=body)
    return JSONResponse(status_code=200, content=body)


@app.post("/upload")
async def upload(
    request: Request,
    name: str = Form(...),
    base_model: str = Form(...),
    adapter: UploadFile = File(...),
    access: str | None = Form(None),
    x_litellm_user_id: str | None = Header(None),
    x_litellm_key_alias: str | None = Header(None),
) -> JSONResponse:
    ident = await _identity(request, x_litellm_user_id, x_litellm_key_alias)
    _check_base_model(base_model)
    try:
        validation.validate_name(name)
    except validation.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    target = _adapter_dir(base_model, name)
    if target.exists():
        raise HTTPException(
            status_code=409,
            detail=f"adapter {name!r} already exists on {base_model}; DELETE first",
        )

    # Streamed copy to /tmp with size guard + SHA256.
    upload_id = uuid.uuid4().hex
    tarball = Path(tempfile.gettempdir()) / f"{upload_id}.tar.gz"
    extract_to = Path(tempfile.gettempdir()) / upload_id
    sha = hashlib.sha256()
    bytes_seen = 0

    try:
        with tarball.open("wb") as out:
            while chunk := await adapter.read(1024 * 1024):
                bytes_seen += len(chunk)
                if bytes_seen > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload exceeds MAX_UPLOAD_BYTES={MAX_UPLOAD_BYTES}",
                    )
                sha.update(chunk)
                out.write(chunk)

        # filter="data" (PEP 706, stdlib since 3.11.4) refuses ../ traversal,
        # symlinks/hardlinks pointing outside, and device/FIFO members. Absolute
        # member paths are sanitised into extract_to rather than refused, so
        # /etc/pwned lands as etc/pwned — contained, and then rejected by
        # validate_adapter_dir's filename allowlist below. See test_extract.py.
        extract_to.mkdir(parents=True, exist_ok=False)
        try:
            with tarfile.open(tarball, "r:*") as tar:
                tar.extractall(extract_to, filter="data")
        except (tarfile.TarError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"tar extraction failed: {e}")

        # If the archive has a single top-level dir (common with tar czf -C ./output .
        # vs. tar czf ./output), unwrap it so files are at the root.
        contents = [p for p in extract_to.iterdir()]
        if len(contents) == 1 and contents[0].is_dir():
            extract_to = contents[0]

        try:
            summary = validation.validate_adapter_dir(extract_to)
        except validation.ValidationError as e:
            raise HTTPException(status_code=400, detail=f"validation failed: {e}")

        # Move validated dir onto the PVC. Use shutil.move to handle cross-device
        # rename (/tmp may be a different filesystem from /adapters).
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extract_to), str(target))

    finally:
        # Clean up tmp regardless of outcome. If we moved extract_to onto the
        # PVC, it no longer exists — ignore_errors handles that.
        if tarball.exists():
            tarball.unlink()
        shutil.rmtree(extract_to, ignore_errors=True)
        shutil.rmtree(Path(tempfile.gettempdir()) / upload_id, ignore_errors=True)

    # The adapter dir is now visible to a reconcile pass but isn't registered
    # yet — claim it so reconciliation doesn't race us to /model/new. Safe to
    # claim here rather than earlier: nothing above awaits between the move onto
    # the PVC and this line, so no other task can observe the gap.
    inflight = (base_model, name)
    reconcile.INFLIGHT.add(inflight)

    # vLLM mounts its own PVC at /adapters, so from its perspective the path is
    # /adapters/{name} (no {base_model} prefix).
    vllm_path = f"/adapters/{name}"
    rollback_actions: list = []
    try:
        try:
            # Hold the adapter lock across activation: a reconcile pass must not
            # slip between the PVC write and /model/new and register this adapter
            # itself (which would leave two router rows for one name).
            async with reconcile.adapter_lock(base_model, name):
                await vllm_client.load_adapter(base_model, name, vllm_path)
                rollback_actions.append(("unload-vllm", base_model, name))

                await litellm_client.register_model(name, base_model, access)
                rollback_actions.append(("delete-litellm", name))

                audit.log_event(
                    base_model,
                    {
                        "action": "upload",
                        "name": name,
                        "user_id": ident.user_id,
                        "key_alias": ident.key_alias,
                        "access": access,
                        "file_count": summary["file_count"],
                        "total_bytes": summary["total_bytes"],
                        "tensor_count": summary["tensor_count"],
                        "sha256": sha.hexdigest(),
                    },
                )
        except Exception as e:
            log.exception("upload failed after PVC write; rolling back")
            for action in reversed(rollback_actions):
                try:
                    if action[0] == "unload-vllm":
                        await vllm_client.unload_adapter(action[1], action[2])
                    elif action[0] == "delete-litellm":
                        await litellm_client.delete_model(action[1], base_model)
                except Exception:
                    log.exception("rollback step %s failed", action)
            shutil.rmtree(target, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"upload failed: {e}")
    finally:
        reconcile.INFLIGHT.discard(inflight)

    return JSONResponse(
        {
            "name": name,
            "base_model": base_model,
            "access": access,
            "vllm_loaded": True,
            "litellm_registered": True,
            "file_count": summary["file_count"],
            "total_bytes": summary["total_bytes"],
            "tensor_count": summary["tensor_count"],
        }
    )


@app.get("/adapters")
async def list_adapters() -> dict:
    out = {}
    for base_model in ALLOWED_BASE_MODELS:
        adapters = []
        for name in reconcile.adapter_names(base_model):
            # Owner/access come from the upload log — no network calls, so this
            # stays cheap. Callers need it to know what they're allowed to delete.
            record = audit.latest_upload(base_model, name)
            adapters.append(
                {
                    "name": name,
                    "owner": record.get("user_id") if record else None,
                    "access": record.get("access") if record else None,
                }
            )
        out[base_model] = adapters
    return out


async def _do_delete(
    base_model: str, name: str, ident: Identity, via: str, owner: str | None
) -> dict:
    """Unload, unregister, remove files. Best-effort: partial failures are
    reported rather than raised, so a half-gone adapter still gets cleaned up."""
    target = _adapter_dir(base_model, name)
    inflight = (base_model, name)
    reconcile.INFLIGHT.add(inflight)
    try:
        errors = []
        # The lock is what actually prevents a concurrent reconcile pass from
        # re-registering this adapter after we remove it: INFLIGHT alone is only
        # consulted when a pass builds its list, which may predate this call.
        async with reconcile.adapter_lock(base_model, name):
            try:
                await vllm_client.unload_adapter(base_model, name)
            except Exception as e:
                errors.append(f"vllm unload: {e}")
            try:
                await litellm_client.delete_model(name, base_model)
            except Exception as e:
                errors.append(f"litellm delete: {e}")
            shutil.rmtree(target, ignore_errors=True)
    finally:
        reconcile.INFLIGHT.discard(inflight)

    audit.log_event(
        base_model,
        {
            "action": "delete",
            "name": name,
            "user_id": ident.user_id,
            "key_alias": ident.key_alias,
            "owner": owner,
            "via": via,
            "errors": errors,
        },
    )

    return {"name": name, "base_model": base_model, "deleted": True, "errors": errors}


@app.post("/delete")
async def delete_adapter_api(
    request: Request,
    name: str = Form(...),
    base_model: str = Form(...),
    x_litellm_user_id: str | None = Header(None),
    x_litellm_key_alias: str | None = Header(None),
) -> dict:
    """User-facing delete, reached via the LiteLLM pass-through /v1/lora/delete.

    Fixed path with form fields rather than DELETE-with-path-params: LiteLLM's
    pass_through_endpoints matches exact paths.
    """
    ident = await _identity(request, x_litellm_user_id, x_litellm_key_alias)
    _check_base_model(base_model)
    try:
        validation.validate_name(name)
    except validation.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not _adapter_dir(base_model, name).exists():
        raise HTTPException(status_code=404, detail=f"adapter {name!r} not found")

    record = audit.latest_upload(base_model, name)
    owner = record.get("user_id") if record else None

    if not _is_ops(ident):
        # Deleting is destructive and attributable, so it needs real identity
        # even though REQUIRE_IDENTITY_HEADERS may be off for uploads.
        if ident.user_id == "anonymous":
            raise HTTPException(
                status_code=401,
                detail="delete requires user identity — request must go through "
                "the LiteLLM pass-through with a personal key",
            )
        if owner is None:
            raise HTTPException(
                status_code=403,
                detail=f"adapter {name!r} has no upload record (registered before "
                "ownership tracking) — ask the ops team to delete it",
            )
        if owner != ident.user_id:
            raise HTTPException(
                status_code=403,
                detail=f"adapter {name!r} belongs to another user",
            )

    return await _do_delete(base_model, name, ident, "api", owner)


@app.delete("/adapters/{base_model}/{name}")
async def delete_adapter(
    request: Request,
    base_model: str,
    name: str,
    x_litellm_user_id: str | None = Header(None),
    x_litellm_key_alias: str | None = Header(None),
) -> dict:
    """In-cluster admin delete. No ownership check — reachable only from inside
    the namespace (kubectl port-forward / exec), which is the existing posture.

    Identity is recorded when available but not required: this is the escape
    hatch for adapters with no owner, so demanding a resolvable caller here would
    make exactly those adapters undeletable.
    """
    ident = await _identity(
        request, x_litellm_user_id, x_litellm_key_alias, internal=True
    )
    _check_base_model(base_model)
    try:
        validation.validate_name(name)
    except validation.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not _adapter_dir(base_model, name).exists():
        raise HTTPException(status_code=404, detail=f"adapter {name!r} not found")

    record = audit.latest_upload(base_model, name)
    owner = record.get("user_id") if record else None
    return await _do_delete(base_model, name, ident, "internal", owner)


@app.get("/reconcile/status")
async def reconcile_status() -> dict:
    return {
        "last_result": reconcile.LAST_RESULT,
        "inflight": sorted(f"{b}/{n}" for b, n in reconcile.INFLIGHT),
    }


@app.post("/reconcile")
async def reconcile_now(
    request: Request,
    x_litellm_user_id: str | None = Header(None),
    x_litellm_key_alias: str | None = Header(None),
) -> dict:
    # internal=True so a keyless in-cluster caller gets the accurate "ops-only"
    # 403 from _require_ops rather than a confusing identity error. "anonymous"
    # is never ops, so this doesn't widen access.
    ident = await _identity(
        request, x_litellm_user_id, x_litellm_key_alias, internal=True
    )
    _require_ops(ident)
    return await reconcile.reconcile_all()
