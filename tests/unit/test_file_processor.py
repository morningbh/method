"""Unit tests for the file_processor service (Task 3.1).

Covers:
  - validate_upload_limits: the 5 limit rules (count/per-file/total/ext/empty)
  - save_and_extract: per-extension behavior (.md/.txt verbatim; .pdf/.docx
    extract to a sibling .extracted.md)
  - Failure modes: encrypted/empty pdf ⇒ extraction_ok=False, original
    preserved; mime-vs-ext mismatch ⇒ LimitExceededError(code="mime_mismatch")
  - HARNESS §2: all stored/extracted paths are absolute
  - Async contract: extraction runs in the executor so a slow extractor
    does NOT block the event loop
  - cleanup_request: removes the per-request dir; idempotent when absent
  - request_id trust-boundary: malformed ULID ⇒ ValueError (path traversal
    defense, design §2)

All tests monkeypatch ``app.config.settings.upload_dir`` to ``tmp_path`` so
they never touch the real ``data/uploads`` tree.
"""
from __future__ import annotations

import asyncio
import time
from io import BytesIO
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

SAMPLE_PDF = FIXTURES / "sample.pdf"
SAMPLE_DOCX = FIXTURES / "sample.docx"
SAMPLE_PPTX = FIXTURES / "sample.pptx"
SAMPLE_XLSX = FIXTURES / "sample.xlsx"
SAMPLE_PNG = FIXTURES / "sample.png"
SAMPLE_JPG = FIXTURES / "sample.jpg"
SAMPLE_MD = FIXTURES / "sample.md"
SAMPLE_TXT = FIXTURES / "sample.txt"
ENCRYPTED_PDF = FIXTURES / "encrypted.pdf"
EMPTY_PDF = FIXTURES / "empty.pdf"

# A real ULID (26-char Crockford base32). Keep as a constant so test output is
# deterministic and grep-friendly.
VALID_ULID = "01HXZK8D7Q3V0S9B4W2N6M5C7R"


# ---------------------------------------------------------------------------
# Fixture: point settings.upload_dir at tmp_path and clear cached settings
# ---------------------------------------------------------------------------


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override settings.upload_dir with a tmp path for this test."""
    target = tmp_path / "uploads"
    target.mkdir(parents=True, exist_ok=True)
    from app import config as config_mod

    monkeypatch.setattr(config_mod.settings, "upload_dir", str(target))
    return target


# ---------------------------------------------------------------------------
# Minimal UploadFile-like stub for validate_upload_limits
# ---------------------------------------------------------------------------


class _FakeUploadFile:
    """Quack-alike for FastAPI's UploadFile: has .filename and .size.

    validate_upload_limits is documented to use declared filename + size only
    (design §2), so this stub is sufficient.
    """

    def __init__(self, filename: str, size: int) -> None:
        self.filename = filename
        self.size = size
        # Some code paths may expect a .file attr for compatibility.
        self.file = BytesIO(b"")


# ---------------------------------------------------------------------------
# #1. .md stored verbatim, no extraction
# ---------------------------------------------------------------------------


async def test_save_md_stores_content_no_extraction(upload_dir: Path) -> None:
    from app.services.file_processor import save_and_extract

    content = SAMPLE_MD.read_bytes()
    result = await save_and_extract(VALID_ULID, "sample.md", content)

    assert result.extracted_path is None
    assert result.extraction_ok is True
    assert result.size_bytes == len(content)
    assert result.stored_path.exists()
    assert result.stored_path.read_bytes() == content


# ---------------------------------------------------------------------------
# #2. .txt stored verbatim, no extraction
# ---------------------------------------------------------------------------


async def test_save_txt_stores_content_no_extraction(upload_dir: Path) -> None:
    from app.services.file_processor import save_and_extract

    content = SAMPLE_TXT.read_bytes()
    result = await save_and_extract(VALID_ULID, "sample.txt", content)

    assert result.extracted_path is None
    assert result.extraction_ok is True
    assert result.size_bytes == len(content)
    assert result.stored_path.exists()
    assert result.stored_path.read_bytes() == content


# ---------------------------------------------------------------------------
# #3. .pdf stored + extracted to sibling .extracted.md
# ---------------------------------------------------------------------------


async def test_save_pdf_stores_and_extracts_text(upload_dir: Path) -> None:
    from app.services.file_processor import save_and_extract

    content = SAMPLE_PDF.read_bytes()
    result = await save_and_extract(VALID_ULID, "sample.pdf", content)

    assert result.extraction_ok is True
    assert result.stored_path.exists()
    assert result.stored_path.read_bytes() == content
    assert result.extracted_path is not None
    assert result.extracted_path.exists()
    assert result.extracted_path.suffix == ".md"
    extracted_text = result.extracted_path.read_text(encoding="utf-8")
    # Sentinel text from the fixture generator.
    assert "Hello from Method test PDF" in extracted_text


# ---------------------------------------------------------------------------
# #4. .docx stored + extracted to sibling .extracted.md
# ---------------------------------------------------------------------------


async def test_save_docx_stores_and_extracts_text(upload_dir: Path) -> None:
    from app.services.file_processor import save_and_extract

    content = SAMPLE_DOCX.read_bytes()
    result = await save_and_extract(VALID_ULID, "sample.docx", content)

    assert result.extraction_ok is True
    assert result.stored_path.exists()
    assert result.stored_path.read_bytes() == content
    assert result.extracted_path is not None
    assert result.extracted_path.exists()
    assert result.extracted_path.suffix == ".md"
    extracted_text = result.extracted_path.read_text(encoding="utf-8")
    assert "Hello from Method test DOCX" in extracted_text


# ---------------------------------------------------------------------------
# #5. encrypted pdf: extraction fails, original preserved
# ---------------------------------------------------------------------------


async def test_save_encrypted_pdf_marks_extraction_failed_but_preserves_file(
    upload_dir: Path,
) -> None:
    from app.services.file_processor import save_and_extract

    content = ENCRYPTED_PDF.read_bytes()
    result = await save_and_extract(VALID_ULID, "encrypted.pdf", content)

    assert result.extraction_ok is False
    assert result.extracted_path is None
    # Original MUST be preserved on disk even when extraction fails (design §4).
    assert result.stored_path.exists()
    assert result.stored_path.read_bytes() == content


# ---------------------------------------------------------------------------
# #6. empty (scanned-like) pdf: extraction_ok=False
# ---------------------------------------------------------------------------


async def test_save_empty_pdf_marks_extraction_ok_false(upload_dir: Path) -> None:
    from app.services.file_processor import save_and_extract

    content = EMPTY_PDF.read_bytes()
    result = await save_and_extract(VALID_ULID, "empty.pdf", content)

    assert result.extraction_ok is False
    assert result.extracted_path is None
    assert result.stored_path.exists()


# ---------------------------------------------------------------------------
# #7. All paths stored in SavedFile are absolute (HARNESS §2)
# ---------------------------------------------------------------------------


async def test_all_paths_are_absolute(upload_dir: Path) -> None:
    from app.services.file_processor import save_and_extract

    content = SAMPLE_PDF.read_bytes()
    result = await save_and_extract(VALID_ULID, "sample.pdf", content)

    assert result.stored_path.is_absolute(), f"stored_path not absolute: {result.stored_path}"
    assert result.extracted_path is not None
    assert result.extracted_path.is_absolute(), (
        f"extracted_path not absolute: {result.extracted_path}"
    )


# ---------------------------------------------------------------------------
# #8. Extraction runs in an executor, does NOT block the event loop.
# ---------------------------------------------------------------------------


async def test_extraction_timeout_does_not_block_event_loop(
    upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatch the PDF extractor to sleep synchronously, then prove that
    a concurrent asyncio.sleep task still makes progress.

    If the extractor were called on the event-loop thread, the `ticker` task
    below would not increment until the fake extractor returned.
    """
    import app.services.file_processor as fp

    def _slow_extract(_path: Path) -> str:
        time.sleep(0.5)
        return "slow"

    # Replace the private extractor. This test documents the contract that
    # file_processor offloads _extract_pdf to a thread pool.
    monkeypatch.setattr(fp, "_extract_pdf", _slow_extract, raising=True)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        # Run long enough to span the 0.5s extract; the test stops it via cancel.
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())
    # Give ticker one chance to start before we begin the extraction.
    await asyncio.sleep(0.01)
    content = SAMPLE_PDF.read_bytes()
    start = time.monotonic()
    await fp.save_and_extract(VALID_ULID, "sample.pdf", content)
    ticks_during_save = ticks
    elapsed = time.monotonic() - start
    ticker_task.cancel()
    try:
        await ticker_task
    except asyncio.CancelledError:
        pass

    # Two invariants must both hold:
    # 1. The ticker accumulated enough ticks *during* the save to prove the
    #    event loop kept running concurrently with the blocking extractor.
    # 2. Total wall time is well under 2x the blocking cost, proving the
    #    extractor ran in parallel with the ticker rather than serially.
    assert ticks_during_save >= 5, (
        f"event loop blocked during extraction: ticks_during_save={ticks_during_save}"
    )
    assert elapsed < 0.75, f"extraction wall-time {elapsed:.2f}s suggests blocking"


# ---------------------------------------------------------------------------
# #9. MIME sniff mismatch rejected
# ---------------------------------------------------------------------------


async def test_mime_mismatch_rejected(upload_dir: Path) -> None:
    """A plain-text payload with a .pdf extension must be rejected with
    LimitExceededError(code="mime_mismatch"). This is the spoof-defense path
    in design §3.
    """
    from app.services.file_processor import LimitExceededError, save_and_extract

    fake_pdf = b"This is just plain text, not a PDF at all.\n"
    with pytest.raises(LimitExceededError) as excinfo:
        await save_and_extract(VALID_ULID, "sniff.pdf", fake_pdf)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["error"] == "mime_mismatch"


# ---------------------------------------------------------------------------
# #10-#14: validate_upload_limits branches
# ---------------------------------------------------------------------------


async def test_validate_limits_too_many_files_raises() -> None:
    from app.services.file_processor import LimitExceededError, validate_upload_limits

    files = [_FakeUploadFile(f"file{i}.md", 10) for i in range(21)]
    with pytest.raises(LimitExceededError) as excinfo:
        await validate_upload_limits(files)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["error"] == "files_too_many"


async def test_validate_limits_file_too_large_raises() -> None:
    from app.services.file_processor import LimitExceededError, validate_upload_limits

    # 51 MB, exceeds the 50 MB per-file cap (bumped 2026-04).
    files = [_FakeUploadFile("big.pdf", 51 * 1024 * 1024)]
    with pytest.raises(LimitExceededError) as excinfo:
        await validate_upload_limits(files)
    assert excinfo.value.detail["error"] == "file_too_large"


async def test_validate_limits_at_per_file_cap_accepted() -> None:
    """A file exactly at 50 MB must be accepted."""
    from app.services.file_processor import validate_upload_limits

    files = [_FakeUploadFile("exact.pdf", 50 * 1024 * 1024)]
    await validate_upload_limits(files)  # no raise = pass


async def test_validate_limits_total_too_large_raises() -> None:
    from app.services.file_processor import LimitExceededError, validate_upload_limits

    # 3 × 40 MB = 120 MB, exceeds the 100 MB total cap (design §3). Each
    # individual file is under the 50 MB per-file cap.
    files = [_FakeUploadFile(f"f{i}.pdf", 40 * 1024 * 1024) for i in range(3)]
    with pytest.raises(LimitExceededError) as excinfo:
        await validate_upload_limits(files)
    assert excinfo.value.detail["error"] == "total_too_large"


async def test_validate_limits_unsupported_extension_raises() -> None:
    from app.services.file_processor import LimitExceededError, validate_upload_limits

    files = [_FakeUploadFile("malware.exe", 1024)]
    with pytest.raises(LimitExceededError) as excinfo:
        await validate_upload_limits(files)
    assert excinfo.value.detail["error"] == "unsupported_type"


async def test_validate_limits_empty_file_raises() -> None:
    from app.services.file_processor import LimitExceededError, validate_upload_limits

    files = [_FakeUploadFile("blank.md", 0)]
    with pytest.raises(LimitExceededError) as excinfo:
        await validate_upload_limits(files)
    assert excinfo.value.detail["error"] == "empty_file"


# ---------------------------------------------------------------------------
# #15. cleanup_request removes the per-request directory
# ---------------------------------------------------------------------------


async def test_cleanup_request_removes_dir(upload_dir: Path) -> None:
    from app.services.file_processor import cleanup_request, save_and_extract

    await save_and_extract(VALID_ULID, "sample.md", SAMPLE_MD.read_bytes())
    req_dir = upload_dir / VALID_ULID
    assert req_dir.exists(), "precondition: request dir should exist after save"

    await cleanup_request(VALID_ULID)
    assert not req_dir.exists()


# ---------------------------------------------------------------------------
# #16. cleanup_request is idempotent when the dir is missing
# ---------------------------------------------------------------------------


async def test_cleanup_request_idempotent_for_missing_dir(upload_dir: Path) -> None:
    from app.services.file_processor import cleanup_request

    # No prior save_and_extract — the dir does not exist. Must NOT raise.
    await cleanup_request(VALID_ULID)
    # And must still be absent afterward.
    assert not (upload_dir / VALID_ULID).exists()


# ---------------------------------------------------------------------------
# #20 (flagged in design §10 as living with the file_processor tests, not
# the models tests).  Malformed request_id ⇒ ValueError (path traversal
# defense, design §2).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PPTX: stored + extracted to sibling .extracted.md with per-slide sections
# ---------------------------------------------------------------------------


async def test_save_pptx_stores_and_extracts_text(upload_dir: Path) -> None:
    from app.services.file_processor import save_and_extract

    content = SAMPLE_PPTX.read_bytes()
    result = await save_and_extract(VALID_ULID, "sample.pptx", content)

    assert result.extraction_ok is True
    assert result.stored_path.exists()
    assert result.stored_path.read_bytes() == content
    assert result.extracted_path is not None
    assert result.extracted_path.exists()
    assert result.extracted_path.suffix == ".md"
    extracted = result.extracted_path.read_text(encoding="utf-8")
    # Sentinel text from fixture generator + per-slide section header.
    assert "Hello from Method test PPTX" in extracted
    assert "## Slide 1" in extracted


# ---------------------------------------------------------------------------
# XLSX: stored + extracted as per-sheet tab-separated text
# ---------------------------------------------------------------------------


async def test_save_xlsx_stores_and_extracts_text(upload_dir: Path) -> None:
    from app.services.file_processor import save_and_extract

    content = SAMPLE_XLSX.read_bytes()
    result = await save_and_extract(VALID_ULID, "sample.xlsx", content)

    assert result.extraction_ok is True
    assert result.stored_path.exists()
    assert result.extracted_path is not None
    extracted = result.extracted_path.read_text(encoding="utf-8")
    assert "## Sheet: Sheet1" in extracted
    assert "Hello from Method test XLSX" in extracted
    # Tab-separated cells on row 1: "Hello..." \t "Slide two cell"
    assert "\t" in extracted


async def test_xlsx_extraction_truncates_large_sheets(
    upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """xlsx extractor must cap rows per sheet to protect the prompt budget."""
    from openpyxl import Workbook

    import app.services.file_processor as fp

    # Build a workbook with 600 rows; our cap is 500.
    wb = Workbook()
    ws = wb.active
    ws.title = "Big"
    for i in range(600):
        ws.cell(row=i + 1, column=1, value=f"row-{i}")
    xlsx_path = upload_dir / "big.xlsx"
    wb.save(str(xlsx_path))

    text = fp._extract_xlsx(xlsx_path)
    assert "row-0" in text
    assert "row-499" in text
    assert "row-500" not in text
    assert "已截断" in text


# ---------------------------------------------------------------------------
# PNG / JPG: image files are stored as-is with no extraction
# ---------------------------------------------------------------------------


async def test_save_png_stores_without_extraction(upload_dir: Path) -> None:
    from app.services.file_processor import save_and_extract

    content = SAMPLE_PNG.read_bytes()
    result = await save_and_extract(VALID_ULID, "sample.png", content)

    assert result.extraction_ok is True
    assert result.extracted_path is None
    assert result.stored_path.exists()
    assert result.stored_path.read_bytes() == content
    assert result.mime_type == "image/png"


async def test_save_jpg_stores_without_extraction(upload_dir: Path) -> None:
    from app.services.file_processor import save_and_extract

    content = SAMPLE_JPG.read_bytes()
    result = await save_and_extract(VALID_ULID, "sample.jpg", content)

    assert result.extraction_ok is True
    assert result.extracted_path is None
    assert result.stored_path.exists()
    assert result.mime_type == "image/jpeg"


async def test_png_with_wrong_extension_mime_mismatch(upload_dir: Path) -> None:
    """A PNG payload masquerading as .jpg must be rejected."""
    from app.services.file_processor import LimitExceededError, save_and_extract

    png_bytes = SAMPLE_PNG.read_bytes()
    with pytest.raises(LimitExceededError) as excinfo:
        await save_and_extract(VALID_ULID, "fake.jpg", png_bytes)
    assert excinfo.value.detail["error"] == "mime_mismatch"


async def test_image_extensions_in_allowed_set() -> None:
    """Guard against silent drift: the allowed-extension set must include
    every image format we document as supported."""
    import app.services.file_processor as fp

    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        assert ext in fp._ALLOWED_EXTS, f"missing {ext} from _ALLOWED_EXTS"


# ---------------------------------------------------------------------------
# Trust-boundary: invalid ULID still blocks filesystem operations.
# ---------------------------------------------------------------------------


async def test_ulid_regex_rejected_by_file_processor(upload_dir: Path) -> None:
    """A request_id that isn't a valid 26-char Crockford ULID must be
    rejected before any filesystem operation — this is a trust-boundary
    check per design §2.
    """
    from app.services.file_processor import save_and_extract

    with pytest.raises(ValueError):
        await save_and_extract("../etc", "sample.md", SAMPLE_MD.read_bytes())
