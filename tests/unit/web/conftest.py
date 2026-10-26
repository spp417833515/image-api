"""The app in-process (FastAPI TestClient, lifespan included) over a fake backend."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# The fake backend and image helpers are shared with the business-layer tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "biz"))

from biz_fakes import FakeBackend, make_service  # noqa: E402

from image_api.web.app import create_app  # noqa: E402


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def service(backend, tmp_path):
    return make_service(backend, tmp_path)


@pytest.fixture
def client(service):
    with TestClient(create_app(service)) as client:
        yield client


@pytest.fixture
def wait_for_job(client):
    return lambda job_id: poll_job(client, job_id)


def poll_job(client: TestClient, job_id: str, timeout_s: float = 10.0) -> list[dict]:
    """Every job body seen while polling, the finished one last."""
    seen = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        seen.append(response.json())
        if seen[-1]["state"] in ("done", "failed"):
            return seen
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish in {timeout_s} s: {seen[-1]}")
