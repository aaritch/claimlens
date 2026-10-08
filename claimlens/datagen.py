"""Synthetic document generator: fake-but-realistic IDR / surprise-billing paperwork.

Everything here is invented. Never put real PHI/PII into this project.
Each document is saved with a ground-truth JSON (the answer key) for Phase 2's
precision/recall evaluation.
"""
from __future__ import annotations

import hashlib
import io
import json
import random
import textwrap
from dataclasses import dataclass
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Optional, Union

from docx import Document
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from claimlens.rules.npi import make_npi
from claimlens.rules.payers import PAYER_ALIASES

PROVIDERS = [
    "Riverbend Orthopedic Group", "Lakeside Emergency Physicians", "Summit Radiology Associates",
    "Harborview Anesthesia Partners", "Cedar Grove Surgical Center", "Pinecrest Cardiology Clinic",
    "Meadowlark Pathology Labs", "Northgate Urgent Care",
]
PATIENTS = ["Jordan Avery", "Casey Morgan", "Riley Thompson", "Taylor Brooks", "Morgan Ellis"]
VIOLATIONS = {
    "surprise_billing": (
        "surprise billing violation",
        "I received emergency care and was later billed by an out-of-network provider for charges that my plan "
        "should have covered at in-network rates. I did not consent to out-of-network care.",
    ),
    "balance_billing": (
        "balance billing violation",
        "After my plan paid its share, the provider sent me a bill for the remaining balance, which federal "
        "protections do not allow in this situation.",
    ),
}
DOC_TYPES = ["surprise_billing_complaint", "provider_appeal", "idr_registration"]
FORMATS = {
    "surprise_billing_complaint": ["docx", "txt", "eml"],
    "provider_appeal": ["pdf", "docx"],
    "idr_registration": ["eml_attach", "pdf", "eml"],
}


@dataclass
class Facts:
    doc_type: str
    npi: str
    provider: str
    payer_written: str
    payer_canonical: str
    claim_ref: str
    service_date: date
    notice_date: date
    amount: float
    violation: Optional[str]
    patient: str
    npi_written: str = ""                    # what appears in the text (may contain an OCR-style typo)
    distractor_amount: Optional[float] = None  # an extra, larger 'total billed' amount
    has_service_date: bool = True
    injected: tuple = ()                       # which difficulties were injected (for error analysis)


def make_facts(rng: random.Random, doc_type: str, hard: bool = False) -> Facts:
    canonical = rng.choice(list(PAYER_ALIASES))
    npi = make_npi(rng.choice("12") + "".join(rng.choice("0123456789") for _ in range(8)))
    # Dates are safely in the past so the "not in the future" rule always passes.
    service = date(2023, 1, 1) + timedelta(days=rng.randint(0, 700))
    facts = Facts(
        doc_type=doc_type, npi=npi, provider=rng.choice(PROVIDERS),
        payer_written=rng.choice(PAYER_ALIASES[canonical]), payer_canonical=canonical,
        claim_ref=f"CLM-{rng.randint(100000, 999999)}", service_date=service,
        notice_date=service + timedelta(days=rng.randint(20, 120)),
        amount=round(rng.uniform(250, 28000), 2),
        violation=rng.choice(list(VIOLATIONS)) if doc_type == "surprise_billing_complaint" else None,
        patient=rng.choice(PATIENTS),
    )
    facts.npi_written = npi
    if hard:  # extra random draws happen ONLY in hard mode, so clean data stays byte-identical per seed
        injected = []
        if rng.random() < 0.25:  # OCR-style typo: one digit wrong. A single wrong digit always breaks the check digit.
            pos = rng.randrange(10)
            wrong = rng.choice([c for c in "0123456789" if c != npi[pos]])
            facts.npi_written = npi[:pos] + wrong + npi[pos + 1:]
            injected.append("npi_ocr_error")
        if rng.random() < 0.5:  # a bigger "total billed" number that is NOT the disputed amount
            facts.distractor_amount = round(facts.amount * rng.uniform(1.5, 4.0), 2)
            injected.append("distractor_amount")
        if doc_type != "surprise_billing_complaint" and rng.random() < 0.3:
            facts.has_service_date = False
            injected.append("service_date_absent")
        facts.injected = tuple(injected)
    return facts


def _fmt_date(d: date, style: int) -> str:
    return [d.isoformat(), d.strftime("%B %d, %Y"), d.strftime("%m/%d/%Y")][style % 3]


def _fmt_amount(a: float, style: int) -> str:
    return f"${a:,.2f}" if style % 2 == 0 else f"USD {a:,.2f}"


def render_lines(f: Facts, variant: int, style: int) -> tuple[str, list[str]]:
    """Return (subject, body lines). `variant` changes labels/wording; `style` changes date/amount formats."""
    d, a, n = _fmt_date(f.service_date, style), _fmt_amount(f.amount, style), _fmt_date(f.notice_date, style + 1)
    v = variant % 2
    npi = f.npi_written or f.npi
    if f.doc_type == "surprise_billing_complaint":
        phrase, narrative = VIOLATIONS[f.violation]
        subject = f"Complaint regarding {phrase}"
        lines = [
            f"RE: {subject}", "To: CMS Oversight Group, Provider Enforcement", f"Date of notice: {n}", "",
            "I am submitting a complaint about a bill that appears to violate federal protections.", "",
            f"Patient: {f.patient}",
            f"Billing provider: {f.provider} (NPI: {npi})" if v == 0 else f"Facility/Provider: {f.provider}, National Provider Identifier {npi}",
            f"Health plan: {f.payer_written}" if v == 0 else f"Insurer: {f.payer_written}",
            f"Claim reference: {f.claim_ref}" if v == 0 else f"Reference # {f.claim_ref}",
            f"Date of service: {d}" if v == 0 else f"Service date: {d}",
            f"Amount billed beyond in-network cost sharing: {a}", "", narrative,
        ]
    elif f.doc_type == "provider_appeal":
        subject = f"Provider Appeal of payment determination {f.claim_ref}"
        lines = [
            subject, f"Date: {n}", "",
            f"We are writing on behalf of {f.provider} to dispute the payment determination on the claim below.", "",
            f"Billing provider: {f.provider} (NPI: {npi})" if v == 0 else f"Provider name: {f.provider}, NPI {npi}",
            f"Payer: {f.payer_written}" if v == 0 else f"Insurer: {f.payer_written}",
            f"Claim reference: {f.claim_ref}", f"Date of service: {d}",
            f"Underpaid amount in dispute: {a}", "",
            "The allowed amount does not reflect the contracted rate. We request reconsideration and payment of the difference.",
        ]
    else:  # idr_registration
        subject = f"Notice of Intent to Initiate Federal IDR - {f.claim_ref}"
        lines = [
            subject, f"Date: {n}", "",
            "This notice is submitted to begin the federal independent dispute resolution process.", "",
            f"Billing provider: {f.provider} (NPI: {npi})" if v == 0 else f"Provider name: {f.provider}, NPI {npi}",
            f"Health plan: {f.payer_written}" if v == 0 else f"Payer: {f.payer_written}",
            f"Claim reference: {f.claim_ref}", f"Date of service: {d}",
            f"Total amount in dispute: {a}", "",
            "The 30-day open negotiation period has ended without agreement.",
        ]
    if f.distractor_amount is not None:
        idx = next(i for i, line in enumerate(lines) if a in line)
        lines.insert(idx, f"Total billed charges: {_fmt_amount(f.distractor_amount, style)}")
    if not f.has_service_date:
        lines = [line for line in lines if not line.startswith(("Date of service:", "Service date:"))]
    return subject, lines


# ---- writers ---------------------------------------------------------------

def write_docx(lines: list[str], path: Path) -> None:
    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    doc.save(str(path))


def write_pdf(lines: list[str], target: Union[Path, io.BytesIO]) -> None:
    c = canvas.Canvas(str(target) if isinstance(target, Path) else target, pagesize=LETTER)
    c.setFont("Helvetica", 10)
    y = 750
    for line in lines:
        for chunk in (textwrap.wrap(line, 95) or [""]):
            if y < 60:
                c.showPage()
                c.setFont("Helvetica", 10)
                y = 750
            c.drawString(60, y, chunk)
            y -= 14
    c.save()


def write_scan_stub_pdf(path: Path) -> None:
    """A PDF with shapes but NO text layer: stands in for a scanned page (OCR lands in Phase 3)."""
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.rect(60, 500, 400, 200)
    c.line(60, 480, 460, 480)
    c.save()


_FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/arial.ttf"]


def _font(size: int):
    for fp in _FONT_PATHS:
        if Path(fp).exists():
            return ImageFont.truetype(fp, size)
    return ImageFont.load_default(size)  # Pillow >= 10.1 can scale its built-in font


def render_scan_pages(lines: list[str], rng: random.Random, lines_per_page: int = 40) -> tuple[list[Image.Image], list[str]]:
    """Draw the text onto 'paper', then make it look scanned: slight skew, blur, speckle.
    Returns (page images, the true text of each page)."""
    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(textwrap.wrap(line, 78) or [""])
    chunks = [wrapped[i:i + lines_per_page] for i in range(0, len(wrapped), lines_per_page)] or [[""]]
    font, W, H = _font(30), 1700, 2200  # ~200 dpi US Letter
    images = []
    for chunk in chunks:
        img = Image.new("L", (W, H), 255)
        draw = ImageDraw.Draw(img)
        y = 140
        for line in chunk:
            draw.text((140, y), line, fill=20, font=font)
            y += 44
        img = img.rotate(rng.uniform(-0.8, 0.8), resample=Image.BICUBIC, fillcolor=255)
        img = img.filter(ImageFilter.GaussianBlur(0.6))
        speckle = ImageDraw.Draw(img)
        for _ in range(350):
            x, yy = rng.randrange(W), rng.randrange(H)
            speckle.ellipse([x, yy, x + rng.choice([1, 2]), yy + rng.choice([1, 2])], fill=rng.choice([60, 90, 140]))
        images.append(img)
    return images, ["\n".join(c) for c in chunks]


def scan_pdf_bytes(images: list[Image.Image]) -> bytes:
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:], resolution=200)
    return buf.getvalue()


def scan_png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def write_oracle_text(oracle_dir: Path, file_bytes: bytes, page_texts: list[str]) -> None:
    """Save the TRUE text keyed by the scan's hash. Used only by the demo OracleOcr."""
    oracle_dir.mkdir(parents=True, exist_ok=True)
    (oracle_dir / f"{hashlib.sha1(file_bytes).hexdigest()}.txt").write_text("\f".join(page_texts))


def write_eml(subject: str, lines: list[str], path: Path, attach_pdf: bool, attachment: Optional[bytes] = None) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"] = "intake@example-provider.test", "oversight@example-agency.test"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 05 Feb 2024 10:00:00 +0000"
    if attach_pdf:
        msg.set_content("Hello,\n\nPlease find our notice attached.\n\nRegards,\nBilling Office")
        if attachment is None:
            buf = io.BytesIO()
            write_pdf(lines, buf)
            attachment = buf.getvalue()
        msg.add_attachment(attachment, maintype="application", subtype="pdf", filename="notice.pdf")
    else:
        msg.set_content("\n".join(lines))
    path.write_bytes(bytes(msg))


# ---- driver ----------------------------------------------------------------

def generate_samples(out_dir: Path, count: int = 12, seed: int = 42, include_scan_stub: bool = True,
                     difficulty: str = "clean", scan_fraction: float = 0.0) -> list[Path]:
    """Create `count` documents + answer keys. Deterministic for a given seed.
    difficulty="hard" injects realistic problems (typo'd NPIs, distractor amounts, missing dates).
    scan_fraction=0.3 turns ~30% of documents into noisy 'scans' (image-only PDFs/PNGs, or emails with a
    scanned attachment) so the OCR path has real work. True text for each scan goes to ocr_oracle/."""
    if not 0.0 <= scan_fraction <= 1.0:
        raise ValueError("scan_fraction must be between 0 and 1")
    if difficulty not in ("clean", "hard"):
        raise ValueError("difficulty must be 'clean' or 'hard'")
    rng = random.Random(seed)
    out_dir = Path(out_dir)
    truth_dir = out_dir / "ground_truth"
    truth_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    scan_rng = random.Random(f"scan:{seed}")  # separate stream: enabling scans never changes the other documents

    for i in range(count):
        doc_type = DOC_TYPES[i % 3]
        round_no = i // 3
        fmt = FORMATS[doc_type][round_no % len(FORMATS[doc_type])]
        facts = make_facts(rng, doc_type, hard=(difficulty == "hard"))
        subject, lines = render_lines(facts, variant=round_no, style=round_no + i)
        stem = f"{i + 1:02d}_{doc_type}"
        scanned = scan_fraction > 0 and scan_rng.random() < scan_fraction
        injected = list(facts.injected)
        scan_pages = 0

        if scanned:
            images, page_texts = render_scan_pages(lines, scan_rng, lines_per_page=scan_rng.choice([40, 9]))
            scan_pages = len(images)
            injected.append("scanned")
            if fmt == "eml_attach":
                pdf = scan_pdf_bytes(images)
                path = out_dir / f"{stem}.eml"
                write_eml(subject, lines, path, attach_pdf=True, attachment=pdf)
                write_oracle_text(out_dir / "ocr_oracle", pdf, page_texts)
            elif len(images) == 1 and scan_rng.random() < 0.35:
                data = scan_png_bytes(images[0])
                path = out_dir / f"{stem}_scan.png"
                path.write_bytes(data)
                write_oracle_text(out_dir / "ocr_oracle", data, page_texts)
            else:
                data = scan_pdf_bytes(images)
                path = out_dir / f"{stem}_scan.pdf"
                path.write_bytes(data)
                write_oracle_text(out_dir / "ocr_oracle", data, page_texts)
        else:
            ext = {"eml_attach": "eml"}.get(fmt, fmt)
            path = out_dir / f"{stem}.{ext}"
            if fmt == "docx":
                write_docx(lines, path)
            elif fmt == "pdf":
                write_pdf(lines, path)
            elif fmt == "txt":
                path.write_text("\n".join(lines))
            else:
                write_eml(subject, lines, path, attach_pdf=(fmt == "eml_attach"))

        truth = {
            "filename": path.name,
            "doc_type": doc_type,
            "difficulty": difficulty,
            "injected": injected,
            "scan_pages": scan_pages,
            "expected": {
                "provider_npi": facts.npi, "provider_name": facts.provider, "payer_name": facts.payer_canonical,
                "claim_reference": facts.claim_ref,
                "service_date": facts.service_date.isoformat() if facts.has_service_date else None,
                "disputed_amount": facts.amount, "billing_violation_type": facts.violation,
            },
        }
        (truth_dir / f"{stem}.json").write_text(json.dumps(truth, indent=2))
        paths.append(path)

    if include_scan_stub:
        stub = out_dir / "scan_stub_01.pdf"
        write_scan_stub_pdf(stub)
        paths.append(stub)
    return paths
