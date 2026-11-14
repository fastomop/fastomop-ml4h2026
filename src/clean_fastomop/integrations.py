"""The two MCP server integrations, each a thin builder returning an agno `MCPTools`.

(Deliberately not named `mcp` — that's the official MCP SDK package; a local module by
the same name would be a confusing shadow.)

Both servers run over stdio. The returned object is an async context manager:

    async with connect_concept_omcp(cfg) as concept_tools, connect_omcp(cfg) as omcp_tools:
        ...

  - omcp         : local clinical DB over stdio (Select_Query, Get_Information_Schema).
  - concept-omcp : local semantic vocab search over stdio (search_concepts, ...).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from agno.tools.mcp import MCPTools
from mcp import StdioServerParameters


def _stdio_params(
    command: str, args: list[str], env: Dict[str, str] | None = None, cwd: str | None = None
) -> StdioServerParameters:
    # Merge onto the parent environment so the child can find `uv`/`python` on PATH.
    merged = {**os.environ, **(env or {})}
    return StdioServerParameters(command=command, args=list(args), env=merged, cwd=cwd)


def connect_omcp(cfg: Dict[str, Any]) -> MCPTools:
    """Build MCPTools for the local omcp SQL server (database agent).

    cfg keys: command, args (list), cwd, timeout_seconds. The server is launched as a
    local subprocess over stdio; its env (DB_TYPE/DB_PATH/CDM_SCHEMA/VOCAB_SCHEMA) is
    inherited from this process environment (see _stdio_params).
    """
    params = _stdio_params(cfg["command"], cfg.get("args", []), cwd=cfg.get("cwd"))
    return MCPTools(server_params=params, timeout_seconds=cfg.get("timeout_seconds", 660))


def connect_concept_omcp(cfg: Dict[str, Any]) -> MCPTools:
    """Build MCPTools for the local concept-omcp semantic-search server (semantic agent).

    cfg keys: command, args (list), artifact_dir, timeout_seconds. `artifact_dir` is
    passed as CONCEPT_OMCP_ARTIFACT_DIR so `serve` finds the prebuilt index.
    """
    env = {}
    artifact_dir = cfg.get("artifact_dir") or os.getenv("CONCEPT_OMCP_ARTIFACT_DIR")
    if artifact_dir:
        env["CONCEPT_OMCP_ARTIFACT_DIR"] = artifact_dir
    params = _stdio_params(cfg["command"], cfg.get("args", []), env=env)
    return MCPTools(server_params=params, timeout_seconds=cfg.get("timeout_seconds", 120))


# Prefixes omcp uses for failed Select_Query responses (see omcp main.py read_query).
_OMCP_ERROR_PREFIXES = ("Failed to execute query", "Query validation failed")


def _looks_like_omcp_error(text: str) -> bool:
    return text.strip().startswith(_OMCP_ERROR_PREFIXES)


def unwrap_mcp_result_text(raw: Any) -> "tuple[str, Optional[bool]]":
    """Peel omcp's double-wrapped tool result down to its inner text + error flag.

    omcp's `read_query` returns a `CallToolResult`; the MCP framework wraps that again
    and JSON-serialises the inner result into the outer text. So a raw result is often
    `{"content": [{"text": "<CSV-or-error>"}], "isError": <bool>}`. Returns the inner
    text (the CSV or error message) and the inner `isError` flag (None if not wrapped).
    """
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        raw = str(raw)
    s = raw.strip()
    if s[:1] in ("{", "["):
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            return raw, None
        if isinstance(obj, dict) and isinstance(obj.get("content"), list):
            text = "".join(b.get("text", "") for b in obj["content"] if isinstance(b, dict))
            return text, obj.get("isError")
    return raw, None


def result_is_error(raw: Any) -> bool:
    """Did an omcp Select_Query result represent a failure?

    Reads the INNER `isError` from the double-wrap (the outer/agno flag is unreliable),
    falling back to omcp's error-text prefixes. Use this instead of trusting agno's
    `tool_call_error`, which reflects the always-false outer wrapper.
    """
    text, inner_error = unwrap_mcp_result_text(raw)
    if inner_error is not None:
        return bool(inner_error)
    return _looks_like_omcp_error(text)
