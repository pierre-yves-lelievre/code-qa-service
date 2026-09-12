"""MCP server tests: the tools over FastMCP's in-memory client, with a mocked service."""

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import app.mcp_server as mcp_server

CONVERSATION = "0c2e5d3a-1b4f-4c6d-8e7a-9f0b1c2d3e4f"
ANSWER = {
    "conversation_id": CONVERSATION,
    "query_id": 7,
    "answer": "The rate is in shop/util.py.",
    "not_found": False,
    "sources": [
        {
            "path": "shop/util.py",
            "start_line": 3,
            "end_line": 9,
            "kind": "function",
            "qualname": "shop.util.tax_rate",
            "tier": "symbol",
            "excerpt": "def tax_rate(): ...",
            "github_url": "https://github.com/pallets/markupsafe/blob/aaa/shop/util.py#L3-L9",
            "cited": True,
        }
    ],
    "retrievers": {},
    "timings": {},
    "tokens": {},
    "snapshot": {},
    "notes": [],
}
REPOS = {
    "items": [
        {
            "repo_id": 4,
            "owner": "pallets",
            "name": "markupsafe",
            "url": "https://github.com/pallets/markupsafe",
            "commit_sha": "b" * 40,
            "indexed_at": "2026-09-12T10:00:00Z",
        }
    ]
}
REPO = {
    "repo_id": 4,
    "owner": "pallets",
    "name": "markupsafe",
    "url": "https://github.com/pallets/markupsafe",
    "summary": "Escapes text for HTML.",
    "suggested_questions": ["What does escape() do?"],
    "snapshot": {"commit_sha": "b" * 40, "indexed_at": "2026-09-12T10:00:00Z"},
}
NOT_FOUND = {"detail": "Repository not found.", "code": "repo_not_found"}


def _service(seen: list[httpx.Request]) -> httpx.Client:
    """A client whose requests never leave the test; every request is recorded."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/ask":
            return httpx.Response(200, json=ANSWER)
        if request.url.path == "/repos":
            return httpx.Response(200, json=REPOS)
        if request.url.path == "/repos/4":
            return httpx.Response(200, json=REPO)
        return httpx.Response(404, json=NOT_FOUND)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://service")


def _tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call one tool through the in-memory client."""

    async def run() -> Any:
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool(name, arguments or {})

    return asyncio.run(run())


def test_the_tools_call_the_service_and_resolve_a_repository_by_name(monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setattr(mcp_server, "http", _service(seen))

    (listed,) = _tool("list_repos").data
    assert (listed["repo_id"], listed["owner"], listed["name"]) == (4, "pallets", "markupsafe")
    assert listed["commit_sha"] == "b" * 40

    answer = _tool(
        "ask",
        {
            "repo": "pallets/markupsafe",  # resolved from the list, not an id
            "question": "How is tax computed?",
            "conversation_id": CONVERSATION,
        },
    )
    assert answer.data["answer"] == "The rate is in shop/util.py."
    assert answer.data["conversation_id"] == CONVERSATION
    (source,) = answer.data["sources"]
    assert (source["qualname"], source["cited"], source["tier"]) == (
        "shop.util.tax_rate",
        True,
        "symbol",
    )
    assert "excerpt" not in source  # the tool hands over the citable fields only

    info = _tool("repo_info", {"repo": 4})  # an id needs no lookup
    assert (info.data["owner"], info.data["name"]) == ("pallets", "markupsafe")
    assert info.data["suggested_questions"] == ["What does escape() do?"]
    assert info.data["snapshot"]["commit_sha"] == "b" * 40

    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/repos"),  # list_repos
        ("GET", "/repos"),  # the name lookup for ask
        ("POST", "/ask"),
        ("GET", "/repos/4"),
    ]
    assert json.loads(seen[2].content) == {
        "repo_id": 4,
        "question": "How is tax computed?",
        "conversation_id": CONVERSATION,
    }


def test_an_unknown_name_and_a_service_error_both_reach_the_caller(monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setattr(mcp_server, "http", _service(seen))

    with pytest.raises(ToolError, match="repo_not_found"):
        _tool("ask", {"repo": "octo/nowhere", "question": "Anything?"})
    assert [r.url.path for r in seen] == ["/repos"]  # the lookup failed: nothing was asked

    with pytest.raises(ToolError, match="repo_not_found"):
        _tool("repo_info", {"repo": 9})  # an id the service does not know
