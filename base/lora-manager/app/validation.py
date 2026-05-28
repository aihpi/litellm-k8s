import json
import re
from pathlib import Path

from .config import MAX_LORA_RANK

# Files we accept inside an adapter archive. Anything else fails the upload.
# safetensors-only (no pickled .bin), config + tokenizer aux files allowed.
ALLOWED_FILES = {
    "adapter_config.json",
    "adapter_model.safetensors",
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "README.md",
}
ALLOWED_SUFFIXES = (".safetensors",)

# Header length cap: safetensors header is JSON tensor metadata. 100MB is
# extreme — real adapter headers are tens of KB.
MAX_HEADER_BYTES = 100 * 1024 * 1024

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class ValidationError(Exception):
    pass


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise ValidationError(
            f"name {name!r} must match {NAME_RE.pattern} "
            "(lowercase alphanumeric + hyphen, 1-63 chars, must start alphanumeric)"
        )


def _parse_safetensors_header(path: Path) -> dict:
    """Parse the safetensors header bytes without loading any tensor data.

    Format: u64 LE header length, then JSON metadata, then tensor bytes.
    """
    with path.open("rb") as f:
        length_bytes = f.read(8)
        if len(length_bytes) < 8:
            raise ValidationError(f"{path.name}: truncated safetensors file")
        header_len = int.from_bytes(length_bytes, "little", signed=False)
        if header_len == 0 or header_len > MAX_HEADER_BYTES:
            raise ValidationError(
                f"{path.name}: implausible header length {header_len}"
            )
        header_bytes = f.read(header_len)
        if len(header_bytes) < header_len:
            raise ValidationError(f"{path.name}: header truncated")
        try:
            return json.loads(header_bytes)
        except json.JSONDecodeError as e:
            raise ValidationError(f"{path.name}: header is not valid JSON ({e})")


def validate_adapter_dir(adapter_dir: Path) -> dict:
    """Validate every file in the extracted archive.

    Returns a summary dict with file_count, total_bytes, tensor_count.
    Raises ValidationError on any failure.
    """
    if not adapter_dir.is_dir():
        raise ValidationError(f"{adapter_dir} is not a directory")

    files = [p for p in adapter_dir.rglob("*") if p.is_file()]
    if not files:
        raise ValidationError("archive contains no files")

    config_seen = False
    weights_seen = False
    tensor_count = 0
    total_bytes = 0

    for f in files:
        # Reject anything escaping the adapter dir via symlinks or absolute paths.
        try:
            f.resolve().relative_to(adapter_dir.resolve())
        except ValueError:
            raise ValidationError(f"file {f} escapes adapter dir")

        if f.is_symlink():
            raise ValidationError(f"{f.name}: symlinks not allowed")

        rel = f.relative_to(adapter_dir).as_posix()
        # Reject hidden / dotfile shenanigans.
        if any(part.startswith(".") for part in f.relative_to(adapter_dir).parts):
            raise ValidationError(f"{rel}: dotfiles not allowed")

        # Allowlist by exact name or suffix.
        if f.name not in ALLOWED_FILES and not f.name.endswith(ALLOWED_SUFFIXES):
            raise ValidationError(
                f"{rel}: filename not in allowlist "
                f"(allowed: {sorted(ALLOWED_FILES)} or *.safetensors)"
            )

        total_bytes += f.stat().st_size

        if f.name.endswith(".safetensors"):
            header = _parse_safetensors_header(f)
            # Drop __metadata__ from tensor count if present.
            tensor_keys = [k for k in header.keys() if k != "__metadata__"]
            tensor_count += len(tensor_keys)
            weights_seen = True

        if f.name == "adapter_config.json":
            config_seen = True
            try:
                cfg = json.loads(f.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise ValidationError(f"adapter_config.json: invalid JSON ({e})")
            peft_type = cfg.get("peft_type")
            if peft_type != "LORA":
                raise ValidationError(
                    f"adapter_config.json: peft_type={peft_type!r}, only 'LORA' supported"
                )
            r = cfg.get("r")
            if not isinstance(r, int) or r <= 0:
                raise ValidationError(f"adapter_config.json: invalid rank r={r!r}")
            if r > MAX_LORA_RANK:
                raise ValidationError(
                    f"adapter_config.json: rank r={r} exceeds max-lora-rank={MAX_LORA_RANK} on vLLM"
                )

    if not config_seen:
        raise ValidationError("missing adapter_config.json")
    if not weights_seen:
        raise ValidationError("missing adapter weights (*.safetensors)")

    return {
        "file_count": len(files),
        "total_bytes": total_bytes,
        "tensor_count": tensor_count,
    }
