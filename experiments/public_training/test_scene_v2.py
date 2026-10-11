import numpy as np
import pytest
import torch
from scene_v2 import DenseModel, DensePredictor, StageFilter, filter_stages, predict_members
from scene_runtime import check_sop, run_recording


def test_dense_models_are_causal_and_padding_independent():
    torch.set_num_threads(4)
    torch.manual_seed(23)
    x = torch.randn(2, 100, 74)
    changed = x.clone()
    changed[:, 50:] += 30
    for kind, depth in [('tcn', 7), ('tcn', 8), ('gru', 0)]:
        model = DenseModel(kind, depth).eval()
        with torch.inference_mode():
            full = model(x)[:, :50]
            assert torch.allclose(full, model(changed)[:, :50], atol=1e-5)
            assert torch.allclose(full, model(x[:, :50]), atol=1e-5)


def test_filter_cannot_see_future_and_does_not_enforce_sop_order():
    p = np.eye(5)[np.repeat([1, 3, 2, 4], 30)]
    valid = np.ones(len(p), dtype=bool)
    kwargs = {'alpha': .3, 'hold': 5, 'threshold': .4}
    full = filter_stages(p, valid, **kwargs)
    assert check_sop(full, [1, 2, 3, 4])['sequence'] == [1, 3, 2, 4]
    assert not check_sop(full, [1, 2, 3, 4])['observed_in_order']
    for end in [20, 47, 90]:
        assert np.array_equal(full[:end], filter_stages(p[:end], valid[:end], **kwargs))
    valid[45:60] = False
    result = filter_stages(p, valid, **kwargs)
    assert np.all(result[45:60] == -1)
    assert np.all(result[60:64] == -1)


def make_member(kind):
    architecture = {'kind': kind, 'depth': 7 if kind == 'tcn' else 0, 'width': 12}
    return {'architecture': architecture, 'state_dict': DenseModel(**architecture).state_dict(),
            'mean': [0.] * 74, 'std': [1.] * 74}


@pytest.mark.parametrize('kind', ['tcn', 'gru'])
def test_streaming_weights_match_full_recording_and_reset(kind):
    torch.set_num_threads(4)
    torch.manual_seed(42)
    members = [make_member(kind)]
    x = np.random.default_rng(42).normal(size=(640, 74)).astype(np.float32)
    full = predict_members(members, x)
    stream = DensePredictor(members)
    boundaries = [0, 1, 8, 137, 141, 402, 511, 640]
    actual = np.concatenate([stream.push(x[a:b]) for a, b in zip(boundaries, boundaries[1:])])
    assert np.allclose(actual, full, atol=1e-6)
    assert stream.push(np.empty((0, 74))).shape == (0, 5)
    stream.reset()
    assert np.array_equal(stream.push(x), full)


@pytest.mark.parametrize('depth', [0, 3, 7])
def test_layer_cache_matches_independent_full_forward_and_is_bounded(depth):
    torch.set_num_threads(4)
    torch.manual_seed(52)
    model = DenseModel('tcn', depth, width=12).eval()
    # Nonzero LayerNorm bias catches caching raw rather than normalized padding.
    for block in model.core:
        torch.nn.init.uniform_(block.norm.bias, -.4, .4)
    member = {'architecture': {'kind': 'tcn', 'depth': depth, 'width': 12},
              'state_dict': model.state_dict(), 'mean': [.2]*74, 'std': [1.3]*74}
    x = np.random.default_rng(52).normal(size=(530, 74)).astype(np.float32)
    with torch.inference_mode():
        normalized = torch.tensor(np.clip((x-np.array(member['mean'])) / np.array(member['std']), -10, 10),
                                  dtype=torch.float32)[None]
        expected = model(normalized)[0].softmax(-1).numpy()
    predictor = DensePredictor([member])
    actual = np.concatenate([predictor.push(row[None]) for row in x])
    assert np.allclose(actual, expected, atol=1e-6)
    assert [tuple(state.shape) for state in predictor.states[0]] == [
        (1, 12, block.padding) for block in model.core]
    # Bad or empty chunks cannot consume history or change the next result.
    previous = predictor.states[0]
    with pytest.raises(ValueError):
        predictor.push(np.full((1, 74), np.nan))
    predictor.push(np.empty((0, 74)))
    assert predictor.states[0] is previous
    predictor.reset()
    assert np.allclose(predictor.push(x), expected, atol=1e-6)


@pytest.mark.skipif(not torch.cuda.is_available(), reason='CUDA unavailable')
def test_cuda_layer_cache_matches_full_forward_across_chunk_boundaries():
    torch.manual_seed(53)
    member = make_member('tcn')
    model = DenseModel(**member['architecture']).cuda().eval()
    model.load_state_dict(member['state_dict'])
    x = np.random.default_rng(53).normal(size=(400, 74)).astype(np.float32)
    with torch.inference_mode():
        expected = model(torch.from_numpy(x).cuda()[None])[0].softmax(-1).cpu().numpy()
    predictor = DensePredictor([member], 'cuda')
    boundaries = [0, 1, 9, 137, 266, 400]
    actual = np.concatenate([predictor.push(x[a:b]) for a, b in zip(boundaries, boundaries[1:])])
    assert np.allclose(actual, expected, atol=1e-6)


def test_filter_chunk_boundary_and_transient_competitor():
    p = np.eye(5)[[1, 1, 1, 2, 2, 1, 3, 3, 3, 3, 4, 4, 4, 4]]
    valid = np.ones(len(p), dtype=bool)
    valid[9] = False
    params = {'alpha': 1., 'hold': 3, 'threshold': .6}
    expected = [-1, -1, 1, 1, 1, 1, 1, 1, 3, -1, -1, -1, 4, 4]
    assert filter_stages(p, valid, **params).tolist() == expected
    post = StageFilter(**params)
    actual = np.concatenate([post.push(p[:4], valid[:4]), post.push(p[4:11], valid[4:11]),
                             post.push(p[11:], valid[11:])])
    assert actual.tolist() == expected
    post.reset()
    assert post.push(p[:1], valid[:1]).tolist() == [-1]


def test_unknown_never_confirms_and_recovery_requires_fresh_evidence():
    p = np.vstack([np.eye(5)[[1, 1, 1]], np.full((1, 5), .2), np.eye(5)[[2, 2, 2]]])
    assert filter_stages(p, np.ones(7), hold=3, threshold=.6).tolist() == [-1, -1, 1, -1, -1, -1, 2]


@pytest.mark.parametrize('params', [{'hold': 1.5}, {'hold': True}, {'hold': 0}, {'alpha': float('nan')},
                                   {'alpha': 0}, {'threshold': float('inf')}, {'threshold': -1}])
def test_invalid_filter_parameters_rejected(params):
    with pytest.raises(ValueError):
        StageFilter(**params)


@pytest.mark.parametrize('p,valid', [(np.eye(5), [1]), (np.eye(5), np.ones((5, 1))),
    (np.eye(5), [1, 1, 1, 1, 2]), (np.full((5, 5), np.nan), np.ones(5)),
    (np.ones((5, 5)), np.ones(5)), (np.ones((5, 4)) / 4, np.ones(5))])
def test_malformed_probabilities_and_validity_cannot_silently_truncate(p, valid):
    with pytest.raises(ValueError):
        filter_stages(p, valid)


@pytest.mark.parametrize('stages,steps,hz', [([1], [1, 1], 10), ([1], [1.5], 10),
    ([1], [True], 10), ([1], [1], 0), ([1], [1], float('nan')), ([1.5], [1], 10)])
def test_sop_configuration_validation(stages, steps, hz):
    with pytest.raises(ValueError):
        check_sop(stages, steps, hz)


@pytest.mark.parametrize('filter_kind', ['ema-persistence-v2', 'confidence-evidence-v1'])
def test_runtime_without_annotations_matches_batch_and_custom_sample_rate(tmp_path, filter_kind):
    import json
    import subprocess
    import sys
    from pathlib import Path
    torch.manual_seed(13)
    checkpoint = {'format': 'dense-causal-ensemble-v2', 'input_dim': 74,
                  'members': [make_member('tcn'), make_member('gru')]}
    config = {'steps': [1, 2, 3, 4], 'sample_hz': 5, 'checkpoint_file': 'stage-ensemble.pt',
              'filter_kind': 'ema-persistence-v2', 'filter_parameters': {'alpha': .3, 'hold': 3, 'threshold': 0}}
    if filter_kind == 'confidence-evidence-v1':
        config.update(filter_kind=filter_kind, filter_parameters={'alpha': .3, 'budget': .3, 'threshold': 0., 'margin': .1})
    x = np.random.default_rng(13).normal(size=(90, 74)).astype(np.float32)
    valid = np.ones(90, dtype=bool)
    valid[30:35] = False
    expected = run_recording(checkpoint, x, valid, config, 90)
    torch.save(checkpoint, tmp_path/'stage-ensemble.pt')
    (tmp_path/'workflow.json').write_text(json.dumps(config))
    np.savez(tmp_path/'input.npz', x=x, input_valid=valid)
    subprocess.run([sys.executable, str(Path(__file__).with_name('scene_runtime.py')),
                    '--bundle', str(tmp_path), '--input', str(tmp_path/'input.npz'),
                    '--output', str(tmp_path/'output.json'), '--chunk-size', '7'], check=True, capture_output=True)
    actual = json.loads((tmp_path/'output.json').read_text())
    assert actual['stages'] == expected['stages']
    assert actual['raw'] == expected['raw']
    assert actual['sop'] == expected['sop']
    assert np.allclose(actual['confidence'], expected['confidence'], atol=1e-6)
    assert actual['stages'][30:35] == [-1]*5
    for event in actual['sop']['events']:
        assert event['seconds'] == event['frame']/5
    for bad_valid in [valid[:-1], valid[:, None], np.full(90, 2)]:
        with pytest.raises(ValueError):
            run_recording(checkpoint, x, bad_valid, config)
