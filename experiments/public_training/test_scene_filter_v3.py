import numpy as np
import pytest

from evidence_filter import EvidenceFilter
from scene_filter_v3 import EvidenceDecayFilter, HMMFilter, filter_stages_v3
from scene_runtime import check_sop


def test_decay_filter_is_causal_chunk_independent_and_does_not_force_order():
    sequence = [3, 1, 2, 1, 4]
    p = np.eye(5)[np.repeat(sequence, 60)]
    valid = np.ones(len(p), dtype=bool)
    params = {'alpha': .3, 'budget': 1., 'threshold': 0., 'margin': .1, 'decay': .9}
    full = EvidenceDecayFilter(**params).push(p, valid)
    assert check_sop(full, [1, 2, 3, 4])['sequence'] == sequence
    stream = EvidenceDecayFilter(**params)
    boundaries = [0, 1, 31, 67, 129, 180, 300]
    chunks = [stream.push(p[a:b], valid[a:b]) for a, b in zip(boundaries, boundaries[1:])]
    assert np.array_equal(np.concatenate(chunks), full)
    for end in boundaries[1:]:
        assert np.array_equal(EvidenceDecayFilter(**params).push(p[:end], valid[:end]), full[:end])
    stream.reset()
    assert np.array_equal(stream.push(p, valid), full)


def test_decay_keeps_partial_evidence_across_brief_flips_where_reset_does_not():
    # Three winning frames at margin .3 followed by one losing frame, repeated.
    # Resetting evidence never reaches budget 1.; decaying evidence accumulates.
    block = np.tile([.65, .35, 0, 0, 0], (3, 1)).tolist()+[[.35, .65, 0, 0, 0]]
    p = np.tile(np.asarray(block), (10, 1))
    valid = np.ones(len(p), dtype=bool)
    decayed = EvidenceDecayFilter(alpha=1., budget=1., threshold=0., margin=0., decay=.9).push(p, valid)
    reset = EvidenceFilter(alpha=1., budget=1., threshold=0., margin=0.).push(p, valid)
    assert np.flatnonzero(decayed == 0).size > 0
    assert np.all(reset == -1)


def test_decay_filter_invalid_input_resets_and_low_confidence_clears_pending():
    p = np.eye(5)[[1]*12]
    valid = np.ones(12, dtype=bool)
    valid[5] = False
    p[9] = .2
    actual = EvidenceDecayFilter(alpha=1., budget=3., threshold=.4, decay=.9).push(p, valid)
    # Decayed evidence grows .9, 1.71, 2.44, 3.10, so budget 3 needs four frames.
    assert actual.tolist() == [-1, -1, -1, 1, 1, -1, -1, -1, -1, -1, -1, -1]


def test_decay_filter_treats_stage_ids_symmetrically():
    rng = np.random.default_rng(88)
    p = rng.dirichlet(np.ones(5), size=200)
    order = np.array([3, 4, 0, 2, 1])
    a = EvidenceDecayFilter(alpha=.3, budget=.3, threshold=.1, decay=.9).push(p, np.ones(200))
    b = EvidenceDecayFilter(alpha=.3, budget=.3, threshold=.1, decay=.9).push(p[:, order], np.ones(200))
    mapped = np.full(200, -1)
    mapped[b >= 0] = order[b[b >= 0]]
    assert np.array_equal(a, mapped)


def test_hmm_filter_is_causal_chunk_independent_and_suppresses_spikes():
    spike = np.eye(5)[[1]*100+[3]*2+[1]*100]
    valid = np.ones(len(spike), dtype=bool)
    params = {'p_self': .8, 'epsilon': .4, 'hold': 3, 'threshold': 0.}
    full = HMMFilter(**params).push(spike, valid)
    assert check_sop(full, [1, 2, 3, 4])['sequence'] == [1]
    stream = HMMFilter(**params)
    boundaries = [0, 1, 51, 99, 100, 101, 137, 202]
    chunks = [stream.push(spike[a:b], valid[a:b]) for a, b in zip(boundaries, boundaries[1:])]
    assert np.array_equal(np.concatenate(chunks), full)
    for end in boundaries[1:]:
        assert np.array_equal(HMMFilter(**params).push(spike[:end], valid[:end]), full[:end])


def test_hmm_filter_does_not_force_order_and_confirms_after_hold():
    sequence = [4, 2, 2, 1]
    p = np.eye(5)[np.repeat(sequence, 20)]
    stages = filter_stages_v3(p, np.ones(len(p)), p_self=.6, epsilon=.2, hold=5, threshold=0.)
    assert check_sop(stages, [1, 2, 3, 4])['sequence'] == [4, 2, 1]
    onset = np.flatnonzero(stages == 4)[0]
    assert onset == 4  # four frames of prior mass plus the hold count


def test_hmm_filter_invalid_input_resets_and_low_confidence_clears_pending():
    p = np.eye(5)[[1]*12]
    valid = np.ones(12, dtype=bool)
    valid[5] = False
    p[9:12] = .2
    actual = HMMFilter(p_self=.6, epsilon=.2, hold=3, threshold=.5).push(p, valid)
    # One ambiguous frame keeps a saturated posterior; sustained ambiguity drops it
    # below the threshold, emits unknown and clears the pending confirmation.
    assert actual.tolist() == [-1, -1, 1, 1, 1, -1, -1, -1, 1, 1, -1, -1]


@pytest.mark.parametrize('parameters', [{'p_self': 0}, {'p_self': 1}, {'p_self': float('nan')},
    {'epsilon': -0.1}, {'epsilon': 1}, {'hold': 0}, {'hold': True}, {'threshold': -1}, {'threshold': 2}])
def test_invalid_hmm_parameters_rejected(parameters):
    with pytest.raises(ValueError):
        HMMFilter(**parameters)


@pytest.mark.parametrize('parameters', [{'alpha': 0}, {'alpha': float('nan')}, {'budget': 0},
    {'budget': float('inf')}, {'margin': 1}, {'threshold': -1}, {'decay': 0}, {'decay': 1}, {'decay': float('nan')}])
def test_invalid_decay_parameters_rejected(parameters):
    with pytest.raises(ValueError):
        EvidenceDecayFilter(**parameters)


@pytest.mark.parametrize('p,v', [(np.eye(5), [1]), (np.eye(5), [[1]]*5),
    (np.eye(5), [0, 0, 0, 0, 2]), (np.full((5, 5), np.nan), np.ones(5)), (np.ones((5, 5)), np.ones(5))])
@pytest.mark.parametrize('factory', [HMMFilter, EvidenceDecayFilter])
def test_malformed_inputs_rejected(factory, p, v):
    with pytest.raises(ValueError):
        factory().push(p, v)
