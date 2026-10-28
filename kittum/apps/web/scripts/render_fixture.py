# /// script
# requires-python = ">=3.12"
# dependencies = ["pymupdf==2.0"]
# ///
"""Renders the open-access ABC-06 trial into the fixtures the mock workbench API serves.

Run once from kittum/apps/web: `uv run scripts/render_fixture.py`. It downloads the PDF from the
PubMed Central open-data bucket (CC BY 4.0), checks its SHA-256, and writes page PNGs, word boxes,
and anchors under src/data/mock/fixtures/abc06/. Tests never call it.

The output follows `kittum.documents`: one layer of NFC text with word boxes in unrotated PDF
points, pages 1-based, and anchors whose offsets are Unicode code points over the layer text.
Mentions are what the server will send for each full-text criterion; values are the sources of
the mock extraction proposals.
"""

import hashlib
import json
import re
import unicodedata
import urllib.request
import uuid
from pathlib import Path

import pymupdf

PDF_URL = "https://pmc-oa-opendata.s3.amazonaws.com/PMC8082275.1/PMC8082275.1.pdf"
PDF_SHA256 = "257ba4c15734c41b959a931d52528d9538f420f30537bdccfec71060cd749026"
SCALE = 2  # 144 dpi: sharp at laptop widths without bloating the repository
NS = uuid.UUID("6b1f3c3e-8f0e-4c55-9a51-4b8f2a6e0b16")

OUT = Path(__file__).parent.parent / "src" / "data" / "mock" / "fixtures" / "abc06"
CACHE = Path(__file__).parent / ".cache" / "abc06.pdf"

# Phrases the protocol's terms would find for each full-text criterion. Every occurrence becomes
# a mention, in document order.
MENTIONS: dict[str, list[str]] = {
    "C_POP_ADV_BTC": [
        "locally advanced or metastatic biliary tract cancer",
        "histologically or cytologically verified locally advanced",
        "intrahepatic cholangiocarcinoma (in 72",
        "aged 18 years or older",
    ],
    "C_POP_PRETREATED": [
        "progression to previous first-line cisplatin and gemcitabine",
        "after progression on cisplatin and gemcitabine",
        "progression on first-line chemotherapy",
        "A maximum of 6 weeks was allowed between disease progression",
    ],
    "C_INT_ARMS": [
        "centrally to active symptom control (ASC) and FOLFOX or ASC alone",
        "FOLFOX chemotherapy was administered",
        "were randomly assigned (1:1) to ASC plus FOLFOX",
        "75 (93%) of 81 patients in the ASC plus FOLFOX group",
    ],
    "C_DES_RCT": [
        "phase 3, open-label, randomised trial",
        "phase 3, open-label, randomised controlled study",
        "This was an open-label study, with no masking",
        "minimisation algorithm",
    ],
    "C_PUB_PRIMARY": [
        "The study is complete and the final results are reported",
        "registered with ClinicalTrials.gov, NCT01926236",
    ],
    "C_OUT_RELEVANT": [
        "The primary endpoint was overall survival",
        "quality-of-life analyses, the results of which will be reported separately",
        "Grade 3–5 adverse events were reported in 56",
        "data on quality of life and health economics",
    ],
}

# Sources of the mock extraction values: (name, page, quote).
VALUES: list[tuple[str, int, str]] = [
    ("n_total", 6, "162 patients from 20 sites in the UK were randomly assigned"),
    ("n_asc", 6, "randomly assigned to ASC alone (n=81)"),
    ("n_folfox", 6, "ASC plus FOLFOX (n=81; figure 1)"),
    ("follow_up", 6, "the median follow-up was 21·7 months (IQR 17·2–30·8)"),
    ("os_median_folfox", 6, "overall survival was 6·2 months (95% CI 5·4–7·6) in the ASC plus FOLFOX group"),
    ("os_median_asc", 6, "versus 5·3 months (4·1–5·8) in the ASC alone group"),
    ("os_hr", 6, "adjusted HR 0·69 [95% CI 0·50–0·97], p=0·031"),
    ("os_hr_sensitivity", 6, "adjusted HR 0·68, 95% CI 0·49–0·95"),
    ("os6_asc", 6, "35·5% (95% CI 25·2–46·0) at 6 months"),
    ("os12_asc", 6, "11·4% (5·6–19·5) at 12 months"),
    ("os6_folfox", 6, "50·6% (39·3–60·9) at 6 months"),
    ("os12_folfox", 6, "25·9% (17·0–35·8) at 12 months"),
    ("ae35_folfox", 7, "Grade 3–5 adverse events were reported in 56 (69%) of 81 patients in the ASC plus FOLFOX group"),
    ("ae35_asc", 7, "42 (52%) of 81 patients in the ASC alone group"),
    ("qol_missing", 5, "data on quality of life and health economics will be reported separately"),
]


def pdf_bytes() -> bytes:
    if not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(PDF_URL, timeout=60) as response:
            CACHE.write_bytes(response.read())
    data = CACHE.read_bytes()
    if hashlib.sha256(data).hexdigest() != PDF_SHA256:
        raise SystemExit("the downloaded PDF does not match the recorded SHA-256")
    return data


def layer_text(doc: pymupdf.Document) -> tuple[str, list[list[tuple[int, int, tuple[float, ...], tuple[int, int]]]]]:
    """Joins words into NFC text: spaces within a line, newlines between lines and blocks."""
    parts: list[str] = []
    cursor = 0
    pages = []
    for page in doc:
        words = []
        previous: tuple[int, int] | None = None
        for x0, y0, x1, y1, word, block, line, _ in sorted(
            page.get_text("words"), key=lambda w: (w[5], w[6], w[7])
        ):
            word = unicodedata.normalize("NFC", word)
            if previous is not None:
                gap = " " if previous == (block, line) else "\n" if previous[0] == block else "\n\n"
                parts.append(gap)
                cursor += len(gap)
            elif parts:
                parts.append("\n\n")
                cursor += 2
            words.append((cursor, cursor + len(word), (x0, y0, x1, y1), (block, line)))
            parts.append(word)
            cursor += len(word)
            previous = (block, line)
        pages.append(words)
    return "".join(parts), pages


def searchable(text: str) -> tuple[str, list[int]]:
    """Lower-cased text without soft hyphens and with whitespace runs collapsed, plus a map
    from each searchable character back to its code-point offset in the layer text."""
    chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        if ch == "\u00ad":
            continue
        if ch.isspace():
            if chars and chars[-1] == " ":
                continue
            ch = " "
        chars.append(ch.lower())
        origin.append(i)
    return "".join(chars), origin


def anchor(name: str, text: str, pages, start: int, end: int, layer_id: str) -> dict:
    boxes: dict[tuple[int, int, int], list[float]] = {}
    for page_index, words in enumerate(pages):
        for w_start, w_end, (x0, y0, x1, y1), (block, line) in words:
            if w_start < end and w_end > start:
                key = (page_index + 1, block, line)
                box = boxes.setdefault(key, [x0, y0, x1, y1])
                box[0], box[1] = min(box[0], x0), min(box[1], y0)
                box[2], box[3] = max(box[2], x1), max(box[3], y1)
    return {
        "id": str(uuid.uuid5(NS, f"{layer_id}/{name}/{start}")),
        "layer_id": layer_id,
        "quote": {
            "exact": text[start:end],
            "prefix": text[max(0, start - 32) : start],
            "suffix": text[end : end + 32],
        },
        "start": start,
        "end": end,
        "boxes": [
            {"page": page, "x0": round(b[0], 2), "y0": round(b[1], 2), "x1": round(b[2], 2), "y1": round(b[3], 2)}
            for (page, _, _), b in sorted(boxes.items(), key=lambda kv: (kv[0][0], kv[1][1], kv[1][0]))
        ],
    }


def find_all(haystack: str, origin: list[int], quote: str) -> list[tuple[int, int]]:
    needle = re.sub(r"\s+", " ", quote.replace("\u00ad", "")).lower()
    spans = []
    for match in re.finditer(re.escape(needle), haystack):
        spans.append((origin[match.start()], origin[match.end() - 1] + 1))
    return spans


def merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Overlapping phrase matches are one mention."""
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def page_of(pages, offset: int) -> int:
    for page_index, words in enumerate(pages):
        if words and words[0][0] <= offset <= words[-1][1]:
            return page_index + 1
    raise ValueError(offset)


def main() -> None:
    data = pdf_bytes()
    doc = pymupdf.open(stream=data, filetype="pdf")
    text, pages = layer_text(doc)
    recipe = {"engine": "pymupdf", "engine_version": pymupdf.VersionBind, "settings": [["words", "sort=block,line,word"]]}
    recipe_hash = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()
    output_hash = hashlib.sha256(text.encode()).hexdigest()
    document_id = str(uuid.uuid5(NS, PDF_SHA256))
    layer_id = str(uuid.uuid5(NS, f"{PDF_SHA256}/{recipe_hash}"))

    (OUT / "pages").mkdir(parents=True, exist_ok=True)
    (OUT / "words").mkdir(parents=True, exist_ok=True)
    page_meta = []
    for index, page in enumerate(doc):
        number = index + 1
        page.get_pixmap(matrix=pymupdf.Matrix(SCALE, SCALE), alpha=False).save(OUT / "pages" / f"{number}.png")
        page_meta.append({"page": number, "width": round(page.rect.width, 2), "height": round(page.rect.height, 2)})
        rows = [[s, e, *(round(v, 2) for v in box)] for s, e, box, _ in pages[index]]
        (OUT / "words" / f"{number}.json").write_text(json.dumps(rows, separators=(",", ":")) + "\n")

    haystack, origin = searchable(text)
    mentions: dict[str, list[dict]] = {}
    for criterion, phrases in MENTIONS.items():
        spans = merge(sorted({span for phrase in phrases for span in find_all(haystack, origin, phrase)}))
        missing = [p for p in phrases if not find_all(haystack, origin, p)]
        if missing:
            raise SystemExit(f"{criterion}: no match for {missing}")
        mentions[criterion] = [anchor(criterion, text, pages, s, e, layer_id) for s, e in spans]
    values: dict[str, dict] = {}
    for name, page, quote in VALUES:
        spans = [span for span in find_all(haystack, origin, quote) if page_of(pages, span[0]) == page]
        if len(spans) != 1:
            raise SystemExit(f"{name}: expected one match on page {page}, found {len(spans)}")
        values[name] = anchor(name, text, pages, *spans[0], layer_id)

    layer = {
        "document": {
            "id": document_id,
            "artifact_hash": PDF_SHA256,
            "media_type": "application/pdf",
            "page_count": doc.page_count,
        },
        "recipe": recipe,
        "layer": {
            "id": layer_id,
            "document_id": document_id,
            "recipe_hash": recipe_hash,
            "output_hash": output_hash,
            "text": text,
        },
        "pages": page_meta,
    }
    (OUT / "layer.json").write_text(json.dumps(layer, ensure_ascii=False, indent=1) + "\n")
    (OUT / "anchors.json").write_text(
        json.dumps({"mentions": mentions, "values": values}, ensure_ascii=False, indent=1) + "\n"
    )
    print(f"{doc.page_count} pages, {len(text)} code points, "
          f"{sum(len(m) for m in mentions.values())} mentions, {len(values)} values")


if __name__ == "__main__":
    main()
