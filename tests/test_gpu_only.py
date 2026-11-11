"""CUDA execution and fail-closed regression tests (no production state writes)."""
from pathlib import Path
import asyncio

import numpy as np
import pytest
import torch

from algorithms.gpu import cuda_index, require_cuda
from algorithms.video import AnalysisError

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('device', ['cpu', 'mps', '-1', '', '0,1', False])
def test_cpu_and_implicit_device_selection_are_rejected(device):
    with pytest.raises(ValueError, match='CUDA'):
        cuda_index(device)


def test_cuda_absence_never_returns_cpu(monkeypatch):
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)
    with pytest.raises(AnalysisError) as error:
        require_cuda()
    assert error.value.code == 'GPU_UNAVAILABLE'
    from backend.realtime import RealtimeDetector
    with pytest.raises(AnalysisError):
        RealtimeDetector(ROOT / 'models/yolo11s-pose.pt').device()
    from algorithms.gpu_classifier import CudaLogisticClassifier
    with pytest.raises(AnalysisError):
        CudaLogisticClassifier()


def test_bad_visible_gpu_index_is_rejected(monkeypatch):
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: True)
    monkeypatch.setattr(torch.cuda, 'device_count', lambda: 1)
    with pytest.raises(AnalysisError):
        require_cuda('cuda:1')


def test_api_startup_fails_before_serving_without_cuda(tmp_path, monkeypatch):
    from backend.app import create_app
    from backend.config import Settings
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)
    app = create_app(Settings('sqlite://', tmp_path, 'admin', 'test-password', tmp_path / 'model.pt'))

    async def startup():
        with pytest.raises(AnalysisError):
            async with app.router.lifespan_context(app):
                pytest.fail('API must not become ready without GPU')

    asyncio.run(startup())


def test_scene_schema_rejects_cpu_and_retains_auto_as_gpu_alias():
    from backend.scenes import SceneDefinition
    # The schema is inspected without requiring a model registration.
    schema = SceneDefinition.model_json_schema()['properties']['device']
    assert 'cpu' not in schema['enum']
    assert schema['default'] == 'cuda'


def test_training_recipe_rejects_cpu_before_dataset_processing(tmp_path):
    from algorithms.training import validate_recipe
    with pytest.raises(ValueError, match='CUDA'):
        validate_recipe(dict(backend='torch', task='feature-classify', dataset='unused.json',
            feature_schema='v1', train={'device': 'cpu'}), tmp_path)


def test_onnx_session_never_constructs_permissive_runtime(monkeypatch):
    ort = pytest.importorskip('onnxruntime')
    pytest.importorskip('rtmlib')
    from rtmlib.tools.base import BaseTool
    from algorithms.rtmlib_pose import RTMLibDetector
    options_seen = []

    class Options:
        def add_session_config_entry(self, key, value):
            options_seen.append((key, value))

    class Session:
        def __init__(self, path, *, sess_options, providers):
            assert providers == [('CUDAExecutionProvider', {'device_id': 0})]
            self.disabled = False
        def disable_fallback(self):
            self.disabled = True
        def get_providers(self):
            return ['CUDAExecutionProvider']

    class Model(BaseTool):
        def __init__(self, path, **kwargs):
            super().__init__(path, **kwargs)
            self.subclass_initialized = True
        def __call__(self, image):
            return image

    monkeypatch.setattr(ort, 'get_available_providers', lambda: ['CUDAExecutionProvider'])
    monkeypatch.setattr(ort, 'SessionOptions', Options)
    monkeypatch.setattr(ort, 'InferenceSession', Session)
    detector = RTMLibDetector.__new__(RTMLibDetector)
    model = detector._build(Model, 'unused.onnx', (32, 32), 'cuda:0')
    assert model.subclass_initialized and model.session.disabled
    assert options_seen == [('session.disable_cpu_ep_fallback', '1')]


@pytest.mark.skipif(not torch.cuda.is_available(), reason='Real CUDA verification requires a GPU')
def test_existing_sticker_weights_keep_calibrated_probabilities_on_cuda(monkeypatch):
    import cv2
    import joblib
    import json
    from algorithms.gpu_classifier import CudaSVC
    from algorithms.sticker_actions import action_crop, action_features
    config = json.loads((ROOT / 'config/sticker-inspection.json').read_text(encoding='utf-8'))
    bundle = joblib.load(ROOT / config['model'])
    reference = cv2.imread(str(ROOT / config['reference']))
    paths = sorted((ROOT / 'outputs/sticker-inspection-2026-10-17/action-frames').glob('*.jpg'))
    if not paths:
        pytest.skip('Local recorded video samples are unavailable')
    images = [reference] + [cv2.imread(str(path)) for path in paths[::max(1, len(paths)//20)]]
    for target in config['targets']:
        source = bundle['sites'][target['id']]
        values = np.stack([action_features(action_crop(image, target), action_crop(reference, target)) for image in images])
        expected = source.predict_proba(values)  # Reference only, outside serving code.
        monkeypatch.setattr(source, 'predict_proba', lambda *args: pytest.fail('CPU model inference'))
        model = CudaSVC(source)
        assert model.support.device.type == 'cuda'
        actual = model.predict_proba(values)
        np.testing.assert_allclose(actual, expected, atol=2e-6, rtol=2e-6)
        np.testing.assert_array_equal(actual.argmax(1), expected.argmax(1))


@pytest.mark.skipif(not torch.cuda.is_available(), reason='Real CUDA verification requires a GPU')
def test_gpu_feature_fit_serialization_and_evaluation(tmp_path):
    import joblib
    from algorithms.gpu_classifier import CudaLogisticClassifier
    rng = np.random.default_rng(42)
    x = np.r_[rng.normal(-2, .2, (16, 3)), rng.normal(2, .2, (16, 3))]
    y = np.repeat([0, 1], 16)
    model = CudaLogisticClassifier(max_iter=50).fit(x, y)
    assert model.weights.is_cuda
    np.testing.assert_array_equal(model.predict(x), y)
    path = tmp_path / 'model.joblib'
    joblib.dump(model, path)
    restored = joblib.load(path)
    assert restored.weights.is_cuda
    np.testing.assert_allclose(restored.predict_proba(x), model.predict_proba(x))


def test_api_rejects_cpu_training_and_reports_missing_gpu(authenticated, monkeypatch):
    import uuid
    _, app, client = authenticated
    response = client.post('/api/v1/admin/training-runs', json=dict(
        name='CPU rejected', station='station1', recipe=dict(backend='torch',
        task='feature-classify', dataset='unused.json', feature_schema='v1', train={'device': 'cpu'})),
        headers={'Idempotency-Key': str(uuid.uuid4())})
    assert response.status_code == 422 and 'CUDA' in response.json()['message']
    def fail(*args, **kwargs):
        raise AnalysisError('GPU_UNAVAILABLE', 'GPU unavailable; no CPU fallback')
    monkeypatch.setattr(app.state.realtime, 'predict', fail)
    response = client.post('/api/v1/realtime/frame', content=b'frame', headers={'Content-Type': 'image/jpeg'})
    assert response.status_code == 503
    assert response.json()['code'] == 'GPU_UNAVAILABLE'


@pytest.mark.skipif(not torch.cuda.is_available(), reason='Real CUDA verification requires a GPU')
@pytest.mark.parametrize('classes', [2, 3, 5])
def test_svc_binary_and_multiclass_calibration(classes):
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC
    from algorithms.gpu_classifier import CudaSVC
    rng = np.random.default_rng(35)
    x = np.concatenate([rng.normal(i * 3, .4, (18, 4)) for i in range(classes)]).astype(np.float32)
    y = np.repeat(np.arange(classes), 18)
    source = make_pipeline(StandardScaler(), SVC(probability=True, random_state=0)).fit(x, y)
    probe = np.r_[x, rng.uniform(-2, 3 * classes, (20, 4)).astype(np.float32)]
    actual = CudaSVC(source).predict_proba(probe)
    np.testing.assert_allclose(actual, source.predict_proba(probe), atol=2e-6, rtol=2e-6)
