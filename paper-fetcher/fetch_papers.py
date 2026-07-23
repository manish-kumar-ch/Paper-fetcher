#!/usr/bin/env python3
"""
paper-fetcher — batch-download open-access PDFs for a PubMed-style CSV, then zip them.

Legal open-access sources only:
  1. PubMed Central / Europe PMC  (free full-text PDFs for OA articles)
  2. Unpaywall                    (locates legal author/publisher OA copies by DOI)
  3. OpenAlex + Semantic Scholar  (independent OA indexes — catch repository /
                                   preprint copies the others miss)

It downloads real PDF files (verified by the %PDF magic header), never text/XML.
It does NOT bypass paywalls or CAPTCHAs. Papers with no legal OA copy are reported
so you can request them through your library / interlibrary loan.

Usage:
    python fetch_papers.py sample/csv-alitretino-set.csv --email you@example.com
    python fetch_papers.py papers.csv -o out --email you@example.com --delay 1.0
"""
from __future__ import annotations

import argparse
import csv
import html
import ipaddress
import re
import socket
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse

import requests

# Reject responses larger than this (protects memory on shared deployments).
MAX_PDF_BYTES = 100 * 1024 * 1024      # 100 MB — a real PDF over this is treated as a failure
MAX_LANDING_BYTES = 512 * 1024          # 512 KB is plenty to find a <meta> tag
MAX_REDIRECTS = 5

EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UNPAYWALL = "https://api.unpaywall.org/v2"
OPENALEX = "https://api.openalex.org/works"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# ----------------------------------------------------------------------------- #
# CSV parsing
# ----------------------------------------------------------------------------- #

# Canonical field -> possible header spellings (matched case-insensitively).
COLUMN_ALIASES = {
    "pmid": ["pmid", "pubmed id", "pubmed_id"],
    "title": ["title"],
    "doi": ["doi"],
    "pmcid": ["pmcid", "pmc id", "pmc"],
    "first_author": ["first author", "first_author", "firstauthor"],
    "year": ["publication year", "year", "pub year", "pubyear"],
}


def _match_columns(fieldnames: list[str]) -> dict[str, str]:
    """Map canonical keys to the CSV's actual header names."""
    lower = {fn.strip().lower(): fn for fn in fieldnames if fn}
    resolved: dict[str, str] = {}
    for canon, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lower:
                resolved[canon] = lower[alias]
                break
    return resolved


@dataclass
class Paper:
    pmid: str = ""
    title: str = ""
    doi: str = ""
    pmcid: str = ""
    first_author: str = ""
    year: str = ""

    @property
    def label(self) -> str:
        return self.pmid or self.doi or self.title[:40] or "unknown"


def parse_papers(reader: csv.DictReader) -> list[Paper]:
    """Turn a csv.DictReader (from a file or in-memory text) into Paper records."""
    if not reader.fieldnames:
        raise ValueError("CSV appears to be empty or has no header row.")
    cols = _match_columns(reader.fieldnames)
    if "pmid" not in cols and "doi" not in cols and "pmcid" not in cols:
        raise ValueError(
            "CSV needs at least one of these columns: PMID, DOI, or PMCID. "
            f"Found headers: {reader.fieldnames}"
        )
    papers: list[Paper] = []
    for row in reader:
        def g(key: str) -> str:
            col = cols.get(key)
            return (row.get(col) or "").strip() if col else ""

        pmcid = g("pmcid").upper()
        if pmcid and not pmcid.startswith("PMC"):
            pmcid = "PMC" + pmcid
        paper = Paper(
            pmid=re.sub(r"\D", "", g("pmid")),
            title=g("title"),
            doi=g("doi").lower().replace("https://doi.org/", "").strip(),
            pmcid=pmcid,
            first_author=g("first_author"),
            year=re.sub(r"\D", "", g("year")),
        )
        if paper.pmid or paper.doi or paper.pmcid:
            papers.append(paper)
    return papers


def read_papers(csv_path: Path) -> list[Paper]:
    """Read a PubMed-style CSV file into Paper records (tolerant of header variations)."""
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        return parse_papers(csv.DictReader(fh))


def read_papers_from_bytes(data: bytes) -> list[Paper]:
    """Parse an uploaded CSV directly from memory (no shared temp file needed)."""
    text = data.decode("utf-8-sig", errors="replace")
    return parse_papers(csv.DictReader(text.splitlines()))


# ----------------------------------------------------------------------------- #
# HTTP helpers
# ----------------------------------------------------------------------------- #


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    return s


def is_safe_url(url: str) -> bool:
    """Allow only http(s) URLs that resolve to public IP addresses.

    Guards against SSRF: the tool fetches URLs handed to it by third-party APIs
    (Unpaywall) and scraped from publisher pages, so a malicious record must not be
    able to make us probe cloud metadata endpoints (169.254.169.254) or internal
    hosts. Every request — including each redirect hop — is checked here.
    """
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _fetch_once(session, url, *, stream, timeout, params) -> requests.Response | None:
    """One request that follows redirects manually, re-validating each hop."""
    current = url
    first_params = params
    for _ in range(MAX_REDIRECTS + 1):
        if not is_safe_url(current):
            return None
        r = session.get(current, params=first_params, stream=stream,
                        timeout=timeout, allow_redirects=False)
        first_params = None  # params only apply to the first hop
        location = r.headers.get("location")
        if r.status_code in (301, 302, 303, 307, 308) and location:
            r.close()
            current = urljoin(current, location)
            continue
        return r
    return None  # too many redirects


def _get(session: requests.Session, url: str, *, stream: bool = False,
         timeout: int = 40, params: dict | None = None) -> requests.Response | None:
    """GET with SSRF-validated redirects and a few retries on transient errors."""
    for attempt in range(3):
        try:
            r = _fetch_once(session, url, stream=stream, timeout=timeout, params=params)
        except requests.RequestException:
            r = None
        if r is not None:
            if r.status_code == 200 or r.status_code not in (429, 500, 502, 503, 504):
                return r
            r.close()
        time.sleep(1.5 * (attempt + 1))
    return None


def download_pdf(session: requests.Session, url: str, dest: Path) -> int | None:
    """Stream a URL to ``dest``, keeping it only if it is a real PDF.

    Returns the byte count on success, or None (and removes any partial file) if the
    response is not a PDF or exceeds MAX_PDF_BYTES. Streaming to disk bounds memory.
    """
    r = _get(session, url, stream=True)
    if r is None or r.status_code != 200:
        return None
    header = b""
    total = 0
    ok = False
    try:
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                # Verify the %PDF- magic once we have enough bytes, before trusting it.
                if header is not None:
                    header += chunk
                    if len(header) < 5:
                        continue
                    if not header.startswith(b"%PDF-"):
                        return None
                    fh.write(header)
                    total += len(header)
                    header = None  # magic confirmed; stream the rest straight through
                    continue
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    return None  # oversize -> treat as failure, not a truncated success
                fh.write(chunk)
        ok = total > 0
        return total if ok else None
    finally:
        r.close()
        if not ok:
            dest.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- #
# Source resolvers
# ----------------------------------------------------------------------------- #


def epmc_render_url(pmcid: str) -> str:
    return f"https://europepmc.org/articles/{pmcid}?pdf=render"


@dataclass
class EpmcInfo:
    pmcid: str = ""
    doi: str = ""
    is_oa: bool = False
    pdf_urls: list[str] = field(default_factory=list)


def europepmc_lookup(session: requests.Session, paper: Paper) -> EpmcInfo:
    """Look a paper up in Europe PMC to discover PMCID / OA status / PDF links."""
    query = None
    if paper.pmid:
        query = f"EXT_ID:{paper.pmid} AND SRC:MED"
    elif paper.doi:
        query = f'DOI:"{paper.doi}"'
    elif paper.pmcid:
        query = f"PMCID:{paper.pmcid}"
    if not query:
        return EpmcInfo()

    r = _get(
        session,
        f"{EUROPEPMC}/search",
        params={"query": query, "format": "json", "resultType": "core", "pageSize": 1},
    )
    if r is None:
        return EpmcInfo()
    try:
        results = r.json().get("resultList", {}).get("result", [])
    except ValueError:
        return EpmcInfo()
    if not results:
        return EpmcInfo()
    rec = results[0]
    info = EpmcInfo(
        pmcid=(rec.get("pmcid") or "").upper(),
        doi=(rec.get("doi") or "").lower(),
        is_oa=rec.get("isOpenAccess") == "Y",
    )
    # Harvest any advertised PDF links.
    for grp in rec.get("fullTextUrlList", {}).get("fullTextUrl", []):
        if grp.get("documentStyle") == "pdf" and grp.get("url"):
            info.pdf_urls.append(grp["url"])
    return info


def _dedupe(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    return [u for u in urls if u and not (u in seen or seen.add(u))]


def unpaywall_locations(session: requests.Session, doi: str, email: str) -> tuple[list[str], list[str]]:
    """Ask Unpaywall for legal OA locations for a DOI.

    Returns (direct_pdf_urls, landing_page_urls). Landing pages are only used as a
    last resort to read a publisher-advertised ``citation_pdf_url`` meta tag.
    """
    if not doi or "@" not in email:  # Unpaywall requires a real contact email
        return [], []
    r = _get(session, f"{UNPAYWALL}/{doi}", params={"email": email})
    if r is None or r.status_code != 200:
        return [], []
    try:
        data = r.json()
    except ValueError:
        return [], []
    if not data.get("is_oa"):
        return [], []
    pdfs: list[str] = []
    landings: list[str] = []
    locs = [data.get("best_oa_location") or {}] + list(data.get("oa_locations") or [])
    for loc in locs:
        if loc.get("url_for_pdf"):
            pdfs.append(loc["url_for_pdf"])
        if loc.get("url"):
            landings.append(loc["url"])
    return _dedupe(pdfs), _dedupe(landings)


def openalex_locations(session: requests.Session, doi: str, email: str) -> tuple[list[str], list[str]]:
    """Ask OpenAlex for OA locations for a DOI (catches repository / preprint copies
    Unpaywall sometimes misses). Returns (direct_pdf_urls, landing_page_urls)."""
    if not doi:
        return [], []
    r = _get(session, f"{OPENALEX}/https://doi.org/{doi}", params={"mailto": email or "anonymous@example.com"})
    if r is None or r.status_code != 200:
        return [], []
    try:
        data = r.json()
    except ValueError:
        return [], []
    pdfs: list[str] = []
    landings: list[str] = []
    locs = [data.get("best_oa_location"), data.get("primary_location")]
    locs += data.get("locations") or []
    for loc in locs:
        if not loc:
            continue
        if loc.get("pdf_url"):
            pdfs.append(loc["pdf_url"])
        if loc.get("is_oa") and loc.get("landing_page_url"):
            landings.append(loc["landing_page_url"])
    oa_url = (data.get("open_access") or {}).get("oa_url")
    if oa_url:  # may be a PDF or a landing page — the %PDF check downstream filters it
        pdfs.append(oa_url)
    return _dedupe(pdfs), _dedupe(landings)


def semantic_scholar_pdfs(session: requests.Session, doi: str) -> list[str]:
    """Ask Semantic Scholar for an open-access PDF URL for a DOI."""
    if not doi:
        return []
    r = _get(session, f"{SEMANTIC_SCHOLAR}/DOI:{doi}", params={"fields": "openAccessPdf"})
    if r is None or r.status_code != 200:
        return []
    try:
        oap = (r.json().get("openAccessPdf") or {})
    except ValueError:
        return []
    return [oap["url"]] if oap.get("url") else []


_CITATION_PDF_RE = re.compile(
    r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
    re.I,
)


def citation_pdf_url(session: requests.Session, landing_url: str) -> str | None:
    """Read a publisher's advertised ``citation_pdf_url`` from a landing page.

    This is the same public metadata Google Scholar uses to locate PDFs — not a
    paywall or bot-detection bypass. If the page is bot-walled (403), we simply
    return nothing and move on. The page is read with a hard byte cap so a huge or
    endless response can't exhaust memory.
    """
    r = _get(session, landing_url, stream=True)
    if r is None or r.status_code != 200 or "html" not in r.headers.get("Content-Type", "").lower():
        if r is not None:
            r.close()
        return None
    try:
        buf = bytearray()
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                buf.extend(chunk)
                if len(buf) >= MAX_LANDING_BYTES:
                    break
    finally:
        r.close()
    m = _CITATION_PDF_RE.search(buf.decode("utf-8", "replace"))
    if not m:
        return None
    # Unescape HTML entities (e.g. &amp;) and resolve any site-relative path.
    return urljoin(landing_url, html.unescape(m.group(1) or m.group(2)))


# ----------------------------------------------------------------------------- #
# Per-paper orchestration
# ----------------------------------------------------------------------------- #

STATUS_OK = "downloaded"
STATUS_NO_OA = "no_open_access"
STATUS_ERROR = "error"


@dataclass
class Result:
    paper: Paper
    status: str = STATUS_NO_OA
    source: str = ""
    url: str = ""
    filename: str = ""
    message: str = ""

    @property
    def manual_url(self) -> str:
        """A human-clickable link to obtain a paper we could not auto-download."""
        p = self.paper
        if p.pmcid:
            return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{p.pmcid}/"
        if p.doi:
            return f"https://doi.org/{p.doi}"
        if p.pmid:
            return f"https://pubmed.ncbi.nlm.nih.gov/{p.pmid}/"
        return ""


def _sanitize(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]+", "_", text)).strip("_")


def _safe_name(paper: Paper) -> str:
    """A base filename that is unique per paper (a distinct identifier is always in it)."""
    ident = paper.pmid or _sanitize(paper.pmcid) or _sanitize(paper.doi)
    author = _sanitize(paper.first_author)
    parts = [p for p in (ident, author, paper.year) if p]
    stem = "_".join(parts) or "paper"
    return stem[:120] + ".pdf"


def _unique_name(base: str, used: set[str]) -> str:
    """Ensure the filename hasn't already been claimed this batch (append _2, _3, ...)."""
    if base not in used:
        used.add(base)
        return base
    stem, ext = base[:-4], base[-4:]
    i = 2
    while f"{stem}_{i}{ext}" in used:
        i += 1
    name = f"{stem}_{i}{ext}"
    used.add(name)
    return name


def fetch_one(session: requests.Session, paper: Paper, email: str, pdf_dir: Path,
              used_names: set[str] | None = None) -> Result:
    """Resolve and download one paper's PDF from legal OA sources."""
    used_names = used_names if used_names is not None else set()
    result = Result(paper=paper, filename=_unique_name(_safe_name(paper), used_names))
    dest = pdf_dir / result.filename

    def save(source: str, url: str, size: int) -> Result:
        result.status = STATUS_OK
        result.source = source
        result.url = url
        result.message = f"{size // 1024} KB"
        return result

    # Build an ordered list of (source, url) candidates.
    candidates: list[tuple[str, str]] = []

    # 1) PMCID straight from the CSV -> Europe PMC render.
    if paper.pmcid:
        candidates.append(("pmc_render", epmc_render_url(paper.pmcid)))

    # 2) Europe PMC lookup (discovers PMCID / OA status / PDF links we didn't have).
    try:
        epmc = europepmc_lookup(session, paper)
    except Exception as exc:  # noqa: BLE001 - lookup must never abort the run
        epmc = EpmcInfo()
        result.message = f"epmc lookup failed: {exc}"
    if epmc.pmcid and epmc.is_oa:
        candidates.append(("europepmc", epmc_render_url(epmc.pmcid)))
    for u in epmc.pdf_urls:
        candidates.append(("europepmc", u))

    # 3) Unpaywall by DOI (CSV DOI first, else the one Europe PMC reported).
    doi = paper.doi or epmc.doi
    landings: list[str] = []
    if doi:
        try:
            pdfs, landings = unpaywall_locations(session, doi, email)
            for u in pdfs:
                candidates.append(("unpaywall", u))
        except Exception as exc:  # noqa: BLE001
            result.message = f"unpaywall failed: {exc}"

    # 4) OpenAlex — surfaces repository / preprint copies the others miss.
    if doi:
        try:
            oa_pdfs, oa_landings = openalex_locations(session, doi, email)
            for u in oa_pdfs:
                candidates.append(("openalex", u))
            landings += oa_landings
        except Exception:  # noqa: BLE001
            pass

    # 5) Semantic Scholar — another independent OA index.
    if doi:
        try:
            for u in semantic_scholar_pdfs(session, doi):
                candidates.append(("semantic_scholar", u))
        except Exception:  # noqa: BLE001
            pass

    # Phase 1: try every direct PDF candidate.
    seen: set[str] = set()
    ordered = [(s, u) for s, u in candidates if not (u in seen or seen.add(u))]
    for source, url in ordered:
        size = download_pdf(session, url, dest)
        if size:
            return save(source, url, size)

    # Phase 2 (last resort): scrape publisher-advertised citation_pdf_url meta tags.
    landings = _dedupe(landings)
    for landing in landings:
        pdf_url = citation_pdf_url(session, landing)
        if pdf_url and pdf_url not in seen:
            seen.add(pdf_url)
            size = download_pdf(session, pdf_url, dest)
            if size:
                return save("citation_meta", pdf_url, size)

    if not ordered and not landings:
        result.message = result.message or "no OA location advertised"
    else:
        result.message = result.message or "OA links found but publisher blocks download"
    result.status = STATUS_NO_OA
    return result


# ----------------------------------------------------------------------------- #
# Batch driver + zipping
# ----------------------------------------------------------------------------- #

ProgressCB = Callable[[int, int, Result], None]


def fetch_all(
    papers: Iterable[Paper],
    out_dir: Path,
    email: str,
    delay: float = 1.0,
    progress: ProgressCB | None = None,
    session: requests.Session | None = None,
) -> list[Result]:
    papers = list(papers)
    pdf_dir = out_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    session = session or make_session()
    used_names: set[str] = set()
    results: list[Result] = []
    for i, paper in enumerate(papers, 1):
        try:
            res = fetch_one(session, paper, email, pdf_dir, used_names)
        except Exception as exc:  # noqa: BLE001 - one bad paper can't kill the batch
            res = Result(paper=paper, status=STATUS_ERROR, message=str(exc))
        results.append(res)
        if progress:
            progress(i, len(papers), res)
        if delay and i < len(papers):
            time.sleep(delay)
    write_report(out_dir / "download_report.csv", results)
    return results


REPORT_FIELDS = [
    "pmid", "pmcid", "doi", "year", "first_author", "title",
    "status", "source", "filename", "size_or_note", "url", "manual_url",
]


def write_report(path: Path, results: list[Result]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(REPORT_FIELDS)
        for r in results:
            p = r.paper
            w.writerow([
                p.pmid, p.pmcid, p.doi, p.year, p.first_author, p.title,
                r.status, r.source, r.filename if r.status == STATUS_OK else "",
                r.message, r.url,
                "" if r.status == STATUS_OK else r.manual_url,
            ])


def make_zip(out_dir: Path, zip_path: Path) -> int:
    """Bundle downloaded PDFs + the report into a single zip. Returns file count."""
    pdf_dir = out_dir / "pdfs"
    report = out_dir / "download_report.csv"
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for pdf in sorted(pdf_dir.glob("*.pdf")):
            zf.write(pdf, arcname=f"pdfs/{pdf.name}")
            count += 1
        if report.exists():
            zf.write(report, arcname="download_report.csv")
    return count


def summarize(results: list[Result]) -> dict[str, int]:
    out = {STATUS_OK: 0, STATUS_NO_OA: 0, STATUS_ERROR: 0}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out


# ----------------------------------------------------------------------------- #
# CLI
# ----------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Download open-access PDFs for a PubMed-style CSV and zip them.",
    )
    ap.add_argument("csv", type=Path, help="Path to the PubMed-export CSV")
    ap.add_argument("-o", "--out", type=Path, default=Path("out"), help="Output directory")
    ap.add_argument(
        "--email",
        default=None,
        help="Contact email for the Unpaywall API (required by their terms)",
    )
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between papers (be polite)")
    ap.add_argument("--max", type=int, default=0, help="Only process the first N papers (0 = all)")
    ap.add_argument("--zip", type=Path, default=None, help="Zip output path (default: <out>/papers_bundle.zip)")
    args = ap.parse_args(argv)

    email = args.email or None
    if not email:
        print("WARNING: no --email given; Unpaywall lookups will be skipped.\n"
              "         Pass --email you@example.com to enable them.", file=sys.stderr)
        email = ""  # Unpaywall calls will be skipped when doi lookups get empty email

    papers = read_papers(args.csv)
    if args.max:
        papers = papers[: args.max]
    print(f"Loaded {len(papers)} papers from {args.csv}")

    def progress(i: int, n: int, res: Result) -> None:
        mark = {STATUS_OK: "OK  ", STATUS_NO_OA: "MISS", STATUS_ERROR: "ERR "}[res.status]
        note = f"[{res.source}] {res.message}" if res.status == STATUS_OK else res.message
        print(f"  [{i:>3}/{n}] {mark} {res.paper.label:<12} {note}")

    results = fetch_all(papers, args.out, email or "anonymous@example.com",
                        delay=args.delay, progress=progress)

    zip_path = args.zip or (args.out / "papers_bundle.zip")
    n = make_zip(args.out, zip_path)
    s = summarize(results)
    print("\n" + "=" * 60)
    print(f"Downloaded : {s[STATUS_OK]}")
    print(f"No OA copy : {s[STATUS_NO_OA]}")
    print(f"Errors     : {s[STATUS_ERROR]}")
    print(f"Zip        : {zip_path}  ({n} PDFs + report)")
    print(f"Report     : {args.out / 'download_report.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
