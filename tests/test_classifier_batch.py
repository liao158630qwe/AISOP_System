import numpy as np
import pytest
import torch

from algorithms.gpu_classifier import CudaSVC


@pytest.mark.skipif(not torch.cuda.is_available(), reason='CUDA probability equivalence requires a GPU')
def test_independent_site_models_keep_probabilities_and_row_order_when_combined(monkeypatch):
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC
    models, probes, expected = [], [], []
    rng = np.random.default_rng(13)
    # Mixed class counts, feature widths, row counts, and two distinct models
    # sharing a coupling group exercise result splitting and convergence masks.
    for index, classes in enumerate([3, 2, 3, 5]):
        width = index + 3
        x = np.concatenate([rng.normal(i, .8, (20, width)) for i in range(classes)]).astype(np.float32)
        labels = np.repeat(np.arange(classes), 20)
        source = make_pipeline(StandardScaler(), SVC(probability=True, random_state=0)).fit(x, labels)
        probe = rng.uniform(-2, classes+1, (index+2, width)).astype(np.float32)
        expected.append(source.predict_proba(probe))
        monkeypatch.setattr(source, 'predict_proba', lambda *args: pytest.fail('CPU prediction during serving'))
        models.append(CudaSVC(source))
        probes.append(probe)
    individual = [model.predict_proba(probe) for model, probe in zip(models, probes)]
    combined = CudaSVC.predict_many(models, probes)
    device_combined=CudaSVC.predict_many(models,[torch.as_tensor(probe,device='cuda') for probe in probes])
    for cpu_input,gpu_input in zip(combined,device_combined):
        np.testing.assert_allclose(cpu_input,gpu_input,atol=1e-12,rtol=1e-12)
    for actual, separate, reference in zip(combined, individual, expected):
        np.testing.assert_allclose(actual, separate, atol=1e-12, rtol=1e-12)
        np.testing.assert_allclose(actual, reference, atol=2e-6, rtol=2e-6)
        np.testing.assert_array_equal(actual.argmax(1), reference.argmax(1))


def test_batch_rejects_missing_models_and_invalid_features_before_cuda():
    assert CudaSVC.predict_many([], []) == []
    with pytest.raises(ValueError, match='count'):
        CudaSVC.predict_many([], [np.zeros((1, 2))])
    model = object.__new__(CudaSVC)
    model.n_features_in_ = 2
    with pytest.raises(ValueError, match='contract'):
        model._pairwise_probabilities(np.zeros((1, 3)))
    for value in [np.inf, np.nan, -np.inf]:
        with pytest.raises(ValueError, match='finite'):
            model._pairwise_probabilities([[0., value]])


@pytest.mark.skipif(not torch.cuda.is_available(), reason='CUDA feature validation requires a GPU')
@pytest.mark.parametrize('invalid', [np.inf, np.nan, -np.inf])
def test_batch_checks_all_gpu_sites_before_running_any_classifier(monkeypatch, invalid):
    model = object.__new__(CudaSVC)
    model.n_features_in_ = 2
    monkeypatch.setattr(model, '_pairwise_probabilities', lambda *args, **kwargs:
                        pytest.fail('invalid batch must fail before classification'))
    inputs = [torch.zeros((1, 2), device='cuda'),
              torch.tensor([[1., invalid]], device='cuda')]
    with pytest.raises(ValueError, match='finite'):
        CudaSVC.predict_many([model, model], inputs)
