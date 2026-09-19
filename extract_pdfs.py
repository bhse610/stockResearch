"""
Extract text from earnings PDFs into clean Markdown documents.

The quarterly-analysis agent reads text documents from `documents/<COMPANY>/`.
This script walks that folder, finds every PDF, extracts its text, and writes a
sibling `.md` file (PDF pages separated by clear markers) so the agent can cite
them with the quote-first rules.

Usage:
    python extract_pdfs.py                       # extract everything under documents/
    python extract_pdfs.py --root documents      # explicit root
    python extract_pdfs.py --company INFY        # only one company folder
    python extract_pdfs.py --force               # overwrite existing .md
    python extract_pdfs.py --keep-marks          # keep page markers in output
"""

import os
import re
import sys
import argparse
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _clean_text(text: str) -> str:
    """Normalise whitespace from extracted PDF text."""
    # Collapse the very common "one word per line" artefacts lightly, but keep
    # paragraph breaks. PDFs often vary, so we keep this conservative.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Replace non-breaking spaces and zero-width chars.
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    # Trim trailing spaces on each line.
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    # Collapse 3+ blank lines into 2.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract text from a PDF, returning one string with page markers."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        raise RuntimeError("pypdf is required. Install it: pip install pypdf")

    reader = PdfReader(str(pdf_path))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("  page %d failed: %s", i, exc)
            page_text = ""
        pages.append((i, _clean_text(page_text)))

    chunks = []
    for i, text in pages:
        if not text:
            continue
        chunks.append(f"<!-- page {i} -->\n{text}")
    return "\n\n".join(chunks)


def pdf_to_markdown(pdf_path: Path, keep_marks: bool) -> str:
    """Convert a single PDF file into a Markdown document."""
    body = extract_pdf_text(pdf_path)
    if not keep_marks:
        body = re.sub(r"<!-- page \d+ -->\n?", "", body)
    header = f"# {pdf_path.stem}\n\n_Source file: {pdf_path.name}_\n\n"
    return header + body + "\n"


def find_pdfs(root: Path, company: str | None = None) -> list[Path]:
    """Find PDFs under root (optionally filtered to one company folder)."""
    base = root
    if company:
        # Case-insensitive match on the company folder.
        match = [d for d in root.iterdir() if d.is_dir() and d.name.lower() == company.lower()]
        if not match:
            logger.error("No folder for company %r under %s", company, root)
            return []
        base = match[0]
    return sorted(p for p in base.rglob("*.pdf") if p.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract earnings PDFs to Markdown.")
    parser.add_argument("--root", default="documents", help="Root docs folder.")
    parser.add_argument("--company", default="", help="Only this company folder.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing .md.")
    parser.add_argument("--keep-marks", action="store_true",
                        help="Keep '<!-- page N -->' markers in output.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")

    root = Path(args.root)
    if not root.is_dir():
        print(f"Root folder not found: {root}")
        return 1

    pdfs = find_pdfs(root, args.company or None)
    if not pdfs:
        print("No PDF files found.")
        return 1

    extracted = 0
    skipped = 0
    empty = 0
    for pdf in pdfs:
        out_path = pdf.with_suffix(".md")
        if out_path.exists() and not args.force:
            logger.info("Skip (exists): %s", out_path.relative_to(root))
            skipped += 1
            continue
        logger.info("Extracting: %s", pdf.relative_to(root))
        text = pdf_to_markdown(pdf, args.keep_marks)
        # Sanity: did we get real text (scanned PDFs yield ~nothing)?
        plain = re.sub(r"<!--.*?-->", "", text).strip()
        if len(plain) < 100:
            logger.warning("  -> almost no text extracted (scanned image PDF?): %s", pdf.name)
            empty += 1
        out_path.write_text(text, encoding="utf-8")
        n_chars = len(text)
        logger.info("  -> wrote %s (%d chars)", out_path.relative_to(root), n_chars)
        extracted += 1

    print(f"\nExtracted {extracted}, skipped {skipped}, empty/scan-like {empty}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
