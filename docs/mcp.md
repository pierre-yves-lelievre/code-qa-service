# MCP server

`app/mcp_server.py` exposes the service as three MCP tools over stdio, so an assistant can ask
questions about an indexed repository and get answers that cite the code.

It is a thin client: every call goes to the running service over HTTP, and nothing touches the
database or an API key directly. Start the service first (`make run`), and index a repository in
the web UI so it has a snapshot to answer from.

| Tool | Arguments | Returns |
|---|---|---|
| `list_repos` | — | every indexed repository: id, owner, name, url, commit sha, index date |
| `ask` | `repo`, `question`, optional `conversation_id` | the answer, its cited sources with GitHub links, whether it was found, and the conversation id to continue with |
| `repo_info` | `repo` | owner, name, url, the active snapshot's commit and index date, the summary and the suggested questions |

`repo` is either an id (the `?repo=` in the web UI) or an `"owner/name"`, which the server
resolves from `list_repos`; a GitHub URL works too. A name that is not indexed comes back as a
`repo_not_found` error listing no result, so call `list_repos` when in doubt.

```
list_repos()                                    → [{"repo_id": 1, "owner": "pallets", "name": "markupsafe", …}]
repo_info("pallets/markupsafe")                 → the snapshot, summary and suggested questions
ask("pallets/markupsafe", "What does escape() do with an __html__ object?")
ask("pallets/markupsafe", "And what about None?", conversation_id="…")
```

Run it directly with `make mcp`. The service URL comes from `SERVICE_URL` (default
`http://localhost:8000`).

## Claude Code

```bash
claude mcp add code-qa -- uv run --directory /path/to/code-qa-service python -m app.mcp_server
```

Or, to share it with a project, `.mcp.json`:

```json
{
  "mcpServers": {
    "code-qa": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/code-qa-service", "python", "-m", "app.mcp_server"],
      "env": { "SERVICE_URL": "http://localhost:8000" }
    }
  }
}
```

## Cursor

`~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project:

```json
{
  "mcpServers": {
    "code-qa": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/code-qa-service", "python", "-m", "app.mcp_server"],
      "env": { "SERVICE_URL": "http://localhost:8000" }
    }
  }
}
```

## Notes

- With `PROVIDERS=fake` the service answers with canned text and fake vectors, which is enough to
  try the tools without spending anything.
- An error from the service (an unknown repository, a repository with no snapshot) comes back as
  its message and code, not as a stack trace.
