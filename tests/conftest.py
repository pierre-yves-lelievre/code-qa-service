import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sklearn.ensemble import RandomForestRegressor

import app.api as api_module
from app.main import app


def _fast_train(features, targets):
    """10-tree forest for test speed; matches production contract exactly."""
    X, y = np.array(features), np.array(targets)
    model = RandomForestRegressor(n_estimators=10, random_state=42)
    model.fit(X, y)
    return model


@pytest.fixture
def tmp_models_dir(tmp_path: Path) -> Path:
    return tmp_path / "models"


@pytest.fixture
def client(tmp_models_dir: Path, monkeypatch: pytest.MonkeyPatch):
    # Point settings at the temp dir so tests never touch ./models
    monkeypatch.setattr("app.config.settings.models_dir", tmp_models_dir)

    # Reset singleton caches so each test gets a fresh ModelStore
    api_module.get_store.cache_clear()
    # JobStore is a module-level instance; replace it with a fresh one
    from app.jobs import JobStore

    monkeypatch.setattr(api_module, "_job_store", JobStore())

    # Use 10-tree model to keep tests fast
    monkeypatch.setattr("app.training.train_model", _fast_train)

    with TestClient(app) as c:
        yield c

    api_module.get_store.cache_clear()


def wait_for_job(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/train/{job_id}").json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")
