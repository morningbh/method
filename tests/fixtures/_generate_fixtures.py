"""One-off fixture generator — NOT a test.

Regenerates the binary fixtures in tests/fixtures/. Normally the fixtures are
committed as binaries alongside this script; run this only if you need to
recreate them (e.g. after corrupting or intentionally rotating them).

Usage:
    pip install --no-deps reportlab pypdf
    python tests/fixtures/_generate_fixtures.py

Fixtures produced (all kept small, < 10 KB each):
    sample.md        — plain markdown
    sample.txt       — plain text
    sample.pdf       — tiny 1-page PDF with known text
    sample.docx      — tiny 1-paragraph DOCX
    encrypted.pdf    — password-protected PDF (pdfplumber raises)
    empty.pdf        — 1-page PDF with no extractable text

Every generated PDF/DOCX contains known sentinel text ("Hello from Method test
PDF/DOCX …") so the tests can assert on the extracted content.
"""
from __future__ import annotations

import io
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent

MD_TEXT = "# Sample\n\nHello from Method test markdown.\n"
TXT_TEXT = "Hello from Method test txt.\n"
PDF_TEXT = "Hello from Method test PDF. This is a sample document for extraction testing."
DOCX_TEXT = "Hello from Method test DOCX. This is a sample document for extraction testing."


def write_md() -> None:
    (FIXTURES_DIR / "sample.md").write_text(MD_TEXT, encoding="utf-8")


def write_txt() -> None:
    (FIXTURES_DIR / "sample.txt").write_text(TXT_TEXT, encoding="utf-8")


def write_sample_pdf() -> None:
    from reportlab.pdfgen import canvas

    path = FIXTURES_DIR / "sample.pdf"
    c = canvas.Canvas(str(path))
    c.drawString(72, 720, PDF_TEXT)
    c.showPage()
    c.save()


def write_empty_pdf() -> None:
    """PDF with zero extractable text (blank page)."""
    from reportlab.pdfgen import canvas

    path = FIXTURES_DIR / "empty.pdf"
    c = canvas.Canvas(str(path))
    # No drawString — blank page.
    c.showPage()
    c.save()


def write_encrypted_pdf() -> None:
    """Password-protected PDF (pdfplumber raises)."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    # First create an unencrypted PDF in memory.
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "Secret content.")
    c.showPage()
    c.save()
    buf.seek(0)

    # Then encrypt it with pypdf.
    reader = PdfReader(buf)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password="secret", owner_password="owner")

    path = FIXTURES_DIR / "encrypted.pdf"
    with path.open("wb") as fh:
        writer.write(fh)


def write_sample_docx() -> None:
    """Build a minimal valid .docx (zip of 3 XML parts).

    python-docx's default Document() template is bloated (~36 KB). We assemble
    the minimum files that python-docx can still parse (<1 KB).
    """
    import zipfile

    path = FIXTURES_DIR / "sample.docx"

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
        "  <w:body>\n"
        f"    <w:p><w:r><w:t>{DOCX_TEXT}</w:t></w:r></w:p>\n"
        "  </w:body>\n"
        "</w:document>"
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>\n'
        "</Relationships>"
    )
    ctypes_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
        "</Types>"
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes_xml)
        z.writestr("_rels/.rels", rels_xml)
        z.writestr("word/document.xml", document_xml)


def main() -> None:
    write_md()
    write_txt()
    write_sample_pdf()
    write_empty_pdf()
    write_encrypted_pdf()
    write_sample_docx()
    for f in sorted(FIXTURES_DIR.iterdir()):
        if f.name.startswith("_") or f.name == "__init__.py":
            continue
        print(f"{f.name}: {f.stat().st_size} bytes")


if __name__ == "__main__":
    main()
