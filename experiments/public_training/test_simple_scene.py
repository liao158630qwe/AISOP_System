import numpy as np
from scene_runtime import stabilize, check_sop


def test_sop_detects_skip_swap_repeat_and_missing():
    steps = [1, 2, 3, 4]
    assert check_sop([0, 1, 1, -1, 2, 3, 4], steps)['observed_in_order']
    skipped = check_sop([1, 3, 4], steps)
    assert skipped['missing_at_end'] == [2]
    assert skipped['events'][1]['status'] == 'out_of_order'
    assert not check_sop([1, 3, 2, 4], steps)['observed_in_order']
    assert check_sop([1, 2, 1, 3, 4], steps)['events'][2]['status'] == 'repeat'
    assert check_sop([], steps)['missing_at_end'] == steps
    assert check_sop([2, 1], [2, 1])['observed_in_order']


def test_smoothing_causal_and_invalid_input_cannot_confirm():
    probability = np.eye(5)[[1, 1, 2, 2, 2, 3, 3]]
    valid = np.ones(7, dtype=bool)
    full = stabilize(probability, valid, .6, 3)
    for length in range(1, 8):
        assert np.array_equal(full[:length], stabilize(probability[:length], valid[:length], .6, 3))
    assert full[3] == -1 and full[4] == 2
    valid[4] = False
    assert stabilize(probability, valid, .6, 3)[4] == -1
    assert stabilize(np.ones((10, 5))*.2, np.ones(10), .6, 3).tolist() == [-1]*10
