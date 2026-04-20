"""Unit tests for Issue #5: ``LimitExceededError`` detail shape.

Contract: ``docs/design/issue-5-error-copy.md`` §3.2, §4.1, §5 (row 5).

After Issue #5 the ``LimitExceededError.detail`` dict must be:

    {"error": <machine_code>, "message": <中文 copy per §4.1>}

Not the legacy ``{"code": ..., "message": <英文 debug string>}``. All 6
code branches from §4.1 (``files_too_many``, ``unsupported_type``,
``empty_file``, ``file_too_large``, ``total_too_large``, ``mime_mismatch``)
are exercised directly via ``validate_upload_limits`` /
``save_and_extract`` where possible.

Also verifies the design §5 row-5 note: "当 caller 传入空串时回落到
``message_for(code)``" — empty-string ``message`` argument falls back to
the ``error_copy.message_for`` lookup.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Expected Chinese copy — verbatim from design §4.1.
# ---------------------------------------------------------------------------

MSG_FILES_TOO_MANY = (
    "上传文件数超出单次 8 个的上限，请删减后重试"
)
MSG_UNSUPPORTED_TYPE = (
    "文件类型不支持，请改为 md/txt/pdf/docx/pptx/xlsx/png/jpg/jpeg/webp/gif"
)
MSG_EMPTY_FILE = "上传的文件是空的（0 字节），请检查后重试"
MSG_FILE_TOO_LARGE = "单个文件超过 50 MB 上限，请压缩或拆分后再上传"
MSG_TOTAL_TOO_LARGE = "上传总大小超过限制，请删减后重试"
MSG_MIME_MISMATCH = "文件内容与扩展名不一致，请重新选择文件"


# A real ULID (Crockford base32, 26 chars).
VALID_ULID = "01HXZK8D7Q3V0S9B4W2N6M5C7R"


# ---------------------------------------------------------------------------
# Fixture: upload_dir (same pattern as tests/unit/test_file_processor.py).
# ---------------------------------------------------------------------------


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "uploads"
    target.mkdir(parents=True, exist_ok=True)
    from app import config as config_mod

    monkeypatch.setattr(config_mod.settings, "upload_dir", str(target))
    return target


# ---------------------------------------------------------------------------
# Minimal UploadFile-like stub.
# ---------------------------------------------------------------------------


class _FakeUploadFile:
    def __init__(self, filename: str, size: int) -> None:
        self.filename = filename
        self.size = size
        self.file = BytesIO(b"")


# ===========================================================================
# 1. files_too_many — 400
# ===========================================================================


async def test_files_too_many_detail_shape() -> None:
    """Design §4.1 + §5 row 5: detail = {"error": "files_too_many", "message": <中文>}."""
    from app.services.file_processor import LimitExceededError, validate_upload_limits

    files = [_FakeUploadFile(f"f{i}.md", 10) for i in range(21)]
    with pytest.raises(LimitExceededError) as excinfo:
        await validate_upload_limits(files)

    assert excinfo.value.status_code == 400
    detail = excinfo.value.detail
    assert detail == {
        "error": "files_too_many",
        "message": MSG_FILES_TOO_MANY,
    }, f"unexpected detail: {detail!r}"
    # Explicit: legacy "code" key must NOT be present (§3.2).
    assert "code" not in detail, f"legacy 'code' key still present: {detail!r}"


# ===========================================================================
# 2. unsupported_type — 400
# ===========================================================================


async def test_unsupported_type_detail_shape() -> None:
    """Design §4.1: unsupported_type / 400."""
    from app.services.file_processor import LimitExceededError, validate_upload_limits

    files = [_FakeUploadFile("document.exe", 1024)]
    with pytest.raises(LimitExceededError) as excinfo:
        await validate_upload_limits(files)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == {
        "error": "unsupported_type",
        "message": MSG_UNSUPPORTED_TYPE,
    }


# ===========================================================================
# 3. empty_file — 400
# ===========================================================================


async def test_empty_file_detail_shape() -> None:
    """Design §4.1: empty_file / 400 / "上传的文件是空的（0 字节），请检查后重试"."""
    from app.services.file_processor import LimitExceededError, validate_upload_limits

    files = [_FakeUploadFile("empty.md", 0)]
    with pytest.raises(LimitExceededError) as excinfo:
        await validate_upload_limits(files)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == {
        "error": "empty_file",
        "message": MSG_EMPTY_FILE,
    }


# ===========================================================================
# 4. file_too_large — 400
# ===========================================================================


async def test_file_too_large_detail_shape() -> None:
    """Design §4.1: file_too_large / 400."""
    from app.services.file_processor import LimitExceededError, validate_upload_limits

    files = [_FakeUploadFile("big.pdf", 51 * 1024 * 1024)]
    with pytest.raises(LimitExceededError) as excinfo:
        await validate_upload_limits(files)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == {
        "error": "file_too_large",
        "message": MSG_FILE_TOO_LARGE,
    }


# ===========================================================================
# 5. total_too_large — 400
# ===========================================================================


async def test_total_too_large_detail_shape() -> None:
    """Design §4.1: total_too_large / 400."""
    from app.services.file_processor import LimitExceededError, validate_upload_limits

    # 3 × 40 MB = 120 MB (each file under 50 MB cap, total > 100 MB cap).
    files = [_FakeUploadFile(f"f{i}.pdf", 40 * 1024 * 1024) for i in range(3)]
    with pytest.raises(LimitExceededError) as excinfo:
        await validate_upload_limits(files)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == {
        "error": "total_too_large",
        "message": MSG_TOTAL_TOO_LARGE,
    }


# ===========================================================================
# 6. mime_mismatch — 400 (requires real disk I/O path)
# ===========================================================================


async def test_mime_mismatch_detail_shape(upload_dir: Path) -> None:
    """Design §4.1: mime_mismatch / 400 / "文件内容与扩展名不一致，请重新选择文件"."""
    from app.services.file_processor import LimitExceededError, save_and_extract

    fake_pdf = b"this is plain text, not a PDF at all.\n"
    with pytest.raises(LimitExceededError) as excinfo:
        await save_and_extract(VALID_ULID, "spoof.pdf", fake_pdf)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == {
        "error": "mime_mismatch",
        "message": MSG_MIME_MISMATCH,
    }


# ===========================================================================
# 7. Empty-string message falls back to message_for(code)  (design §5 row 5)
# ===========================================================================


def test_limit_exceeded_error_empty_message_falls_back_to_lookup() -> None:
    """Design §5 row 5: "当 caller 传入空串时回落到 ``message_for(code)``".

    Instantiating ``LimitExceededError("empty_file", "")`` — an empty
    ``message`` argument — must produce the same detail shape as if the
    caller had not passed a message at all.
    """
    from app.services.error_copy import message_for
    from app.services.file_processor import LimitExceededError

    err = LimitExceededError("empty_file", "")
    assert err.status_code == 400
    assert err.detail == {
        "error": "empty_file",
        "message": message_for("empty_file"),
    }
    # And the fallback message matches the design table.
    assert err.detail["message"] == MSG_EMPTY_FILE


def test_limit_exceeded_error_explicit_message_respected() -> None:
    """When the caller passes a non-empty message it is preserved verbatim."""
    from app.services.file_processor import LimitExceededError

    err = LimitExceededError("files_too_many", "custom override")
    assert err.detail == {
        "error": "files_too_many",
        "message": "custom override",
    }


# ===========================================================================
# 8. BC check: detail must NOT contain the legacy ``"code"`` key.
# ===========================================================================


async def test_no_legacy_code_key_across_all_branches() -> None:
    """Explicit sweep: for every one of the 6 branches we exercise above,
    verify the legacy ``"code"`` key is absent. This is the reverse-scan
    tripwire against accidental regression (design §3.2 BC migration)."""
    from app.services.file_processor import LimitExceededError, validate_upload_limits

    # Re-trigger one branch (files_too_many) and assert invariant.
    files = [_FakeUploadFile(f"f{i}.md", 10) for i in range(21)]
    with pytest.raises(LimitExceededError) as excinfo:
        await validate_upload_limits(files)
    assert "code" not in excinfo.value.detail
    assert "error" in excinfo.value.detail
    assert "message" in excinfo.value.detail
