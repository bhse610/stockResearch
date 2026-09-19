"""
One-shot runner for the whole pipeline (sequential, in-process).

Runs, in order:
    1. extract_pdfs            -> PDFs in documents/ become .md text
    2. portfolio_to_excel      -> fetch Kite portfolio into kite_portfolio.xlsx
                                  (skipped automatically if the file already exists)
    3. quarterly_analysis_agent-> analyse each holding's quarterly results
    4. site_builder (optional) -> static GitHub-Pages website in site/

Each step calls the module's existing `main()` (no subprocesses), so all the
existing flags, guards and behaviours are reused. The pipeline stops early if a
step returns a non-zero exit code, unless --keep-going is passed.

Usage:
    python run_all.py
    python run_all.py --quarter "Q1 FY27"
    python run_all.py --quarter "Q1 FY27" --limit 2
    python run_all.py --quarter "Q1 FY27" --build-site        # + GitHub Pages site
    python run_all.py --refresh                 # re-fetch portfolio from Kite
    python run_all.py --skip-extract            # skip the PDF step
    python run_all.py --keep-going              # continue past a failing step
    python run_all.py --no-portfolio            # assume Excel already exists
"""

import os
import sys
import time
import argparse
import logging

logger = logging.getLogger("run_all")

# Make sure emoji/unicode in child output never crashes the console.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def step_extract_pdfs(docs_dir: str, force: bool) -> int:
    """Step 1: extract PDFs under docs_dir into sibling .md files."""
    import extract_pdfs

    argv = ["--root", docs_dir]
    if force:
        argv.append("--force")
    return extract_pdfs.main(argv)


def step_portfolio_to_excel(
    excel_path: str,
    refresh: bool,
    from_json: str | None,
    save_json: str | None,
) -> int:
    """Step 2: fetch the Kite portfolio into an Excel workbook."""
    import portfolio_to_excel

    argv = ["--out", excel_path]
    if refresh:
        argv.append("--refresh")
    if from_json:
        argv += ["--from-json", from_json]
    if save_json:
        argv += ["--save-json", save_json]
    return portfolio_to_excel.main(argv)


def step_quarterly_analysis(
    excel_path: str,
    quarter: str,
    docs_dir: str,
    companies: str,
    limit: int,
    prompt_file: str | None,
    out_md: str,
    out_json: str,
) -> int:
    """Step 3: analyse quarterly results for the holdings."""
    import quarterly_analysis_agent

    argv = ["--excel", excel_path, "--docs-dir", docs_dir,
            "--out-md", out_md, "--out-json", out_json]
    if quarter:
        argv += ["--quarter", quarter]
    if companies:
        argv += ["--companies", companies]
    if limit:
        argv += ["--limit", str(limit)]
    if prompt_file:
        argv += ["--prompt-file", prompt_file]
    return quarterly_analysis_agent.main(argv)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _run_step(index: int, total: int, name: str, fn, keep_going: bool) -> int:
    """
    Run one step, log timing, and return its exit code.

    The caller decides whether to continue based on the code and --keep-going.
    """
    print(f"\n{'=' * 70}")
    print(f"STEP {index}/{total}: {name}")
    print("=" * 70)
    start = time.time()
    try:
        code = fn()
    except Exception as exc:  # noqa: BLE001 - report and honour keep_going
        logger.exception("Step %r raised: %s", name, exc)
        code = 1
    elapsed = time.time() - start

    status = "OK" if code == 0 else f"FAILED (exit {code})"
    print(f"\n[step {index}/{total}] {name}: {status} in {elapsed:.1f}s")

    if code != 0 and not keep_going:
        print(f"\nAborting: step {index} ({name}) failed. "
              f"Use --keep-going to continue anyway.")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the full pipeline: PDFs -> Excel -> quarterly analysis.")
    parser.add_argument("--docs-dir", default="documents",
                        help="Documents root (default: documents).")
    parser.add_argument("--excel", default="kite_portfolio.xlsx",
                        help="Portfolio workbook (default: kite_portfolio.xlsx).")
    parser.add_argument("--quarter", default="",
                        help='Quarter label, e.g. "Q1 FY27".')
    parser.add_argument("--companies", default="",
                        help="Comma-separated symbols (overrides Excel).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max companies to analyse (0 = all).")
    parser.add_argument("--prompt-file", default="",
                        help="Prompt template (default: prompts/QUARTERLYANALYSIS.md).")
    parser.add_argument("--out-md", default="quarterly_analysis.md",
                        help="Analysis Markdown output.")
    parser.add_argument("--out-json", default="quarterly_analysis.json",
                        help="Analysis JSON output.")

    parser.add_argument("--refresh", action="store_true",
                        help="Re-fetch the portfolio from Kite even if the Excel exists.")
    parser.add_argument("--from-json", default="",
                        help="Build the Excel from a saved JSON instead of fetching.")
    parser.add_argument("--save-json", default="",
                        help="Also save the raw portfolio JSON during the fetch step.")
    parser.add_argument("--extract-force", action="store_true",
                        help="Re-extract PDFs even if .md already exists.")

    parser.add_argument("--skip-extract", action="store_true",
                        help="Skip the PDF extraction step.")
    parser.add_argument("--no-portfolio", action="store_true",
                        help="Skip the portfolio fetch step (assume Excel exists).")
    parser.add_argument("--keep-going", action="store_true",
                        help="Continue to the next step even if one fails.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    # Build the step list based on skips.
    steps: list[tuple[str, object]] = []
    if not args.skip_extract:
        steps.append((
            "Extract PDFs -> Markdown",
            lambda: step_extract_pdfs(args.docs_dir, args.extract_force),
        ))
    if not args.no_portfolio:
        steps.append((
            "Fetch portfolio -> Excel",
            lambda: step_portfolio_to_excel(
                args.excel, args.refresh, args.from_json or None, args.save_json or None),
        ))
    steps.append((
        "Analyse quarterly results",
        lambda: step_quarterly_analysis(
            args.excel, args.quarter, args.docs_dir, args.companies, args.limit,
            args.prompt_file or None, args.out_md, args.out_json),
    ))

    total = len(steps)
    pipeline_start = time.time()
    ok_count = 0
    fail_count = 0
    for i, (name, fn) in enumerate(steps, start=1):
        code = _run_step(i, total, name, fn, args.keep_going)
        if code == 0:
            ok_count += 1
        else:
            fail_count += 1
            if not args.keep_going:
                break

    total_elapsed = time.time() - pipeline_start
    print(f"\n{'=' * 70}")
    print(f"PIPELINE DONE: {ok_count} ok, {fail_count} failed "
          f"({ok_count + fail_count}/{total} attempted) in {total_elapsed:.1f}s")
    print(f"Report: {args.out_md}")
    print(f"Data:   {args.out_json}")
    print("=" * 70)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
