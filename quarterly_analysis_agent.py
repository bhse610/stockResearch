"""
LangGraph node that analyses quarterly results for the companies held in the
portfolio Excel file, using the quote-first prompt defined in
`prompts/QUARTERLYANALYSIS.md`.

Flow:
    LangGraph node
      -> read_holdings_from_excel   (companies from kite_portfolio.xlsx)
      -> analyse_quarterly_results  (per-company LLM analysis, quote-first)
      -> write_analysis_report      (Markdown + JSON output)

The analysis prompt is quote-first: every factual claim must be backed by an
exact quote from a source document. Documents live under a `documents/`
directory, either globally or per-company, e.g.:

    documents/
        INFY/                # optional per-company folder
            2025-Q2-earnings.md
            2025-Q1-earnings.md
        HDFCBANK/
            guidance.md
        annual-report.pdf    # global docs (applied to every company)

Supported document types: .md, .txt, .json, and .pdf (PDFs need `pypdf`,
installed automatically if available).

Requirements:
    pip install -r requirements.txt      (openpyxl, langchain-openai, ...)
    Env var: DEEPSEEK_API_KEY

Usage:
    python quarterly_analysis_agent.py
    python quarterly_analysis_agent.py --excel kite_portfolio.xlsx
    python quarterly_analysis_agent.py --quarter "Q2 2026"
    python quarterly_analysis_agent.py --companies INFY,HDFCBANK
    python quarterly_analysis_agent.py --quarter "Q2 2026" --limit 1
"""

import os
import sys
import json
import asyncio
import logging
from pathlib import Path
from typing import Any, Optional, TypedDict

from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Console encoding (Windows default cp1252 can't print emoji/unicode)
# ---------------------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - not all streams support reconfigure
        pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

DEFAULT_EXCEL = "kite_portfolio.xlsx"
DEFAULT_PROMPT_FILE = os.path.join("prompts", "QUARTERLYANALYSIS.md")
DEFAULT_DOCS_DIR = "documents"
DEFAULT_OUT_MD = "quarterly_analysis.md"
DEFAULT_OUT_JSON = "quarterly_analysis.json"

# Placeholders the prompt template uses.
PLACEHOLDER_COMPANY = "[COMPANY NAME]"
PLACEHOLDER_QUARTER = "[Q_ 20__]"

# Document extensions we can read directly; PDFs handled if pypdf present.
TEXT_EXTS = {".md", ".txt", ".json", ".csv"}

# Documentation files that must never be fed to the model as source evidence.
IGNORED_DOC_NAMES = {"readme.md", "readme.txt", "license", "license.md"}

# Max characters of source documents to include per company. Documents beyond
# this budget are truncated (with a marker) to stay within the model context.
# Override with the DOC_CHAR_BUDGET env var. ~4 chars/token, so 120k ~ 30k tok.
DOC_CHAR_BUDGET = int(os.environ.get("DOC_CHAR_BUDGET", "120000"))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class QuarterlyState(TypedDict, total=False):
    """State passed between LangGraph nodes."""
    # Input
    excel_path: str                 # portfolio workbook (source of companies)
    prompt_file: str                # quarterly analysis prompt template
    docs_dir: str                   # directory of source documents
    quarter: str                    # e.g. "Q2 2026" (fills [Q_ 20__])
    companies: list                 # explicit company list (overrides Excel)
    limit: int                      # max number of companies to analyse

    # Intermediate
    company_symbols: list           # resolved from Excel if `companies` empty
    company_documents: dict         # {symbol: [loaded document text]}

    # Output
    analyses: list                  # [{company, quarter, analysis, error}]
    report_md: str                  # combined Markdown report
    error: Optional[str]


# ---------------------------------------------------------------------------
# Excel: read the companies we hold
# ---------------------------------------------------------------------------
def read_holdings_from_excel(excel_path: str) -> list[dict]:
    """
    Read the 'Holdings' sheet of the portfolio workbook and return its rows.

    Falls back to the first worksheet if there is no 'Holdings' sheet.
    Returns [] if the file or sheet cannot be read.
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        logger.error("openpyxl is required. Install it: pip install openpyxl")
        return []

    if not os.path.exists(excel_path):
        logger.error("Excel file not found: %s", excel_path)
        return []

    wb = load_workbook(excel_path, read_only=True, data_only=True)
    ws = wb["Holdings"] if "Holdings" in wb.sheetnames else wb[wb.sheetnames[0]]

    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        logger.warning("Sheet %r is empty.", ws.title)
        return []

    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    records = []
    for row in rows[1:]:
        if row is None or all(c is None for c in row):
            continue
        records.append({headers[i]: row[i] for i in range(min(len(headers), len(row)))})
    return records


def _symbol_column(records: list[dict]) -> Optional[str]:
    """Find the column that holds the tradingsymbol/ticker."""
    if not records:
        return None
    keys = list(records[0].keys())
    for candidate in ("tradingsymbol", "symbol", "ticker", "scrip", "instrument"):
        for key in keys:
            if key and key.lower() == candidate:
                return key
    # Fallback: first column whose header mentions symbol/ticker.
    for key in keys:
        if key and ("symbol" in key.lower() or "ticker" in key.lower()):
            return key
    return keys[0] if keys else None


def resolve_company_symbols(records: list[dict]) -> list[str]:
    """Extract a de-duplicated, ordered list of company symbols from holdings."""
    col = _symbol_column(records)
    if not col:
        return []
    symbols: list[str] = []
    for rec in records:
        val = rec.get(col)
        if val is None:
            continue
        sym = str(val).strip()
        if sym and sym not in symbols:
            symbols.append(sym)
    return symbols


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
def _read_pdf(path: Path) -> str:
    """Best-effort PDF text extraction (needs pypdf)."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        logger.warning("pypdf not installed; skipping PDF %s (pip install pypdf)", path.name)
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read PDF %s: %s", path, exc)
        return ""


def _read_document(path: Path) -> str:
    """Read one document file into text (empty on failure/unsupported)."""
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXTS:
            return path.read_text(encoding="utf-8", errors="replace")
        if ext == ".pdf":
            return _read_pdf(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read %s: %s", path, exc)
    return ""


def collect_documents_for(symbol: str, docs_dir: str) -> list[dict]:
    """
    Return documents relevant to a company as [{'name': ..., 'text': ...}].

    Sources (both applied):
      * all files directly under docs_dir (global documents), and
      * all files under docs_dir/<symbol> (company-specific documents).
    """
    result: list[dict] = []
    base = Path(docs_dir)
    if not base.is_dir():
        return result

    candidates: list[Path] = []
    # Global documents (files directly in docs_dir).
    candidates.extend(p for p in sorted(base.iterdir()) if p.is_file())

    # Per-company folder (case-insensitive match on the folder name).
    for child in base.iterdir():
        if child.is_dir() and child.name.lower() == symbol.lower():
            candidates.extend(p for p in sorted(child.rglob("*")) if p.is_file())
            break

    candidates = [p for p in candidates if p.name.lower() not in IGNORED_DOC_NAMES]

    # Prefer an extracted .md over its source .pdf: if X.md and X.pdf both
    # exist, skip X.pdf so we don't feed the same content twice.
    md_stems = {p.stem for p in candidates if p.suffix.lower() in TEXT_EXTS}
    filtered: list[Path] = []
    for p in candidates:
        if p.suffix.lower() == ".pdf" and p.stem in md_stems:
            logger.debug("Skipping %s (extracted %s.md already present)", p.name, p.stem)
            continue
        filtered.append(p)

    for path in filtered:
        text = _read_document(path)
        if text.strip():
            result.append({"name": path.name, "text": text})
    return result


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
def load_prompt_template(prompt_file: str) -> str:
    """Read the quarterly analysis prompt template."""
    try:
        return Path(prompt_file).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Prompt template not found: {prompt_file}. "
            f"Expected something like prompts/QUARTERLYANALYSIS.md."
        )


def build_company_prompt(
    template: str,
    company: str,
    quarter: str,
    documents: list[dict],
) -> str:
    """
    Fill the template for a company and prepend the relevant source documents.

    `quarter` fills [Q_ 20__]; company fills [COMPANY NAME]. Any remaining
    placeholders are also substituted generically.
    """
    filled = (
        template
        .replace(PLACEHOLDER_COMPANY, company)
        .replace(PLACEHOLDER_QUARTER, quarter or "[Q_ 20__]")
        .replace("[Q_ 20__]", quarter or "[Q_ 20__]")
    )

    if documents:
        doc_block = "\n\n".join(
            f"===== DOCUMENT: {d['name']} =====\n{d['text']}" for d in documents
        )
    else:
        doc_block = (
            "(No source documents were provided for this company. "
            "Per the rules above, state 'Not found in uploaded documents.' for "
            "any claim you cannot support with a quote.)"
        )

    return (
        "SOURCE DOCUMENTS (quote from these; cite the document name):\n"
        f"{doc_block}\n\n"
        "------------------------------------------------------------\n"
        f"{filled}"
    )


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
def _build_llm() -> ChatOpenAI:
    """DeepSeek exposes an OpenAI-compatible API, so we reuse ChatOpenAI."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("Missing DEEPSEEK_API_KEY environment variable.")
    return ChatOpenAI(
        model=DEEPSEEK_MODEL,
        base_url=DEEPSEEK_BASE_URL,
        api_key=api_key,
        temperature=0,
    )


def _analyse_company_blocking(prompt: str) -> str:
    """Run a single synchronous LLM call for one company."""
    llm = _build_llm()
    resp = llm.invoke(prompt)
    return getattr(resp, "content", str(resp))


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
def plan_companies(state: QuarterlyState) -> QuarterlyState:
    """
    Node: resolve the list of companies to analyse.

    Uses state['companies'] if provided; otherwise reads them from the Excel
    portfolio file. Also loads any per-company source documents.
    """
    try:
        companies = state.get("companies") or []
        if not companies:
            excel_path = state.get("excel_path") or DEFAULT_EXCEL
            records = read_holdings_from_excel(excel_path)
            companies = resolve_company_symbols(records)
            logger.info("Resolved %d companies from %s", len(companies), excel_path)

        limit = state.get("limit") or 0
        if limit and len(companies) > limit:
            companies = companies[:limit]

        docs_dir = state.get("docs_dir") or DEFAULT_DOCS_DIR
        company_documents = {c: collect_documents_for(c, docs_dir) for c in companies}
        total_docs = sum(len(v) for v in company_documents.values())
        logger.info("Loaded %d document(s) across %d companies from %s",
                    total_docs, len(companies), docs_dir)

        return {**state, "company_symbols": companies,
                "company_documents": company_documents, "error": None}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to plan companies")
        return {**state, "company_symbols": [], "error": str(exc)}


def analyse_quarterly_results(state: QuarterlyState) -> QuarterlyState:
    """
    Node: run the quarterly analysis prompt for each company.

    Populates state['analyses'] with one entry per company. LLM calls are
    blocking, so we offload them to a worker thread.
    """
    companies = state.get("company_symbols") or []
    if not companies:
        return {**state, "analyses": [], "error": state.get("error") or
                "No companies to analyse."}

    try:
        template = load_prompt_template(state.get("prompt_file") or DEFAULT_PROMPT_FILE)
    except FileNotFoundError as exc:
        return {**state, "analyses": [], "error": str(exc)}

    quarter = state.get("quarter") or ""
    company_documents = state.get("company_documents") or {}
    analyses: list[dict] = []

    # Single event loop; offload the blocking LLM call per company.
    async def _run_all() -> list[dict]:
        results = []
        for symbol in companies:
            docs = company_documents.get(symbol, [])
            prompt = build_company_prompt(template, symbol, quarter, docs)
            logger.info("Analysing %s (%d document(s))...", symbol, len(docs))
            try:
                text = await asyncio.to_thread(_analyse_company_blocking, prompt)
                results.append({"company": symbol, "quarter": quarter,
                                "analysis": text, "error": None})
            except Exception as exc:  # noqa: BLE001
                logger.exception("Analysis failed for %s", symbol)
                results.append({"company": symbol, "quarter": quarter,
                                "analysis": "", "error": str(exc)})
        return results

    analyses = asyncio.run(_run_all())
    return {**state, "analyses": analyses, "error": None}


def write_analysis_report(state: QuarterlyState) -> QuarterlyState:
    """
    Node: assemble the combined Markdown report from the analyses.

    The report is also written to DEFAULT_OUT_MD when no explicit path is set
    via state['report_md'] being a path.
    """
    analyses = state.get("analyses") or []
    quarter = state.get("quarter") or "latest quarter"

    parts = [f"# Quarterly Results Analysis — {quarter}\n"]
    for item in analyses:
        company = item.get("company", "UNKNOWN")
        parts.append(f"\n\n---\n\n## {company}\n")
        if item.get("error"):
            parts.append(f"> **Analysis failed:** {item['error']}\n")
        else:
            parts.append(item.get("analysis", "").strip() + "\n")

    report_md = "".join(parts)
    return {**state, "report_md": report_md}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
def build_graph():
    """LangGraph: plan companies -> analyse quarterly -> write report."""
    from langgraph.graph import StateGraph, START, END

    graph = StateGraph(QuarterlyState)
    graph.add_node("plan_companies", plan_companies)
    graph.add_node("analyse_quarterly_results", analyse_quarterly_results)
    graph.add_node("write_analysis_report", write_analysis_report)
    graph.add_edge(START, "plan_companies")
    graph.add_edge("plan_companies", "analyse_quarterly_results")
    graph.add_edge("analyse_quarterly_results", "write_analysis_report")
    graph.add_edge("write_analysis_report", END)
    return graph.compile()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Analyse quarterly results for portfolio companies.")
    parser.add_argument("--excel", default=DEFAULT_EXCEL,
                        help=f"Portfolio workbook (default: {DEFAULT_EXCEL}).")
    parser.add_argument("--prompt-file", default=DEFAULT_PROMPT_FILE,
                        help=f"Prompt template (default: {DEFAULT_PROMPT_FILE}).")
    parser.add_argument("--docs-dir", default=DEFAULT_DOCS_DIR,
                        help=f"Source documents dir (default: {DEFAULT_DOCS_DIR}).")
    parser.add_argument("--quarter", default="", help='e.g. "Q2 2026".')
    parser.add_argument("--companies", default="",
                        help="Comma-separated symbols (overrides Excel).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max companies to analyse (0 = all).")
    parser.add_argument("--out-md", default=DEFAULT_OUT_MD, help="Markdown output path.")
    parser.add_argument("--out-json", default=DEFAULT_OUT_JSON, help="JSON output path.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    companies = [c.strip() for c in args.companies.split(",") if c.strip()]
    init: QuarterlyState = {
        "excel_path": args.excel,
        "prompt_file": args.prompt_file,
        "docs_dir": args.docs_dir,
        "quarter": args.quarter,
        "companies": companies,
        "limit": args.limit,
    }

    app = build_graph()
    out = app.invoke(init)

    if out.get("error") and not out.get("analyses"):
        print(f"Error: {out['error']}")
        return 1

    # Write outputs.
    report_md = out.get("report_md", "")
    Path(args.out_md).write_text(report_md, encoding="utf-8")
    Path(args.out_json).write_text(
        json.dumps({
            "quarter": out.get("quarter"),
            "companies": out.get("company_symbols"),
            "analyses": out.get("analyses"),
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Analysed {len(out.get('analyses', []))} company(ies).")
    print(f"Wrote {args.out_md}")
    print(f"Wrote {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
