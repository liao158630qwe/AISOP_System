from types import SimpleNamespace

from backend.realtime import RealtimeDetector


def test_health_describes_configured_service_without_loading_model(context, monkeypatch):
    _, app, client = context
    detector = app.state.realtime
    monkeypatch.setattr(detector, '_ensure_model', lambda: (_ for _ in ()).throw(AssertionError('must not load')))
    data = client.get('/api/v1/health').json()
    assert data['status'] == 'ready'
    assert data['realtime']['state'] == 'not_loaded'
    assert data['realtime']['model'] == app.state.settings.model_path.stem
    assert data['realtime']['device'] is None
    assert data['capabilities']['personDetection'] is True
    assert data['capabilities']['poseEstimation'] is True
    assert data['capabilities']['actionSequence'] is False
    assert data['capabilities']['screwInspection'] is True
    assert data['capabilities']['productionSop'] is False
    assert detector.metrics_snapshot()['completed'] == 0


def test_health_reports_actual_loaded_backend_and_device(context):
    _, app, client = context
    app.state.realtime.model = SimpleNamespace(label='RTMPose-test', _device='cpu')
    app.state.realtime.model_kind = 'rtmlib'
    data = client.get('/api/v1/health').json()['realtime']
    assert data == {'state': 'loaded', 'model': 'RTMPose-test', 'backend': 'rtmlib', 'device': 'cpu'}


def test_missing_yolo_and_loading_states_are_distinct(tmp_path):
    detector = RealtimeDetector(tmp_path / 'missing.pt', backend='yolo')
    assert detector.status_snapshot()['state'] == 'unavailable'
    lock = detector._slots[0].load_lock
    lock.acquire()
    try:
        assert detector.status_snapshot()['state'] == 'loading'
    finally:
        lock.release()
