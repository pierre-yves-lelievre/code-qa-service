"""MCP server: the service's ask and repo_info as two tools over stdio.

A thin HTTP client, so no key, database or provider is touched here: every call goes to the
running service at `settings.service_url` (`SERVICE_URL`), which must be up (`make run`).
"""

from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from app.config import settings

TIMEOUT = 120.0  # a real answer takes 10-20 s: one planner call, retrieval, one answer call

# The fields of a source worth handing to an assistant; the excerpt stays in the web UI.
SOURCE_FIELDS = (
    "path",
    "start_line",
    "end_line",
    "kind",
    "qualname",
    "tier",
    "cited",
    "github_url",
)

mcp = FastMCP("code-qa")
http = httpx.Client(base_url=settings.service_url, timeout=TIMEOUT)


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool
def ask(repo: int, question: str, conversation_id: str | None = None) -> dict[str, Any]:
    """Ask a question about an indexed repository; the answer cites the code it used.

    `repo` is the indexed repository's id, the `?repo=` of the web UI or `repo_id` from a search.
    Pass the returned `conversation_id` back to ask a follow-up in the same conversation.
    """
    payload: dict[str, Any] = {"repo_id": repo, "question": question}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    data = _call("POST", "/ask", json=payload)
    return {
        "answer": data["answer"],
        "not_found": data["not_found"],
        "conversation_id": data["conversation_id"],
        "sources": [
            {field: source[field] for field in SOURCE_FIELDS} for source in data["sources"]
        ],
        "notes": data["notes"],
    }


@mcp.tool
def repo_info(repo: int) -> dict[str, Any]:
    """What is indexed for a repository: its snapshot, summary and suggested questions."""
    data = _call("GET", f"/repos/{repo}")
    return {
        "owner": data["owner"],
        "name": data["name"],
        "url": data["url"],
        "summary": data["summary"],
        "suggested_questions": data["suggested_questions"],
        "snapshot": data["snapshot"],
    }


# ── Transport ─────────────────────────────────────────────────────────────────


def _call(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """One request to the service; its error contract becomes a ToolError."""
    try:
        response = http.request(method, path, **kwargs)
    except httpx.RequestError:
        raise ToolError(
            f"The service is unreachable at {settings.service_url}; start it with `make run`."
        ) from None
    if response.is_error:
        raise ToolError(_error(response))
    return response.json()


def _error(response: httpx.Response) -> str:
    """The service's `{detail, code}` as one line; any other body as its status."""
    try:
        body = response.json()
        return f"{body['detail']} ({body['code']})"
    except (ValueError, KeyError, TypeError):
        return f"The service answered HTTP {response.status_code}."


def main() -> None:
    """Serve the two tools over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
