"""GitHub: repo URL validation, the search and repo API calls, shallow clones, the file walk."""

import os
import stat
import string
import subprocess
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.errors import (
    CloneFailedError,
    GitHubRateLimitedError,
    GitHubRepoNotFoundError,
    GitHubUnavailableError,
    InvalidRepoUrlError,
    RepoTooLargeError,
)
from app.logging_setup import get_logger

log = get_logger(__name__)

HOST = "github.com"
API_BASE = "https://api.github.com"
CLONE_BASE = "https://github.com"
SEARCH_RESULTS = 5
NAME_CHARS = frozenset(string.ascii_letters + string.digits + "_.-")
_API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
_GIT_TIMEOUT_S = 10.0  # for local git commands after the clone

# Directories never walked, and never recorded.
PRUNED_DIRS = frozenset(
    {".git", "node_modules", "vendor", "dist", "build", ".venv", "venv", "__pycache__",
     ".mypy_cache", ".pytest_cache", ".tox", ".next", "target", "coverage"}
)  # fmt: skip
# Files recorded as skipped/ignored: generated, lockfiles, and secrets-shaped (lower-case names).
IGNORED_FILES = (
    "*.min.js", "*.min.css", "*.map",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "cargo.lock",
    "bun.lock", "bun.lockb", "go.sum", "gemfile.lock", "composer.lock", "pipfile.lock", "pdm.lock",
    ".env", ".env.*", "*.pem", "*.key", "id_rsa*",
)  # fmt: skip
KEPT_FILES = frozenset({".env.example"})
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"
SNIFF_BYTES = 8192


@dataclass(frozen=True)
class RepoRef:
    """A validated repository reference; branch is None until the default branch is known."""

    owner: str
    name: str
    branch: str | None


@dataclass(frozen=True)
class RepoHit:
    """One repository search result."""

    full_name: str
    owner: str
    name: str
    description: str | None
    stars: int
    language: str | None
    size_kb: int
    default_branch: str
    url: str
    clone_url: str


@dataclass(frozen=True)
class RepoInfo:
    """A public repository's canonical owner and name, default branch, and size."""

    owner: str
    name: str
    default_branch: str
    size_kb: int


@dataclass(frozen=True)
class WalkEntry:
    """One file found under a clone: its repo-relative path and why it is skipped, if it is."""

    path: str
    abs_path: Path
    size_bytes: int
    skip_reason: str | None


# ── URL validation ────────────────────────────────────────────────────────────


def parse_repo_url(url: str) -> RepoRef:
    """Validate `https://github.com/<owner>/<name>[.git][/tree/<branch>]` into a RepoRef."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise InvalidRepoUrlError() from None
    if parts.scheme != "https" or parts.netloc.lower() != HOST or parts.query or parts.fragment:
        raise InvalidRepoUrlError("Only https://github.com/<owner>/<name> URLs are accepted.")
    segments = parts.path.strip("/").split("/")
    if len(segments) < 2:
        raise InvalidRepoUrlError("The URL must name an owner and a repository.")
    owner, name, *rest = segments
    name = name.removesuffix(".git")
    for part in (owner, name):
        if part in ("", ".", "..") or not set(part) <= NAME_CHARS:
            raise InvalidRepoUrlError("Owner and repository may only use A-Z a-z 0-9 _ . -")
    branch = None
    if rest:
        if rest[0] != "tree" or len(rest) < 2 or not all(rest[1:]):
            raise InvalidRepoUrlError("Only a /tree/<branch> suffix is accepted.")
        branch = "/".join(rest[1:])
        if branch.startswith("-"):
            raise InvalidRepoUrlError("The branch must not start with '-'.")
    return RepoRef(owner=owner, name=name, branch=branch)


# ── Client ────────────────────────────────────────────────────────────────────


class GitHubClient:
    """GitHub REST calls (search, repo metadata) and git shallow clones, with timeouts."""

    def __init__(
        self,
        token: str | None = None,
        *,
        api_base: str = API_BASE,
        clone_base: str = CLONE_BASE,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Build the HTTP client; tests pass a MockTransport and a file:// clone base."""
        headers = dict(_API_HEADERS)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(
            base_url=api_base,
            headers=headers,
            timeout=settings.github_timeout_s,
            transport=transport,
        )
        self._clone_base = clone_base.rstrip("/")

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

    def search(self, q: str) -> list[RepoHit]:
        """Top repositories matching a GitHub search query, at most five."""
        data = self._get("/search/repositories", {"q": q, "per_page": SEARCH_RESULTS})
        hits = [_hit(item) for item in data.get("items", [])[:SEARCH_RESULTS]]
        log.info("github_search_done", q_len=len(q), results=len(hits))
        return hits

    def repo(self, ref: RepoRef) -> RepoInfo:
        """Metadata of a public repository; refuse it over `max_repo_mb` before any clone."""
        data = self._get(f"/repos/{ref.owner}/{ref.name}")
        if data.get("private"):
            raise GitHubRepoNotFoundError()
        owner, name = data["full_name"].split("/", 1)
        info = RepoInfo(
            owner=owner,
            name=name,
            default_branch=data["default_branch"],
            size_kb=int(data.get("size") or 0),
        )
        if info.size_kb > settings.max_repo_mb * 1024:
            raise RepoTooLargeError(f"Repository is over the {settings.max_repo_mb} MB limit.")
        return info

    def shallow_clone(self, ref: RepoRef, dest: Path, timeout: float) -> str:
        """Clone one branch at depth 1 into `dest`, without submodules or LFS; return its sha."""
        if ref.branch is None:
            raise ValueError("The branch must be resolved before cloning.")
        started = time.monotonic()
        url = f"{self._clone_base}/{ref.owner}/{ref.name}"
        _git(
            "clone", "--depth", "1", "--single-branch", "--no-tags",
            "--branch", ref.branch, "--", url, str(dest),
            timeout=timeout,
        )  # fmt: skip
        sha = _git("rev-parse", "HEAD", cwd=dest, timeout=_GIT_TIMEOUT_S).strip()
        size = _checkout_bytes(dest)
        if size > settings.max_repo_mb * 1024 * 1024:
            raise RepoTooLargeError(f"Checkout is over the {settings.max_repo_mb} MB limit.")
        log.info(
            "clone_done",
            seconds=round(time.monotonic() - started, 2),
            bytes=size,
            commit_sha=sha,
        )
        return sha

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET one API path and return its JSON, mapping failures to typed errors."""
        try:
            response = self._http.get(path, params=params)
            if response.status_code == 404:
                raise GitHubRepoNotFoundError()
            if response.status_code in (403, 429):
                raise GitHubRateLimitedError()
            if not response.is_success:
                log.warning("github_request_failed", status=response.status_code)
                raise GitHubUnavailableError()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:  # timeouts, connection errors, bad JSON
            log.warning("github_request_failed", error_type=type(exc).__name__)
            raise GitHubUnavailableError() from None


def _hit(item: dict[str, Any]) -> RepoHit:
    """A RepoHit from one search result item."""
    return RepoHit(
        full_name=item["full_name"],
        owner=item["owner"]["login"],
        name=item["name"],
        description=item.get("description"),
        stars=int(item.get("stargazers_count") or 0),
        language=item.get("language"),
        size_kb=int(item.get("size") or 0),
        default_branch=item["default_branch"],
        url=item["html_url"],
        clone_url=item["clone_url"],
    )


def _git(*args: str, timeout: float, cwd: Path | None = None) -> str:
    """Run one git command with no prompt and no LFS smudge; CloneFailedError on failure."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    try:
        result = subprocess.run(
            ["git", "-c", "advice.detachedHead=false", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=max(timeout, 0.001),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CloneFailedError(f"git {args[0]} timed out after {timeout:.0f} s.") from None
    if result.returncode != 0:
        log.warning("git_failed", command=args[0], returncode=result.returncode)
        raise CloneFailedError()
    return result.stdout


def _checkout_bytes(root: Path) -> int:
    """Bytes of the regular files under a checkout, outside `.git`, not following symlinks."""
    total = 0
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            info = os.lstat(os.path.join(folder, name))
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


# ── File walk ─────────────────────────────────────────────────────────────────


def walk_files(root: Path) -> list[WalkEntry]:
    """Every file under a clone, sorted by path, with its skip reason; pruned dirs are omitted."""
    root = root.resolve()
    entries: list[WalkEntry] = []
    for folder, dirs, files in os.walk(root):  # followlinks=False: symlinked dirs are not entered
        here = Path(folder)
        kept = []
        for name in dirs:
            if name in PRUNED_DIRS:
                continue
            if (here / name).is_symlink():
                entries.append(_skipped(root, here / name, "symlink"))
            else:
                kept.append(name)
        dirs[:] = kept
        entries.extend(_classify(root, here / name) for name in files)
        if len(entries) > settings.max_files:
            raise RepoTooLargeError(f"Repository has more than {settings.max_files} files.")
    return sorted(entries, key=lambda e: e.path)


def _skipped(root: Path, path: Path, reason: str, size: int = 0) -> WalkEntry:
    """A walk entry for a skipped file."""
    return WalkEntry(path.relative_to(root).as_posix(), path, size, reason)


def _classify(root: Path, path: Path) -> WalkEntry:
    """A file's walk entry: its skip reason, cheapest check first, or None when indexable."""
    if path.is_symlink():
        return _skipped(root, path, "symlink")
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        return _skipped(root, path, "outside_root")
    size = resolved.stat().st_size
    name = path.name.lower()
    if name not in KEPT_FILES and any(fnmatchcase(name, p) for p in IGNORED_FILES):
        return _skipped(root, path, "ignored", size)
    if size > settings.max_file_kb * 1024:
        return _skipped(root, path, "too_large", size)
    with resolved.open("rb") as handle:
        head = handle.read(SNIFF_BYTES)
    if head.startswith(LFS_POINTER):
        return _skipped(root, path, "lfs_pointer", size)
    if b"\0" in head:
        return _skipped(root, path, "binary", size)
    return WalkEntry(path.relative_to(root).as_posix(), resolved, size, None)
