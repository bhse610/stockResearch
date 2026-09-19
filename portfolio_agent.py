"""
LangGraph node that reads a Kite portfolio via the Kite MCP server,
using DeepSeek as the LLM that drives the MCP tool calls.

Flow:
    LangGraph node
      -> DeepSeek (deepseek-chat, OpenAI-compatible API)
      -> langchain-mcp-adapters (stdio transport)
      -> `npx mcp-remote https://mcp.kite.trade/mcp`
      -> Zerodha Kite Connect (authenticated via OAuth in the browser)

On the first run (or when the session has expired), the node calls the
Kite `login` tool, opens the login URL in your browser, and WAITS for you
to finish authenticating before fetching the portfolio.

Requirements:
    pip install -r requirements.txt
    Node.js / npx available on PATH (ships with Node.js)
    Env var: DEEPSEEK_API_KEY

Windows note:
    `npx` is a `.cmd` shim on Windows, which anyio cannot exec directly.
    `_npx_command()` therefore launches it via `cmd.exe /c`. Node.js must
    be installed and on PATH (restart the terminal after installing).

Login behaviour (env var KITE_SKIP_LOGIN):
    By default the node will invoke the Kite `login` tool and pause until
    you press Enter (after completing the browser login). Set
    KITE_SKIP_LOGIN=1 to skip the interactive prompt (useful once the
    session is cached, or in non-interactive/CI runs).
"""

import os
import sys
import shutil
import asyncio
import logging
from typing import Any, Optional, TypedDict

from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

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
KITE_MCP_URL = os.environ.get("KITE_MCP_URL", "https://mcp.kite.trade/mcp")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# When true, skip the interactive "press Enter after login" pause.
KITE_SKIP_LOGIN = os.environ.get("KITE_SKIP_LOGIN", "").strip().lower() in ("1", "true", "yes")

# How long to wait (seconds) for the user to complete Kite login in the
# browser after the login URL is opened. The code polls get_profile until it
# succeeds. Set to 0 to fall back to "press Enter" behaviour.
KITE_LOGIN_TIMEOUT = int(os.environ.get("KITE_LOGIN_TIMEOUT", "300"))


def _find_npx() -> Optional[str]:
    """
    Locate the `npx` executable.

    Prefers PATH, but falls back to common Node.js install locations so the
    script keeps working even when the current shell's PATH is stale (e.g.
    Node was installed after the shell/IDE was started).
    """
    found = shutil.which("npx") or shutil.which("npx.cmd")
    if found:
        return found

    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs", "npx.cmd"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "nodejs", "npx.cmd"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs", "npx.cmd"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "npx.cmd"),
        "/usr/local/bin/npx",
        "/usr/bin/npx",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _ensure_node_on_path() -> None:
    """
    Make sure the directory containing `node`/`npx` is on PATH for this
    process and any child processes it spawns.

    Needed when Node.js was installed after the shell/IDE started, so the
    inherited PATH is stale and `npx` (which shells out to `node`) fails.
    """
    npx = _find_npx()
    if not npx:
        return
    node_dir = os.path.dirname(npx)

    if not os.path.exists(os.path.join(node_dir, "node.exe")) and sys.platform == "win32":
        # Some installs keep npx.cmd next to node.exe already; if not, skip.
        pass

    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep)
    if node_dir not in parts:
        os.environ["PATH"] = node_dir + os.pathsep + current
        logger.debug("Prepended %s to PATH for MCP subprocess", node_dir)


def _npx_command() -> dict:
    """
    Build a stdio server definition that reliably launches `mcp-remote`
    for the Kite MCP server.

    On Windows, `npx` is a `.cmd` shim and anyio's open_process cannot
    execute `.cmd` files directly, so we invoke it through `cmd.exe /c`.
    We also ensure Node's directory is on PATH so the child process can
    resolve `node` (which `npx` calls internally).
    """
    npx = _find_npx()
    if not npx:
        raise RuntimeError(
            "Could not find 'npx'. Install Node.js LTS (https://nodejs.org), "
            "then restart your terminal/IDE so PATH is refreshed."
        )

    _ensure_node_on_path()

    if sys.platform == "win32":
        command = os.environ.get("COMSPEC", "cmd.exe")
        args = ["/c", npx, "mcp-remote", KITE_MCP_URL]
    else:
        command = npx
        args = ["mcp-remote", KITE_MCP_URL]

    return {"command": command, "args": args, "transport": "stdio"}


def _kite_mcp_server() -> dict:
    """Resolved lazily so importing the module never fails."""
    return {"kite": _npx_command()}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class PortfolioState(TypedDict, total=False):
    """State passed between LangGraph nodes."""
    # Input
    prompt: str                 # what to ask the agent (defaults below)

    # Output
    portfolio: str              # natural-language answer from DeepSeek
    raw_tool_calls: list        # tool calls the agent made (audit trail)
    error: Optional[str]


DEFAULT_PROMPT = (
    "Read my Kite portfolio. The Kite login is ALREADY done for this "
    "session - do NOT call any login tool.\n"
    "Use the available tools to fetch all three of: (1) my holdings, "
    "(2) my net positions, (3) my available margins/funds.\n"
    "Call the tools one after another WITHOUT stopping or asking me "
    "anything. When you have all three, return ONLY a single JSON object "
    "with keys: holdings, positions, margins, and a short 'summary' string. "
    "Do not wrap it in markdown fences."
)


# ---------------------------------------------------------------------------
# Helpers
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


def _find_tool(tools: list, name: str):
    """Find a tool by exact or case-insensitive name."""
    for tool in tools:
        if tool.name == name or tool.name.lower() == name.lower():
            return tool
    return None


async def _verify_login(tools: list) -> bool:
    """
    Make a lightweight authenticated call (get_profile) to confirm the Kite
    session is live after login. Returns True if authenticated.
    """
    profile_tool = _find_tool(tools, "get_profile")
    if profile_tool is None:
        # No way to verify cheaply; assume the caller proceeds.
        return True
    try:
        result = await profile_tool.ainvoke({})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Login verification (get_profile) failed: %s", exc)
        return False

    text = str(result).lower()
    # Failure markers: the Kite MCP server reports auth/session problems, and
    # it also returns a generic "Failed to execute <tool>" when it cannot.
    failure_markers = (
        "failed to execute",
        "not authenticated",
        "unauthor",
        "please log in",
        "invalid token",
        "token exception",
        "forbidden",
    )
    if any(marker in text for marker in failure_markers):
        logger.warning("Login verification suggests NOT authenticated: %s", str(result)[:300])
        return False
    logger.info("Login verified via get_profile.")
    return True


def _open_url(url: str) -> None:
    """Best-effort open of a URL in the default browser."""
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        logger.warning("Could not open browser automatically. Open it manually: %s", url)


async def _ensure_logged_in(tools: list) -> None:
    """
    Call the Kite `login` tool to start the OAuth flow, then wait until the
    user has completed it in the browser.

    Skipped entirely when KITE_SKIP_LOGIN is set.
    """
    if KITE_SKIP_LOGIN:
        logger.info("KITE_SKIP_LOGIN set - skipping interactive login step.")
        return

    login_tool = _find_tool(tools, "login")
    if login_tool is None:
        logger.info("No 'login' tool exposed by the Kite MCP server; continuing.")
        return

    logger.info("Requesting Kite login (OAuth)...")
    try:
        result = await login_tool.ainvoke({})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Kite login tool call raised: %s", exc)
        result = None

    # Try to surface a login URL from the tool result if one is present.
    url = _extract_url(result)
    if url:
        logger.info("Opening Kite login URL in your browser: %s", url)
        _open_url(url)
        print("\n" + "=" * 70)
        print("A browser window has been opened for Kite login.")
        print(f"If it did not open, visit this URL manually:\n{url}")
        print("=" * 70)
    else:
        print("\n" + "=" * 70)
        print("Kite login requested. Complete the login in the opened browser.")
        if result:
            print(f"Login tool response: {str(result)[:500]}")
        print("=" * 70)

        # Wait for the user to finish logging in.
    #
    # Preferred approach: poll get_profile until the session becomes
    # authenticated. This works both interactively and in non-interactive
    # contexts (where a keypress prompt would be skipped instantly).
    if KITE_LOGIN_TIMEOUT > 0:
        print(
            f"\n>>> Complete the Kite login in your browser. "
            f"Waiting up to {KITE_LOGIN_TIMEOUT}s for authentication..."
        )
        deadline = asyncio.get_event_loop().time() + KITE_LOGIN_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            if await _verify_login(tools):
                logger.info("Kite login confirmed.")
                return
            await asyncio.sleep(3)
        logger.warning(
            "Timed out waiting for Kite login after %ss; continuing anyway.",
            KITE_LOGIN_TIMEOUT,
        )
        return

    # Fallback: interactive keypress prompt (when KITE_LOGIN_TIMEOUT=0).
    if not sys.stdin or not sys.stdin.isatty():
        logger.info("stdin is not interactive; not waiting for login.")
        return

    while True:
        try:
            await asyncio.to_thread(
                input,
                "\n>>> Finish logging in to Kite in your browser, "
                "then press Enter to continue... ",
            )
        except (EOFError, KeyboardInterrupt):
            logger.info("No interactive input received; continuing.")
            return

        if await _verify_login(tools):
            return
        print(
            "\n[!] Kite login is not confirmed yet. Complete the login in the "
            "browser, then press Enter again (Ctrl+C to abort)."
        )


def _extract_url(result: Any) -> Optional[str]:
    """Pull an http(s) URL out of a tool result (str / dict / list)."""
    import re
    text = ""
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        text = " ".join(str(v) for v in result.values())
    elif isinstance(result, (list, tuple)):
        text = " ".join(str(v) for v in result)
    else:
        text = str(result)
    match = re.search(r"https?://[^\s\"'<>\\)\]]+", text)
    return match.group(0) if match else None


async def _run_agent(prompt: str) -> dict[str, Any]:
    """
    Connect to the Kite MCP server using a SINGLE persistent session, log in,
    then run the ReAct agent against the same session.

    A persistent session is essential: the Kite MCP server authenticates the
    connection when the `login` tool is called and its URL is completed. If
    later tool calls open a *new* session, they are unauthenticated and
    return "Please log in first using the login tool". Therefore login and
    all data tool calls must share one session.
    """
    from langchain_mcp_adapters.tools import load_mcp_tools

    client = MultiServerMCPClient(_kite_mcp_server())

    async with client.session("kite") as session:
        tools = await load_mcp_tools(session)
        logger.info("Loaded %d tools from Kite MCP server: %s",
                    len(tools), [t.name for t in tools])

        if not tools:
            raise RuntimeError(
                "No tools returned by the Kite MCP server. "
                "Check that `npx mcp-remote` launched."
            )

        # Authenticate on THIS session before asking the LLM to read data.
        await _ensure_logged_in(tools)

        # Hide the login tool from the agent: login is already handled on
        # this session. Re-calling it would start a competing auth flow.
        blocked = {"login"}
        agent_tools = [t for t in tools if t.name.lower() not in blocked]
        logger.info("Agent will use %d tools (excluded: %s)",
                    len(agent_tools), sorted(blocked))

        agent = create_react_agent(_build_llm(), agent_tools)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]})
        return result


def _extract_tool_calls(messages: list) -> list:
    """Collect all tool calls the agent made, for auditing/debugging."""
    calls = []
    for msg in messages:
        for call in getattr(msg, "tool_calls", []) or []:
            calls.append({"name": call.get("name"), "args": call.get("args")})
    return calls


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------
def read_portfolio(state: PortfolioState) -> PortfolioState:
    """
    LangGraph node: ask DeepSeek (via the Kite MCP server) to read the portfolio.

    Populates `portfolio` (final answer) and `raw_tool_calls`.
    On failure, sets `error`.
    """
    prompt = state.get("prompt") or DEFAULT_PROMPT
    try:
        result = asyncio.run(_run_agent(prompt))
        messages = result.get("messages", [])
        final = messages[-1].content if messages else ""
        return {
            **state,
            "portfolio": final,
            "raw_tool_calls": _extract_tool_calls(messages),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - surface any error into state
        logger.exception("Failed to read portfolio via Kite MCP + DeepSeek")
        return {**state, "error": str(exc)}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
def build_graph():
    """Minimal LangGraph that runs the portfolio reader node."""
    from langgraph.graph import StateGraph, START, END

    graph = StateGraph(PortfolioState)
    graph.add_node("read_portfolio", read_portfolio)
    graph.add_edge(START, "read_portfolio")
    graph.add_edge("read_portfolio", END)
    return graph.compile()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = build_graph()
    out = app.invoke({})
    if out.get("error"):
        print(f"Error: {out['error']}")
    else:
        print(out["portfolio"])
