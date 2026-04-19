# Task 3.1 — File Processor (Issue #2 / M3)

Date: 2026-04-19
Status: Draft (pre-review)
Scope: Milestone 3 Task 3.1 — isolated service module for upload persistence + pdf/docx text extraction.

---

## 1. Purpose

Accept user-uploaded background documents (pdf/docx/md/txt), enforce request-level limits, persist originals under a per-request dir, and pre-extract text from pdf/docx into sibling `.extracted.md` files so `claude_runner` only needs `Read` on plain markdown. Also adds the two DB tables (`research_requests`, `uploaded_files`) that downstream M3 tasks reference. Non-goals: upload UX, SSE, authz — handled in Task 3.3.

---

## 2. Public API

```python
# app/services/file_processor.py

from dataclasses import dataclass
from pathlib import Path
from fastapi import UploadFile


@dataclass(frozen=True)
class SavedFile:
    stored_path: Path          # absolute path on disk (original bytes)
    extracted_path: Path | None  # absolute path to .extracted.md, or None
    size_bytes: int
    mime_type: str             # sniffed via python-magic
    extraction_ok: bool        # True = nothing to extract OR extraction succeeded
                               # False = pdf/docx recognized but extraction failed / empty


async def validate_upload_limits(files: list[UploadFile]) -> None:
    """Raise LimitExceededError (HTTPException 400) if any of:
    - len(files) > 20               → code 'files_too_many'
    - any file size > 30 MB         → code 'file_too_large'
    - total size > 100 MB           → code 'total_too_large'
    - ext not in {.md,.txt,.pdf,.docx} → 'unsupported_type'
    - any file size == 0            → 'empty_file'
    MIME sniff happens in save_and_extract (needs bytes); this fn uses declared filename+size.
    """


async def save_and_extract(
    request_id: str,
    original_name: str,
    content: bytes,
) -> SavedFile:
    """1. Sniff MIME with python-magic on content[:2048]; reject if mismatch with ext → 'mime_mismatch'.
    2. mkdir -p {settings.upload_dir}/{request_id}
    3. Write content to {upload_dir}/{request_id}/{uuid4().hex}.{ext}
    4. For pdf/docx, run extractor in executor with 10s timeout; write result to {uuid4}.extracted.md.
    5. Return SavedFile with absolute paths.
    Does NOT insert DB row — caller (Task 3.3 router) handles persistence.

    request_id parameter contract: MUST be a 26-character ULID string (base32
    Crockford alphabet, matching ^[0-9A-HJKMNP-TV-Z]{26}$) validated by the
    caller (router, Task 3.3). file_processor defensively validates this regex
    and raises ValueError("invalid request_id") on mismatch — this is a
    trust-boundary check because request_id is used as a directory name
    component; a malformed value would allow path traversal. Callers are
    responsible for generating valid ULIDs via the standard library or a
    python-ulid equivalent.
    """


async def cleanup_request(request_id: str) -> None:
    """Recursively delete {upload_dir}/{request_id}/. Idempotent: silently returns if missing."""
```

`LimitExceededError(HTTPException)` subclasses HTTPException with `status_code=400` and `detail={"code": <code>, "message": <str>}`.

---

## 3. Allowed file types, limits, MIME

Limits: ≤ 20 files/request, ≤ 30 MB/file, ≤ 100 MB total, 0-byte rejected.

MIME sniffing: `magic.Magic(mime=True).from_buffer(content[:2048])`. Ext → handler + accepted sniffed MIMEs:

| Ext | Handler | Accepted sniffed MIMEs |
|---|---|---|
| `.md` | store only | `text/plain`, `text/markdown`, `text/x-markdown` |
| `.txt` | store only | `text/plain` |
| `.pdf` | pdfplumber | `application/pdf` |
| `.docx` | python-docx | `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `application/zip` (docx is a zip; libmagic sometimes reports outer container — accept both) |

Mismatch → `mime_mismatch`.

---

## 4. Extraction behavior

**PDF (pdfplumber)**
```python
def _extract_pdf(path: Path) -> str:
    with pdfplumber.open(path) as doc:
        return "\n\n".join((p.extract_text() or "") for p in doc.pages).strip()
```
Empty string → `extraction_ok=False`, `extracted_path=None` (scanned pdf).

**DOCX (python-docx)**
```python
def _extract_docx(path: Path) -> str:
    return "\n".join(p.text for p in Document(path).paragraphs).strip()
```

**Async wrapper + timeout**
```python
loop = asyncio.get_running_loop()
try:
    text = await asyncio.wait_for(
        loop.run_in_executor(None, _extract_pdf, stored_path),
        timeout=10.0,
    )
except (asyncio.TimeoutError, Exception) as e:
    log.warning("extraction_failed", path=stored_path, err=str(e))
    return None
```

Failure catches: `asyncio.TimeoutError`, `pdfplumber.PDFSyntaxError`, `docx.opc.exceptions.PackageNotFoundError`, `OSError`, and a final `Exception` (best-effort, logged). The final catch is intentionally broad because extraction is best-effort and the original file is always preserved; new failure modes (e.g. library version bumps) should not crash the request.

All failures → log WARN, return `extraction_ok=False`, `extracted_path=None`, **preserve original file**. Never raise up to caller.

Success: write extracted text to `{stem}.extracted.md` (sibling of original), `extraction_ok=True`.

---

## 5. Storage layout

All paths absolute (HARNESS §2).

```
{settings.upload_dir}/
  {request_id}/                           # request_id is a ULID (TEXT PK on research_requests)
    7f3a…b1.pdf                           # original bytes
    7f3a…b1.extracted.md                  # extraction result (pdf/docx success only)
    9c01…ef.md                            # md/txt stored verbatim, no extraction sibling
    …
```

- Filename on disk = `{uuid4.hex}.{ext}` — never `original_name`, to avoid path traversal and collisions. `original_name` preserved in DB column for display.
- `{settings.upload_dir}` is an absolute path (already enforced by M1 scaffolding — tests use `/home/ubuntu/method/data/uploads`, prod uses `/var/method/uploads`).
- Absolute path guarantee: `Path(settings.upload_dir).resolve() / request_id / …`.

---

## 6. Data model changes (`app/models.py`)

Two new ORM classes registered on `Base.metadata`, so `init_db()` picks them up.

**Datetime convention (consistent with Task 2.x)**: All `DateTime` columns use SQLAlchemy naive `DateTime` (no `timezone=True`). Values are written as `datetime.now(timezone.utc).replace(tzinfo=None)` via a module-level `_utcnow() -> datetime` helper, matching the convention in `app/models.py` established by Task 2.3. No column uses `server_default`; all `created_at` values are set at application write time. `completed_at` is `DateTime NULL` with no default (set explicitly when research transitions to `done`/`failed`).

### 6.1 `ResearchRequest`
Table `research_requests` (spec §2.1):

| Column | Type | Constraints |
|---|---|---|
| id | TEXT | PRIMARY KEY (ULID) |
| user_id | INTEGER | NOT NULL, FK users(id) |
| question | TEXT | NOT NULL |
| status | TEXT | NOT NULL, CHECK IN ('pending','running','done','failed') |
| plan_path | TEXT | NULL (absolute; set on done) |
| error_message | TEXT | NULL (must be non-null when status='failed' — enforced in router, HARNESS §1) |
| model | TEXT | NOT NULL (e.g. 'claude-opus-4-7') |
| created_at | DATETIME | NOT NULL |
| completed_at | DATETIME | NULL |

Index: `idx_requests_user_created ON (user_id, created_at DESC)`.

Note on HARNESS §1 ("status=failed must have error_message"): this is enforced at **router level** (Task 3.3), not via DB CHECK, because SQLite's CHECK can reference columns but mixing multi-column conditions with ORM defaults is awkward. Task 3.1 tests only verify the status CHECK constraint; the `failed ⇒ error_message` invariant is tested in Task 3.3 (flag in §10 #20).

### 6.2 `UploadedFile`
Table `uploaded_files` (spec §2.1):

| Column | Type | Constraints |
|---|---|---|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT |
| request_id | TEXT | NOT NULL, FK research_requests(id) |
| original_name | TEXT | NOT NULL (user-supplied; display only) |
| stored_path | TEXT | NOT NULL (absolute) |
| extracted_path | TEXT | NULL (absolute) |
| size_bytes | INTEGER | NOT NULL |
| mime_type | TEXT | NOT NULL |
| created_at | DATETIME | NOT NULL |

FK enforcement: SQLite requires `PRAGMA foreign_keys=ON` per connection. M1's `app/db.py` already sets this via the `connect` event listener; tests must confirm it's active (test #18).

Both classes added to `app/models.py` `__all__`.

---

## 7. Error handling

```python
class LimitExceededError(HTTPException):
    def __init__(self, code: str, message: str):
        super().__init__(status_code=400, detail={"code": code, "message": message})
```

Single class, six `code` values: `files_too_many`, `file_too_large`, `total_too_large`, `unsupported_type`, `empty_file`, `mime_mismatch`.

Extraction failures: logged via `logging.getLogger(__name__).warning(...)`, never raised. Caller sees `extraction_ok=False`.

OS errors from disk write (ENOSPC, EACCES): propagate as-is (becomes 500); router is responsible for disk-space pre-check per spec §8.

---

## 8. Field mapping table

| Field | Input source | DB storage | SavedFile return | Notes |
|---|---|---|---|---|
| original_name | `UploadFile.filename` | `uploaded_files.original_name` | — (not returned; router persists) | sanitized only for display; never used on disk |
| content bytes | `await UploadFile.read()` | disk at `stored_path` | — | full in-memory read (spec §13 defers streaming) |
| stored_path | derived `{upload_dir}/{req}/{uuid}.{ext}` | `uploaded_files.stored_path` | `SavedFile.stored_path` | absolute (HARNESS §2) |
| size_bytes | `len(content)` | `uploaded_files.size_bytes` | `SavedFile.size_bytes` | also used in limits check |
| mime_type | `magic.from_buffer(content[:2048])` | `uploaded_files.mime_type` | `SavedFile.mime_type` | sniffed, not declared |
| extracted text | `pdfplumber` / `python-docx` | file at `extracted_path` | — | |
| extracted_path | derived sibling `.extracted.md` | `uploaded_files.extracted_path` | `SavedFile.extracted_path` | None if md/txt OR extraction failed |
| extraction_ok | derived: True unless pdf/docx and extraction failed/empty | (implied: `extracted_path IS NOT NULL` for pdf/docx) | `SavedFile.extraction_ok` | for md/txt always True |

`extraction_ok` semantics: `True` when no extraction is needed (md/txt) OR when extraction produced non-empty text (pdf/docx). `False` only when the file is a supported extraction type (pdf/docx) AND extraction failed or produced empty text.

---

## 9. Files created/modified

| Path | Action | Purpose |
|---|---|---|
| `app/services/file_processor.py` | **create** | implementation (public API §2) |
| `app/services/__init__.py` | modify | export `file_processor` module name if needed (no re-exports required) |
| `app/models.py` | **modify** | add `ResearchRequest`, `UploadedFile` ORM classes + index |
| `tests/fixtures/__init__.py` | create | package marker |
| `tests/fixtures/sample.pdf` | create | tiny 1-page real pdf (generated via reportlab in a one-off script, committed as binary) |
| `tests/fixtures/sample.docx` | create | tiny 1-paragraph real docx |
| `tests/fixtures/sample.md` | create | `"# Hello\n\nMarkdown fixture.\n"` |
| `tests/fixtures/sample.txt` | create | `"Plain text fixture.\n"` |
| `tests/fixtures/encrypted.pdf` | create | password-protected pdf (pdfplumber raises) |
| `tests/fixtures/empty.pdf` | create | pdf with no extractable text (scanned-like / blank) |
| `tests/unit/test_file_processor.py` | **create** | tests #1–#16, #20-flag |
| `tests/unit/test_models.py` | modify | add tests #17–#19 for the two new tables |
| `pyproject.toml` | modify | add `pdfplumber`, `python-docx`, `python-magic` runtime deps |

No router or migration file changes (tables materialize via `init_db()` at startup; M1 already wires this).

---

## 10. Test plan (hint for /tester)

1. `test_save_md_stores_content_no_extraction` — .md ⇒ `extracted_path is None`, `extraction_ok=True`.
2. `test_save_txt_stores_content_no_extraction` — same for .txt.
3. `test_save_pdf_stores_and_extracts_text` — sample.pdf ⇒ `.extracted.md` exists, content non-empty.
4. `test_save_docx_stores_and_extracts_text` — sample.docx ⇒ `.extracted.md` exists, content non-empty.
5. `test_save_encrypted_pdf_marks_extraction_failed_but_preserves_file` — encrypted.pdf ⇒ `extracted_path is None`, `extraction_ok=False`, original file still exists on disk.
6. `test_save_empty_pdf_marks_extraction_ok_false` — empty.pdf ⇒ same (empty text treated as failure).
7. `test_all_paths_are_absolute` — `SavedFile.stored_path.is_absolute()` and (if set) `extracted_path.is_absolute()` — HARNESS §2.
8. `test_extraction_timeout_does_not_block_event_loop` — monkeypatch extractor to `time.sleep(11)`; await completes in ~10s with `extraction_ok=False`; concurrent `asyncio.sleep(0)` tick proves loop not blocked.
9. `test_mime_mismatch_rejected` — rename `sample.txt` → `sniff.pdf`; `save_and_extract` raises LimitExceededError code=`mime_mismatch`.
10. `test_validate_limits_too_many_files_raises` — 21 UploadFile objs ⇒ `files_too_many`.
11. `test_validate_limits_file_too_large_raises` — one 31 MB file ⇒ `file_too_large`.
12. `test_validate_limits_total_too_large_raises` — 10 × 11 MB ⇒ `total_too_large`.
13. `test_validate_limits_unsupported_extension_raises` — `.exe` ⇒ `unsupported_type`.
14. `test_validate_limits_empty_file_raises` — 0-byte ⇒ `empty_file`.
15. `test_cleanup_request_removes_dir` — after save, `cleanup_request` removes the full `{req}/` tree.
16. `test_cleanup_request_idempotent_for_missing_dir` — call on non-existent request_id returns without error.
17. `test_research_requests_table_created_with_constraints` — insert row; query back; verify CHECK on status.
18. `test_uploaded_files_table_fk_enforces` — insert `uploaded_files` row with non-existent `request_id` ⇒ IntegrityError (requires `PRAGMA foreign_keys=ON`, which M1 sets).
19. `test_research_requests_status_check_constraint` — status='bogus' ⇒ IntegrityError.
20. `test_research_requests_status_failed_requires_error_message` — **FLAG**: this invariant is app-logic (router), not DB. Not implemented in Task 3.1. Will be covered in Task 3.3 router tests.

Tests use `tmp_path` fixture for `upload_dir`; a conftest override of `settings.upload_dir` pointed at `tmp_path` is required (add a `settings_override` fixture in `tests/conftest.py` if absent — confirmed absent in current conftest; tester will add).

---

## 11. Infrastructure dependency table

| Dep | Failure mode | Degradation |
|---|---|---|
| `pdfplumber` | parse error / encrypted pdf | log WARN, `extraction_ok=False`, `extracted_path=None`; original file kept |
| `python-docx` | parse error / corrupt zip | same as above |
| `python-magic` (Python binding) | ImportError if libmagic missing | startup import fails → FastAPI won't boot (fail-fast, acceptable) |
| `libmagic1` (system lib) | not installed on host | `python-magic` raises `magic.MagicException` on first use → upload rejected with `mime_mismatch`. **ACTION: verify `apt list --installed libmagic1` on the Tencent server before deploy; install via `apt-get install libmagic1` if missing.** ✓ verified installed on 2026-04-19 (libmagic1t64:amd64 + libmagic-mgc). |
| `asyncio` executor (default ThreadPoolExecutor) | default max_workers = min(32, cpu+4) — fine for MVP | N/A |
| `{upload_dir}` disk full | `OSError: [Errno 28] No space left` | propagates as 500; router precheck via `shutil.disk_usage` (spec §8) in Task 3.3 |
| `{upload_dir}` permission denied | `PermissionError` | 500; deploy checklist ensures ownership by `method` user |
| `uuid4` collision | negligible (2^122) | N/A |

---

## 12. Security notes

- **Filename traversal**: on-disk filename is always `{uuid4.hex}.{ext}`; `original_name` is never joined into a filesystem path. Ext derived via `Path(name).suffix.lower()` against a hardcoded whitelist.
- **PDF parser abuse**: pdfplumber can be slow/memory-hungry on adversarial pdfs. 10s timeout mitigates DoS; acceptable for MVP single-admin-invite traffic.
- **ZIP bomb via docx**: python-docx does not auto-expand; 30 MB/file cap limits exposure.
- **Prompt injection via extracted text**: `.extracted.md` content is injected into the claude prompt later. Upstream `research-method-designer` skill has a documented "non-answer" boundary (skill.md) that absorbs basic injection. No mitigation at this layer; documented so future scope doesn't accidentally treat `.extracted.md` as trusted.
- **`magic.from_buffer`** on untrusted bytes: libmagic has a CVE history but is battle-tested; acceptable risk.

---

## 13. Out of scope (deferred)

- Streaming upload (currently reads full content into memory — OK under the size caps).
- Virus scanning (ClamAV etc.); OCR for scanned pdfs (tesseract); per-user storage quotas.
- DB row persistence — happens in Task 3.3 router after calling `save_and_extract`.

---

## 14. Open points flagged for review

- **Test #20 exclusion** — HARNESS §1 "failed ⇒ error_message" is tested at router level (Task 3.3), not here. Agreed?
- **libmagic1 server dependency** — already installed on the Tencent host? If not, add to Task 5.1 deploy prereqs.
- **Fixture generation** — `sample.pdf`, `sample.docx`, `encrypted.pdf`, `empty.pdf` generated by a one-off script (not committed); fixtures themselves committed as binary. Acceptable vs. on-the-fly generation in conftest?
