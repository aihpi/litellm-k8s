import asyncio
import hashlib
import logging
import shutil
import tarfile
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from . import audit, litellm_client, reconcile, validation, vllm_client
from .config import (
    ADAPTERS_BASE_PATH,
    ADMIN_KEY_ALIASES,
    ADMIN_USER_IDS,
    ALLOWED_BASE_MODELS,
    LOG_HEADERS_ON_UPLOAD,
    MAX_UPLOAD_BYTES,
    REQUIRE_IDENTITY_HEADERS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("lora-manager")


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


def _identity(
    request: Request,
    x_litellm_user_id: str | None,
    x_litellm_key_alias: str | None,
) -> tuple[str, str]:
    if LOG_HEADERS_ON_UPLOAD:
        # Redact bearer tokens before logging.
        safe = {
            k: ("Bearer <redacted>" if k.lower() == "authorization" else v)
            for k, v in request.headers.items()
        }
        log.info("incoming /upload headers: %s", safe)

    # Best-effort identity. Try the documented LiteLLM headers first, then a
    # few alternate spellings we've seen in different versions, then fall back
    # to "anonymous" if we genuinely have nothing.
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

    if REQUIRE_IDENTITY_HEADERS and user_id == "anonymous":
        raise HTTPException(
            status_code=401,
            detail="missing user identity header — request must go through LiteLLM pass-through",
        )
    return (user_id, key_alias)


def _check_base_model(base_model: str) -> None:
    if base_model not in ALLOWED_BASE_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"base_model {base_model!r} not in allowlist {list(ALLOWED_BASE_MODELS)}",
        )


def _adapter_dir(base_model: str, name: str) -> Path:
    return Path(ADAPTERS_BASE_PATH) / base_model / name


def _is_ops(user_id: str, key_alias: str) -> bool:
    """Ops callers can delete any adapter and trigger a manual reconcile."""
    return user_id in ADMIN_USER_IDS or key_alias in ADMIN_KEY_ALIASES


def _require_ops(user_id: str, key_alias: str) -> None:
    if not _is_ops(user_id, key_alias):
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
    user_id, key_alias = _identity(request, x_litellm_user_id, x_litellm_key_alias)
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

        # Extract with hardening: reject absolute paths, ../, symlinks, devices.
        extract_to.mkdir(parents=True, exist_ok=False)
        try:
            with tarfile.open(tarball, "r:*") as tar:
                _safe_extract(tar, extract_to)
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
            await vllm_client.load_adapter(base_model, name, vllm_path)
            rollback_actions.append(("unload-vllm", base_model, name))

            await litellm_client.register_model(name, base_model, access)
            rollback_actions.append(("delete-litellm", name))

            audit.log_event(
                base_model,
                {
                    "action": "upload",
                    "name": name,
                    "user_id": user_id,
                    "key_alias": key_alias,
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
                        await litellm_client.delete_model(action[1])
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
    base_model: str, name: str, user_id: str, key_alias: str, via: str, owner: str | None
) -> dict:
    """Unload, unregister, remove files. Best-effort: partial failures are
    reported rather than raised, so a half-gone adapter still gets cleaned up."""
    target = _adapter_dir(base_model, name)
    inflight = (base_model, name)
    reconcile.INFLIGHT.add(inflight)
    try:
        errors = []
        try:
            await vllm_client.unload_adapter(base_model, name)
        except Exception as e:
            errors.append(f"vllm unload: {e}")
        try:
            await litellm_client.delete_model(name)
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
            "user_id": user_id,
            "key_alias": key_alias,
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
    user_id, key_alias = _identity(request, x_litellm_user_id, x_litellm_key_alias)
    _check_base_model(base_model)
    try:
        validation.validate_name(name)
    except validation.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not _adapter_dir(base_model, name).exists():
        raise HTTPException(status_code=404, detail=f"adapter {name!r} not found")

    record = audit.latest_upload(base_model, name)
    owner = record.get("user_id") if record else None

    if not _is_ops(user_id, key_alias):
        # Deleting is destructive and attributable, so it needs real identity
        # even though REQUIRE_IDENTITY_HEADERS may be off for uploads.
        if user_id == "anonymous":
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
        if owner != user_id:
            raise HTTPException(
                status_code=403,
                detail=f"adapter {name!r} belongs to another user",
            )

    return await _do_delete(base_model, name, user_id, key_alias, "api", owner)


@app.delete("/adapters/{base_model}/{name}")
async def delete_adapter(
    request: Request,
    base_model: str,
    name: str,
    x_litellm_user_id: str | None = Header(None),
    x_litellm_key_alias: str | None = Header(None),
) -> dict:
    """In-cluster admin delete. No ownership check — reachable only from inside
    the namespace (kubectl port-forward / exec), which is the existing posture."""
    user_id, key_alias = _identity(request, x_litellm_user_id, x_litellm_key_alias)
    _check_base_model(base_model)
    try:
        validation.validate_name(name)
    except validation.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not _adapter_dir(base_model, name).exists():
        raise HTTPException(status_code=404, detail=f"adapter {name!r} not found")

    record = audit.latest_upload(base_model, name)
    owner = record.get("user_id") if record else None
    return await _do_delete(base_model, name, user_id, key_alias, "internal", owner)


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
    user_id, key_alias = _identity(request, x_litellm_user_id, x_litellm_key_alias)
    _require_ops(user_id, key_alias)
    return await reconcile.reconcile_all()


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """tarfile.extractall but blocks path traversal, abs paths, and special files."""
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        if member.isdev() or member.issym() or member.islnk():
            raise ValueError(f"refusing to extract special file: {member.name}")
        target = (dest / member.name).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError:
            raise ValueError(f"path traversal in archive: {member.name}")
    tar.extractall(dest)
