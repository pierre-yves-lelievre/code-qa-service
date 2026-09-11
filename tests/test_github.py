"""GitHub tests: URL validation, API errors over a mock transport, clone and walk on local repos."""

import json
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.errors import (
    CloneFailedError,
    GitHubRateLimitedError,
    GitHubRepoNotFoundError,
    GitHubUnavailableError,
    InvalidRepoUrlError,
    RepoTooLargeError,
)
from app.github import LFS_POINTER, GitHubClient, RepoInfo, RepoRef, parse_repo_url, walk_files

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIXTURES / "github" / "search.json").read_text())


def _client(handler) -> GitHubClient:
    """A client whose API calls are answered by `handler`."""
    return GitHubClient(transport=httpx.MockTransport(handler))


def _repo_json(**overrides) -> dict:
    """A trimmed `GET /repos/{owner}/{name}` body."""
    return {"full_name": "Octo/Demo", "private": False, "default_branch": "main", "size": 120} | (
        overrides
    )


def _write(root: Path, files: dict[str, bytes]) -> None:
    """Write files under root, creating parent directories."""
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)


# ── URL validation ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "ref"),
    [
        ("https://github.com/fastapi/fastapi", ("fastapi", "fastapi", None)),
        ("https://github.com/fastapi/fastapi.git", ("fastapi", "fastapi", None)),
        ("https://github.com/fastapi/fastapi/", ("fastapi", "fastapi", None)),
        ("  https://GitHub.com/Octo/my.repo_1-x  ", ("Octo", "my.repo_1-x", None)),
        ("https://github.com/octo/repo/tree/feature/login", ("octo", "repo", "feature/login")),
    ],
)
def test_repo_url_accepts_github_repository_shapes(url: str, ref: tuple):
    assert parse_repo_url(url) == RepoRef(*ref)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://github.com/octo/repo",
        "git@github.com:octo/repo.git",
        "https://gitlab.com/octo/repo",
        "https://github.com.evil.com/octo/repo",
        "https://user@github.com/octo/repo",
        "https://github.com:8443/octo/repo",
        "https://github.com/octo",
        "https://github.com/octo//repo",
        "https://github.com/../repo",
        "https://github.com/octo/re po",
        "https://github.com/octo/repo;rm",
        "https://github.com/octo/repo?tab=readme",
        "https://github.com/octo/repo#readme",
        "https://github.com/octo/repo/blob/main/app.py",
        "https://github.com/octo/repo/tree/",
        "https://github.com/octo/repo/tree/-upload-pack=touch",
    ],
)
def test_repo_url_rejects_everything_else(url: str):
    with pytest.raises(InvalidRepoUrlError):
        parse_repo_url(url)


# ── API ───────────────────────────────────────────────────────────────────────


def test_search_returns_five_hits_from_the_recorded_response():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the request and answer with the recorded search response."""
        requests.append(request)
        return httpx.Response(200, json=SEARCH)

    hits = _client(handler).search("fastapi")
    assert [h.full_name for h in hits] == [item["full_name"] for item in SEARCH["items"]]
    first, item = hits[0], SEARCH["items"][0]
    assert (first.owner, first.stars, first.language, first.size_kb) == (
        item["owner"]["login"],
        item["stargazers_count"],
        item["language"],
        item["size"],
    )
    assert (first.default_branch, first.url, first.clone_url) == (
        item["default_branch"],
        item["html_url"],
        item["clone_url"],
    )
    (request,) = requests
    assert request.url.path == "/search/repositories"
    assert dict(request.url.params) == {"q": "fastapi", "per_page": "5"}
    assert "authorization" not in request.headers


def test_token_is_sent_as_a_bearer_header():
    headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the Authorization header."""
        headers.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"items": []})

    client = GitHubClient("t0k", transport=httpx.MockTransport(handler))
    assert client.search("x") == []
    assert headers == ["Bearer t0k"]


def test_repo_returns_canonical_name_default_branch_and_size():
    info = _client(lambda request: httpx.Response(200, json=_repo_json())).repo(
        RepoRef("octo", "demo", None)
    )
    assert info == RepoInfo(owner="Octo", name="Demo", default_branch="main", size_kb=120)


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (404, {"message": "Not Found"}, GitHubRepoNotFoundError),
        (200, _repo_json(private=True), GitHubRepoNotFoundError),
        (403, {"message": "API rate limit exceeded"}, GitHubRateLimitedError),
        (429, {}, GitHubRateLimitedError),
        (500, {}, GitHubUnavailableError),
        (200, _repo_json(size=(settings.max_repo_mb + 1) * 1024), RepoTooLargeError),
    ],
)
def test_repo_maps_github_answers_to_typed_errors(status: int, body: dict, error: type):
    client = _client(lambda request: httpx.Response(status, json=body))
    with pytest.raises(error):
        client.repo(RepoRef("octo", "demo", None))


def test_github_timeout_is_github_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        """Fail like an unreachable GitHub."""
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(GitHubUnavailableError):
        _client(handler).search("fastapi")
    with pytest.raises(GitHubUnavailableError):
        _client(handler).repo(RepoRef("octo", "demo", None))


# ── Clone ─────────────────────────────────────────────────────────────────────


def test_shallow_clone_checks_out_the_branch_and_returns_its_sha(fixture_repo, tmp_path: Path):
    repo = fixture_repo("py_app")
    dest = tmp_path / "clone"
    sha = GitHubClient(clone_base=repo.clone_base).shallow_clone(
        RepoRef("octo", "py_app", "main"), dest, timeout=30
    )
    assert sha == repo.head()
    models = (FIXTURES / "py_app" / "shop" / "models.py").read_text()
    assert (dest / "shop" / "models.py").read_text() == models


def test_clone_over_max_repo_mb_is_refused_after_checkout(
    fixture_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = fixture_repo("py_app")
    monkeypatch.setattr("app.config.settings.max_repo_mb", 0)
    with pytest.raises(RepoTooLargeError):
        GitHubClient(clone_base=repo.clone_base).shallow_clone(
            RepoRef("octo", "py_app", "main"), tmp_path / "clone", timeout=30
        )


def test_clone_of_unknown_repo_or_branch_is_clone_failed(fixture_repo, tmp_path: Path):
    client = GitHubClient(clone_base=fixture_repo("py_app").clone_base)
    with pytest.raises(CloneFailedError):
        client.shallow_clone(RepoRef("octo", "missing", "main"), tmp_path / "a", timeout=30)
    with pytest.raises(CloneFailedError):
        client.shallow_clone(RepoRef("octo", "py_app", "nope"), tmp_path / "b", timeout=30)


# ── Walk ──────────────────────────────────────────────────────────────────────


def test_walk_prunes_build_dirs_and_records_skip_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("app.config.settings.max_file_kb", 1)
    root = tmp_path / "clone"
    _write(
        root,
        {
            "app/main.py": b"print(1)\n",
            ".env.example": b"KEY=\n",
            ".env": b"KEY=secret\n",
            ".env.local": b"KEY=secret\n",
            "certs/server.pem": b"-----BEGIN-----\n",
            "deploy.key": b"key\n",
            "id_rsa": b"key\n",
            "web/app.min.js": b"x\n",
            "web/app.js.map": b"{}\n",
            "package-lock.json": b"{}\n",
            "Cargo.lock": b"\n",
            "big.txt": b"x" * 2048,
            "logo.png": b"\x89PNG\r\n\x1a\n\0\0",
            "model.bin": LFS_POINTER + b"\noid sha256:abc\nsize 1\n",
            "node_modules/pkg/index.js": b"x\n",
            "dist/bundle.js": b"x\n",
            "src/__pycache__/m.pyc": b"\0",
            ".git/config": b"[core]\n",
        },
    )
    assert {e.path: e.skip_reason for e in walk_files(root)} == {
        "app/main.py": None,
        ".env.example": None,
        ".env": "ignored",
        ".env.local": "ignored",
        "certs/server.pem": "ignored",
        "deploy.key": "ignored",
        "id_rsa": "ignored",
        "web/app.min.js": "ignored",
        "web/app.js.map": "ignored",
        "package-lock.json": "ignored",
        "Cargo.lock": "ignored",
        "big.txt": "too_large",
        "logo.png": "binary",
        "model.bin": "lfs_pointer",
    }


def test_lockfiles_of_other_ecosystems_are_skipped_as_ignored(tmp_path: Path):
    names = ["bun.lock", "bun.lockb", "go.sum", "Gemfile.lock", "composer.lock", "Pipfile.lock"]
    names.append("pdm.lock")
    root = tmp_path / "clone"
    _write(root, {name: b"x\n" for name in names} | {"go.mod": b"module octo\n"})
    skipped = {e.path: e.skip_reason for e in walk_files(root)}
    assert skipped == dict.fromkeys(names, "ignored") | {"go.mod": None}


def test_walk_skips_symlinks_to_files_and_dirs_outside_the_clone(tmp_path: Path):
    root, outside = tmp_path / "clone", tmp_path / "outside"
    _write(root, {"app.py": b"x = 1\n"})
    _write(outside, {"secret.py": b"TOKEN = 1\n"})
    (root / "link.py").symlink_to(outside / "secret.py")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    entries = walk_files(root)
    assert [(e.path, e.skip_reason) for e in entries] == [
        ("app.py", None),
        ("link.py", "symlink"),
        ("linked", "symlink"),
    ]
    assert all(
        e.abs_path.resolve().is_relative_to(root.resolve())
        for e in entries
        if e.skip_reason is None
    )


def test_walk_refuses_more_than_max_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.settings.max_files", 2)
    _write(tmp_path, {"a.py": b"", "b.py": b"", "c.py": b""})
    with pytest.raises(RepoTooLargeError):
        walk_files(tmp_path)
