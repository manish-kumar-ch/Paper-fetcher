# 📄 Paper Fetcher

Give it a **PubMed-export CSV** (PMID / DOI / PMCID columns) and it downloads the
**open-access PDFs** for those papers, then bundles them into a zip — from the
command line or a small web app.

It downloads *real PDF files* (verified by the `%PDF` header), never text or XML.

## What it does — and what it deliberately doesn't

**Legal open-access sources only:**

| Source | What it provides |
| --- | --- |
| **PubMed Central / Europe PMC** | Full-text PDFs for articles in the redistributable Open Access subset |
| **Unpaywall** | Locates legal author/publisher OA copies by DOI |
| **OpenAlex** | Independent OA index — catches repository / preprint copies Unpaywall misses |
| **Semantic Scholar** | Another independent OA index, by DOI |
| **`citation_pdf_url` meta tag** | The public PDF link publishers advertise for Google Scholar (last resort) |

**It does _not_ bypass paywalls or CAPTCHAs and does not scrape read-only content.**
Papers with no legal OA copy — or whose publisher blocks automated downloads — are
**reported, not scraped**, with a clickable link (PMC reader / DOI) so you can grab
them yourself or via interlibrary loan. This keeps the tool reliable and keeps your
IP/institution out of trouble; scrapers that fight bot-detection break constantly and
get blocked.

> **Coverage depends entirely on your list.** A set of recent PLoS/BMC/Frontiers papers
> may come through at ~90%; a set of 1990s–2000s subscription-journal articles may only
> yield the handful that were ever made open access. The report tells you exactly which
> is which.

## Install

```bash
pip install -r requirements.txt
```

## Use it — command line

```bash
python fetch_papers.py sample/csv-alitretino-set.csv --email you@example.com
```

Options:

| Flag | Meaning | Default |
| --- | --- | --- |
| `-o, --out` | Output directory | `out` |
| `--email` | Contact email for Unpaywall (required by their API terms) | — |
| `--delay` | Seconds between papers (be polite to the APIs) | `1.0` |
| `--max` | Only process the first N papers (for testing) | all |
| `--zip` | Zip output path | `<out>/papers_bundle.zip` |

Output:

```
out/
├── pdfs/                     # the downloaded PDFs
├── download_report.csv       # per-paper status + links for the ones it couldn't get
└── papers_bundle.zip         # pdfs/ + report, ready to share
```

`download_report.csv` columns: `pmid, pmcid, doi, year, first_author, title, status,
source, filename, size_or_note, url, manual_url`. `status` is `downloaded`,
`no_open_access`, or `error`; `manual_url` is a link you can click to fetch anything
that wasn't auto-downloaded.

## Use it — web app

```bash
streamlit run app.py
```

Upload a CSV, click **Fetch**, watch progress, download the zip. That's it.

### Deploy the web app (free)

- **Streamlit Community Cloud** — push this folder to GitHub, point
  [share.streamlit.io](https://share.streamlit.io) at `app.py`. Done.
- **Hugging Face Spaces** — create a new **Streamlit** Space, then add these files
  *alongside* the Space's auto-generated `README.md` — **do not overwrite it**, because
  HF stores the Space's `sdk`/`app_file` config in that file's YAML front-matter. If you
  do replace it, prepend this block so the Space still builds:

  ```yaml
  ---
  title: Paper Fetcher
  emoji: 📄
  colorFrom: blue
  colorTo: indigo
  sdk: streamlit
  app_file: app.py
  pinned: false
  ---
  ```
- **Docker** — a ready [`Dockerfile`](Dockerfile) is included:

  ```bash
  docker build -t paper-fetcher . && docker run -p 8501:8501 paper-fetcher
  ```

## Getting a CSV from PubMed

Run your search on [pubmed.ncbi.nlm.nih.gov](https://pubmed.ncbi.nlm.nih.gov) →
**Save** → *Selection: All results* → *Format: CSV*. Any CSV with a `PMID`, `DOI`,
or `PMCID` column works.

## Notes

- Be a good citizen: keep `--delay` at ~1s; these are free public APIs.
- Unpaywall needs a valid contact email — it's only sent as a courtesy identifier.
- Filenames are `PMID_FirstAuthor_Year.pdf` (DOI/PMCID fill in when a PMID is absent),
  de-duplicated so two papers never overwrite each other.

## Safety hardening (for public deployments)

Because the tool fetches URLs returned by third-party APIs, it:

- **Blocks SSRF** — every request (and every redirect hop) is validated to be `http(s)`
  and to resolve to a public IP, so a malicious record can't make the server probe
  cloud-metadata endpoints or internal hosts.
- **Bounds memory** — PDFs stream to disk with a size cap; landing pages are read with a
  hard byte limit. A huge or endless response can't OOM the process.
- **Cleans up** — the web app parses uploads in memory and deletes each run's temp files.
