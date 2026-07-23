"""
paper-fetcher web UI (Streamlit).

Upload a PubMed-style CSV, fetch the open-access PDFs, and download them as a zip
— all in the browser. Deploy free on Streamlit Community Cloud or Hugging Face Spaces.

Run locally:
    pip install -r requirements.txt
    streamlit run app.py
"""
from __future__ import annotations

import io
import shutil
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

import fetch_papers as fp

st.set_page_config(page_title="Paper Fetcher", page_icon="📄", layout="centered")

st.title("📄 Paper Fetcher")
st.caption(
    "Batch-download **open-access** PDFs for a PubMed-export CSV, then grab them as a zip. "
    "Sources: PubMed Central / Europe PMC and Unpaywall. No paywall or CAPTCHA bypass — "
    "paywalled papers are listed with a link so you can request them from your library."
)

with st.sidebar:
    st.header("Settings")
    email = st.text_input(
        "Contact email (for Unpaywall)",
        value="",
        help="Unpaywall's API requires a contact email. It's only used as a courtesy header.",
        placeholder="you@example.com",
    )
    delay = st.slider("Delay between papers (sec)", 0.0, 3.0, 1.0, 0.1,
                      help="Be polite to the APIs. Lower = faster, higher = gentler.")
    st.markdown("---")
    st.markdown(
        "**CSV needs at least one of:** `PMID`, `DOI`, or `PMCID`. "
        "PubMed's *Save → CSV* export works out of the box."
    )
    st.caption("Very large batches (hundreds of papers) are better run with the CLI "
               "locally — a huge zip can exceed a free tier's memory.")

uploaded = st.file_uploader("Upload your CSV", type=["csv"])

if uploaded is not None:
    try:
        # Parse straight from memory — no shared temp file, so concurrent users
        # on a hosted deployment never clobber each other's upload.
        papers = fp.read_papers_from_bytes(uploaded.getvalue())
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read that CSV: {exc}")
        st.stop()

    st.success(f"Loaded **{len(papers)}** papers.")
    with st.expander("Preview parsed papers"):
        st.dataframe(
            pd.DataFrame(
                [{"PMID": p.pmid, "PMCID": p.pmcid, "DOI": p.doi,
                  "Year": p.year, "Title": p.title[:70]} for p in papers]
            ),
            use_container_width=True, hide_index=True,
        )

    if len(papers) > 300:
        st.warning(f"{len(papers)} papers is a large batch for the web app. "
                   "Consider the CLI (`python fetch_papers.py ...`) for very large sets.")

    if st.button("⬇️  Fetch open-access PDFs", type="primary", use_container_width=True):
        if not email or "@" not in email:
            st.warning("Enter a valid contact email in the sidebar so Unpaywall lookups work "
                       "(they're skipped otherwise).")
        # Work inside a temp dir that we always clean up; only the bytes we need
        # are lifted into session_state so results survive Streamlit reruns.
        out_dir = Path(tempfile.mkdtemp(prefix="paperfetch_"))
        try:
            progress = st.progress(0.0, text="Starting…")
            log = st.empty()
            lines: list[str] = []

            def cb(i: int, n: int, res: fp.Result) -> None:
                mark = {fp.STATUS_OK: "✅", fp.STATUS_NO_OA: "⬜", fp.STATUS_ERROR: "⚠️"}[res.status]
                note = res.message if res.status == fp.STATUS_OK else "no OA copy"
                lines.append(f"{mark} `{res.paper.label}` — {note}")
                progress.progress(i / n, text=f"{i}/{n} processed")
                log.markdown("\n".join(lines[-12:]))

            results = fp.fetch_all(
                papers, out_dir, email or "anonymous@example.com",
                delay=delay, progress=cb,
            )
            zip_path = out_dir / "papers_bundle.zip"
            n_pdfs = fp.make_zip(out_dir, zip_path)

            # Stash everything we render as plain bytes/values, then the temp dir is
            # safe to delete in `finally`.
            st.session_state["pf_result"] = {
                "summary": fp.summarize(results),
                "n_pdfs": n_pdfs,
                "zip_bytes": zip_path.read_bytes(),
                "report_bytes": (out_dir / "download_report.csv").read_bytes(),
            }
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

# Render results OUTSIDE the button block so a download click (which reruns the
# script) doesn't wipe the other button or force a re-fetch.
res = st.session_state.get("pf_result")
if res:
    s = res["summary"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Downloaded", s[fp.STATUS_OK])
    c2.metric("No OA copy", s[fp.STATUS_NO_OA])
    c3.metric("Errors", s[fp.STATUS_ERROR])

    st.download_button(
        f"📦  Download zip ({res['n_pdfs']} PDFs + report)",
        data=res["zip_bytes"],
        file_name="papers_bundle.zip",
        mime="application/zip",
        type="primary",
        use_container_width=True,
    )
    st.download_button(
        "Download report.csv",
        data=res["report_bytes"],
        file_name="download_report.csv",
        mime="text/csv",
        use_container_width=True,
    )
    st.subheader("Report")
    st.dataframe(
        pd.read_csv(io.BytesIO(res["report_bytes"])),
        use_container_width=True, hide_index=True,
    )
elif uploaded is None:
    st.info("Upload a CSV to begin. Try the included `sample/csv-alitretino-set.csv`.")
