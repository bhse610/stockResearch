"""
LangGraph node that reads a portfolio (holdings, positions, margins)
from the Zerodha Kite Connect API.
"""

import os
import logging
from typing import Any, List, Optional, TypedDict

from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class PortfolioState(TypedDict, total=False):
    """State passed between LangGraph nodes."""
    # Credentials / config (optional; can also come from env vars)
    api_key: Optional[str]
    access_token: Optional[str]

    # Outputs populated by the read_portfolio node
    holdings: List[dict]
    positions: List[dict]
    margins: dict
    portfolio_summary: dict
    error: Optional[str]


# ---------------------------------------------------------------------------
# Kite client helper
# ---------------------------------------------------------------------------
def get_kite_client(state: PortfolioState) -> KiteConnect:
    """
    Build an authenticated KiteConnect client.

    Credentials are resolved in this order:
      1. Values present in the graph state
      2. Environment variables KITE_API_KEY / KITE_ACCESS_TOKEN
    """
    api_key = state.get("api_key") or os.environ.get("KITE_API_KEY")
    access_token = state.get("access_token") or os.environ.get("KITE_ACCESS_TOKEN")

    if not api_key:
        raise ValueError("Missing Kite API key (state['api_key'] or KITE_API_KEY env var).")
    if not access_token:
        raise ValueError(
            "Missing Kite access token (state['access_token'] or KITE_ACCESS_TOKEN env var)."
        )

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------
def read_portfolio(state: PortfolioState) -> PortfolioState:
    """
    LangGraph node: fetch the user's portfolio from Kite Connect.

    Populates `holdings`, `positions`, `margins`, and a derived
    `portfolio_summary` in the state. On failure, sets `error`.
    """
    try:
        kite = get_kite_client(state)

        holdings = kite.holdings()
        positions = kite.positions()          # {"net": [...], "day": [...]}
        margins = kite.margins()

        summary = _build_summary(holdings, positions, margins)

        logger.info(
            "Fetched portfolio: %d holdings, %d net positions",
            len(holdings),
            len(positions.get("net", [])),
        )

        return {
            **state,
            "holdings": holdings,
            "positions": positions,
            "margins": margins,
            "portfolio_summary": summary,
            "error": None,
        }

    except Exception as exc:  # noqa: BLE001 - surface any Kite/network error into state
        logger.exception("Failed to read portfolio from Kite")
        return {**state, "error": str(exc)}


def _build_summary(
    holdings: List[dict],
    positions: dict,
    margins: dict,
) -> dict[str, Any]:
    """Compute a lightweight aggregate summary of the portfolio."""
    holdings_investment = sum(h.get("average_price", 0) * h.get("quantity", 0) for h in holdings)
    holdings_current = sum(h.get("last_price", 0) * h.get("quantity", 0) for h in holdings)
    holdings_pnl = holdings_current - holdings_investment

    net_positions = positions.get("net", [])
    positions_pnl = sum(p.get("pnl", 0) for p in net_positions)

    # Available cash from the equity segment, if present.
    equity = (margins or {}).get("equity", {})
    available_cash = equity.get("available", {}).get("live_balance", 0)

    return {
        "holdings_count": len(holdings),
        "holdings_investment": round(holdings_investment, 2),
        "holdings_current_value": round(holdings_current, 2),
        "holdings_pnl": round(holdings_pnl, 2),
        "positions_count": len(net_positions),
        "positions_pnl": round(positions_pnl, 2),
        "total_pnl": round(holdings_pnl + positions_pnl, 2),
        "available_cash": available_cash,
    }


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
def build_graph():
    """Build a minimal LangGraph that runs the portfolio reader node."""
    from langgraph.graph import StateGraph, START, END

    graph = StateGraph(PortfolioState)
    graph.add_node("read_portfolio", read_portfolio)
    graph.add_edge(START, "read_portfolio")
    graph.add_edge("read_portfolio", END)
    return graph.compile()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = build_graph()
    result = app.invoke({})
    if result.get("error"):
        print(f"Error: {result['error']}")
    else:
        import json
        print(json.dumps(result["portfolio_summary"], indent=2))

