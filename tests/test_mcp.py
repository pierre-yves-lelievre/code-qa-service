"""MCP server tests: both tools through FastMCP's in-memory client, with a mocked service."""

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import app.mcp_server as mcp_server

ANSWER = {
    "conversation_id": "0c2e5d3a-1b4f-4c6d-8e7a-9f0b1c2d3e4f",
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
            "github_url": "https://github.com/octo/py_app/blob/aaa/shop/util.py#L3-L9",
            "cited": True,
        }
    ],
    "retrievers": {},
    "timings": {},
    "tokens": {},
    "snapshot": {},
    "notes": [],
}
REPO = {
    "repo_id": 1,
    "owner": "octo",
    "name": "py_app",
    "url": "https://github.com/octo/py_app",
    "summary": "A tiny shop.",
    "suggested_questions": ["What is the tax rate?"],
    "snapshot": {"commit_sha": "a" * 40, "indexed_at": "2026-09-12T10:00:00Z"},
}
NOT_FOUND = {"detail": "Repository not found.", "code": "repo_not_found"}


def _service(seen: list[httpx.Request]) -> httpx.Client:
    """A client whose requests never leave the test; every request is recorded."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/ask":
            return httpx.Response(200, json=ANSWER)
        if request.url.path == "/repos/1":
            return httpx.Response(200, json=REPO)
        return httpx.Response(404, json=NOT_FOUND)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://service")


def _tool(name: str, arguments: dict[str, Any]) -> Any:
    """Call one tool through the in-memory client."""

    async def run() -> Any:
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(run())


def test_both_tools_call_the_service_and_its_errors_reach_the_caller(monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setattr(mcp_server, "http", _service(seen))

    answer = _tool(
        "ask",
        {
            "repo": 1,
            "question": "How is tax computed?",
            "conversation_id": ANSWER["conversation_id"],
        },
    )
    assert answer.data["answer"] == "The rate is in shop/util.py."
    assert answer.data["conversation_id"] == ANSWER["conversation_id"]
    (source,) = answer.data["sources"]
    assert (source["qualname"], source["cited"], source["tier"]) == (
        "shop.util.tax_rate",
        True,
        "symbol",
    )
    assert "excerpt" not in source  # the tool hands over the citable fields only

    info = _tool("repo_info", {"repo": 1})
    assert (info.data["owner"], info.data["name"]) == ("octo", "py_app")
    assert info.data["suggested_questions"] == ["What is the tax rate?"]
    assert info.data["snapshot"]["commit_sha"] == "a" * 40

    with pytest.raises(ToolError, match="repo_not_found"):
        _tool("repo_info", {"repo": 2})

    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/ask"),
        ("GET", "/repos/1"),
        ("GET", "/repos/2"),
    ]
    assert json.loads(seen[0].content) == {
        "repo_id": 1,
        "question": "How is tax computed?",
        "conversation_id": ANSWER["conversation_id"],
    }
