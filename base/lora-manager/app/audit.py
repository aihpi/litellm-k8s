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
