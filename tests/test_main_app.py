"""FastAPI surface: /health and /metrics with a lifespan-managed store."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    with TestClient(app) as c:
        yield c


def test_health_reports_shape(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["scheduler_enabled"] is False
    assert body["active_findings"] == {}
    assert body["last_runs"] == []


def test_metrics_exposes_prometheus_series(client: TestClient) -> None:
    client.get("/health")  # populates the findings gauge
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "seo_agent_active_findings" in r.text
