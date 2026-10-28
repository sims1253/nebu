"""Page triage: is a page's text layer usable, does it need OCR, or is its encoding broken?

Firecrawl's pdf-inspector classifies pages and flags garbled text. A second check counts
characters a working ToUnicode map never produces (U+FFFD, private use, controls) in PyMuPDF's
text, because a page can have a broken font that pdf-inspector's sample missed. pdf-inspector does
not read OCRmyPDF's invisible text layers yet (firecrawl/pdf-inspector#554), so triage runs on the
original PDF, before OCR.
"""

import unicodedata

import pdf_inspector
import pymupdf

from kittum.documents.model import PageKind, PageTriage

_BAD_SHARE = 0.05  # share of suspicious characters that marks a page's encoding as broken
_MIN_CHARS = 20  # fewer visible characters than this on a page with images means a scan


def _suspicious(ch: str) -> bool:
    if ch == "\ufffd":
        return True
    return unicodedata.category(ch) in ("Co", "Cc", "Cs") and ch not in "\n\r\t"


def triage(pdf: bytes) -> tuple[PageTriage, ...]:
    report = pdf_inspector.process_pdf_bytes(pdf)
    reasons = {r.page: tuple(r.reasons) for r in report.ocr_reasons_by_page}
    needs_ocr = set(report.pages_needing_ocr)
    out: list[PageTriage] = []
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        for page in doc:
            number = page.number + 1  # ty: ignore[unsupported-operator]
            why = list(reasons.get(number, ()))
            text = "".join(ch for ch in page.get_text() if not ch.isspace())
            bad = sum(1 for ch in text if _suspicious(ch))
            kind: PageKind = "text"
            if any("garbl" in r or "encoding" in r or "cmap" in r for r in why) or (
                text and bad / len(text) > _BAD_SHARE
            ):
                kind = "broken_encoding"
                if text and bad / len(text) > _BAD_SHARE:
                    why.append(f"pymupdf_suspicious_chars:{bad}/{len(text)}")
            elif number in needs_ocr or (len(text) < _MIN_CHARS and page.get_images()):
                kind = "ocr"
                if number not in needs_ocr:
                    why.append("pymupdf_no_text_over_images")
            out.append(PageTriage(page=number, kind=kind, reasons=tuple(why)))
    return tuple(out)
