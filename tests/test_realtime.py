from io import BytesIO
from PIL import Image
import pytest
from fastapi import HTTPException
from backend.realtime import RealtimeDetector


def test_realtime_requires_login(context):
    _, _, client = context
    assert client.post('/api/v1/realtime/frame', content=b'x').status_code == 401


def test_realtime_bounds_and_csrf(authenticated):
    _, app, client = authenticated
    url = '/api/v1/realtime/frame'
    assert client.post(url, content=b'x').status_code == 415
    headers = {'Content-Type': 'image/jpeg'}
    assert client.post(url, content=b'x'*(1024*1024+1), headers=headers).status_code == 413
    assert client.post(url, content=b'invalid', headers=headers).status_code == 422
    data = BytesIO()
    Image.new('RGB', (1281, 720)).save(data, format='JPEG')
    assert client.post(url, content=data.getvalue(), headers=headers).status_code == 422
    app.state.realtime.predict = lambda payload: {'people': [], 'verdict': 'REVIEW'}
    assert client.post(url, content=b'test', headers=headers).json()['verdict'] == 'REVIEW'
    client.headers.pop('X-CSRF-Token')
    assert client.post(url, content=b'test', headers=headers).status_code == 403


def test_realtime_busy_does_not_queue(tmp_path):
    detector = RealtimeDetector(tmp_path/'model.pt')
    detector.lock.acquire()
    try:
        with pytest.raises(HTTPException) as exc:
            detector.predict(b'x')
        assert exc.value.status_code == 429
    finally:
        detector.lock.release()


class _FakeBoxes:
    def __init__(self, xyxy, conf):
        self.xyxy, self.conf = xyxy, conf


class _FakeKeypoints:
    def __init__(self, data):
        self.data = data


class _FakeResult:
    def __init__(self, xyxy, conf, keypoints):
        self.boxes, self.keypoints = _FakeBoxes(xyxy, conf), _FakeKeypoints(keypoints)
        self.speed = {'inference': 1.0}


class _FakeModel:
    """替代 RTMLibDetector：同样的 predict() 契约，不需要 CUDA/ONNX。"""
    label = 'RTMO-m'
    _device = 'cuda'

    def __init__(self):
        self.calls = 0

    def predict(self, frame, **kwargs):
        import torch
        self.calls += 1
        return [_FakeResult(torch.tensor([[64., 36., 320., 324.]]), torch.tensor([.9]),
                            torch.tensor([[[32., 18., .9]] * 17]))]


def test_realtime_backend_defaults_and_validation(monkeypatch, tmp_path):
    monkeypatch.delenv('REALTIME_BACKEND', raising=False)
    assert RealtimeDetector(tmp_path/'model.pt').backend == 'rtmo'
    monkeypatch.setenv('REALTIME_BACKEND', 'YOLO')
    assert RealtimeDetector(tmp_path/'model.pt').backend == 'yolo'
    assert RealtimeDetector(tmp_path/'model.pt', backend='rtmpose').backend == 'rtmpose'
    with pytest.raises(ValueError):
        RealtimeDetector(tmp_path/'model.pt', backend='mmpose')


def test_realtime_predict_reports_rtmo_backend(tmp_path):
    detector = RealtimeDetector(tmp_path/'model.pt', backend='rtmo')
    detector.model, detector.model_kind = _FakeModel(), 'rtmlib'
    buffer = BytesIO()
    Image.new('RGB', (640, 360)).save(buffer, format='JPEG')
    data = detector.predict(buffer.getvalue())
    assert (data['backend'], data['model'], data['device']) == ('rtmlib', 'RTMO-m', 'cuda')
    assert (data['width'], data['height']) == (640, 360)
    assert len(data['people']) == 1 and data['people'][0]['trackId'] == 1
    assert detector.model.calls == 1


def test_realtime_missing_model_and_warmup(tmp_path):
    detector = RealtimeDetector(tmp_path/'missing.pt', backend='yolo')
    buffer = BytesIO()
    Image.new('RGB', (320, 180)).save(buffer, format='JPEG')
    with pytest.raises(HTTPException) as exc:
        detector.predict(buffer.getvalue())
    assert exc.value.status_code == 503
    assert detector.warmup() is False
