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
    app.state.realtime.predict = lambda payload, **kwargs: {'people': [], 'verdict': 'REVIEW'}
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
    assert RealtimeDetector(tmp_path/'model.pt').backend == 'yolo'
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
    assert (data['backend'], data['model'], data['device']) == ('rtmlib', 'RTMO-m', 'cuda:0')
    assert (data['width'], data['height']) == (640, 360)
    assert data['skeleton']['jointCount'] == 17 and data['skeleton']['handChains']
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


def _jpeg(width=640, height=360):
    buffer = BytesIO()
    Image.new('RGB', (width, height)).save(buffer, format='JPEG')
    return buffer.getvalue()


def test_realtime_hands_are_opt_in_and_cached_per_slot(tmp_path, monkeypatch):
    import algorithms.hands as hands_module
    detector = RealtimeDetector(tmp_path/'model.pt', backend='rtmo')
    detector.model, detector.model_kind = _FakeModel(), 'rtmlib'
    payload = _jpeg()
    assert detector.predict(payload)['hands'] is None
    created = []
    for slot in detector._slots:
        slot.model, slot.model_kind = _FakeModel(), 'rtmlib'

    class _FakeHands:
        def __init__(self, model_path):
            self.model_path = model_path
            created.append(model_path)

        def detect(self, frame):
            assert frame.ndim == 3
            return [{'points': [[.1, .1, .0]] * 21, 'shape': 'CLOSED', 'label': '握拳／抓握候选'}]

    monkeypatch.setattr(hands_module, 'HandLandmarker', _FakeHands)
    data = detector.predict(payload, hands=True)
    assert [hand['shape'] for hand in data['hands']] == ['CLOSED']
    assert data['hands'][0]['points'][0] == [.1, .1, .0]
    for _ in range(2 * detector.pool_size):
        detector.predict(payload, hands=True)
    assert len(created) == 1, '单路检测应复用已加载槽位'
    reserved = []
    try:
        # Reserve busy replicas to exercise the otherwise lazy pool slots.
        for _ in range(detector.pool_size - 1):
            reserved.append(detector._available.get_nowait())
            detector.predict(payload, hands=True)
            detector.predict(payload, hands=True)
    finally:
        for slot in reversed(reserved):
            detector._available.put_nowait(slot)
    assert len(created) == detector.pool_size, '手部模型应按推理槽位缓存复用'


def test_realtime_hands_fail_open_when_model_missing(tmp_path):
    detector = RealtimeDetector(tmp_path/'model.pt', backend='rtmo')
    detector.model, detector.model_kind = _FakeModel(), 'rtmlib'
    data = detector.predict(_jpeg(), hands=True)
    assert data['hands'] is None
    assert data['people'], '手部模型缺失不应影响人体检测结果'


def test_realtime_hands_flag_is_ignored_by_algorithm_tests(tmp_path):
    detector = RealtimeDetector(tmp_path/'model.pt', backend='rtmo')
    detector.model, detector.model_kind = _FakeModel(), 'rtmlib'
    data = detector.predict(_jpeg(), None, ['raise'], hands=True)
    assert data['lab'] is not None and data['hands'] is None


def test_realtime_frame_forwards_hands_query(authenticated, monkeypatch):
    _, app, client = authenticated
    seen = {}

    def fake(payload, config=None, names=None, **kwargs):
        seen.update(kwargs)
        return {'people': [], 'verdict': 'REVIEW', 'hands': None}

    monkeypatch.setattr(app.state.realtime, 'predict', fake)
    headers = {'Content-Type': 'image/jpeg'}
    assert client.post('/api/v1/realtime/frame?hands=1', content=b'test', headers=headers).json()['verdict'] == 'REVIEW'
    assert seen['hands'] is True
    seen.clear()
    assert client.post('/api/v1/realtime/frame', content=b'test', headers=headers).status_code == 200
    assert seen['hands'] is False
