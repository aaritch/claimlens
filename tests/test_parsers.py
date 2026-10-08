import pytest

from claimlens.datagen import write_scan_stub_pdf
from claimlens.parsers.base import ParseError, UnsupportedFileType
from claimlens.parsers.router import parse_bytes, parse_file


def test_every_sample_parses_and_contains_an_npi(samples):
    for path in sorted(samples.glob("[0-9]*")):
        parsed = parse_file(path)
        assert "NPI" in parsed.full_text() or "National Provider Identifier" in parsed.full_text(), path.name
        assert not parsed.any_needs_ocr, path.name


def test_email_with_attachment_recurses(samples):
    eml = next(p for p in samples.glob("*.eml") if parse_file(p).attachments)
    parsed = parse_file(eml)
    assert parsed.attachments[0].file_type == "pdf"
    assert "ATTACHMENT: notice.pdf" in parsed.full_text()


def test_scanned_pdf_is_flagged_for_ocr(tmp_path):
    stub = tmp_path / "scan.pdf"
    write_scan_stub_pdf(stub)
    parsed = parse_file(stub)
    assert parsed.needs_ocr and parsed.chars_per_page < 100


def test_image_goes_to_ocr():
    assert parse_bytes(b"\x89PNG....", "x.png").needs_ocr


def test_unsupported_type():
    with pytest.raises(UnsupportedFileType):
        parse_bytes(b"data", "notes.xyz")


@pytest.mark.parametrize("name", ["bad.pdf", "bad.docx"])
def test_corrupt_files_raise_parse_error(name):
    with pytest.raises(ParseError):
        parse_bytes(b"this is not a real file", name)
