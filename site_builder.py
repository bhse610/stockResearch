"""
Build a static website (GitHub Pages ready) from quarterly_analysis.json.

Outputs a self-contained `output/` folder:
    output/
        index.html            # landing page: all companies + summary table
        <SYMBOL>.html         # one page per company (full analysis)
        assets/style.css      # single stylesheet (responsive, light/dark)
        .nojekyll             # tells GitHub Pages to serve files as-is
        data/quarterly_analysis.json   # copy of the source data (for reuse)

The HTML uses no JavaScript frameworks and no external CDNs, so it works on
plain GitHub Pages (project or user site).

Usage:
    python site_builder.py
    python site_builder.py --in quarterly_analysis.json --out output
    python site_builder.py --title "My Portfolio — Q1 FY27"
    python site_builder.py --site-url "https://user.github.io/repo"
"""

import os
import re
import sys
import json
import html
import shutil
import argparse
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_IN = "quarterly_analysis.json"
DEFAULT_OUT = "site"
DEFAULT_TITLE = "Quarterly Results Analysis"


# ---------------------------------------------------------------------------
# Markdown -> HTML
# ---------------------------------------------------------------------------
def md_to_html(md_text: str) -> str:
    """Convert Markdown to HTML (tables + fenced code + sane defaults)."""
    try:
        import markdown  # type: ignore
    except ImportError:
        raise RuntimeError("The 'markdown' package is required. Install it: pip install markdown")

    return markdown.markdown(
        md_text or "",
        extensions=["tables", "fenced_code", "sane_lists", "toc", "attr_list"],
    )


def slugify(text: str) -> str:
    """Make a safe HTML id / filename slug from arbitrary text."""
    s = re.sub(r"[^\w\-]+", "-", (text or "").strip().lower())
    return re.sub(r"-{2,}", "-", s).strip("-") or "item"


def _first_paragraph(md_text: str, limit: int = 240) -> str:
    """Extract a short plain-text summary from an analysis (for previews)."""
    text = (md_text or "").strip()
    if not text:
        return ""
    # Drop the leading H1 line(s) and headings.
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    body = "\n".join(lines).strip()
    # First non-empty paragraph.
    para = ""
    for chunk in re.split(r"\n\s*\n", body):
        chunk = chunk.strip()
        if chunk and not chunk.startswith(">"):
            para = chunk
            break
    # Strip markdown emphasis/links for a clean snippet.
    para = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", para)
    para = re.sub(r"[*_`>#]", "", para).strip()
    para = re.sub(r"\s+", " ", para)
    return (para[:limit] + "…") if len(para) > limit else para


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="stylesheet" href="{root}assets/style.css">
</head>
<body>
<header class="site-header">
  <div class="wrap">
    <a class="brand" href="{root}index.html">{brand}</a>
    <nav class="nav">{nav}</nav>
  </div>
</header>
<main class="wrap">
{content}
</main>
<footer class="site-footer">
  <div class="wrap">
    <p>Generated {generated} &middot; {footer_note}</p>
  </div>
</footer>
</body>
</html>
"""

_INDEX_CONTENT = """<h1>{title}</h1>
<p class="lede">{lede}</p>
{stats}
<table class="companies">
<thead><tr><th>Company</th><th>Status</th><th>Summary</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
"""

_COMPANY_CONTENT = """<nav class="crumbs"><a href="{root}index.html">← All companies</a></nav>
<h1>{company}</h1>
<p class="meta">{quarter_meta}</p>
{badge}
<article class="analysis">
{body}
</article>
"""


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
def _render_index(analyses: list[dict], title: str, quarter: str) -> str:
    rows = []
    ok = 0
    failed = 0
    for item in analyses:
        company = item.get("company", "UNKNOWN")
        slug = slugify(company) + ".html"
        err = item.get("error")
        if err:
            failed += 1
            status = '<span class="pill pill-err">Failed</span>'
            summary = html.escape(err[:200])
        else:
            ok += 1
            status = '<span class="pill pill-ok">Analysed</span>'
            summary = html.escape(_first_paragraph(item.get("analysis", "")))
        rows.append(
            f'<tr><td><a href="{slug}">{html.escape(company)}</a></td>'
            f"<td>{status}</td><td class=\"snippet\">{summary}</td></tr>"
        )

    stats = (
        '<div class="stats">'
        f'<div class="stat"><span class="num">{len(analyses)}</span><span class="lab">Companies</span></div>'
        f'<div class="stat"><span class="num">{ok}</span><span class="lab">Analysed</span></div>'
        f'<div class="stat"><span class="num">{failed}</span><span class="lab">Failed</span></div>'
        "</div>"
    )
    lede = f"Quarterly results analysis for held companies — {html.escape(quarter)}." if quarter \
        else "Quarterly results analysis for held companies."
    return _INDEX_CONTENT.format(
        title=html.escape(title),
        lede=lede,
        stats=stats,
        rows="\n".join(rows) or '<tr><td colspan="3">No companies.</td></tr>',
    )


def _render_company(item: dict, root: str) -> str:
    company = item.get("company", "UNKNOWN")
    quarter = item.get("quarter") or ""
    err = item.get("error")
    if err:
        badge = f'<p class="alert">Analysis failed: {html.escape(err)}</p>'
        body = "<p>No analysis content.</p>"
    else:
        badge = ""
        # Render the company analysis; strip a duplicated leading H1 if present.
        text = item.get("analysis", "").strip()
        text = re.sub(r"^#\s+.*\n+", "", text, count=1)
        body = md_to_html(text)
    meta = f"Quarter: {html.escape(quarter)}" if quarter else "Quarter: (unspecified)"
    return _COMPANY_CONTENT.format(
        root=root,
        company=html.escape(company),
        quarter_meta=meta,
        badge=badge,
        body=body,
    )


def build_site(
    in_path: str,
    out_dir: str,
    title: str = DEFAULT_TITLE,
    site_url: str = "",
    clean: bool = True,
) -> str:
    """Build the static site and return the output directory."""
    if not os.path.exists(in_path):
        raise FileNotFoundError(f"Input JSON not found: {in_path}")

    with open(in_path, encoding="utf-8") as fh:
        data = json.load(fh)

    analyses = data.get("analyses") or []
    quarter = data.get("quarter") or ""

    if os.path.isdir(out_dir) and clean:
        shutil.rmtree(out_dir)
    os.makedirs(os.path.join(out_dir, "assets"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    footer_note = "Review for informational purposes only — not investment advice."

    # Company pages.
    nav_links = ["<a href=\"index.html\">Home</a>"]
    for item in analyses:
        company = item.get("company", "UNKNOWN")
        slug = slugify(company) + ".html"
        page_html = _PAGE.format(
            title=f"{company} — {title}",
            description=_first_paragraph(item.get("analysis", ""), 160),
            root="",
            brand=html.escape(title),
            nav=" ".join([f'<a href="{slug}">{html.escape(company)}</a>' for item in analyses]),
            content=_render_company(item, root=""),
            generated=generated,
            footer_note=footer_note,
        )
        with open(os.path.join(out_dir, slug), "w", encoding="utf-8") as fh:
            fh.write(page_html)
        nav_links.append(f'<a href="{slug}">{html.escape(company)}</a>')

    # Index page.
    index_html = _PAGE.format(
        title=title,
        description="Quarterly results analysis for portfolio companies.",
        root="",
        brand=html.escape(title),
        nav=" ".join(nav_links) if len(nav_links) <= 8 else "<a href=\"index.html\">Home</a>",
        content=_render_index(analyses, title, quarter),
        generated=generated,
        footer_note=footer_note,
    )
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(index_html)

    # Stylesheet.
    with open(os.path.join(out_dir, "assets", "style.css"), "w", encoding="utf-8") as fh:
        fh.write(CSS)

    # Copy source data for reuse / download.
    shutil.copyfile(in_path, os.path.join(out_dir, "data", os.path.basename(in_path)))

    # .nojekyll so GitHub Pages serves files verbatim (no Jekyll processing).
    with open(os.path.join(out_dir, ".nojekyll"), "w", encoding="utf-8") as fh:
        fh.write("")

    logger.info("Built site: %d page(s) + index in %s", len(analyses), out_dir)
    return out_dir


# ---------------------------------------------------------------------------
# Stylesheet (single file, no external deps, light/dark aware)
# ---------------------------------------------------------------------------
CSS = """\
:root {
  --bg: #ffffff; --fg: #1c1e21; --muted: #5c6470; --card: #f6f8fa;
  --border: #e2e6ea; --accent: #1f6feb; --ok: #1a7f37; --err: #cf222e;
  --quote-bg: #f0f4ff; --quote-border: #1f6feb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --card: #161b22;
    --border: #30363d; --accent: #58a6ff; --ok: #3fb950; --err: #f85149;
    --quote-bg: #161b22; --quote-border: #58a6ff;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
.wrap { max-width: 900px; margin: 0 auto; padding: 0 20px; }
.site-header { border-bottom: 1px solid var(--border); background: var(--bg); position: sticky; top: 0; z-index: 5; }
.site-header .wrap { display: flex; align-items: center; gap: 16px; height: 58px; flex-wrap: wrap; }
.brand { font-weight: 700; color: var(--fg); text-decoration: none; font-size: 17px; }
.nav { display: flex; gap: 12px; flex-wrap: wrap; font-size: 14px; }
.nav a { color: var(--muted); text-decoration: none; }
.nav a:hover { color: var(--accent); }
main { padding: 28px 20px 60px; }
h1 { font-size: 28px; line-height: 1.25; margin: 8px 0 12px; }
h2 { font-size: 22px; margin-top: 34px; padding-top: 14px; border-top: 1px solid var(--border); }
h3 { font-size: 18px; margin-top: 24px; }
.lede { color: var(--muted); max-width: 70ch; }
a { color: var(--accent); }
.stats { display: flex; gap: 14px; flex-wrap: wrap; margin: 20px 0 8px; }
.stat { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px 18px; min-width: 110px; }
.stat .num { display: block; font-size: 24px; font-weight: 700; }
.stat .lab { color: var(--muted); font-size: 13px; }
table.companies { width: 100%; border-collapse: collapse; margin-top: 18px; }
table.companies th, table.companies td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
table.companies th { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.snippet { color: var(--muted); font-size: 14px; max-width: 60ch; }
.pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.pill-ok { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }
.pill-err { background: color-mix(in srgb, var(--err) 16%, transparent); color: var(--err); }
.meta { color: var(--muted); font-size: 14px; margin-top: 0; }
.crumbs { margin-bottom: 6px; font-size: 14px; }
.alert { background: color-mix(in srgb, var(--err) 12%, transparent); border: 1px solid var(--err); color: var(--err); padding: 10px 14px; border-radius: 8px; }
.analysis blockquote {
  margin: 14px 0; padding: 10px 16px; background: var(--quote-bg);
  border-left: 4px solid var(--quote-border); border-radius: 0 8px 8px 0; color: var(--fg);
}
.analysis table { width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 15px; }
.analysis th, .analysis td { border: 1px solid var(--border); padding: 8px 10px; text-align: left; }
.analysis th { background: var(--card); }
.analysis code { background: var(--card); padding: 1px 5px; border-radius: 5px; font-size: 90%; }
.analysis pre { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px; overflow: auto; }
.analysis hr { border: none; border-top: 1px solid var(--border); margin: 28px 0; }
.site-footer { border-top: 1px solid var(--border); color: var(--muted); font-size: 13px; }
.site-footer .wrap { padding-top: 16px; padding-bottom: 16px; }
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a static (GitHub Pages) website from the analysis JSON.")
    parser.add_argument("--in", dest="in_path", default=DEFAULT_IN,
                        help=f"Input analysis JSON (default: {DEFAULT_IN}).")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help=f"Output directory (default: {DEFAULT_OUT}).")
    parser.add_argument("--title", default=DEFAULT_TITLE, help="Site title.")
    parser.add_argument("--site-url", default="",
                        help="Base URL (informational; for your records).")
    parser.add_argument("--no-clean", action="store_true",
                        help="Do not wipe the output dir before building.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")

    try:
        out = build_site(
            args.in_path, args.out,
            title=args.title, site_url=args.site_url, clean=not args.no_clean,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}")
        return 1

    print(f"Built site in: {out}")
    print(f"  - {os.path.join(out, 'index.html')}")
    print(f"  - assets/style.css, .nojekyll, data/")
    if args.site_url:
        print(f"Preview at: {args.site_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
