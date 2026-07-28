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
from datetime import datetime, timezone
from pathlib import Path

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

# Single replica (see deployment.yaml), so a plain lock is enough to keep the
# periodic loop and a manual POST /reconcile from overlapping.
_LOCK = asyncio.Lock()


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


async def _reconcile_base_model(base_model: str, registered: set[str] | None) -> dict:
    """One base model. `registered` is None when LiteLLM couldn't be reached."""
    out: dict = {
        "adapters": [],
        "loaded_in_vllm": [],
        "registered_in_litellm": [],
        "adopted_without_metadata": [],
        "stale_in_litellm": [],
        "skipped_inflight": [],
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
                await vllm_client.load_adapter(base_model, name, f"/adapters/{name}")
                out["loaded_in_vllm"].append(name)
                log.info("reconcile: loaded %s into vllm %s", name, base_model)
            except Exception as e:
                out["errors"].append(f"vllm load {name}: {e}")

    # --- LiteLLM side ---
    if registered is not None:
        for name in names:
            if name in registered:
                continue
            record = audit.latest_upload(base_model, name)
            if record is None and not RECONCILE_ADOPT_UNKNOWN:
                out["errors"].append(
                    f"litellm register {name}: no upload record and "
                    "RECONCILE_ADOPT_UNKNOWN is off"
                )
                continue
            access = record.get("access") if record else None
            try:
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
            result["errors"].append(f"litellm model/info: {e}")
            log.warning("reconcile: LiteLLM unreachable (%s) — vLLM half only", e)
            registered = None

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
