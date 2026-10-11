from dataclasses import replace
from pathlib import Path
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.db import initialize


@pytest.fixture
def context(tmp_path):
    model = tmp_path / "model.pt"
    model.write_bytes(b"test-model-not-for-inference")
    settings = Settings("sqlite:///" + str(tmp_path / "test.db"), tmp_path / "data", "admin", "test-password-12345", model, "test", False, lease_seconds=4, pose_backend="yolo")
    app = create_app(settings)
    initialize(app.state.engine)
    with TestClient(app) as client:
        yield settings, app, client
    app.state.engine.dispose()


@pytest.fixture
def authenticated(context):
    settings, app, client = context
    response = client.post("/api/v1/login", json={"username": settings.username, "password": settings.password})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf"]
    return context


@pytest.fixture
def video_bytes(tmp_path):
    path = tmp_path / "sample.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 48))
    assert writer.isOpened()
    for _ in range(10):
        writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
    writer.release()
    return path.read_bytes()


def new_run(client, data, key="test-run", options=None):
    response = client.post("/api/v1/media?filename=sample.avi", content=data, headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 201, response.text
    mid = response.json()["id"]
    response = client.post("/api/v1/analysis-runs", json={"media_id": mid, "options": options or {}}, headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    return response.json()
