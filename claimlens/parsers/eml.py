"""Email (.eml) parser: headers + body + attachments, recursively.

Analogy: opening an envelope, reading the letter, then also opening every
smaller envelope inside it.
"""
from __future__ import annotations

import re
from email import policy
from email.parser import BytesParser

from claimlens.parsers.base import ParsedDocument, ParseError, UnsupportedFileType

MAX_DEPTH = 3  # guard against emails nested inside emails inside emails...


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text)


def parse_eml(data: bytes, filename: str, min_chars_per_page: int = 100, _depth: int = 0) -> ParsedDocument:
    from claimlens.parsers.router import parse_bytes  # local import avoids a circular import

    try:
        msg = BytesParser(policy=policy.default).parsebytes(data)
    except Exception as exc:  # the stdlib parser is lenient; this is a safety net
        raise ParseError(f"{filename}: unreadable email ({exc.__class__.__name__})") from exc

    header_lines = [f"{h}: {msg[h]}" for h in ("From", "To", "Date", "Subject") if msg[h]]
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = _strip_html(body)

    doc = ParsedDocument(filename=filename, file_type="eml", text="\n".join(header_lines) + "\n\n" + body)

    if _depth >= MAX_DEPTH:
        doc.warnings.append("Attachment depth limit reached; deeper attachments skipped")
        return doc

    for part in msg.iter_attachments():
        att_name = part.get_filename() or "unnamed_attachment"
        payload = part.get_content()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)):
            doc.warnings.append(f"Skipped attachment {att_name}: unsupported content")
            continue
        try:
            doc.attachments.append(
                parse_bytes(bytes(payload), att_name, min_chars_per_page=min_chars_per_page, _depth=_depth + 1)
            )
        except UnsupportedFileType:
            doc.warnings.append(f"Skipped attachment {att_name}: unsupported type")
        except ParseError as exc:
            doc.warnings.append(f"Attachment failed to parse: {exc}")
    return doc
