"""Shared with surrogate-model-service; adapted: codeqa_test, bare client, rolled-back db."""

import os
from pathlib import Path

ENV_TEST = Path(__file__).parent.parent / ".env.test"
TEST_DATABASE = "codeqa_test"


def _load_env_test() -> None:
    """Put `.env.test` into the environment, over the shell and `.env`, before `app` loads."""
    for line in ENV_TEST.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            os.environ[key.strip()] = value.strip()


_load_env_test()

import itertools  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from contextlib import nullcontext  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import run_migrations  # noqa: E402
from app.jobs import JobStore  # noqa: E402
from app.main import app  # noqa: E402
from app.store import ChunkStore  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to run against any database but codeqa_test, so dev data is never touched."""
    if urlsplit(settings.database_url).path.lstrip("/") != TEST_DATABASE:
        pytest.exit(f"Tests run only against {TEST_DATABASE}; see .env.test.", returncode=2)


def _git(cwd: Path, *args: str) -> str:
    """Run git in a fixture repo with a fixed identity; the global config is never changed."""
    identity = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.com"]
    quiet = ["-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
    result = subprocess.run(
        ["git", *identity, *quiet, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class FixtureRepo:
    """A git repository built from `tests/fixtures/<name>`, cloneable from `clone_base`."""

    path: Path
    clone_base: str
    owner: str
    name: str

    def commit(self, message: str = "Edit the fixture.") -> str:
        """Commit everything in the working tree and return the new sha."""
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "--allow-empty", "-m", message)
        return self.head()

    def head(self) -> str:
        """The sha of the current commit."""
        return _git(self.path, "rev-parse", "HEAD")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """TestClient with fake providers, no keys, and a temporary data directory."""
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path / "data")
    monkeypatch.setattr("app.config.settings.providers", "fake")
    monkeypatch.setattr("app.config.settings.voyage_api_key", None)
    monkeypatch.setattr("app.config.settings.anthropic_api_key", None)
    monkeypatch.setattr("app.config.settings.github_token", None)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def migrated() -> None:
    """Apply the real migrations once per session, committed, before any db test."""
    run_migrations()


@pytest.fixture
def db(migrated: None) -> Iterator[psycopg.Connection]:
    """A connection inside one transaction that is rolled back when the test ends."""
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        with conn.transaction(force_rollback=True):
            yield conn


@pytest.fixture
def jobs(db: psycopg.Connection) -> JobStore:
    """JobStore whose every unit of work runs inside the test's rolled-back transaction."""
    return JobStore(connect=lambda: nullcontext(db))


@pytest.fixture
def store(db: psycopg.Connection) -> ChunkStore:
    """ChunkStore whose every unit of work runs inside the test's rolled-back transaction."""
    return ChunkStore(connect=lambda: nullcontext(db))


@pytest.fixture
def make_repo(db: psycopg.Connection) -> Callable[[], int]:
    """Factory inserting a distinct `repos` row per call and returning its id."""
    counter = itertools.count(1)

    def _make() -> int:
        """Insert one repo named after the next counter value."""
        name = f"repo{next(counter)}"
        (repo_id,) = db.execute(
            "INSERT INTO repos (owner, name, url) VALUES (%s, %s, %s) RETURNING id",
            ("octo", name, f"https://github.com/octo/{name}"),
        ).fetchone()
        return repo_id

    return _make


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Callable[[str], FixtureRepo]:
    """Factory: `git init` a copy of `tests/fixtures/<name>` as octo/<name> under file://."""

    def _make(name: str) -> FixtureRepo:
        """Copy one fixture tree, initialise it on `main`, and commit it."""
        remote = tmp_path / "remote"
        path = remote / "octo" / name
        shutil.copytree(FIXTURES / name, path)
        _git(path, "init", "-q", "--initial-branch=main")
        repo = FixtureRepo(path=path, clone_base=f"file://{remote}", owner="octo", name=name)
        repo.commit("Initial fixture.")
        return repo

    return _make
