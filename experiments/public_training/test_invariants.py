"""Small, meaningful checks for leakage and temporal metric behavior."""
import numpy as np
import torch

from common import temporal_metrics
from train_sop import SOPModel
from train_temporal import TemporalModel, Windows


def test_windows_never_cross_recordings():
    arrays = {'a': (np.ones((4, 74), np.float32), np.zeros(4, np.int64)),
              'b': (np.full((5, 74), 7, np.float32), np.zeros(5, np.int64))}
    windows = Windows(['a', 'b'], arrays, np.zeros(74), np.ones(74), 'cpu')
    second_first = windows.ranges[1][1][0]
    x, _ = windows.batch([second_first])
    assert torch.count_nonzero(x[:, :-1]) == 0
    assert torch.all(x[:, -1] == 7)


def test_segment_metrics_penalize_fragmentation_and_label_errors():
    gt = [np.array([0, 0, 0, 0, 1, 1, 1, 1])]
    perfect = temporal_metrics(gt, gt)
    fragmented = temporal_metrics(gt, [np.array([0, 1, 0, 0, 1, 1, 1, 1])])
    wrong = temporal_metrics(gt, [np.array([2]*8)])
    assert perfect['edit_mean'] == perfect['segment_f1_at_0.5'] == 1
    assert fragmented['edit_mean'] < perfect['edit_mean']
    assert fragmented['segment_f1_at_0.5'] < 1
    assert wrong['segment_f1_at_0.5'] == 0


def test_temporal_models_cannot_see_future():
    torch.manual_seed(1)
    x = torch.randn(2, 64, 74)
    altered = x.clone()
    altered[:, 32:] += 20
    for kind in ['frame_mlp', 'tcn', 'transformer']:
        model = TemporalModel(kind).eval()
        with torch.inference_mode():
            assert torch.allclose(model(x)[:, :32], model(altered)[:, :32], atol=1e-6)


def test_sop_models_cannot_see_future():
    torch.manual_seed(2)
    tokens = torch.randint(0, 5, (2, 10, 3))
    numeric = torch.randn(2, 10, 1)
    altered = tokens.clone()
    altered[:, 5:] = 1
    for kind in ['event_mlp', 'gru']:
        model = SOPModel(kind, 5, 5).eval()
        with torch.inference_mode():
            assert torch.allclose(model(tokens, numeric)[:, :5], model(altered, numeric)[:, :5], atol=1e-6)
