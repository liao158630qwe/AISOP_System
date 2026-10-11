import numpy as np
import pytest

from evidence_filter import EvidenceFilter
from scene_runtime import check_sop


def test_evidence_is_causal_chunk_independent_and_does_not_force_order():
    sequence = [3, 1, 2, 1, 4]
    p = np.eye(5)[np.repeat(sequence, 60)]
    valid = np.ones(len(p), dtype=bool)
    params = {'alpha': .3, 'budget': 2., 'threshold': .4, 'margin': .1}
    full = EvidenceFilter(**params).push(p, valid)
    assert check_sop(full, [1, 2, 3, 4])['sequence'] == sequence
    stream = EvidenceFilter(**params)
    boundaries = [0, 1, 31, 67, 129, 180, 300]
    chunks = [stream.push(p[a:b], valid[a:b]) for a, b in zip(boundaries, boundaries[1:])]
    assert np.array_equal(np.concatenate(chunks), full)
    for end in boundaries[1:]:
        assert np.array_equal(EvidenceFilter(**params).push(p[:end], valid[:end]), full[:end])
    stream.reset()
    assert np.array_equal(stream.push(p, valid), full)


def test_strong_evidence_confirms_faster_than_ambiguous_evidence():
    strong = np.eye(5)[[1]*40]
    ambiguous = np.tile([.1, .6, .3, 0, 0], (40, 1))
    post = lambda p: EvidenceFilter(alpha=1., budget=2., threshold=.4).push(p, np.ones(len(p)))
    assert np.flatnonzero(post(strong) == 1)[0] < np.flatnonzero(post(ambiguous) == 1)[0]
    spike = np.eye(5)[[2]+[1]*30+[3, 3]+[1]*30]
    assert check_sop(EvidenceFilter(alpha=1., budget=3.).push(spike, np.ones(len(spike))), [1, 2, 3, 4])['sequence'] == [1]


def test_invalid_and_low_confidence_clear_all_pending_confirmation():
    p = np.eye(5)[[1]*12]
    valid = np.ones(12, dtype=bool)
    valid[5] = False
    p[9] = .2
    actual = EvidenceFilter(alpha=1., budget=3., threshold=.4).push(p, valid)
    assert actual.tolist() == [-1, -1, 1, 1, 1, -1, -1, -1, 1, -1, -1, -1]


def test_filter_treats_stage_ids_symmetrically():
    rng = np.random.default_rng(88)
    p = rng.dirichlet(np.ones(5), size=200)
    order = np.array([3, 4, 0, 2, 1])
    a = EvidenceFilter(alpha=.3, budget=.3, threshold=.1).push(p, np.ones(200))
    b = EvidenceFilter(alpha=.3, budget=.3, threshold=.1).push(p[:, order], np.ones(200))
    mapped = np.full(200, -1)
    mapped[b >= 0] = order[b[b >= 0]]
    assert np.array_equal(a, mapped)


@pytest.mark.parametrize('parameters', [{'alpha': 0}, {'alpha': float('nan')}, {'budget': 0},
    {'budget': float('inf')}, {'margin': 1}, {'threshold': -1}])
def test_invalid_parameters_rejected(parameters):
    with pytest.raises(ValueError):
        EvidenceFilter(**parameters)


@pytest.mark.parametrize('p,v', [(np.eye(5), [1]), (np.eye(5), [[1]]*5),
    (np.eye(5), [0, 0, 0, 0, 2]), (np.full((5, 5), np.nan), np.ones(5)), (np.ones((5, 5)), np.ones(5))])
def test_invalid_probabilities_or_masks_rejected(p, v):
    with pytest.raises(ValueError):
        EvidenceFilter().push(p, v)
