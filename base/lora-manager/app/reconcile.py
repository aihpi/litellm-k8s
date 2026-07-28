"""Reconcile the adapters PVC against vLLM and LiteLLM.

The PVC is the source of truth. Anything sitting in /adapters/{base_model}/{name}
should be loaded in the matching vLLM pod and registered in LiteLLM's router;
this module puts that back whenever it drifts.

Why it exists: a litellm-proxy restart rebuilds the router from config.yaml plus
Postgres rows flagged db_model=true. Adapters registered without that flag (or
before store_model_in_db was enabled) silently vanish from the router while
their files and their vLLM slot stay perfectly healthy — inference then fails
with "400: Invalid model name". vLLM's side is already self-healing via the
--lora-modules auto-discovery wrapper in the model deployments; LiteLLM's isn't.

Deliberately additive: entries are never removed from LiteLLM here. The proxy
also routes base models and non-adapter models from config.yaml, and a pruning
bug there breaks unrelated traffic. Missing files are reported, not acted on.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import audit, litellm_client, vllm_client
from .config import (
    ADAPTERS_BASE_PATH,
    ALLOWED_BASE_MODELS,
    RECONCILE_ADOPT_UNKNOWN,
    RECONCILE_INTERVAL_SECONDS,
)

log = logging.getLogger("lora-manager.reconcile")

# (base_model, name) pairs currently being uploaded or deleted. A reconcile pass
# skips these: an upload is not yet registered between the PVC write and its
# /model/new call, and re-registering underneath it would fail on the duplicate.
INFLIGHT: set[tuple[str, str]] = set()

LAST_RESULT: dict | None = None

# Whether LiteLLM accepted our master key. None until the first attempt.
# LITELLM_MASTER_KEY comes from a secretKeyRef, which is injected at pod start
# and never refreshed, so rotating litellm-secret leaves this pod holding a dead
# key — every /model/new and /model/delete then 401s while uploads appear to
# work right up to the register step. Surfacing it on /health turns that into a
# NotReady pod instead of a silent write failure.
AUTH_OK: bool | None = None

# Single replica (see deployment.yaml, strategy: Recreate), so a plain lock is
# enough to keep the periodic loop and a manual POST /reconcile from overlapping.
_LOCK = asyncio.Lock()

# One lock per adapter, shared with the upload and delete handlers.
#
# INFLIGHT alone is not sufficient: a pass filters the adapter list once and then
# awaits (load_adapter allows 60s), so a delete that starts *after* that snapshot
# can finish entirely inside one await, and the pass would then re-register an
# adapter whose files it just watched disappear. That leaves a db_model=true row
# pointing at an unloaded LoRA which survives every restart, is never pruned
# (this module is additive only), and which neither delete route can remove
# because both 404 once the directory is gone.
#
# Never pruned: dropping an entry while another coroutine is waiting on it would
# let a third create a fresh lock and defeat the mutual exclusion. Adapter names
# are bounded and validated, so the dict stays small.
_NAME_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


@asynccontextmanager
async def adapter_lock(base_model: str, name: str):
    """Serialise mutations of one adapter across reconcile, upload and delete."""
    lock = _NAME_LOCKS.setdefault((base_model, name), asyncio.Lock())
    async with lock:
        yield


def _note_auth(exc: Exception | None) -> None:
    global AUTH_OK
    if exc is None:
        if AUTH_OK is False:
            log.info("litellm credentials accepted again")
        AUTH_OK = True
    elif isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
        if AUTH_OK is not False:
            log.error(
                "litellm rejected LITELLM_MASTER_KEY (%s) — litellm-secret was "
                "probably rotated after this pod started; "
                "kubectl rollout restart deploy/lora-manager",
                exc.response.status_code,
            )
        AUTH_OK = False
    # Anything else (connection refused, timeout, 5xx) is LiteLLM being down,
    # not a credential problem. Leave the last known verdict alone so a proxy
    # restart doesn't drag this pod out of the Service.


async def check_credentials() -> bool | None:
    """One authenticated call, purely to classify the master key."""
    try:
        await litellm_client.list_registered_models()
    except Exception as e:
        _note_auth(e)
    else:
        _note_auth(None)
    return AUTH_OK


def adapter_names(base_model: str) -> list[str]:
    """Adapter dirs on the PVC for one base model.

    Dotfiles are skipped, which keeps .upload-log.jsonl out and matches the
    `/adapters/*/` glob the vLLM startup wrapper uses (sh globs don't match
    leading dots either), so both sides agree on what an adapter is.
    """
    model_dir = Path(ADAPTERS_BASE_PATH) / base_model
    if not model_dir.is_dir():
        return []
    return sorted(
        p.name for p in model_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


def _still_present(base_model: str, name: str) -> bool:
    """Re-check under the adapter lock, immediately before mutating anything.

    The pass's file listing is a snapshot; by the time we act on an entry a
    delete may have removed it. Acting on a stale entry is how a deleted adapter
    gets resurrected in the router.
    """
    if (base_model, name) in INFLIGHT:
        return False
    return (Path(ADAPTERS_BASE_PATH) / base_model / name).is_dir()


async def _reconcile_base_model(base_model: str, registered: set[str] | None) -> dict:
    """One base model. `registered` is None when LiteLLM couldn't be reached."""
    out: dict = {
        "adapters": [],
        "loaded_in_vllm": [],
        "registered_in_litellm": [],
        "adopted_without_metadata": [],
        "stale_in_litellm": [],
        "skipped_inflight": [],
        "vanished_mid_pass": [],
        "errors": [],
    }

    names = adapter_names(base_model)
    out["adapters"] = names
    inflight = [n for n in names if (base_model, n) in INFLIGHT]
    out["skipped_inflight"] = inflight
    names = [n for n in names if n not in inflight]

    # --- vLLM side ---
    try:
        loaded = set(await vllm_client.list_loaded_models(base_model))
    except Exception as e:
        out["errors"].append(f"vllm list: {e}")
        loaded = None

    if loaded is not None:
        for name in names:
            if name in loaded:
                continue
            # vLLM mounts its own per-model PVC at /adapters, so from inside that
            # pod the path has no base_model prefix.
            try:
                async with adapter_lock(base_model, name):
                    if not _still_present(base_model, name):
                        out["vanished_mid_pass"].append(name)
                        continue
                    await vllm_client.load_adapter(
                        base_model, name, f"/adapters/{name}"
                    )
                out["loaded_in_vllm"].append(name)
                log.info("reconcile: loaded %s into vllm %s", name, base_model)
            except Exception as e:
                out["errors"].append(f"vllm load {name}: {e}")

    # --- LiteLLM side ---
    if registered is not None:
        for name in names:
            if name in registered:
                continue
            # Metadata comes from the raw event, NOT from latest_upload(): that
            # helper nulls anonymously-recorded uploads because they have no
            # *owner*, and reading `access` through it would silently publish a
            # restricted adapter the moment it needed re-registering.
            record = audit.latest_upload_event(base_model, name)
            if record is None and not RECONCILE_ADOPT_UNKNOWN:
                out["errors"].append(
                    f"litellm register {name}: no upload record and "
                    "RECONCILE_ADOPT_UNKNOWN is off"
                )
                continue
            access = record.get("access") if record else None
            try:
                async with adapter_lock(base_model, name):
                    if not _still_present(base_model, name):
                        out["vanished_mid_pass"].append(name)
                        continue
                    await litellm_client.register_model(name, base_model, access)
            except Exception as e:
                out["errors"].append(f"litellm register {name}: {e}")
                continue
            out["registered_in_litellm"].append(name)
            if record is None:
                out["adopted_without_metadata"].append(name)
                log.warning(
                    "reconcile: registered %s on %s with no upload record — "
                    "no access group applied",
                    name,
                    base_model,
                )
            else:
                log.info(
                    "reconcile: re-registered %s on %s (access=%s)",
                    name,
                    base_model,
                    access,
                )

        # Report-only: adapters we know were uploaded here, still in the router,
        # but with no files left on the PVC.
        try:
            uploaded_here = {
                e.get("name")
                for e in audit.read_log(base_model)
                if e.get("action") == "upload" and e.get("name")
            }
        except Exception as e:
            out["errors"].append(f"audit read: {e}")
            uploaded_here = set()
        out["stale_in_litellm"] = sorted(
            (uploaded_here & registered) - set(out["adapters"])
        )

    return out


async def reconcile_all() -> dict:
    """One full pass over every allowed base model. Never raises."""
    global LAST_RESULT

    async with _LOCK:
        started = time.monotonic()
        result: dict = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "per_base_model": {},
            "errors": [],
        }

        try:
            registered = await litellm_client.list_registered_models()
        except Exception as e:
            # Expected while LiteLLM is still coming up after a co-restart. The
            # vLLM half still runs; the next tick retries this half.
            _note_auth(e)
            result["errors"].append(f"litellm model/info: {e}")
            log.warning("reconcile: LiteLLM unreachable (%s) — vLLM half only", e)
            registered = None
        else:
            _note_auth(None)

        for base_model in ALLOWED_BASE_MODELS:
            try:
                result["per_base_model"][base_model] = await _reconcile_base_model(
                    base_model, registered
                )
            except Exception as e:
                log.exception("reconcile: %s failed", base_model)
                result["per_base_model"][base_model] = {"errors": [str(e)]}

        result["duration_s"] = round(time.monotonic() - started, 3)
        LAST_RESULT = result

    changed = sum(
        len(v.get("loaded_in_vllm", [])) + len(v.get("registered_in_litellm", []))
        for v in result["per_base_model"].values()
    )
    log.info(
        "reconcile: done in %ss, %d repair(s)", result["duration_s"], changed
    )
    return result


async def reconcile_loop() -> None:
    """Background pass every RECONCILE_INTERVAL_SECONDS. Must never die."""
    if RECONCILE_INTERVAL_SECONDS <= 0:
        log.info("reconcile: background loop disabled (RECONCILE_INTERVAL_SECONDS=0)")
        # Still classify the master key once, so /health reports it either way.
        await check_credentials()
        return
    log.info("reconcile: loop starting, interval %ss", RECONCILE_INTERVAL_SECONDS)
    while True:
        try:
            await reconcile_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reconcile: pass raised, continuing")
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
