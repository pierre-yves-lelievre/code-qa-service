---
name: add-eval-case
description: Add a question to the golden eval set (evals/golden.json) with a verified expected chunk, without changing code and without spending on API calls.
---

# Add an eval case

The golden set in `evals/golden.json` is pinned to one commit of one repository (`repo`,
`branch`, `commit` at the top of the file). A case states where the answer lives. The runner
checks that chunk against the top five full hits (hit@5), or checks that a `null` case comes back
`not_found`.

## 1. Collect the case

Ask the author for anything missing:

- **question**: as a user would type it.
- **expect**: where the answer lives.
  - Give the repo-relative `path`, plus the `qualname` when one symbol is the answer. The qualname
    matches the chunk's qualname or name exactly, or as a dotted suffix: `authenticate` matches
    `Crud.authenticate`, but `User` never matches `UserBase`.
  - Use `null` only for a question the repository truly cannot answer (a not-found case).
- **proves**: a few words on the retrieval behaviour this case exercises, such as "identifier
  split" or "TS symbols".
- For a follow-up question, collect every **turn** in order. Only the last turn is scored.

One fact per case. If the question has two answers, make two cases.

## 2. Verify the target at the pinned commit

Read `commit` from `evals/golden.json`, then check the file exists and defines the symbol:

```bash
gh api "repos/<owner>/<name>/contents/<path>?ref=<commit>" \
  -H "Accept: application/vnd.github.raw" | grep -nE "(def|class|function|const) <qualname>"
```

These GitHub reads are free. If the path or symbol is not there, **fix the case, never the code**.
Do not change chunking, retrieval or the matcher to make a case pass.

## 3. Append it

Use the next unused integer `id` across `entries` and `conversations`.

- A single question goes in `entries`:
  `{"id": 15, "question": "...", "expect": {"path": "...", "qualname": "..."}, "proves": "..."}`.
  Drop `qualname` for a path-only case, and use `"expect": null` for a not-found case.
- A follow-up goes in `conversations`:
  `{"id": 16, "turns": ["first question", "follow-up"], "expect": {...}, "proves": "..."}`.

## 4. Check the file

```bash
uv run pytest tests/test_evals.py -x
```

This loads the golden file and rejects duplicate ids, a malformed `expect`, empty questions or a
short commit. It makes no API call.

Optionally, `PROVIDERS=fake make eval` indexes the pinned repo with fake vectors (it clones from
GitHub for free) and runs every case with canned answers. It costs nothing, and a target that is
not in the index shows as `absent`. Ignore its hit@5; fake vectors mean nothing.

## 5. Do not run the real eval

Never run `make eval` with `PROVIDERS=real` unless the author asks in this session. When they do,
state the cost first:

- about $0.003 per case with a target (one planner call and one query embedding);
- about $0.05 per `null` case, and per turn before a conversation's last (each is answered).

Add the first index run if the snapshot is not current: about $0.05, printed by the runner before
it indexes.

## Rules

- No string heuristics: the case records where the answer is. It never adds keywords or regexes
  to the code.
- Keep `commit` unless the author asks to move the pin. Moving it means re-verifying every target
  at the new commit.
- Correct the file, not the code.
