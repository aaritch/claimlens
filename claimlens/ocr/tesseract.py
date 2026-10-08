"""Tesseract engine: free, runs locally, no data leaves your machine.

Needs the Tesseract program installed (apt install tesseract-ocr / brew install tesseract)
plus: pip install pytesseract pypdfium2 pillow.

Analogy: the free scanner app on your phone. Good on clean pages, shakier on blurry
faxes, and it costs CPU time instead of per-page fees.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

from claimlens.llm.pricing import estimate_ocr_cost
from claimlens.ocr.base import OcrError, OcrResult

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}


class TesseractEngine:
    name = "tesseract"

    def __init__(self, lang: str = "eng", dpi: int = 300):
        try:
            import pytesseract
            from PIL import Image  # noqa: F401
        except ImportError as exc:
            raise OcrError("Tesseract support needs: pip install pytesseract pillow pypdfium2") from exc
        self._pt = pytesseract
        self.lang, self.dpi = lang, dpi
        try:
            pytesseract.get_tesseract_version()
        except Exception as exc:  # pytesseract raises TesseractNotFoundError
            raise OcrError("Tesseract program not found. Install it (apt install tesseract-ocr / brew install tesseract).") from exc

    def extract(self, data: bytes, filename: str) -> OcrResult:
        ext = Path(filename).suffix.lower()
        if ext == ".pdf":
            images = self._render_pdf(data)
        elif ext in IMAGE_EXTS:
            images = self._load_images(data)
        else:
            raise OcrError(f"{filename}: Tesseract cannot read '{ext}' files")

        texts, confs = [], []
        for img in images:
            texts.append(self._pt.image_to_string(img, lang=self.lang).strip())
            confs.extend(self._confidences(img))
        mean = sum(confs) / len(confs) if confs else None
        return OcrResult(text="\n\n".join(texts), pages=len(images), engine=self.name,
                         mean_confidence=mean, cost_usd=estimate_ocr_cost(self.name, len(images)))

    def _confidences(self, img) -> list[float]:
        d = self._pt.image_to_data(img, lang=self.lang, output_type=self._pt.Output.DICT)
        return [float(c) for c, t in zip(d["conf"], d["text"]) if str(t).strip() and float(c) >= 0]

    def _render_pdf(self, data: bytes) -> list:
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise OcrError("PDF OCR with Tesseract needs: pip install pypdfium2") from exc
        try:
            pdf = pdfium.PdfDocument(data)
            return [pdf[i].render(scale=self.dpi / 72).to_pil().convert("L") for i in range(len(pdf))]
        except Exception as exc:
            raise OcrError("PDF could not be rendered for OCR") from exc

    def _load_images(self, data: bytes) -> list:
        from PIL import Image, ImageSequence
        try:
            img = Image.open(io.BytesIO(data))
            return [frame.convert("L") for frame in ImageSequence.Iterator(img)]  # handles multi-page TIFF
        except Exception as exc:
            raise OcrError("Image could not be read for OCR") from exc
