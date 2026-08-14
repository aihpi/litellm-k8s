"""Pins the two things /upload relies on for archive safety.

Extraction is stdlib now (tarfile filter="data", PEP 706) rather than a
hand-rolled member scan, so this guards the composition rather than our code:
the filter must keep hostile members out of the destination, and the filename
allowlist in validation.py must reject whatever the filter merely *sanitises*
instead of refusing. Case 5 is the one that matters — it's the behavioural
difference from the old _safe_extract, which raised on absolute paths.

Deliberately outside app/ so the Dockerfile's `COPY app/ ./app/` leaves it out
of the image. No pytest: the repo has no Python test setup and CI runs kubeval
plus kustomize only.

    python base/lora-manager/test_extract.py
"""

import io
import tarfile
import tempfile
from pathlib import Path

from app.validation import ValidationError, validate_adapter_dir

LORA_CONFIG = b'{"peft_type": "LORA", "r": 8}'


def _tar(build) -> tarfile.TarFile:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        build(t)
    buf.seek(0)
    return tarfile.open(fileobj=buf)


def _add(tar, name, data=b"", **attrs):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    for k, v in attrs.items():
        setattr(info, k, v)
    tar.addfile(info, io.BytesIO(data) if data else None)


def _extract(build, dest):
    """Exactly what main.py does."""
    with _tar(build) as tar:
        tar.extractall(dest, filter="data")


def _refused(build) -> str:
    with tempfile.TemporaryDirectory() as td:
        try:
            _extract(build, td)
        except Exception as e:
            return type(e).__name__
        raise AssertionError(f"archive was NOT refused; extracted {list(Path(td).iterdir())}")


def test_traversal_refused():
    assert _refused(lambda t: _add(t, "../escaped.txt")) == "OutsideDestinationError"


def test_absolute_symlink_refused():
    assert (
        _refused(lambda t: _add(t, "link", type=tarfile.SYMTYPE, linkname="/etc/passwd"))
        == "AbsoluteLinkError"
    )


def test_device_node_refused():
    assert (
        _refused(lambda t: _add(t, "bad", type=tarfile.CHRTYPE, devmajor=1, devminor=3))
        == "SpecialFileError"
    )


def test_valid_adapter_accepted():
    with tempfile.TemporaryDirectory() as td:
        _extract(
            lambda t: (
                _add(t, "adapter_config.json", LORA_CONFIG),
                # 8-byte little-endian header length, then that many JSON bytes.
                _add(
                    t,
                    "adapter_model.safetensors",
                    len(b'{"w":{}}').to_bytes(8, "little") + b'{"w":{}}',
                ),
            ),
            td,
        )
        summary = validate_adapter_dir(Path(td))
        assert summary["file_count"] == 2, summary
        assert summary["tensor_count"] == 1, summary


def test_contained_symlink_rejected_by_validation():
    """filter="data" refuses only *escaping* symlinks, so a contained one survives
    extraction. A symlinked directory isn't a file, so it never reaches the
    per-file allowlist — validation must reject symlinks explicitly."""
    with tempfile.TemporaryDirectory() as td:
        _extract(
            lambda t: (
                _add(t, "adapter_config.json", LORA_CONFIG),
                _add(
                    t,
                    "adapter_model.safetensors",
                    len(b'{"w":{}}').to_bytes(8, "little") + b'{"w":{}}',
                ),
                # Points at the adapter dir itself: contained, so the filter allows it.
                _add(t, "sneaky", type=tarfile.SYMTYPE, linkname="."),
            ),
            td,
        )
        assert (Path(td) / "sneaky").is_symlink(), "filter unexpectedly refused it"
        try:
            validate_adapter_dir(Path(td))
        except ValidationError as e:
            assert "symlinks not allowed" in str(e), e
        else:
            raise AssertionError("a symlinked directory reached the PVC")


def test_absolute_path_is_contained_then_rejected():
    """filter="data" strips the leading slash rather than refusing, so the member
    lands inside dest. The allowlist is what stops it going any further."""
    with tempfile.TemporaryDirectory() as td:
        _extract(lambda t: _add(t, "/etc/pwned", b"x"), td)

        escaped = Path("/etc/pwned")
        assert not escaped.exists() or escaped.stat().st_size != 1, "wrote outside dest"
        assert (Path(td) / "etc" / "pwned").is_file(), "expected containment, not refusal"

        try:
            validate_adapter_dir(Path(td))
        except ValidationError as e:
            assert "allowlist" in str(e), e
        else:
            raise AssertionError("allowlist did not reject the sanitised member")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nall passed")
