import hashlib
import logging
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from . import audit, litellm_client, validation, vllm_client
from .config import (
    ADAPTERS_BASE_PATH,
    ALLOWED_BASE_MODELS,
    LOG_HEADERS_ON_UPLOAD,
    MAX_UPLOAD_BYTES,
    REQUIRE_IDENTITY_HEADERS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("lora-manager")

app = FastAPI(title="lora-manager", version="0.1.0")


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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "allowed_base_models": list(ALLOWED_BASE_MODELS)}


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

    # vLLM mounts its own PVC at /adapters, so from its perspective the path is
    # /adapters/{name} (no {base_model} prefix).
    vllm_path = f"/adapters/{name}"
    rollback_actions: list = []
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
        model_dir = Path(ADAPTERS_BASE_PATH) / base_model
        if not model_dir.is_dir():
            out[base_model] = []
            continue
        adapters = []
        for sub in sorted(model_dir.iterdir()):
            if sub.is_dir() and not sub.name.startswith("."):
                adapters.append({"name": sub.name})
        out[base_model] = adapters
    return out


@app.delete("/adapters/{base_model}/{name}")
async def delete_adapter(
    request: Request,
    base_model: str,
    name: str,
    x_litellm_user_id: str | None = Header(None),
    x_litellm_key_alias: str | None = Header(None),
) -> dict:
    user_id, key_alias = _identity(request, x_litellm_user_id, x_litellm_key_alias)
    _check_base_model(base_model)
    try:
        validation.validate_name(name)
    except validation.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    target = _adapter_dir(base_model, name)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"adapter {name!r} not found")

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

    audit.log_event(
        base_model,
        {
            "action": "delete",
            "name": name,
            "user_id": user_id,
            "key_alias": key_alias,
            "errors": errors,
        },
    )

    return {"name": name, "base_model": base_model, "deleted": True, "errors": errors}


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
