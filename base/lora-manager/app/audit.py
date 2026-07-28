import json
from datetime import datetime, timezone
from pathlib import Path

from .config import ADAPTERS_BASE_PATH


def _log_path(base_model: str) -> Path:
    return Path(ADAPTERS_BASE_PATH) / base_model / ".upload-log.jsonl"


def log_event(base_model: str, event: dict) -> None:
    """Append a JSONL audit entry. base_model dir must exist (it does after
    upload; for delete, file is left in place even if the dir is torn down)."""
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    path = _log_path(base_model)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(event) + "\n")


def latest_upload_event(base_model: str, name: str) -> dict | None:
    """Most recent upload event for `name`, verbatim — no ownership judgement.

    Use this for adapter METADATA (notably `access`). It returns the record even
    when the uploader was unidentified, which matters because an anonymously
    recorded upload can still carry an access group that must be preserved.
    """
    for event in reversed(read_log(base_model)):
        if event.get("action") == "upload" and event.get("name") == name:
            return event
    return None


def latest_upload(base_model: str, name: str) -> dict | None:
    """The ownership record for an adapter, or None if it has no owner.

    Use this for AUTHORIZATION only. A recorded user_id of "anonymous" is not
    ownership: uploads made while identity was unenforced must not become
    deletable by any other caller who also arrives unidentified. Callers that
    want the adapter's metadata regardless of attribution want
    latest_upload_event() instead — conflating the two silently drops the
    access group of anonymously recorded uploads.
    """
    event = latest_upload_event(base_model, name)
    if event is None or event.get("user_id", "anonymous") == "anonymous":
        return None
    return event


def read_log(base_model: str) -> list[dict]:
    path = _log_path(base_model)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries
