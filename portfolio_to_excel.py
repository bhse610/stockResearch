"""
Fetch a Kite portfolio via the Kite MCP server and export it to an Excel
workbook (.xlsx) with separate sheets for holdings, positions, margins,
and a summary.

This reuses the authenticated MCP plumbing from portfolio_agent.py, so the
same login/wait logic applies: it opens the Kite login URL in the browser
and polls until the session is authenticated.

Usage:
    python portfolio_to_excel.py
    python portfolio_to_excel.py --out my_portfolio.xlsx
    python portfolio_to_excel.py --skip-login        # reuse a live session
    python portfolio_to_excel.py --from-json data.json  # offline: no MCP

Requires DEEPSEEK_API_KEY only when an LLM summary is requested; for the
plain export no LLM is used.
"""

import os
import sys
import json
import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from portfolio_agent import (
    _kite_mcp_server,
    _ensure_logged_in,
    _find_tool,
)

logger = logging.getLogger(__name__)

DEFAULT_OUT = "kite_portfolio.xlsx"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
async def _call_tool(tools: list, name: str) -> Any:
    """Invoke an MCP tool by name and return its parsed payload."""
    tool = _find_tool(tools, name)
    if tool is None:
        logger.warning("Tool %r not available; skipping.", name)
        return None
    try:
        raw = await tool.ainvoke({})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Tool %r failed: %s", name, exc)
        return None
    return _parse_tool_result(raw)


def _parse_tool_result(raw: Any) -> Any:
    """
    MCP tools often return a list like:
        [{'type': 'text', 'text': '<json string>', ...}]
    Extract and JSON-decode the text payload when possible.
    """
    text = raw
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, dict) and "text" in first:
            text = first["text"]

    if isinstance(text, str):
        stripped = text.strip()
        # The server reports failures as plain strings, not JSON.
        if stripped.lower().startswith("failed to execute"):
            raise RuntimeError(stripped)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
    return text


async def fetch_portfolio(skip_login: bool = False) -> dict[str, Any]:
    """Connect to Kite MCP, authenticate, and return holdings/positions/margins."""
    client = MultiServerMCPClient(_kite_mcp_server())

    async with client.session("kite") as session:
        tools = await load_mcp_tools(session)
        logger.info("Loaded %d tools from Kite MCP server.", len(tools))

        if not skip_login:
            await _ensure_logged_in(tools)

        data = {
            "holdings": await _call_tool(tools, "get_holdings"),
            "positions": await _call_tool(tools, "get_positions"),
            "margins": await _call_tool(tools, "get_margins"),
            "profile": await _call_tool(tools, "get_profile"),
        }
        return data


# ---------------------------------------------------------------------------
# Excel writing
# ---------------------------------------------------------------------------
def _autosize(ws, min_width: int = 10, max_width: int = 40) -> None:
    """Approximate column auto-width based on cell contents."""
    for col in ws.columns:
        longest = 0
        letter = None
        for cell in col:
            if letter is None and cell.coordinate:
                letter = cell.column_letter
            value = "" if cell.value is None else str(cell.value)
            longest = max(longest, len(value))
        if letter:
            ws.column_dimensions[letter].width = max(min_width, min(max_width, longest + 2))


def _write_table(ws, rows: list[dict], title: str) -> None:
    """Write a list of dict rows to a worksheet with a bold header row."""
    if not rows:
        ws["A1"] = f"{title}: (no data)"
        return

    # Union of keys, preserving first-seen order.
    headers: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in headers:
                headers.append(key)

    from openpyxl.styles import Font, PatternFill

    header_fill = PatternFill("solid", fgColor="1F4E78")
    for col, name in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill

    for r, row in enumerate(rows, start=2):
        for c, name in enumerate(headers, start=1):
            ws.cell(row=r, column=c, value=_cell_value(row.get(name)))

    ws.freeze_panes = "A2"
    _autosize(ws)



def _cell_value(value: Any) -> Any:
    """
    Coerce a value into something openpyxl can write.

    Scalars (str/int/float/bool/None) pass through; anything structured
    (dict/list) is JSON-encoded so nested fields survive in the cell.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)

def _flatten_positions(positions: Any) -> list[dict]:
    """Kite positions come as {'net': [...], 'day': [...]}. Flatten to rows."""
    if positions is None:
        return []
    if isinstance(positions, list):
        return positions
    if isinstance(positions, dict):
        rows = []
        for bucket in ("net", "day"):
            for pos in positions.get(bucket, []) or []:
                row = dict(pos)
                row["bucket"] = bucket
                rows.append(row)
        return rows
    return []


def _flatten_margins(margins: Any) -> list[dict]:
    """Flatten the nested margins structure into segment/available/utilised rows."""
    if not isinstance(margins, dict):
        return []
    rows = []
    for segment, seg in margins.items():
        if not isinstance(seg, dict):
            continue
        row = {"segment": segment, "enabled": seg.get("enabled"), "net": seg.get("net")}
        for k, v in (seg.get("available") or {}).items():
            row[f"available_{k}"] = v
        for k, v in (seg.get("utilised") or {}).items():
            row[f"utilised_{k}"] = v
        rows.append(row)
    return rows


def _build_summary(holdings: list[dict], positions: list[dict], margins: dict) -> list[dict]:
    """Compute a few aggregate metrics as label/value rows."""
    def as_float(x) -> float:
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    inv = sum(as_float(h.get("average_price")) * as_float(h.get("quantity")) for h in holdings)
    cur = sum(as_float(h.get("last_price")) * as_float(h.get("quantity")) for h in holdings)
    pnl = sum(as_float(h.get("pnl")) for h in holdings)
    pos_pnl = sum(as_float(p.get("pnl")) for p in positions)

    equity = (margins or {}).get("equity", {}) if isinstance(margins, dict) else {}
    cash = ((equity.get("available") or {}).get("live_balance")
            if isinstance(equity.get("available"), dict) else None)

    return [
        {"metric": "Generated at", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        {"metric": "Holdings count", "value": len(holdings)},
        {"metric": "Holdings investment", "value": round(inv, 2)},
        {"metric": "Holdings current value", "value": round(cur, 2)},
        {"metric": "Holdings P&L", "value": round(pnl, 2)},
        {"metric": "Positions count", "value": len(positions)},
        {"metric": "Positions P&L", "value": round(pos_pnl, 2)},
        {"metric": "Total P&L", "value": round(pnl + pos_pnl, 2)},
        {"metric": "Available cash (equity)", "value": cash},
    ]


def write_excel(data: dict[str, Any], out_path: str) -> str:
    """Write the portfolio data to an .xlsx workbook and return the path."""
    from openpyxl import Workbook

    holdings = data.get("holdings") or []
    if not isinstance(holdings, list):
        holdings = []
    position_rows = _flatten_positions(data.get("positions"))
    margin_rows = _flatten_margins(data.get("margins"))

    wb = Workbook()

    # Summary sheet first.
    ws_sum = wb.active
    ws_sum.title = "Summary"
    _write_table(ws_sum, _build_summary(holdings, position_rows, data.get("margins")), "Summary")

    _write_table(wb.create_sheet("Holdings"), holdings, "Holdings")
    _write_table(wb.create_sheet("Positions"), position_rows, "Positions")
    _write_table(wb.create_sheet("Margins"), margin_rows, "Margins")

    # Profile (single dict -> key/value rows).
    profile = data.get("profile")
    if isinstance(profile, dict):
        prof_rows = [{"field": k, "value": v} for k, v in profile.items()]
        _write_table(wb.create_sheet("Profile"), prof_rows, "Profile")

    wb.save(out_path)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _load_from_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, dict) and "holdings" in payload:
        return payload
    raise ValueError("JSON file must contain a 'holdings' key (and optionally positions/margins).")


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Export Kite portfolio to Excel.")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output .xlsx path.")
    parser.add_argument("--skip-login", action="store_true",
                        help="Skip the login step (reuse a live session).")
    parser.add_argument("--from-json", help="Use a local JSON file instead of MCP.")
    parser.add_argument("--save-json", help="Also dump the raw portfolio data to this JSON file.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    if args.from_json:
        data = _load_from_json(args.from_json)
    else:
        data = asyncio.run(fetch_portfolio(skip_login=args.skip_login))

    holdings = data.get("holdings") or []
    if not holdings and not data.get("positions"):
        print("Warning: no holdings/positions retrieved. The Excel file will be empty.")
        print("(Did the Kite login complete? Try again and finish the browser login.)")

    if args.save_json:
        with open(args.save_json, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        print(f"Wrote raw data to {args.save_json}")
    path = write_excel(data, args.out)
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
