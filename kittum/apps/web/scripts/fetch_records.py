"""Records the PubMed records behind the mock title and abstract queue.

Run once from kittum/apps/web: `uv run --no-project python scripts/fetch_records.py`.
It calls NCBI E-utilities and writes src/data/mock/fixtures/records.json. Tests never call it.
The PMIDs are a realistic queue for second-line advanced biliary tract cancer: randomised trials
that belong in it and the first-line, adjuvant, single-arm, other-tumour, and review records that
a search for it also returns.
"""

import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

PMIDS = [
    "32416072",  # ClarIDHy
    "33798493",  # ABC-06
    "20375404",  # ABC-02, first line
    "34656226",  # NIFTY
    "37075781",  # KEYNOTE-966, first line
    "36652354",  # FOENIX-CCA2, single arm
    "30922733",  # BILCAP, adjuvant
    "26615328",  # NAPOLI-1, pancreatic cancer
    "40910862",  # network meta-analysis
    "29955136",  # XELIRI versus irinotecan
    "32464280",  # regorafenib versus placebo
    "34554208",  # ClarIDHy final overall survival
    "37901200",  # KEYNOTE-394, hepatocellular carcinoma
    "41264278",  # HERIZON-BTC-01, single arm
    "36951834",  # liposomal irinotecan, randomised phase 2
    "38870977",  # NALIRICC
    "34973222",  # letter
    "38838500",  # FIGHT-202, single arm
    "37751561",  # SGNTUC-019, single arm
    "39438001",  # individual patient data meta-analysis
    "35510484",  # HERB protocol
    "41349554",  # tinengotinib, single arm
    "40631456",  # SUN-CK, single arm
    "38319896",  # TOPAZ-1, first line
    "41646284",  # ClarIDHy benefit-risk
    "38353044",  # meta-analysis
    "38278871",  # ClarIDHy pharmacokinetics
    "40147791",  # pooled analysis
]

OUT = Path(__file__).parent.parent / "src" / "data" / "mock" / "fixtures" / "records.json"
URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&retmode=xml&id="


def text(node: ET.Element | None) -> str:
    return re.sub(r"\s+", " ", "".join(node.itertext())).strip() if node is not None else ""


def record(article: ET.Element) -> dict:
    citation = article.find("MedlineCitation")
    assert citation is not None
    art = citation.find("Article")
    assert art is not None
    sections = [
        {"label": (part.get("Label") or "").capitalize() or None, "text": text(part)}
        for part in art.findall("Abstract/AbstractText")
        if "TRANSLATION" not in (part.get("Label") or "")
    ]
    year = text(art.find("Journal/JournalIssue/PubDate/Year")) or text(
        art.find("Journal/JournalIssue/PubDate/MedlineDate")
    )[:4]
    ids = {i.get("IdType"): text(i) for i in article.findall("PubmedData/ArticleIdList/ArticleId")}
    abstract = " ".join(s["text"] for s in sections)
    registry = sorted(
        set(re.findall(r"NCT\d{8}", abstract))
        | {text(a) for a in art.findall("DataBankList/DataBank/AccessionNumberList/AccessionNumber")}
    )
    return {
        "pmid": text(citation.find("PMID")),
        "doi": ids.get("doi"),
        "title": text(art.find("ArticleTitle")).rstrip("."),
        "journal": text(art.find("Journal/ISOAbbreviation")),
        "year": int(year),
        "publication_types": [text(p) for p in art.findall("PublicationTypeList/PublicationType")],
        "registry_ids": [r for r in registry if r.startswith("NCT")],
        "language": text(art.find("Language")),
        "sections": sections,
    }


def main() -> None:
    with urllib.request.urlopen(URL + ",".join(PMIDS), timeout=60) as response:
        root = ET.fromstring(response.read())
    by_pmid = {r["pmid"]: r for r in map(record, root.findall("PubmedArticle"))}
    missing = [p for p in PMIDS if p not in by_pmid]
    if missing:
        raise SystemExit(f"PubMed returned no record for {missing}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "NCBI E-utilities efetch, db=pubmed",
        "retrieved": time.strftime("%Y-%m-%d"),
        "records": [by_pmid[p] for p in PMIDS],
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
    print(f"wrote {len(PMIDS)} records to {OUT}")


if __name__ == "__main__":
    main()
